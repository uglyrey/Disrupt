"""Signed campaign tag carried in the registration page, without a tracking cookie."""

import hashlib
import hmac
import re
import time

LIFETIME = 7 * 86400
TAG = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def secret(settings):
    return settings.analytics_pseudonym_key.encode()


def signature(key, payload):
    return hmac.new(key, b"beresta-first-touch-v1:" + payload.encode(), hashlib.sha256).hexdigest()


def read(value, settings, *, default="unknown", now=None):
    key = secret(settings)
    if not key or not isinstance(value, str) or len(value) > 200:
        return default
    parts = value.split(":")
    if len(parts) != 3 or not TAG.fullmatch(parts[0]) or not parts[1].isdigit():
        return default
    payload = ":".join(parts[:2])
    if not hmac.compare_digest(parts[2], signature(key, payload)):
        return default
    current = time.time() if now is None else now
    return parts[0] if -60 <= current - int(parts[1]) <= LIFETIME else default


def page_token(request, settings):
    key = secret(settings)
    if not key:
        return ""
    supplied = request.query_params.get("utm_source")
    source = (
        supplied.lower()
        if supplied and TAG.fullmatch(supplied.lower())
        else "unknown"
        if supplied
        else "direct"
    )
    payload = source + ":" + str(int(time.time()))
    return payload + ":" + signature(key, payload)
