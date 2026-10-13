"""Bounded raster uploads. No external downloads, metadata, SVG or model calls."""

import hashlib
import io
import re
import threading

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select

from app.models import Capture, NoteImage, new_id

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
    return db.scalar(select(func.coalesce(func.sum(NoteImage.byte_size), 0)).join(Capture).where(Capture.user_id == user_id))


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
        db.add(NoteImage(id=copies[key], note_id=note.id, capture_id=note.capture_id, idempotency_key="merge:"+key,
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


def capture_image_views(db, capture_id):
    return [image_view(image) for image in db.scalars(select(NoteImage).where(
        NoteImage.capture_id == capture_id,
    ).order_by(NoteImage.created_at, NoteImage.id))]


def prepare_images(sources):
    if not 1 <= len(sources) <= MAX_IMAGES_PER_NOTE:
        raise HTTPException(422, "Добавьте от 1 до 20 картинок")
    if sum(map(len, sources)) > 10 * 1024 * 1024:
        raise HTTPException(413, "Все картинки вместе должны быть не больше 10 МБ")
    return [(hashlib.sha256(source).hexdigest(), *normalize_image(source)) for source in sources]


def check_capture_replay(db, capture_id, prepared):
    digests = list(db.scalars(select(NoteImage.source_sha256).where(
        NoteImage.capture_id == capture_id, NoteImage.idempotency_key.like("capture:%"),
    ).order_by(NoteImage.idempotency_key)))
    if digests != [item[0] for item in prepared]:
        raise HTTPException(409, "С этим кодом уже сохранена запись с другими картинками")


def store_capture_images(db, capture, prepared):
    if image_bytes_used(db, capture.user_id) + sum(len(item[1]) for item in prepared) > MAX_USER_BYTES:
        raise HTTPException(429, "Лимит картинок 50 МБ. Удалите ненужные заметки из корзины")
    for index, (digest, data, width, height) in enumerate(prepared):
        db.add(NoteImage(capture_id=capture.id, idempotency_key=f"capture:{index:04d}",
                         source_sha256=digest, media_type="image/webp", data=data,
                         byte_size=len(data), width=width, height=height))
    db.flush()


def adopt_capture_images(db, note):
    images = list(db.scalars(select(NoteImage).where(
        NoteImage.capture_id == note.capture_id, NoteImage.note_id.is_(None),
    ).order_by(NoteImage.idempotency_key)))
    if not images:
        return
    blocks = "\n\n".join(f"![Изображение](/api/v1/note-images/{image.id}?align=center&width=100)" for image in images)
    for image in images:
        image.note_id = note.id
    # Full model output is never truncated. In this rare case the originals remain in the source gallery.
    if len(note.markdown) + len(blocks) + 2 <= 24000:
        note.markdown += "\n\n" + blocks


def save_image(db, user_id, note_id, key, source):
    from app.services import lock_account, owned_note

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
    note = owned_note(db, note_id, user_id)
    image = NoteImage(note_id=note_id, capture_id=note.capture_id, idempotency_key=key, source_sha256=digest,
                      media_type="image/webp", byte_size=len(data), width=width, height=height, data=data)
    db.add(image)
    db.commit()
    return image_view(image)
