"""Корзина, удаление задач, ручные связи, объединение заметок и состояние подсказок.

Документ docs/notes-manage-v1.md. Здесь только работа с базой. Маршруты лежат в app/routes/notes_manage.py,
а объединение запускает воркер через complete_merge.
"""

import logging
import re
import time
from collections import Counter

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.analytics import record_event
from app.audio_storage import AudioStorageUnavailable
from app.limits import capture_cost, within_daily_limit
from app.models import (
    Capture,
    Item,
    Job,
    Note,
    NoteLink,
    Outbox,
    Reminder,
    Revision,
    TranscriptRevision,
    User,
    new_id,
)
from app.providers import MERGE_MARKER
from app.services import (
    advance_note,
    cancel_item_reminders,
    cancel_reminder_deliveries,
    edit_note,
    lock_account,
    owned_note,
    save_revision,
)

DAY = 86400
MAX_LINKS = 50
MIN_MERGE, MAX_MERGE = 2, 5
MAX_MERGE_CHARS = 12000
PURGE_BATCH = 50
LIMIT_TEXT = "Лимит ИИ на сегодня исчерпан. Объединить заметки можно завтра."

GUIDES = ("note_tools", "categories")

log = logging.getLogger(__name__)


# ---- Trash ----

def trash_note(db, note, version):
    """Move a note to the trash. Its reminders stay but nothing delivers them until it is restored."""
    advance_note(db, note, version, deleted_at=time.time())
    record_event(db, note.user_id, "note_deleted", f"{note.id}:{note.version}", subject_id=note.id)
    db.commit()


def restore_note(db, user_id, note_id, now=None):
    """Bring a note back. A reminder whose time passed in the trash is cancelled so it never arrives late."""
    now = time.time() if now is None else now
    lock_account(db, user_id)
    note = owned_note(db, note_id, user_id, state="trashed")
    advance_note(db, note, note.version, deleted_at=None, merged_into=None)
    stale = db.scalars(select(Reminder.id).where(
        Reminder.note_id == note.id, Reminder.user_id == user_id,
        Reminder.status == "confirmed", Reminder.scheduled_at <= now,
    )).all()
    if stale:
        db.execute(
            update(Reminder).where(Reminder.id.in_(stale))
            .values(status="cancelled", generation=Reminder.generation + 1),
            execution_options={"synchronize_session": False},
        )
        cancel_reminder_deliveries(db, user_id, stale)
    record_event(db, user_id, "note_restored", f"{note.id}:{note.version}", subject_id=note.id)
    db.commit()
    return note


def trash_views(db, user_id, settings, limit=200):
    rows = db.scalars(
        select(Note).where(Note.user_id == user_id, Note.deleted_at.is_not(None))
        .order_by(Note.deleted_at.desc(), Note.id).limit(limit)
    ).all()
    targets = {row.merged_into for row in rows if row.merged_into}
    titles = dict(db.execute(
        select(Note.id, Note.title).where(Note.user_id == user_id, Note.id.in_(targets))
    ).all()) if targets else {}
    return {
        "trash_days": settings.trash_days,
        "notes": [{
            "id": row.id, "title": row.title, "version": row.version, "deleted_at": row.deleted_at,
            "purge_at": row.deleted_at + settings.trash_days * DAY, "category_id": row.category_id,
            "merged_into": row.merged_into, "merged_into_title": titles.get(row.merged_into),
        } for row in rows],
    }


class PurgeBlocked(Exception):
    """The audio file of the note could not be removed, so the note stays and is tried again later."""


