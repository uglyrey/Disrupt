"""Opt-in recaps of owned notes and tasks, without paid model calls."""

import time
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.models import Item, Note, Outbox, TelegramDigestSettings, TelegramIdentity, User
from app.services import lock_account

DIGEST_KINDS = ("digest_morning", "digest_evening")
FRESH_SECONDS = 90 * 60


def available_identity(db, user_id):
    return db.scalar(select(TelegramIdentity).where(
        TelegramIdentity.user_id == user_id, TelegramIdentity.notifications_enabled.is_(True),
        TelegramIdentity.delivery_status == "available",
    ).order_by(TelegramIdentity.created_at, TelegramIdentity.id).limit(1))


def settings_view(db, user_id):
    row = db.get(TelegramDigestSettings, user_id)
    return {
        "morning_enabled": row.morning_enabled if row else False,
        "evening_enabled": row.evening_enabled if row else False,
        "morning_time": row.morning_time if row else "09:00",
        "evening_time": row.evening_time if row else "20:00",
        "timezone": row.timezone if row else "Europe/Moscow",
        "telegram_available": available_identity(db, user_id) is not None,
        "configured": row is not None,
    }


def update_settings(db, user_id, body):
    lock_account(db, user_id)
    row = db.get(TelegramDigestSettings, user_id)
    values = body.model_dump()
    if row is not None and all(getattr(row, name) == value for name, value in values.items()):
        return settings_view(db, user_id)
    if row is None:
        row = TelegramDigestSettings(user_id=user_id, generation=1)
        db.add(row)
    else:
        row.generation += 1
    for name, value in values.items():
        setattr(row, name, value)
    row.updated_at = time.time()
    # Recheck the subscription again at authorization. An authorized send can already be in flight.
    for pending in db.scalars(select(Outbox).where(
        Outbox.user_id == user_id, Outbox.message_kind.in_(DIGEST_KINDS),
        Outbox.status.in_(["pending", "leased", "retryable"]),
    )):
        pending.status, pending.error_code, pending.message_text = "cancelled", "digest_settings_changed", None
    db.commit()
    return settings_view(db, user_id)


def slot_timestamp(local, clock, zone):
    """One local wall time: first fold on DST overlap, skip nonexistent time on spring transition."""
    hour, minute = map(int, clock.split(":"))
    wall = local.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None)
    candidates = []
    for fold in (0, 1):
        instant = wall.replace(tzinfo=zone, fold=fold).timestamp()
        if datetime.fromtimestamp(instant, zone).replace(tzinfo=None) == wall:
            candidates.append(instant)
    return min(candidates) if candidates else None


def digest_text(db, user_id, slot, local):
    from app.deliveries import clip

    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    # Morning looks back at yesterday's notes; evening looks at today's notes.
    note_start = start - timedelta(days=1) if slot == "morning" else start
    note_end = start if slot == "morning" else end
    notes = db.scalars(select(Note).where(
        Note.user_id == user_id, Note.created_at >= note_start.timestamp(),
        Note.created_at < note_end.timestamp(),
    ).order_by(Note.created_at.desc(), Note.id).limit(5)).all()
    tasks = db.scalars(select(Item).join(Note, Note.id == Item.note_id).where(
        Item.user_id == user_id, Note.user_id == user_id, Item.kind == "task", Item.status == "open",
        Item.due_at.is_not(None), Item.due_at < end.timestamp(),
    ).order_by(Item.due_at, Item.id).limit(5)).all()
    if not notes and not tasks:
        return None  # No empty daily ping.
    title = "Утренний обзор" if slot == "morning" else "Вечерний обзор"
    lines = [title + " · " + local.strftime("%d.%m"), ""]
    if notes:
        lines.append("Заметки за вчера:" if slot == "morning" else "Заметки за сегодня:")
        lines.extend("• " + clip(" ".join(note.title.split()), 200) for note in notes)
    if tasks:
        lines.extend(["", "Открытые задачи со сроком до конца дня:"])
        lines.extend("• " + clip(" ".join(task.text.split()), 240) for task in tasks)
    lines.extend(["", "Это обзор ваших записей, без нового запроса к ИИ.",
                  "Время и отключение: Beresta → Подключить Telegram → Ежедневный обзор."])
    return clip("\n".join(lines), 3500)


def deliverable(db, row, now):
    settings = db.get(TelegramDigestSettings, row.user_id)
    user = db.get(User, row.user_id)
    slot = row.message_kind.removeprefix("digest_")
    identity = db.scalar(select(TelegramIdentity.id).where(
        TelegramIdentity.user_id == row.user_id, TelegramIdentity.bot_id == row.bot_id,
        TelegramIdentity.chat_id == row.chat_id, TelegramIdentity.notifications_enabled.is_(True),
        TelegramIdentity.delivery_status == "available",
    ))
    return bool(settings and user and user.deletion_requested_at is None and identity
                and getattr(settings, slot + "_enabled", False)
                and settings.generation == row.generation and row.message_text is not None
                and 0 <= now - row.created_at <= FRESH_SECONDS)


def enqueue(sessions, limit=100, *, now=None):
    now = time.time() if now is None else now
    with sessions() as db:
        users = db.scalars(select(TelegramDigestSettings.user_id).join(User).where(
            User.deletion_requested_at.is_(None),
            or_(TelegramDigestSettings.morning_enabled.is_(True), TelegramDigestSettings.evening_enabled.is_(True)),
        ).order_by(TelegramDigestSettings.user_id)).all()
    created = 0
    for user_id in users:
        if created >= limit:
            break
        with sessions.begin() as db:
            lock_account(db, user_id)
            settings = db.get(TelegramDigestSettings, user_id)
            user = db.get(User, user_id)
            identity = available_identity(db, user_id)
            if not settings or not identity or not user or user.deletion_requested_at is not None:
                continue
            zone = ZoneInfo(settings.timezone)
            local = datetime.fromtimestamp(now, zone)
            for slot in ("morning", "evening"):
                if created >= limit or not getattr(settings, slot + "_enabled"):
                    continue
                scheduled = slot_timestamp(local, getattr(settings, slot + "_time"), zone)
                if scheduled is None or not 0 <= now - scheduled <= FRESH_SECONDS:
                    continue
                # Enabling a schedule after today's slot never sends a surprise catch-up message.
                if settings.updated_at > scheduled:
                    continue
                delivery_id = str(uuid5(NAMESPACE_URL, f"beresta:digest:{user_id}:{local.date()}:{slot}"))
                if db.get(Outbox, delivery_id) is not None:
                    continue
                text = digest_text(db, user_id, slot, local)
                # Persist the empty slot as cancelled too: later notes cannot trigger an unexpected catch-up.
                db.add(Outbox(id=delivery_id, user_id=user_id, bot_id=identity.bot_id, chat_id=identity.chat_id,
                              generation=settings.generation, message_kind="digest_" + slot,
                              message_text=text, status="pending" if text else "cancelled", created_at=scheduled))
                created += 1
    return created
