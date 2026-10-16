"""The «Добавить в Google Календарь» link: a prefilled event in the person's own Google Calendar.

No sign-in with Google and no access to the account: the link only opens Google's page with the fields filled in, and
the person presses «Сохранить» there. Nothing is sent to Google by beresta. See docs/profile-v1.md.
"""

from datetime import UTC, datetime
from urllib.parse import quote

BASE = "https://calendar.google.com/calendar/render?action=TEMPLATE"
EVENT_SECONDS = 15 * 60
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


def calendar_url(text, scheduled_at, link):
    """URL of Google's «new event» page for a 15 minute event at `scheduled_at` (a UTC timestamp)."""
    title, details = clean_title(text), DETAILS_LINE + "\n" + link
    while True:
        url = (
            f"{BASE}&text={quote(title, safe='')}&dates={stamp(scheduled_at)}/{stamp(scheduled_at + EVENT_SECONDS)}"
            f"&details={quote(details, safe='')}"
        )
        shorter = title[:max(10, len(title) // 2)].rstrip() + "…"
        if len(url) <= MAX_URL_CHARS or shorter == title or len(shorter) >= len(title):
            break
        title = shorter
    if len(url) > MAX_URL_CHARS:  # A very long site address: keep the event, drop the details.
        url = f"{BASE}&text={quote(title, safe='')}&dates={stamp(scheduled_at)}/{stamp(scheduled_at + EVENT_SECONDS)}"
    return url
