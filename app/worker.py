"""Durable dispatch of AI processing with a few careful automatic retries.

Every paid call is reserved and recorded before it is sent. A failure puts the same job back to `queued` with a
`not_before` time only when `app.retry_policy` allows it: clear gateway errors up to two retries, an ambiguous call
(it may have been billed) one retry, bad output one retry, everything else never. Retries are made here and nowhere
else, spaced out with jitter, capped per person and per deployment, and paused by a circuit breaker when the gateway
keeps failing. A retry takes the same job and capture, so it does not charge the daily units again. Assistant
requests (`app/assistant.py`) stay at-most-once.
"""

import signal
import threading
import time

from pydantic import ValidationError
from sqlalchemy import or_, select, update

from app import crypto
from app.ai_guard import BLOCKED_CODE, OutputGuard
from app.analytics import record_event
from app.assistant import run_assistant_once
from app.audio_contracts import AUDIO_MEDIA_TYPES, AudioReader, SpeechProvider
from app.audio_storage import AudioStorage
from app.config import Settings
from app.db import make_database
from app.error_logging import log_error
from app.gateway_budget import available as money_available
from app.models import (
    Capture,
    Category,
    Item,
    Job,
    Note,
    Outbox,
    ProviderBudget,
    TranscriptRevision,
    User,
    new_id,
)
from app.providers import ProviderError, make_provider, validate_result
from app.reminders import auto_reminders
from app.retry_policy import breaker_open, decide
from app.services import lock_account, resolve_ai_category, save_revision
from app.speech import CloudRuSpeechProvider, make_speech_provider
from app.usage import UsageRecorder

PROCESSING_FAILED_TEXT = (
    "Не получилось разобрать запись с ИИ. Исходник сохранён. "
    "Откройте beresta, в истории его можно отправить ещё раз или сохранить без ИИ."
)


def cancel_processing_reply(db, job_id):
    """The note reply will never come. Cancel it and tell the Telegram chat once, in the existing message outbox."""
    rows = db.scalars(select(Outbox).where(
        Outbox.job_id == job_id, Outbox.status.in_({"pending", "leased", "retryable"}),
    )).all()
    for row in rows:
        row.status, row.error_code = "cancelled", "processing_failed"
        db.add(Outbox(
            user_id=row.user_id, bot_id=row.bot_id, chat_id=row.chat_id, generation=1, status="pending",
            message_kind="processing_failed", message_text=PROCESSING_FAILED_TEXT,
        ))


