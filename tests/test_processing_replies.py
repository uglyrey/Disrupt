import asyncio
import json
import os
import time

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.models import Job, Note, Outbox, ProductEvent, TelegramIdentity, User
from app.providers import MockProvider, ProviderError
from app.worker import Worker
from telegram_adapter.delivery import DeliverySender
from tests.telegram_adapter.test_delivery import clients, journal_for, settings_for, success
from tests.test_internal import BASE, message
from tests.test_reminders import auth, claim, due_delivery, result
from tests.test_reminders import reminders as reminders


def processed(app, client, *, update_id=200):
    saved = message(client, update_id=update_id).json()
    worker = Worker(app.state.sessions, app.state.settings, provider=MockProvider())
    assert worker.run_once()
    return saved


def test_reply_is_bound_to_original_chat_and_update_replay_is_safe(reminders):
    app, client, ids = reminders
    saved = message(client, update_id=200).json()
    assert message(client, update_id=200).json() == saved
    assert claim(client).json() == {"items": []}
    with app.state.sessions() as db:
        row = db.scalar(select(Outbox))
        assert row.job_id == saved["job_id"] and row.reminder_id is None
        assert (row.bot_id, row.chat_id) == (BASE["bot_id"], BASE["chat_id"])
        assert db.scalar(select(func.count()).select_from(Outbox)) == 1
    assert Worker(app.state.sessions, app.state.settings, provider=MockProvider()).run_once()
    response = claim(client).json()["items"][0]
    assert response["text"].startswith("Заметка готова.") and response["callback_token"] is None
    assert "?capture=" + saved["capture_id"] in response["note_url"] and "reminder=" not in response["note_url"]
    assert auth(client, response).json() == {"send": True}
    assert result(client, response).status_code == 200
    assert result(client, response).status_code == 200
    assert claim(client).json() == {"items": []}
    with app.state.sessions() as db:
        assert db.scalar(select(Outbox)).status == "sent"
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "processing_reply_sent")) == 1
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "reminder_sent")) == 0


@pytest.mark.parametrize("change", ["unlinked", "disabled", "blocked", "different_chat", "foreign_note", "foreign_job"])
def test_reply_rechecks_owner_and_link_before_authorization(reminders, change):
    app, client, ids = reminders
    saved = processed(app, client)
    delivery = claim(client).json()["items"][0]
    with app.state.sessions.begin() as db:
        identity = db.scalar(select(TelegramIdentity))
        if change == "unlinked":
            db.delete(identity)
        elif change == "disabled":
            identity.notifications_enabled = False
        elif change == "blocked":
            identity.delivery_status = "blocked"
        elif change == "different_chat":
            identity.chat_id += 1
        else:
            other = User(username="different", password_hash="test-only")
            db.add(other)
            db.flush()
            if change == "foreign_note":
                db.scalar(select(Note).where(Note.capture_id == saved["capture_id"])).user_id = other.id
            else:
                db.get(Job, saved["job_id"]).user_id = other.id
    assert auth(client, delivery).json() == {"send": False}


def test_unknown_reply_is_never_sent_again_and_retryable_requires_explicit_rejection(reminders):
    app, client, ids = reminders
    processed(app, client)
    delivery = claim(client).json()["items"][0]
    assert auth(client, delivery).json()["send"]
    assert result(client, delivery, "retryable", retry_after_seconds=0).status_code == 200
    retried = claim(client).json()["items"][0]
    assert retried["delivery_id"] == delivery["delivery_id"] and retried["lease_token"] != delivery["lease_token"]
    assert auth(client, retried).json()["send"]
    assert result(client, retried, "unknown").status_code == 200
    assert claim(client).json() == {"items": []}


def test_expired_authorization_is_unknown_not_resent(reminders):
    app, client, ids = reminders
    processed(app, client)
    delivery = claim(client).json()["items"][0]
    assert auth(client, delivery).json()["send"]
    with app.state.sessions.begin() as db:
        db.get(Outbox, delivery["delivery_id"]).lease_until = time.time() - 1
    assert claim(client).json() == {"items": []}
    with app.state.sessions() as db:
        assert db.get(Outbox, delivery["delivery_id"]).status == "unknown"


