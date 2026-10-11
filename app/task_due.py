"""The deadline of a task as one clear moment, for the «Задачи» screen.

`due_at` wins when it is set. Otherwise the quoted `due_text` is resolved with the reminder parser, counted
from the moment the note was written, so «завтра» in an old note stays the day it meant. Nothing is stored.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.reminder_proposals import find_date, find_time, normalize, parse_relative, propose_reminder


def mentions_time(due_text, local_now):
    """True when the quote names an hour, a daypart or a relative span in minutes or hours."""
    text = normalize(due_text)
    base, rest = parse_relative(text, local_now)
    if isinstance(base, datetime):
        return True
    if base is None:
        _, _, rest = find_date(text, local_now.date())
    return find_time(rest)[2]


def resolve_due(due_at, due_text, anchor, timezone):
    """(moment as epoch seconds or None, whether the moment has a time of day). `anchor` is the note's epoch."""
    if isinstance(due_at, (int, float)):
        return float(due_at), True
    if not isinstance(due_text, str) or not due_text.strip() or not isinstance(anchor, (int, float)):
        return None, False
    try:
        zone = ZoneInfo(timezone)
        now = datetime.fromtimestamp(anchor, UTC)
        proposal = propose_reminder(due_text, now, timezone)
        if proposal is None:
            return None, False
        moment = datetime.fromisoformat(proposal["local_time"]).replace(tzinfo=zone)
        has_time = mentions_time(due_text, now.astimezone(zone).replace(tzinfo=None))
    except (ValueError, OverflowError, OSError):
        return None, False
    return moment.timestamp(), has_time