def purge_note(db, note, audio_storage):
    """Erase one note and all that belongs to it. The caller owns the account lock and commits."""
    capture = db.get(Capture, note.capture_id)
    if capture is not None and capture.audio_key:
        if audio_storage is None:
            raise PurgeBlocked()
        try:
            audio_storage.delete(capture.audio_key)
        except AudioStorageUnavailable:
            raise PurgeBlocked() from None
    note_id, capture_id = note.id, note.capture_id
    reminder_ids = db.scalars(select(Reminder.id).where(Reminder.note_id == note_id)).all()
    job_ids = db.scalars(select(Job.id).where(Job.capture_id == capture_id)).all()
    if reminder_ids:
        db.execute(delete(Outbox).where(Outbox.reminder_id.in_(reminder_ids)))
    if job_ids:
        db.execute(delete(Outbox).where(Outbox.job_id.in_(job_ids)))
    db.execute(delete(Reminder).where(Reminder.note_id == note_id))
    db.execute(delete(NoteLink).where(or_(NoteLink.note_a == note_id, NoteLink.note_b == note_id)))
    db.execute(delete(Revision).where(Revision.note_id == note_id))
    db.execute(delete(Item).where(Item.note_id == note_id))
    db.execute(delete(Note).where(Note.id == note_id))
    db.execute(delete(TranscriptRevision).where(TranscriptRevision.capture_id == capture_id))
    db.execute(delete(Job).where(Job.capture_id == capture_id))
    db.execute(delete(Capture).where(Capture.id == capture_id))
    db.expire_all()


def delete_forever(db, user_id, note_id, audio_storage):
    lock_account(db, user_id)
    note = owned_note(db, note_id, user_id, state="trashed")
    try:
        purge_note(db, note, audio_storage)
    except PurgeBlocked:
        db.rollback()
        raise HTTPException(503, "Не удалось удалить файл записи. Попробуйте позже.") from None
    record_event(db, user_id, "note_purged", note_id)
    db.commit()


def purge_expired(sessions, settings, audio_storage=None, now=None, batch=PURGE_BATCH):
    """Erase notes trashed longer than NOTES_TRASH_DAYS ago, and deleted tasks of the same age.

    Safe to run again and again: a note is rechecked under its account lock and an erased row is simply absent.
    Returns counts only. Nothing about the content is logged.
    """
    now = time.time() if now is None else now
    cutoff = now - settings.trash_days * DAY
    counts = {"notes": 0, "items": 0, "skipped": 0}
    with sessions() as db:
        due_notes = db.execute(
            select(Note.id, Note.user_id).where(Note.deleted_at.is_not(None), Note.deleted_at < cutoff)
            .order_by(Note.deleted_at, Note.id).limit(batch)
        ).all()
    for note_id, user_id in due_notes:
        with sessions.begin() as db:
            lock_account(db, user_id)
            note = db.scalar(select(Note).where(
                Note.id == note_id, Note.deleted_at.is_not(None), Note.deleted_at < cutoff,
            ))
            if note is None:
                continue
            try:
                purge_note(db, note, audio_storage)
            except PurgeBlocked:
                counts["skipped"] += 1
                continue
            counts["notes"] += 1
    with sessions() as db:
        due_items = db.execute(
            select(Item.id, Item.user_id).where(Item.deleted_at.is_not(None), Item.deleted_at < cutoff)
            .order_by(Item.deleted_at, Item.id).limit(batch)
        ).all()
    for item_id, user_id in due_items:
        with sessions.begin() as db:
            lock_account(db, user_id)
            item = db.scalar(select(Item).where(
                Item.id == item_id, Item.deleted_at.is_not(None), Item.deleted_at < cutoff,
            ))
            if item is None:
                continue
            reminder_ids = db.scalars(select(Reminder.id).where(Reminder.item_id == item_id)).all()
            if reminder_ids:
                db.execute(delete(Outbox).where(Outbox.reminder_id.in_(reminder_ids)))
                db.execute(delete(Reminder).where(Reminder.id.in_(reminder_ids)))
            db.execute(delete(Item).where(Item.id == item_id))
            counts["items"] += 1
    if counts["notes"] or counts["items"] or counts["skipped"]:
        log.warning("trash_purged notes=%d items=%d skipped=%d", counts["notes"], counts["items"], counts["skipped"])
    return counts


# ---- Tasks and ideas ----

