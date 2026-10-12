"""One durable receipt per dispatched stage. Unknown billing is never inferred."""

import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.fallback_budget import ESTIMATE_VERSION, llm_cost, stt_cost
from app.models import Capture, Job, ProductEvent, ProviderUsage, User, new_id
from app.providers import CloudRuProvider, MockProvider, ProviderError
from app.retry_policy import CLEAR_CODES, FALLBACK_PROVIDER
from app.services import lock_account
from app.speech import CloudRuSpeechProvider, MockSpeechProvider


@dataclass(frozen=True)
class Tokens:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_tokens: int | None = None


def response_tokens(data):
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return Tokens()

    def counter(value):
        return value if type(value) is int and 0 <= value <= 2**31 - 1 else None

    prompt = counter(usage.get("prompt_tokens"))
    output = counter(usage.get("completion_tokens"))
    detail = usage.get("prompt_tokens_details")
    cache = counter(detail.get("cached_tokens")) if isinstance(detail, dict) else None
    if prompt is None or cache is not None and cache > prompt:
        cache = None
    return Tokens(prompt, output, cache)


def receipt_request_id(job, kind):
    """One receipt per real attempt. The first attempt keeps the old id, retry N adds `:rN`."""
    base = job.id + ":" + kind
    return base if not job.attempts else f"{base}:r{job.attempts}"


class UsageRecorder:
    def __init__(self, sessions, settings):
        self.sessions, self.settings = sessions, settings

    def start(self, job_id, kind, provider, *, spend=None):
        with self.sessions() as db:
            job = db.get(Job, job_id)
            lock_account(db, job.user_id)
            db.refresh(job)
            now = time.time()
            if job.status != "running" or job.lease_until < now:
                raise ProviderError("execution_unknown")
            capture = db.get(Capture, job.capture_id)
            event = db.scalar(select(ProductEvent).where(
                ProductEvent.user_id == job.user_id, ProductEvent.name == "capture_saved",
                ProductEvent.operation_id == capture.id,
            ))
            mock = type(provider) in {MockProvider, MockSpeechProvider}
            model = "mock" if mock else (
                provider.model_name if isinstance(provider, (CloudRuProvider, CloudRuSpeechProvider)) else "injected"
            )
            # Вызов резерва записывается с оценкой в долларах. Она заменит худший случай, когда ответ придёт.
            fallback = spend is not None
            label = FALLBACK_PROVIDER if fallback else (
                "mock" if mock else getattr(provider, "provider_label", None) or "injected"
            )
            row = ProviderUsage(
                id=new_id(), user_id=job.user_id, operation_id=job.id, request_id=receipt_request_id(job, kind),
                session_id=event.session_id if event else None, channel=capture.channel,
                is_test=db.get(User, job.user_id).is_test or bool(event and event.is_test),
                kind=kind, model=model, provider=label,
                tariff_version=ESTIMATE_VERSION if fallback else "mock-no-billing-v1" if mock else None,
                estimated_cost=spend.estimates.get(kind, Decimal(0)) if fallback else None,
                status="unknown", cost=Decimal(0) if mock else None,
                input_tokens=0 if mock and kind == "llm" else None,
                output_tokens=0 if mock and kind == "llm" else None,
                cache_tokens=0 if mock and kind == "llm" else None,
                stt_seconds=capture.audio_seconds if kind == "stt" else None, occurred_at=now,
            )
            db.add(row)
            db.commit()  # Receipt exists before any provider dispatch, including a process crash.
            return row.id

    def tokens(self, row_id, values):
        with self.sessions() as db:
            row = db.get(ProviderUsage, row_id)
            row.input_tokens, row.output_tokens, row.cache_tokens = (
                values.input_tokens, values.output_tokens, values.cache_tokens,
            )
            db.commit()

    def finish(self, row_id, started, status):
        with self.sessions() as db:
            row = db.get(ProviderUsage, row_id)
            row.status, row.latency_ms = status, max(0, int((time.monotonic() - started) * 1000))
            db.commit()

    def settle_stage(self, row_id, kind, spend, error=None):
        """Записать оценку расхода этапа, который шёл через резерв, и отдать её в резерв попытки."""
        with self.sessions() as db:
            row = db.get(ProviderUsage, row_id)
            actual = None
            if kind == "llm" and row.input_tokens is not None and row.output_tokens is not None:
                actual = llm_cost(self.settings, row.input_tokens, row.output_tokens)
            elif kind == "stt" and error is None and row.stt_seconds:
                actual = stt_cost(self.settings, row.stt_seconds)
            if actual is None:
                certain = isinstance(error, ProviderError) and (
                    error.code in CLEAR_CODES or (error.http_status is not None and not error.code.endswith("_unknown"))
                )
                # Неясно, списали ли деньги, поэтому остаётся худший случай. Известный отказ ничего не стоил.
                actual = Decimal(0) if certain else spend.estimates.get(kind, Decimal(0))
            row.estimated_cost = actual
            db.commit()
        spend.stage_done(kind, actual)

    def call(self, job_id, kind, provider, function, *, spend=None):
        row_id = self.start(job_id, kind, provider, spend=spend)
        started = time.monotonic()
        try:
            result = function(lambda values: self.tokens(row_id, values))
        except Exception as exc:
            ambiguous = isinstance(exc, ProviderError) and (
                exc.code.endswith("_unknown") or exc.code in {"execution_unknown", "provider_unavailable", "stt_unavailable"}
            )
            self.finish(row_id, started, "unknown" if ambiguous else "failed")
            if spend is not None:
                self.settle_stage(row_id, kind, spend, exc)
            raise
        self.finish(row_id, started, "succeeded")
        if spend is not None:
            self.settle_stage(row_id, kind, spend)
        return result
