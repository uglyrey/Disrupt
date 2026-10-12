"""WebSocket push for Telegram sign-in, deletion confirmation and linking. Telegram is always a fake."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.live import Slots, socket_ip
from app.models import LinkRequest, TelegramLogin
from app.security import COOKIE_NAME
from tests.conftest import register
from tests.test_auth_telegram import confirm, poll, rows, start, token_of
from tests.test_internal import BASE, HEADERS, SERVICE_TOKEN, linked, pending_link

ORIGIN = "http://127.0.0.1:8000"
ON = {"origin": ORIGIN}
FAST = {"ws_poll_seconds": 0.05, "internal_api_token": SERVICE_TOKEN}


@pytest.fixture
def service(app_factory):
    app = app_factory(**FAST)
    with TestClient(app) as client:
        yield app, client


def login_url(started):
    return "/api/v1/auth/telegram/ws/" + started.json()["login_id"]


def refused(client, url, **headers):
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(url, headers=headers):
            pass
    return caught.value.code


def finished(socket):
    with pytest.raises(WebSocketDisconnect) as caught:
        socket.receive_json()
    return caught.value.code


# Sign-in


def test_login_socket_pushes_pending_then_confirmed(service):
    app, client = service
    started = start(client)
    with client.websocket_connect(login_url(started), headers=ON) as socket:
        assert socket.receive_json() == {"status": "pending"}
        assert confirm(client, token_of(started)).status_code == 200
        assert socket.receive_json() == {"status": "confirmed"}
        assert finished(socket) == 1000
    # The socket never signs anyone in. The ordinary status call does.
    assert COOKIE_NAME not in client.cookies
    assert poll(client, started).json()["status"] == "ok"
    assert COOKIE_NAME in client.cookies


def test_login_socket_is_for_the_browser_that_started_it(service):
    app, client = service
    started = start(client)
    with TestClient(app) as stranger:
        assert refused(stranger, login_url(started), **ON) == 4404
        stranger.cookies.set("notes_tg_login", "x" * 43, path="/api/v1/auth/telegram")
        assert refused(stranger, login_url(started), **ON) == 4404
    assert refused(client, "/api/v1/auth/telegram/ws/no-such-login", **ON) == 4404
    assert refused(client, "/api/v1/auth/telegram/ws/" + "a" * 80, **ON) == 4404


def test_login_socket_reports_expiry_and_use(service):
    app, client = service
    expired = start(client)
    with app.state.sessions.begin() as db:
        db.get(TelegramLogin, expired.json()["login_id"]).expires_at = 1.0
    with client.websocket_connect(login_url(expired), headers=ON) as socket:
        assert socket.receive_json() == {"status": "expired"}
        assert finished(socket) == 1000
    used = start(client)
    assert confirm(client, token_of(used), update_id=2000).status_code == 200
    assert poll(client, used).json()["status"] == "ok"
    # The binding cookie is removed after the sign-in, so a second socket for it is refused.
    assert refused(client, login_url(used), **ON) == 4404


def test_login_socket_closes_after_the_longest_time(app_factory):
    app = app_factory(**FAST | {"ws_max_seconds": 0.3})
    with TestClient(app) as client:
        started = start(client)
        with client.websocket_connect(login_url(started), headers=ON) as socket:
            assert socket.receive_json() == {"status": "pending"}
            assert finished(socket) == 1000


def test_socket_checks_the_origin(service):
    app, client = service
    started = start(client)
    assert refused(client, login_url(started)) == 4403
    assert refused(client, login_url(started), origin="https://evil.example") == 4403
    assert refused(client, login_url(started), origin=ORIGIN + ".evil.example") == 4403
    assert refused(client, "/api/v1/telegram/links/ws", origin="https://evil.example") == 4403
    with client.websocket_connect(login_url(started), headers=ON) as socket:
        assert socket.receive_json() == {"status": "pending"}


def test_socket_limits_open_sockets_per_address(app_factory):
    app = app_factory(**FAST | {"ws_per_ip": 2})
    with TestClient(app) as client:
        url = login_url(start(client))
        with client.websocket_connect(url, headers=ON) as first, client.websocket_connect(url, headers=ON) as second:
            assert first.receive_json() == second.receive_json() == {"status": "pending"}
            assert refused(client, url, **ON) == 4429
        # A closed socket frees its place.
        with client.websocket_connect(url, headers=ON) as again:
            assert again.receive_json() == {"status": "pending"}
    assert app.state.live_slots.counts == {}


def test_socket_limits_handshakes_per_minute(app_factory):
    app = app_factory(**FAST | {"ws_handshakes_per_minute": 2})
    with TestClient(app) as client:
        url = login_url(start(client))
        for _ in range(2):
            with client.websocket_connect(url, headers=ON) as socket:
                assert socket.receive_json() == {"status": "pending"}
        assert refused(client, url, **ON) == 4429


def test_the_address_comes_from_the_proxy_only_for_private_peers():
    class Fake:
        def __init__(self, host, forwarded=None):
            self.client = type("Peer", (), {"host": host})()
            self.headers = {"x-forwarded-for": forwarded} if forwarded else {}

    assert socket_ip(Fake("172.18.0.5", "198.51.100.7")) == "198.51.100.7"
    assert socket_ip(Fake("172.18.0.5", "1.1.1.1, 198.51.100.7")) == "198.51.100.7"  # The proxy appends the last one.
    assert socket_ip(Fake("172.18.0.5", "not-an-address")) == "172.18.0.5"
    assert socket_ip(Fake("172.18.0.5")) == "172.18.0.5"
    assert socket_ip(Fake("8.8.8.8", "198.51.100.7")) == "8.8.8.8"  # A direct caller cannot choose its address.


def test_slots_count_and_free():
    slots = Slots()
    assert slots.take("a", 1) and not slots.take("a", 1) and slots.take("b", 1)
    slots.free("a")
    slots.free("b")
    assert slots.counts == {}


def test_the_csp_names_the_socket_address(app_factory):
    plain = app_factory()
    with TestClient(plain) as client:
        csp = client.get("/health").headers["content-security-policy"]
        assert "connect-src 'self' ws://127.0.0.1:8000;" in csp
    secure = app_factory(public_origin="https://beresta.example", secure_cookies=True)
    with TestClient(secure) as client:
        csp = client.get("/health").headers["content-security-policy"]
        assert "connect-src 'self' wss://beresta.example;" in csp
        assert "script-src 'self';" in csp


# Deletion confirmation


def test_deletion_socket_belongs_to_the_account(service):
    app, client = service
    linked(client)
    started = client.post("/api/v1/account/delete-request/telegram")
    url = "/api/v1/account/delete-request/telegram/ws/" + started.json()["login_id"]
    with TestClient(app) as other:
        assert refused(other, url, **ON) == 4401  # Not signed in.
        register(other, "other")
        assert refused(other, url, **ON) == 4404  # Someone else's request.
    with client.websocket_connect(url, headers=ON) as socket:
        assert socket.receive_json() == {"status": "pending"}
        assert confirm(client, token_of(started), update_id=7).status_code == 200
        assert socket.receive_json() == {"status": "confirmed"}
        assert finished(socket) == 1000
    # The socket did not delete anything. The usual POST does.
    assert client.get("/api/v1/auth/me").status_code == 200
    check = "/api/v1/account/delete-request/telegram/" + started.json()["login_id"]
    assert client.post(check).json() == {"status": "requested"}


def test_a_login_id_does_not_open_the_deletion_socket(service):
    app, client = service
    linked(client)
    with TestClient(app) as browser:
        started = start(browser)
        url = "/api/v1/account/delete-request/telegram/ws/" + started.json()["login_id"]
    assert refused(client, url, **ON) == 4404


def test_account_limit_for_deletion_sockets(app_factory):
    app = app_factory(**FAST | {"ws_per_account": 1})
    with TestClient(app) as client:
        linked(client)
        started = client.post("/api/v1/account/delete-request/telegram")
        url = "/api/v1/account/delete-request/telegram/ws/" + started.json()["login_id"]
        with client.websocket_connect(url, headers=ON) as socket:
            assert socket.receive_json() == {"status": "pending"}
            assert refused(client, url, **ON) == 4429
    assert app.state.live_slots.counts == {}


# Linking


def test_links_socket_needs_a_session(service):
    app, client = service
    assert refused(client, "/api/v1/telegram/links/ws", **ON) == 4401


def test_links_socket_pushes_only_for_its_own_account(service):
    app, client = service
    register(client)
    with TestClient(app) as other:
        register(other, "other")
        with (
            client.websocket_connect("/api/v1/telegram/links/ws", headers=ON) as mine,
            other.websocket_connect("/api/v1/telegram/links/ws", headers=ON) as theirs,
        ):
            assert mine.receive_json() == {"pending": [], "linked": 0}
            assert theirs.receive_json() == {"pending": [], "linked": 0}
            code, _, _ = pending_link(client)
            assert mine.receive_json() == {"pending": [code["link_request_id"]], "linked": 0}
            confirmed = client.post("/api/v1/telegram/links/" + code["link_request_id"] + "/confirm")
            assert confirmed.status_code == 200
            assert mine.receive_json() == {"pending": [], "linked": 1}
            # The other account heard nothing from all this. Its first news is its own request.
            mine_code = other.post("/api/v1/telegram/link-code").json()
            payload = {**BASE, "telegram_user_id": 5, "chat_id": 5, "update_id": 50, "code": mine_code["code"]}
            assert client.post("/internal/v1/telegram/link-request", json=payload, headers=HEADERS).status_code == 200
            assert theirs.receive_json() == {"pending": [mine_code["link_request_id"]], "linked": 0}
    assert rows(app, LinkRequest)[0].status == "confirmed"


def test_links_socket_ends_with_the_session(service):
    app, client = service
    register(client)
    with client.websocket_connect("/api/v1/telegram/links/ws", headers=ON) as socket:
        assert socket.receive_json() == {"pending": [], "linked": 0}
        assert client.post("/api/v1/auth/logout").status_code in {200, 204}
        assert finished(socket) == 4401
