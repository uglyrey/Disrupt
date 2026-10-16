"""Личные часы из профиля в разборе времени и во времени напоминания по умолчанию."""

import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app import profile as service
from app.models import Item, Note, Reminder, UserProfile
from app.reminder_proposals import mentions_time, parse_due_text_detailed
from app.reminders import awake_at, default_reminder_time, next_awake, request_reminders
from app.time_check import decide
from tests.test_reminder_requests import default_run
from tests.test_reminders import reminders as reminders  # noqa: F401

MOSCOW = ZoneInfo("Europe/Moscow")
WEDNESDAY = datetime(2026, 10, 14, 9, 0)
DAYPARTS = {
    "morning": (7, 30), "evening": (20, 0), "after_study": (15, 10), "weekend": (11, 0), "day": (13, 0),
    "after_work": (18, 30),
}
PREFS = {"dayparts": {"morning": (7, 30)}, "quiet": ((23, 0), (7, 0)), "wake": (7, 30), "sleep": (23, 0)}


def read(text, dayparts=None):
    return parse_due_text_detailed(text, WEDNESDAY, dayparts)["result"]


@pytest.mark.parametrize("text,default,personal", [
    ("завтра утром", datetime(2026, 10, 15, 9, 0), datetime(2026, 10, 15, 7, 30)),
    ("завтра вечером", datetime(2026, 10, 15, 19, 0), datetime(2026, 10, 15, 20, 0)),
    ("сегодня вечером", datetime(2026, 10, 14, 19, 0), datetime(2026, 10, 14, 20, 0)),
    ("на выходных", datetime(2026, 10, 17, 10, 0), datetime(2026, 10, 17, 11, 0)),
    ("завтра", datetime(2026, 10, 15, 9, 0), datetime(2026, 10, 15, 7, 30)),
    ("в субботу", datetime(2026, 10, 17, 9, 0), datetime(2026, 10, 17, 7, 30)),
    ("после пар", None, datetime(2026, 10, 14, 15, 10)),
    ("после работы", None, datetime(2026, 10, 14, 18, 30)),
    ("завтра после пар", datetime(2026, 10, 15, 9, 0), datetime(2026, 10, 15, 15, 10)),
])
def test_personal_times_replace_the_default_ones(text, default, personal):
    assert read(text) == default
    assert read(text, DAYPARTS) == personal


@pytest.mark.parametrize("text", ["завтра в 8 утра", "в 18:45", "завтра в 10"])
def test_an_exact_time_always_wins(text):
    assert read(text, DAYPARTS) == read(text)


def test_the_profile_gives_the_person_a_second_word_for_their_own_hours():
    assert mentions_time("Напомни после пар про лабу", DAYPARTS) is True
    assert mentions_time("Напомни про лабу", DAYPARTS) is False


def test_the_clock_reading_takes_the_profile_only_when_it_has_one():
    when = decide("завтра вечером", None, WEDNESDAY, DAYPARTS).when
    assert when == datetime(2026, 10, 15, 20, 0)
    assert decide("завтра вечером", None, WEDNESDAY).when == datetime(2026, 10, 15, 19, 0)
    assert decide("завтра вечером", None, WEDNESDAY, {}).when == datetime(2026, 10, 15, 19, 0)


@pytest.mark.parametrize("hour,minute,expected", [
    (12, 0, True), (7, 29, False), (7, 30, True), (22, 59, True), (23, 0, False), (3, 0, False),
])
def test_awake_by_wake_sleep_and_quiet_hours(hour, minute, expected):
    assert awake_at(datetime(2026, 10, 14, hour, minute), PREFS) is expected


def test_quiet_hours_alone_define_the_waking_day():
    prefs = {"dayparts": {}, "quiet": ((22, 0), (8, 0)), "wake": None, "sleep": None}
    assert awake_at(datetime(2026, 10, 14, 8, 0), prefs) and not awake_at(datetime(2026, 10, 14, 22, 0), prefs)
    assert next_awake(datetime(2026, 10, 14, 23, 0), prefs) == datetime(2026, 10, 15, 9, 0)  # Default morning.


def test_next_awake_waits_for_the_persons_morning():
    after = next_awake(datetime(2026, 10, 14, 23, 30), PREFS)
    assert after == datetime(2026, 10, 15, 7, 30)


def moscow(hour, minute):
    return datetime(2026, 10, 14, hour, minute, tzinfo=MOSCOW).astimezone(UTC)


