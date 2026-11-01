"""«Верно?» for reminders with a guessed time: the buttons in Telegram and the written correction.

The core decides everything here, the bot only shows the result. Every call checks the owner and works only
for a reminder that is still waiting: confirmed, ahead in time, and (if it belongs to a task) the task is open.
See docs/reminder-confirm-v1.md.
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.integration import IntegrationRejection
from app.models import Item, Note, Reminder
from app.profile import time_dayparts
from app.reminder_proposals import label_for, mentions_time
from app.reminders import instant_of, reschedule, sync_note_line
from app.time_check import ASK, CONFIRMED, CORRECTED, NEEDS_CHECK, parse_words, read_model_time

EXPIRED = ("action_expired", "Напоминание отменено, изменено или уже прошло")
MAX_BUTTON_REMINDERS = 3


def local_now_of(row, now):
    return datetime.fromtimestamp(now, ZoneInfo(row.timezone)).replace(tzinfo=None)


def local_time_of(row, stamp=None):
    return datetime.fromtimestamp(row.scheduled_at if stamp is None else stamp, ZoneInfo(row.timezone)).replace(tzinfo=None)


def time_label(row, now, stamp=None):
    """«сегодня в 16:00» for the reminder (or for another UTC timestamp in its zone)."""
    return label_for(local_time_of(row, stamp), local_now_of(row, now))


def short_label(row, now, stamp):
    """Button text for the other reading: «В 04:00» on the same day, otherwise «Завтра в 04:00»."""
    other, main = local_time_of(row, stamp), local_time_of(row)
    if other.date() == main.date():
        return f"В {other:%H:%M}"
    text = label_for(other, local_now_of(row, now))
    return text[:1].upper() + text[1:]


def waiting_reminder(db, user_id, reminder_id, now):
    """The reminder if it is still waiting for its time (owner, confirmed, ahead, its task open), else None."""
    if not isinstance(reminder_id, str) or "\x00" in reminder_id:
        return None
    row = db.scalar(select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id))
    if row is None or row.status != "confirmed" or row.scheduled_at <= now:
        return None
    if db.scalar(select(Note.id).where(Note.id == row.note_id, Note.user_id == user_id, Note.deleted_at.is_(None))) is None:
        return None
    if row.item_id is not None:
        item = db.get(Item, row.item_id)
        if (
            item is None or item.user_id != user_id or item.note_id != row.note_id or item.kind != "task"
            or item.status != "open" or item.deleted_at is not None
        ):
            return None
    return row


def expired():
    return IntegrationRejection(409, *EXPIRED)


def apply_time(db, row, stamp, check, alt=None):
    """Move the reminder to `stamp` (a UTC timestamp) and record how the time was settled."""
    reschedule(db, row, stamp, row.timezone, row.text, "telegram", sync=False)
    row.time_check, row.time_alt = check, alt
    sync_note_line(db, row)  # Only a settled time reaches the note.


def button_entries(db, user_id, reminder_ids, now=None, *, edit_only=False):
    """What the bot needs to draw «Верно» / «Изменить время» for each reminder that is still waiting.

    One dict per reminder, at most three, in the order given: reminder_id, generation, alt_label (or None).
    With `edit_only` the reminder only has to be waiting (any state): the entry is for «Изменить время» alone.
    """
    now = time.time() if now is None else now
    entries = []
    for reminder_id in reminder_ids:
        row = waiting_reminder(db, user_id, reminder_id, now)
        if row is None or (not edit_only and row.time_check not in NEEDS_CHECK):
            continue
        alt = row.time_alt if row.time_alt and row.time_alt > now and not edit_only else None
        entries.append({
            "reminder_id": row.id, "generation": row.generation,
            "alt_label": short_label(row, now, alt) if alt else None,
            **({"edit_only": True} if edit_only else {}),
        })
        if len(entries) == MAX_BUTTON_REMINDERS:
            break
    return entries


def confirm_action(db, user_id, reminder_id, generation, action, now=None):
    """«Верно», «Изменить время» or the button with the other reading. Idempotent."""
    now = time.time() if now is None else now
    row = db.scalar(select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id)) \
        if isinstance(reminder_id, str) and "\x00" not in reminder_id else None
    if row is None:
        raise expired()
    if action == "alt" and row.time_check == CORRECTED and row.generation == generation + 1 \
            and waiting_reminder(db, user_id, reminder_id, now) is not None:
        return {"status": "corrected", "label": time_label(row, now)}  # The same button pressed twice.
    if row.generation != generation or waiting_reminder(db, user_id, reminder_id, now) is None:
        raise expired()
    if action == "edit":
        return {"status": "ask_time", "label": time_label(row, now)}
    if action == "ok":
        if row.time_check in NEEDS_CHECK:
            row.time_check, row.time_alt = CONFIRMED, None
            sync_note_line(db, row)
        return {"status": "confirmed", "label": time_label(row, now)}
    if row.time_check not in NEEDS_CHECK or not row.time_alt:
        raise expired()
    if row.time_alt <= now:
        raise IntegrationRejection(409, "time_passed", "Это время уже прошло")
    apply_time(db, row, row.time_alt, CORRECTED)
    return {"status": "corrected", "label": time_label(row, now)}


def answer_time(db, user_id, reminder_id, generation, text, ai_value=None, now=None, defer=None):
    """Handle the written answer to «Изменить время». No AI here: `ai_value` is the time the worker got from it.

    status set: moved, corrected. ask: moved to our best guess and the person is asked again.
    unclear: nothing understood, nothing changed. pending: our parser found nothing but the text names some time,
    and `defer(row)` queued it for the worker (it returns False when the person has too many waiting: unclear).
    """
    now = time.time() if now is None else now
    row = waiting_reminder(db, user_id, reminder_id, now)
    if row is None or row.generation != generation:
        raise expired()
    local_now = local_now_of(row, now)
    dayparts = time_dayparts(db, user_id)
    guess, certain, others = parse_words(text, local_now, dayparts)
    stamp = instant_of(guess, row.timezone) if guess is not None else None
    if stamp is not None and stamp > now:
        if certain:
            apply_time(db, row, stamp, CORRECTED)
            return {"status": "set", "label": time_label(row, now)}
        alt = next((s for s in (instant_of(o, row.timezone) for o in others) if s and s > now and s != stamp), None)
        apply_time(db, row, stamp, ASK, alt)
        return {
            "status": "ask", "label": time_label(row, now), "reminder_id": row.id, "generation": row.generation,
            "alt_label": short_label(row, now, alt) if alt else None,
        }
    model = read_model_time(ai_value, local_now)
    stamp = instant_of(model, row.timezone) if model is not None else None
    if stamp is not None and stamp > now:
        apply_time(db, row, stamp, CORRECTED)
        return {"status": "set", "label": time_label(row, now)}
    if ai_value is None and defer is not None and mentions_time(text, dayparts) and defer(row):
        return {"status": "pending"}
    return {"status": "unclear"}
