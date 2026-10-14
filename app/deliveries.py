"""Transactional delivery permissions. No Telegram calls or scheduler loop."""

import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from app.analytics import record_event
from app.components import record as record_component
from app.models import Capture, Item, Job, Note, Outbox, Reminder, TelegramIdentity, User
from app.reminder_proposals import label_for
from app.reminders import AUTO_KEY_PREFIX, NOT_UNDERSTOOD, unscheduled_requests
from app.security import hash_token
from app.services import item_view, lock_account, reminder_proposals
from app.timezones import by_zone, zone_guessed


def locked_delivery(db, delivery_id):
    if "\x00" in delivery_id:
        raise HTTPException(404, "Доставка не найдена")
    owner = db.scalar(select(Outbox.user_id).where(Outbox.id == delivery_id))
    if owner is None:
        raise HTTPException(404, "Доставка не найдена")
    lock_account(db, owner)
    return db.scalar(select(Outbox).where(Outbox.id == delivery_id).execution_options(populate_existing=True))


def matches_lease(row, body):
    return row.generation == body.generation and row.lease_token_hash is not None and hmac.compare_digest(
        row.lease_token_hash, hash_token(body.lease_token),
    )


def current_target(db, row, now):
    if row.job_id is not None:
        job = db.get(Job, row.job_id)
        if job is None or job.user_id != row.user_id or job.status != "succeeded" or row.generation != 1:
            return None
        capture = db.get(Capture, job.capture_id)
        note = db.scalar(select(Note).where(Note.capture_id == job.capture_id, Note.user_id == row.user_id))
        identity = db.scalar(select(TelegramIdentity).where(
            TelegramIdentity.user_id == row.user_id, TelegramIdentity.bot_id == row.bot_id,
            TelegramIdentity.chat_id == row.chat_id, TelegramIdentity.notifications_enabled.is_(True),
            TelegramIdentity.delivery_status == "available",
        ))
        if (
            capture is None or capture.user_id != row.user_id or capture.channel != "telegram"
            or note is None or note.deleted_at is not None
        ):
            return None
        return (None, note) if identity else None
    reminder = db.get(Reminder, row.reminder_id)
    if (
        reminder is None or reminder.user_id != row.user_id or reminder.generation != row.generation
        or reminder.status != "confirmed" or reminder.scheduled_at > now
    ):
        return None
    note = db.get(Note, reminder.note_id)
    if note is None or note.user_id != row.user_id or note.deleted_at is not None:
        return None
    if reminder.item_id is not None:
        item = db.get(Item, reminder.item_id)
        if (
            item is None or item.user_id != row.user_id or item.note_id != note.id
            or item.kind != "task" or item.status != "open" or item.deleted_at is not None
        ):
            return None
    identity = db.scalar(select(TelegramIdentity).where(
        TelegramIdentity.user_id == row.user_id, TelegramIdentity.bot_id == row.bot_id,
        TelegramIdentity.chat_id == row.chat_id, TelegramIdentity.notifications_enabled.is_(True),
        TelegramIdentity.delivery_status == "available",
    ))
    return (reminder, note) if identity else None


def attempt_id(row):
    return row.id + ":" + (row.lease_token_hash[:16] if row.lease_token_hash else "untracked")


MESSAGE_TTL = 900  # Account messages carry one-time links that die after 15 minutes.


def message_deliverable(db, row, now):
    """An account message goes only to the chat it was addressed to, while it is fresh."""
    if row.message_kind in {"digest_morning", "digest_evening"}:
        from app.digests import deliverable
        return deliverable(db, row, now)
    if row.message_text is None or now - row.created_at > MESSAGE_TTL:
        return False
    return db.scalar(select(TelegramIdentity.id).where(
        TelegramIdentity.user_id == row.user_id, TelegramIdentity.bot_id == row.bot_id,
        TelegramIdentity.chat_id == row.chat_id, TelegramIdentity.delivery_status == "available",
    )) is not None


def expire_authorization(db, row):
    row.status, row.error_code = "unknown", "delivery_lease_expired"
    reminder = db.get(Reminder, row.reminder_id) if row.reminder_id is not None else None
    if (
        reminder is not None and reminder.user_id == row.user_id
        and reminder.generation == row.generation and reminder.status == "confirmed"
    ):
        reminder.status = "unknown"
    if row.message_kind is not None:
        row.message_text = None  # Never resent, so the one-time link must not stay in the database.
        return
    event = "processing_reply_failed" if row.job_id is not None else "reminder_failed"
    record_event(db, row.user_id, event, attempt_id(row), "telegram", outcome="unknown")


