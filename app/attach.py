"""Продолжение заметки: узнать, что новая запись относится к уже существующей заметке, и добавить её туда.

Документ docs/attach-to-note-v1.md. Здесь поиск кандидата без обращения к базе в цикле, решение (ответить сразу или
спросить) и сами действия: добавить, оставить отдельной, отменить. Запись никогда не теряется: своя заметка
создаётся всегда, как раньше. «Добавить» переносит её содержимое в заметку-цель, а свою заметку прячет в корзину
вместе с исходником. «Отдельная заметка» и «Отменить» возвращают всё назад.
"""

import re
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select, update

from app.analytics import record_event
from app.models import Capture, Item, Note, NoteImage, Reminder, User
from app.related import NoteDoc, build_index
from app.reminder_proposals import MONTH_NAMES
from app.services import lock_account, owned_note, save_revision
from app.timezones import effective_zone

OFFER_TTL = 86400  # Сутки на выбор. Потом запись остаётся отдельной заметкой, как была.
UNDO_TTL = 86400  # И сутки на «Отменить» после добавления.
MIN_RECORD_CHARS = 20  # Очень короткую запись ни о чём не спрашиваем.
MIN_NOTE_CHARS = 20  # И к почти пустой заметке ничего не предлагаем.
MIN_CONFIDENCE = 0.7  # Ниже модель только гадает.
MODEL_MIN_SIMILARITY = 0.08  # Модель не может назвать заметку, которая с записью не имеет общих слов.
CANDIDATE_MIN = 0.05
HINT_MIN_SCORE = 0.2  # Слова «по курсовой» должны заметно совпасть с заметкой.
HINT_CLEAR_RATIO = 1.3  # Лучшая заметка должна быть заметно лучше второй, иначе она не единственная.
MAX_CANDIDATES = 3
CORPUS = 500
TITLE_CLIP = 80
MAX_MARKDOWN = 24000
NEW_ID, HINT_ID = "__new__", "__hint__"

# «Добавь к заметке про курсовую: ...», «допиши в заметку Ремонт». Человек сам попросил, спрашивать не нужно.
EXPLICIT = re.compile(
    r"(?:^|[.!?\n]\s*)(?:пожалуйста,?\s*)?"
    r"(?:добавь(?:те)?|допиши(?:те)?|запиши(?:те)?|внеси(?:те)?|дополни(?:те)?)\s+"
    r"(?:это\s+|ещё\s+|еще\s+|туда\s+)?(?:к|в|на)\s+заметк\w*\s+(?:(?:про|о|об|по)\s+)?(?P<hint>[^:\n,.;]{2,80})",
    re.IGNORECASE,
)
# «Ещё по курсовой: добавить главу 3», «к заметке про ремонт: купить краску». Похоже на продолжение, но спросим.
CONTINUES = re.compile(
    r"^\s*(?:(?:и\s+)?(?:ещё|еще)\s+(?:по|про|к|в)\s+|(?:к|в)\s+заметке\s+(?:(?:про|о|об|по)\s+)?"
    r"|продолжение\s+(?:заметки\s+|темы\s+)?(?:про\s+)?)(?P<hint>[^:\n]{2,80}):",
    re.IGNORECASE,
)
QUOTES = "«»\"'„“”‘’ "

ATTACHED, OFFERED, SEPARATE = "attached", "offered", "separate"


def clip_title(title, limit=TITLE_CLIP):
    return " ".join((title or "").split())[:limit]


def hint_of(original):
    """(hint, explicit) from the words of the person, or (None, False)."""
    text = original or ""
    for pattern, explicit in ((EXPLICIT, True), (CONTINUES, False)):
        found = pattern.search(text)
        if found:
            hint = found.group("hint").strip(QUOTES)
            if len(hint) >= 2:
                return hint, explicit
    return None, False


def normal(text):
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").casefold().replace("ё", "е")).split())


def corpus(db, user_id):
    from app.routes.brain import load_docs  # The same rows as the related-notes screen.
    return load_docs(db, user_id, cap=CORPUS)


