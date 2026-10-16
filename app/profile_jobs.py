"""Worker side of the questionnaire «Знакомство»: answers in, facts and hints out.

The API only writes the answers and sets `user_profiles.state = pending` (app/profile.py). Here the worker takes one
such row, pays one AI unit, asks the model through the same provider stack as the notes (gateway, one repeat after an
unusable answer, one try on the reserve, budgets, receipts, OutputGuard) and stores the cleaned result. The row is the
queue: no separate table. See docs/profile-v1.md.
"""

import logging
import time

from sqlalchemy import select, update

from app.ai_guard import BLOCKED_CODE
from app.error_logging import log_error
from app.models import UserProfile
from app.profile import FAILED, PENDING, READY, clean_profile, fact_rows, output_texts, touch
from app.profile_questions import QUESTIONS
from app.providers import PROFILE_MAX_TOKENS, PROFILE_PROMPT, ProviderError
from app.services import lock_account
from app.time_ai import TimeResolver

# A failure with one of these codes happened before anything was sent, so the unit is given back.
FREE_CODES = frozenset({"budget_exhausted", "provider_connect_failed"})
KEPT_UNITS = 50  # How many paid moments one row remembers. Only today's matter for the daily limit.


class ProfileAnalyzer(TimeResolver):
    """The retry and reserve logic of the time resolver with the questionnaire call in the middle."""

    CHANNEL, STAGE = "web", "profile"

    @staticmethod
    def prompt():
        return PROFILE_PROMPT, PROFILE_MAX_TOKENS

    def analyze(self, user_id, operation_id, data, source):
        """(facts, prefs) from the model. Raises ProviderError."""
        self._source = source
        return self._resolve(self.worker, user_id, operation_id, data)

    def _call(self, worker, user_id, operation_id, data, attempt, suffix):
        provider = worker.fallback_provider if attempt.fallback else worker.provider
        call = getattr(provider, "profile_with_usage", None) or getattr(provider, "profile", None)
        if call is None:
            raise ProviderError("provider_unavailable")
        with worker.sessions() as db:
            if worker.settings.provider == "cloudru" and not attempt.fallback:
                worker.reserve_budget(db, user_id)
            receipt_id = self._start_receipt(db, operation_id, user_id, provider, attempt, suffix)
        started = time.monotonic()
        try:
            raw = call(data, on_usage=lambda values: worker.usage.tokens(receipt_id, values))
            facts, prefs = clean_profile(raw)
            if worker.guard.violation(output_texts(facts, prefs), self._source):
                logging.getLogger(__name__).warning("profile_output_blocked")
                raise ProviderError(BLOCKED_CODE)
        except (ValueError, TypeError, KeyError):
            self._finish(worker, receipt_id, started, "failed", attempt, ProviderError("provider_invalid_response"))
            raise ProviderError("provider_invalid_response") from None
        except Exception as exc:
            ambiguous = isinstance(exc, ProviderError) and (
                exc.code.endswith("_unknown") or exc.code in {"execution_unknown", "provider_unavailable"}
            )
            self._finish(worker, receipt_id, started, "unknown" if ambiguous else "failed", attempt, exc)
            raise
        self._finish(worker, receipt_id, started, "succeeded", attempt)
        return facts, prefs


def claim(worker):
    """(user_id, version, answers) of one pending profile whose unit is now paid, or None."""
    now = time.time()
    with worker.sessions() as db:
        for row in db.scalars(select(UserProfile).where(UserProfile.state == PENDING, UserProfile.lease_until < now)).all():
            # The outcome of the call is unknown and the unit may be spent, so the person tries again.
            lock_account(db, row.user_id)
            row.state, row.error_code, row.lease_until = FAILED, "execution_unknown", None
            touch(row)
            log_error("profile_lease_expired", "execution_unknown", log_file=worker.settings.error_log_file)
        user_id = db.scalar(
            select(UserProfile.user_id).where(UserProfile.state == PENDING, UserProfile.lease_until.is_(None))
            .order_by(UserProfile.updated_at, UserProfile.user_id).limit(1)
        )
        if user_id is None:
            db.commit()
            return None
        claimed = db.execute(
            update(UserProfile).where(
                UserProfile.user_id == user_id, UserProfile.state == PENDING, UserProfile.lease_until.is_(None),
            ).values(lease_until=now + worker.settings.lease_seconds)
        ).rowcount
        if not claimed:
            db.commit()
            return None
        db.expire_all()
        row = db.get(UserProfile, user_id)
        answers = dict(row.answers or {})
        version = row.version
        # From here the unit counts as spent: the moment is saved before the model is asked.
        row.unit_times = [*(row.unit_times or [])[-(KEPT_UNITS - 1):], now]
        db.commit()
        return user_id, version, answers, now


def model_input(answers):
    return {
        "questions": [{"id": qid, "text": text} for qid, text in QUESTIONS if qid in answers],
        "answers": answers,
    }


def settle(worker, user_id, version, charged_at, *, facts=None, prefs=None, code=None):
    """Store the result (or the failure) if the row still waits for this very run. Gives a free failure's unit back."""
    with worker.sessions() as db:
        lock_account(db, user_id)
        db.expire_all()
        row = db.get(UserProfile, user_id)
        if row is None or row.state != PENDING or row.version != version:
            db.rollback()
            return False  # The answers were deleted or replaced while the model worked: this result is stale.
        row.lease_until = None
        if code is None:
            row.state, row.facts, row.prefs, row.error_code = READY, fact_rows(facts), prefs, None
        else:
            row.state, row.error_code = FAILED, code
            if code in FREE_CODES:
                row.unit_times = [moment for moment in (row.unit_times or []) if moment != charged_at]
        touch(row)
        db.commit()
        return True


def run_profile_once(worker):
    """Process one pending profile. Returns False when nothing was waiting. Never raises."""
    taken = claim(worker)
    if taken is None:
        return False
    user_id, version, answers, charged_at = taken
    code = None
    try:
        result = ProfileAnalyzer(worker).analyze(
            user_id, f"profile:{user_id}:{version}", model_input(answers), "\n".join(answers.values()),
        )
    except ProviderError as exc:
        code = exc.code
        log_error("profile_failed", code, log_file=worker.settings.error_log_file, http_status=exc.http_status)
    except Exception as exc:
        code = "internal_error"
        log_error("profile_failed", code, log_file=worker.settings.error_log_file, exception=exc)
    try:
        if code is None:
            settle(worker, user_id, version, charged_at, facts=result[0], prefs=result[1])
        else:
            settle(worker, user_id, version, charged_at, code=code)
    except Exception as exc:
        log_error("profile_failed", "internal_error", log_file=worker.settings.error_log_file, exception=exc)
    return True
