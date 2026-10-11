"""Live updates over WebSocket for Telegram sign-in, deletion confirmation and account linking.

The socket only tells the page that something changed. It never creates a session and never carries
secrets. The page then calls the usual HTTP endpoint, which sets cookies the normal way.

Limits are counted inside one worker process (see docs/telegram-live-v1.md). Behind the reverse proxy the
peer address is the proxy, so the real client address is read from X-Forwarded-For, and only when the peer
is a private address (the proxy of this deployment). A page that cannot use the socket polls as before.
"""

import asyncio
import ipaddress
import time
from typing import NamedTuple

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from app.security import throttle

CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_BUSY = 4429


class Gone(Exception):
    """The login or the session is not there (or not yours). Nothing else is told to the caller."""

    def __init__(self, code=CLOSE_NOT_FOUND):
        super().__init__(code)
        self.code = code


class Step(NamedTuple):
    message: dict
    final: bool
    owner: str | None = None  # Account id, when the socket counts against the per-account limit.


class Slots:
    """Counts open sockets per key inside this process."""

    def __init__(self):
        self.counts = {}

    def take(self, key, limit):
        if self.counts.get(key, 0) >= limit:
            return False
        self.counts[key] = self.counts.get(key, 0) + 1
        return True

    def free(self, key):
        left = self.counts.get(key, 0) - 1
        if left > 0:
            self.counts[key] = left
        else:
            self.counts.pop(key, None)


def slots_of(app):
    if not hasattr(app.state, "live_slots"):
        app.state.live_slots = Slots()
    return app.state.live_slots


def socket_ip(websocket):
    peer = websocket.client.host if websocket.client else "unknown"
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not (address.is_private or address.is_loopback):
        return peer
    # The proxy of this deployment appends the real address last. Headers from anyone else are ignored.
    forwarded = websocket.headers.get("x-forwarded-for", "").split(",")[-1].strip()
    try:
        return str(ipaddress.ip_address(forwarded)) if forwarded else peer
    except ValueError:
        return peer


async def refuse(websocket, code):
    try:
        await websocket.close(code=code)
    except RuntimeError:
        pass


async def gate(websocket):
    """Origin, handshake rate and per-address limit. Returns a release function, or None after refusing."""
    app = websocket.app
    settings, sessions = app.state.settings, app.state.sessions
    if websocket.headers.get("origin") != settings.public_origin:
        await refuse(websocket, CLOSE_FORBIDDEN)  # Cookies travel on cross-site handshakes, so the origin must match.
        return None
    ip = socket_ip(websocket)

    def limit_rate():
        with sessions() as db:
            throttle(db, "live-ws:" + ip, limit=settings.ws_handshakes_per_minute)

    try:
        await run_in_threadpool(limit_rate)
    except HTTPException:
        await refuse(websocket, CLOSE_BUSY)
        return None
    slots = slots_of(app)
    key = "ip:" + ip
    if not slots.take(key, settings.ws_per_ip):
        await refuse(websocket, CLOSE_BUSY)
        return None
    return lambda: slots.free(key)


async def serve(websocket, check):
    """Run check(db) on a fresh session every poll and push the message when it changes.

    check raises Gone when the caller may not watch this any more. The socket closes after a final message,
    after the maximum lifetime or when the client leaves.
    """
    app = websocket.app
    settings, sessions = app.state.settings, app.state.sessions
    release = await gate(websocket)
    if release is None:
        return

    def step():
        with sessions() as db:
            return check(db)

    owner_key = None
    try:
        try:
            current = await run_in_threadpool(step)
        except Gone as gone:
            await refuse(websocket, gone.code)
            return
        if current.owner:
            owner_key = "user:" + current.owner
            if not slots_of(app).take(owner_key, settings.ws_per_account):
                owner_key = None
                await refuse(websocket, CLOSE_BUSY)
                return
        await websocket.accept()
        deadline = time.monotonic() + settings.ws_max_seconds
        last = None
        try:
            while True:
                if current.message != last:
                    await websocket.send_json(current.message)
                    last = current.message
                if current.final or time.monotonic() >= deadline:
                    break
                try:
                    received = await asyncio.wait_for(websocket.receive(), timeout=settings.ws_poll_seconds)
                except TimeoutError:
                    received = None
                if received is not None and received["type"] == "websocket.disconnect":
                    return
                try:
                    current = await run_in_threadpool(step)
                except Gone as gone:
                    await refuse(websocket, gone.code)
                    return
            await refuse(websocket, 1000)
        except (WebSocketDisconnect, RuntimeError):
            return
    finally:
        if owner_key:
            slots_of(app).free(owner_key)
        release()


def login_message(row, now):
    if row.status == "consumed":
        return {"status": "used"}, True
    if row.expires_at <= now:
        return {"status": "expired"}, True
    if row.status == "pending":
        return {"status": "pending"}, False
    return {"status": "confirmed"}, True