def test_processing_failure_cancels_reply_and_tells_the_chat_once(reminders):
    app, client, ids = reminders
    saved = message(client, update_id=200).json()
    class Failed(MockProvider):
        def structure(self, text, *, categories=()):
            raise ProviderError("synthetic_failure")
    assert Worker(app.state.sessions, app.state.settings, provider=Failed()).run_once()
    with app.state.sessions() as db:
        assert db.get(Job, saved["job_id"]).status == "failed"
        rows = db.scalars(select(Outbox).order_by(Outbox.created_at, Outbox.id)).all()
        assert [(row.status, row.message_kind) for row in rows] == [("cancelled", None), ("pending", "processing_failed")]
        assert rows[0].job_id == saved["job_id"] and rows[1].job_id is None and rows[1].reminder_id is None
        assert (rows[1].bot_id, rows[1].chat_id) == (BASE["bot_id"], BASE["chat_id"])
    items = claim(client).json()["items"]
    assert len(items) == 1
    assert items[0]["text"].startswith("Не получилось разобрать запись с ИИ. Исходник сохранён.")
    assert "в истории" in items[0]["text"] and items[0]["callback_token"] is None
    assert auth(client, items[0]).json() == {"send": True}
    assert result(client, items[0]).status_code == 200
    assert claim(client).json() == {"items": []}
    assert not Worker(app.state.sessions, app.state.settings, provider=Failed()).run_once()
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Outbox)) == 2
        sent = db.scalar(select(Outbox).where(Outbox.message_kind == "processing_failed"))
        assert sent.status == "sent" and sent.message_text is None


def test_expired_lease_also_tells_the_chat(reminders):
    app, client, ids = reminders
    saved = message(client, update_id=200).json()
    worker = Worker(app.state.sessions, app.state.settings, provider=MockProvider())
    with app.state.sessions.begin() as db:
        job = db.get(Job, saved["job_id"])
        job.status, job.lease_until = "running", time.time() - 5
    worker.claim()
    with app.state.sessions() as db:
        assert db.get(Job, saved["job_id"]).error_code == "execution_unknown"
        kinds = sorted(str(row.message_kind) for row in db.scalars(select(Outbox)))
        assert kinds == ["None", "processing_failed"]
    assert len(claim(client).json()["items"]) == 1


def test_no_failure_message_when_nobody_waits_in_telegram(app, client):
    from tests.conftest import register
    from tests.test_notes import submit

    register(client)
    job = submit(client, text="Купить хлеб").json()
    class Failed(MockProvider):
        def structure(self, text, *, categories=()):
            raise ProviderError("synthetic_failure")
    assert Worker(app.state.sessions, app.state.settings, provider=Failed()).run_once()
    with app.state.sessions() as db:
        assert db.get(Job, job["id"]).status == "failed"
        assert db.scalar(select(func.count()).select_from(Outbox)) == 0


def test_shared_claim_limit_and_utf16_length(reminders):
    app, client, ids = reminders
    first = processed(app, client, update_id=200)
    processed(app, client, update_id=201)
    with app.state.sessions.begin() as db:
        db.scalar(select(Note).where(Note.capture_id == first["capture_id"])).markdown = "😀" * 6000
    deliveries = claim(client, limit=1).json()["items"]
    assert len(deliveries) == 1
    assert len(deliveries[0]["text"].encode("utf-16-le")) <= 4096 * 2


@pytest.mark.skipif(os.name != "posix", reason="Durable Telegram journal requires POSIX directory fsync")
def test_actual_adapter_sends_text_once_without_task_button(reminders, tmp_path):
    app, client, ids = reminders
    processed(app, client)
    settings, sends = settings_for(tmp_path), []
    def handler(request):
        sends.append(json.loads(request.content))
        return success(request)
    async def scenario():
        core_http, telegram_http, core, telegram = await clients(app, settings, handler)
        async with core_http, telegram_http:
            sender = DeliverySender(settings, core, telegram, journal_for(settings))
            assert await sender.run_once()
            assert not await sender.run_once()
    asyncio.run(scenario())
    assert len(sends) == 1 and sends[0]["text"].startswith("Заметка готова.")
    assert len(sends[0]["reply_markup"]["inline_keyboard"]) == 1
    assert "parse_mode" not in sends[0]


def test_migration_preserves_existing_reminder_delivery(reminders):
    app, client, ids = reminders
    legacy = due_delivery(app, client, ids)
    command.downgrade(Config("alembic.ini"), "d84f210ac937")
    command.upgrade(Config("alembic.ini"), "head")
    with app.state.sessions() as db:
        row = db.get(Outbox, legacy["delivery"])
        assert row.reminder_id == legacy["reminder"] and row.job_id is None and row.status == "pending"
    assert len(claim(client).json()["items"]) == 1
