"""User-confirmed reminders. The scheduler and Telegram sender live elsewhere."""

import hashlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from datetime import time as clock
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.analytics import record_event
from app.config import Settings
from app.models import Capture, Item, Outbox, Reminder, User
from app.reminder_proposals import mentions_time, propose_reminder, sentence_spans
from app.services import cancel_reminder_deliveries, item_view, lock_account, owned_note, reminder_proposals

AUTO_KEY_PREFIX = "ai:"
MAX_REMINDER_TEXT = 1000


def reminder_view(db, reminder):
    delivery = db.scalar(select(Outbox.status).where(
        Outbox.reminder_id == reminder.id, Outbox.user_id == reminder.user_id,
        Outbox.generation == reminder.generation,
    ))
    previous_unknown = db.scalar(select(Outbox.id).where(
        Outbox.reminder_id == reminder.id, Outbox.user_id == reminder.user_id,
        Outbox.generation < reminder.generation, Outbox.status == "unknown",
        Outbox.authorized_at.is_not(None),
    ).limit(1)) is not None
    return {
        "id": reminder.id, "note_id": reminder.note_id, "item_id": reminder.item_id,
        "scheduled_at": datetime.fromtimestamp(reminder.scheduled_at, UTC).isoformat(),
        "timezone": reminder.timezone, "text": reminder.text, "status": reminder.status,
        "generation": reminder.generation,
        "confirmed_at": datetime.fromtimestamp(reminder.confirmed_at, UTC).isoformat(),
        "delivery_status": delivery,
        "previous_attempt_unknown": previous_unknown,
    }


def owned_reminder(db, reminder_id, user_id):
    if "\x00" in reminder_id:
        raise HTTPException(404, "Напоминание не найдено")
    row = db.scalar(select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id))
    if row is None:
        raise HTTPException(404, "Напоминание не найдено")
    return row


def validate_target(db, note_id, item_id, user_id):
    owned_note(db, note_id, user_id)
    if item_id is not None:
        if "\x00" in item_id:
            raise HTTPException(404, "Задача не найдена")
        item = db.scalar(select(Item).where(
            Item.id == item_id, Item.note_id == note_id, Item.user_id == user_id,
        ))
        if item is None:
            raise HTTPException(404, "Задача не найдена")
        if item.kind != "task" or item.status != "open":
            raise HTTPException(409, "Напоминание можно назначить только открытой задаче")


def future_time(value):
    scheduled_at = datetime.fromisoformat(value).timestamp()
    if scheduled_at <= time.time():
        raise HTTPException(422, "Выберите точное время в будущем")
    return scheduled_at