REPLY_UNITS, TELEGRAM_UNITS = 3500, 4096
REPLY_FOOTER = "\n\nПолный результат в Beresta."
HINT_TASK_CHARS = 100
HINT_LIST = 5


def utf16_units(text):
    return len(text.encode("utf-16-le")) // 2


def clip(text, units):
    """Cut text to at most `units` UTF-16 units without splitting a character. Adds an ellipsis when cut."""
    if utf16_units(text) <= units:
        return text
    cut = text.encode("utf-16-le")[:max(units - 1, 0) * 2].decode("utf-16-le", errors="ignore")
    return cut.rstrip() + "…"


def short_task(text):
    task = " ".join(text.split())
    return task if len(task) <= HINT_TASK_CHARS else task[:HINT_TASK_CHARS - 1].rstrip() + "…"


def auto_reminder_hint(db, note, settings, now):
    """The line for reminders the AI has set for this note, or an empty string. They are still ahead and can be changed."""
    rows = db.scalars(select(Reminder).where(
        Reminder.note_id == note.id, Reminder.user_id == note.user_id, Reminder.status == "confirmed",
        Reminder.scheduled_at > now, Reminder.idempotency_key.like(AUTO_KEY_PREFIX + "%"),
    ).order_by(Reminder.scheduled_at, Reminder.id)).all()
    if not rows:
        return ""
    first = rows[0]
    link = settings.public_origin + "/?capture=" + note.capture_id + "&reminder=" + first.id

    def when(row):
        zone = ZoneInfo(row.timezone)
        local = datetime.fromtimestamp(row.scheduled_at, zone).replace(tzinfo=None)
        text = label_for(local, datetime.fromtimestamp(now, zone).replace(tzinfo=None))
        # The account has no zone yet, so the time was read in the service zone. Say which one.
        return text + " " + by_zone(row.timezone) if row.zone_guessed else text

    if len(rows) == 1:
        return f"Напоминание поставлено: {when(first)}, «{short_task(first.text)}». Изменить: {link}"
    lines = [f"• {when(row)}, «{short_task(row.text)}»" for row in rows[:HINT_LIST]]
    if len(rows) > HINT_LIST:
        lines.append(f"и ещё {len(rows) - HINT_LIST}")
    return "Напоминания поставлены:\n" + "\n".join(lines) + f"\nИзменить: {link}"


def reminder_hint(db, note, settings, now=None):
    """One line about reminders for a processed note, or an empty string.

    Reminders the AI has set are named first. Otherwise the line offers a reminder for the first open
    task whose deadline can be read, and nothing is stored: the link opens the note, where the user
    confirms or changes the time.
    """
    missed = NOT_UNDERSTOOD if unscheduled_requests(db, note) else ""
    done = auto_reminder_hint(db, note, settings, time.time() if now is None else now.timestamp())
    if done:
        return done + ("\n" + missed if missed else "")
    items = [item_view(item) for item in db.scalars(
        select(Item).where(Item.note_id == note.id, Item.user_id == note.user_id, Item.deleted_at.is_(None))
        .order_by(Item.position, Item.id)
    )]
    proposals = reminder_proposals(db, note.user_id, items, settings, now)
    for item in items:
        proposal = proposals.get(item["id"])
        if proposal:
            link = settings.public_origin + "/?capture=" + note.capture_id + "#remind"
            when = proposal["label"]
            if zone_guessed(db.get(User, note.user_id)):
                when += " " + by_zone(proposal["timezone"])
            line = (
                f"Предлагаю напоминание: {when}, «{short_task(item['text'])}». "
                f"Подтвердить или изменить время: {link}"
            )
            return line + ("\n" + missed if missed else "")
    return missed


def add_reminder_set_message(db, note, identity, settings):
    """A note saved without AI has no processing reply, so a reminder it got is confirmed in a short message."""
    text = reminder_hint(db, note, settings)
    if text:
        db.add(Outbox(
            user_id=identity.user_id, bot_id=identity.bot_id, chat_id=identity.chat_id, generation=1,
            status="pending", message_kind="reminder_set", message_text=text,
        ))


def reminder_message_text(reminder, note):
    """Plain text of a due reminder: a heading, the reminder itself, and the note it came from."""
    source = ""
    if note.title and note.title.strip():
        source = "\n\nИз заметки «" + clip(" ".join(note.title.split()), 200) + "»"
    head = "Напоминание\n\n"
    room = TELEGRAM_UNITS - utf16_units(head) - utf16_units(source)
    return head + clip(reminder.text.strip(), room) + source


