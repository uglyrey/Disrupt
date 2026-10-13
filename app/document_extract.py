"""Standalone, bounded text parser. Run by document_import in a child process."""

import io
import json
import re
import sys
import zipfile

MAX_TEXT = 12000


class InvalidDocument(Exception):
    pass


def checked(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip().lstrip("\ufeff")
    if "\x00" in text:
        raise InvalidDocument("В файле есть двоичные данные. Выберите текстовый документ")
    if len(text) > MAX_TEXT:
        raise InvalidDocument("В документе больше 12 000 знаков. Разбейте его на части")
    return text


def office(source, kind):
    from defusedxml.ElementTree import fromstring

    name = "word/document.xml" if kind == "docx" else "content.xml"
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        members = archive.infolist()
        if len(members) > 1000 or sum(item.file_size for item in members) > 30 * 1024 * 1024:
            raise InvalidDocument("Документ слишком сложный. Сохраните его текст отдельно")
        matches = [item for item in members if item.filename == name]
        if len(matches) != 1 or matches[0].file_size > 4 * 1024 * 1024:
            raise InvalidDocument("Не удалось прочитать структуру документа")
        root = fromstring(archive.read(matches[0]))
    paragraphs = []
    for node in root.iter():
        if node.tag == "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p" and kind == "docx":
            text = ""
            for child in node.iter():
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "t":
                    text += child.text or ""
                elif tag == "tab":
                    text += "\t"
                elif tag in {"br", "cr"}:
                    text += "\n"
            paragraphs.append(text)
        elif kind == "odt" and node.tag in {
            "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p",
            "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}h",
        }:
            paragraphs.append("".join(node.itertext()))
        if sum(map(len, paragraphs)) > MAX_TEXT:
            raise InvalidDocument("В документе больше 12 000 знаков. Разбейте его на части")
    return "\n\n".join(paragraphs)


def extract(source, kind):
    warnings = []
    if kind in {"docx", "odt"}:
        text = office(source, kind)
        warnings.append("Импортирован текст. Оформление и картинки документа не переносятся")
    elif kind == "pdf":
        from pypdf import PdfReader

        if not source.startswith(b"%PDF-"):
            raise InvalidDocument("Файл не является PDF")
        reader = PdfReader(io.BytesIO(source))
        if reader.is_encrypted:
            raise InvalidDocument("PDF защищён паролем. Загрузите копию без защиты")
        if len(reader.pages) > 100:
            raise InvalidDocument("В PDF больше 100 страниц. Разбейте его на части")
        pages = []
        empty = 0
        for page in reader.pages:
            stream = page.get_contents()
            if stream is not None and len(stream.get_data()) > 4 * 1024 * 1024:
                raise InvalidDocument("PDF слишком сложный. Сохраните его текст отдельно")
            value = page.extract_text() or ""
            empty += not value.strip()
            pages.append(value)
            checked("\n\n".join(pages))
        text = "\n\n".join(pages)
        if empty:
            warnings.append("На некоторых страницах нет текста. Картинки и сканы не распознаются")
        warnings.append("Проверьте порядок строк и таблиц после импорта")
    elif kind == "rtf":
        from striprtf.striprtf import rtf_to_text

        if not source.startswith(b"{\\rtf"):
            raise InvalidDocument("Файл не является RTF")
        codepage = re.search(br"\\ansicpg([0-9]{3,5})", source[:256])
        encoding = "cp" + codepage[1].decode("ascii") if codepage else "cp1252"
        text = rtf_to_text(source.decode(encoding), encoding=encoding, errors="strict")
    elif kind in {"txt", "md", "markdown"}:
        if source.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = source.decode("utf-16")
        else:
            try:
                text = source.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = source.decode("cp1251")
    else:
        raise InvalidDocument("Поддерживаются DOCX, PDF, RTF, ODT, TXT и MD. Старый DOC сохраните как DOCX")
    text = checked(text)
    if not text:
        raise InvalidDocument("В документе нет текста. Для сканированного PDF сначала нужно распознавание")
    return {"text": text, "format": kind, "warnings": warnings}


def main():
    if sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    import socket
    def denied(*args, **kwargs):
        raise OSError("Network disabled in document extraction")
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    source = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    try:
        if len(source) > 5 * 1024 * 1024:
            raise InvalidDocument("Файл должен быть не больше 5 МБ")
        result = extract(source, sys.argv[1])
    except InvalidDocument as error:
        result = {"error": str(error)}
    except Exception:
        # Never expose filenames, raw text or parser diagnostics to logs or the response.
        result = {"error": "Не удалось прочитать документ. Проверьте файл или сохраните его как TXT"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