def create_reminder(db, user_id, note_id, body, key):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", key):
        raise HTTPException(422, "Некорректный Idempotency-Key")
    lock_account(db, user_id)
    owned_note(db, note_id, user_id)
    digest = hashlib.sha256(json.dumps(
        {"note_id": note_id, **body.model_dump()}, sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    previous = db.scalar(select(Reminder).where(
        Reminder.user_id == user_id, Reminder.idempotency_key == key,
    ))
    if previous:
        if previous.creation_hash != digest:
            raise HTTPException(409, "Этот Idempotency-Key уже использован для другого напоминания")
        return previous
    validate_target(db, note_id, body.item_id, user_id)
    row = Reminder(
        user_id=user_id, note_id=note_id, item_id=body.item_id,
        scheduled_at=future_time(body.scheduled_at), timezone=body.timezone, text=body.text,
        idempotency_key=key, creation_hash=digest,
    )
    db.add(row)
    db.flush()
    record_event(db, user_id, "reminder_confirmed", f"{row.id}:1", subject_id=row.note_id)
    return row


def edit_reminder(db, user_id, reminder_id, body):
    lock_account(db, user_id)
    row = owned_reminder(db, reminder_id, user_id)
    if row.generation != body.generation:
        raise HTTPException(409, "Напоминание уже изменено. Обновите его")
    validate_target(db, row.note_id, row.item_id, user_id)
    reschedule(db, row, future_time(body.scheduled_at), body.timezone, body.text)
    return row


def reschedule(db, row, scheduled_at, timezone, text, channel="web"):
    """The one way to move a reminder. The site edit and the Telegram snooze buttons both come here."""
    cancel_reminder_deliveries(db, row.user_id, [row.id])
    row.scheduled_at, row.timezone, row.text = scheduled_at, timezone, text
    row.status, row.confirmed_at = "confirmed", time.time()
    row.generation += 1
    record_event(db, row.user_id, "reminder_confirmed", f"{row.id}:{row.generation}", channel, subject_id=row.note_id)


def snooze_time(row, action, now):
    """New UTC timestamp for a Telegram snooze button.

    snooze_hour is now plus one hour. snooze_tomorrow is the next calendar day after today in the
    reminder's own zone, at the wall-clock time of the current schedule. Zoneinfo keeps it right across DST;
    a wall time that does not exist on that day moves forward with the clock.
    """
    if action == "snooze_hour":
        return now + 3600
    zone = ZoneInfo(row.timezone)
    wall = datetime.fromtimestamp(row.scheduled_at, zone)
    day = datetime.fromtimestamp(now, zone).date() + timedelta(days=1)
    return datetime.combine(day, clock(wall.hour, wall.minute), tzinfo=zone).timestamp()


def local_clock(row):
    return datetime.fromtimestamp(row.scheduled_at, ZoneInfo(row.timezone)).strftime("%H:%M")


def add_auto_reminder(db, note, item, text, scheduled_at, timezone):
    """One confirmed reminder for a task, keyed by the task so it is never made twice. None if it already exists."""
    key = AUTO_KEY_PREFIX + item.id
    digest = hashlib.sha256(json.dumps(
        {"note_id": note.id, "item_id": item.id, "scheduled_at": scheduled_at,
         "timezone": timezone, "text": text, "auto": True}, sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    row = Reminder(
        user_id=note.user_id, note_id=note.id, item_id=item.id, scheduled_at=scheduled_at,
        timezone=timezone, text=text, idempotency_key=key, creation_hash=digest,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None  # Another process made it first.
    return row


def auto_reminders(db, note, settings=None, now=None):
    """Set a confirmed reminder for every open task of a fresh note whose deadline can be read.

    Same parser and zone as the proposals on the note screen. The reminder is tied to the task and
    never made twice: its idempotency key is built from the task. A time in the past gives nothing.
    The caller holds the account lock and owns the transaction. Returns the created reminders.
    """
    user = db.get(User, note.user_id)
    if user is None or not user.ai_reminders_enabled:
        return []
    items = db.scalars(select(Item).where(
        Item.note_id == note.id, Item.user_id == note.user_id, Item.kind == "task", Item.status == "open",
    ).order_by(Item.position, Item.id)).all()
    proposals = reminder_proposals(db, note.user_id, [item_view(item) for item in items], settings, now)
    stamp = (now or datetime.now(UTC)).timestamp()
    created = []
    for item in items:
        proposal = proposals.get(item.id)
        text = " ".join(item.text.split())[:MAX_REMINDER_TEXT].strip()
        if not proposal or not text:
            continue
        key = AUTO_KEY_PREFIX + item.id
        if db.scalar(select(Reminder.id).where(Reminder.user_id == note.user_id, Reminder.idempotency_key == key)):
            continue
        local = datetime.fromisoformat(proposal["local_time"]).replace(tzinfo=ZoneInfo(proposal["timezone"]))
        if local.timestamp() <= stamp:
            continue
        row = add_auto_reminder(db, note, item, text, local.timestamp(), proposal["timezone"])
        if row is not None:
            created.append(row)
    return created


# Explicit requests to be reminded. They are the person's own instruction, so they are honoured
# even when «ИИ сам ставит напоминания» is off, and they do not depend on what the model returned.
REQUEST = re.compile(
    r"\b(?:напомни(?:те)?|напомнить|не\s+забыть|не\s+забудь(?:те)?|remind\s+me"
    r"|(?:постав\w*|созда\w*|сдела\w*|добав\w*)\s+(?:мне\s+)?(?:напоминани[еяю]|будильник|таймер)"
    r"|(?:нужно|надо|нужен|нужна)\s+(?:мне\s+)?(?:напоминани[еяю]|будильник|таймер))\b",
    re.IGNORECASE,
)
FILLER = re.compile(r"\b(?:мне|пожалуйста|плиз|please)\b|^(?:что|чтобы|to)\s+", re.IGNORECASE)
MAX_ITEMS = 30
DAY_START, DAY_END, MORNING_HOUR = 8, 21, 9
NOT_UNDERSTOOD = "Не понял, когда напомнить. Укажите время, например «завтра в 10»"


def request_task_text(sentence, match):
    """The action of a request: the sentence without the trigger words, first letter capital."""
    rest = sentence[:match.start()] + " " + sentence[match.end():]
    for _ in range(2):
        rest = " ".join(FILLER.sub(" ", rest).split()).strip(" ,.;:!?-–")
    rest = rest[:MAX_REMINDER_TEXT].strip()
    if not rest:
        rest = " ".join(sentence.split())[:MAX_REMINDER_TEXT]
    return rest[:1].upper() + rest[1:]


def default_reminder_time(now, settings):
    """When to remind about a request that names no time, as an aware datetime in the account zone.

    Between 08:00 and 21:00 local time it is now plus NOTES_REMINDER_DEFAULT_DELAY (an hour by default).
    Earlier it is 09:00 the same morning, later it is 09:00 the next day. A delay of 0 turns this off.
    """
    delay = settings.reminder_default_delay_seconds
    if delay <= 0:
        return None
    zone = ZoneInfo(settings.limit_timezone)
    local = now.astimezone(zone)
    if DAY_START <= local.hour < DAY_END:
        target = local + timedelta(seconds=delay)
        if target.second or target.microsecond:
            target = target.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return target
    day = local.date() + timedelta(days=1 if local.hour >= DAY_END else 0)
    return datetime.combine(day, clock(MORNING_HOUR, 0), tzinfo=zone)


def reminder_requests(db, note, original):
    """Explicit requests of the original, sentence by sentence, with the task and reminder that cover each.

    Each entry is a dict with sentence, match, task (an open task whose quote touches the sentence, or None)
    and covered (that task already has a reminder, even a cancelled one, so it is never made again).
    """
    items = db.scalars(select(Item).where(Item.note_id == note.id, Item.user_id == note.user_id)
                       .order_by(Item.position, Item.id)).all()
    taken = set(db.scalars(select(Reminder.item_id).where(
        Reminder.note_id == note.id, Reminder.user_id == note.user_id, Reminder.item_id.is_not(None),
    )))
    located = []
    for item in items:
        at = original.find(item.source_quote) if item.source_quote else -1
        if at >= 0:
            located.append((item, at, at + len(item.source_quote)))
    found = []
    for start, end in sentence_spans(original):
        sentence = original[start:end]
        match = REQUEST.search(sentence)
        if match is None:
            continue
        touching = [item for item, a, b in located if a < end and start < b]
        task = next((i for i in touching if i.kind == "task" and i.status == "open"), None)
        covered = any(i.id in taken for i in touching if i.kind == "task")
        found.append({"sentence": sentence, "match": match, "task": task, "covered": covered})
    return found


def request_reminders(db, note, original, settings=None, now=None):
    """Make sure every explicit «напомни ...» of the original has a reminder. No AI call is made.

    The time comes from the same parser as the proposals, read from the task's quoted deadline first
    and from the whole sentence second. A sentence without any time gets the default time. A sentence
    whose time cannot be used (already past, for example) gets nothing and is reported by
    `unscheduled_requests`. When the model made no task for the sentence, a task is added with the
    sentence as its quote. Idempotent through the `ai:<item id>` key. The caller owns the transaction
    and the account lock. Returns the created reminders.
    """
    settings = settings or Settings()
    now = now or datetime.now(UTC)
    zone = settings.limit_timezone
    created = []
    for request in reminder_requests(db, note, original):
        if request["covered"]:
            continue
        sentence, task = request["sentence"], request["task"]
        proposal = task and task.due_text and propose_reminder(task.due_text, now, zone)
        proposal = proposal or propose_reminder(sentence, now, zone)
        if proposal:
            local = datetime.fromisoformat(proposal["local_time"]).replace(tzinfo=ZoneInfo(proposal["timezone"]))
        elif mentions_time(sentence):
            continue
        else:
            local = default_reminder_time(now, settings)
            if local is None:
                continue
        if local.timestamp() <= now.timestamp():
            continue
        if task is None:
            count = db.scalar(select(func.count()).select_from(Item).where(Item.note_id == note.id))
            if count >= MAX_ITEMS:
                continue
            position = db.scalar(select(func.max(Item.position)).where(Item.note_id == note.id))
            task = Item(
                user_id=note.user_id, note_id=note.id, kind="task", status="open", version=1,
                text=request_task_text(sentence, request["match"]), source_quote=sentence,
                due_text=None, due_at=None, position=0 if position is None else position + 1,
            )
            db.add(task)
            db.flush()
        text = " ".join(task.text.split())[:MAX_REMINDER_TEXT].strip()
        row = add_auto_reminder(db, note, task, text, local.timestamp(), zone)
        if row is not None:
            created.append(row)
    return created


def unscheduled_requests(db, note):
    """Explicit requests that ended without a reminder, for the «Не понял, когда напомнить» line."""
    capture = db.get(Capture, note.capture_id)
    original = (capture.original_text or capture.transcript or "") if capture is not None else ""
    if not original:
        return []
    return [r["sentence"] for r in reminder_requests(db, note, original) if not r["covered"]]


def cancel_reminder(db, user_id, reminder_id, generation):
    lock_account(db, user_id)
    row = owned_reminder(db, reminder_id, user_id)
    if row.status == "cancelled" and row.generation in {generation, generation + 1}:
        return row
    if row.generation != generation:
        raise HTTPException(409, "Напоминание уже изменено. Обновите его")
    cancel_reminder_deliveries(db, user_id, [row.id])
    row.status = "cancelled"
    row.generation += 1
    record_event(db, user_id, "reminder_cancelled", f"{row.id}:{row.generation}")
    return row
