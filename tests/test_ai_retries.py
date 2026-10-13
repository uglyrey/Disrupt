"""Automatic retries of AI processing. Synthetic providers and HTTP substitutions only, no live service."""

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
from sqlalchemy import func, select, update

from app.assistant_retrieval import AssistantContext
from app.limits import units_used
from app.models import Capture, Job, Outbox, ProviderUsage
from app.providers import DEMO_TEXT, CloudRuProvider, MockProvider, ProviderError, parse_retry_after
from app.retry_policy import AMBIGUOUS, BAD_OUTPUT, CLEAR, backoff, breaker_open, classify
from app.worker import Worker
from tests.conftest import register
from tests.test_internal import message
from tests.test_notes import submit
from tests.test_reminders import reminders as reminders


class Script:
    """Provider that follows a list. An exception is raised, anything else means success."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def structure(self, text, *, categories=()):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return MockProvider().structure(text, categories=categories)


def fail(code, **extra):
    return ProviderError(code, **extra)


def make_job(client, key, text=DEMO_TEXT):
    response = submit(client, text, key)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def job_state(app, job_id):
    with app.state.sessions() as db:
        job = db.get(Job, job_id)
        return job.status, job.attempts, job.error_code, job.last_error_code, job.not_before


def due(app, job_id=None):
    """Move the clock past the wait of one job, or of every waiting job."""
    with app.state.sessions() as db:
        query = update(Job).where(Job.status == "queued", Job.not_before.is_not(None)).values(not_before=0)
        if job_id:
            query = query.where(Job.id == job_id)
        db.execute(query)
        db.commit()


def receipts(app):
    with app.state.sessions() as db:
        return sorted(db.scalars(select(ProviderUsage.request_id)))


# Classification

@pytest.mark.parametrize("code,kind", [
    ("provider_rate_limit", CLEAR), ("provider_unavailable", CLEAR), ("provider_connect_failed", CLEAR),
    ("stt_rate_limit", CLEAR), ("stt_unavailable", CLEAR), ("stt_connect_failed", CLEAR),
    ("provider_timeout_unknown", AMBIGUOUS), ("provider_connection_unknown", AMBIGUOUS),
    ("stt_timeout_unknown", AMBIGUOUS), ("stt_connection_unknown", AMBIGUOUS), ("execution_unknown", AMBIGUOUS),
    ("provider_invalid_response", BAD_OUTPUT), ("provider_incomplete_response", BAD_OUTPUT),
    ("ungrounded_quote", BAD_OUTPUT),
    ("provider_auth", None), ("provider_model_not_found", None), ("provider_bad_request", None),
    ("provider_conflict", None), ("provider_http_418", None), ("provider_response_too_large", None),
    ("budget_exhausted", None), ("stt_not_configured", None), ("stt_auth", None), ("stt_invalid_response", None),
    ("audio_invalid", None), ("audio_storage_unavailable", None), ("stt_audio_conversion_failed", None),
    ("input_kind_invalid", None), ("internal_error", None), ("anything_else", None),
])
def test_failure_classification(code, kind):
    assert classify(code) == kind


@pytest.mark.parametrize("status,code", [
    (400, "provider_bad_request"), (401, "provider_auth"), (403, "provider_auth"), (404, "provider_model_not_found"),
    (408, "provider_timeout_unknown"), (422, "provider_bad_request"), (429, "provider_rate_limit"),
    (500, "provider_unavailable"), (502, "provider_unavailable"), (503, "provider_unavailable"),
    (504, "provider_timeout_unknown"),
])
def test_http_status_to_class(status, code):
    assert CloudRuProvider._status_code(status) == code


def gateway(error):
    def transport(request):
        raise error(request) if callable(error) else error

    return CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(transport))


@pytest.mark.parametrize("raised,code", [
    (lambda request: httpx.ConnectError("synthetic", request=request), "provider_connect_failed"),
    (lambda request: httpx.ConnectTimeout("synthetic", request=request), "provider_connect_failed"),
    (lambda request: httpx.ReadTimeout("synthetic", request=request), "provider_timeout_unknown"),
    (lambda request: httpx.WriteTimeout("synthetic", request=request), "provider_timeout_unknown"),
    (lambda request: httpx.ReadError("synthetic", request=request), "provider_connection_unknown"),
    (lambda request: httpx.RemoteProtocolError("synthetic", request=request), "provider_connection_unknown"),
])
def test_connection_failures_are_told_apart(raised, code):
    with pytest.raises(ProviderError) as caught:
        gateway(raised).structure("Тест")
    assert caught.value.code == code
    with pytest.raises(ProviderError) as caught:
        gateway(raised).assistant("ask", AssistantContext(notes=[], items=[]), question="Тест")
    assert caught.value.code == code


def test_retry_after_is_read_from_a_429_only():
    def transport(request):
        return httpx.Response(429, headers={"Retry-After": "120"})

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(transport))
    with pytest.raises(ProviderError) as caught:
        provider.structure("Тест")
    assert caught.value.code == "provider_rate_limit" and caught.value.retry_after == 120

    def unavailable(request):
        return httpx.Response(503, headers={"Retry-After": "120"})

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(unavailable))
    with pytest.raises(ProviderError) as caught:
        provider.structure("Тест")
    assert caught.value.retry_after is None


def test_retry_after_parsing():
    now = 1_800_000_000
    assert parse_retry_after("30") == 30 and parse_retry_after(" 1.5 ") == 1.5
    assert parse_retry_after("-5") is None and parse_retry_after("") is None and parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    stamp = format_datetime(datetime.fromtimestamp(now + 90, timezone.utc), usegmt=True)
    assert parse_retry_after(stamp, now=now) == pytest.approx(90, abs=1)
    past = format_datetime(datetime.fromtimestamp(now - 90, timezone.utc), usegmt=True)
    assert parse_retry_after(past, now=now) is None


# Backoff

def test_backoff_follows_policy_with_jitter(app):
    settings = app.state.settings
    assert settings.retry_clear_delays == (60, 300) and settings.retry_ambiguous_delays == (120,)
    assert settings.retry_bad_output_delays == (60,)
    for rng, factor in ((lambda: 0.0, 0.8), (lambda: 0.5, 1.0), (lambda: 1.0, 1.2)):
        assert backoff(settings, CLEAR, 0, rng=rng) == pytest.approx(60 * factor)
        assert backoff(settings, CLEAR, 1, rng=rng) == pytest.approx(300 * factor)
        assert backoff(settings, AMBIGUOUS, 0, rng=rng) == pytest.approx(120 * factor)
        assert backoff(settings, BAD_OUTPUT, 0, rng=rng) == pytest.approx(60 * factor)
    assert backoff(settings, CLEAR, 2) is None and backoff(settings, AMBIGUOUS, 1) is None
    assert backoff(settings, BAD_OUTPUT, 1) is None and backoff(settings, None, 0) is None


def test_retry_after_only_lengthens_and_is_capped(app):
    settings = app.state.settings
    low = lambda: 0.0  # noqa: E731
    assert backoff(settings, CLEAR, 0, 10, rng=low) == pytest.approx(48)
    assert backoff(settings, CLEAR, 0, 600, rng=low) == 600
    assert backoff(settings, CLEAR, 0, 99999, rng=low) == settings.retry_after_max_seconds == 900
    assert backoff(settings, CLEAR, 0, 0, rng=low) == pytest.approx(48)


# Job life cycle

def test_clear_failure_is_retried_twice_then_fails(app, client):
    register(client)
    job_id = make_job(client, "k1")
    provider = Script(fail("provider_unavailable"), fail("provider_rate_limit"), fail("provider_unavailable"))
    worker = Worker(app.state.sessions, app.state.settings, provider)
    before = time.time()
    assert worker.run_once()
    status, attempts, error, last, not_before = job_state(app, job_id)
    assert (status, attempts, error, last) == ("queued", 1, None, "provider_unavailable")
    assert before + 48 <= not_before <= time.time() + 72
    view = client.get("/api/v1/jobs/" + job_id).json()
    assert view["status"] == "queued" and view["attempts"] == 1 and view["retry_at"] == not_before
    assert not worker.run_once() and provider.calls == 1  # Still waiting.
    due(app)
    assert worker.run_once()
    status, attempts, error, last, not_before = job_state(app, job_id)
    assert (status, attempts, error, last) == ("queued", 2, None, "provider_rate_limit")
    assert time.time() + 240 <= not_before <= time.time() + 361
    due(app)
    assert worker.run_once()
    assert job_state(app, job_id)[:4] == ("failed", 2, "provider_unavailable", "provider_unavailable")
    view = client.get("/api/v1/jobs/" + job_id).json()
    assert view["status"] == "failed" and view["retry_at"] is None and view["attempts"] == 2
    assert provider.calls == 3
    assert receipts(app) == [job_id + ":llm", job_id + ":llm:r1", job_id + ":llm:r2"]


def test_success_after_retry_makes_one_note_and_one_receipt_per_attempt(app, client):
    register(client)
    job_id = make_job(client, "k1")
    provider = Script(fail("provider_unavailable"))
    worker = Worker(app.state.sessions, app.state.settings, provider)
    assert worker.run_once()
    due(app)
    assert worker.run_once() and not worker.run_once()
    status, attempts, error, _, not_before = job_state(app, job_id)
    assert (status, attempts, error, not_before) == ("succeeded", 1, None, None)
    view = client.get("/api/v1/jobs/" + job_id).json()
    assert view["note_id"] and view["retry_at"] is None
    assert len(client.get("/api/v1/notes").json()) == 1
    with app.state.sessions() as db:
        rows = list(db.scalars(select(ProviderUsage).order_by(ProviderUsage.occurred_at)))
    assert [row.request_id for row in rows] == [job_id + ":llm", job_id + ":llm:r1"]
    assert [row.status for row in rows] == ["unknown", "succeeded"]  # A 5xx is still recorded as billing unknown
    from app.models import ComponentCall
    with app.state.sessions() as db:
        stages = db.scalars(select(ComponentCall).where(ComponentCall.component == "capture_worker")
                            .order_by(ComponentCall.occurred_at)).all()
        assert [row.operation_id for row in stages] == [job_id+":r0", job_id+":r1"]
        assert [row.status for row in stages] == ["failed", "succeeded"]


@pytest.mark.parametrize("first,second,kind", [
    ("provider_timeout_unknown", "provider_timeout_unknown", AMBIGUOUS),
    ("provider_connection_unknown", "provider_incomplete_response", AMBIGUOUS),
    ("provider_invalid_response", "provider_invalid_response", BAD_OUTPUT),
    ("provider_incomplete_response", "ungrounded_quote", BAD_OUTPUT),
    ("ungrounded_quote", "provider_timeout_unknown", BAD_OUTPUT),
])
def test_ambiguous_and_bad_output_get_one_retry(app, client, first, second, kind):
    register(client)
    job_id = make_job(client, "k1")
    provider = Script(fail(first), fail(second))
    worker = Worker(app.state.sessions, app.state.settings, provider)
    assert worker.run_once()
    status, attempts, _, last, not_before = job_state(app, job_id)
    assert (status, attempts, last) == ("queued", 1, first)
    wait = 120 if kind == AMBIGUOUS else 60
    assert time.time() + wait * 0.8 - 2 <= not_before <= time.time() + wait * 1.2 + 2
    due(app)
    assert worker.run_once()
    assert job_state(app, job_id)[:3] == ("failed", 1, second)
    assert provider.calls == 2


def test_a_clear_failure_after_an_ambiguous_retry_may_still_retry(app, client):
    register(client)
    job_id = make_job(client, "k1")
    worker = Worker(app.state.sessions, app.state.settings, Script(fail("provider_timeout_unknown"), fail("provider_unavailable")))
    assert worker.run_once()
    due(app)
    assert worker.run_once()
    assert job_state(app, job_id)[:3] == ("queued", 2, None)


def test_a_clear_retry_does_not_open_a_second_ambiguous_retry(app, client):
    register(client)
    job_id = make_job(client, "k1")
    worker = Worker(app.state.sessions, app.state.settings, Script(fail("provider_unavailable"), fail("provider_timeout_unknown")))
    assert worker.run_once()
    due(app)
    assert worker.run_once()
    assert job_state(app, job_id)[:3] == ("failed", 1, "provider_timeout_unknown")


@pytest.mark.parametrize("code", [
    "provider_auth", "provider_model_not_found", "provider_bad_request", "provider_conflict",
    "budget_exhausted", "provider_response_too_large", "audio_invalid", "stt_not_configured", "synthetic_new_code",
])
def test_other_failures_are_final(app, client, code):
    register(client)
    job_id = make_job(client, "k1")
    provider = Script(fail(code))
    assert Worker(app.state.sessions, app.state.settings, provider).run_once()
    assert job_state(app, job_id)[:3] == ("failed", 0, code)
    assert provider.calls <= 1 and receipts(app) in ([], [job_id + ":llm"])


def test_unexpected_exception_is_final(app, client):
    register(client)
    job_id = make_job(client, "k1")
    assert Worker(app.state.sessions, app.state.settings, Script(RuntimeError("synthetic"))).run_once()
    assert job_state(app, job_id)[:3] == ("failed", 0, "internal_error")


def test_switch_off_restores_single_attempt(app_factory):
    app = app_factory(ai_retries_enabled=False)
    with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(app) as client:
        register(client)
        job_id = make_job(client, "k1")
        provider = Script(fail("provider_unavailable"))
        assert Worker(app.state.sessions, app.state.settings, provider).run_once()
        assert job_state(app, job_id)[:3] == ("failed", 0, "provider_unavailable")


def test_claim_respects_not_before_and_keeps_fifo(app, client):
    register(client)
    first, second = make_job(client, "k1", "Первая заметка"), make_job(client, "k2", "Вторая заметка")
    worker = Worker(app.state.sessions, app.state.settings, MockProvider())
    with app.state.sessions() as db:
        db.get(Job, first).not_before = time.time() + 600
        db.commit()
    assert worker.claim() == second
    assert worker.claim() is None
    with app.state.sessions() as db:
        db.get(Job, second).status = "succeeded"
        db.get(Job, first).not_before = time.time() - 1
        db.commit()
    assert worker.claim() == first
    with app.state.sessions() as db:
        job = db.get(Job, first)
        assert job.status == "running" and job.not_before is None and job.lease_until > time.time()


def test_expired_lease_is_one_ambiguous_attempt_and_then_final(app, client):
    register(client)
    job_id = make_job(client, "k1")
    worker = Worker(app.state.sessions, app.state.settings, MockProvider())
    assert worker.claim() == job_id
    worker.usage.start(job_id, "llm", worker.provider)
    with app.state.sessions() as db:
        db.get(Job, job_id).lease_until = time.time() - 1
        db.commit()
    assert worker.claim() is None  # The retry waits about two minutes.
    status, attempts, error, last, not_before = job_state(app, job_id)
    assert (status, attempts, error, last) == ("queued", 1, None, "execution_unknown")
    assert not_before > time.time() + 90
    due(app)
    assert worker.claim() == job_id
    worker.usage.start(job_id, "llm", worker.provider)
    with app.state.sessions() as db:
        db.get(Job, job_id).lease_until = time.time() - 1
        db.commit()
    assert worker.claim() is None
    assert job_state(app, job_id)[:4] == ("failed", 1, "execution_unknown", "execution_unknown")
    assert receipts(app) == [job_id + ":llm", job_id + ":llm:r1"]


def test_late_result_after_lease_expiry_is_not_stored(app, client):
    register(client)
    job_id = make_job(client, "k1")

    class Slow:
        def structure(self, text, *, categories=()):
            with app.state.sessions() as db:
                db.get(Job, job_id).lease_until = time.time() - 1
                db.commit()
            other.claim()  # A second worker sweeps the expired lease while the call is still out.
            return MockProvider().structure(text, categories=categories)

    worker = Worker(app.state.sessions, app.state.settings, Slow())
    other = Worker(app.state.sessions, app.state.settings, MockProvider())
    assert worker.run_once()
    assert job_state(app, job_id)[:2] == ("queued", 1)
    assert client.get("/api/v1/notes").json() == []


# Daily units and the live budget

def test_retries_do_not_charge_units_again(app, client):
    account = register(client)
    job_id = make_job(client, "k1")
    with app.state.sessions() as db:
        used = units_used(db, app.state.settings, account["id"])
    assert used > 0
    worker = Worker(app.state.sessions, app.state.settings, Script(fail("provider_unavailable"), fail("provider_unavailable")))
    for _ in range(2):
        assert worker.run_once()
        due(app)
    assert worker.run_once()
    assert job_state(app, job_id)[0] == "succeeded"
    with app.state.sessions() as db:
        assert units_used(db, app.state.settings, account["id"]) == used
        assert db.scalar(select(func.count()).select_from(Capture)) == 1


def live_app(app_factory, **changes):
    return app_factory(
        provider="cloudru", allow_live_requests=True, cloudru_model="synthetic",
        live_call_limit=changes.pop("live_call_limit", 10), live_user_call_limit=changes.pop("live_user_call_limit", 10),
        **changes,
    )


def test_retry_spends_the_live_budget_and_stops_when_it_is_gone(app_factory):
    from fastapi.testclient import TestClient

    app = app_factory()
    with TestClient(app) as client:
        register(client)
        job_id = make_job(client, "k1")
        with app.state.sessions() as db:
            db.get(Job, job_id).provider = "cloudru"
            db.commit()
        settings = replace(
            app.state.settings, provider="cloudru", allow_live_requests=True, cloudru_model="synthetic",
            live_call_limit=1, live_user_call_limit=5,
        )
        provider = Script(fail("provider_unavailable"))
        worker = Worker(app.state.sessions, settings, provider)
        assert worker.run_once()
        assert job_state(app, job_id)[:2] == ("queued", 1)
        due(app)
        assert worker.run_once()
        assert job_state(app, job_id)[:3] == ("failed", 1, "budget_exhausted")
        assert provider.calls == 1  # The second attempt never reached the gateway.


def test_retry_uses_a_budget_slot_like_any_call(app_factory):
    from fastapi.testclient import TestClient

    from app.models import ProviderBudget

    app = app_factory()
    with TestClient(app) as client:
        register(client)
        job_id = make_job(client, "k1")
        with app.state.sessions() as db:
            db.get(Job, job_id).provider = "cloudru"
            db.commit()
        settings = replace(
            app.state.settings, provider="cloudru", allow_live_requests=True, cloudru_model="synthetic",
            live_call_limit=5, live_user_call_limit=5,
        )
        worker = Worker(app.state.sessions, settings, Script(fail("provider_unavailable")))
        assert worker.run_once()
        due(app)
        assert worker.run_once()
        with app.state.sessions() as db:
            assert db.get(ProviderBudget, "cloudru").reserved_calls == 2


# Caps

def test_per_user_hourly_cap(app_factory):
    from fastapi.testclient import TestClient

    app = app_factory(retry_user_per_hour=1)
    with TestClient(app) as client:
        register(client)
        first, second = make_job(client, "k1", "Первая заметка"), make_job(client, "k2", "Вторая заметка")
        worker = Worker(app.state.sessions, app.state.settings, Script(fail("provider_unavailable"), fail("provider_unavailable")))
        assert worker.run_once() and worker.run_once()
        assert job_state(app, first)[:2] == ("queued", 1)
        assert job_state(app, second)[:3] == ("failed", 0, "provider_unavailable")  # Over the cap: final, as before.


def test_cap_counts_retries_that_already_ran(app_factory):
    from fastapi.testclient import TestClient

    app = app_factory(retry_user_per_hour=1)
    with TestClient(app) as client:
        register(client)
        first, second = make_job(client, "k1", "Первая заметка"), make_job(client, "k2", "Вторая заметка")
        worker = Worker(app.state.sessions, app.state.settings, Script(fail("provider_unavailable"), "ok", fail("provider_unavailable")))
        assert worker.run_once()          # first fails, retry waits
        due(app)
        assert worker.run_once()          # the retry of first runs and succeeds (one retry receipt now)
        assert worker.run_once()          # second fails and the hour cap is already used
        assert job_state(app, first)[0] == "succeeded"
        assert job_state(app, second)[:3] == ("failed", 0, "provider_unavailable")


def test_global_hourly_cap_spans_users(app_factory):
    from fastapi.testclient import TestClient

    app = app_factory(retry_global_per_hour=1, retry_user_per_hour=5)
    with TestClient(app) as one, TestClient(app) as two:
        register(one, "first-user")
        register(two, "second-user")
        a, b = make_job(one, "k1", "Первая заметка"), make_job(two, "k1", "Вторая заметка")
        worker = Worker(app.state.sessions, app.state.settings, Script(fail("provider_unavailable"), fail("provider_unavailable")))
        assert worker.run_once() and worker.run_once()
        states = {job_state(app, a)[0], job_state(app, b)[0]}
        assert states == {"queued", "failed"}


# Circuit breaker

def test_breaker_trips_across_workers_and_only_fresh_jobs_try(app_factory):
    from fastapi.testclient import TestClient

    app = app_factory(retry_breaker_failures=3, retry_user_per_hour=50, retry_global_per_hour=50)
    with TestClient(app) as client:
        register(client)
        ids = [make_job(client, f"k{i}", f"Заметка номер {i}") for i in range(5)]
        # Two worker processes share only the database.
        one = Worker(app.state.sessions, app.state.settings, Script(*[fail("provider_unavailable")] * 5))
        two = Worker(app.state.sessions, app.state.settings, Script(*[fail("provider_unavailable")] * 5))
        with app.state.sessions() as db:
            assert not breaker_open(db, app.state.settings, time.time())
        assert one.run_once() and two.run_once() and one.run_once()
        assert [job_state(app, i)[:2] for i in ids[:3]] == [("queued", 1)] * 3
        with app.state.sessions() as db:
            assert breaker_open(db, app.state.settings, time.time())
        # The fourth job is fresh. It tries once and, with the breaker open, fails for good.
        assert two.run_once()
        assert job_state(app, ids[3])[:3] == ("failed", 0, "provider_unavailable")
        # Waiting retries stay put even though their time has come.
        due(app)
        assert one.claim() == ids[4]
        assert one.claim() is None
        assert [job_state(app, i)[0] for i in ids[:3]] == ["queued"] * 3


def test_breaker_closes_after_the_pause(app_factory):
    from fastapi.testclient import TestClient

    app = app_factory(retry_breaker_failures=2, retry_user_per_hour=50, retry_global_per_hour=50)
    with TestClient(app) as client:
        register(client)
        ids = [make_job(client, f"k{i}", f"Заметка номер {i}") for i in range(2)]
        worker = Worker(app.state.sessions, app.state.settings, Script(*[fail("provider_unavailable")] * 2))
        assert worker.run_once() and worker.run_once()
        due(app)
        assert worker.claim() is None
        with app.state.sessions() as db:
            now = time.time()
            assert breaker_open(db, app.state.settings, now)
            assert not breaker_open(db, app.state.settings, now + 601)
            assert not breaker_open(db, app.state.settings, now + 100000)
            db.execute(update(Job).values(last_failed_at=now - 700))
            db.commit()
            assert not breaker_open(db, app.state.settings, time.time())
        assert worker.claim() in ids


def test_breaker_ignores_failures_that_are_not_the_gateways(app_factory):
    from fastapi.testclient import TestClient

    app = app_factory(retry_breaker_failures=2)
    with TestClient(app) as client:
        register(client)
        for i in range(3):
            make_job(client, f"k{i}", f"Заметка номер {i}")
        worker = Worker(app.state.sessions, app.state.settings, Script(*[fail("provider_bad_request")] * 3))
        assert worker.run_once() and worker.run_once() and worker.run_once()
        with app.state.sessions() as db:
            assert not breaker_open(db, app.state.settings, time.time())


def test_breaker_needs_distinct_jobs_within_the_window(app_factory):
    from fastapi.testclient import TestClient

    app = app_factory(retry_breaker_failures=3, retry_breaker_window_seconds=60)
    with TestClient(app) as client:
        register(client)
        ids = [make_job(client, f"k{i}", f"Заметка номер {i}") for i in range(3)]
        now = time.time()
        with app.state.sessions() as db:
            for offset, job_id in zip((0, 30, 200), ids):
                job = db.get(Job, job_id)
                job.last_error_code, job.last_failed_at = "provider_unavailable", now - offset
            db.commit()
            assert not breaker_open(db, app.state.settings, now)  # Third failure is outside the 60 s window.
            db.get(Job, ids[2]).last_failed_at = now - 55
            db.commit()
            assert breaker_open(db, app.state.settings, now)


# Telegram

def test_telegram_failure_message_is_sent_only_on_the_final_outcome(reminders):

    app, client, _ = reminders
    saved = message(client, update_id=300).json()
    worker = Worker(app.state.sessions, app.state.settings, Script(*[fail("provider_unavailable")] * 3))

    def outbox():
        with app.state.sessions() as db:
            return [(row.message_kind, row.status) for row in db.scalars(select(Outbox).order_by(Outbox.created_at, Outbox.id))]

    assert worker.run_once()
    assert job_state(app, saved["job_id"])[:2] == ("queued", 1)
    assert outbox() == [(None, "pending")]
    due(app)
    assert worker.run_once()
    assert outbox() == [(None, "pending")]
    due(app)
    assert worker.run_once()
    assert job_state(app, saved["job_id"])[:3] == ("failed", 2, "provider_unavailable")
    assert sorted(outbox(), key=str) == [("processing_failed", "pending"), (None, "cancelled")]
    assert not worker.run_once()
    assert len(outbox()) == 2


def test_default_settings_are_documented_values(app):
    settings = app.state.settings
    assert settings.ai_retries_enabled is True
    assert (settings.retry_user_per_hour, settings.retry_global_per_hour) == (10, 60)
    assert (settings.retry_breaker_failures, settings.retry_breaker_window_seconds,
            settings.retry_breaker_pause_seconds) == (5, 300, 600)
    assert settings.retry_jitter == 0.2 and settings.retry_after_max_seconds == 900
    assert timedelta(seconds=settings.retry_after_max_seconds) == timedelta(minutes=15)


def test_retry_settings_come_from_the_environment_and_are_checked(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("NOTES_RETRY_CLEAR_DELAYS", "30, 90,600")
    monkeypatch.setenv("NOTES_RETRY_JITTER_PERCENT", "10")
    monkeypatch.setenv("NOTES_RETRY_USER_PER_HOUR", "3")
    monkeypatch.setenv("NOTES_AI_RETRIES_ENABLED", "false")
    settings = Settings.from_env()
    assert settings.retry_clear_delays == (30, 90, 600) and settings.retry_jitter == 0.1
    assert settings.retry_user_per_hour == 3 and settings.ai_retries_enabled is False
    for name, value in (
        ("NOTES_RETRY_CLEAR_DELAYS", "10,x"), ("NOTES_RETRY_CLEAR_DELAYS", "0"),
        ("NOTES_RETRY_CLEAR_DELAYS", "1,2,3,4"), ("NOTES_RETRY_AMBIGUOUS_DELAYS", "99999"),
        ("NOTES_RETRY_JITTER_PERCENT", "80"), ("NOTES_RETRY_USER_PER_HOUR", "-1"),
        ("NOTES_RETRY_BREAKER_FAILURES", "0"), ("NOTES_RETRY_AFTER_MAX_SECONDS", "99999"),
    ):
        with monkeypatch.context() as inner:
            inner.setenv(name, value)
            with pytest.raises(ValueError):
                Settings.from_env()
