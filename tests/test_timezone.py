"""Часовой пояс аккаунта: список, проверка, API, учёт на сервере и маршруты для бота. Имитация ИИ, без сети."""

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import timezones as tz
from app.config import Settings
from app.dashboard import build_dashboard
from app.limits import daily_state
from app.models import Inbox, Reminder, User
from app.providers import CloudRuProvider, MockProvider, structure_context
from app.reminders import default_reminder_time
from app.services import reminder_proposals
from app.worker import Worker
from tests.conftest import register
from tests.test_internal import BASE, HEADERS, message
from tests.test_providers import completion
from tests.test_reminder_requests import ASK, Silent, Tasking, auto_rows, run_worker
from tests.test_reminders import claim, payload
from tests.test_reminders import reminders as reminders

URL = "/api/v1/account/timezone"
NOVOSIBIRSK, MOSCOW = ZoneInfo("Asia/Novosibirsk"), ZoneInfo("Europe/Moscow")


def tomorrow_at(zone, hour):
    day = datetime.now(zone).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=zone)


def set_zone(client, zone="Asia/Novosibirsk", source="manual"):
    response = client.patch(URL, json={"timezone": zone, "source": source})
    assert response.status_code == 200, response.text
    return response.json()


# Список и проверка

def test_zone_list_has_every_russian_zone_with_a_label_sorted_by_offset():
    rows = tz.zone_list()
    by_id = {row["id"]: row for row in rows}
    assert len(tz.RUSSIA) >= 26
    for zone, label in tz.RUSSIA.items():
        assert by_id[zone]["label"] == label
    for label in ("Калининград", "Москва", "Самара", "Екатеринбург", "Омск", "Новосибирск", "Красноярск", "Иркутск",
                  "Якутск", "Владивосток", "Магадан", "Камчатка", "Волгоград", "Саратов", "Ульяновск", "Астрахань",
                  "Барнаул", "Томск", "Новокузнецк", "Чита", "Сахалин", "Анадырь", "Среднеколымск", "Хандыга",
                  "Усть-Нера"):
        assert any(row["label"] == label for row in rows), label
    assert "UTC" in by_id
    keys = [(row["offset_minutes"], row["label"].lower(), row["id"]) for row in rows]
    assert keys == sorted(keys)
    assert by_id["Europe/Moscow"]["offset"] == "+03:00" and by_id["Asia/Kolkata"]["offset"] == "+05:30"
    assert by_id["Asia/Novosibirsk"]["offset_minutes"] == 420


def test_zone_list_offsets_are_current_and_all_ids_are_known():
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    for row in tz.zone_list(now):
        assert tz.valid_zone(row["id"])
        expected = now.astimezone(ZoneInfo(row["id"])).utcoffset().total_seconds() // 60
        assert row["offset_minutes"] == expected
        assert row["offset"][0] in "+-" and len(row["offset"]) == 6
    winter = {row["id"]: row for row in tz.zone_list(datetime(2026, 1, 15, 12, tzinfo=UTC))}
    summer = {row["id"]: row for row in tz.zone_list(now)}
    assert winter["Europe/Berlin"]["offset"] == "+01:00" and summer["Europe/Berlin"]["offset"] == "+02:00"


def test_zone_list_skips_legacy_aliases_but_every_database_name_is_accepted():
    ids = {row["id"] for row in tz.zone_list()}
    assert not {"Asia/Calcutta", "Europe/Kiev", "US/Pacific", "Etc/GMT+5", "Factory", "posixrules"} & ids
    assert {"Europe/Kyiv", "Asia/Kolkata", "America/Los_Angeles", "UTC"} <= ids
    for name in ("Asia/Calcutta", "US/Pacific", "Etc/GMT+5", "Europe/Kiev", "Asia/Novosibirsk"):
        assert tz.valid_zone(name)
    # A zone without a Russian name shows the city part of the IANA name.
    assert tz.label_for_zone("America/Argentina/Cordoba") == "Cordoba"
    assert tz.label_for_zone("Pacific/Port_Moresby") == "Port Moresby"


