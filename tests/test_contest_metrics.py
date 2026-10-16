"""Observer boundaries and verified activity. Only synthetic accounts and mock providers."""

import re
import time
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import totp
from app.admin_auth import COOKIE_NAME
from app.admin_models import AdminAccount
from app.contest_metrics import first_targets, report, target_completions
from app.models import ComponentCall, ProductEvent, User, new_id
from app.policy import POLICY_VERSION
from tests.conftest import ADMIN_ADDRESS, ADMIN_PASSWORD, admin_login, make_admin, register

KEY = "synthetic-review-analytics-key-1234567890"


def make_observer(app, *, expires=None):
    secret = make_admin(app, username="observer")
    with app.state.sessions() as db:
        account = db.scalar(select(AdminAccount).where(AdminAccount.username == "observer"))
        account.role, account.access_expires_at = "observer", expires or time.time() + 3600
        db.commit()
    return secret


def reviewer_login(client, secret):
    return client.post(
        "/review-api/v1/login",
        headers={"Origin": "https://testserver"},
        json={
            "username": "observer",
            "password": ADMIN_PASSWORD,
            "totp": totp.code_at(secret, time.time()),
        },
    )


def test_observer_is_https_only_read_only_and_separate_from_owner(app_factory):
    app = app_factory(review_enabled=True, analytics_pseudonym_key=KEY, public_origin="https://testserver")
    secret = make_observer(app)
    owner = make_admin(app)
    with TestClient(app, base_url="https://testserver", client=("198.51.100.7", 50000)) as client:
        assert client.get("/review").status_code == 200
        assert client.get("/review-api/v1/summary?from=2026-10-01&to=2026-10-02").status_code == 401
        assert client.get("/admin").status_code == 404
        assert client.get("/review/assets/metrics.js").status_code == 200
        assert client.get("/review/assets/../../secrets/data_key.txt").status_code == 404
        assert (
            client.post(
                "/review-api/v1/login",
                headers={"Origin": "https://testserver"},
                json={
                    "username": "owner",
                    "password": ADMIN_PASSWORD,
                    "totp": totp.code_at(owner, time.time()),
                },
            ).status_code
            == 401
        )
        assert reviewer_login(client, secret).status_code == 200
        cookie = client.cookies.get("beresta_review")
        response = client.get("/review-api/v1/summary?from=2026-10-01&to=2026-10-02")
        assert response.status_code == 200
        assert response.json()["budget"] is None
        assert response.json()["contest"]["method_version"] == "saved-once-v3"
        assert client.get("/review-api/v1/feedback").status_code == 404
        assert client.get("/review-api/v1/audit").status_code == 404
        assert client.patch("/review-api/v1/feedback/fake", json={"status": "resolved"}).status_code == 404
        assert client.get("/review-api/v1/contest").status_code == 200
    with TestClient(app, client=ADMIN_ADDRESS) as private:
        private.cookies.set(COOKIE_NAME, cookie)
        assert private.get("/admin-api/v1/summary?from=2026-10-01&to=2026-10-02").status_code == 401
    with TestClient(app, base_url="http://testserver") as plain:
        assert plain.get("/review").status_code == 404


def test_observer_expiry_and_wrong_origin(app_factory):
    app = app_factory(review_enabled=True, analytics_pseudonym_key=KEY, public_origin="https://testserver")
    secret = make_observer(app)
    with TestClient(app, base_url="https://testserver") as client:
        assert (
            client.post(
                "/review-api/v1/login",
                json={
                    "username": "observer",
                    "password": ADMIN_PASSWORD,
                    "totp": totp.code_at(secret, time.time()),
                },
            ).status_code
            == 403
        )
        assert reviewer_login(client, secret).status_code == 200
        with app.state.sessions() as db:
            account = db.scalar(select(AdminAccount).where(AdminAccount.username == "observer"))
            account.access_expires_at = time.time() - 1
            db.commit()
        assert client.get("/review-api/v1/me").status_code == 401


def test_owner_access_stays_private_and_working(app_factory):
    app = app_factory(review_enabled=True, analytics_pseudonym_key=KEY, public_origin="https://testserver")
    secret = make_admin(app)
    with TestClient(app, client=ADMIN_ADDRESS) as client:
        assert admin_login(app, client, secret).status_code == 200
        assert client.get("/admin-api/v1/summary?from=2026-10-01&to=2026-10-02").status_code == 200


def test_campaign_tag_is_signed_and_saved_only_after_registration(app_factory):
    app = app_factory(analytics_pseudonym_key=KEY)
    with TestClient(app) as client:
        response = client.get("/?utm_source=sber_chat")
        token = re.search('data-source-token="([^"]+)"', response.text).group(1)
        assert not response.headers.get("set-cookie")
        response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "campaign_user",
                "password": "synthetic-password-123",
                "accept_policy": True,
                "policy_version": POLICY_VERSION,
                "confirm_age": True,
                "source_token": token,
            },
        )
        assert response.status_code == 201
        with app.state.sessions() as db:
            assert db.get(User, response.json()["id"]).source == "sber_chat"
    with TestClient(app) as forged:
        response = forged.post(
            "/api/v1/auth/register",
            json={
                "username": "forged_source",
                "password": "synthetic-password-123",
                "accept_policy": True,
                "policy_version": POLICY_VERSION,
                "confirm_age": True,
                "source_token": token[:-1] + "x",
            },
        )
        with app.state.sessions() as db:
            assert db.get(User, response.json()["id"]).source == "unknown"


def test_saved_and_viewed_is_enough_and_get_alone_never_completes(app, client):
    user = register(client)
    saved = client.post(
        "/api/v1/captures/text",
        headers={"Idempotency-Key": "target-test"},
        json={"text": "Синтетическая заметка", "processing_mode": "manual"},
    ).json()
    note = saved["note_id"]
    assert client.get("/api/v1/notes/" + note).status_code == 200
    with app.state.sessions() as db:
        assert user["id"] in first_targets(db, time.time() + 1)
        assert user["id"] not in target_completions(db, time.time() + 1)
    one, two = new_id(), new_id()
    for operation in (one, one, two):
        assert (
            client.post("/api/v1/notes/" + note + "/opened", json={"operation_id": operation}).status_code
            == 204
        )
    with app.state.sessions() as db:
        assert user["id"] in first_targets(db, time.time() + 1)
        assert user["id"] in target_completions(db, time.time() + 1)
        assert len(db.scalars(select(ComponentCall).where(ComponentCall.component == "note_view")).all()) == 1


def test_assistant_submission_is_activity_and_replay_does_not_duplicate(app, client):
    register(client)
    for _ in range(2):
        assert (
            client.post(
                "/api/v1/assistant/ask",
                headers={"Idempotency-Key": "assistant-event"},
                json={"question": "Есть ли задачи?"},
            ).status_code
            == 202
        )
    with app.state.sessions() as db:
        assert (
            len(db.scalars(select(ProductEvent).where(ProductEvent.name == "assistant_requested")).all()) == 1
        )


def test_fixed_period_includes_zero_days_and_is_preliminary(app):
    with app.state.sessions() as db:
        result = report(
            db,
            "2026-11-03",
            "2026-11-30",
            now=datetime.fromisoformat("2026-10-12T12:00:00+03:00").timestamp(),
        )
    assert result["days"] == 28 and result["completed_days"] == 0 and result["preliminary"]
    assert result["average_dau"] == result["average_calls_per_dau"] == 0
    assert len(result["daily"]) == 28
