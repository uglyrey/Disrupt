"""Human notes, the event instead of the reminder request, and the length of a calendar event (docs/note-style-v1.md)."""

import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models import Note, Reminder, Revision, User
from app.note_style import (
    STYLE_PROMPT,
    estimate_duration,
    explicit_duration,
    human_when,
    restyle,
    strip_reminder_words,
    strip_system_talk,
    valid_minutes,
    with_reminder_lines,
)
from app.providers import SYSTEM_PROMPT, MockProvider, validate_result
from app.schemas import ProposedItem, StructuredNote
from app.worker import Worker
from tests.test_internal import message
from tests.test_reminders import reminders as reminders  # noqa: F401

MOSCOW = ZoneInfo("Europe/Moscow")


def run_worker(app, provider=None):
    assert Worker(app.state.sessions, app.state.settings, provider=provider or MockProvider()).run_once()


def the_note(app, capture_id):
    with app.state.sessions() as db:
        note = db.scalar(select(Note).where(Note.capture_id == capture_id))
        db.expunge(note)
        return note


def reminder_rows(app):
    with app.state.sessions() as db:
        rows = db.scalars(select(Reminder).where(Reminder.advance_of.is_(None)).order_by(Reminder.scheduled_at)).all()
        db.expunge_all()
        return rows