def pseudo(note_id, title, text):
    return NoteDoc(note_id, title[:200], text[:3000], (), None, 0.0)


def rank_hint(index, docs, hint, skip=()):
    """(best id, unique) for the note the words of the person name, or (None, False)."""
    found = {k: v for k, v in index.scores(HINT_ID).items() if k not in (NEW_ID, HINT_ID, *skip)}
    if not found:
        return None, False
    wanted = normal(hint)
    same = [k for k in index.order(found) if normal(docs[k].title) == wanted]
    if len(same) == 1:
        return same[0], True
    ordered = index.order(found)
    best = ordered[0]
    if found[best] < HINT_MIN_SCORE:
        return None, False
    second = found[ordered[1]] if len(ordered) > 1 else 0.0
    return best, found[best] >= HINT_CLEAR_RATIO * second


def candidate_context(db, user_id, original):
    """Before the model call: [{"ref", "title"}] of up to three notes it may pick from, and {ref: note id}."""
    text = (original or "").strip()
    if len(text) < MIN_RECORD_CHARS:
        return [], {}
    docs = corpus(db, user_id)
    if not docs:
        return [], {}
    by_id = {doc.id: doc for doc in docs}
    hint, _ = hint_of(text)
    extra = [pseudo(NEW_ID, text.splitlines()[0], text)]
    if hint:
        extra.append(pseudo(HINT_ID, hint, ""))
    index = build_index([*docs, *extra])
    picked = []
    if hint:
        best, _ = rank_hint(index, by_id, hint)
        if best:
            picked.append(best)
    found = {k: v for k, v in index.scores(NEW_ID).items() if k not in (NEW_ID, HINT_ID)}
    for note_id in index.order(found):
        if found[note_id] >= CANDIDATE_MIN and note_id not in picked:
            picked.append(note_id)
    picked = [i for i in picked if len((by_id[i].markdown or "").strip()) >= MIN_NOTE_CHARS][:MAX_CANDIDATES]
    refs = {f"n{number}": note_id for number, note_id in enumerate(picked, 1)}
    return [{"ref": ref, "title": clip_title(by_id[note_id].title)} for ref, note_id in refs.items()], refs


@dataclass(frozen=True)
class Decision:
    mode: str  # "attach" (человек сам попросил) или "ask"
    target_id: str


def usable_target(db, user_id, temp, target_id, add_chars):
    target = db.scalar(select(Note).where(Note.id == target_id, Note.user_id == user_id, Note.deleted_at.is_(None)))
    if (
        target is None or target.id == temp.id or len((target.markdown or "").strip()) < MIN_NOTE_CHARS
        or len(target.markdown) + add_chars + 2 > MAX_MARKDOWN
    ):
        return None
    return target


def decide(db, temp, original, continues=None, refs=None):
    """Attach at once, ask, or leave the note alone. Called with the fresh note already stored (not committed)."""
    text = (original or "").strip()
    if len(text) < MIN_RECORD_CHARS or len((temp.markdown or "").strip()) < MIN_RECORD_CHARS // 2:
        return None
    hint, explicit = hint_of(text)
    docs = corpus(db, temp.user_id)
    by_id = {doc.id: doc for doc in docs}
    if temp.id not in by_id:
        return None
    extra = [pseudo(HINT_ID, hint, "")] if hint else []
    index = build_index([*docs, *extra])
    room = len(temp.markdown) + 40
    if hint:
        best, unique = rank_hint(index, by_id, hint, skip=(temp.id,))
        if best and usable_target(db, temp.user_id, temp, best, room):
            return Decision("attach" if explicit and unique else "ask", best)
        if explicit:
            return None
    if continues is not None and continues.confidence >= MIN_CONFIDENCE:
        target_id = (refs or {}).get(continues.ref)
        if target_id and target_id != temp.id and target_id in by_id:
            similar = index.scores(temp.id).get(target_id, 0.0)
            if similar >= MODEL_MIN_SIMILARITY and usable_target(db, temp.user_id, temp, target_id, room):
                return Decision("ask", target_id)
    return None


