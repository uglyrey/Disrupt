"""Sign-in by an emailed code, account password change and «Восстановить аккаунт». Mail is always a fake."""

import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.feedback_models import Feedback
from app.mailer import MailError
from app.models import EmailRegistrationPending as Pending
from app.models import LoginSession, ProductEvent, TelegramIdentity, User
from app.policy import POLICY_VERSION
from app.security import UNUSABLE_PASSWORD, has_usable_password, verify_password
from tests.conftest import register
from tests.test_auth_telegram import confirm as tg_confirm
from tests.test_auth_telegram import poll, start, token_of
from tests.test_email_registration import Mail
from tests.test_internal import SERVICE_TOKEN

BASE = "/api/v1/auth/email"
CODE = BASE + "/code"
CONSENT = {"accept_policy": True, "policy_version": POLICY_VERSION, "confirm_age": True}
EVIL = {"Origin": "https://evil.invalid"}


class SupportMail(Mail):
    """The fake mailer that also records Reply-To."""

    def send(self, to, subject, body, *, reply_to=None):
        if self.fail:
            raise MailError("synthetic transport failure")
        self.sent.append((to, subject, body, reply_to))


@pytest.fixture
def stack(app_factory):
    mail = SupportMail()
    app = app_factory(mailer=mail, email_registration_enabled=True)
    with TestClient(app) as client:
        yield app, client, mail


@pytest.fixture
def service_app(app_factory):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    with TestClient(app) as client:
        yield app, client


def ask(client, email="owner@example.test"):
    return client.post(CODE + "/start", json={"email": email})


def last_code(mail):
    return re.search(r"код входа: ([0-9]{6})", mail.sent[-1][2]).group(1)


def enter(client, value, **extra):
    return client.post(CODE + "/confirm", json={"code": value, **extra})


def make_account(app, email="owner@example.test"):
    with app.state.sessions() as db:
        user = User(username="old", password_hash=UNUSABLE_PASSWORD, email=email, email_verified_at=time.time())
        db.add(user)
        db.commit()
        return user.id


def age_pending(app):
    with app.state.sessions() as db:
        db.execute(update(Pending).values(created_at=time.time() - 120))
        db.commit()


def test_existing_account_signs_in_without_consent_and_code_works_once(stack):
    app, client, mail = stack
    user_id = make_account(app)
    started = ask(client, "  Owner@Example.TEST ")
    assert started.status_code == 202 and started.json()["resend_after"] == 60
    assert "httponly" in started.headers["set-cookie"].lower()
    assert mail.sent[-1][0] == "owner@example.test"
    value = last_code(mail)
    assert value not in started.text
    result = enter(client, value)
    assert result.status_code == 200 and result.json()["id"] == user_id
    assert result.json()["has_password"] is False
    assert client.get("/api/v1/auth/me").json()["id"] == user_id
    assert enter(client, value).status_code == 400
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1
        assert db.scalar(select(ProductEvent.outcome).where(ProductEvent.name == "login")) == "email_code"


def test_new_address_needs_consent_then_account_is_created_without_password(stack):
    app, client, mail = stack
    ask(client)
    value = last_code(mail)
    missing = enter(client, value)
    assert missing.status_code == 409 and missing.json()["code"] == "consent_required"
    assert "Чтобы создать аккаунт" in missing.json()["detail"]
    half = enter(client, value, accept_policy=True, policy_version=POLICY_VERSION)
    assert half.status_code == 409 and half.json()["code"] == "consent_required"
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
        assert db.scalars(select(Pending.attempts)).one() == 0  # Нужное согласие не тратит попытки.
    result = enter(client, value, **CONSENT)
    assert result.status_code == 200 and result.json()["has_password"] is False
    with app.state.sessions() as db:
        user = db.get(User, result.json()["id"])
        assert user.email == "owner@example.test" and user.email_verified_at
        assert user.password_hash == UNUSABLE_PASSWORD
        assert user.policy_version == POLICY_VERSION and user.username == "owner"
        assert not verify_password("", user.password_hash) and not verify_password("!", user.password_hash)
        assert db.scalar(select(ProductEvent.outcome).where(ProductEvent.name == "registered")) == "email_code"


def test_new_address_rejects_stale_policy(stack):
    app, client, mail = stack
    ask(client)
    value = last_code(mail)
    stale = enter(client, value, accept_policy=True, policy_version="1999-01-01", confirm_age=True)
    assert stale.status_code == 422
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
    assert enter(client, value, **CONSENT).status_code == 200


