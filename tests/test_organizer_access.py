"""Observer boundaries and verified activity. Only synthetic accounts and mock providers."""

import time
import zipfile
from io import BytesIO

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import totp
from app.admin_auth import COOKIE_NAME
from app.admin_models import AdminAccount, AdminAudit, AdminSession
from app.config import Settings
from tests.conftest import ADMIN_ADDRESS, ADMIN_PASSWORD, admin_login, make_admin

KEY = "synthetic-review-analytics-key-1234567890"


def make_observer(app, *, expires=None):
    secret = make_admin(app, username="observer")
    with app.state.sessions() as db:
        account = db.scalar(select(AdminAccount).where(AdminAccount.username == "observer"))
        account.role, account.access_expires_at = "observer", expires or time.time() + 3600
        db.commit()
    return secret


def reviewer_login(client, secret):
    return client.post('/review-api/v1/login', headers={'Origin': 'https://testserver'}, json={
        'username': 'observer', 'password': ADMIN_PASSWORD, 'totp': totp.code_at(secret, time.time()),
    })


def test_observer_is_https_only_read_only_and_separate_from_owner(app_factory):
    app = app_factory(review_enabled=True, analytics_pseudonym_key=KEY, public_origin='https://testserver')
    secret = make_observer(app)
    owner = make_admin(app)
    with TestClient(app, base_url='https://testserver', client=('198.51.100.7', 50000)) as client:
        assert client.get('/review').status_code == 200
        assert client.get('/review-api/v1/summary?from=2026-10-01&to=2026-10-02').status_code == 401
        assert client.get('/admin').status_code == 404
        assert client.get('/review/assets/metrics.js').status_code == 200
        assert client.get('/review/assets/../../secrets/data_key.txt').status_code == 404
        assert client.post('/review-api/v1/login', headers={'Origin': 'https://testserver'}, json={
            'username': 'owner', 'password': ADMIN_PASSWORD, 'totp': totp.code_at(owner, time.time()),
        }).status_code == 401
        assert reviewer_login(client, secret).status_code == 200
        cookie = client.cookies.get('beresta_review')
        response = client.get('/review-api/v1/summary?from=2026-10-01&to=2026-10-02')
        assert response.status_code == 200
        assert response.json()['budget'] is None
        assert client.get('/review-api/v1/feedback').status_code == 404
        assert client.get('/review-api/v1/audit').status_code == 404
        assert client.patch('/review-api/v1/feedback/fake', json={'status': 'resolved'}).status_code == 404
    with TestClient(app, client=ADMIN_ADDRESS) as private:
        private.cookies.set(COOKIE_NAME, cookie)
        assert private.get('/admin-api/v1/summary?from=2026-10-01&to=2026-10-02').status_code == 401
    with TestClient(app, base_url='http://testserver') as plain:
        assert plain.get('/review').status_code == 404


def test_observer_expiry_and_wrong_origin(app_factory):
    app = app_factory(review_enabled=True, analytics_pseudonym_key=KEY, public_origin='https://testserver')
    secret = make_observer(app)
    with TestClient(app, base_url='https://testserver') as client:
        assert client.post('/review-api/v1/login', json={
            'username':'observer','password':ADMIN_PASSWORD,'totp':totp.code_at(secret,time.time()),
        }).status_code == 403
        assert reviewer_login(client, secret).status_code == 200
        with app.state.sessions() as db:
            account = db.scalar(select(AdminAccount).where(AdminAccount.username == 'observer'))
            account.access_expires_at = time.time()-1
            db.commit()
        assert client.get('/review-api/v1/me').status_code == 401


def test_owner_access_stays_private_and_working(app_factory):
    app = app_factory(review_enabled=True, analytics_pseudonym_key=KEY, public_origin='https://testserver')
    secret = make_admin(app)
    with TestClient(app, client=ADMIN_ADDRESS) as client:
        assert admin_login(app, client, secret).status_code == 200
        assert client.get('/admin-api/v1/summary?from=2026-10-01&to=2026-10-02').status_code == 200