def test_valid_zone_rejects_junk():
    for bad in ("", "Factory", "posixrules", "Mars/Olympus", "../etc/passwd", "Europe/Moscow\x00", " Europe/Moscow",
                "europe/moscow", "x" * 65, None, 5, ["Europe/Moscow"]):
        assert not tz.valid_zone(bad), bad


def test_effective_zone_is_the_account_zone_else_the_service_zone():
    settings = Settings()
    assert tz.effective_zone(User(timezone="Asia/Omsk"), settings) == "Asia/Omsk"
    assert tz.effective_zone(User(timezone=None), settings) == settings.limit_timezone == "Europe/Moscow"
    assert tz.effective_zone(User(timezone="Not/AZone"), settings) == "Europe/Moscow"
    assert tz.effective_zone(None, settings) == "Europe/Moscow"
    assert tz.zone_guessed(User(timezone=None)) and not tz.zone_guessed(User(timezone="Asia/Omsk"))


# Город по местоположению и по тексту

def test_nearest_city_gives_the_zone():
    assert tz.nearest_city(55.03, 82.92)[:2] == ("Новосибирск", "Asia/Novosibirsk")
    assert tz.nearest_city(54.72, 20.5)[1] == "Europe/Kaliningrad"
    assert tz.nearest_city(43.1, 131.9)[1] == "Asia/Vladivostok"
    assert tz.nearest_city(43.25, 76.95)[1] == "Asia/Almaty"
    assert tz.nearest_city(55.75, 37.6)[1] == "Europe/Moscow"
    assert tz.nearest_city(48.5, 135.0)[1] == "Asia/Vladivostok"  # Хабаровск
    assert tz.nearest_city(0.0, -150.0) is None  # open ocean
    assert tz.nearest_city(91, 0) is None and tz.nearest_city(float("nan"), 0) is None
    assert tz.nearest_city("55", "82") is None


def test_city_table_covers_every_russian_zone_and_the_cis_capitals():
    zones = {row[1] for row in tz.CITIES}
    assert len(tz.CITIES) >= 60
    assert set(tz.RUSSIA) <= zones
    for city in ("Киев", "Минск", "Кишинёв", "Тбилиси", "Ереван", "Баку", "Ташкент", "Астана", "Бишкек", "Душанбе",
                 "Ашхабад", "Рига", "Вильнюс", "Таллин"):
        assert any(row[0] == city for row in tz.CITIES), city
    for _city, zone, lat, lon in tz.CITIES:
        assert tz.valid_zone(zone) and -90 <= lat <= 90 and -180 <= lon <= 180


def test_find_zone_by_label_iana_name_and_offset():
    assert tz.find_zone("Новосибирск") == "Asia/Novosibirsk"
    assert tz.find_zone("  новосибирск ") == "Asia/Novosibirsk"
    assert tz.find_zone("екатеринбург") == "Asia/Yekaterinburg"
    assert tz.find_zone("Asia/Omsk") == "Asia/Omsk" and tz.find_zone("asia/omsk") == "Asia/Omsk"
    assert tz.find_zone("Omsk") == "Asia/Omsk"
    assert tz.find_zone("г. Омск, Россия") == "Asia/Omsk"
    assert tz.find_zone("Хабаровск") == "Asia/Vladivostok"
    assert tz.find_zone("Санкт-Петербург") == "Europe/Moscow"
    for text, zone in (("+5", "Asia/Yekaterinburg"), ("UTC+5", "Asia/Yekaterinburg"), ("utc +7", "Asia/Novosibirsk"),
                       ("GMT+3", "Europe/Moscow"), ("+10", "Asia/Vladivostok"), ("+12", "Asia/Kamchatka")):
        assert tz.find_zone(text) == zone, text
    # An offset never becomes an Etc/GMT name.
    for text in ("+1", "-5", "+0", "UTC", "+13", "", "абракадабра", "x" * 100, None, 5):
        assert tz.find_zone(text) in (None, "UTC"), text
    assert tz.find_zone("+1") is None and tz.find_zone("-5") is None


# API

