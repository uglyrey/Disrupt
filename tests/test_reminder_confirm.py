"""Reminder times that are right: the model's `due_at` against the parser, «Верно?», the written correction.

Mock provider, fake provider answers and httpx.MockTransport only. No live model, no Telegram.
The real parser is used unless a case needs a pinned answer (`patch_parser`, for example an unsure parser with a chosen
alternative). Cases marked `real parser` run `parse_due_text_detailed` as shipped.
"""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import reminder_proposals
from app.models import Inbox, Item, Note, Outbox, ProviderUsage, Reminder, User
from app.providers import (
    SYSTEM_PROMPT,
    TIME_PROMPT,
    CloudRuProvider,
    MockProvider,
    ProviderError,
    StructuredNote,
    clean_due_times,
    repair_structure_quotes,
    structure_context,
)
from app.reminders import AUTO_KEY_PREFIX, auto_reminders, request_reminders
from app.time_ai import TimeResolver
from app.time_check import ASK, CONFIRMED, decide, read_model_time
from tests.test_internal import BASE, HEADERS, SERVICE_TOKEN, linked, message
from tests.test_reminders import claim

MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 15, 5, 47)  # The owner's morning: naive local wall clock.
SENTENCE = "Напомни сегодня в шестнадцать часов позвонить маме"
QUOTE = "сегодня в шестнадцать часов"


def at(hour, minute=0, day=15):
    return datetime(2026, 10, day, hour, minute)


def pinned_parser(table):
    """A `parse_due_text_detailed` that answers from {phrase: dict or datetime}. Other phrases give nothing."""

    def parse(text, now):
        value = table.get(text)
        if isinstance(value, datetime):
            value = {"result": value, "certain": True, "time_spoken": True, "alternatives": []}
        base = {"result": None, "certain": False, "time_spoken": False, "alternatives": []}
        return base if value is None else {**base, **value}

    return parse


def patch_parser(monkeypatch, table):
    monkeypatch.setattr(reminder_proposals, "parse_due_text_detailed", pinned_parser(table))


# The matrix: model due_at against the parser

def test_both_agree_and_the_parser_is_sure_is_confirmed(monkeypatch):
    patch_parser(monkeypatch, {QUOTE: at(16)})
    reading = decide(QUOTE, "2026-10-15T16:00", NOW)
    assert (reading.when, reading.check, reading.alt) == (at(16), CONFIRMED, None)


def test_the_parser_disagrees_so_the_model_time_stands_and_the_person_is_asked(monkeypatch):
    patch_parser(monkeypatch, {QUOTE: at(9)})  # The old bug: the time was silently dropped.
    reading = decide(QUOTE, "2026-10-15T16:00", NOW)
    assert (reading.when, reading.check, reading.alt) == (at(16), ASK, at(9))


def test_an_unsure_parser_that_picks_the_same_minute_as_the_model_is_confirmed(monkeypatch):
    patch_parser(monkeypatch, {"в четыре": {"result": at(16), "certain": False, "alternatives": [at(4, day=16)]}})
    reading = decide("в четыре", "2026-10-15T16:00", NOW)
    assert (reading.when, reading.check, reading.alt) == (at(16), CONFIRMED, None)


def test_an_unsure_parser_that_picks_another_minute_than_the_model_is_asked_with_both_readings(monkeypatch):
    patch_parser(monkeypatch, {"в четыре": {"result": at(16), "certain": False, "alternatives": [at(4, day=16)]}})
    reading = decide("в четыре", "2026-10-16T04:00", NOW)
    assert (reading.when, reading.check, reading.alt) == (at(4, day=16), ASK, at(16))


def test_real_parser_tomorrow_at_ten_with_the_same_model_time_is_not_asked():
    reading = decide("завтра в 10", "2026-10-16T10:00", NOW)
    assert (reading.when, reading.check, reading.alt) == (at(10, day=16), CONFIRMED, None)


def test_real_parser_tomorrow_at_ten_against_a_model_evening_time_is_asked_with_the_parser_reading():
    reading = decide("завтра в 10", "2026-10-16T22:00", NOW)
    assert (reading.when, reading.check, reading.alt) == (at(22, day=16), ASK, at(10, day=16))


def test_real_parser_the_owner_case_with_and_without_a_model_time():
    assert decide(QUOTE, "2026-10-15T16:00", NOW) == decide(QUOTE, None, NOW).__class__(at(16), CONFIRMED, None)
    reading = decide(QUOTE, None, NOW)
    assert (reading.when, reading.check, reading.alt) == (at(16), None, None)


def test_the_parser_finds_nothing_so_the_model_time_is_set_and_asked(monkeypatch):
    patch_parser(monkeypatch, {})
    reading = decide("после обеда часа в три", "2026-10-15T15:00", NOW)
    assert (reading.when, reading.check, reading.alt) == (at(15), ASK, None)


def test_no_model_time_and_a_sure_parser_needs_no_question(monkeypatch):
    patch_parser(monkeypatch, {"завтра в 10": at(10, day=16)})
    reading = decide("завтра в 10", None, NOW)
    assert (reading.when, reading.check, reading.alt) == (at(10, day=16), None, None)


def test_no_model_time_and_an_unsure_parser_sets_the_guess_and_offers_the_other_reading(monkeypatch):
    patch_parser(monkeypatch, {"в четыре": {"result": at(16), "certain": False, "alternatives": [at(4, day=16)]}})
    reading = decide("в четыре", None, NOW)
    assert (reading.when, reading.check, reading.alt) == (at(16), ASK, at(4, day=16))


