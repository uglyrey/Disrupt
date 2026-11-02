"""Dashboard numbers without AI. Plain counts over the user's own rows, no note text.

Days are local days in the zone of the account, else in NOTES_LIMIT_TIMEZONE. The daily AI numbers
(`ai`) keep using NOTES_LIMIT_TIMEZONE, because the limit is a quota and not the person's clock.
"""

import time
from datetime import UTC, datetime, timedelta
from datetime import time as day_start
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.limits import daily_state
from app.models import Capture, Category, Item, Note, ProductEvent, Reminder, User
from app.timezones import effective_zone

STREAK_LOOKBACK_DAYS = 400
UPCOMING_DAYS = 7


def local_day(stamp, zone):
    return datetime.fromtimestamp(stamp, zone).date()


def day_floor(day, zone):
    return datetime.combine(day, day_start.min, tzinfo=zone).timestamp()


def streak_length(days_with_captures, today):
    """Consecutive local days with a capture. A quiet today does not break a streak that ends yesterday."""
    day = today if today in days_with_captures else today - timedelta(days=1)
    length = 0
    while day in days_with_captures:
        length += 1
        day -= timedelta(days=1)
    return length


def completed_tasks(db, user_id, period_start, alive_notes):
    """(tasks done now in the whole history, of them done since `period_start`). Deleting never lowers it.

    The history is the events `task_completed` and `task_reopened`, both keyed «item_id:version». The event with
    the highest version of a task says where it stands. Tasks that were done before events existed and have none
    count by their live row. Note and item rows may be gone (deleted forever): the events do not need them."""
    state = {}
    for name, operation, stamp in db.execute(select(
        ProductEvent.name, ProductEvent.operation_id, ProductEvent.occurred_at,
    ).where(ProductEvent.user_id == user_id, ProductEvent.name.in_(["task_completed", "task_reopened"]))):
        item_id, _, version = operation.rpartition(":")
        rank = int(version) if version.isdigit() else 0
        last = state.get(item_id)
        if last is None or rank > last[0] or (rank == last[0] and name == "task_reopened"):
            # The latest event decides: a task done again after a reopen counts once, at the new moment.
            state[item_id] = (rank, name, stamp)
    done = {item_id: row[2] for item_id, row in state.items() if row[1] == "task_completed"}
    live = db.scalars(select(Item.id).where(
        Item.user_id == user_id, Item.kind == "task", Item.status == "completed", Item.deleted_at.is_(None),
        Item.note_id.in_(alive_notes),
    )).all()
    # Old rows have no moment, they count in the total but not in a period.
    legacy = [item_id for item_id in live if item_id not in state]
    return len(done) + len(legacy), sum(1 for stamp in done.values() if stamp >= period_start)


