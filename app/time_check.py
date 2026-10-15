"""Two readings of one reminder time, and what to do when they differ.

The model works out the local time (`due_at`), our parser reads the quoted words (`due_text`). Everything here
works on naive local wall-clock datetimes in the zone of the reminder. No database, no network, no AI.
See docs/reminder-confirm-v1.md.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from app import reminder_proposals
from app.reminder_proposals import MAX_AHEAD

ASK, UNCLEAR, CONFIRMED, CORRECTED = "ask", "unclear", "confirmed", "corrected"
# Reminders in these two states still wait for «Верно» or a new time. The others are settled.
NEEDS_CHECK = frozenset({ASK, UNCLEAR})
SKEW = timedelta(minutes=2)  # The model reads the clock a little before our code does.
LOCAL_MINUTE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


@dataclass(frozen=True)
class Reading:
    """When to remind and how sure we are. `when` is None if nothing usable was found."""

    when: datetime | None
    check: str | None = None  # None: certain, no question. ask: set, but the person should look. confirmed: two readings agree.
    alt: datetime | None = None  # The other reading, for a button.


def read_model_time(value, local_now, *, skew=timedelta(0)):
    """The model's `due_at` as a naive local datetime, or None if it is malformed, past or too far ahead."""
    if not isinstance(value, str) or not LOCAL_MINUTE.fullmatch(value.strip()):
        return None
    try:
        moment = datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return moment if local_now - skew < moment <= local_now + MAX_AHEAD else None


def parse_words(text, local_now):
    """Our parser's answer for a phrase: (best guess or None, certain, other readings). Past readings are dropped."""
    if not isinstance(text, str) or not text.strip():
        return None, False, []
    detail = reminder_proposals.parse_due_text_detailed(text, local_now)
    guess = detail.get("result")
    if not isinstance(guess, datetime) or guess <= local_now:
        guess = None
    others = [a for a in detail.get("alternatives") or () if isinstance(a, datetime) and a > local_now]
    return guess, bool(detail.get("certain")) and guess is not None, others


def other_reading(chosen, *candidates):
    """The first candidate that is a different minute than `chosen`, or None."""
    for candidate in candidates:
        if isinstance(candidate, datetime) and candidate != chosen:
            return candidate
    return None


def decide(words, model_value, local_now):
    """Combine the parser's reading of `words` with the model's `due_at` (either may be missing).

    Both agree to the minute: confirmed, even if the parser alone would have doubted (two independent readings
    met). The model is present and the parser disagrees or finds nothing: the model's time stands and the person
    is asked. No model time: a sure parser needs no question, a doubtful one is asked about. Nothing usable:
    `Reading(None)`.
    """
    local_now = local_now.replace(second=0, microsecond=0)
    model = read_model_time(model_value, local_now)
    guess, certain, others = parse_words(words, local_now)
    if model is not None:
        if guess == model:
            return Reading(model, CONFIRMED)
        return Reading(model, ASK, other_reading(model, guess, *others))
    if guess is None:
        return Reading(None)
    if certain:
        return Reading(guess)
    return Reading(guess, ASK, other_reading(guess, *others))