def test_nothing_usable_gives_nothing(monkeypatch):
    patch_parser(monkeypatch, {})
    assert decide("когда-нибудь", None, NOW).when is None
    assert decide(None, None, NOW).when is None


@pytest.mark.parametrize("value", [
    None, "", "завтра", "2026-10-15", "2026-10-15T16:00:00", "2026-13-15T16:00", "2026-10-15T25:00",
    "2026-10-15T05:30",  # Past.
    "2029-10-15T16:00",  # Further than the parser looks ahead.
    16, ["2026-10-15T16:00"],
])
def test_a_bad_model_time_is_ignored(value):
    assert read_model_time(value, NOW) is None


def test_a_model_time_a_minute_in_the_past_passes_only_with_the_clock_skew():
    assert read_model_time("2026-10-15T05:46", NOW) is None
    from app.time_check import SKEW

    assert read_model_time("2026-10-15T05:46", NOW, skew=SKEW) == at(5, 46)
    assert read_model_time("2026-10-15T05:44", NOW, skew=SKEW) is None


# Schema, prompt and providers

def test_due_at_is_optional_and_a_malformed_value_never_breaks_the_note():
    base = {"title": "T", "markdown": "m", "conclusions": []}
    item = {"kind": "task", "text": "Позвонить", "source_quote": "позвонить", "due_text": "завтра"}
    for bad in ("завтра в 10", "2026-10-15 16:00", "2026-02-31T10:00", 16, ""):
        note = StructuredNote.model_validate({**base, "items": [{**item, "due_at": bad}]})
        assert note.items[0].due_at is None
    assert StructuredNote.model_validate({**base, "items": [item]}).items[0].due_at is None
    assert StructuredNote.model_validate({**base, "items": [{**item, "due_at": "2026-10-16T10:00"}]}).items[0].due_at \
        == "2026-10-16T10:00"


def test_due_at_goes_away_with_a_deadline_that_did_not_survive_grounding():
    original = "Завтра в 10 напомни позвонить маме."
    note = StructuredNote.model_validate({"title": "T", "markdown": "m", "conclusions": [], "items": [
        {"kind": "task", "text": "Позвонить маме", "source_quote": "позвонить маме", "due_text": "в пятницу",
         "due_at": "2026-10-16T10:00"},
        {"kind": "task", "text": "Позвонить маме", "source_quote": "Завтра в 10 напомни позвонить маме",
         "due_text": "Завтра в 10", "due_at": "2026-10-16T10:00"},
    ]})
    items = repair_structure_quotes(note, original).items
    assert (items[0].due_text, items[0].due_at) == (None, None)
    assert (items[1].due_text, items[1].due_at) == ("Завтра в 10", "2026-10-16T10:00")


def test_a_past_or_far_model_time_is_dropped_silently_but_the_quote_stays():
    note = StructuredNote.model_validate({"title": "T", "markdown": "m", "conclusions": [], "items": [
        {"kind": "task", "text": "A", "source_quote": "a", "due_text": "a", "due_at": due}
        for due in ("2026-10-15T05:46", "2026-10-15T05:44", "2030-01-01T10:00", "2026-10-15T16:00")
    ]})
    cleaned = clean_due_times(note, NOW.replace(second=0))
    assert [item.due_at for item in cleaned.items] == ["2026-10-15T05:46", None, None, "2026-10-15T16:00"]
    assert all(item.due_text == "a" for item in cleaned.items)


def test_the_model_sees_the_local_clock_and_the_prompt_stays_short():
    context = json.loads(structure_context(["Работа"], "Europe/Moscow", datetime(2026, 10, 15, 5, 47, tzinfo=MOSCOW)))
    assert (context["today"], context["now"], context["weekday"], context["timezone"]) == (
        "2026-10-15", "05:47", "четверг", "Europe/Moscow")
    assert len(SYSTEM_PROMPT) <= 3436 * 1.1
    assert "due_at" in SYSTEM_PROMPT and "09:00" in SYSTEM_PROMPT and "null" in SYSTEM_PROMPT
    assert "now" in SYSTEM_PROMPT.splitlines()[1] and '"due_at":null' in SYSTEM_PROMPT
    assert len(TIME_PROMPT) < 1000


def provider_answer(items, *, label="cloudru", model="claude-opus-4.7", now=None):
    body = {"title": "Звонок", "markdown": "Позвонить маме.", "conclusions": [], "items": items, "category_name": None}
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(body)}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 50}})

    base = "https://foundation-models.api.cloud.ru/v1"
    provider = CloudRuProvider("synthetic-key-for-tests", model, base_url=base, transport=httpx.MockTransport(handle),
                               label=label, allowed_base_urls={base})
    return provider, seen


@pytest.mark.parametrize("model, label", [("claude-opus-4.7", "cloudru"), ("gpt-6.1-sol", "openai")])
def test_both_gateways_return_and_keep_due_at(model, label):
    """The main model (strict schema) and the reserve (plain JSON) share one schema."""
    text = "Напомни сегодня в шестнадцать часов позвонить маме"
    item = {"kind": "task", "text": "Позвонить маме", "source_quote": text, "due_text": QUOTE,
            "due_at": "2026-10-15T16:00"}
    provider, seen = provider_answer([item], label=label, model=model)
    now = datetime(2026, 10, 15, 5, 47, tzinfo=MOSCOW)
    result = provider.structure(text, timezone="Europe/Moscow", now=now)
    assert result.items[0].due_at == "2026-10-15T16:00" and result.items[0].due_text == QUOTE
    request = seen[0]
    assert json.loads(request["messages"][1]["content"])["now"] == "05:47"
    if model == "claude-opus-4.7":
        schema = request["response_format"]["json_schema"]["schema"]
        assert "due_at" in json.dumps(schema)
    else:
        assert request["response_format"] == {"type": "json_object"}