class Worker:
    def __init__(
        self, sessions, settings: Settings, provider=None, *,
        audio_storage: AudioReader | None = None, speech_provider: SpeechProvider | None = None,
    ):
        self.sessions = sessions
        self.settings = settings
        self.provider = provider if provider is not None else make_provider(settings)
        self.audio_storage = audio_storage
        self.speech_provider = speech_provider
        self.usage = UsageRecorder(sessions, settings)
        self.guard = OutputGuard.for_runtime(settings, self.provider)

    def original_for_job(self, job_id):
        with self.sessions() as db:
            job = db.get(Job, job_id)
            if job.status != "running" or job.lease_until < time.time():
                raise ProviderError("execution_unknown")
            capture = db.get(Capture, job.capture_id)
            if capture.input_kind == "text":
                return capture.original_text
            if capture.input_kind != "audio":
                raise ProviderError("input_kind_invalid")
            if capture.transcript is not None:
                return self.validate_transcript(capture.transcript)
            key, media_type = capture.audio_key, capture.audio_media_type
        if self.speech_provider is None:
            raise ProviderError("stt_not_configured")
        if self.audio_storage is None:
            raise ProviderError("audio_storage_unavailable")
        if not key or media_type not in AUDIO_MEDIA_TYPES:
            raise ProviderError("audio_invalid")
        if isinstance(self.speech_provider, CloudRuSpeechProvider):
            if self.settings.provider != "cloudru" or not self.settings.allow_live_requests:
                raise ProviderError("stt_not_configured")
            with self.sessions() as db:
                # STT and LLM each consume a durable slot before their own dispatch.
                self.reserve_budget(db, db.get(Job, job_id).user_id)
        try:
            with self.audio_storage.open_original(key) as source:
                transcript = self.usage.call(
                    job_id, "stt", self.speech_provider,
                    lambda _: self.validate_transcript(self.speech_provider.transcribe(source, media_type=media_type)),
                )
        except OSError:
            raise ProviderError("audio_storage_unavailable") from None
        transcript = self.validate_transcript(transcript)
        with self.sessions() as db:
            # Serialize with lease expiration. A late STT result cannot start the LLM.
            active = db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running", Job.lease_until >= time.time())
                .values(status="running", lease_until=time.time() + self.settings.lease_seconds)
            ).rowcount
            if not active:
                raise ProviderError("execution_unknown")
            capture = db.get(Capture, job.capture_id)
            if capture.transcript is None:
                capture.transcript = transcript
                capture.original_text = transcript
                db.add(TranscriptRevision(capture_id=capture.id, version=capture.transcript_version,
                                          text=transcript, origin="stt", created_at=time.time()))
            else:
                transcript = self.validate_transcript(capture.transcript)
            # Keep the transcript even if the next stage fails or the process stops.
            db.commit()
        return transcript

    @staticmethod
    def validate_transcript(text):
        if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text) > 12000:
            raise ProviderError("stt_invalid_response")
        return text

    def record_failure(self, db, job_id, code, *, retry_after=None, expired_before=None, now=None):
        """Settle one failed attempt in the caller's transaction. Returns "retry", "failed" or None (nothing to do).

        The row is locked, so two workers never settle the same attempt twice. With `expired_before` the job must
        still hold a lease that ended before that time.
        """
        now = time.time() if now is None else now
        job = db.scalar(
            select(Job).where(Job.id == job_id).with_for_update().execution_options(populate_existing=True)
        )
        if job is None or job.status != "running":
            return None
        if expired_before is not None and (job.lease_until is None or job.lease_until >= expired_before):
            return None
        delay = decide(db, self.settings, job, code, retry_after, now=now)
        job.last_error_code, job.last_failed_at = code, now
        if delay is not None:
            job.status, job.lease_until = "queued", None
            job.attempts, job.not_before, job.error_code = (job.attempts or 0) + 1, now + delay, None
            return "retry"
        job.status, job.error_code, job.finished_at, job.not_before = "failed", code, now, None
        capture = db.get(Capture, job.capture_id)
        record_event(db, job.user_id, "processing_failed", job.id, capture.channel)
        cancel_processing_reply(db, job.id)
        return "failed"

    def claim(self):
        now = time.time()
        with self.sessions() as db:
            expired = db.scalars(select(Job.id).where(Job.status == "running", Job.lease_until < now)).all()
            for expired_id in expired:
                # A call that outlived its lease is an ambiguous failure, which the policy may retry once.
                outcome = self.record_failure(db, expired_id, "execution_unknown", expired_before=now, now=now)
                if outcome:
                    log_error(
                        "job_lease_expired" if outcome == "failed" else "job_lease_expired_retry",
                        "execution_unknown",
                        log_file=self.settings.error_log_file,
                        job_id=expired_id,
                    )
            waiting = (
                Job.status == "queued", Job.provider == self.settings.provider,
                or_(Job.not_before.is_(None), Job.not_before <= now),
            )
            ready = db.execute(
                select(Job.id, Job.attempts).where(*waiting).order_by(Job.created_at, Job.id).limit(1)
            ).first()
            if ready is not None and ready.attempts and breaker_open(db, self.settings, now):
                # The gateway is struggling. Retries wait, fresh jobs may still try once.
                ready = db.execute(
                    select(Job.id, Job.attempts).where(*waiting, Job.attempts == 0)
                    .order_by(Job.created_at, Job.id).limit(1)
                ).first()
            if ready is None:
                db.commit()
                return None
            claimed = db.execute(
                update(Job)
                .where(Job.id == ready.id, Job.status == "queued")
                .values(status="running", lease_until=now + self.settings.lease_seconds, not_before=None)
            ).rowcount
            db.commit()
            return ready.id if claimed else None

    def reserve_budget(self, db, user_id):
        if self.settings.gateway_budget_file:
            budget = db.scalar(select(ProviderBudget).where(ProviderBudget.id == "cloudru").with_for_update())
            if budget is None or not money_available(self.settings, budget.reserved_calls):
                db.rollback()
                raise ProviderError("budget_exhausted")
        global_ok = db.execute(
            update(ProviderBudget)
            .where(
                ProviderBudget.id == "cloudru",
                ProviderBudget.reserved_calls < self.settings.live_call_limit,
            )
            .values(reserved_calls=ProviderBudget.reserved_calls + 1)
        ).rowcount
        user_ok = db.execute(
            update(User)
            .where(
                User.id == user_id,
                User.live_calls < self.settings.live_user_call_limit,
            )
            .values(live_calls=User.live_calls + 1)
        ).rowcount
        if not (global_ok and user_ok):
            db.rollback()
            raise ProviderError("budget_exhausted")
        db.commit()  # Reservation survives crashes; an uncertain call still consumes a slot.

    def fail(self, job_id, code, *, exception=None, http_status=None, retry_after=None):
        with self.sessions() as db:
            outcome = self.record_failure(db, job_id, code, retry_after=retry_after)
            db.commit()
        if outcome:
            log_error(
                "job_failed" if outcome == "failed" else "job_retry_scheduled",
                code,
                log_file=self.settings.error_log_file,
                job_id=job_id,
                http_status=http_status,
                exception=exception,
            )

    def run_once(self):
        job_id = self.claim()
        if job_id is None:
            return run_assistant_once(self)  # Capture jobs always go first.
        try:
            original = self.original_for_job(job_id)
            with self.sessions() as db:
                job = db.get(Job, job_id)
                capture_id, user_id = job.capture_id, job.user_id
                category_context = {
                    category.name: category.id
                    for category in db.scalars(
                        select(Category).where(Category.user_id == user_id).order_by(Category.id).limit(100)
                    )
                }
                if self.settings.provider == "cloudru":
                    self.reserve_budget(db, user_id)
            def structure(on_usage):
                measured = getattr(self.provider, "structure_with_usage", None)
                result = (
                    measured(original, categories=tuple(category_context), on_usage=on_usage)
                    if measured else self.provider.structure(original, categories=tuple(category_context))
                )
                checked = validate_result(result, original)
                rule = self.guard.structured_violation(checked, original)
                if rule is None:
                    return checked
                # The answer repeated a secret, our instructions or a key-like string. Keep only what the person wrote.
                blocked.append(rule)
                log_error("ai_output_blocked", rule, log_file=self.settings.error_log_file, job_id=job_id)
                return self.guard.safe_note(original)

            blocked = []
            result = self.usage.call(job_id, "llm", self.provider, structure)
            now = time.time()
            with self.sessions() as db:
                lock_account(db, user_id)
                completed = db.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "running", Job.lease_until >= now)
                    .values(status="succeeded", finished_at=now,
                            error_code=BLOCKED_CODE if blocked else (
                                "ungrounded_details_omitted" if result._grounding_details_omitted else None))
                ).rowcount
                if not completed:
                    db.rollback()
                    self.fail(job_id, "execution_unknown")
                    return True
                note = Note(
                    id=new_id(),
                    capture_id=capture_id,
                    user_id=user_id,
                    title=result.title,
                    markdown=result.markdown,
                    version=1,
                    provider=self.settings.provider,
                    conclusions=[
                        {"id": new_id(), **c.model_dump(), "status": "proposed"} for c in result.conclusions
                    ],
                )
                if result.category_name:
                    category = resolve_ai_category(db, user_id, result.category_name, category_context)
                    note.category_id = category.id if category else None
                db.add(note)
                db.flush()
                for position, item in enumerate(result.items):
                    db.add(
                        Item(
                            user_id=user_id,
                            note_id=note.id,
                            kind=item.kind,
                            text=item.text,
                            source_quote=item.source_quote,
                            due_text=item.due_text,
                            due_at=None,
                            status="open",
                            version=1,
                            position=position,
                        )
                    )
                db.flush()
                save_revision(db, note)
                try:
                    # The note matters more than its reminders: a failure here leaves the note as it is.
                    with db.begin_nested():
                        auto_reminders(db, note, self.settings)
                except Exception as exc:
                    log_error("auto_reminder_failed", "internal_error", log_file=self.settings.error_log_file,
                              job_id=job_id, exception=exc)
                capture = db.get(Capture, capture_id)
                record_event(db, user_id, "processing_completed", job_id, capture.channel)
                db.commit()
        except ProviderError as exc:
            self.fail(job_id, exc.code, exception=exc, http_status=exc.http_status, retry_after=exc.retry_after)
        except ValidationError as exc:
            self.fail(job_id, "provider_invalid_response", exception=exc)
        except Exception as exc:
            self.fail(job_id, "internal_error", exception=exc)
        return True

    def budget_available(self):
        """Keep queued originals intact when the deployment's total call cap is spent."""
        if self.settings.provider != "cloudru":
            return True
        with self.sessions() as db:
            budget = db.get(ProviderBudget, "cloudru")
            return (budget is not None and budget.reserved_calls < self.settings.live_call_limit
                    and money_available(self.settings, budget.reserved_calls))


def main():
    settings = Settings.from_env()
    crypto.configure_from_settings(settings)
    engine, sessions = make_database(settings.database_url)
    stopping = threading.Event()
    def request_stop(signum, frame):
        stopping.set()
    previous_handlers = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        worker = Worker(sessions, settings, audio_storage=AudioStorage(settings.audio_storage_path, read_only=True),
                        speech_provider=make_speech_provider(settings))
        while not stopping.is_set():
            if not worker.budget_available():
                time.sleep(1)
                continue
            if not worker.run_once():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        engine.dispose()


if __name__ == "__main__":
    main()