@pytest.mark.parametrize("hour,minute,default,personal", [
    (12, 0, (14, 13, 0), (14, 13, 0)),
    (6, 0, (14, 9, 0), (14, 7, 30)),
    (22, 30, (15, 9, 0), (15, 7, 30)),
    (23, 30, (15, 9, 0), (15, 7, 30)),
    (3, 0, (14, 9, 0), (14, 7, 30)),
])
def test_default_time_for_a_request_without_a_time(app, hour, minute, default, personal):
    settings = app.state.settings
    day, h, m = default
    assert default_reminder_time(moscow(hour, minute), settings, "Europe/Moscow") == datetime(2026, 10, day, h, m, tzinfo=MOSCOW)
    day, h, m = personal
    assert default_reminder_time(moscow(hour, minute), settings, "Europe/Moscow", PREFS) == datetime(
        2026, 10, day, h, m, tzinfo=MOSCOW)


def test_an_hour_later_never_lands_in_quiet_hours(app):
    when = default_reminder_time(moscow(22, 20), app.state.settings, "Europe/Moscow", PREFS)
    assert when == datetime(2026, 10, 14, 23, 20, tzinfo=MOSCOW) or when == datetime(2026, 10, 15, 7, 30, tzinfo=MOSCOW)
    assert awake_at(when.astimezone(MOSCOW), PREFS)


def test_a_late_wake_up_moves_the_default_from_the_early_morning(app):
    late = {"dayparts": {}, "quiet": None, "wake": (11, 0), "sleep": (1, 0)}
    when = default_reminder_time(moscow(9, 0), app.state.settings, "Europe/Moscow", late)
    assert when == datetime(2026, 10, 14, 11, 0, tzinfo=MOSCOW)


def make_ready(app, user_id, prefs):
    with app.state.sessions.begin() as db:
        db.merge(UserProfile(user_id=user_id, state="ready", answers={"q1": "x"}, facts=[], updated_at=time.time(),
                             prefs={"dayparts": prefs.get("dayparts", {}), "quiet_hours": prefs.get("quiet_hours"),
                                    "wake": prefs.get("wake"), "sleep": prefs.get("sleep")}))


def test_time_prefs_come_only_from_a_ready_profile(app, reminders):
    app_, client, ids = reminders
    user_id = client.get("/api/v1/auth/me").json()["id"]
    with app.state.sessions() as db:
        assert service.time_prefs(db, user_id) is None and service.time_dayparts(db, user_id) is None
    make_ready(app, user_id, {"dayparts": {"morning": "07:30", "weekend": "bad"}, "wake": "07:30", "sleep": "23:00",
                              "quiet_hours": {"from": "23:00", "to": "07:00"}})
    with app.state.sessions() as db:
        assert service.time_prefs(db, user_id) == {
            "dayparts": {"morning": (7, 30)}, "quiet": ((23, 0), (7, 0)), "wake": (7, 30), "sleep": (23, 0),
            "style": None,
        }
        assert service.time_dayparts(db, user_id) == {"morning": (7, 30)}
        assert service.prompt_dayparts(db, user_id) == {"dayparts": {"morning": "07:30", "weekend": "bad"}}
        db.get(UserProfile, user_id).state = "pending"
        assert service.time_prefs(db, user_id) is None and service.prompt_dayparts(db, user_id) is None
        db.rollback()


def test_a_request_without_a_time_uses_the_persons_morning_at_night(app, reminders):
    app_, client, ids = reminders
    user_id = client.get("/api/v1/auth/me").json()["id"]
    now = datetime(2026, 10, 10, 19, 30, tzinfo=UTC)  # 22:30 in Moscow
    assert [w for w, _ in default_run(app_, ids, now)] == [datetime(2026, 10, 11, 9, 0, tzinfo=MOSCOW)]
    with app_.state.sessions.begin() as db:
        for reminder in db.scalars(select(Reminder)).all():
            db.delete(reminder)
        db.flush()
        for item in db.scalars(select(Item).where(Item.source_quote == "Напомни позвонить маме")).all():
            db.delete(item)
    make_ready(app_, user_id, {"dayparts": {"morning": "07:30"}, "wake": "07:30", "sleep": "23:00"})
    assert [w for w, _ in default_run(app_, ids, now)] == [datetime(2026, 10, 11, 7, 30, tzinfo=MOSCOW)]


def test_request_reminders_reads_a_personal_daypart_from_the_sentence(app, reminders):
    app_, client, ids = reminders
    user_id = client.get("/api/v1/auth/me").json()["id"]
    make_ready(app_, user_id, {"dayparts": {"evening": "21:15"}})
    now = datetime(2026, 10, 10, 7, 0, tzinfo=UTC)  # 10:00 in Moscow
    with app_.state.sessions.begin() as db:
        created = request_reminders(db, db.get(Note, ids["note"]), "Напомни сегодня вечером позвонить маме",
                                    app_.state.settings, now=now)
        assert [datetime.fromtimestamp(r.scheduled_at, MOSCOW) for r in created] == [
            datetime(2026, 10, 10, 21, 15, tzinfo=MOSCOW)]
