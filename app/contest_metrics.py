"""One versioned definition shared by the dashboard and the content-free contest export."""

import math
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from datetime import time as day_time
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from app.models import Capture, ComponentCall, Note, ProductEvent, ProviderUsage, User

METHOD_VERSION = "saved-and-viewed-v2"
ACTIVE = {
    "capture_saved",
    "note_opened",
    "note_edited",
    "reminder_confirmed",
    "task_completed",
    "assistant_requested",
}
MOSCOW = ZoneInfo("Europe/Moscow")


def day(timestamp):
    return datetime.fromtimestamp(timestamp, MOSCOW).date()


def midnight(value):
    return datetime.combine(value, day_time.min, MOSCOW).timestamp()


def target_completions(db, high, *, low=None, source="all", channel="all"):
    """A saved capture and a recorded view of its resulting note. Never reconstruct absent views."""
    saved, opened = aliased(ProductEvent), aliased(ProductEvent)
    matching_note = or_(
        opened.subject_id == Note.id,
        and_(
            opened.subject_id.is_(None),
            func.substr(opened.operation_id, 1, 36) == Note.id,
            func.substr(opened.operation_id, 37, 1) == ":",
        ),
    )
    query = (
        select(User.id, func.min(opened.occurred_at))
        .join(saved, saved.user_id == User.id)
        .join(Capture, and_(Capture.id == saved.operation_id, Capture.user_id == User.id))
        .join(Note, and_(Note.capture_id == Capture.id, Note.user_id == User.id))
        .join(opened, and_(opened.user_id == User.id, matching_note))
        .where(
            User.is_test.is_(False),
            saved.is_test.is_(False),
            opened.is_test.is_(False),
            saved.name == "capture_saved",
            opened.name == "note_opened",
            saved.occurred_at <= opened.occurred_at,
            opened.occurred_at < high,
        )
        .group_by(User.id)
    )
    if source != "all":
        query = query.where(User.source == source)
    if low is not None:
        query = query.where(opened.occurred_at >= low)
    if channel != "all":
        # Attribution follows the original capture; a Telegram note may be viewed on the web.
        query = query.where(Capture.channel == channel)
    return dict(db.execute(query).all())


def first_targets(db, high, *, source="all"):
    return target_completions(db, high, source=source)


def report(db, start, end, *, source="all", channel="all", counted_components=("llm", "stt"), now=None):
    """Daily ratios use the same qualified population and include zero-activity days in the mean."""
    now = time.time() if now is None else now
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last or (last - first).days >= 366:
        raise ValueError("Invalid contest period")
    low, high = midnight(first), min(midnight(last + timedelta(days=1)), math.nextafter(now, math.inf))
    qualified = first_targets(db, high, source=source)
    active = defaultdict(set)
    events = (
        select(ProductEvent.user_id, ProductEvent.name, ProductEvent.occurred_at)
        .join(User)
        .where(
            User.is_test.is_(False),
            ProductEvent.is_test.is_(False),
            ProductEvent.occurred_at >= low,
            ProductEvent.occurred_at < high,
            ProductEvent.name.in_(ACTIVE),
        )
    )
    if channel != "all":
        events = events.where(ProductEvent.channel == channel)
    for uid, _, at in db.execute(events.execution_options(yield_per=2000)):
        if uid in qualified and day(at) >= day(qualified[uid]):
            active[day(at)].add(uid)
    by_day, breakdown = defaultdict(Counter), Counter()
    for model, name_field in ((ProviderUsage, ProviderUsage.kind), (ComponentCall, ComponentCall.component)):
        query = (
            select(model.user_id, model.occurred_at, name_field, model.status)
            .join(User)
            .where(
                User.is_test.is_(False),
                model.is_test.is_(False),
                model.occurred_at >= low,
                model.occurred_at < high,
            )
        )
        if model is ProviderUsage:
            query = query.where(ProviderUsage.model != "mock")
        if channel != "all":
            query = query.where(model.channel == channel)
        for uid, at, component, status in db.execute(query.execution_options(yield_per=2000)):
            if uid not in active[day(at)]:
                continue
            by_day[day(at)][component] += 1
            breakdown[(component, status)] += 1
    days = []
    cursor = first
    while cursor <= last:
        dau = len(active[cursor])
        calls = sum(n for name, n in by_day[cursor].items() if name in counted_components)
        models = sum(by_day[cursor][name] for name in ("llm", "stt"))
        recorded = sum(by_day[cursor].values())
        days.append(
            {
                "date": cursor.isoformat(),
                "dau": dau,
                "calls": calls,
                "model_calls": models,
                "recorded_calls": recorded,
                "calls_per_dau": calls / dau if dau else None,
                "recorded_calls_per_dau": recorded / dau if dau else None,
            }
        )
        cursor += timedelta(days=1)
    average = sum(row["dau"] for row in days) / len(days)
    ratio = sum(row["calls_per_dau"] or 0 for row in days) / len(days)
    source_query = select(User.source).where(User.is_test.is_(False), User.created_at < high)
    if source != "all":
        source_query = source_query.where(User.source == source)
    sources = Counter(db.scalars(source_query))
    real_sources = (
        Counter(db.scalars(select(User.source).where(User.id.in_(qualified)))) if qualified else Counter()
    )
    return {
        "method_version": METHOD_VERSION,
        "start": start,
        "end": end,
        "days": len(days),
        "completed_days": sum(
            midnight(date.fromisoformat(row["date"]) + timedelta(days=1)) <= now for row in days
        ),
        "preliminary": midnight(last + timedelta(days=1)) > now,
        "real_users": len(qualified),
        "average_dau": average,
        "average_calls_per_dau": ratio,
        "days_dau_1000": sum(row["dau"] >= 1000 for row in days),
        "days_dau_2500": sum(row["dau"] >= 2500 for row in days),
        "counted_components": list(counted_components),
        "component_method_pending": any(name not in counted_components for name, _ in breakdown),
        "sources": [
            {"source": tag, "registrations": n, "real_users": real_sources[tag]}
            for tag, n in sorted(sources.items())
        ],
        "components": [
            {"component": name, "status": status, "calls": n, "counted": name in counted_components}
            for (name, status), n in sorted(breakdown.items())
        ],
        "daily": days,
    }