def test_a_gateway_answer_without_due_at_or_with_a_bad_one_still_works():
    text = "Напомни сегодня в шестнадцать часов позвонить маме"
    base = {"kind": "task", "text": "Позвонить маме", "source_quote": text, "due_text": QUOTE}
    now = datetime(2026, 10, 15, 5, 47, tzinfo=MOSCOW)
    for extra in ({}, {"due_at": "в четыре"}, {"due_at": "2026-10-14T16:00"}, {"due_at": None}):
        provider, _ = provider_answer([{**base, **extra}])
        assert provider.structure(text, timezone="Europe/Moscow", now=now).items[0].due_at is None


# From a note to a reminder

@pytest.fixture
def notes(app_factory):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    with TestClient(app) as client:
        owner = linked(client)
        assert message(client, mode="manual").status_code == 200
        with app.state.sessions() as db:
            note = db.scalar(select(Note).where(Note.user_id == owner["id"]))
            yield app, client, {"user": owner["id"], "note": note.id}


def make_task(app, ids, sentence=SENTENCE, due_text=QUOTE):
    with app.state.sessions.begin() as db:
        item = Item(user_id=ids["user"], note_id=ids["note"], kind="task", text="Позвонить маме",
                    source_quote=sentence, due_text=due_text)
        db.add(item)
        db.flush()
        return item.id


def run_request(app, ids, item_id, due_at, now, sentence=SENTENCE, settings=None):
    with app.state.sessions.begin() as db:
        created = request_reminders(
            db, db.get(Note, ids["note"]), sentence, settings or app.state.settings, now=now,
            model_times={item_id: due_at} if due_at else None,
        )
        return [(datetime.fromtimestamp(r.scheduled_at, MOSCOW).replace(tzinfo=None), r.time_check, r.time_alt)
                for r in created]


MORNING = datetime(2026, 10, 15, 2, 47, tzinfo=UTC)  # 05:47 in Moscow


def test_the_owners_case_is_set_to_sixteen_and_confirmed_without_a_question(notes, monkeypatch):
    app, client, ids = notes
    patch_parser(monkeypatch, {QUOTE: at(16)})
    item_id = make_task(app, ids)
    assert run_request(app, ids, item_id, "2026-10-15T16:00", MORNING) == [(at(16), CONFIRMED, None)]
    assert claim(client).json() == {"items": []}  # Nothing is due and no text is sent for a confirmed time.


def test_real_parser_the_owners_case_with_the_model_time_is_confirmed_without_a_question(notes):
    app, client, ids = notes
    item_id = make_task(app, ids)
    assert run_request(app, ids, item_id, "2026-10-15T16:00", MORNING) == [(at(16), CONFIRMED, None)]
    assert claim(client).json() == {"items": []}


def test_real_parser_the_owners_case_without_a_model_time_is_sixteen_and_no_question(notes):
    app, client, ids = notes
    item_id = make_task(app, ids)
    assert run_request(app, ids, item_id, None, MORNING) == [(at(16), None, None)]
    assert claim(client).json() == {"items": []}


def test_real_parser_tomorrow_at_ten_with_the_model_ten_is_not_asked(notes):
    app, client, ids = notes
    sentence = "Напомни завтра в 10 позвонить маме"
    item_id = make_task(app, ids, sentence, "завтра в 10")
    assert run_request(app, ids, item_id, "2026-10-16T10:00", MORNING, sentence) == [(at(10, day=16), CONFIRMED, None)]


def test_real_parser_tomorrow_at_ten_against_the_model_twenty_two_asks_with_the_ten_button(notes):
    app, client, ids = notes
    sentence = "Напомни завтра в 10 позвонить маме"
    item_id = make_task(app, ids, sentence, "завтра в 10")
    ((when, check, alt),) = run_request(app, ids, item_id, "2026-10-16T22:00", MORNING, sentence)
    assert (when, check) == (at(22, day=16), ASK)
    assert datetime.fromtimestamp(alt, MOSCOW).replace(tzinfo=None) == at(10, day=16)


def test_when_the_parser_drops_the_hour_the_model_time_is_set_and_asked(notes, monkeypatch):
    app, client, ids = notes
    patch_parser(monkeypatch, {QUOTE: at(9)})
    item_id = make_task(app, ids)
    ((when, check, alt),) = run_request(app, ids, item_id, "2026-10-15T16:00", MORNING)
    assert (when, check) == (at(16), ASK)
    assert datetime.fromtimestamp(alt, MOSCOW).replace(tzinfo=None) == at(9)  # The parser's reading is the button.


def test_no_model_time_and_an_unsure_parser_is_set_at_the_guess_with_the_other_reading(notes, monkeypatch):
    app, client, ids = notes
    sentence = "Напомни в четыре позвонить маме"
    patch_parser(monkeypatch, {"в четыре": {"result": at(16), "certain": False, "alternatives": [at(4, day=16)]}})
    item_id = make_task(app, ids, sentence, "в четыре")
    ((when, check, alt),) = run_request(app, ids, item_id, None, MORNING, sentence)
    assert (when, check) == (at(16), ASK)
    assert datetime.fromtimestamp(alt, MOSCOW).replace(tzinfo=None) == at(4, day=16)


