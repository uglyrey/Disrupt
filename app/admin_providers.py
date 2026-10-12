"""Админка: какой ИИ-сервис отвечал, сколько вызовов и токенов, расход резерва в долларах."""

import time
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.admin_metrics import calendar_day, midnight, period, utc
from app.contracts import AiProviders
from app.fallback_budget import MICRO
from app.models import ProviderUsage
from app.provider_router import provider_status


def usd(value):
    return format(Decimal(str(value or 0)).quantize(Decimal("0.000001")), "f")


def stats(db, low, high):
    """По сервисам за полуоткрытый промежуток времени. Тестовые аккаунты не считаются."""
    name = func.coalesce(ProviderUsage.provider, "cloudru")
    rows = db.execute(
        select(
            name, func.count(), func.coalesce(func.sum(ProviderUsage.input_tokens), 0),
            func.coalesce(func.sum(ProviderUsage.output_tokens), 0),
            func.coalesce(func.sum(ProviderUsage.stt_seconds), 0.0),
            func.coalesce(func.sum(ProviderUsage.estimated_cost), 0),
        )
        .where(ProviderUsage.is_test.is_(False), ProviderUsage.occurred_at >= low, ProviderUsage.occurred_at < high)
        .group_by(name).order_by(name)
    ).all()
    return [
        {"provider": provider, "calls": calls, "input_tokens": int(tokens_in), "output_tokens": int(tokens_out),
         "stt_seconds": float(seconds), "estimated_usd": usd(cost)}
        for provider, calls, tokens_in, tokens_out, seconds, cost in rows
    ]


def ai_providers(db, settings, start, end, now=None):
    now = time.time() if now is None else now
    first, last = period(start, end, now)
    today = calendar_day(now)
    status = provider_status(db, settings, now)
    return AiProviders.model_validate({
        "fallback_enabled": status["fallback_enabled"],
        "primary_state": status["primary_state"],
        "primary_changed_at": utc(status["primary_changed_at"]) if status["primary_changed_at"] else None,
        "next_probe_at": utc(status["next_probe_at"]) if status["next_probe_at"] else None,
        "fallback_spent_today_usd": usd(Decimal(status["fallback_spent_micro_usd"]) / MICRO),
        "fallback_cap_usd": usd(Decimal(status["fallback_cap_micro_usd"]) / MICRO),
        "today": stats(db, midnight(today), midnight(today + timedelta(days=1))),
        "period": stats(db, midnight(first), midnight(last + timedelta(days=1))),
        "generated_at": utc(now),
    })
