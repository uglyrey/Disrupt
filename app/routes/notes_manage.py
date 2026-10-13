"""Корзина, удаление задач, объединение заметок, ручные связи и состояние подсказок. См. docs/notes-manage-v1.md."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from app.models import User
from app.notes_manage import (
    add_link,
    delete_conclusion,
    delete_forever,
    delete_item,
    guide_views,
    link_views,
    remove_link,
    request_merge,
    restore_item,
    restore_note,
    set_guide,
    trash_note,
    trash_views,
)
from app.schemas import (
    MAX_COUNTER,
    AudioJobResponse,
    GuidesResponse,
    GuideUpdate,
    MergeRequest,
    NoteLinkCreate,
    NoteLinksResponse,
    NoteResponse,
    TrashResponse,
)
from app.security import get_login_session
from app.services import job_view, note_view, owned_note


def build_router(database, settings, audio_storage):
    router = APIRouter(prefix="/api/v1", tags=["notes-manage"])
    version_query = Query(ge=1, le=MAX_COUNTER)

    @router.delete("/notes/{note_id}", status_code=204)
    def remove_note(note_id: str, request: Request, version: int = version_query, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        trash_note(db, note, version)

    @router.get("/trash", response_model=TrashResponse)
    def trash(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return trash_views(db, session.user_id, settings)

    @router.post("/trash/{note_id}/restore", response_model=NoteResponse)
    def restore(note_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        note = restore_note(db, session.user_id, note_id)
        return note_view(db, note, settings)

    @router.delete("/trash/{note_id}", status_code=204)
    def forget(note_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        delete_forever(db, session.user_id, note_id, audio_storage)

    @router.delete("/notes/{note_id}/items/{item_id}", response_model=NoteResponse)
    def remove_item(
        note_id: str, item_id: str, request: Request, version: int = version_query, db=Depends(database),
    ):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        delete_item(db, note, item_id, version)
        return note_view(db, note, settings)

    @router.post("/notes/{note_id}/items/{item_id}/restore", response_model=NoteResponse)
    def bring_back_item(note_id: str, item_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        restore_item(db, note, item_id, settings)
        return note_view(db, note, settings)

    @router.delete("/notes/{note_id}/conclusions/{conclusion_id}", response_model=NoteResponse)
    def remove_conclusion(
        note_id: str, conclusion_id: str, request: Request, version: int = version_query, db=Depends(database),
    ):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        delete_conclusion(db, note, conclusion_id, version)
        return note_view(db, note, settings)

    @router.post("/notes/merge", response_model=AudioJobResponse, status_code=202)
    def merge(body: MergeRequest, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        job = request_merge(db, session.user_id, body.note_ids, body.idempotency_key, settings)
        return job_view(db, job)

    @router.get("/notes/{note_id}/links", response_model=NoteLinksResponse)
    def links(note_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return link_views(db, owned_note(db, note_id, session.user_id))

    @router.post("/notes/{note_id}/links", response_model=NoteLinksResponse, status_code=201)
    def link(note_id: str, body: NoteLinkCreate, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        add_link(db, note, body.note_id)
        return link_views(db, note)

    @router.delete("/notes/{note_id}/links/{other_id}", status_code=204)
    def unlink(note_id: str, other_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        remove_link(db, owned_note(db, note_id, session.user_id), other_id)

    @router.get("/account/guides", response_model=GuidesResponse)
    def guides(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return guide_views(db.scalar(select(User).where(User.id == session.user_id)))

    @router.patch("/account/guides", response_model=GuidesResponse)
    def update_guide(body: GuideUpdate, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        return set_guide(db, session.user_id, body.guide, body.step, body.done)

    return router