def delete_item(db, note, item_id, version):
    """Soft delete: the row stays for the «Отменить» toast. Reminders of the item are cancelled."""
    if "\x00" in item_id:
        raise HTTPException(404, "Элемент не найден")
    advance_note(db, note, version)
    item = db.scalar(select(Item).where(
        Item.id == item_id, Item.note_id == note.id, Item.user_id == note.user_id, Item.deleted_at.is_(None),
    ))
    if item is None:
        raise HTTPException(404, "Элемент не найден")
    item.deleted_at = time.time()
    item.version += 1
    cancel_item_reminders(db, note.user_id, item.id)
    record_event(db, note.user_id, "item_deleted", f"{item.id}:{item.version}", subject_id=note.id)
    db.flush()
    save_revision(db, note)
    db.commit()


def restore_item(db, note, item_id, settings, now=None):
    """Undo a delete made within the trash period. Reminders that the delete cancelled stay cancelled."""
    if "\x00" in item_id:
        raise HTTPException(404, "Элемент не найден")
    now = time.time() if now is None else now
    lock_account(db, note.user_id)
    item = db.scalar(select(Item).where(
        Item.id == item_id, Item.note_id == note.id, Item.user_id == note.user_id,
        Item.deleted_at.is_not(None), Item.deleted_at >= now - settings.trash_days * DAY,
    ))
    if item is None:
        raise HTTPException(404, "Элемент не найден")
    advance_note(db, note, note.version)
    item.deleted_at = None
    item.version += 1
    record_event(db, note.user_id, "item_restored", f"{item.id}:{item.version}", subject_id=note.id)
    db.flush()
    save_revision(db, note)
    db.commit()


def delete_conclusion(db, note, conclusion_id, version):
    """Remove one AI addition from the list. The statuses accepted and rejected stay for those who keep them."""
    kept = [c for c in note.conclusions if c.get("id") != conclusion_id]
    if len(kept) == len(note.conclusions):
        raise HTTPException(404, "Вывод не найден")
    return edit_note(db, note, version, conclusions=kept)


# ---- Manual links ----

def link_pair(first, second):
    return (first, second) if first < second else (second, first)


def link_views(db, note):
    """Manual links of a note whose other side is alive, newest first."""
    rows = db.execute(
        select(NoteLink.created_at, Note)
        .join(Note, Note.id == func.coalesce(
            func.nullif(NoteLink.note_a, note.id), NoteLink.note_b,
        ))
        .where(
            NoteLink.user_id == note.user_id, or_(NoteLink.note_a == note.id, NoteLink.note_b == note.id),
            Note.user_id == note.user_id, Note.deleted_at.is_(None), Note.id != note.id,
        )
        .order_by(NoteLink.created_at.desc(), Note.id)
    ).all()
    return {"note_id": note.id, "links": [
        {"note_id": other.id, "title": other.title, "category_id": other.category_id,
         "updated_at": other.updated_at, "created_at": created}
        for created, other in rows
    ]}


def add_link(db, note, other_id):
    if other_id == note.id:
        raise HTTPException(422, "Нельзя связать заметку с самой собой")
    lock_account(db, note.user_id)
    other = owned_note(db, other_id, note.user_id)
    a, b = link_pair(note.id, other.id)
    exists = db.scalar(select(NoteLink.id).where(NoteLink.note_a == a, NoteLink.note_b == b))
    if exists is None:
        total = db.scalar(select(func.count()).select_from(NoteLink).where(
            or_(NoteLink.note_a == note.id, NoteLink.note_b == note.id),
        ))
        if total >= MAX_LINKS:
            raise HTTPException(429, f"К одной заметке можно привязать не больше {MAX_LINKS} заметок")
        db.add(NoteLink(user_id=note.user_id, note_a=a, note_b=b))
        record_event(db, note.user_id, "note_linked", f"{a}:{b}", subject_id=note.id)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()  # Another tab made the same link a moment ago.
            return
    db.commit()


def remove_link(db, note, other_id):
    if "\x00" in other_id:
        raise HTTPException(404, "Связь не найдена")
    lock_account(db, note.user_id)
    a, b = link_pair(note.id, other_id)
    removed = db.execute(delete(NoteLink).where(
        NoteLink.user_id == note.user_id, NoteLink.note_a == a, NoteLink.note_b == b,
    )).rowcount
    if not removed:
        db.rollback()
        raise HTTPException(404, "Связь не найдена")
    db.commit()


