"""The «Добавить в Google Календарь» link: a prefilled event in the person's own Google Calendar.

No sign-in with Google and no access to the account: the link only opens Google's page with the fields filled in, and
the person presses «Сохранить» there. Nothing is sent to Google by beresta. See docs/profile-v1.md.
"""

from datetime import UTC, datetime
from urllib.parse import quote

BASE = "https://calendar.google.com/calendar/render?action=TEMPLATE"
DEFAULT_MINUTES = 60
TITLE_CHARS = 120
MAX_URL_CHARS = 1800  # Telegram accepts a URL button of up to about 2000 characters. Keep well below it.
DETAILS_LINE = "Напоминание из beresta"
# The text of the link at the end of a Telegram confirmation line. It is an inline text link, not a button.
LABEL = "Добавить в Google Календарь"
LINE_END = " " + LABEL


def stamp(moment):
    return datetime.fromtimestamp(moment, UTC).strftime("%Y%m%dT%H%M%SZ")


def clean_title(text):
    title = " ".join((text or "").split())
    title = "".join(char for char in title if char.isprintable())
    if len(title) > TITLE_CHARS:
        title = title[:TITLE_CHARS - 1].rstrip() + "…"
    return title or "Напоминание"


def note_link(public_origin, capture_id, reminder_id):
    """The same address the Telegram reminder buttons open."""
    return f"{public_origin}/?capture={capture_id}&reminder={reminder_id}"


def event_seconds(minutes):
    return (minutes if isinstance(minutes, int) and not isinstance(minutes, bool) and 5 <= minutes <= 720
            else DEFAULT_MINUTES) * 60


def calendar_url(text, scheduled_at, link, minutes=None):
    """URL of Google's «new event» page for an event at `scheduled_at` (a UTC timestamp) that lasts `minutes`.

    `minutes` is the reminder's own length or the person's default; an unusable value gives an hour."""
    ends = scheduled_at + event_seconds(minutes)
    title, details = clean_title(text), DETAILS_LINE + "\n" + link
    while True:
        url = (
            f"{BASE}&text={quote(title, safe='')}&dates={stamp(scheduled_at)}/{stamp(ends)}"
            f"&details={quote(details, safe='')}"
        )
        shorter = title[:max(10, len(title) // 2)].rstrip() + "…"
        if len(url) <= MAX_URL_CHARS or shorter == title or len(shorter) >= len(title):
            break
        title = shorter
    if len(url) > MAX_URL_CHARS:  # A very long site address: keep the event, drop the details.
        url = f"{BASE}&text={quote(title, safe='')}&dates={stamp(scheduled_at)}/{stamp(ends)}"
    return url