def day_text(days, hour):
    """«ср, 5 ноября, 09:00» for the day `days` ahead of today in Moscow."""
    now = datetime.now(MOSCOW)
    moment = (now + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    return human_when(moment, now.year)


def span_minutes(url):
    start, end = parse_qs(urlsplit(url).query)["dates"][0].split("/")
    parse = lambda value: datetime.strptime(value, "%Y%m%dT%H%M%SZ")  # noqa: E731
    return int((parse(end) - parse(start)).total_seconds() // 60)


# ---- Prompt ----


def test_the_prompt_carries_the_style_rules_and_examples():
    assert STYLE_PROMPT.strip() in SYSTEM_PROMPT
    for must in ("от первого лица", "Никогда не упоминай ИИ", "«напомнить»", "duration_minutes", "5..720", "null"):
        assert must in SYSTEM_PROMPT
    assert 4 <= SYSTEM_PROMPT.count(" даёт ") <= 8
    assert "—" not in SYSTEM_PROMPT and '"duration_minutes":null' in SYSTEM_PROMPT


# ---- Text rules ----


@pytest.mark.parametrize("text, due, expected", [
    ("Завтра в 10 напомни позвонить маме", "Завтра в 10", "Позвонить маме"),
    ("Напомнить позвонить маме", None, "Позвонить маме"),
    ("Не забыть напомнить про встречу", None, "Про встречу"),
    ("Поставь напоминание купить хлеб", None, "Купить хлеб"),
    ("Напомни мне, что нужно оплатить счёт", None, "Нужно оплатить счёт"),
    ("Напомнить", None, "Напомнить"),  # Nothing would be left, so the text stays.
])
def test_reminder_words_are_stripped(text, due, expected):
    assert strip_reminder_words(text, due) == expected


def test_talk_to_the_system_is_stripped_but_ordinary_words_stay():
    assert strip_system_talk("Запиши, что я решил перейти на новую CRM") == "Я решил перейти на новую CRM"
    assert strip_system_talk("Подумать про цены, сказать ИИ") == "Подумать про цены"
    assert strip_system_talk("Записать Марка к врачу") == "Записать Марка к врачу"
    assert strip_system_talk("Новый ИИ-проект для школы") == "Новый ИИ-проект для школы"


def test_restyle_cleans_title_text_and_tasks_and_keeps_the_quotes():
    original = "Завтра в 10 напомни позвонить маме"
    raw = StructuredNote.model_validate({
        "title": "Напомнить позвонить маме", "markdown": "Напомнить позвонить маме завтра в 10.\n\n- Дизайн готов",
        "conclusions": [],
        "items": [{"kind": "task", "text": "Напомнить позвонить маме", "source_quote": original, "due_text": "Завтра в 10"}],
    })
    result = restyle(raw)
    assert result.title == "Позвонить маме"
    assert result.markdown == "Позвонить маме.\n\n- Дизайн готов"  # The time moves to the reminder line.
    assert result.items[0].text == "Позвонить маме" and result.items[0].source_quote == original
    assert validate_result(result, original).items[0].due_text == "Завтра в 10"
    plain = StructuredNote.model_validate({"title": "Запуск", "markdown": "Андрей завтра уточнит.", "conclusions": [],
                                           "items": [{"kind": "task", "text": "Андрей завтра уточнит.",
                                                      "source_quote": "Андрей завтра уточнит.", "due_text": "завтра"}]})
    assert restyle(plain).items[0].text == "Андрей завтра уточнит."  # A task without reminder words is not touched.


def test_reminder_lines_one_per_event():
    assert with_reminder_lines("Позвонить маме.", [("Позвонить маме", "пн, 3 ноября, 10:00")]) == \
        "Позвонить маме.\n\nНапоминание: пн, 3 ноября, 10:00"
    two = with_reminder_lines("- Тренировка.\n- Созвон.", [("тренировка", "завтра, 09:00"), ("созвон", "завтра, 18:00")])
    assert two.endswith("Напоминание «Тренировка»: завтра, 09:00  \nНапоминание «Созвон»: завтра, 18:00")
    assert with_reminder_lines(two, [("тренировка", "завтра, 09:00"), ("созвон", "завтра, 18:00")]) == two
    assert with_reminder_lines("Текст", []) == "Текст"


# ---- Length of the event ----


@pytest.mark.parametrize("text, minutes", [
    ("встреча с 14 до 16", 120), ("с 10:00 до 11:30", 90), ("с 22 до 2", 240), ("созвон на два часа", 120),
    ("на 30 минут", 30), ("на час", 60), ("на полчаса", 30), ("на полтора часа", 90),
    ("в 8 утра", None), ("на 5 часов вечера", None), ("с 9 до 23", 840 - 0 and None),
])
def test_a_named_length_is_read_exactly(text, minutes):
    assert explicit_duration(text) == minutes


def test_the_estimate_follows_the_kind_of_event():
    assert estimate_duration("Позвонить маме") == 15
    assert estimate_duration("Созвон с командой") == estimate_duration("Встреча с Лидой") == 60
    assert estimate_duration("Пара по математике") == 90 and estimate_duration("Тренировка") == 60
    assert estimate_duration("Забрать посылку") is None


@pytest.mark.parametrize("value, expected", [(5, 5), (720, 720), (4, None), (721, None), (0, None), (-30, None),
                                             (60.0, 60), (60.5, None), ("60", None), (True, None), (None, None)])
def test_the_length_must_be_between_5_and_720(value, expected):
    assert valid_minutes(value) == expected
    item = ProposedItem.model_validate({"kind": "task", "text": "Дело", "source_quote": "дело", "duration_minutes": value})
    assert item.duration_minutes == expected


def test_a_model_item_without_a_length_is_valid():
    item = ProposedItem.model_validate({"kind": "task", "text": "Дело", "source_quote": "дело"})
    assert item.duration_minutes is None
    with pytest.raises(ValidationError):
        ProposedItem.model_validate({"kind": "task", "text": "Дело", "source_quote": "дело", "extra": 1})


# ---- Mock provider ----


def test_the_mock_writes_the_event_and_a_length():
    result = MockProvider().structure("Завтра в 10 напомни позвонить маме")
    assert result.title == "Позвонить маме" and result.markdown == "Позвонить маме."
    (task,) = result.items
    assert (task.text, task.due_text, task.duration_minutes) == ("Позвонить маме", "Завтра в 10", 15)
    lengths = {i.text: i.duration_minutes for i in MockProvider().structure(
        "Напомни завтра в 9 про тренировку, потом в 18 про созвон с командой. Напомни в пятницу в 14 про встречу с Лидой на два часа",
    ).items}
    assert lengths == {"Тренировка": 60, "Созвон с командой": 60, "Встреча с Лидой": 120}


def test_the_mock_drops_talk_to_the_system():
    result = MockProvider().structure("Запиши, что я решил перейти на новую CRM. Скажи ИИ, что надо подумать про цены.")
    text = result.title + result.markdown
    assert "Запиши" not in text and "ИИ" not in text and "скажи" not in text.casefold()
    assert result.markdown == "- Я решил перейти на новую CRM.\n- Надо подумать про цены."


def test_a_plain_text_keeps_the_old_mock_draft():
    assert MockProvider().structure("Просто мысль про сад").title == "Черновик мыслей"


# ---- Through the worker ----


def test_the_note_names_the_event_and_the_time_of_the_reminder(reminders):
    app, client, ids = reminders
    saved = message(client, update_id=900, text="Завтра в 10 утра напомни позвонить маме").json()
    run_worker(app)
    note = the_note(app, saved["capture_id"])
    assert note.title == "Позвонить маме"
    assert note.markdown == f"Позвонить маме.\n\nНапоминание: {day_text(1, 10)}"
    assert "напомни" not in (note.title + note.markdown).casefold().replace("напоминание:", "")
    (row,) = reminder_rows(app)
    assert row.text == "Позвонить маме" and row.duration_minutes == 15 and row.time_check is None
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW).strftime("%H:%M") == "10:00"


def test_several_reminders_give_one_line_each(reminders):
    app, client, ids = reminders
    saved = message(client, update_id=901,
                    text="Напомни послезавтра в 9 утра про тренировку, потом послезавтра в 18 вечера про созвон с командой").json()
    run_worker(app)
    note = the_note(app, saved["capture_id"])
    assert note.markdown.endswith(
        f"Напоминание «Тренировка»: {day_text(2, 9)}  \nНапоминание «Созвон с командой»: {day_text(2, 18)}")
    assert [r.text for r in reminder_rows(app)] == ["Тренировка", "Созвон с командой"]


def test_a_guessed_time_keeps_the_question_and_adds_no_line(reminders):
    app, client, ids = reminders
    saved = message(client, update_id=902, text="Напомни позвонить маме").json()
    run_worker(app)
    note = the_note(app, saved["capture_id"])
    (row,) = reminder_rows(app)
    assert row.time_check == "ask" and "Напоминание:" not in note.markdown and note.title == "Позвонить маме"


# ---- Calendar length ----


def calendar_url_of(client, row_id):
    return client.get("/api/v1/reminders/" + row_id).json()["calendar_url"]


def test_a_named_range_gives_exactly_that_length(reminders):
    app, client, ids = reminders
    message(client, update_id=910, text="Напомни завтра в 14 утра про встречу с 14 до 16")
    run_worker(app)
    (row,) = reminder_rows(app)
    assert row.duration_minutes == 120
    assert span_minutes(calendar_url_of(client, row.id)) == 120


def test_the_ai_estimate_is_used_when_nothing_is_named(reminders):
    app, client, ids = reminders
    message(client, update_id=911, text="Напомни завтра в 10 утра позвонить маме")
    run_worker(app)
    (row,) = reminder_rows(app)
    assert row.duration_minutes == 15 and span_minutes(calendar_url_of(client, row.id)) == 15


def test_without_a_length_the_default_hour_applies_and_the_person_can_change_it(reminders):
    app, client, ids = reminders
    message(client, update_id=912, text="Напомни завтра в 10 утра забрать посылку")
    run_worker(app)
    (row,) = reminder_rows(app)
    assert row.duration_minutes is None
    assert span_minutes(calendar_url_of(client, row.id)) == 60
    assert client.get("/api/v1/auth/me").json()["calendar_default_minutes"] == 60
    assert client.get("/api/v1/profile").json()["calendar_default_minutes"] == 60
    reply = client.patch("/api/v1/profile/calendar-minutes", json={"calendar_default_minutes": 90})
    assert reply.status_code == 200 and reply.json() == {"calendar_default_minutes": 90}
    assert span_minutes(calendar_url_of(client, row.id)) == 90
    assert client.get("/api/v1/auth/me").json()["calendar_default_minutes"] == 90
    with app.state.sessions() as db:
        assert db.scalar(select(User.calendar_default_minutes)) == 90


def test_the_default_length_accepts_only_the_five_choices(reminders):
    app, client, ids = reminders
    for good in (15, 30, 60, 90, 120):
        assert client.patch("/api/v1/profile/calendar-minutes", json={"calendar_default_minutes": good}).status_code == 200
    for bad in (0, 5, 45, 121, 720, "60", None, True):
        assert client.patch("/api/v1/profile/calendar-minutes", json={"calendar_default_minutes": bad}).status_code == 422
    assert client.patch("/api/v1/profile/calendar-minutes", json={}).status_code == 422
    assert client.get("/api/v1/profile").json()["calendar_default_minutes"] == 120


class Estimating(MockProvider):
    """A model that answers with whatever length the test gives it."""

    minutes = 600

    def structure(self, text, *, categories=()):
        return StructuredNote.model_validate({
            "title": "Дело", "markdown": "Дело.", "conclusions": [],
            "items": [{"kind": "task", "text": "Дело", "source_quote": text, "due_text": "завтра в 10 утра",
                       "duration_minutes": self.minutes}],
        })


@pytest.mark.parametrize("minutes, stored", [(5, 5), (720, 720), (4, None), (721, None), (-1, None)])
def test_bounds_of_the_model_length(reminders, minutes, stored):
    app, client, ids = reminders
    message(client, update_id=920 + (minutes % 7), text="завтра в 10 утра напомни про дело")
    provider = Estimating()
    provider.minutes = minutes
    run_worker(app, provider)
    (row,) = reminder_rows(app)
    assert row.duration_minutes == stored
    assert span_minutes(calendar_url_of(client, row.id)) == (stored or 60)


def test_the_telegram_link_uses_the_same_length(reminders):
    from app.calendar_link import calendar_url
    at = 1_792_000_000
    assert span_minutes(calendar_url("Дело", at, "https://x.example/", 15)) == 15
    assert span_minutes(calendar_url("Дело", at, "https://x.example/", None)) == 60
    assert span_minutes(calendar_url("Дело", at, "https://x.example/", 9999)) == 60
    assert timedelta(minutes=60) == timedelta(hours=1)


# ---- The line with an absolute date, the guessed time, and a moved reminder ----


def test_the_date_is_absolute_and_the_year_only_when_it_differs():
    assert human_when(datetime(2026, 11, 3, 10, 0), 2026) == "вт, 3 ноября, 10:00"
    assert human_when(datetime(2026, 11, 5, 9, 5), 2026) == "чт, 5 ноября, 09:05"
    assert human_when(datetime(2027, 1, 4, 18, 30), 2026) == "пн, 4 января 2027, 18:30"


def move(client, row, days, hour):
    when = (datetime.now(MOSCOW) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    body = {"scheduled_at": when.isoformat(), "timezone": row.timezone, "text": row.text, "generation": row.generation}
    reply = client.patch("/api/v1/reminders/" + row.id, json=body)
    assert reply.status_code == 200, reply.text


def set_markdown(app, note_id, markdown):
    with app.state.sessions.begin() as db:
        db.get(Note, note_id).markdown = markdown


def note_state(app, note_id):
    with app.state.sessions() as db:
        note = db.get(Note, note_id)
        return note.markdown, note.version, db.scalar(
            select(func.count()).select_from(Revision).where(Revision.note_id == note_id))


def test_a_guessed_time_writes_the_line_once_when_the_person_confirms(reminders):
    from app.reminder_confirm import confirm_action
    app, client, ids = reminders
    saved = message(client, update_id=930, text="Напомни позвонить маме").json()
    run_worker(app)
    note = the_note(app, saved["capture_id"])
    (row,) = reminder_rows(app)
    assert row.time_check == "ask" and "Напоминание" not in note.markdown and row.note_line is None
    with app.state.sessions.begin() as db:
        assert confirm_action(db, row.user_id, row.id, row.generation, "ok")["status"] == "confirmed"
    markdown, version, _ = note_state(app, note.id)
    (row,) = reminder_rows(app)
    line = "Напоминание: " + human_when(datetime.fromtimestamp(row.scheduled_at, MOSCOW).replace(tzinfo=None), datetime.now(MOSCOW).year)
    assert markdown == "Позвонить маме.\n\n" + line and row.note_line == line and version == note.version + 1
    with app.state.sessions.begin() as db:  # Pressed twice: still one line.
        confirm_action(db, row.user_id, row.id, row.generation, "ok")
    assert note_state(app, note.id)[0] == markdown


def test_a_time_written_in_the_check_flow_reaches_the_note(reminders):
    from app.reminder_confirm import answer_time
    app, client, ids = reminders
    saved = message(client, update_id=931, text="Напомни позвонить маме").json()
    run_worker(app)
    note = the_note(app, saved["capture_id"])
    (row,) = reminder_rows(app)
    with app.state.sessions.begin() as db:
        assert answer_time(db, row.user_id, row.id, row.generation, "послезавтра в 15:00")["status"] == "set"
    assert note_state(app, note.id)[0].endswith("Напоминание: " + day_text(2, 15))


def test_a_moved_reminder_replaces_its_own_line_and_saves_a_revision(reminders):
    app, client, ids = reminders
    saved = message(client, update_id=932, text="Завтра в 10 утра напомни позвонить маме").json()
    run_worker(app)
    note = the_note(app, saved["capture_id"])
    (row,) = reminder_rows(app)
    before = note_state(app, note.id)
    move(client, row, 3, 16)
    markdown, version, revisions = note_state(app, note.id)
    assert markdown == f"Позвонить маме.\n\nНапоминание: {day_text(3, 16)}"
    assert version == before[1] + 1 and revisions == before[2] + 1
    assert reminder_rows(app)[0].note_line == f"Напоминание: {day_text(3, 16)}"


def test_a_line_the_person_edited_or_removed_is_left_alone(reminders):
    app, client, ids = reminders
    saved = message(client, update_id=933, text="Завтра в 10 утра напомни позвонить маме").json()
    run_worker(app)
    note = the_note(app, saved["capture_id"])
    (row,) = reminder_rows(app)
    edited = f"Позвонить маме.\n\nНапоминание: {day_text(1, 10)}, не забыть цветы"
    set_markdown(app, note.id, edited)
    before = note_state(app, note.id)
    move(client, row, 3, 16)
    assert note_state(app, note.id) == before and before[0] == edited  # Not a fuzzy match.
    set_markdown(app, note.id, "Позвонить маме.")
    before = note_state(app, note.id)
    (row,) = reminder_rows(app)
    move(client, row, 4, 11)
    assert note_state(app, note.id) == before and before[0] == "Позвонить маме."


def test_the_telegram_snooze_moves_the_line_too(reminders):
    from app.reminders import reschedule, snooze_time
    app, client, ids = reminders
    saved = message(client, update_id=934, text="Завтра в 10 утра напомни позвонить маме").json()
    run_worker(app)
    note = the_note(app, saved["capture_id"])
    (row,) = reminder_rows(app)
    with app.state.sessions.begin() as db:
        live = db.get(Reminder, row.id)
        reschedule(db, live, snooze_time(live, "snooze_hour", time.time()), live.timezone, live.text, "telegram")
        stamp = live.scheduled_at
    local = datetime.fromtimestamp(stamp, MOSCOW).replace(tzinfo=None)
    assert note_state(app, note.id)[0].endswith("Напоминание: " + human_when(local, datetime.now(MOSCOW).year))