def test_account_zone_needs_a_session_and_csrf(app, client):
    for method, path in (("get", "/api/v1/timezones"), ("get", URL)):
        assert getattr(client, method)(path).status_code == 401
    assert client.patch(URL, json={"timezone": "Asia/Omsk", "source": "manual"}).status_code == 401
    register(client)
    assert client.patch(URL, json={"timezone": "Asia/Omsk", "source": "manual"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.get(URL).json() == {"timezone": None, "source": None, "label": None}


def test_zones_endpoint_returns_the_list_with_private_cache(app, client):
    register(client)
    response = client.get("/api/v1/timezones")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=3600"
    zones = response.json()["zones"]
    assert [z["id"] for z in zones] == [z["id"] for z in tz.zone_list()]
    assert set(zones[0]) == {"id", "label", "offset", "offset_minutes"}
    assert next(z for z in zones if z["id"] == "Asia/Novosibirsk") == {
        "id": "Asia/Novosibirsk", "label": "Новосибирск", "offset": "+07:00", "offset_minutes": 420,
    }


def test_auto_fills_only_an_empty_value_and_manual_always_writes(app, client):
    register(client)
    assert set_zone(client, "Asia/Omsk", "auto") == {"timezone": "Asia/Omsk", "source": "auto", "label": "Омск"}
    # Auto never overwrites: the answer is the current state.
    assert set_zone(client, "Europe/Berlin", "auto") == {"timezone": "Asia/Omsk", "source": "auto", "label": "Омск"}
    assert set_zone(client, "Asia/Novosibirsk", "manual") == {
        "timezone": "Asia/Novosibirsk", "source": "manual", "label": "Новосибирск",
    }
    assert set_zone(client, "Europe/Berlin", "auto")["timezone"] == "Asia/Novosibirsk"
    assert client.get(URL).json() == {"timezone": "Asia/Novosibirsk", "source": "manual", "label": "Новосибирск"}
    # Any database name is accepted on write, even an alias the list skips.
    assert set_zone(client, "Asia/Calcutta", "manual")["timezone"] == "Asia/Calcutta"
    assert set_zone(client, "Pacific/Port_Moresby", "manual")["label"] == "Port Moresby"


def test_unknown_zone_or_source_is_422(app, client):
    register(client)
    for body in (
        {"timezone": "Mars/Olympus", "source": "manual"}, {"timezone": "", "source": "manual"},
        {"timezone": "Factory", "source": "manual"}, {"timezone": "Asia/Omsk", "source": "telegram"},
        {"timezone": "Asia/Omsk", "source": "other"}, {"timezone": "Asia/Omsk"}, {"source": "manual"},
        {"timezone": "Asia/Omsk", "source": "manual", "extra": 1}, {"timezone": 5, "source": "manual"},
    ):
        assert client.patch(URL, json=body).status_code == 422, body
    assert client.get(URL).json()["timezone"] is None


def test_auth_me_returns_the_zone_and_sessions_are_isolated(app, client):
    register(client)
    assert client.get("/api/v1/auth/me").json()["timezone"] is None
    set_zone(client)
    assert client.get("/api/v1/auth/me").json()["timezone"] == "Asia/Novosibirsk"
    with TestClient(app) as other:
        register(other, "second")
        assert other.get("/api/v1/auth/me").json()["timezone"] is None
        assert other.get(URL).json() == {"timezone": None, "source": None, "label": None}
        set_zone(other, "Europe/Berlin")
    assert client.get(URL).json()["timezone"] == "Asia/Novosibirsk"


# Пояс аккаунта на сервере

def test_ai_context_gets_the_account_zone_per_job(reminders):
    app, client, ids = reminders
    seen = []

    class Recording(MockProvider):
        def structure(self, text, *, categories=(), timezone=None):
            seen.append(timezone)
            return super().structure(text, categories=categories)

    message(client, update_id=400, text="Заметка про звонок")
    assert Worker(app.state.sessions, app.state.settings, provider=Recording()).run_once()
    set_zone(client)
    message(client, update_id=401, text="Ещё одна заметка")
    assert Worker(app.state.sessions, app.state.settings, provider=Recording()).run_once()
    assert seen == ["Europe/Moscow", "Asia/Novosibirsk"]


def test_a_provider_without_the_zone_argument_still_works(reminders):
    app, client, ids = reminders
    set_zone(client)
    message(client, update_id=402, text=ASK)
    run_worker(app, Tasking())  # Its structure() has no timezone argument.
    assert len(auto_rows(app)) == 1


def test_cloudru_provider_puts_the_job_zone_in_the_context():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(handler))
    provider.structure("Мысль")
    provider.structure("Мысль", timezone="Asia/Novosibirsk")
    contexts = [json.loads(body["messages"][1]["content"]) for body in bodies]
    assert [c["timezone"] for c in contexts] == ["Europe/Moscow", "Asia/Novosibirsk"]
    now = datetime(2026, 10, 14, 20, 30, tzinfo=UTC)  # Already 15 October in Novosibirsk.
    assert json.loads(structure_context([], "Asia/Novosibirsk", now))["today"] == "2026-10-15"
    assert json.loads(structure_context([], "Europe/Moscow", now))["today"] == "2026-10-14"


def test_tomorrow_at_ten_is_ten_in_the_account_zone(reminders):
    app, client, ids = reminders
    set_zone(client)
    message(client, update_id=410, text=ASK)
    run_worker(app, Silent())
    (row,) = auto_rows(app)
    assert row.timezone == "Asia/Novosibirsk" and row.zone_guessed is False
    assert datetime.fromtimestamp(row.scheduled_at, NOVOSIBIRSK) == tomorrow_at(NOVOSIBIRSK, 10)
    text = claim(client).json()["items"][0]["text"]
    assert "завтра в 10:00, «Завтра в 10 позвонить маме»" in text and "по Москве" not in text


def test_model_deadline_is_read_in_the_account_zone(reminders):
    app, client, ids = reminders
    set_zone(client)
    message(client, update_id=411, text=ASK)
    run_worker(app, Tasking())
    (row,) = auto_rows(app)
    assert row.timezone == "Asia/Novosibirsk"
    assert datetime.fromtimestamp(row.scheduled_at, NOVOSIBIRSK) == tomorrow_at(NOVOSIBIRSK, 10)


def test_without_a_zone_the_service_zone_is_used_and_the_reminder_is_marked_and_named(reminders):
    app, client, ids = reminders
    message(client, update_id=412, text=ASK)
    run_worker(app, Silent())
    (row,) = auto_rows(app)
    assert row.timezone == "Europe/Moscow" and row.zone_guessed is True
    assert datetime.fromtimestamp(row.scheduled_at, MOSCOW) == tomorrow_at(MOSCOW, 10)
    assert "завтра в 10:00 по Москве, «" in claim(client).json()["items"][0]["text"]


def test_explicit_web_reminders_are_not_guessed(reminders):
    app, client, ids = reminders
    created = client.post(
        "/api/v1/notes/" + ids["note"] + "/reminders", json=payload(ids["item"]), headers={"Idempotency-Key": "k-explicit"},
    )
    assert created.status_code == 201, created.text
    with app.state.sessions() as db:
        assert db.get(Reminder, created.json()["id"]).zone_guessed is False


def test_default_delay_is_counted_in_the_account_zone():
    settings = Settings()
    now = datetime(2026, 10, 14, 5, 0, tzinfo=UTC)  # 08:00 in Moscow, 12:00 in Novosibirsk
    moscow = default_reminder_time(now, settings)
    novosibirsk = default_reminder_time(now, settings, "Asia/Novosibirsk")
    assert moscow.hour == 9 and moscow.tzinfo.key == "Europe/Moscow"
    assert novosibirsk.hour == 13 and novosibirsk.tzinfo.key == "Asia/Novosibirsk"
    late = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)  # 22:30 in Novosibirsk, so 09:00 the next morning there
    assert default_reminder_time(late, settings, "Asia/Novosibirsk").day == 15
    assert default_reminder_time(late, settings, "Asia/Novosibirsk").hour == 9


