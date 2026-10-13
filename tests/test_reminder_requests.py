"""A person who asks to be reminded gets a reminder. Prompt, quote grounding, server-side safety net, parser phrases."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Item, Note, User
from app.providers import SYSTEM_PROMPT, MockProvider, repair_structure_quotes, validate_result
from app.reminder_proposals import mentions_time, propose_reminder, sentence_spans
from app.reminders import NOT_UNDERSTOOD, request_reminders
from app.schemas import StructuredNote
from app.worker import Worker
from tests.test_ai_reminders import auto_rows
from tests.test_internal import message
from tests.test_reminders import claim
from tests.test_reminders import reminders as reminders

MOSCOW = ZoneInfo("Europe/Moscow")
ASK = "Напомни завтра в 10 позвонить маме"


def structured(**changes):
    return StructuredNote.model_validate({"title": "Список", "markdown": "Позвонить маме.",
                                         "conclusions": [], "items": [], **changes})


class Silent(MockProvider):
    """The model returned no task at all."""


class Tasking(MockProvider):
    """The model understood the request and made the task with a deadline."""

    def structure(self, text, *, categories=()):
        return structured(items=[{"kind": "task", "text": "Позвонить маме", "source_quote": text,
                                  "due_text": "завтра в 10"}])


def run_worker(app, provider):
    assert Worker(app.state.sessions, app.state.settings, provider=provider).run_once()


def tomorrow_at(hour):
    day = datetime.now(MOSCOW).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=MOSCOW)


# Prompt

def test_prompt_asks_for_a_task_and_no_longer_forbids_reminders():
    lowered = SYSTEM_PROMPT.lower()
    assert "не создавай напоминания" not in lowered
    assert "напомни" in lowered and "remind me" in lowered and "будильник" in lowered
    assert "due_text" in SYSTEM_PROMPT and "Не придумывай даты" in SYSTEM_PROMPT
    assert len(SYSTEM_PROMPT) <= 3200  # about 1.1 times the previous prompt


# Grounding

def test_deadline_outside_the_quote_extends_the_quote_to_the_sentence():
    original = "Завтра в 10 напомни позвонить маме. Потом отдохнуть."
    result = repair_structure_quotes(structured(items=[
        {"kind": "task", "text": "Позвонить маме", "source_quote": "позвонить маме", "due_text": "Завтра в 10"},
    ]), original)
    item = result.items[0]
    assert item.source_quote == "Завтра в 10 напомни позвонить маме"
    assert item.due_text == "Завтра в 10" and item.due_text in item.source_quote
    assert not result._grounding_details_omitted
    assert validate_result(result, original).items[0].source_quote in original


def test_deadline_after_the_quote_extends_to_its_end_and_is_normalised():
    original = "Нужно позвонить маме завтра в 10, и ещё купить хлеб."
    result = repair_structure_quotes(structured(items=[
        {"kind": "task", "text": "Позвонить маме", "source_quote": "позвонить маме", "due_text": "ЗАВТРА В 10"},
    ]), original)
    item = result.items[0]
    assert item.source_quote == "Нужно позвонить маме завтра в 10"
    assert item.due_text == "завтра в 10"


def test_deadline_from_another_sentence_is_still_dropped():
    original = "Позвонить маме. Завтра в 10 будет поздно."
    result = repair_structure_quotes(structured(items=[
        {"kind": "task", "text": "Позвонить маме", "source_quote": "Позвонить маме.", "due_text": "Завтра в 10"},
    ]), original)
    assert result.items[0].due_text is None and result.items[0].source_quote == "Позвонить маме."
    assert result._grounding_details_omitted


def test_invented_deadline_is_still_dropped_when_the_quote_is_short():
    original = "Завтра в 10 напомни позвонить маме."
    result = repair_structure_quotes(structured(items=[
        {"kind": "task", "text": "Позвонить маме", "source_quote": "позвонить маме", "due_text": "в пятницу"},
    ]), original)
    assert result.items[0].due_text is None and result.items[0].source_quote == "позвонить маме"


def test_sentence_spans_split_on_marks_and_newlines():
    text = "Первое. Второе!  Третье?\nЧетвёртое без точки"
    assert [text[a:b] for a, b in sentence_spans(text)] == ["Первое.", "Второе!", "Третье?", "Четвёртое без точки"]


# Safety net through the worker

def test_nothing_from_the_model_still_gives_exactly_one_reminder(reminders):
    app, client, ids = reminders
    saved = message(client, update_id=300, text=ASK).json()
    run_worker(app, Silent())
    (row,) = auto_rows(app)
    assert row.status == "confirmed" and row.idempotency_key.startswith("ai:")
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW) == tomorrow_at(10)
    assert row.text == "Завтра в 10 позвонить маме"
    with app.state.sessions() as db:
        note = db.scalar(select(Note).where(Note.capture_id == saved["capture_id"]))
        item = db.get(Item, row.item_id)
        assert item.kind == "task" and item.status == "open" and item.source_quote == ASK
        assert row.note_id == note.id and item.note_id == note.id
    # Running the net again changes nothing.
    with app.state.sessions.begin() as db:
        assert request_reminders(db, db.get(Note, row.note_id), ASK, app.state.settings) == []
    assert len(auto_rows(app)) == 1


def test_telegram_reply_names_the_reminder_with_the_change_link(reminders):
    app, client, ids = reminders
    message(client, update_id=301, text=ASK)
    run_worker(app, Silent())
    (row,) = auto_rows(app)
    delivery = claim(client).json()["items"][0]
    assert delivery["text"].split("\n")[-1] == (
        "Напоминание поставлено: завтра в 10:00, «Завтра в 10 позвонить маме». "
        f"Изменить: {delivery['note_url']}&reminder={row.id}"
    )


def test_two_requests_give_two_reminders_and_a_short_list(reminders):
    app, client, ids = reminders
    message(client, update_id=302, text="Напомни завтра в 10 позвонить маме. Не забудь послезавтра в 12 оплатить счёт.")
    run_worker(app, Silent())
    rows = auto_rows(app)
    assert [r.text for r in rows] == ["Завтра в 10 позвонить маме", "Послезавтра в 12 оплатить счёт"]
    text = claim(client).json()["items"][0]["text"]
    tail = text.split("\n\n")[-1].split("\n")
    assert tail[0] == "Напоминания поставлены:"
    assert tail[1] == "• завтра в 10:00, «Завтра в 10 позвонить маме»"
    assert tail[2] == "• послезавтра в 12:00, «Послезавтра в 12 оплатить счёт»"
    assert tail[3].startswith("Изменить: ")


def test_no_duplicate_when_the_model_already_made_the_reminder(reminders):
    app, client, ids = reminders
    message(client, update_id=303, text=ASK)
    run_worker(app, Tasking())
    (row,) = auto_rows(app)
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW) == tomorrow_at(10)
    with app.state.sessions() as db:
        items = db.scalars(select(Item).where(Item.note_id == row.note_id, Item.kind == "task")).all()
        assert [i.id for i in items] == [row.item_id]  # the model's task was reused, none was added


def test_setting_off_keeps_explicit_requests_but_not_implicit_deadlines(reminders):
    app, client, ids = reminders
    with app.state.sessions.begin() as db:
        db.get(User, ids["user"]).ai_reminders_enabled = False
    message(client, update_id=304, text=ASK)
    run_worker(app, Tasking())
    assert len(auto_rows(app)) == 1
    # A deadline without a request to be reminded stays a proposal.
    message(client, update_id=305, text="Позвонить маме завтра в 11")

    class Implicit(MockProvider):
        def structure(self, text, *, categories=()):
            return structured(items=[{"kind": "task", "text": "Позвонить маме", "source_quote": text,
                                      "due_text": "завтра в 11"}])

    run_worker(app, Implicit())
    assert len(auto_rows(app)) == 1


def test_manual_capture_with_a_request_gets_a_reminder_and_a_telegram_message(reminders):
    app, client, ids = reminders
    assert message(client, update_id=306, text=ASK, mode="manual").status_code == 200
    (row,) = auto_rows(app)
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW) == tomorrow_at(10)
    text = claim(client).json()["items"][0]["text"]
    assert text.startswith("Напоминание поставлено: завтра в 10:00, «Завтра в 10 позвонить маме»")


def test_manual_capture_without_a_request_makes_nothing(reminders):
    app, client, ids = reminders
    assert message(client, update_id=307, text="Позвонить маме завтра в 10", mode="manual").status_code == 200
    assert auto_rows(app) == []
    assert claim(client).json() == {"items": []}


def test_request_with_an_unusable_time_keeps_the_note_and_says_so(reminders):
    app, client, ids = reminders
    assert message(client, update_id=308, text="Напомни вчера в 10 позвонить маме", mode="manual").status_code == 200
    assert auto_rows(app) == []
    assert claim(client).json()["items"][0]["text"] == NOT_UNDERSTOOD
    with app.state.sessions() as db:
        assert [n.markdown for n in db.scalars(select(Note))].count("Напомни вчера в 10 позвонить маме") == 1


def test_cancelled_reminder_is_not_made_again_by_the_net(reminders):
    app, client, ids = reminders
    message(client, update_id=309, text=ASK)
    run_worker(app, Silent())
    (row,) = auto_rows(app)
    assert client.post(f"/api/v1/reminders/{row.id}/cancel", json={"generation": 1}).status_code == 200
    with app.state.sessions.begin() as db:
        assert request_reminders(db, db.get(Note, row.note_id), ASK, app.state.settings) == []
    assert len(auto_rows(app)) == 1


# Default time

def default_run(app, ids, now, text="Напомни позвонить маме", settings=None):
    with app.state.sessions.begin() as db:
        created = request_reminders(db, db.get(Note, ids["note"]), text, settings or app.state.settings, now=now)
        return [(datetime.fromtimestamp(r.scheduled_at, MOSCOW), r.text) for r in created]


def test_no_time_in_the_daytime_means_in_an_hour(reminders):
    app, client, ids = reminders
    now = datetime(2026, 10, 10, 12, 20, tzinfo=UTC)  # 15:20 in Moscow
    assert default_run(app, ids, now) == [(datetime(2026, 10, 10, 16, 20, tzinfo=MOSCOW), "Позвонить маме")]


@pytest.mark.parametrize("hour_utc, expected", [
    (19, datetime(2026, 10, 11, 9, 0, tzinfo=MOSCOW)),  # 22:30 in Moscow, tomorrow morning
    (0, datetime(2026, 10, 10, 9, 0, tzinfo=MOSCOW)),   # 03:30 in Moscow, this morning
])
def test_no_time_at_night_means_nine_in_the_morning(reminders, hour_utc, expected):
    app, client, ids = reminders
    now = datetime(2026, 10, 10, hour_utc, 30, tzinfo=UTC)
    assert [when for when, _ in default_run(app, ids, now)] == [expected]


def test_default_delay_zero_turns_the_default_off(reminders):
    app, client, ids = reminders
    off = replace(app.state.settings, reminder_default_delay_seconds=0)
    assert default_run(app, ids, datetime(2026, 10, 10, 12, 0, tzinfo=UTC), settings=off) == []


def test_default_delay_comes_from_the_setting(reminders):
    app, client, ids = reminders
    quick = replace(app.state.settings, reminder_default_delay_seconds=1800)
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert default_run(app, ids, now, settings=quick) == [(datetime(2026, 10, 10, 15, 30, tzinfo=MOSCOW), "Позвонить маме")]


def test_a_named_time_beats_the_default(reminders):
    app, client, ids = reminders
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert default_run(app, ids, now, "Напомните через 2 часа выключить духовку") == [
        (datetime(2026, 10, 10, 17, 0, tzinfo=MOSCOW), "Через 2 часа выключить духовку"),
    ]


def test_a_sentence_without_a_trigger_is_ignored(reminders):
    app, client, ids = reminders
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert default_run(app, ids, now, "В приложении нет напоминаний. Он напомнил вчера.") == []


# Parser phrases. Saturday 2026-10-10, 15:00 in Moscow.

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
WEDNESDAY = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)  # 12:00 in Moscow
ZONE = "Europe/Moscow"

PHRASES = [
    ("через 15 минут", NOW, "2026-10-10T15:15:00"),
    ("через полчаса", NOW, "2026-10-10T15:30:00"),
    ("через 2 часа", NOW, "2026-10-10T17:00:00"),
    ("в 6 вечера", NOW, "2026-10-10T18:00:00"),
    ("в 7 утра", NOW, "2026-10-11T07:00:00"),
    ("к обеду", NOW, "2026-10-11T13:00:00"),
    ("к обеду", WEDNESDAY.replace(hour=5), "2026-10-14T13:00:00"),
    ("после обеда", NOW, "2026-10-11T15:00:00"),
    ("завтра после обеда", NOW, "2026-10-11T15:00:00"),
    ("на выходных", NOW, "2026-10-11T10:00:00"),
    ("на выходных", WEDNESDAY, "2026-10-17T10:00:00"),
    ("в конце недели", NOW, "2026-10-16T18:00:00"),
    ("в конце недели", WEDNESDAY, "2026-10-16T18:00:00"),
    ("вечером в пятницу", NOW, "2026-10-16T19:00:00"),
    ("завтра в полдень", NOW, "2026-10-11T12:00:00"),
    ("послезавтра утром", NOW, "2026-10-12T09:00:00"),
    ("15 октября в 14:30", NOW, "2026-10-15T14:30:00"),
    ("в 16.30", NOW, "2026-10-10T16:30:00"),
    ("завтра в 14.30", NOW, "2026-10-11T14:30:00"),
    ("в 9 часов 30 минут", NOW, "2026-10-11T09:30:00"),
    ("завтра в 9 часов 30 минут", NOW, "2026-10-11T09:30:00"),
]


@pytest.mark.parametrize("text, now, local_time", PHRASES)
def test_parser_reads_the_phrase(text, now, local_time):
    assert propose_reminder(text, now, ZONE)["local_time"] == local_time


def test_parser_keeps_plain_dates_and_rejects_yesterday():
    assert propose_reminder("14.10", NOW, ZONE)["local_time"] == "2026-10-14T09:00:00"
    assert propose_reminder("до 14.10", NOW, ZONE)["local_time"] == "2026-10-14T09:00:00"
    assert propose_reminder("вчера в 10", NOW, ZONE) is None


def test_mentions_time_tells_no_time_from_an_unusable_one():
    assert mentions_time("Напомни завтра в 10") and mentions_time("вчера в 10")
    assert mentions_time("Напомни к обеду") and mentions_time("напомни на выходных")
    assert not mentions_time("Напомни позвонить маме") and not mentions_time("")
