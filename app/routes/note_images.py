"""Only the signed-in owner can upload or view note images."""

import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import Response

from app.models import Note, NoteImage
from app.note_images import MAX_UPLOAD_BYTES, prepare_images, save_image
from app.schemas import TextCapture
from app.security import get_login_session
from app.services import capture_text, job_view, owned_capture, owned_note


class NoteImageResponse(BaseModel):
    id: str
    url: str
    width: int
    height: int
    byte_size: int


def build_router(database, settings):
    router = APIRouter(prefix="/api/v1", tags=["note-images"])

    def save_capture(db, user_id, text, mode, key, sources):
        prepared = prepare_images(sources)
        result = capture_text(db, user_id, text, key, settings, processing_mode=mode, images=prepared)
        if isinstance(result, Note):
            saved = {"capture_id": result.capture_id, "job_id": None, "status": "saved", "note_id": result.id}
            if mode == "ai":
                saved["ai_limit_exceeded"] = True
            return saved
        return job_view(db, result)

    @router.post("/captures/with-images", status_code=202)
    async def create_with_images(request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", key):
            raise HTTPException(422, "Нужен код повторной загрузки")
        async with request.form(max_files=20, max_fields=2, max_part_size=48000) as form:
            if any(name not in {"text", "processing_mode", "images"} for name, _ in form.multi_items()):
                raise HTTPException(422, "Неизвестное поле записи")
            if len(form.getlist("text")) > 1 or len(form.getlist("processing_mode")) > 1:
                raise HTTPException(422, "Повторяющиеся поля записи")
            sources = []
            for image in form.getlist("images"):
                if not isinstance(image, UploadFile):
                    raise HTTPException(422, "Загрузите картинку")
                sources.append(await image.read(MAX_UPLOAD_BYTES + 1))
            text, mode = form.get("text", ""), form.get("processing_mode", "manual")
            if not isinstance(text, str) or not isinstance(mode, str):
                raise HTTPException(422, "Нужен текст записи")
            if not text.strip():
                text, mode = "Заметка с изображением", "manual"
            try:
                body = TextCapture(text=text, processing_mode=mode)
            except ValueError:
                raise HTTPException(422, "Проверьте текст и режим обработки") from None
            return await run_in_threadpool(save_capture, db, session.user_id, body.text, body.processing_mode, key, sources)

    @router.post("/notes/{note_id}/images", response_model=NoteImageResponse, status_code=201)
    async def upload(note_id: UUID, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        owned_note(db, str(note_id), session.user_id)
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,100}", key):
            raise HTTPException(422, "Нужен код повторной загрузки")
        async with request.form(max_files=1, max_fields=0, max_part_size=1024) as form:
            if len(list(form.multi_items())) != 1 or not isinstance(form.get("image"), UploadFile):
                raise HTTPException(422, "Загрузите одну картинку")
            source = await form["image"].read(MAX_UPLOAD_BYTES + 1)
            return await run_in_threadpool(save_image, db, session.user_id, str(note_id), key, source)

    @router.get("/note-images/{image_id}", response_class=Response)
    def content(image_id: UUID, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        image = db.scalar(select(NoteImage).where(NoteImage.id == str(image_id)))
        if image is None:
            raise HTTPException(404, "Картинка не найдена")
        owned_capture(db, image.capture_id, session.user_id)
        # Deleted notes hide their images even when requested through the original capture.
        note = db.scalar(select(Note).where(Note.capture_id == image.capture_id))
        if note is not None:
            owned_note(db, note.id, session.user_id)
        return Response(image.data, media_type=image.media_type, headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin", "Content-Disposition": 'inline; filename="image.webp"',
        })

    return router
