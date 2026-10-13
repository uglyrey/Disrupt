"""Only the signed-in owner can upload or view note images."""

import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import Response

from app.models import NoteImage
from app.note_images import MAX_UPLOAD_BYTES, save_image
from app.security import get_login_session
from app.services import owned_note


class NoteImageResponse(BaseModel):
    id: str
    url: str
    width: int
    height: int
    byte_size: int


def build_router(database):
    router = APIRouter(prefix="/api/v1", tags=["note-images"])

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
        owned_note(db, image.note_id, session.user_id)
        return Response(image.data, media_type=image.media_type, headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin", "Content-Disposition": 'inline; filename="image.webp"',
        })

    return router