# ---- State of an offer ----

def offer_state(db, capture, now=None):
    """(own note, target) while the question can still be answered, else None."""
    now = time.time() if now is None else now
    if capture.attach_state != OFFERED or capture.attach_offered_at is None or capture.attach_note_id is None:
        return None
    if now - capture.attach_offered_at >= OFFER_TTL:
        return None
    own = db.scalar(select(Note).where(
        Note.capture_id == capture.id, Note.user_id == capture.user_id, Note.deleted_at.is_(None),
    ))
    target = db.scalar(select(Note).where(
        Note.id == capture.attach_note_id, Note.user_id == capture.user_id, Note.deleted_at.is_(None),
    ))
    if own is None or target is None or own.id == target.id:
        return None
    return own, target


def attached_target(db, capture):
    """The note that holds the record, or None when it is not added or the note is in the trash."""
    if capture.attach_state != ATTACHED or capture.attach_note_id is None:
        return None
    return db.scalar(select(Note).where(
        Note.id == capture.attach_note_id, Note.user_id == capture.user_id, Note.deleted_at.is_(None),
    ))


def can_undo(capture, now=None):
    now = time.time() if now is None else now
    return (
        capture.attach_state == ATTACHED and capture.attach_decided_at is not None
        and now - capture.attach_decided_at < UNDO_TTL
    )


def offer_view(db, capture, now=None):
    state = offer_state(db, capture, now)
    if state is None:
        return None
    _, target = state
    return {
        "capture_id": capture.id, "note_id": target.id, "title": clip_title(target.title),
        "expires_at": capture.attach_offered_at + OFFER_TTL,
    }


def attached_views(db, note, now=None):
    """Records added to this note, newest first. Their text stays here as extra sources."""
    now = time.time() if now is None else now
    rows = db.scalars(select(Capture).where(
        Capture.user_id == note.user_id, Capture.attach_note_id == note.id, Capture.attach_state == ATTACHED,
    ).order_by(Capture.attach_decided_at.desc(), Capture.id).limit(20)).all()
    return [{
        "capture_id": row.id, "created_at": row.created_at, "added_at": row.attach_decided_at,
        "channel": row.channel, "input_kind": row.input_kind,
        "original_text": row.original_text if row.input_kind == "text" else (row.transcript or ""),
        "can_undo": can_undo(row, now),
        "undo_until": row.attach_decided_at + UNDO_TTL if can_undo(row, now) else None,
    } for row in rows]


def visible_note_ids(db, captures):
    """{capture id: the note a person opens for it}: the target for an added record, else its own live note."""
    ids = [c.id for c in captures]
    if not ids:
        return {}
    own = dict(db.execute(
        select(Note.capture_id, Note.id).where(Note.capture_id.in_(ids), Note.deleted_at.is_(None))
    ).all())
    result = dict(own)
    added = {c.id: c.attach_note_id for c in captures if c.attach_state == ATTACHED and c.attach_note_id}
    if added:
        alive = set(db.scalars(select(Note.id).where(Note.id.in_(set(added.values())), Note.deleted_at.is_(None))))
        for capture_id, target_id in added.items():
            if target_id in alive:
                result[capture_id] = target_id
    return result


def hidden_note_ids(db, user_id):
    """Own notes of added records. They sit in the trash only to hold the original; no list shows them."""
    return select(Note.id).join(Capture, Capture.id == Note.capture_id).where(
        Note.user_id == user_id, Capture.attach_state == ATTACHED,
    )


# ---- Actions ----

def heading_for(db, user_id, settings, now):
    zone = ZoneInfo(effective_zone(db.get(User, user_id), settings))
    local = datetime.fromtimestamp(now, zone)
    year = "" if local.year == datetime.now(zone).year else f" {local.year}"
    return f"{local.day} {MONTH_NAMES[local.month]}{year}"


