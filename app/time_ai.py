"""One tiny AI call for a reminder time that our parser could not read from the person's written answer.

Used only by the Telegram answer to «Изменить время» (docs/reminder-confirm-v1.md). It goes through the same
provider stack as the notes: the main gateway first, one try on the reserve when the gateway does not answer,
one repeat after an unusable answer, the per-user live call limit and the daily money checks, a receipt per
call, and the output guard. It costs no AI unit of the person. It never raises: a failure is `None`.
"""

import json
import logging
import time
from decimal import Decimal

from pydantic import ValidationError

from app.ai_guard import BLOCKED_CODE
from app.error_logging import log_error
from app.fallback_budget import ESTIMATE_VERSION, PROMPT_OVERHEAD_CHARS, llm_max_cost
from app.models import ProviderUsage, User, new_id
from app.provider_router import FALLBACK, PRIMARY, Attempt
from app.providers import TIME_MAX_TOKENS, CloudRuProvider, MockProvider, ProviderError
from app.retry_policy import BAD_OUTPUT, GATEWAY_CODES, classify, routing_code
from app.services import lock_account

MAX_ANSWER_CHARS = 200


def model_name(provider):
    if type(provider) is MockProvider:
        return "mock"
    return provider.model_name if isinstance(provider, CloudRuProvider) else "injected"


class TimeResolver:
    """Built once per app. The heavy parts (worker, keys) are made on the first use, never at start-up."""

    def __init__(self, sessions, settings, provider=None, fallback_provider=None):
        self.sessions, self.settings = sessions, settings
        self.provider, self.fallback_provider = provider, fallback_provider
        self._worker = None
        self._failed = False

    @property
    def worker(self):
        if self._worker is None and not self._failed:
            from app.providers import make_fallback_provider
            from app.worker import Worker

            try:
                fallback = self.fallback_provider
                if fallback is None and self.provider is None:
                    fallback = make_fallback_provider(self.settings)
                self._worker = Worker(self.sessions, self.settings, self.provider, fallback_provider=fallback)
            except Exception as exc:
                self._failed = True
                log_error("time_ai_unavailable", "internal_error", log_file=self.settings.error_log_file, exception=exc)
        return self._worker

    def resolve(self, user_id, operation_id, data):
        """The local time `YYYY-MM-DDTHH:MM` the answer in `data` means, or None. Never raises."""
        worker = self.worker
        if worker is None:
            return None
        try:
            return self._resolve(worker, user_id, operation_id, data)
        except ProviderError as exc:
            log_error("time_ai_failed", exc.code, log_file=self.settings.error_log_file, http_status=exc.http_status)
        except Exception as exc:
            log_error("time_ai_failed", "internal_error", log_file=self.settings.error_log_file, exception=exc)
        return None

    def _resolve(self, worker, user_id, operation_id, data):
        from app.providers import TIME_PROMPT

        chars = len(TIME_PROMPT) + len(json.dumps(data, ensure_ascii=False)) + PROMPT_OVERHEAD_CHARS
        estimate = llm_max_cost(self.settings, chars, TIME_MAX_TOKENS) if worker.router is not None else None
        attempt = worker.begin_assistant_attempt(estimate)
        step, repeated, failed_over = 0, False, False
        while True:
            try:
                try:
                    return self._call(worker, user_id, operation_id, data, attempt, f":{step}")
                finally:
                    attempt.finish()
            except ProviderError as exc:
                step += 1
                on_reserve = attempt.fallback
                route_code = routing_code(exc.code, exc.http_status, FALLBACK if on_reserve else PRIMARY)
                if worker.router is not None and not on_reserve:
                    self._note_primary_failure(worker, route_code)
                if classify(route_code) == BAD_OUTPUT and not repeated:
                    repeated = True  # One repeat after an unusable answer, on the same route.
                    following = worker.begin_assistant_attempt(estimate, force_fallback=True) if on_reserve else Attempt()
                elif worker.router is not None and not on_reserve and not failed_over and route_code in GATEWAY_CODES:
                    failed_over = True  # The main gateway did not answer: one try on the reserve.
                    following = worker.begin_assistant_attempt(estimate, force_fallback=True)
                else:
                    following = None
                if following is None:
                    raise
                attempt = following

    @staticmethod
    def _note_primary_failure(worker, code):
        with worker.sessions() as db:
            worker.router.on_failure(db, PRIMARY, code, time.time())
            db.commit()

    def _call(self, worker, user_id, operation_id, data, attempt, suffix):
        provider = worker.fallback_provider if attempt.fallback else worker.provider
        call = getattr(provider, "resolve_time", None)
        if call is None:
            raise ProviderError("provider_unavailable")
        with worker.sessions() as db:
            if worker.settings.provider == "cloudru" and not attempt.fallback:
                worker.reserve_budget(db, user_id)
            receipt_id = self._start_receipt(db, operation_id, user_id, provider, attempt, suffix)
        started = time.monotonic()
        try:
            value = call(data, on_usage=lambda values: worker.usage.tokens(receipt_id, values))
            value = self._checked(value)
            if value is not None and worker.guard.violation([value], data.get("answer", "")):
                logging.getLogger(__name__).warning("time_ai_output_blocked")
                raise ProviderError(BLOCKED_CODE)
        except (ValidationError, ValueError, TypeError):
            self._finish(worker, receipt_id, started, "failed", attempt, ProviderError("provider_invalid_response"))
            raise ProviderError("provider_invalid_response") from None
        except Exception as exc:
            ambiguous = isinstance(exc, ProviderError) and (
                exc.code.endswith("_unknown") or exc.code in {"execution_unknown", "provider_unavailable"}
            )
            self._finish(worker, receipt_id, started, "unknown" if ambiguous else "failed", attempt, exc)
            raise
        self._finish(worker, receipt_id, started, "succeeded", attempt)
        return value

    @staticmethod
    def _checked(value):
        """The answer must be null or exactly YYYY-MM-DDTHH:MM. Anything else is an unusable answer."""
        if value is None:
            return None
        from app.schemas import LOCAL_MINUTE

        if not isinstance(value, str) or not LOCAL_MINUTE.fullmatch(value):
            raise ValueError("not a local minute")
        return value

    @staticmethod
    def _finish(worker, receipt_id, started, status, attempt, error=None):
        worker.usage.finish(receipt_id, started, status)
        if attempt.spend is not None:
            worker.usage.settle_stage(receipt_id, "llm", attempt.spend, error)

    @staticmethod
    def _start_receipt(db, operation_id, user_id, provider, attempt, suffix):
        """The receipt is saved before the call, so from here the live call slot counts as spent."""
        from app.provider_router import FALLBACK as FALLBACK_LABEL

        lock_account(db, user_id)
        mock = type(provider) is MockProvider
        fallback = attempt.spend is not None
        label = FALLBACK_LABEL if fallback else "mock" if mock else getattr(provider, "provider_label", None) or "injected"
        row = ProviderUsage(
            id=new_id(), user_id=user_id, operation_id=operation_id, request_id=f"{operation_id}:time{suffix}",
            channel="telegram", kind="llm", model=model_name(provider), provider=label,
            is_test=db.get(User, user_id).is_test,
            tariff_version=ESTIMATE_VERSION if fallback else "mock-no-billing-v1" if mock else None, status="unknown",
            estimated_cost=attempt.spend.estimates.get("llm") if fallback else None,
            cost=Decimal(0) if mock else None, input_tokens=0 if mock else None, output_tokens=0 if mock else None,
            cache_tokens=0 if mock else None, occurred_at=time.time(),
        )
        db.add(row)
        db.commit()
        return row.id