def manual_edges(db, user_id, node_ids):
    """Manual links inside a set of alive notes, for the overview graph."""
    ids = list(node_ids)
    if not ids:
        return []
    rows = db.execute(select(NoteLink.note_a, NoteLink.note_b).where(
        NoteLink.user_id == user_id, NoteLink.note_a.in_(ids), NoteLink.note_b.in_(ids),
    ).order_by(NoteLink.note_a, NoteLink.note_b)).all()
    return [{"source": a, "target": b, "score": 1.0, "manual": True} for a, b in rows]


# ---- Guides ----

def guide_views(user):
    state = user.guide_state if isinstance(user.guide_state, dict) else {}
    views = {}
    for name in GUIDES:
        saved = state.get(name) if isinstance(state.get(name), dict) else {}
        step = saved.get("step")
        views[name] = {"step": step if isinstance(step, int) and step >= 0 else 0, "done": saved.get("done") is True}
    return {"guides": views}


def set_guide(db, user_id, guide, step, done):
    lock_account(db, user_id)
    user = db.get(User, user_id)
    db.refresh(user)
    state = dict(user.guide_state) if isinstance(user.guide_state, dict) else {}
    state[guide] = {"step": step, "done": done}
    user.guide_state = state  # A new dict, so the JSON column notices the change.
    db.commit()
    return guide_views(user)


# ---- Merge ----

def clean_title(title):
    return " ".join((title or "Без названия").split())[:200]


def merge_text(notes):
    """The merge input: a marker line, then every note as «=== Заметка N: title ===» and its current Markdown."""
    parts = [MERGE_MARKER]
    for number, note in enumerate(notes, 1):
        parts.append(f"=== Заметка {number}: {clean_title(note.title)} ===\n{note.markdown.strip()}")
    return "\n\n".join(parts)