def test_request_without_time_gets_the_default_time_in_the_account_zone(reminders):
    app, client, ids = reminders
    set_zone(client)
    message(client, update_id=413, text="Напомни позвонить маме")
    run_worker(app, Silent())
    (row,) = auto_rows(app)
    assert row.timezone == "Asia/Novosibirsk"
    local = datetime.fromtimestamp(row.scheduled_at, NOVOSIBIRSK)
    assert local.hour in {9, *range(9, 22)} and row.scheduled_at > time.time()


def test_proposals_use_the_account_zone(reminders):
    app, client, ids = reminders
    now = datetime(2026, 10, 14, 5, 0, tzinfo=UTC)
    items = [{"id": ids["item"], "kind": "task", "status": "open", "due_text": "завтра в 10"}]
    with app.state.sessions() as db:
        before = reminder_proposals(db, ids["user"], items, app.state.settings, now)[ids["item"]]
    assert before["timezone"] == "Europe/Moscow" and before["local_time"] == "2026-10-15T10:00:00"
    set_zone(client)
    with app.state.sessions() as db:
        after = reminder_proposals(db, ids["user"], items, app.state.settings, now)[ids["item"]]
    assert after["timezone"] == "Asia/Novosibirsk" and after["local_time"] == "2026-10-15T10:00:00"