def test_nothing_usable_but_an_explicit_request_gets_the_default_time_with_a_question(notes, monkeypatch):
    app, client, ids = notes
    sentence = "Напомни когда-нибудь в четверг после дождичка позвонить маме"
    patch_parser(monkeypatch, {})
    item_id = make_task(app, ids, sentence, "после дождичка")
    ((when, check, alt),) = run_request(app, ids, item_id, None, MORNING, sentence)
    assert check == "unclear" and alt is None
    assert when == at(9)  # 05:47 is before 08:00, so the morning default.


def test_the_cross_check_works_for_the_ai_set_reminders_too(notes, monkeypatch):
    app, client, ids = notes
    patch_parser(monkeypatch, {QUOTE: at(9)})
    item_id = make_task(app, ids)
    with app.state.sessions.begin() as db:
        (row,) = auto_reminders(db, db.get(Note, ids["note"]), app.state.settings, now=MORNING,
                                model_times={item_id: "2026-10-15T16:00"})
        assert row.idempotency_key == AUTO_KEY_PREFIX + item_id
        assert (datetime.fromtimestamp(row.scheduled_at, MOSCOW).replace(tzinfo=None), row.time_check) == (at(16), ASK)


def test_a_past_model_time_is_not_trusted_at_creation(notes, monkeypatch):
    app, client, ids = notes
    patch_parser(monkeypatch, {QUOTE: at(16)})
    item_id = make_task(app, ids)
    assert run_request(app, ids, item_id, "2026-10-15T04:00", MORNING) == [(at(16), None, None)]


# The same through the worker, with a provider that returns due_at

class Dating(MockProvider):
    """Returns the task of the request with the deadline and the time it worked out."""

    def __init__(self, due_text, due_at):
        self.due_text, self.due_at = due_text, due_at

    def structure(self, text, *, categories=(), timezone=None):
        return StructuredNote.model_validate({"title": "Звонок", "markdown": "Позвонить маме.", "conclusions": [], "items": [
            {"kind": "task", "text": "Позвонить маме", "source_quote": text, "due_text": self.due_text,
             "due_at": self.due_at},
        ]})


def tomorrow(hour):
    day = datetime.now(MOSCOW).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, hour, 0)


def run_worker(app, provider):
    from app.worker import Worker

    assert Worker(app.state.sessions, app.state.settings, provider=provider).run_once()


def auto_rows(app):
    with app.state.sessions() as db:
        return db.scalars(select(Reminder).where(Reminder.idempotency_key.like(AUTO_KEY_PREFIX + "%"))).all()


def test_worker_agreement_sets_a_confirmed_reminder_and_sends_no_question(notes):
    app, client, ids = notes
    text = "Напомни завтра в 10 позвонить маме"
    assert message(client, update_id=40, text=text).status_code == 200
    run_worker(app, Dating("завтра в 10", tomorrow(10).strftime("%Y-%m-%dT%H:%M")))
    (row,) = auto_rows(app)
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW).replace(tzinfo=None) == tomorrow(10)
    assert row.time_check == CONFIRMED
    (item,) = claim(client).json()["items"]
    assert item["confirm"] == [] and "Верно?" not in item["text"]
    assert "Напоминание поставлено: завтра в 10:00" in item["text"]


def test_worker_disagreement_sets_the_model_time_and_asks(notes):
    app, client, ids = notes
    text = "Напомни завтра в 10 позвонить маме"
    assert message(client, update_id=41, text=text).status_code == 200
    run_worker(app, Dating("завтра в 10", tomorrow(11).strftime("%Y-%m-%dT%H:%M")))
    (row,) = auto_rows(app)
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW).replace(tzinfo=None) == tomorrow(11)
    assert row.time_check == ASK and row.time_alt is not None
    assert datetime.fromtimestamp(row.time_alt, MOSCOW).replace(tzinfo=None) == tomorrow(10)
    (delivery,) = claim(client).json()["items"]
    assert delivery["text"].endswith("Напоминание поставлено: завтра в 11:00 по Москве, «Позвонить маме». Верно?")
    assert delivery["confirm"] == [{"reminder_id": row.id, "generation": 1, "alt_label": "В 10:00"}]


def test_a_voice_style_note_with_no_time_from_the_model_keeps_the_parser_time_and_asks(notes):
    app, client, ids = notes
    assert message(client, update_id=42, text="Напомни завтра в 10 позвонить маме").status_code == 200
    run_worker(app, Dating("завтра в 10", None))
    (row,) = auto_rows(app)
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW).replace(tzinfo=None) == tomorrow(10)
    assert row.time_check == "ask"  # No daypart: the real parser is unsure and the model gave nothing.


def test_a_sure_phrase_with_no_time_from_the_model_needs_no_question(notes):
    app, client, ids = notes
    assert message(client, update_id=43, text="Напомни завтра в 10 вечера позвонить маме").status_code == 200
    run_worker(app, Dating("завтра в 10 вечера", None))
    (row,) = auto_rows(app)
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW).replace(tzinfo=None) == tomorrow(22)
    assert row.time_check is None


# The line in the chat

def add_reminders(app, ids, count, check="ask", base_hours=3):
    rows = []
    with app.state.sessions.begin() as db:
        for number in range(count):
            item = Item(user_id=ids["user"], note_id=ids["note"], kind="task", text=f"Дело {number + 1}")
            db.add(item)
            db.flush()
            row = Reminder(
                user_id=ids["user"], note_id=ids["note"], item_id=item.id, timezone="Europe/Moscow",
                scheduled_at=time.time() + 3600 * (base_hours + number), text=f"Дело {number + 1}",
                idempotency_key=AUTO_KEY_PREFIX + item.id, creation_hash="x", time_check=check,
                time_alt=time.time() + 3600 * (base_hours + number) + 600 if check == "ask" else None,
            )
            db.add(row)
            db.flush()
            rows.append(row.id)
    return rows


