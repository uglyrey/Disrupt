"""Вход держится 30 дней, продлевается раз в сутки и закрывается на других устройствах."""

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import LoginSession, ProductEvent
from app.security import COOKIE_NAME, hash_token
from tests.conftest import register

DAY = 86400
MONTH = 30 * DAY


def shift_clock(monkeypatch, seconds):
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + seconds)


def stored(app, token):
    with app.state.sessions() as db:
        return db.scalar(select(LoginSession.expires_at).where(LoginSession.token_hash == hash_token(token)))


def set_cookies(response):
    return [value for value in response.headers.get_list("set-cookie") if value.startswith(COOKIE_NAME + "=")]


def max_age(cookie):
    return int(next(part for part in cookie.split("; ") if part.lower().startswith("max-age=")).split("=")[1])


def test_login_cookie_and_row_last_thirty_days(app, client):
    register(client)
    before = time.time()
    response = other_browser(app, response_only=True)
    assert max_age(set_cookies(response)[0]) == MONTH
    token = response.cookies.get(COOKIE_NAME)
    assert abs(stored(app, token) - (before + MONTH)) < 60


def test_old_session_is_renewed_with_the_same_cookie(app, client, monkeypatch):
    register(client)
    token = client.cookies.get(COOKIE_NAME)
    first = stored(app, token)
    shift_clock(monkeypatch, 2 * DAY)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert stored(app, token) > first + DAY
    cookies = set_cookies(response)
    assert len(cookies) == 1
    assert cookies[0].startswith(f"{COOKIE_NAME}={token};")
    assert max_age(cookies[0]) == MONTH
    lowered = cookies[0].lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "path=/" in lowered


def test_renewal_also_works_on_post(app, client, monkeypatch):
    register(client)
    token = client.cookies.get(COOKIE_NAME)
    shift_clock(monkeypatch, 3 * DAY)
    response = client.post("/api/v1/auth/logout-others")
    assert response.status_code == 200
    assert len(set_cookies(response)) == 1
    assert stored(app, token) > time.time() + MONTH - DAY


def test_fresh_session_is_not_rewritten(app, client, monkeypatch):
    register(client)
    token = client.cookies.get(COOKIE_NAME)
    first = stored(app, token)
    shift_clock(monkeypatch, DAY - 600)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert set_cookies(response) == []
    assert stored(app, token) == first


def test_renewal_happens_once_a_day(app, client, monkeypatch):
    register(client)
    token = client.cookies.get(COOKIE_NAME)
    shift_clock(monkeypatch, 2 * DAY)
    assert len(set_cookies(client.get("/api/v1/auth/me"))) == 1
    renewed = stored(app, token)
    again = client.get("/api/v1/auth/me")
    assert set_cookies(again) == [] and stored(app, token) == renewed


def test_expired_session_is_refused_and_not_revived(app, client, monkeypatch):
    register(client)
    token = client.cookies.get(COOKIE_NAME)
    shift_clock(monkeypatch, MONTH + 60)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert set_cookies(response) == []
    assert stored(app, token) < time.time()


def test_logout_does_not_bring_the_cookie_back(app, client, monkeypatch):
    register(client)
    shift_clock(monkeypatch, 2 * DAY)
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    cookies = set_cookies(response)
    assert len(cookies) == 1 and max_age(cookies[0]) == 0
    assert client.get("/api/v1/auth/me").status_code == 401


def test_session_seconds_can_be_set_by_environment(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("NOTES_SESSION_SECONDS", str(7 * DAY))
    assert Settings.from_env().session_seconds == 7 * DAY
    monkeypatch.setenv("NOTES_SESSION_SECONDS", "60")
    with pytest.raises(ValueError, match="NOTES_SESSION_SECONDS"):
        Settings.from_env()


def other_browser(app, username="tester", response_only=False):
    other = TestClient(app)
    response = other.post("/api/v1/auth/login", json={"username": username, "password": "test-only-password-123"})
    assert response.status_code == 200
    return response if response_only else other


def count(app):
    with app.state.sessions() as db:
        return db.scalar(select(func.count()).select_from(LoginSession))


def test_logout_others_closes_the_rest_and_keeps_this_one(app, client):
    register(client)
    second, third = other_browser(app), other_browser(app)
    assert count(app) == 3
    response = client.post("/api/v1/auth/logout-others")
    assert response.status_code == 200 and response.json() == {"closed": 2}
    assert client.get("/api/v1/auth/me").status_code == 200
    assert second.get("/api/v1/auth/me").status_code == 401
    assert third.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/logout-others").json() == {"closed": 0}
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "sessions_closed")) == 2


def test_logout_others_needs_csrf_and_own_origin(app, client):
    register(client)
    second = other_browser(app)
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/v1/auth/logout-others").status_code == 403
    assert client.post("/api/v1/auth/logout-others", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post(
        "/api/v1/auth/logout-others", headers={"X-CSRF-Token": csrf, "Origin": "https://evil.invalid"}
    ).status_code == 403
    assert second.get("/api/v1/auth/me").status_code == 200
    assert TestClient(app).post("/api/v1/auth/logout-others").status_code == 401
    assert client.post("/api/v1/auth/logout-others", headers={"X-CSRF-Token": csrf}).json() == {"closed": 1}


def test_logout_others_leaves_other_accounts_alone(app, client):
    register(client)
    stranger = TestClient(app)
    register(stranger, "stranger")
    other_browser(app)
    assert client.post("/api/v1/auth/logout-others").json() == {"closed": 1}
    assert stranger.get("/api/v1/auth/me").status_code == 200
    assert count(app) == 2
