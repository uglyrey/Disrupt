"""Bounded raster uploads. No external downloads, metadata, SVG or model calls."""

import hashlib
import io
import re
import threading

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select

from app.models import Note, NoteImage, new_id
from app.services import lock_account, owned_note

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_STORED_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_IMAGES_PER_NOTE = 20
MAX_USER_BYTES = 50 * 1024 * 1024
_decode_slot = threading.BoundedSemaphore(1)
IMAGE_BLOCK = re.compile(
    r"^!\[[^\]\r\n]{0,200}\]\(/api/v1/note-images/([0-9a-f-]{36})"
    r"(?:\?align=(?:left|center|right)&width=(?:25|50|75|100))?\)[ \t]*$", re.MULTILINE,
)


class ImageMergeUnavailable(Exception):
    pass


def merge_images(db, notes):
    blocks = [match for note in notes for match in IMAGE_BLOCK.finditer(note.markdown)]
    if not blocks:
        return [], {}
    images = {image.id: image for image in db.scalars(select(NoteImage).where(
        NoteImage.note_id.in_([note.id for note in notes]), NoteImage.id.in_([m[1] for m in blocks]),
    ))}
    return [m for m in blocks if m[1] in images], images


def image_bytes_used(db, user_id):
    return db.scalar(select(func.coalesce(func.sum(NoteImage.byte_size), 0)).join(Note).where(Note.user_id == user_id))


def check_merge_space(db, notes, user_id):
    _, images = merge_images(db, notes)
    return (len(images) <= MAX_IMAGES_PER_NOTE
            and image_bytes_used(db, user_id) + sum(image.byte_size for image in images.values()) <= MAX_USER_BYTES)


def carry_merge_images(db, sources, note):
    """Copy referenced attachments so purging either original never breaks the merged note."""
    blocks, images = merge_images(db, sources)
    if not images:
        return
    if not check_merge_space(db, sources, note.user_id):
        raise ImageMergeUnavailable()
    copies = {key: new_id() for key in images}
    moved = [m[0].replace(m[1], copies[m[1]]) for m in blocks]
    result = IMAGE_BLOCK.sub("", note.markdown).strip() + "\n\n" + "\n\n".join(moved)
    if len(result) > 24000:
        raise ImageMergeUnavailable()
    for key, image in images.items():
        db.add(NoteImage(id=copies[key], note_id=note.id, idempotency_key="merge:"+key,
                         source_sha256=image.source_sha256, media_type=image.media_type,
                         width=image.width, height=image.height, byte_size=image.byte_size, data=image.data))
    note.markdown = result


def _normalize_image(source):
    if not source or len(source) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Картинка должна быть не больше 5 МБ")
    try:
        with Image.open(io.BytesIO(source), formats=("JPEG", "PNG", "WEBP")) as opened:
            if opened.width * opened.height > MAX_PIXELS or max(opened.size) > 16000:
                raise HTTPException(413, "Слишком большое разрешение картинки. Максимум 24 мегапикселя")
            if getattr(opened, "n_frames", 1) != 1:
                raise HTTPException(422, "Нужна обычная картинка JPG, PNG или WebP, без анимации")
            opened.load()  # Reject truncated files before storing anything.
            oriented = ImageOps.exif_transpose(opened)
            alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            converted = oriented.convert("RGBA" if alpha else "RGB")
            converted.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            # A fresh image discards EXIF, GPS, ICC, comments and all source metadata.
            clean = Image.new(converted.mode, converted.size)
            clean.paste(converted)
            output = io.BytesIO()
            clean.save(output, format="WEBP", quality=90, method=4)
            data = output.getvalue()
            if len(data) > MAX_STORED_BYTES:
                raise HTTPException(413, "Картинка слишком большая. Уменьшите её и загрузите снова")
            return data, clean.width, clean.height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "Не удалось прочитать картинку. Загрузите JPG, PNG или WebP") from None


def normalize_image(source):
    # One decoder at a time: a 24 MP phone photo must not multiply memory use on a small VPS.
    if not _decode_slot.acquire(blocking=False):
        raise HTTPException(429, "Сервер обрабатывает другую картинку. Попробуйте через несколько секунд")
    try:
        return _normalize_image(source)
    finally:
        _decode_slot.release()


def image_view(image):
    return {
        "id": image.id, "url": "/api/v1/note-images/" + image.id,
        "width": image.width, "height": image.height, "byte_size": image.byte_size,
    }


def save_image(db, user_id, note_id, key, source):
    owned_note(db, note_id, user_id)
    digest = hashlib.sha256(source).hexdigest()
    # Serializes concurrent uploads and quota checks with note deletion on both databases.
    lock_account(db, user_id)
    owned_note(db, note_id, user_id)
    previous = db.scalar(select(NoteImage).where(NoteImage.note_id == note_id, NoteImage.idempotency_key == key))
    if previous is not None:
        if previous.source_sha256 != digest:
            raise HTTPException(409, "С этим кодом уже загружена другая картинка")
        return image_view(previous)
    count = db.scalar(select(func.count()).select_from(NoteImage).where(NoteImage.note_id == note_id))
    if count >= MAX_IMAGES_PER_NOTE:
        raise HTTPException(429, "В одной заметке можно хранить до 20 картинок, включая историю правок")
    data, width, height = normalize_image(source)
    used = image_bytes_used(db, user_id)
    if used + len(data) > MAX_USER_BYTES:
        raise HTTPException(429, "Лимит картинок 50 МБ. Удалите ненужные заметки из корзины")
    image = NoteImage(note_id=note_id, idempotency_key=key, source_sha256=digest,
                      media_type="image/webp", byte_size=len(data), width=width, height=height, data=data)
    db.add(image)
    db.commit()
    return image_view(image)