def test_wrong_code_attempts_lock_the_code(stack):
    app, client, mail = stack
    make_account(app)
    ask(client)
    good = last_code(mail)
    wrong = "000000" if good != "000000" else "111111"
    for _ in range(5):
        assert enter(client, wrong).status_code == 400
    assert enter(client, good).status_code == 400
    with app.state.sessions() as db:
        row = db.scalars(select(Pending)).one()
        assert row.used_at and not row.code_hash and not row.binding_hash


def test_expired_code_and_missing_or_foreign_binding(stack):
    app, client, mail = stack
    make_account(app)
    ask(client)
    value = last_code(mail)
    with TestClient(app) as other:
        assert enter(other, value).status_code == 400
        other.cookies.set("notes_email_code", "x" * 40)
        assert enter(other, value).status_code == 400
    with app.state.sessions() as db:
        db.execute(update(Pending).values(expires_at=time.time() - 1))
        db.commit()
    assert enter(client, value).status_code == 400
    assert client.get("/api/v1/auth/me").status_code == 401


def test_resend_waits_then_new_code_replaces_old_one(stack):
    app, client, mail = stack
    make_account(app)
    ask(client)
    first = last_code(mail)
    again = ask(client)
    assert again.status_code == 429 and again.headers["retry-after"] == "60"
    age_pending(app)
    assert ask(client).status_code == 202
    second = last_code(mail)
    if first != second:
        assert enter(client, first).status_code == 400
    assert enter(client, second).status_code == 200


def test_throttles_by_network_address(stack):
    app, client, mail = stack
    for index in range(10):
        age_pending(app)
        assert ask(client, f"user{index}@example.test").status_code == 202
    age_pending(app)
    assert ask(client, "user11@example.test").status_code == 429
    assert len(mail.sent) == 10


def test_same_answer_for_known_and_unknown_address(stack):
    app, client, mail = stack
    make_account(app, "known@example.test")
    known = ask(client, "known@example.test")
    with TestClient(app) as other:
        unknown = ask(other, "stranger@example.test")
    assert known.status_code == unknown.status_code == 202
    assert set(known.json()) == set(unknown.json())
    assert mail.sent[0][1] == mail.sent[1][1]
    assert re.sub(r"[0-9]{6}", "", mail.sent[0][2]) == re.sub(r"[0-9]{6}", "", mail.sent[1][2])
    assert known.headers["set-cookie"].split("=")[0] == unknown.headers["set-cookie"].split("=")[0]


def test_registration_closed_blocks_new_address_but_not_existing_one(app_factory):
    mail = SupportMail()
    app = app_factory(mailer=mail, email_registration_enabled=True, allow_registration=False)
    make_account(app, "known@example.test")
    with TestClient(app) as client:
        ask(client, "new@example.test")
        closed = enter(client, last_code(mail), **CONSENT)
        assert closed.status_code == 403 and "закрыта" in closed.json()["detail"]
        age_pending(app)
        ask(client, "known@example.test")
        assert enter(client, last_code(mail)).status_code == 200


def test_mail_disabled_failing_or_feature_off(app_factory):
    mail = SupportMail()
    app = app_factory(mailer=mail)
    with TestClient(app) as client:
        assert ask(client).status_code == 503
    mail.enabled = False
    app = app_factory(mailer=mail, email_registration_enabled=True)
    with TestClient(app) as client:
        disabled = ask(client)
        assert disabled.status_code == 503 and "Почта" in disabled.json()["detail"]
        assert client.get(BASE + "/options").json()["code_login_enabled"] is False
    mail.enabled, mail.fail = True, True
    app = app_factory(mailer=mail, email_registration_enabled=True)
    with TestClient(app) as client:
        assert client.get(BASE + "/options").json()["code_login_enabled"] is True
        assert ask(client).status_code == 503
        with app.state.sessions() as db:
            row = db.scalars(select(Pending)).one()
            assert row.used_at and not row.code_hash
    assert mail.sent == []


def test_origin_and_input_format(stack):
    _, client, _ = stack
    assert client.post(CODE + "/start", json={"email": "a@example.test"}, headers=EVIL).status_code == 403
    assert client.post(CODE + "/confirm", json={"code": "123456"}, headers=EVIL).status_code == 403
    assert client.post(CODE + "/confirm", json={"code": "12ab56"}).status_code == 422
    assert client.post(CODE + "/start", json={"email": "not-an-address"}).status_code == 422