def section_of(heading, body):
    return f"## {heading}\n\n{(body or '').strip()}"


def bump(db, note, markdown, now, conclusions=None):
    """New version of a note under the account lock, with a revision. The open editing session is closed."""
    from app.services import close_edit_session
    note.markdown = markdown
    if conclusions is not None:
        note.conclusions = conclusions
    note.version += 1
    note.updated_at = now
    close_edit_session(db, note.id)
    db.flush()
    save_revision(db, note)


def attach_capture(db, capture, target, settings, *, explicit, now=None):
    """Add the record to `target`. The caller holds the account lock and commits (the worker does it in its own)."""
    now = time.time() if now is None else now
    own = db.scalar(select(Note).where(Note.capture_id == capture.id, Note.user_id == capture.user_id))
    heading = heading_for(db, capture.user_id, settings, now)
    section = section_of(heading, own.markdown)
    if target.id == own.id or len(target.markdown) + len(section) + 2 > MAX_MARKDOWN:
        raise HTTPException(409, "Заметка получится слишком длинной. Запись осталась отдельной.")
    user_id = capture.user_id
    top = db.scalar(select(func.max(Item.position)).where(
        Item.note_id == target.id, Item.user_id == user_id, Item.deleted_at.is_(None),
    ))
    position = -1 if top is None else top
    moved_items = []
    for item in db.scalars(select(Item).where(
        Item.note_id == own.id, Item.user_id == user_id, Item.deleted_at.is_(None),
    ).order_by(Item.position, Item.id)):
        position += 1
        moved_items.append([item.id, item.position])
        item.note_id, item.position = target.id, position
    db.flush()
    reminder_ids = list(db.scalars(select(Reminder.id).where(Reminder.note_id == own.id, Reminder.user_id == user_id)))
    if reminder_ids:
        db.execute(update(Reminder).where(Reminder.id.in_(reminder_ids)).values(note_id=target.id),
                   execution_options={"synchronize_session": False})
    for image in db.scalars(select(NoteImage).where(NoteImage.capture_id == capture.id)):
        image.note_id = target.id
    known = {c.get("id") for c in target.conclusions}
    taken = [c for c in own.conclusions if c.get("id") not in known]
    bump(db, target, target.markdown.rstrip() + "\n\n" + section, now, [*target.conclusions, *taken])
    # The own note keeps the original, hidden in the trash. Purging the target purges it too.
    db.execute(update(Note).where(Note.id == own.id).values(
        deleted_at=now, merged_into=target.id, version=Note.version + 1, updated_at=now),
        execution_options={"synchronize_session": False})
    capture.attach_state, capture.attach_note_id, capture.attach_explicit = ATTACHED, target.id, explicit
    capture.attach_decided_at, capture.attach_heading = now, heading
    capture.attach_moved = {"items": moved_items, "reminders": reminder_ids, "conclusions": [c["id"] for c in taken]}
    record_event(db, user_id, "capture_attached", capture.id, capture.channel, subject_id=target.id,
                 outcome="explicit" if explicit else "chosen")
    db.flush()
    db.expire(own)
    return target