def processing_reply_text(note, hint=""):
    """Reply text. The note part stays within 3500 UTF-16 units, the whole text within Telegram's 4096."""
    tail = "\n\n" + hint if hint else ""
    text = "Заметка готова.\n\n" + note.title + "\n\n" + note.markdown
    room = min(REPLY_UNITS, TELEGRAM_UNITS - utf16_units(tail) - utf16_units(REPLY_FOOTER))
    if utf16_units(text) > room:
        text = text.encode("utf-16-le")[:room * 2].decode("utf-16-le", errors="ignore") + REPLY_FOOTER
    return text + tail


def claim_deliveries(db, body, settings):
    now = time.time()
    expired = db.execute(select(Outbox.id, Outbox.user_id, Outbox.created_at).where(
        Outbox.bot_id == body.bot_id, Outbox.status == "authorized", Outbox.lease_until <= now,
    ).order_by(Outbox.user_id, Outbox.created_at, Outbox.id).limit(100)).all()
    ready = db.execute(
        select(Outbox.id, Outbox.user_id, Outbox.created_at)
        .join(Reminder, Reminder.id == Outbox.reminder_id)
        .join(Note, and_(Note.id == Reminder.note_id, Note.user_id == Outbox.user_id))
        .join(TelegramIdentity, and_(
            TelegramIdentity.user_id == Outbox.user_id, TelegramIdentity.bot_id == Outbox.bot_id,
            TelegramIdentity.chat_id == Outbox.chat_id,
        ))
        .outerjoin(Item, Item.id == Reminder.item_id)
        .where(
            Outbox.bot_id == body.bot_id, Reminder.user_id == Outbox.user_id,
            Reminder.generation == Outbox.generation, Reminder.status == "confirmed", Reminder.scheduled_at <= now,
            Note.deleted_at.is_(None),
            TelegramIdentity.notifications_enabled.is_(True), TelegramIdentity.delivery_status == "available",
            or_(Reminder.item_id.is_(None), and_(
                Item.user_id == Outbox.user_id, Item.note_id == Reminder.note_id, Item.kind == "task",
                Item.status == "open", Item.deleted_at.is_(None),
            )),
            or_(
                and_(Outbox.status == "pending", Outbox.authorized_at.is_(None)),
                and_(Outbox.status == "retryable", Outbox.result_hash.is_not(None), Outbox.retry_at <= now),
                and_(Outbox.status == "leased", Outbox.authorized_at.is_(None), Outbox.lease_until <= now),
            ),
        ).order_by(Outbox.user_id, Outbox.created_at, Outbox.id).limit(body.limit)
    ).all()
    ready_replies = db.execute(
        select(Outbox.id, Outbox.user_id, Outbox.created_at)
        .join(Job, and_(Job.id == Outbox.job_id, Job.user_id == Outbox.user_id))
        .join(Note, and_(Note.capture_id == Job.capture_id, Note.user_id == Outbox.user_id))
        .join(TelegramIdentity, and_(
            TelegramIdentity.user_id == Outbox.user_id, TelegramIdentity.bot_id == Outbox.bot_id,
            TelegramIdentity.chat_id == Outbox.chat_id,
        ))
        .where(
            Outbox.bot_id == body.bot_id, Job.status == "succeeded", Outbox.generation == 1, Note.deleted_at.is_(None),
            TelegramIdentity.notifications_enabled.is_(True), TelegramIdentity.delivery_status == "available",
            or_(
                and_(Outbox.status == "pending", Outbox.authorized_at.is_(None)),
                and_(Outbox.status == "retryable", Outbox.result_hash.is_not(None), Outbox.retry_at <= now),
                and_(Outbox.status == "leased", Outbox.authorized_at.is_(None), Outbox.lease_until <= now),
            ),
        ).order_by(Outbox.user_id, Outbox.created_at, Outbox.id).limit(body.limit)
    ).all()
    ready_messages = db.execute(
        select(Outbox.id, Outbox.user_id, Outbox.created_at)
        .where(
            Outbox.bot_id == body.bot_id, Outbox.message_kind.is_not(None),
            or_(
                and_(Outbox.status == "pending", Outbox.authorized_at.is_(None)),
                and_(Outbox.status == "retryable", Outbox.result_hash.is_not(None), Outbox.retry_at <= now),
                and_(Outbox.status == "leased", Outbox.authorized_at.is_(None), Outbox.lease_until <= now),
            ),
        ).order_by(Outbox.user_id, Outbox.created_at, Outbox.id).limit(body.limit)
    ).all()
    results = []
    # All account locks follow the same order, including expired attempts.
    candidates = sorted({
        (row.user_id, row.created_at, row.id) for row in [*expired, *ready, *ready_replies, *ready_messages]
    })
    for _, _, delivery_id in candidates:
        row = locked_delivery(db, delivery_id)
        now = time.time()
        if row.status == "authorized" and row.lease_until <= now:
            expire_authorization(db, row)
            continue
        if len(results) >= body.limit:
            continue
        if not (
            row.status == "pending" and row.authorized_at is None
            or row.status == "retryable" and row.result_hash is not None and row.retry_at <= now
            or row.status == "leased" and row.authorized_at is None and row.lease_until <= now
        ):
            continue
        message = row.message_kind is not None
        if message:
            if not message_deliverable(db, row, now):
                row.status, row.error_code, row.message_text = "cancelled", "delivery_invalid", None
                continue
            reminder = note = None
        else:
            target = current_target(db, row, now)
            if target is None:
                continue
            reminder, note = target
        lease = secrets.token_urlsafe(32)
        callback = secrets.token_urlsafe(32) if reminder is not None else None
        row.status, row.lease_token_hash = "leased", hash_token(lease)
        row.lease_until = now + settings.delivery_lease_seconds
        row.authorized_at = row.result_hash = row.retry_at = row.error_code = row.telegram_message_id = None
        row.callback_token_hash = hash_token(callback) if callback else None
        results.append({
            "delivery_id": row.id, "lease_token": lease, "generation": row.generation,
            "chat_id": row.chat_id,
            "text": row.message_text if message else reminder_message_text(reminder, note) if reminder is not None
            else processing_reply_text(note, reminder_hint(db, note, settings)),
            "note_url": settings.public_origin + "/" if message else settings.public_origin
                        + "/?capture=" + note.capture_id
                        + ("&reminder=" + reminder.id if reminder is not None else ""), "callback_token": callback,
        })
    db.flush()
    return {"items": results}


