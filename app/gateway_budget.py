"""Own-key billing snapshot. No generations, discovery, redirects or raw response logging."""

import argparse
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from app.config import PROGRAM_BASE_URL, read_secret_file

MAXIMUM_BYTES = 65536
MAX_AGE = 900


class BudgetError(Exception):
    pass


def amount(value):
    if isinstance(value, bool) or value is None:
        raise BudgetError("invalid_amount")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise BudgetError("invalid_amount") from None
    if not result.is_finite() or not 0 <= result <= 1000000000000:
        raise BudgetError("invalid_amount")
    return result


def counter(value):
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise BudgetError("invalid_counter")
    return value


def query(key, *, reserved_calls, unsettled_calls, transport=None, now=None):
    """The owner confirmed this gateway's prices and key budget are denominated in RUB."""
    started = time.time() if now is None else now
    try:
        with httpx.Client(timeout=httpx.Timeout(20, connect=5), trust_env=False,
                          follow_redirects=False, transport=transport) as client:
            with client.stream("GET", PROGRAM_BASE_URL.removesuffix("/v1") + "/key/info",
                               headers={"Authorization": "Bearer " + key}) as response:
                if response.status_code != 200:
                    raise BudgetError("billing_http_" + str(response.status_code))
                body = bytearray()
                for part in response.iter_bytes():
                    body.extend(part)
                    if len(body) > MAXIMUM_BYTES:
                        raise BudgetError("billing_response_too_large")
        info = json.loads(body, parse_float=Decimal)["info"]
        if info.get("currency") is not None and info["currency"] != "RUB":
            raise BudgetError("billing_currency_changed")
        total, spent = amount(info["max_budget"]), amount(info["spend"])
        if total <= 0:
            raise BudgetError("billing_budget_missing")
        if info.get("budget_duration"):
            raise BudgetError("billing_budget_resets")
        reserved, unsettled = counter(reserved_calls), counter(unsettled_calls)
        return {"version": 1, "currency": "RUB", "currency_source": "owner_confirmed",
                "total": str(total), "spent": str(spent), "checked_at": started,
                "reserved_calls": reserved, "unsettled_calls": min(reserved, unsettled)}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise BudgetError("billing_unavailable_or_invalid") from None


def save(snapshot, destination):
    target = Path(destination)
    descriptor, temp = tempfile.mkstemp(dir=target.parent, prefix=".billing-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)


def load(path):
    try:
        with Path(path).open("rb") as stream:
            body = stream.read(MAXIMUM_BYTES + 1)
        if len(body) > MAXIMUM_BYTES:
            raise BudgetError("snapshot_too_large")
        data = json.loads(body)
        if data["version"] != 1 or data["currency"] != "RUB":
            raise BudgetError("invalid_snapshot")
        if amount(data["checked_at"]) > 4102444800 or amount(data["total"]) <= 0:
            raise BudgetError("invalid_snapshot")
        if counter(data["unsettled_calls"]) > counter(data["reserved_calls"]):
            raise BudgetError("invalid_snapshot")
        return {"total": amount(data["total"]), "spent": amount(data["spent"]),
                "checked_at": float(amount(data["checked_at"])),
                "reserved_calls": counter(data["reserved_calls"]),
                "unsettled_calls": counter(data["unsettled_calls"])}
    except (OSError, ValueError, KeyError, TypeError, BudgetError):
        return None


def fresh(snapshot, now=None):
    return snapshot is not None and -60 <= (time.time() if now is None else now) - snapshot["checked_at"] <= MAX_AGE


def available(settings, reserved_calls, *, now=None):
    if not settings.gateway_budget_file:
        return True
    snapshot = load(settings.gateway_budget_file)
    if not fresh(snapshot, now):
        return False
    baseline = max(0, snapshot["reserved_calls"] - snapshot["unsettled_calls"])
    pending = max(0, reserved_calls - baseline) + 1
    return snapshot["total"] - snapshot["spent"] - settings.budget_reserve_rub >= pending * settings.budget_attempt_reserve_rub


def view(settings, reserved_calls, *, now=None):
    snapshot = load(settings.gateway_budget_file) if settings.gateway_budget_file else None
    def money(value):
        return str(value.quantize(Decimal("0.01")))
    return {
        "currency": "RUB", "source": "gateway_key_info", "configured": bool(settings.gateway_budget_file),
        "total": money(snapshot["total"]) if snapshot else None,
        "spent": money(snapshot["spent"]) if snapshot else None,
        "remaining": money(snapshot["total"] - snapshot["spent"]) if snapshot else None,
        "reserve": money(Decimal(settings.budget_reserve_rub)),
        "pilot_remaining": money(max(Decimal(0), snapshot["total"] - snapshot["spent"] - settings.budget_reserve_rub)) if snapshot else None,
        "checked_at": datetime.fromtimestamp(snapshot["checked_at"], UTC).isoformat() if snapshot else None,
        "fresh": fresh(snapshot, now),
        "processing_paused": not available(settings, reserved_calls, now=now),
        "model": settings.cloudru_model, "daily_unit_limit": settings.daily_unit_limit,
    }


def main():
    parser = argparse.ArgumentParser(description="Refresh only our key's billing snapshot")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--reserved-calls", required=True, type=int)
    parser.add_argument("--unsettled-calls", required=True, type=int)
    parser.add_argument("--currency", choices=["RUB"], required=True)
    args = parser.parse_args()
    try:
        snapshot = query(read_secret_file(args.key_file), reserved_calls=args.reserved_calls,
                         unsettled_calls=args.unsettled_calls)
        save(snapshot, args.destination)
    except (BudgetError, OSError):
        raise SystemExit("Budget refresh failed; previous snapshot retained") from None
    print("Budget snapshot refreshed; no generations or STT sent")


if __name__ == "__main__":
    main()