def test_dashboard_today_and_zone_follow_the_account(reminders):
    app, client, ids = reminders
    assert client.get("/api/v1/dashboard?days=7").json()["timezone"] == "Europe/Moscow"
    set_zone(client, "Pacific/Kiritimati")
    body = client.get("/api/v1/dashboard?days=7").json()
    assert body["timezone"] == "Pacific/Kiritimati"
    assert body["period"]["to"] == datetime.now(ZoneInfo("Pacific/Kiritimati")).date().isoformat()
    with app.state.sessions() as db:
        assert build_dashboard(db, app.state.settings, ids["user"], 7)["timezone"] == "Pacific/Kiritimati"


def test_limits_and_budget_stay_on_the_service_zone(reminders):
    app, client, ids = reminders
    settings = app.state.settings
    now = datetime(2026, 10, 14, 20, 30, tzinfo=UTC).timestamp()  # 23:30 in Moscow, next day in Kiritimati
    with app.state.sessions() as db:
        before = daily_state(db, settings, ids["user"], now)
    set_zone(client, "Pacific/Kiritimati")
    with app.state.sessions() as db:
        after = daily_state(db, settings, ids["user"], now)
    assert before["limit_resets_at"] == after["limit_resets_at"]
    assert settings.limit_timezone == "Europe/Moscow"
    reset = datetime.fromisoformat(after["limit_resets_at"]).astimezone(MOSCOW)
    assert (reset.hour, reset.minute) == (0, 0)


# Догаданные напоминания

def guessed_reminder(app, ids, *, hours=26, zone="Europe/Moscow", guessed=True, key="g1"):
    with app.state.sessions() as db:
        row = Reminder(
            user_id=ids["user"], note_id=ids["note"], item_id=None, scheduled_at=time.time() + hours * 3600,
            timezone=zone, text="Позвонить", idempotency_key=key, creation_hash="x", zone_guessed=guessed,
        )
        db.add(row)
        db.commit()
        return row.id


def test_first_zone_moves_guessed_reminders_to_the_same_wall_clock(reminders):
    app, client, ids = reminders
    rid = guessed_reminder(app, ids)
    with app.state.sessions() as db:
        wall = datetime.fromtimestamp(db.get(Reminder, rid).scheduled_at, MOSCOW).replace(tzinfo=None)
    set_zone(client, "Asia/Novosibirsk", "auto")
    with app.state.sessions() as db:
        row = db.get(Reminder, rid)
        assert row.timezone == "Asia/Novosibirsk" and row.zone_guessed is False
        assert datetime.fromtimestamp(row.scheduled_at, NOVOSIBIRSK).replace(tzinfo=None) == wall
        assert row.generation == 2 and row.status == "confirmed"
        assert row.scheduled_at > time.time()


def test_explicit_reminders_stay_where_they_are(reminders):
    app, client, ids = reminders
    rid = guessed_reminder(app, ids, guessed=False, key="e1")
    with app.state.sessions() as db:
        before = db.get(Reminder, rid).scheduled_at
    set_zone(client)
    with app.state.sessions() as db:
        row = db.get(Reminder, rid)
        assert (row.scheduled_at, row.timezone, row.generation) == (before, "Europe/Moscow", 1)


