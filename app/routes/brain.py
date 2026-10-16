"""Second brain reads without AI: related notes, the note graph and the dashboard. Read-only."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, func, select

from app.dashboard import build_dashboard
from app.models import Item, Note, User
from app.notes_manage import manual_edges
from app.related import MIN_SCORE, NoteDoc, build_index, graph_edges, related_notes
from app.security import get_login_session
from app.services import general_category_id, owned_note
from app.task_due import resolve_due
from app.timezones import effective_zone

CORPUS_LIMIT = 1000
GRAPH_NEIGHBOURS = 3


def load_docs(db, user_id, *, must_include=None, cap=CORPUS_LIMIT):
    """The user's latest notes with the columns that matter, and their item texts, in two queries."""
    rows = db.execute(
        select(Note.id, Note.title, Note.markdown, Note.category_id, Note.updated_at)
        .where(Note.user_id == user_id, Note.deleted_at.is_(None)).order_by(Note.updated_at.desc(), Note.id).limit(cap)
    ).all()
    if must_include is not None and all(row.id != must_include for row in rows):
        rows = [*rows, *db.execute(
            select(Note.id, Note.title, Note.markdown, Note.category_id, Note.updated_at)
            .where(Note.user_id == user_id, Note.deleted_at.is_(None), Note.id == must_include)
        ).all()]
    texts = {row.id: [] for row in rows}
    for note_id, text in db.execute(
        select(Item.note_id, Item.text).where(
            Item.user_id == user_id, Item.deleted_at.is_(None), Item.note_id.in_(list(texts)),
        )
        .order_by(Item.note_id, Item.position, Item.id)
    ):
        texts[note_id].append(text)
    return [
        NoteDoc(row.id, row.title, row.markdown, tuple(texts[row.id]), row.category_id, row.updated_at)
        for row in rows
    ]


def task_row(item, title, note_version, updated_at, created_at, timezone):
    """One task for the list. `due_resolved_at` is the deadline as a moment, or None when it is unknown."""
    resolved, has_time = resolve_due(item.due_at, item.due_text, created_at, timezone)
    return {
        "id": item.id, "text": item.text, "status": item.status, "version": item.version,
        "due_at": item.due_at, "due_text": item.due_text, "due_resolved_at": resolved, "due_has_time": has_time,
        "position": item.position, "category_id": item.category_id, "note_id": item.note_id,
        "note_title": title, "note_version": note_version, "note_updated_at": updated_at,
    }


def build_router(database, settings):
    router = APIRouter(prefix="/api/v1", tags=["second-brain"])

    @router.get("/notes/{note_id}/related")
    def related(note_id: str, request: Request, limit: int = Query(5, ge=1, le=20), db=Depends(database)):
        session = get_login_session(request, db)
        owned_note(db, note_id, session.user_id)
        docs = load_docs(db, session.user_id, must_include=note_id)
        index = build_index(docs, general_category_id(db, session.user_id))
        by_id = {doc.id: doc for doc in docs}
        return {"note_id": note_id, "related": [
            {
                "note_id": hit["note_id"], "title": by_id[hit["note_id"]].title,
                "category_id": by_id[hit["note_id"]].category_id,
                "updated_at": by_id[hit["note_id"]].updated_at,
                "score": hit["score"], "shared_terms": hit["shared_terms"],
            }
            for hit in related_notes(index, note_id, limit, MIN_SCORE)
        ]}

    @router.get("/graph")
    def graph(request: Request, limit: int = Query(200, ge=1, le=CORPUS_LIMIT), db=Depends(database)):
        session = get_login_session(request, db)
        docs = load_docs(db, session.user_id)
        index = build_index(docs, general_category_id(db, session.user_id))
        nodes = docs[:limit]  # Newest first, the same order the query used.
        return {
            "nodes": [
                {"id": doc.id, "title": doc.title, "category_id": doc.category_id, "updated_at": doc.updated_at}
                for doc in nodes
            ],
            "edges": graph_edges(index, [doc.id for doc in nodes], GRAPH_NEIGHBOURS, MIN_SCORE),
            "manual_edges": manual_edges(db, session.user_id, [doc.id for doc in nodes]),
        }

    @router.get("/dashboard")
    def dashboard(request: Request, days: int = Query(30, ge=1, le=366), db=Depends(database)):
        session = get_login_session(request, db)
        return build_dashboard(db, settings, session.user_id, days)

    @router.get("/tasks")
    def tasks(
        request: Request, status: Literal["open", "completed", "all"] = "open",
        limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000), db=Depends(database),
    ):
        """Every task of the user across notes. Text columns are encrypted, so nothing is filtered on them."""
        session = get_login_session(request, db)
        mine = (Item.user_id == session.user_id, Item.kind == "task", Item.deleted_at.is_(None))
        counts = dict(db.execute(
            select(Item.status, func.count())
            .join(Note, (Note.id == Item.note_id) & (Note.user_id == Item.user_id))
            .where(*mine, Note.deleted_at.is_(None)).group_by(Item.status)
        ).all())
        statement = (
            select(Item, Note.title, Note.version, Note.updated_at, Note.created_at)
            .join(Note, (Note.id == Item.note_id) & (Note.user_id == Item.user_id))
            .where(*mine, Note.deleted_at.is_(None))
        )
        if status != "all":
            statement = statement.where(Item.status == status)
        # Open tasks go by deadline, undated last. Done ones go by the latest change of their note.
        # Open first when both are shown. Position and id keep paging stable.
        statement = statement.order_by(
            (Item.status == "completed").asc(),
            (Item.due_at.is_(None) | (Item.status == "completed")).asc(),
            case((Item.status == "open", Item.due_at), else_=None).asc(),
            Note.updated_at.desc(), Item.position, Item.id,
        ).limit(limit + 1).offset(offset)
        rows = db.execute(statement).all()
        zone = effective_zone(db.get(User, session.user_id), settings)
        return {
            "status": status, "limit": limit, "offset": offset, "has_more": len(rows) > limit,
            "counts": {"open": counts.get("open", 0), "completed": counts.get("completed", 0)},
            "items": [task_row(item, title, note_version, updated_at, created_at, zone)
                      for item, title, note_version, updated_at, created_at in rows[:limit]],
        }

    return router
