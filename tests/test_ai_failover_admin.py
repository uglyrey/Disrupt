"""Резервный ИИ в админке, в миграции и под PostgreSQL. Данные выдуманные, живые сервисы не вызываются."""

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.admin_metrics import calendar_day
from app.db import make_database
from app.fallback_budget import FallbackBudget, day_key, to_micro
from app.models import FallbackSpend, ProviderRoute, ProviderUsage, User, new_id
from app.provider_router import PRIMARY, ProviderRouter
from tests.conftest import register
from tests.test_ai_failover import FALLBACK_SETTINGS


def usage(user_id, provider, at, *, kind="llm", tokens=(1000, 500), cost=None, is_test=False):
    key = new_id()
    return ProviderUsage(
        id=key, user_id=user_id, operation_id=key, request_id=key + ":llm", channel="web", kind=kind,
        model="gpt-4o" if provider == "openai" else "synthetic", provider=provider, status="succeeded",
        occurred_at=at, input_tokens=tokens[0], output_tokens=tokens[1],
        stt_seconds=30.0 if kind == "stt" else None, estimated_cost=cost, is_test=is_test,
    )


def seed(app, user_id):
    now = time.time()
    yesterday = now - 86400
    with app.state.sessions() as db:
        db.add_all([
            usage(user_id, "openai", now - 60, cost=Decimal("0.0075")),
            usage(user_id, "openai", now - 30, cost=Decimal("0.0075")),
            usage(user_id, "openai", now - 20, kind="stt", tokens=(None, None), cost=Decimal("0.003")),
            usage(user_id, "openai", yesterday, cost=Decimal("0.0100")),
            usage(user_id, "cloudru", now - 10),
            usage(user_id, None, now - 5),  # Запись до резерва считается основным шлюзом.
            usage(user_id, "openai", now - 5, cost=Decimal("5"), is_test=True),
        ])
        db.add(ProviderRoute(id=PRIMARY, state="down", failures=2, changed_at=now - 600, next_probe_at=now + 120))
        db.add(FallbackSpend(day=day_key(app.state.settings, now), committed_micro_usd=1_250_000, updated_at=now))
        db.commit()


def today_and_yesterday():
    today = calendar_day(time.time())
    return (today - timedelta(days=1)).isoformat(), today.isoformat()


def test_admin_sees_state_calls_tokens_and_estimated_dollars(app, client, admin_client):
    user = register(client)
    seed(app, user["id"])
    start, end = today_and_yesterday()
    response = admin_client.get("/admin-api/v1/ai-providers", params={"from": start, "to": end})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["primary_state"] == "down" and data["fallback_enabled"] is False
    assert data["primary_changed_at"] and data["next_probe_at"]
    assert data["fallback_spent_today_usd"] == "1.250000" and data["fallback_cap_usd"] == "10.000000"
    today = {row["provider"]: row for row in data["today"]}
    period = {row["provider"]: row for row in data["period"]}
    assert set(today) == {"openai", "cloudru"} and set(period) == {"openai", "cloudru"}
    assert today["openai"] == {
        "provider": "openai", "calls": 3, "input_tokens": 2000, "output_tokens": 1000, "stt_seconds": 30.0,
        "estimated_usd": "0.018000",
    }
    assert today["cloudru"]["calls"] == 2 and today["cloudru"]["estimated_usd"] == "0.000000"
    assert period["openai"]["calls"] == 4 and period["openai"]["estimated_usd"] == "0.028000"
    assert period["openai"]["input_tokens"] == 3000  # Строка тестового аккаунта не считается.


def test_admin_providers_view_without_activity_is_empty_and_up(admin_client):
    start, end = today_and_yesterday()
    data = admin_client.get("/admin-api/v1/ai-providers", params={"from": start, "to": end}).json()
    assert data["primary_state"] == "up" and data["today"] == [] and data["period"] == []
    assert data["fallback_spent_today_usd"] == "0.000000" and data["next_probe_at"] is None
    tomorrow = (calendar_day(time.time()) + timedelta(days=1)).isoformat()
    assert admin_client.get("/admin-api/v1/ai-providers", params={"from": start, "to": tomorrow}).status_code == 422


def test_fallback_calls_stay_out_of_the_ruble_accounting(app, client, admin_client):
    user = register(client)
    seed(app, user["id"])
    start, end = today_and_yesterday()
    summary = admin_client.get("/admin-api/v1/summary", params={"from": start, "to": end}).json()
    costs = summary["quality"]["costs"]
    assert costs["llm_calls"] == 2 and costs["stt_calls"] == 0  # Только cloudru и запись без сервиса.
    assert all(row["model"] != "gpt-4o" for row in summary["usage"])


