import io
import subprocess
import zipfile

import pytest
from fastapi import HTTPException
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import func, select

from app.models import Capture, Job, ProviderUsage
from app.routes.document_import import read_document
from tests.conftest import register


def office(kind="docx", text="Текст из документа"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        if kind == "docx":
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+text+'</w:t></w:r></w:p></w:body></w:document>')
        else:
            archive.writestr("content.xml", '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><text:p>'+text+'</text:p></office:document>')
    return output.getvalue()


def pdf(blank=False, protected=False):
    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    if not blank:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 100 Td (Synthetic document text) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
    if protected:
        writer.encrypt("synthetic-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("kind,source,expected", [
    ("docx", office(), "Текст из документа"), ("odt", office("odt"), "Текст из документа"),
    ("pdf", pdf(), "Synthetic document text"), ("txt", "Привет".encode(), "Привет"),
    ("txt", "Привет".encode("utf-16"), "Привет"), ("txt", "Привет".encode("cp1251"), "Привет"),
    ("rtf", br"{\rtf1\ansi\ansicpg1251 \u1055?\u1088?\u1080?\u1074?\u1077?\u1090?}", "Привет"),
])
def test_preview_extracts_without_capture_or_paid_calls(app, client, kind, source, expected):
    register(client)
    response = client.post("/api/v1/imports/text", files={"file": ("document."+kind, source, "application/octet-stream")})
    assert response.status_code == 200, response.text
    assert expected in response.json()["text"]
    with app.state.sessions() as db:
        for model in (Capture, Job, ProviderUsage):
            assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("kind,source", [("pdf", pdf(blank=True)), ("pdf", pdf(protected=True)),
                                         ("docx", b"broken"), ("pdf", b"html"), ("doc", b"old"),
                                         ("txt", b"binary\0file"), ("txt", b"x" * 12001),
                                         ("docx", office(text="x" * 12001))])
def test_bad_documents_are_rejected(client, kind, source):
    register(client)
    response = client.post("/api/v1/imports/text", files={"file": ("document."+kind, source)})
    assert response.status_code == 422, response.text


def test_access_and_limits(client):
    assert client.post("/api/v1/imports/text", files={"file": ("x.txt", b"text")}).status_code == 401
    register(client)
    assert client.post("/api/v1/imports/text", headers={"X-CSRF-Token": "wrong"}, files={"file": ("x.txt", b"text")}).status_code == 403
    assert client.post("/api/v1/imports/text", files={"file": ("x.pdf", b"x" * (5 * 1024 * 1024 + 65537))}).status_code == 413


def test_child_timeout_and_secret_isolation(monkeypatch):
    monkeypatch.setenv("NOTES_CLOUDRU_API_KEY", "synthetic-must-not-leak")
    monkeypatch.setenv("NOTES_DATABASE_URL", "synthetic-db-secret")
    def timeout(command, **kwargs):
        assert "-I" in command
        assert all(not key.startswith("NOTES_") for key in kwargs["env"])
        assert kwargs["stderr"] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(command, 15)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(HTTPException) as error:
        read_document(b"%PDF-test", "pdf")
    assert error.value.status_code == 422
    # The semaphore is released even when the child times out.
    with pytest.raises(HTTPException):
        read_document(b"%PDF-test", "pdf")