def reply_for(app, client, ids, update_id=60, text="Позвонить маме завтра"):
    """A processed note whose reply is claimed after the reminders were made."""
    assert message(client, update_id=update_id, text=text).status_code == 200
    run_worker(app, MockProvider())
    with app.state.sessions() as db:
        return db.scalar(select(Note).order_by(Note.created_at.desc()))


def test_one_guessed_reminder_asks_verno_with_one_pair_and_the_other_reading(notes):
    app, client, ids = notes
    assert message(client, update_id=61, text="Заметка про звонок").status_code == 200
    run_worker(app, MockProvider())
    with app.state.sessions() as db:
        note = db.scalar(select(Note).where(Note.capture_id != db.get(Note, ids["note"]).capture_id))
        note_id, user = note.id, note.user_id
    ids2 = {"user": user, "note": note_id}
    (reminder_id,) = add_reminders(app, ids2, 1)
    (item,) = claim(client).json()["items"]
    assert "Напоминание поставлено: " in item["text"] and item["text"].endswith("«Дело 1». Верно?")
    assert [(c["reminder_id"], c["generation"]) for c in item["confirm"]] == [(reminder_id, 1)]
    assert item["confirm"][0]["alt_label"].startswith("В ")


def test_up_to_three_reminders_get_their_own_pair_and_the_rest_say_check_the_site(notes):
    app, client, ids = notes
    assert message(client, update_id=62, text="Заметка про дела").status_code == 200
    run_worker(app, MockProvider())
    with app.state.sessions() as db:
        note = db.scalar(select(Note).where(Note.capture_id != db.get(Note, ids["note"]).capture_id))
        ids2 = {"user": note.user_id, "note": note.id}
    made = add_reminders(app, ids2, 4)
    (item,) = claim(client).json()["items"]
    assert [c["reminder_id"] for c in item["confirm"]] == made[:3]
    text = item["text"]
    assert "Напоминания поставлены:" in text
    assert text.count("Верно?") == 3 and text.count("Проверьте время на сайте.") == 1
    assert "1. " in text and "3. " in text and "«Дело 4»" in text


def test_a_settled_time_gets_the_ordinary_line_and_no_buttons(notes):
    app, client, ids = notes
    assert message(client, update_id=63, text="Заметка про дела").status_code == 200
    run_worker(app, MockProvider())
    with app.state.sessions() as db:
        note = db.scalar(select(Note).where(Note.capture_id != db.get(Note, ids["note"]).capture_id))
        ids2 = {"user": note.user_id, "note": note.id}
    add_reminders(app, ids2, 1, check="confirmed")
    (item,) = claim(client).json()["items"]
    assert item["confirm"] == [] and "Верно?" not in item["text"] and "Изменить: " in item["text"]


# The buttons in the core

@pytest.fixture
def asking(app_factory):
    ai = FakeAI()
    app = app_factory(internal_api_token=SERVICE_TOKEN, llm_provider=ai)
    with TestClient(app) as client:
        owner = linked(client)
        assert message(client, mode="manual").status_code == 200
        with app.state.sessions() as db:
            note = db.scalar(select(Note).where(Note.user_id == owner["id"]))
            note_id = note.id
        ids = {"user": owner["id"], "note": note_id}
        (reminder_id,) = add_reminders(app, ids, 1)
        yield app, client, {**ids, "reminder": reminder_id}, ai


class FakeAI(MockProvider):
    """What the model answers to the time question. Counts the calls."""

    def __init__(self):
        self.calls, self.value, self.error = [], None, None

    def resolve_time(self, data, *, on_usage=None):
        self.calls.append(data)
        if self.error:
            raise self.error
        return self.value(data) if callable(self.value) else self.value


def post(client, path, update_id, ids, **body):
    return client.post("/internal/v1/telegram/" + path, headers=HEADERS, json={
        **BASE, "update_id": update_id, "reminder_id": ids["reminder"], "generation": 1, **body})


def confirm(client, ids, action, update_id=100, **body):
    return post(client, "reminder-confirm", update_id, ids, action=action, **body)


def answer(client, ids, text, update_id=200, **body):
    return post(client, "reminder-time", update_id, ids, text=text, **body)


def state(app, ids):
    with app.state.sessions() as db:
        row = db.get(Reminder, ids["reminder"])
        return row.time_check, row.generation, row.scheduled_at, row.status


def test_verno_confirms_and_a_repeat_changes_nothing(asking):
    app, client, ids, _ = asking
    before = state(app, ids)
    first = confirm(client, ids, "ok")
    assert first.status_code == 200 and first.json()["status"] == "confirmed"
    assert state(app, ids) == ("confirmed", 1, before[2], "confirmed")
    again = confirm(client, ids, "ok", update_id=101)
    assert again.status_code == 200 and again.json()["status"] == "confirmed"
    assert confirm(client, ids, "ok").json() == first.json()  # The same update is replayed from the Inbox.
    assert state(app, ids) == ("confirmed", 1, before[2], "confirmed")


def test_edit_only_asks_for_the_time_and_changes_nothing(asking):
    app, client, ids, _ = asking
    before = state(app, ids)
    reply = confirm(client, ids, "edit")
    assert reply.status_code == 200 and reply.json()["status"] == "ask_time"
    assert state(app, ids) == before