def test_observer_exports_and_logout_do_not_expose_owner_data(app_factory):
    app = app_factory(review_enabled=True, analytics_pseudonym_key=KEY, public_origin='https://testserver')
    secret = make_observer(app)
    with TestClient(app, base_url='https://testserver') as client:
        response = reviewer_login(client, secret)
        assert response.status_code == 200
        cookie = response.headers['set-cookie'].lower()
        assert 'secure' in cookie and 'httponly' in cookie and 'samesite=strict' in cookie
        csrf = response.json()['csrf_token']
        csv = client.get('/review-api/v1/export?from=2026-10-01&to=2026-10-02')
        assert csv.status_code == 200 and csv.headers['cache-control'] == 'no-store'
        archive = client.get('/review-api/v1/export.zip?from=2026-10-01&to=2026-10-02')
        assert archive.status_code == 200
        with zipfile.ZipFile(BytesIO(archive.content)) as bundle:
            assert {'users.csv', 'events.csv', 'daily.csv'} <= set(bundle.namelist())
            assert not any('admin' in name or 'feedback' in name for name in bundle.namelist())
        assert client.post('/review-api/v1/logout').status_code == 403
        assert client.post('/review-api/v1/logout', headers={'X-CSRF-Token':csrf}).status_code == 204
        assert client.get('/review-api/v1/me').status_code == 401
    with app.state.sessions() as db:
        assert db.scalars(select(AdminSession)).all() == []
        assert {'export_csv', 'export_zip', 'logout'} <= {row.action for row in db.scalars(select(AdminAudit))}


def test_disabled_portal_and_wrong_host_deny_even_a_valid_cookie(app_factory):
    app = app_factory(review_enabled=True, analytics_pseudonym_key=KEY, public_origin='https://testserver')
    secret = make_observer(app)
    with TestClient(app, base_url='https://testserver') as client:
        assert reviewer_login(client, secret).status_code == 200
        token = client.cookies.get('beresta_review')
    with TestClient(app, base_url='https://other.example') as wrong:
        wrong.cookies.set('beresta_review',token)
        assert wrong.get('/review-api/v1/me').status_code == 404
    disabled = app_factory(analytics_pseudonym_key=KEY, public_origin='https://testserver')
    with TestClient(disabled,base_url='https://testserver') as client:
        client.cookies.set('beresta_review',token)
        assert client.get('/review').status_code == 404
        assert client.get('/review-api/v1/me').status_code == 404
        assert client.get('/review/assets/auth.js').status_code == 404


def test_observer_requires_confirmation_expiry_and_cannot_modify_users(app_factory):
    app = app_factory(review_enabled=True, analytics_pseudonym_key=KEY, public_origin='https://testserver')
    secret = make_observer(app)
    with TestClient(app, base_url='https://testserver') as client:
        with app.state.sessions() as db:
            account = db.scalar(select(AdminAccount))
            account.totp_enabled = False
            db.commit()
        assert reviewer_login(client, secret).status_code == 401
        with app.state.sessions() as db:
            account = db.scalar(select(AdminAccount))
            account.totp_enabled, account.access_expires_at = True, None
            db.commit()
        assert reviewer_login(client, secret).status_code == 401
        with app.state.sessions() as db:
            account = db.scalar(select(AdminAccount))
            account.access_expires_at = time.time()+3600
            db.commit()
        assert reviewer_login(client, secret).status_code == 200
        assert client.post('/review-api/v1/users',json={}).status_code == 404
        assert client.delete('/review-api/v1/users/fake').status_code == 404
        with app.state.sessions() as db:
            account = db.scalar(select(AdminAccount))
            account.disabled = True
            db.commit()
        assert client.get('/review-api/v1/me').status_code == 401


def test_config_requires_https_and_stable_pseudonyms():
    import pytest

    for changes in [{}, {'public_origin':'https://testserver'}]:
        with pytest.raises(ValueError):
            Settings(review_enabled=True, **changes)


def test_owner_token_cannot_enter_public_portal_and_remains_valid_private(app_factory):
    app = app_factory(review_enabled=True, analytics_pseudonym_key=KEY, public_origin='https://testserver')
    secret = make_admin(app)
    with TestClient(app, client=ADMIN_ADDRESS) as private:
        assert admin_login(app, private, secret).status_code == 200
        token = private.cookies.get(COOKIE_NAME)
        with TestClient(app,base_url='https://testserver') as public:
            public.cookies.set('beresta_review',token)
            assert public.get('/review-api/v1/me').status_code == 401
        assert private.get('/admin-api/v1/me').status_code == 200


