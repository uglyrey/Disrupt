"""Automatic retries of AI processing. The worker is the only caller.

A paid call is repeated only when its failure is understood, the number of retries is small and the whole
deployment is not already struggling. Every limit is read from the database, so several workers agree.
"""

import random
import time

from sqlalchemy import func, or_, select

from app.models import Job, ProviderUsage

CLEAR = "clear"
AMBIGUOUS = "ambiguous"
BAD_OUTPUT = "bad_output"

# The request certainly produced no billable answer.
CLEAR_CODES = frozenset({
    "provider_rate_limit", "provider_unavailable", "provider_connect_failed",
    "stt_rate_limit", "stt_unavailable", "stt_connect_failed",
})
# The call may have been billed. One careful retry at most.
AMBIGUOUS_CODES = frozenset({
    "provider_timeout_unknown", "provider_connection_unknown",
    "stt_timeout_unknown", "stt_connection_unknown", "execution_unknown",
})
BAD_OUTPUT_CODES = frozenset({"provider_invalid_response", "provider_incomplete_response", "ungrounded_quote"})
# Failures that show the gateway is unwell. They feed the circuit breaker.
GATEWAY_CODES = CLEAR_CODES | AMBIGUOUS_CODES

# Задание, чья последняя попытка шла на резерв (docs/ai-failover-v1.md), предохранитель основного шлюза не считает.
FALLBACK_PROVIDER = "openai"

HOUR = 3600
MAX_BREAKER_ROWS = 500


def classify(code):
    """Class of a failure code, or None when it must never be retried (auth, bad request, limits, user errors)."""
    if code in CLEAR_CODES:
        return CLEAR
    if code in AMBIGUOUS_CODES:
        return AMBIGUOUS
    if code in BAD_OUTPUT_CODES:
        return BAD_OUTPUT
    return None


def delays_for(settings, kind):
    return {
        CLEAR: settings.retry_clear_delays,
        AMBIGUOUS: settings.retry_ambiguous_delays,
        BAD_OUTPUT: settings.retry_bad_output_delays,
    }.get(kind, ())


def backoff(settings, kind, attempts, retry_after=None, *, rng=random.random):
    """Seconds to wait before the next attempt, or None when this class has no retries left.

    `attempts` is the number of retries already scheduled for the job. The pause gets plus or minus
    `retry_jitter`. A Retry-After from the gateway can only lengthen it, and is capped.
    """
    delays = delays_for(settings, kind)
    if attempts < 0 or attempts >= len(delays):
        return None
    delay = delays[attempts] * (1 + settings.retry_jitter * (2 * rng() - 1))
    if retry_after is not None and retry_after > 0:
        delay = max(delay, min(retry_after, settings.retry_after_max_seconds))
    return delay


def retries_in_last_hour(db, now, user_id=None):
    """Retries that ran in the last hour plus retries that are waiting for their turn.

    A retry leaves a receipt whose request_id contains `:r<N>` (see UsageRecorder), so nothing new is stored.
    """
    receipts = select(func.count()).select_from(ProviderUsage).where(
        ProviderUsage.occurred_at >= now - HOUR,
        # Exactly the retry receipts of capture jobs (`<job>:llm:rN`, `<job>:stt:rN`), never other ids with ":r".
        or_(ProviderUsage.request_id.like("%:llm:r_"), ProviderUsage.request_id.like("%:stt:r_")),
    )
    waiting = select(func.count()).select_from(Job).where(Job.status == "queued", Job.attempts > 0)
    if user_id is not None:
        receipts = receipts.where(ProviderUsage.user_id == user_id)
        waiting = waiting.where(Job.user_id == user_id)
    return db.scalar(receipts) + db.scalar(waiting)


def breaker_open(db, settings, now):
    """True while the gateway failed for enough different records recently.

    The breaker trips at the latest moment T when `retry_breaker_failures` records had a gateway failure inside
    `retry_breaker_window_seconds`. It stays open for `retry_breaker_pause_seconds` after T. While it is open, retries
    wait and only fresh jobs try once.
    """
    need = settings.retry_breaker_failures
    horizon = settings.retry_breaker_window_seconds + settings.retry_breaker_pause_seconds
    stamps = db.scalars(
        select(Job.last_failed_at)
        .where(
            Job.last_failed_at >= now - horizon, Job.last_error_code.in_(GATEWAY_CODES),
            or_(Job.provider_used.is_(None), Job.provider_used != FALLBACK_PROVIDER),
        )
        .order_by(Job.last_failed_at.desc()).limit(MAX_BREAKER_ROWS)
    ).all()
    stamps.reverse()
    for index in range(len(stamps) - 1, need - 2, -1):
        if stamps[index] - stamps[index - need + 1] <= settings.retry_breaker_window_seconds:
            return now < stamps[index] + settings.retry_breaker_pause_seconds
    return False


def decide(db, settings, job, code, retry_after=None, *, now=None, rng=random.random, failover=False):
    """Seconds until the next attempt of `job` after failure `code`, or None for a final failure.

    `failover` means the failed attempt went to the main gateway and the reserve service is ready. The next
    attempt then goes to the reserve at once. It still counts as one attempt and obeys the hourly caps,
    but neither the pause table nor the breaker of the main gateway apply to it.
    """
    now = time.time() if now is None else now
    if not settings.ai_retries_enabled:
        return None
    kind = classify(code)
    if kind is None:
        return None
    if failover:
        if retries_in_last_hour(db, now, job.user_id) >= settings.retry_user_per_hour:
            return None
        if retries_in_last_hour(db, now) >= settings.retry_global_per_hour:
            return None
        return 0.0
    delay = backoff(settings, kind, job.attempts or 0, retry_after, rng=rng)
    if delay is None:
        return None
    if breaker_open(db, settings, now):
        return None
    if retries_in_last_hour(db, now, job.user_id) >= settings.retry_user_per_hour:
        return None
    if retries_in_last_hour(db, now) >= settings.retry_global_per_hour:
        return None
    return delay