def test_the_other_reading_button_moves_the_reminder_once(asking):
    app, client, ids, _ = asking
    with app.state.sessions() as db:
        alt = db.get(Reminder, ids["reminder"]).time_alt
    reply = confirm(client, ids, "alt")
    assert reply.status_code == 200 and reply.json()["status"] == "corrected"
    check, generation, scheduled, _ = state(app, ids)
    assert (check, generation, scheduled) == ("corrected", 2, alt)
    again = confirm(client, ids, "alt", update_id=101)  # Pressed twice before the buttons went away.
    assert again.status_code == 200 and again.json()["status"] == "corrected"
    assert state(app, ids)[1] == 2


def test_stale_buttons_are_refused(asking):
    app, client, ids, _ = asking
    assert confirm(client, ids, "ok", generation=7).status_code == 409
    refused = confirm(client, ids, "edit", update_id=101, generation=7)
    assert refused.json()["error"]["code"] == "action_expired"
    assert state(app, ids)[0] == "ask"


def test_a_reminder_that_is_cancelled_sent_or_in_the_past_cannot_be_confirmed(asking):
    app, client, ids, _ = asking
    with app.state.sessions.begin() as db:
        db.get(Reminder, ids["reminder"]).scheduled_at = time.time() - 5
    assert confirm(client, ids, "ok").json()["error"]["code"] == "action_expired"
    with app.state.sessions.begin() as db:
        row = db.get(Reminder, ids["reminder"])
        row.scheduled_at, row.status = time.time() + 600, "cancelled"
    assert confirm(client, ids, "ok", update_id=101).json()["error"]["code"] == "action_expired"
    assert answer(client, ids, "завтра в 10", update_id=102).json()["error"]["code"] == "action_expired"
    with app.state.sessions.begin() as db:
        row = db.get(Reminder, ids["reminder"])
        row.status = "completed"
    assert confirm(client, ids, "edit", update_id=103).json()["error"]["code"] == "action_expired"


def test_the_other_reading_that_has_passed_is_refused(asking):
    app, client, ids, _ = asking
    with app.state.sessions.begin() as db:
        db.get(Reminder, ids["reminder"]).time_alt = time.time() - 5
    reply = confirm(client, ids, "alt")
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "time_passed"


def test_someone_elses_buttons_do_nothing(asking):
    from tests.conftest import register

    app, client, ids, ai = asking
    before = state(app, ids)
    with TestClient(app) as other:
        user = register(other, "another")
        with app.state.sessions() as db:
            from app.models import TelegramIdentity

            db.add(TelegramIdentity(user_id=user["id"], bot_id=BASE["bot_id"], telegram_user_id=17, chat_id=17))
            db.commit()
        for number, action in enumerate(("ok", "edit", "alt")):
            reply = other.post("/internal/v1/telegram/reminder-confirm", headers=HEADERS, json={
                **BASE, "telegram_user_id": 17, "chat_id": 17, "update_id": 300 + number,
                "reminder_id": ids["reminder"], "generation": 1, "action": action})
            assert reply.status_code == 409 and reply.json()["error"]["code"] == "action_expired"
        reply = other.post("/internal/v1/telegram/reminder-time", headers=HEADERS, json={
            **BASE, "telegram_user_id": 17, "chat_id": 17, "update_id": 310, "reminder_id": ids["reminder"],
            "generation": 1, "text": "завтра в 10"})
        assert reply.status_code == 409
    assert state(app, ids) == before and ai.calls == []
    unlinked = client.post("/internal/v1/telegram/reminder-confirm", headers=HEADERS, json={
        **BASE, "telegram_user_id": 99, "chat_id": 99, "update_id": 320, "reminder_id": ids["reminder"],
        "generation": 1, "action": "ok"})
    assert unlinked.status_code == 403 and unlinked.json()["error"]["code"] == "telegram_not_linked"


def test_garbage_ids_are_a_clean_refusal(asking):
    app, client, ids, _ = asking
    assert post(client, "reminder-confirm", 400, {"reminder": "../x"}, action="ok").status_code == 422
    unknown = post(client, "reminder-confirm", 401, {"reminder": "0" * 8 + "-0000-0000-0000-" + "0" * 12}, action="ok")
    assert unknown.status_code == 409


# The written answer

def local(stamp):
    return datetime.fromtimestamp(stamp, MOSCOW).replace(tzinfo=None)


def test_a_clear_answer_is_read_locally_for_free(asking, monkeypatch):
    app, client, ids, ai = asking
    target = (datetime.now(MOSCOW) + timedelta(days=1)).replace(hour=16, minute=30, second=0, microsecond=0, tzinfo=None)
    patch_parser(monkeypatch, {"в 16:30": {"result": target, "certain": True}})
    reply = answer(client, ids, "в 16:30")
    assert reply.status_code == 200 and reply.json() == {"status": "set", "label": "завтра в 16:30"}
    check, generation, scheduled, _ = state(app, ids)
    assert (check, generation, local(scheduled)) == ("corrected", 2, target)
    assert ai.calls == []
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProviderUsage).where(ProviderUsage.kind == "llm")) == 0
        assert db.scalar(select(func.count()).select_from(Note)) == 1  # The answer is never saved as a note.


def test_an_unsure_parser_sets_its_guess_and_asks_again_with_the_other_reading(asking, monkeypatch):
    app, client, ids, ai = asking
    first = (datetime.now(MOSCOW) + timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0, tzinfo=None)
    second = first.replace(hour=4)
    patch_parser(monkeypatch, {"в четыре": {"result": first, "certain": False, "alternatives": [second]}})
    reply = answer(client, ids, "в четыре")
    body = reply.json()
    assert reply.status_code == 200 and body["status"] == "ask" and body["label"] == "завтра в 16:00"
    assert (body["reminder_id"], body["generation"], body["alt_label"]) == (ids["reminder"], 2, "В 04:00")
    check, generation, scheduled, _ = state(app, ids)
    assert (check, generation, local(scheduled)) == ("ask", 2, first)
    assert ai.calls == []
    # The new buttons work: the other reading.
    switched = post(client, "reminder-confirm", 201, ids, action="alt", generation=2)
    assert switched.json()["status"] == "corrected" and local(state(app, ids)[2]) == second