def authorize_delivery(db, delivery_id, body, settings):
    row = locked_delivery(db, delivery_id)
    now = time.time()
    if (
        not matches_lease(row, body) or row.status != "leased"
        or row.lease_until is None or row.lease_until <= now
    ):
        return {"send": False}
    deliverable = message_deliverable(db, row, now) if row.message_kind is not None else (
        current_target(db, row, now) is not None
    )
    if not deliverable:
        row.status, row.error_code = "cancelled", "delivery_invalid"
        row.message_text = None
        return {"send": False}
    row.status, row.authorized_at = "authorized", now
    row.lease_until = now + settings.delivery_lease_seconds
    return {"send": True}


def record_delivery_result(db, delivery_id, body):
    row = locked_delivery(db, delivery_id)
    if not matches_lease(row, body) or row.authorized_at is None:
        raise HTTPException(409, "Результат не относится к разрешённой попытке")
    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    if row.result_hash is not None:
        if not hmac.compare_digest(row.result_hash, digest):
            raise HTTPException(409, "Для этой попытки уже записан другой результат")
        return {"status": "recorded"}
    if row.status not in {"authorized", "unknown"}:
        raise HTTPException(409, "Попытка не была разрешена")
    row.status, row.result_hash = body.status, digest
    record_component(db, row.user_id, attempt_id(row), "telegram_send", "telegram",
                     status="succeeded" if body.status == "sent" else "unknown" if body.status == "unknown" else "failed",
                     error_code=body.error_code, occurred_at=row.authorized_at)
    row.telegram_message_id, row.error_code = body.telegram_message_id, body.error_code
    row.retry_at = time.time() + body.retry_after_seconds if body.status == "retryable" else None
    reminder = db.get(Reminder, row.reminder_id) if row.reminder_id is not None else None
    if (
        reminder is not None and reminder.user_id == row.user_id
        and reminder.generation == row.generation and reminder.status in {"confirmed", "unknown"}
    ):
        reminder.status = "confirmed" if body.status == "retryable" else body.status
    if body.status == "blocked":
        identity = db.scalar(select(TelegramIdentity).where(
            TelegramIdentity.user_id == row.user_id, TelegramIdentity.bot_id == row.bot_id,
            TelegramIdentity.chat_id == row.chat_id,
        ))
        if identity:
            identity.delivery_status = "blocked"
    if row.message_kind is not None:
        if body.status != "retryable":
            row.message_text = None  # The attempt is final: keep no one-time link.
        return {"status": "recorded"}
    prefix = "processing_reply" if row.job_id is not None else "reminder"
    record_event(
        db, row.user_id, prefix + ("_sent" if body.status == "sent" else "_failed"),
        attempt_id(row), "telegram", outcome=body.status,
    )
    record_event(db, row.user_id, prefix + "_result", attempt_id(row), "telegram", outcome=body.status)
    return {"status": "recorded"}