def test_email_attached_to_telegram_account_signs_into_it(stack):
    app, client, mail = stack
    with app.state.sessions() as db:
        user = User(username="tguser", password_hash=UNUSABLE_PASSWORD, email="tg@example.test", email_verified_at=time.time())
        db.add(user)
        db.flush()
        db.add(TelegramIdentity(user_id=user.id, bot_id="bot", telegram_user_id=77, chat_id=77))
        db.commit()
        owner = user.id
    ask(client, "TG@example.test")
    result = enter(client, last_code(mail))
    assert result.status_code == 200 and result.json()["id"] == owner
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_unverified_holder_of_the_address_is_not_a_sign_in(stack):
    app, client, mail = stack
    with app.state.sessions() as db:
        db.add(User(username="claim", password_hash=UNUSABLE_PASSWORD, email="owner@example.test"))
        db.commit()
    ask(client)
    assert enter(client, last_code(mail)).status_code == 409


def test_old_registration_and_login_routes_still_work(stack):
    _, client, mail = stack
    started = client.post(BASE + "/registration/start", json={
        "email": "legacy@example.test", "password": "synthetic-email-password-1", **CONSENT,
    })
    assert started.status_code == 202
    code = re.search(r"код регистрации: ([0-9]{6})", mail.sent[-1][2]).group(1)
    done = client.post(BASE + "/registration/confirm", json={
        "registration_id": started.json()["registration_id"], "code": code,
    })
    assert done.status_code == 201 and done.json()["has_password"] is True
    login = client.post(BASE + "/login", json={"email": "legacy@example.test", "password": "synthetic-email-password-1"})
    assert login.status_code == 200


def test_telegram_signin_makes_account_with_consent_and_no_password(service_app):
    app, client = service_app
    assert client.post("/api/v1/auth/telegram/start", json={}).status_code == 422
    started = start(client)
    assert tg_confirm(client, token_of(started)).status_code == 200
    result = poll(client, started)
    assert result.status_code == 200 and result.json()["new_account"] is True
    assert result.json()["has_password"] is False
    with app.state.sessions() as db:
        assert db.scalars(select(User.password_hash)).one() == UNUSABLE_PASSWORD


# Смена пароля

PASSWORD = "test-only-password-123"
NEWER = "another-password-456"
CHANGE = "/api/v1/account/password"


def test_password_change_checks_old_keeps_this_session_and_ends_others(app, client):
    register(client)
    with TestClient(app) as other:
        other.post("/api/v1/auth/login", json={"username": "tester", "password": PASSWORD})
        assert other.get("/api/v1/auth/me").status_code == 200
        assert client.get("/api/v1/auth/me").json()["has_password"] is True
        assert client.post(CHANGE, json={"old_password": "wrong-password-1", "new_password": NEWER}).status_code == 403
        assert client.post(CHANGE, json={"old_password": PASSWORD, "new_password": PASSWORD}).status_code == 422
        assert client.post(CHANGE, json={"old_password": PASSWORD, "new_password": "short"}).status_code == 422
        done = client.post(CHANGE, json={"old_password": PASSWORD, "new_password": NEWER})
        assert done.status_code == 200 and done.json() == {"status": "ok"}
        assert client.get("/api/v1/auth/me").status_code == 200
        assert other.get("/api/v1/auth/me").status_code == 401
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 1
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "password_changed")) == 1
    with TestClient(app) as fresh:
        assert fresh.post("/api/v1/auth/login", json={"username": "tester", "password": PASSWORD}).status_code == 401
        assert fresh.post("/api/v1/auth/login", json={"username": "tester", "password": NEWER}).status_code == 200


def test_password_change_needs_session_csrf_and_origin(app, client):
    body = {"old_password": PASSWORD, "new_password": NEWER}
    assert client.post(CHANGE, json=body).status_code == 401
    register(client)
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post(CHANGE, json=body).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.post(CHANGE, json=body, headers=EVIL).status_code == 403


def test_wrong_old_password_is_rate_limited(app, client):
    register(client)
    for _ in range(5):
        assert client.post(CHANGE, json={"old_password": "wrong-password-1", "new_password": NEWER}).status_code == 403
    assert client.post(CHANGE, json={"old_password": PASSWORD, "new_password": NEWER}).status_code == 429