def test_when_the_parser_fails_the_ai_reads_the_answer(asking, monkeypatch):
    app, client, ids, ai = asking
    patch_parser(monkeypatch, {})
    target = (datetime.now(MOSCOW) + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0, tzinfo=None)
    ai.value = target.strftime("%Y-%m-%dT%H:%M")
    reply = answer(client, ids, "после обеда часа в три")
    assert reply.status_code == 200 and reply.json() == {"status": "set", "label": "завтра в 15:00"}
    assert state(app, ids)[0] == "corrected" and local(state(app, ids)[2]) == target
    (data,) = ai.calls
    assert data["answer"] == "после обеда часа в три" and data["reminder_text"] == "Дело 1"
    assert set(data) == {"answer", "sentence", "reminder_text", "today", "weekday", "now", "timezone"}
    assert data["timezone"] == "Europe/Moscow" and len(data["now"]) == 5
    with app.state.sessions() as db:
        (receipt,) = db.scalars(select(ProviderUsage).where(ProviderUsage.kind == "llm")).all()
        assert receipt.operation_id.startswith("rt:") and receipt.channel == "telegram" and receipt.status == "succeeded"
        assert db.scalar(select(func.count()).select_from(Note)) == 1


@pytest.mark.parametrize("value", [None, "2020-01-01T10:00", "завтра", "2099-01-01T10:00"])
def test_an_ai_that_finds_no_usable_time_leaves_the_reminder_and_keeps_waiting(asking, monkeypatch, value):
    app, client, ids, ai = asking
    patch_parser(monkeypatch, {})
    ai.value = value
    before = state(app, ids)
    reply = answer(client, ids, "после обеда часа в три")
    assert reply.status_code == 200 and reply.json() == {"status": "unclear"}
    assert state(app, ids) == before


def test_a_failing_ai_is_just_unclear_never_a_server_error(asking, monkeypatch):
    app, client, ids, ai = asking
    patch_parser(monkeypatch, {})
    ai.error = ProviderError("provider_unavailable")
    before = state(app, ids)
    reply = answer(client, ids, "после обеда часа в три")
    assert reply.status_code == 200 and reply.json() == {"status": "unclear"}
    assert state(app, ids) == before


def test_a_text_that_names_no_time_is_not_a_time_and_is_stored_nowhere(asking, monkeypatch):
    app, client, ids, ai = asking
    patch_parser(monkeypatch, {})
    reply = answer(client, ids, "Купить молоко и хлеб")
    assert reply.status_code == 200 and reply.json() == {"status": "not_time"}
    assert ai.calls == []
    with app.state.sessions() as db:
        assert db.scalar(select(Inbox.id).where(Inbox.update_id == 200)) is None
    # The bot now saves the same update as a note, which must not collide with this call.
    assert message(client, update_id=200, text="Купить молоко и хлеб", mode="manual").status_code == 200


def test_a_repeated_update_is_answered_from_the_inbox_without_another_ai_call(asking, monkeypatch):
    app, client, ids, ai = asking
    patch_parser(monkeypatch, {})
    target = (datetime.now(MOSCOW) + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0, tzinfo=None)
    ai.value = target.strftime("%Y-%m-%dT%H:%M")
    first = answer(client, ids, "после обеда часа в три")
    again = answer(client, ids, "после обеда часа в три")
    assert first.json() == again.json() and len(ai.calls) == 1 and state(app, ids)[1] == 2
    changed = answer(client, ids, "после обеда часа в пять")
    assert changed.status_code == 409  # The same update with other content.


def test_a_past_time_is_never_set(asking, monkeypatch):
    app, client, ids, ai = asking
    yesterday = datetime.now(MOSCOW).replace(tzinfo=None) - timedelta(hours=3)
    patch_parser(monkeypatch, {"сегодня в 4 утра": {"result": yesterday, "certain": True}})
    before = state(app, ids)
    reply = answer(client, ids, "сегодня в 4 утра")
    assert reply.json() == {"status": "unclear"} and state(app, ids) == before


def test_the_web_edit_settles_a_pending_question(asking):
    app, client, ids, _ = asking
    with app.state.sessions() as db:
        row = db.get(Reminder, ids["reminder"])
        scheduled = datetime.fromtimestamp(row.scheduled_at, MOSCOW).replace(tzinfo=None) + timedelta(hours=1)
    response = client.patch("/api/v1/reminders/" + ids["reminder"], json={
        "scheduled_at": scheduled.replace(tzinfo=MOSCOW).isoformat(), "timezone": "Europe/Moscow",
        "text": "Дело 1", "generation": 1})
    assert response.status_code == 200, response.text
    assert response.json()["time_check"] == "corrected"
    assert confirm(client, ids, "ok").json()["error"]["code"] == "action_expired"  # The buttons are stale now.
    listed = client.get("/api/v1/notes/" + ids["note"] + "/reminders").json()
    assert [r["time_check"] for r in listed] == ["corrected"]


