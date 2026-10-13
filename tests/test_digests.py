import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.digests import enqueue, slot_timestamp
from app.models import (
    Item,
    Note,
    Outbox,
    ProductEvent,
    ProviderUsage,
    TelegramDigestSettings,
    TelegramIdentity,
    User,
)
from tests.conftest import register
from tests.test_reminders import auth, claim, result
from tests.test_reminders import reminders as reminders

PATH = "/api/v1/telegram/digest-settings"
BODY = {"morning_enabled": True, "evening_enabled": True, "morning_time": "09:00",
        "evening_time": "20:00", "timezone": "Europe/Moscow"}
NOW = datetime(2026, 10, 14, 9, tzinfo=ZoneInfo("Europe/Moscow")).timestamp()


def prepare(reminders):
    app, client, ids = reminders
    assert client.patch(PATH, json=BODY).status_code == 200
    with app.state.sessions.begin() as db:
        db.get(TelegramDigestSettings, ids["user"]).updated_at = NOW - 86400
        db.get(Note, ids["note"]).created_at = NOW - 86400
        db.get(Item, ids["item"]).due_at = NOW
    return app, client, ids


def test_default_is_off_read_does_not_subscribe(reminders):
    app, client, _ = reminders
    value = client.get(PATH).json()
    assert value["morning_enabled"] is value["evening_enabled"] is False
    assert value["telegram_available"] is True
    assert enqueue(app.state.sessions, now=NOW) == 0
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(TelegramDigestSettings)) == 0


def test_concurrent_scheduler_and_restart_send_one_per_local_slot(reminders):
    app, _, ids = prepare(reminders)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: enqueue(app.state.sessions, now=NOW), range(4))) == 1
    assert enqueue(app.state.sessions, now=NOW + 30) == 0
    assert enqueue(app.state.sessions, now=NOW + 11 * 3600) == 1
    with app.state.sessions() as db:
        rows = db.scalars(select(Outbox).order_by(Outbox.created_at)).all()
        assert len(rows) == 2 and {row.message_kind for row in rows} == {"digest_morning", "digest_evening"}
        assert all(row.user_id == ids["user"] and "Позвонить" in row.message_text for row in rows)
        assert db.scalar(select(func.count()).select_from(ProviderUsage)) == 0


def test_delivery_authorization_result_and_no_fake_active_event(reminders, monkeypatch):
    app, client, _ = prepare(reminders)
    before = None
    with app.state.sessions() as db:
        before = db.scalar(select(func.count()).select_from(ProductEvent))
    enqueue(app.state.sessions, now=NOW)
    monkeypatch.setattr(time, "time", lambda: NOW + 1)
    delivery = claim(client).json()["items"][0]
    assert "Утренний обзор" in delivery["text"] and delivery["callback_token"] is None
    assert auth(client, delivery).json() == {"send": True}
    assert result(client, delivery).status_code == 200
    assert result(client, delivery).status_code == 200
    assert claim(client).json()["items"] == []
    with app.state.sessions() as db:
        assert db.get(Outbox, delivery["delivery_id"]).message_text is None
        assert db.scalar(select(func.count()).select_from(ProductEvent)) == before


@pytest.mark.parametrize("change", ["disable", "edit_time", "notification_off", "blocked", "unlinked", "delete"])
def test_cancel_or_revoke_after_claim_prevents_send(reminders, monkeypatch, change):
    app, client, ids = prepare(reminders)
    enqueue(app.state.sessions, now=NOW)
    monkeypatch.setattr(time, "time", lambda: NOW + 1)
    delivery = claim(client).json()["items"][0]
    if change in {"disable", "edit_time"}:
        body = {**BODY, "morning_enabled": False} if change == "disable" else {**BODY, "morning_time": "10:00"}
        assert client.patch(PATH, json=body).status_code == 200
    else:
        with app.state.sessions.begin() as db:
            link = db.scalar(select(TelegramIdentity))
            if change == "notification_off":
                link.notifications_enabled = False
            elif change == "blocked":
                link.delivery_status = "blocked"
            elif change == "unlinked":
                db.delete(link)
            else:
                db.get(User, ids["user"]).deletion_requested_at = NOW
    assert auth(client, delivery).json() == {"send": False}
    with app.state.sessions() as db:
        assert db.get(Outbox, delivery["delivery_id"]).message_text is None


def test_late_enable_no_catchup_stale_slot_and_empty_digest(reminders):
    app, _, ids = prepare(reminders)
    with app.state.sessions.begin() as db:
        settings = db.get(TelegramDigestSettings, ids["user"])
        settings.updated_at = NOW + 1
    assert enqueue(app.state.sessions, now=NOW + 2) == 0
    assert enqueue(app.state.sessions, now=NOW + 6000) == 0
    with app.state.sessions.begin() as db:
        db.get(TelegramDigestSettings, ids["user"]).updated_at = NOW - 1
        db.get(Note, ids["note"]).created_at = NOW - 10 * 86400
        db.get(Item, ids["item"]).status = "completed"
    assert enqueue(app.state.sessions, now=NOW) == 1
    with app.state.sessions() as db:
        assert db.scalar(select(Outbox)).status == "cancelled"


def test_settings_owned_csrf_and_requires_available_link(reminders):
    app, client, ids = prepare(reminders)
    with TestClient(app) as other:
        assert other.get(PATH).status_code == 401
        register(other, "another_digest_owner")
        assert other.get(PATH).json()["morning_enabled"] is False
        assert other.patch(PATH, json=BODY).status_code == 409
        assert other.patch(PATH, json={**BODY, "morning_enabled": False, "evening_enabled": False},
                           headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.get(PATH).json()["morning_enabled"] is True
    with app.state.sessions() as db:
        assert db.get(TelegramDigestSettings, ids["user"]).morning_enabled is True


@pytest.mark.parametrize("field,value", [("timezone", "../etc/passwd"), ("timezone", "No/Zone"),
                                         ("morning_time", "24:00"), ("evening_time", "9:00:00")])
def test_invalid_schedule_is_rejected(reminders, field, value):
    _, client, _ = reminders
    assert client.patch(PATH, json={**BODY, field: value}).status_code == 422


def test_dst_fold_and_nonexistent_time_are_deterministic():
    zone = ZoneInfo("Europe/Berlin")
    overlap = datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=1)
    assert slot_timestamp(overlap, "02:30", zone) == datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0).timestamp()
    gap = datetime(2026, 3, 29, 3, 0, tzinfo=zone)
    assert slot_timestamp(gap, "02:30", zone) is None
