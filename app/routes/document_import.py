"""Authenticated preview only. Files are never retained and no model is called."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.security import get_login_session

_slot = threading.BoundedSemaphore(1)
MAX_BYTES = 5 * 1024 * 1024


class ImportedText(BaseModel):
    text: str
    format: str
    warnings: list[str]


def read_document(source, kind):
    if kind not in {"docx", "pdf", "rtf", "odt", "txt", "md", "markdown"}:
        raise HTTPException(422, "Поддерживаются DOCX, PDF, RTF, ODT, TXT и MD. Старый DOC сохраните как DOCX")
    if not source or len(source) > MAX_BYTES:
        raise HTTPException(413, "Нужен непустой файл не больше 5 МБ")
    if not _slot.acquire(blocking=False):
        raise HTTPException(429, "Сервер читает другой документ. Повторите через несколько секунд")
    try:
        # A separate process bounds malformed PDF/ZIP work. It has no database/provider credentials.
        env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH", "LANG") if name in os.environ}
        script = Path(__file__).resolve().parents[1] / "document_extract.py"
        result = subprocess.run([sys.executable, "-I", str(script), kind], input=source, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=15, env=env, check=False)
        if result.returncode or len(result.stdout) > 100000:
            raise HTTPException(422, "Документ слишком сложный. Сохраните его текст отдельно")
        data = json.loads(result.stdout)
        if "error" in data:
            raise HTTPException(422, data["error"])
        return ImportedText.model_validate(data)
    except subprocess.TimeoutExpired:
        raise HTTPException(422, "Чтение документа заняло слишком долго. Разбейте его на части") from None
    except (OSError, ValueError):
        raise HTTPException(422, "Не удалось прочитать документ") from None
    finally:
        _slot.release()


def build_router(database):
    router = APIRouter(prefix="/api/v1/imports", tags=["imports"])

    @router.post("/text", response_model=ImportedText)
    async def preview(request: Request, db=Depends(database)):
        get_login_session(request, db, write=True)
        async with request.form(max_files=1, max_fields=0, max_part_size=1024) as form:
            file = form.get("file")
            if len(list(form.multi_items())) != 1 or not isinstance(file, UploadFile):
                raise HTTPException(422, "Выберите один документ")
            kind = Path(file.filename or "").suffix.lower().lstrip(".")
            return await run_in_threadpool(read_document, await file.read(MAX_BYTES + 1), kind)

    return router