def test_a_guessed_reminder_that_would_land_in_the_past_is_not_moved(reminders):
    app, client, ids = reminders
    rid = guessed_reminder(app, ids, hours=1)
    with app.state.sessions() as db:
        before = db.get(Reminder, rid).scheduled_at
    set_zone(client, "Asia/Vladivostok")  # Seven hours ahead of Moscow: the same wall clock is already gone.
    with app.state.sessions() as db:
        row = db.get(Reminder, rid)
        assert row.scheduled_at == before and row.timezone == "Europe/Moscow"
        assert row.zone_guessed is False and row.generation == 1


def test_auto_never_touches_reminders_once_the_account_has_a_zone(reminders):
    app, client, ids = reminders
    set_zone(client, "Asia/Omsk")
    rid = guessed_reminder(app, ids, key="late")
    with app.state.sessions() as db:
        before = db.get(Reminder, rid).scheduled_at
    set_zone(client, "Asia/Novosibirsk", "auto")
    with app.state.sessions() as db:
        row = db.get(Reminder, rid)
        assert (row.scheduled_at, row.timezone, row.generation) == (before, "Europe/Moscow", 1)


def test_other_accounts_are_untouched(reminders):
    app, client, ids = reminders
    mine = guessed_reminder(app, ids, key="mine")
    with TestClient(app) as other:
        register(other, "second")
    with app.state.sessions() as db:
        second = db.scalar(select(User).where(User.username == "second"))
        theirs = Reminder(
            user_id=second.id, note_id=ids["note"], scheduled_at=time.time() + 90000, timezone="Europe/Moscow",
            text="Чужое", idempotency_key="theirs", creation_hash="y", zone_guessed=True,
        )
        db.add(theirs)
        db.commit()
        their_id, their_at = theirs.id, theirs.scheduled_at
    set_zone(client)
    with app.state.sessions() as db:
        assert db.get(Reminder, mine).timezone == "Asia/Novosibirsk"
        row = db.get(Reminder, their_id)
        assert (row.timezone, row.scheduled_at, row.zone_guessed) == ("Europe/Moscow", their_at, True)


# Маршруты для бота

def zone_state(client, **params):
    return client.get("/internal/v1/telegram/timezone", headers=HEADERS, params={
        "bot_id": BASE["bot_id"], "telegram_user_id": BASE["telegram_user_id"], **params,
    })


def internal(client, path, update_id, **extra):
    return client.post("/internal/v1/telegram/" + path, headers=HEADERS, json={**BASE, "update_id": update_id, **extra})


def test_bot_state_is_for_a_linked_user_only(reminders):
    app, client, ids = reminders
    reset_asked(app, ids)
    state = zone_state(client)
    assert state.status_code == 200
    assert state.json() == {"timezone": None, "source": None, "label": None, "utc": None, "asked": False}
    assert zone_state(client, telegram_user_id=777).status_code == 404
    assert client.get("/internal/v1/telegram/timezone", params={**BASE}).status_code == 401


def test_bot_marks_asked_once_and_replays_safely(reminders):
    app, client, ids = reminders
    reset_asked(app, ids)
    first = internal(client, "timezone-asked", 500)
    assert first.json() == {"first": True}
    assert internal(client, "timezone-asked", 500).json() == {"first": True}  # The same update repeats its answer.
    assert internal(client, "timezone-asked", 501).json() == {"first": False}
    assert zone_state(client).json()["asked"] is True
    with app.state.sessions() as db:
        assert db.get(User, ids["user"]).timezone_asked_at is not None


def test_bot_sets_the_zone_with_source_telegram_and_moves_guessed_reminders(reminders):
    app, client, ids = reminders
    guessed_reminder(app, ids, key="a")
    guessed_reminder(app, ids, hours=30, key="b")
    response = internal(client, "timezone", 510, timezone="Asia/Novosibirsk")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "timezone": "Asia/Novosibirsk", "source": "telegram", "label": "Новосибирск", "utc": "UTC+7",
        "asked": True, "moved": 2,
    }
    assert client.get(URL).json() == {"timezone": "Asia/Novosibirsk", "source": "telegram", "label": "Новосибирск"}
    assert internal(client, "timezone", 510, timezone="Asia/Novosibirsk").json()["moved"] == 2  # Replay.
    assert internal(client, "timezone", 511, timezone="Asia/Omsk").json()["moved"] == 0