def test_the_scheduler_still_delivers_a_reminder_that_was_never_answered(asking):
    app, client, ids, _ = asking
    with app.state.sessions.begin() as db:
        db.get(Reminder, ids["reminder"]).scheduled_at = time.time() - 1
        db.add(Outbox(user_id=ids["user"], reminder_id=ids["reminder"], bot_id=BASE["bot_id"],
                      chat_id=BASE["chat_id"], generation=1))
    (item,) = [i for i in claim(client).json()["items"] if i["callback_token"]]
    assert item["text"].startswith("Напоминание\n\nДело 1") and item["confirm"] == []


# The AI call itself

def make_resolver(app, provider, fallback=None, **settings):
    changes = {"provider": "cloudru", "allow_live_requests": True, "cloudru_model": "synthetic",
               "live_call_limit": 10, "live_user_call_limit": 10, **settings}
    return TimeResolver(app.state.sessions, replace(app.state.settings, **changes), provider, fallback)


DATA = {"answer": "после обеда", "sentence": "s", "reminder_text": "t", "today": "2026-10-15", "weekday": "четверг",
        "now": "05:47", "timezone": "Europe/Moscow"}


def test_the_ai_call_counts_toward_the_per_user_live_call_limit(asking):
    app, client, ids, _ = asking
    ai = FakeAI()
    ai.value = "2026-10-16T15:00"
    resolver = make_resolver(app, ai, live_user_call_limit=1)
    assert resolver.resolve(ids["user"], "rt:one", DATA) == "2026-10-16T15:00"
    assert resolver.resolve(ids["user"], "rt:two", DATA) is None  # Limit spent: no second call.
    assert len(ai.calls) == 1
    with app.state.sessions() as db:
        assert db.get(User, ids["user"]).live_calls == 1


def test_a_spent_global_call_budget_stops_the_call(asking):
    from app.models import ProviderBudget

    app, client, ids, _ = asking
    ai = FakeAI()
    ai.value = "2026-10-16T15:00"
    resolver = make_resolver(app, ai, live_call_limit=3)
    with app.state.sessions.begin() as db:
        budget = db.get(ProviderBudget, "cloudru") or ProviderBudget(id="cloudru")
        budget.reserved_calls = 3
        db.add(budget)
    assert resolver.resolve(ids["user"], "rt:one", DATA) is None and ai.calls == []


def test_the_reserve_answers_when_the_main_gateway_does_not(asking):
    from tests.test_ai_failover import FALLBACK_SETTINGS, RELAY_TOKEN

    app, client, ids, _ = asking
    primary = FakeAI()
    primary.error = ProviderError("provider_unavailable")
    base = FALLBACK_SETTINGS["fallback_base_url"]
    seen = []

    def relay(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"due_at": "2026-10-16T15:00"})}}], "usage": {"prompt_tokens": 200, "completion_tokens": 20}})

    reserve = CloudRuProvider(RELAY_TOKEN, "gpt-6.1-sol", base_url=base, transport=httpx.MockTransport(relay),
                              allowed_base_urls={base}, label="openai")
    resolver = make_resolver(app, primary, reserve, **FALLBACK_SETTINGS)
    assert resolver.resolve(ids["user"], "rt:fb", DATA) == "2026-10-16T15:00"
    assert len(primary.calls) == 1 and len(seen) == 1
    assert seen[0]["messages"][0]["content"] == TIME_PROMPT
    assert json.loads(seen[0]["messages"][1]["content"])["answer"] == "после обеда"
    with app.state.sessions() as db:
        providers = [r.provider for r in db.scalars(select(ProviderUsage).order_by(ProviderUsage.occurred_at))]
    assert providers[-2:] == ["injected", "openai"]


def test_an_unusable_answer_is_repeated_once_then_given_up(asking):
    app, client, ids, _ = asking
    ai = FakeAI()
    ai.error = ProviderError("provider_invalid_response")
    assert make_resolver(app, ai).resolve(ids["user"], "rt:bad", DATA) is None
    assert len(ai.calls) == 2


def test_the_real_provider_asks_for_one_strict_answer_and_reads_it():
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"due_at":"2026-10-16T15:00"}'}}], "usage": {"prompt_tokens": 90, "completion_tokens": 12}})

    base = "https://foundation-models.api.cloud.ru/v1"
    provider = CloudRuProvider("synthetic-key-for-tests", "claude-opus-4.7", base_url=base,
                               transport=httpx.MockTransport(handle), allowed_base_urls={base})
    tokens = []
    assert provider.resolve_time(DATA, on_usage=tokens.append) == "2026-10-16T15:00"
    request = seen[0]
    assert request["max_tokens"] == 100 and request["response_format"]["json_schema"]["name"] == "TimeAnswer"
    assert request["messages"][0]["content"] == TIME_PROMPT and "temperature" not in request
    assert tokens[0].input_tokens == 90
    assert json.loads(request["messages"][1]["content"]) == DATA


def test_the_mock_provider_answers_the_time_question_deterministically():
    data = {**DATA, "answer": "после обеда часа в три", "now": "05:47"}
    assert MockProvider().resolve_time(data) == "2026-10-15T15:00"
    assert MockProvider().resolve_time({**data, "now": "16:00"}) == "2026-10-16T15:00"
    assert MockProvider().resolve_time({**data, "answer": "никогда"}) is None


def test_the_new_columns_start_empty_and_old_rows_read_as_no_question(notes):
    app, client, ids = notes
    with app.state.sessions.begin() as db:
        row = Reminder(user_id=ids["user"], note_id=ids["note"], scheduled_at=time.time() + 600,
                       timezone="Europe/Moscow", text="x")
        db.add(row)
        db.flush()
        assert (row.time_check, row.time_alt) == (None, None)