def request_merge(db, user_id, note_ids, key, settings):
    """Queue an AI merge. It is one capture and one job, so limits, retries and failover work as for any note."""
    lock_account(db, user_id)
    ids = list(note_ids)
    if not MIN_MERGE <= len(ids) <= MAX_MERGE or len(set(ids)) != len(ids):
        raise HTTPException(422, f"Выберите от {MIN_MERGE} до {MAX_MERGE} разных заметок")
    capture_key = "merge:" + key

    def existing():
        capture = db.scalar(select(Capture).where(Capture.user_id == user_id, Capture.idempotency_key == capture_key))
        if capture is None:
            return None
        job = db.scalar(select(Job).where(Job.capture_id == capture.id))
        if job is None or job.kind != "merge" or job.merge_note_ids != ids:
            raise HTTPException(409, "Этот ключ уже использован для другого объединения")
        return job

    found = existing()
    if found is not None:
        return found
    notes = [owned_note(db, note_id, user_id) for note_id in ids]
    from app.note_images import check_merge_space
    if not check_merge_space(db, notes, user_id):
        raise HTTPException(429, "Для объединения нужно место. Максимум 20 картинок в результате и 50 МБ на аккаунте")
    text = merge_text(notes)
    if len(text) > MAX_MERGE_CHARS:
        raise HTTPException(422, "Заметки слишком длинные для объединения. Выберите меньше заметок или сократите их.")
    if not within_daily_limit(db, settings, user_id, capture_cost(settings, "text")):
        raise HTTPException(429, LIMIT_TEXT)
    pending = db.scalar(
        select(func.count()).select_from(Job).where(Job.user_id == user_id, Job.status.in_(["queued", "running"]))
    )
    if pending >= settings.max_pending_per_user:
        raise HTTPException(429, "Слишком много записей ожидают обработки")
    capture = Capture(
        id=new_id(), user_id=user_id, original_text=text, idempotency_key=capture_key,
        processing_mode="ai", channel="web", input_kind="text",
    )
    job = Job(
        id=new_id(), capture_id=capture.id, user_id=user_id, provider=settings.provider,
        kind="merge", merge_note_ids=ids,
    )
    try:
        db.add(capture)
        db.flush()
        db.add(job)
        db.flush()
        record_event(db, user_id, "merge_requested", capture.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        found = existing()
        if found is not None:
            return found
        raise
    return job


def normal_text(text):
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").casefold().replace("ё", "е"))
    return " ".join(cleaned.split())


class MergeSourcesChanged(Exception):
    """One of the notes was deleted or merged while the model worked. Nothing is changed."""


def merge_sources_alive(db, job):
    ids = job.merge_note_ids or []
    found = db.scalar(select(func.count()).select_from(Note).where(
        Note.id.in_(ids), Note.user_id == job.user_id, Note.deleted_at.is_(None),
    ))
    return bool(ids) and found == len(set(ids))


def complete_merge(db, job, note, new_items, now=None):
    """Finish a merge in the worker's transaction: carry tasks and reminders over, move links, trash the originals.

    `note` and `new_items` are the freshly added merged note and the items the model produced. The caller owns
    the account lock and the commit. Raises MergeSourcesChanged when an original is gone.
    """
    now = time.time() if now is None else now
    ids = list(job.merge_note_ids or [])
    by_id = {row.id: row for row in db.scalars(select(Note).where(
        Note.id.in_(ids), Note.user_id == job.user_id, Note.deleted_at.is_(None),
    ))}
    if not ids or len(by_id) != len(set(ids)):
        raise MergeSourcesChanged()
    sources = [by_id[note_id] for note_id in ids]
    from app.note_images import carry_merge_images
    carry_merge_images(db, sources, note)

    if note.category_id is None:
        votes = Counter(source.category_id for source in sources if source.category_id)
        if votes:
            top = max(votes.values())
            note.category_id = next(s.category_id for s in sources if s.category_id and votes[s.category_id] == top)

    known = {normal_text(item.text): item for item in new_items}
    position = len(new_items)
    mapping = {}
    for source in sources:
        open_tasks = db.scalars(select(Item).where(
            Item.note_id == source.id, Item.user_id == job.user_id, Item.kind == "task",
            Item.status == "open", Item.deleted_at.is_(None),
        ).order_by(Item.position, Item.id)).all()
        for old in open_tasks:
            key = normal_text(old.text)
            target = known.get(key)
            if target is None:
                # The quote belongs to the original note, not to the merged text, so it is not copied.
                target = Item(
                    user_id=job.user_id, note_id=note.id, kind="task", text=old.text, status="open", version=1,
                    due_at=old.due_at, due_text=old.due_text, category_id=old.category_id, position=position,
                )
                position += 1
                db.add(target)
                known[key] = target
            mapping[old.id] = target
    db.flush()

    for reminder in db.scalars(select(Reminder).where(
        Reminder.note_id.in_(ids), Reminder.user_id == job.user_id, Reminder.status == "confirmed",
    )):
        if reminder.item_id is None:
            reminder.note_id = note.id
        elif reminder.item_id in mapping:
            reminder.note_id, reminder.item_id = note.id, mapping[reminder.item_id].id

    source_ids = set(ids)
    links = db.scalars(select(NoteLink).where(
        NoteLink.user_id == job.user_id, or_(NoteLink.note_a.in_(ids), NoteLink.note_b.in_(ids)),
    )).all()
    others = []
    for link in links:
        other = link.note_b if link.note_a in source_ids else link.note_a
        if other not in source_ids and other not in others:
            others.append(other)
        db.delete(link)
    db.flush()
    alive = set(db.scalars(select(Note.id).where(
        Note.id.in_(others), Note.user_id == job.user_id, Note.deleted_at.is_(None),
    ))) if others else set()
    for other in others:
        if other in alive:
            a, b = link_pair(note.id, other)
            db.add(NoteLink(user_id=job.user_id, note_a=a, note_b=b))

    db.execute(
        update(Note).where(Note.id.in_(ids), Note.user_id == job.user_id).values(
            deleted_at=now, merged_into=note.id, version=Note.version + 1, updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    record_event(db, job.user_id, "notes_merged", job.id, subject_id=note.id)