def test_account_without_password_gets_no_password_answer(stack):
    app, client, mail = stack
    ask(client)
    signed = enter(client, last_code(mail), **CONSENT)
    client.headers["X-CSRF-Token"] = signed.json()["csrf_token"]
    answer = client.post(CHANGE, json={"old_password": "anything-at-all-1", "new_password": NEWER})
    assert answer.status_code == 409 and answer.json()["code"] == "no_password"
    with app.state.sessions() as db:
        assert not has_usable_password(db.scalars(select(User.password_hash)).one())


# Восстановить аккаунт

RECOVERY = "/api/v1/support/recovery"
REQUEST = {"email": "Lost@Example.test", "text": "Входил через Telegram, аккаунт удалил. Помогите.", "accept_processing": True}


def test_recovery_is_stored_for_admin_and_mailed_to_support(app_factory):
    mail = SupportMail()
    app = app_factory(mailer=mail, support_email="help@example.test")
    with TestClient(app) as client:
        answer = client.post(RECOVERY, json=REQUEST)
        assert answer.status_code == 200 and "Запрос отправлен" in answer.json()["message"]
    assert mail.sent[0][0] == "help@example.test" and mail.sent[0][3] == "lost@example.test"
    assert REQUEST["text"] in mail.sent[0][2] and "lost@example.test" in mail.sent[0][2]
    with app.state.sessions() as db:
        row = db.scalars(select(Feedback)).one()
        assert row.kind == "recovery" and row.user_id is None and row.contact == "lost@example.test"
        assert row.description == REQUEST["text"] and row.status == "new"


def test_recovery_shows_in_admin_inbox(app, admin_client):
    with TestClient(app) as client:
        assert client.post(RECOVERY, json=REQUEST).status_code == 200
    rows = admin_client.get("/admin-api/v1/feedback").json()
    assert rows[0]["kind"] == "recovery" and rows[0]["user_id"] is None
    assert rows[0]["subject"] == "Восстановление аккаунта" and rows[0]["contact"] == "lost@example.test"
    changed = admin_client.patch("/admin-api/v1/feedback/" + rows[0]["id"], json={"status": "resolved", "version": 1})
    assert changed.status_code == 200


def test_recovery_survives_mail_failure_and_disabled_mail(app_factory):
    mail = SupportMail()
    mail.fail = True
    failing = app_factory(mailer=mail)
    with TestClient(failing) as client:
        assert client.post(RECOVERY, json=REQUEST).status_code == 200
    plain = app_factory()
    with TestClient(plain) as client:
        assert client.post(RECOVERY, json=REQUEST).status_code == 200
    with plain.state.sessions() as db:  # Обе проверки идут в одну базу.
        assert db.scalar(select(func.count()).select_from(Feedback)) == 2


def test_recovery_validation_origin_and_throttles(app_factory):
    mail = SupportMail()
    app = app_factory(mailer=mail)
    with TestClient(app) as client:
        assert client.post(RECOVERY, json=REQUEST | {"accept_processing": False}).status_code == 422
        assert client.post(RECOVERY, json=REQUEST | {"text": "   "}).status_code == 422
        assert client.post(RECOVERY, json=REQUEST | {"text": "я" * 2001}).status_code == 422
        assert client.post(RECOVERY, json=REQUEST | {"email": "nope"}).status_code == 422
        assert client.post(RECOVERY, json=REQUEST, headers=EVIL).status_code == 403
        for _ in range(3):
            assert client.post(RECOVERY, json=REQUEST).status_code == 200
        blocked = client.post(RECOVERY, json=REQUEST)
        assert blocked.status_code == 429 and "час" in blocked.json()["detail"]
        # Отказ по адресу почты уже занял одну попытку сети: пятая проходит, шестая нет.
        assert client.post(RECOVERY, json=REQUEST | {"email": "x@example.test"}).status_code == 200
        assert client.post(RECOVERY, json=REQUEST | {"email": "y@example.test"}).status_code == 429
    assert len(mail.sent) == 4


def test_recovery_letter_has_no_session_data(app_factory):
    mail = SupportMail()
    app = app_factory(mailer=mail)
    with TestClient(app) as client:
        client.post(RECOVERY, json=REQUEST)
    body = mail.sent[0][2].lower()
    assert "csrf" not in body and "cookie" not in body
