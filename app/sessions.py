"""Browser sessions shared by password sign-in, Telegram sign-in and registration."""

import secrets
import time

from fastapi import Response
from sqlalchemy import delete

from app.analytics import record_event
from app.config import Settings
from app.models import LoginSession, User
from app.policy import POLICY_VERSION
from app.profile import profile_state
from app.security import COOKIE_NAME, has_usable_password, hash_token
from app.timezones import valid_zone


def user_payload(user: User, csrf: str, db=None) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "csrf_token": csrf,
        "policy_current": user.policy_version == POLICY_VERSION,
        "policy_version": POLICY_VERSION,
        "has_password": has_usable_password(user.password_hash),
        "deletion_requested": user.deletion_requested_at is not None,
        "timezone": user.timezone if valid_zone(user.timezone) else None,
        # Whether the web shows the questionnaire «Знакомство» (new), and whether the calendar link is offered (docs/profile-v1.md).
        "profile_state": profile_state(db, user.id) if db is not None else "new",
        "calendar_offer": user.calendar_offer,
        "calendar_default_minutes": user.calendar_default_minutes,
    }


def create_session(db, response: Response, user: User, request, settings: Settings, *, method=None):
    """Rotate the current browser session and start a new one. `method` is recorded with the login event."""
    old = request.cookies.get(COOKIE_NAME)
    if old:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == hash_token(old)))
    db.execute(delete(LoginSession).where(LoginSession.expires_at < time.time()))
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    if user.first_login_at is None:
        user.first_login_at = time.time()
    record_event(db, user.id, "login", hash_token(token), outcome=method)
    db.add(
        LoginSession(
            token_hash=hash_token(token),
            user_id=user.id,
            csrf_token=csrf,
            expires_at=time.time() + settings.session_seconds,
        )
    )
    db.commit()
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=settings.session_seconds,
        path="/",
    )
    return user_payload(user, csrf, db)