def test_the_view_reflects_the_switch_and_a_new_day_starts_at_zero(app_factory):
    from fastapi.testclient import TestClient

    from tests.conftest import ADMIN_ADDRESS, admin_login, make_admin

    app = app_factory(**FALLBACK_SETTINGS)
    secret = make_admin(app)
    with TestClient(app, client=ADMIN_ADDRESS) as admin:
        assert admin_login(app, admin, secret).status_code == 200
        start, end = today_and_yesterday()
        assert admin.get("/admin-api/v1/ai-providers", params={"from": start, "to": end}).json()["fallback_enabled"]
        budget = FallbackBudget(app.state.sessions, app.state.settings)
        yesterday = time.time() - 86400
        assert budget.reserve(Decimal("2"), yesterday) is not None
        data = admin.get("/admin-api/v1/ai-providers", params={"from": start, "to": end}).json()
        assert data["fallback_spent_today_usd"] == "0.000000"


# Миграция

def test_migration_marks_old_receipts_and_round_trips(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "failover.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "a9d4e2f7c1b8")
    engine, sessions = make_database(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users (id, username, password_hash, live_calls) VALUES ('u','legacy','hash',0)"))
        for key, model in (("r1", "mock"), ("r2", "deepseek-v4-flash"), ("r3", "injected")):
            db.execute(text(
                "INSERT INTO provider_usage (id, user_id, operation_id, request_id, channel, kind, model, status, "
                "occurred_at) VALUES (:k, 'u', :k, :k, 'web', 'llm', :m, 'succeeded', 1)"), {"k": key, "m": model})
    command.upgrade(config, "head")
    command.check(config)
    with sessions() as db:
        rows = {row.id: row.provider for row in db.scalars(select(ProviderUsage))}
        assert rows == {"r1": "mock", "r2": "cloudru", "r3": "cloudru"}
        assert db.scalars(select(ProviderRoute)).all() == [] and db.scalars(select(FallbackSpend)).all() == []
        assert db.get(User, "u").live_calls == 0
        db.add(ProviderRoute(id=PRIMARY))
        db.add(FallbackSpend(day="2026-10-12", updated_at=1.0))
        db.commit()
        assert db.get(ProviderRoute, PRIMARY).state == "up" and db.get(FallbackSpend, "2026-10-12").committed_micro_usd == 0
    command.downgrade(config, "a9d4e2f7c1b8")
    with engine.connect() as db:
        assert db.execute(text("SELECT COUNT(*) FROM provider_usage")).scalar() == 3
        tables = set(db.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars())
        assert "provider_routes" not in tables and "fallback_spend" not in tables
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()


# PostgreSQL: одновременные воркеры

@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Local PostgreSQL is not configured")
def test_postgresql_concurrent_reservations_and_probe_claims(monkeypatch):
    url = os.environ["TEST_POSTGRES_URL"]
    assert make_url(url).host in {"127.0.0.1", "localhost", "::1"}
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    monkeypatch.setenv("NOTES_ALLOW_LIVE_REQUESTS", "false")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    from app.config import Settings

    settings = replace(
        Settings(database_url=url, auto_worker=False), **FALLBACK_SETTINGS, fallback_daily_usd=Decimal("0.10"),
    )
    engine, sessions = make_database(url)
    try:
        # Выдуманные сутки далеко в будущем, чтобы не мешать другим запускам на той же базе.
        day = 4_000_000_000 + (uuid.uuid4().int % 10_000) * 86400
        budget = FallbackBudget(sessions, settings)
        with ThreadPoolExecutor(max_workers=12) as pool:
            taken = list(pool.map(lambda _: budget.reserve(Decimal("0.03"), day), range(24)))
        assert sum(item is not None for item in taken) == 3
        with sessions() as db:
            assert budget.spent(db, day) == to_micro("0.09")

        with sessions() as db:
            row = db.get(ProviderRoute, PRIMARY)
            if row is None:
                db.add(ProviderRoute(id=PRIMARY))
                db.commit()
            row = db.get(ProviderRoute, PRIMARY)
            row.state, row.next_probe_at, row.probe_until = "down", time.time() - 1, None
            db.commit()
        routers = [ProviderRouter(sessions, settings) for _ in range(12)]
        with ThreadPoolExecutor(max_workers=12) as pool:
            answers = list(pool.map(lambda router: router.choose(), routers))
        assert answers.count((PRIMARY, True)) == 1 and len(answers) == 12
    finally:
        with sessions() as db:
            row = db.get(ProviderRoute, PRIMARY)
            if row is not None:
                row.state, row.failures, row.next_probe_at, row.probe_until = "up", 0, None, None
                db.commit()
        engine.dispose()