def separate_capture(db, capture, settings=None, now=None):
    """The record stays (or becomes again) a note of its own. Returns (own note, text_kept)."""
    now = time.time() if now is None else now
    user_id = capture.user_id
    own = db.scalar(select(Note).where(Note.capture_id == capture.id, Note.user_id == user_id))
    if own is None:
        raise HTTPException(404, "Заметка не найдена")
    if capture.attach_state != ATTACHED:
        if capture.attach_state != SEPARATE:
            capture.attach_state, capture.attach_decided_at = SEPARATE, now
            record_event(db, user_id, "capture_separated", capture.id, capture.channel, subject_id=own.id,
                         outcome="declined")
        return own, False
    target = attached_target(db, capture)
    if target is None:
        raise HTTPException(409, "Заметка, куда добавили запись, в корзине или удалена. Верните её и повторите.")
    moved = capture.attach_moved or {}
    kept = False
    section = "\n\n" + section_of(capture.attach_heading or "", own.markdown)
    taken = set(moved.get("conclusions") or [])
    conclusions = [c for c in target.conclusions if c.get("id") not in taken]
    if section in target.markdown:
        markdown = target.markdown.replace(section, "", 1)
    else:
        markdown, kept = target.markdown, True  # The section was edited: leave the text, lose nothing.
    if not markdown.strip():
        markdown, kept = target.markdown, True
    bump(db, target, markdown, now, conclusions)
    positions = dict(moved.get("items") or [])
    for item in db.scalars(select(Item).where(
        Item.id.in_(list(positions)), Item.user_id == user_id, Item.note_id == target.id,
    )):
        item.note_id, item.position = own.id, positions[item.id]
    ids = list(moved.get("reminders") or [])
    if ids:
        db.execute(update(Reminder).where(Reminder.id.in_(ids), Reminder.note_id == target.id).values(note_id=own.id),
                   execution_options={"synchronize_session": False})
    for image in db.scalars(select(NoteImage).where(NoteImage.capture_id == capture.id)):
        image.note_id = own.id
    db.execute(update(Note).where(Note.id == own.id).values(
        deleted_at=None, merged_into=None, version=Note.version + 1, updated_at=now),
        execution_options={"synchronize_session": False})
    capture.attach_state, capture.attach_decided_at, capture.attach_moved = SEPARATE, now, None
    record_event(db, user_id, "capture_attach_undone", capture.id, capture.channel, subject_id=own.id)
    db.flush()
    db.expire(own)
    return own, kept


def owned_open_capture(db, capture_id, user_id):
    if "\x00" in capture_id:
        raise HTTPException(404, "Запись не найдена")
    capture = db.scalar(select(Capture).where(Capture.id == capture_id, Capture.user_id == user_id))
    if capture is None:
        raise HTTPException(404, "Запись не найдена")
    return capture


def result_of(db, capture, *, kept=False):
    target = attached_target(db, capture)
    if capture.attach_state == ATTACHED and target is not None:
        return {"capture_id": capture.id, "state": ATTACHED, "note_id": target.id, "note_title": target.title,
                "target_text_kept": False}
    own = db.scalar(select(Note).where(Note.capture_id == capture.id, Note.user_id == capture.user_id))
    return {"capture_id": capture.id, "state": SEPARATE, "note_id": own.id, "note_title": own.title,
            "target_text_kept": kept}


def choose_attach(db, user_id, capture_id, note_id, settings, now=None, commit=True):
    """«Добавить к «X»». Idempotent: pressing again answers the same."""
    now = time.time() if now is None else now
    lock_account(db, user_id)
    capture = owned_open_capture(db, capture_id, user_id)
    target = owned_note(db, note_id, user_id)  # Another person's or a trashed note is simply not found.
    if capture.attach_state == ATTACHED:
        if capture.attach_note_id == target.id:
            return result_of(db, capture)
        raise HTTPException(409, "Эта запись уже добавлена в другую заметку. Сначала отмените добавление.")
    state = offer_state(db, capture, now)
    if state is None or state[1].id != target.id:
        raise HTTPException(409, "Время выбора вышло, запись осталась отдельной заметкой.")
    attach_capture(db, capture, target, settings, explicit=False, now=now)
    if commit:
        db.commit()
    return result_of(db, capture)


def choose_separate(db, user_id, capture_id, settings=None, now=None, commit=True):
    """«Отдельная заметка» before the answer, «Отменить» after it. Idempotent."""
    now = time.time() if now is None else now
    lock_account(db, user_id)
    capture = owned_open_capture(db, capture_id, user_id)
    if capture.attach_state == ATTACHED and not can_undo(capture, now):
        raise HTTPException(409, "Отменить добавление можно в течение суток.")
    _, kept = separate_capture(db, capture, settings, now)
    if commit:
        db.commit()
    return result_of(db, capture, kept=kept)
