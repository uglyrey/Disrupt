"""Marketing consent is optional, separate from policy acceptance, and revocable."""

import pytest
from fastapi.testclient import TestClient

from app.models import User
from app.policy import MARKETING_CONSENT_VERSION, POLICY_VERSION
from tests.test_auth_telegram import confirm, poll, start, token_of
from tests.test_email_registration import BASE, PASSWORD, Mail, code
from tests.test_internal import SERVICE_TOKEN


@pytest.mark.parametrize("opt_in", [False, True])
def test_email_registration_records_only_an_explicit_choice(app_factory, opt_in):
    mail = Mail()
    app = app_factory(mailer=mail, email_registration_enabled=True)
    with TestClient(app) as client:
        started = client.post(BASE + "/registration/start", json={
            "email": "person@example.test", "password": PASSWORD,
            "accept_policy": True, "policy_version": POLICY_VERSION, "marketing_opt_in": opt_in,
        })
        assert started.status_code == 202
        confirmed = client.post(BASE + "/registration/confirm", json={
            "registration_id": started.json()["registration_id"], "code": code(mail),
        })
        assert confirmed.status_code == 201
        with app.state.sessions() as db:
            user = db.get(User, confirmed.json()["id"])
            assert (user.marketing_consent_at is not None) is opt_in
            assert user.marketing_consent_version == (MARKETING_CONSENT_VERSION if opt_in else None)
            assert user.marketing_consent_source == ("email-registration" if opt_in else None)


def test_telegram_choice_and_account_revocation(app_factory):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    with TestClient(app) as client:
        started = start(client, marketing_opt_in=True, confirm_age=False)
        assert started.status_code == 201
        assert confirm(client, token_of(started)).status_code == 200
        signed_in = poll(client, started)
        assert signed_in.status_code == 200
        user_id = signed_in.json()["id"]
        headers = {"X-CSRF-Token": signed_in.json()["csrf_token"]}
        assert client.get("/api/v1/account/marketing-consent").json() == {"enabled": True}
        denied = client.put("/api/v1/account/marketing-consent", json={"enabled": False},
                            headers={"X-CSRF-Token": "wrong"})
        assert denied.status_code == 403
        assert client.put("/api/v1/account/marketing-consent", json={"enabled": False}, headers=headers).json() == {"enabled": False}
        with app.state.sessions() as db:
            user = db.get(User, user_id)
            assert user.marketing_consent_at is None
            assert user.marketing_consent_revoked_at is not None
        assert client.put("/api/v1/account/marketing-consent", json={"enabled": True}, headers=headers).json() == {"enabled": True}
        with app.state.sessions() as db:
            user = db.get(User, user_id)
            assert user.marketing_consent_at is not None
            assert user.marketing_consent_source == "account-settings"
            assert user.marketing_consent_revoked_at is None