def build_dashboard(db, settings, user_id, days, now=None):
    now = time.time() if now is None else now
    zone_name = effective_zone(db.get(User, user_id), settings)
    zone = ZoneInfo(zone_name)
    today = local_day(now, zone)
    first_day = today - timedelta(days=days - 1)
    period_start = day_floor(first_day, zone)
    lookback = day_floor(today - timedelta(days=max(days, STREAK_LOOKBACK_DAYS)), zone)

    captures = db.execute(select(
        Capture.created_at, Capture.channel, Capture.input_kind, Capture.processing_mode,
    ).where(Capture.user_id == user_id, Capture.created_at >= lookback)).all()
    in_period = [row for row in captures if row.created_at >= period_start]
    by_channel = {"web": 0, "telegram": 0}
    by_kind = {"text": 0, "audio": 0}
    for row in in_period:
        by_channel[row.channel] = by_channel.get(row.channel, 0) + 1
        by_kind[row.input_kind] = by_kind.get(row.input_kind, 0) + 1
    ai = sum(1 for row in in_period if row.processing_mode == "ai")

    note_stamps = db.scalars(select(Note.created_at).where(
        Note.user_id == user_id, Note.deleted_at.is_(None), Note.created_at >= period_start,
    )).all()
    per_day = {first_day + timedelta(days=offset): 0 for offset in range(days)}
    for stamp in note_stamps:
        day = local_day(stamp, zone)
        if day in per_day:
            per_day[day] += 1
    total_notes = db.scalar(select(func.count()).select_from(Note).where(
        Note.user_id == user_id, Note.deleted_at.is_(None),
    ))

    alive_notes = select(Note.id).where(Note.user_id == user_id, Note.deleted_at.is_(None))
    kinds = db.execute(
        select(Note.category_id, Item.kind, Item.status, func.count())
        .select_from(Item)
        .join(Note, (Note.id == Item.note_id) & (Note.user_id == Item.user_id))
        .where(
            Item.user_id == user_id, Item.kind.in_(["task", "idea"]), Item.deleted_at.is_(None),
            Note.deleted_at.is_(None),
        )
        .group_by(Note.category_id, Item.kind, Item.status)
    ).all()
    task_open = sum(n for _, kind, status, n in kinds if kind == "task" and status == "open")
    ideas = sum(n for _, kind, _, n in kinds if kind == "idea")
    done_total, done_in_period = completed_tasks(db, user_id, period_start, alive_notes)

    note_counts = dict(db.execute(
        select(Note.category_id, func.count())
        .where(Note.user_id == user_id, Note.deleted_at.is_(None)).group_by(Note.category_id)
    ).all())
    categories = []
    for category in db.scalars(select(Category).where(Category.user_id == user_id)):
        mine = {category.id, None} if category.is_general else {category.id}  # A note without a category is in «Общее»
        tasks = [(status, n) for cid, kind, status, n in kinds if cid in mine and kind == "task"]
        categories.append({
            "category_id": category.id, "name": category.name, "notes": sum(note_counts.get(cid, 0) for cid in mine),
            "tasks_total": sum(n for _, n in tasks),
            "tasks_done": sum(n for status, n in tasks if status == "completed"),
        })
    categories.sort(key=lambda row: (-row["notes"], row["name"].casefold(), row["category_id"]))

    horizon = now + UPCOMING_DAYS * 86400
    pending = (
        Reminder.user_id == user_id, Reminder.status == "confirmed", Reminder.scheduled_at > now,
        Reminder.note_id.in_(alive_notes), Reminder.advance_of.is_(None),
    )
    upcoming = db.scalar(
        select(func.count()).select_from(Reminder).where(*pending, Reminder.scheduled_at <= horizon)
    )
    following = db.execute(
        select(Reminder.scheduled_at, Reminder.timezone)
        .where(*pending).order_by(Reminder.scheduled_at, Reminder.id).limit(1)
    ).first()
    usage = daily_state(db, settings, user_id, now)
    total_tasks = task_open + done_total
    return {
        "days": days,
        "timezone": zone_name,
        "period": {"from": first_day.isoformat(), "to": today.isoformat()},
        "totals": {"notes": total_notes, "notes_in_period": len(note_stamps)},
        "captures": {
            "total": len(in_period), "by_channel": by_channel, "by_input_kind": by_kind,
            "ai": ai, "manual": len(in_period) - ai,
        },
        "notes_per_day": [{"date": day.isoformat(), "notes": count} for day, count in per_day.items()],
        "tasks": {
            "open": task_open, "completed": done_total,
            "completed_in_period": done_in_period,
            "completion_rate": round(done_total / total_tasks, 2) if total_tasks else None,
        },
        "ideas": ideas,
        "categories": categories,
        "streak_days": streak_length({local_day(row.created_at, zone) for row in captures}, today),
        "reminders": {
            "upcoming_7d": upcoming,
            "next_at": datetime.fromtimestamp(following.scheduled_at, UTC).isoformat() if following else None,
            "next_timezone": following.timezone if following else None,
        },
        "ai": {
            "units_used": usage["daily_units_used"], "units_limit": usage["daily_unit_limit"],
            "units_remaining": usage["daily_units_remaining"], "resets_at": usage["limit_resets_at"],
        },
    }
