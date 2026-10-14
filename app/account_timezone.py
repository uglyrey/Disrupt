"""The time zone of an account: reading it, setting it and moving the reminders that were made by guess."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import Reminder, User
from app.reminders import reschedule
from app.services import lock_account
from app.timezones import label_for_zone, offset_minutes, utc_text, valid_zone


def timezone_state(user):
    zone = user.timezone if valid_zone(user.timezone) else None
    return {
        "timezone": zone,
        "source": user.timezone_source if zone else None,
        "label": label_for_zone(zone) if zone else None,
    }


def telegram_zone_state(user):
    """What the bot needs: the zone with its Russian name and «UTC+7», and whether it already asked."""
    state = timezone_state(user)
    zone = state["timezone"]
    return {
        **state,
        "utc": utc_text(offset_minutes(zone)) if zone else None,
        "asked": user.timezone_asked_at is not None,
    }


def move_guessed_reminders(db, user_id, zone, now, channel="web"):
    """Move reminders made with the service zone to the same wall-clock time in `zone`. Returns how many moved.

    Only pending future reminders with `zone_guessed` are moved. One that would land in the past stays
    as it is. The flag is cleared on all of them: from now on the account has a zone of its own.
    Reminders the person set with an explicit zone never have the flag.
    """
    rows = db.scalars(select(Reminder).where(Reminder.user_id == user_id, Reminder.zone_guessed.is_(True))).all()
    target_zone = ZoneInfo(zone)
    moved = 0
    for row in rows:
        if row.status == "confirmed" and row.scheduled_at > now and row.timezone != zone:
            wall = datetime.fromtimestamp(row.scheduled_at, ZoneInfo(row.timezone)).replace(tzinfo=None)
            target = wall.replace(tzinfo=target_zone).timestamp()
            if target > now:
                reschedule(db, row, target, zone, row.text, channel)
                moved += 1
        row.zone_guessed = False
    return moved


def set_account_timezone(db, user_id, zone, source, *, now=None):
    """Write the zone of an account. `auto` only fills an empty value, `manual` and `telegram` always write.

    The caller checks `valid_zone`. Returns (user, moved) where moved counts the reminders that were moved.
    """
    lock_account(db, user_id)
    user = db.get(User, user_id)
    db.refresh(user)
    previous = user.timezone if valid_zone(user.timezone) else None
    if source == "auto" and previous is not None:
        return user, 0
    user.timezone, user.timezone_source = zone, source
    moved = 0
    if previous != zone:
        moved = move_guessed_reminders(
            db, user_id, zone, time.time() if now is None else now, "telegram" if source == "telegram" else "web",
        )
    return user, moved


def zone_question_due(db, user_id):
    """True once for an account without a zone that was never asked. The mark is set in the same transaction,
    so a processed Telegram message carries the question at most one time, and a replay repeats the same answer."""
    user = db.get(User, user_id)
    if valid_zone(user.timezone):
        return False
    return mark_zone_asked(db, user_id)


def mark_zone_asked(db, user_id, *, now=None):
    """The bot asked for the zone. True only for the call that set the mark, so it asks once."""
    lock_account(db, user_id)
    user = db.get(User, user_id)
    db.refresh(user)
    if user.timezone_asked_at is not None:
        return False
    user.timezone_asked_at = int(time.time() if now is None else now)
    return True