def test_bot_refuses_an_unknown_zone_and_an_unlinked_user(reminders):
    app, client, ids = reminders
    bad = internal(client, "timezone", 520, timezone="Mars/Olympus")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_timezone"
    other = client.post("/internal/v1/telegram/timezone", headers=HEADERS, json={
        **BASE, "telegram_user_id": 777, "chat_id": 777, "update_id": 521, "timezone": "Asia/Omsk",
    })
    assert other.status_code == 403 and other.json()["error"]["code"] == "telegram_not_linked"
    assert client.get(URL).json()["timezone"] is None


def test_bot_find_by_text_and_by_place(reminders):
    app, client, ids = reminders
    found = internal_find(client, text="Новосибирск")
    assert found == {"found": True, "timezone": "Asia/Novosibirsk", "label": "Новосибирск", "city": None, "utc": "UTC+7"}
    assert internal_find(client, text="+5")["timezone"] == "Asia/Yekaterinburg"
    assert internal_find(client, text="Нигде-на-свете") == {"found": False}
    place = internal_find(client, latitude=55.03, longitude=82.92)
    assert place["timezone"] == "Asia/Novosibirsk" and place["city"] == "Новосибирск"
    assert internal_find(client, latitude=43.24, longitude=76.89)["timezone"] == "Asia/Almaty"
    assert internal_find(client, latitude=0.0, longitude=-150.0) == {"found": False}
    assert client.post("/internal/v1/telegram/timezone-find", headers=HEADERS, json={
        **BASE, "latitude": 95, "longitude": 0,
    }).status_code == 422


def internal_find(client, **what):
    response = client.post("/internal/v1/telegram/timezone-find", headers=HEADERS, json={**BASE, **what})
    assert response.status_code == 200, response.text
    return response.json()


def test_find_requires_a_linked_user(reminders):
    app, client, ids = reminders
    response = client.post("/internal/v1/telegram/timezone-find", headers=HEADERS, json={
        **BASE, "telegram_user_id": 777, "chat_id": 777, "text": "Омск",
    })
    assert response.status_code == 403 and response.json()["error"]["code"] == "telegram_not_linked"


def test_coordinates_are_not_logged_or_stored(reminders, caplog):
    app, client, ids = reminders
    caplog.set_level(logging.DEBUG)
    with app.state.sessions() as db:
        inbox_before = db.scalar(select(func.count()).select_from(Inbox))
    internal_find(client, latitude=55.030199, longitude=82.920430)
    assert "55.030199" not in caplog.text and "82.920430" not in caplog.text
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Inbox)) == inbox_before
        stored = json.dumps([row.response for row in db.scalars(select(Inbox))], default=str)
        assert "55.0301" not in stored and "82.9204" not in stored


# Вопрос бота приходит в ответе на запись

def reset_asked(app, ids):
    with app.state.sessions() as db:
        db.get(User, ids["user"]).timezone_asked_at = None
        db.commit()


def test_processed_note_carries_the_question_once(reminders):
    app, client, ids = reminders
    assert message(client, update_id=600, text="Первая").json()["ask_timezone"] is False  # The fixture already asked.
    reset_asked(app, ids)
    first = message(client, update_id=601, text="Вторая").json()
    assert first["ask_timezone"] is True
    assert message(client, update_id=601, text="Вторая").json() == first  # A replay repeats the answer.
    assert message(client, update_id=602, text="Третья").json()["ask_timezone"] is False
    assert zone_state(client).json()["asked"] is True


def test_no_question_when_the_account_has_a_zone(reminders):
    app, client, ids = reminders
    reset_asked(app, ids)
    set_zone(client, "Asia/Omsk", "auto")
    assert message(client, update_id=610, text="Заметка").json()["ask_timezone"] is False


def test_voice_message_carries_the_question_too(app_factory, tmp_path):
    from app.audio_storage import AudioStorage
    from tests.test_audio_routes import telegram
    from tests.test_internal import SERVICE_TOKEN, linked

    app = app_factory(internal_api_token=SERVICE_TOKEN, audio_storage=AudioStorage(tmp_path / "audio"))
    with TestClient(app) as client:
        linked(client)
        first = telegram(client, update=2)
        assert first.status_code == 200, first.text
        assert first.json()["ask_timezone"] is True
        assert telegram(client, update=3).json()["ask_timezone"] is False
