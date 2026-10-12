"""Резервный ИИ: маршрутизация, потолок расхода, речь, помощник. Только подмена HTTP, ни один живой сервис не вызывается."""

import json
import time
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.ai_guard import BLOCKED_CODE, loaded_secrets
from app.fallback_budget import FallbackBudget, day_key, to_micro
from app.models import (
    AssistantRequest,
    Capture,
    FallbackSpend,
    Job,
    ProviderBudget,
    ProviderRoute,
    ProviderUsage,
    User,
)
from app.provider_router import FALLBACK, PRIMARY, ProviderRouter
from app.providers import DEMO_TEXT, CloudRuProvider, MockProvider, ProviderError, make_fallback_provider
from app.retry_policy import breaker_open
from app.speech import CloudRuSpeechProvider, MockSpeechProvider, make_fallback_speech_provider
from app.worker import Worker
from tests.conftest import register
from tests.test_assistant import add_note, post, read
from tests.test_audio_storage import wav

BASE = "http://10.77.0.2:18100/v1"
RELAY_TOKEN = "fallback-relay-token-for-tests-0123456789"
FALLBACK_SETTINGS = {"fallback_enabled": True, "fallback_base_url": BASE, "fallback_token": RELAY_TOKEN}
COST_OF_ONE_CALL = Decimal("0.0075")  # 1000 токенов на входе по 2,50 и 500 на выходе по 10 за миллион.


def chat(text=DEMO_TEXT, *, prompt=1000, completion=500, edit=None, content=None):
    note = MockProvider().structure(text).model_dump()
    note.update(edit or {})
    body = json.dumps(note, ensure_ascii=False) if content is None else content
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": body}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


class Relay:
    """Подмена посредника. Помнит запросы и отвечает по сценарию, потом всегда удачно."""

    def __init__(self, *answers, speech_text=DEMO_TEXT):
        self.requests, self.answers, self.speech_text = [], list(answers), speech_text

    def __call__(self, request):
        self.requests.append(request)
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": self.speech_text})
        return httpx.Response(200, json=chat())

    @property
    def calls(self):
        return len(self.requests)

    def provider(self):
        return CloudRuProvider(
            RELAY_TOKEN, "gpt-4o", base_url=BASE, transport=httpx.MockTransport(self), allowed_base_urls={BASE},
            label="openai",
        )

    def speech(self):
        return CloudRuSpeechProvider(
            RELAY_TOKEN, "whisper-1", base_url=BASE, transport=httpx.MockTransport(self), allowed_base_urls={BASE},
            label="openai",
        )


class Script:
    """Основной шлюз по сценарию. Исключение поднимается, всё остальное означает удачу."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls, self.during = list(outcomes), 0, None

    def structure(self, text, *, categories=()):
        self.calls += 1
        if self.during:
            self.during()
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return MockProvider().structure(text, categories=categories)

    def assistant(self, kind, context, *, question=None):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return MockProvider().assistant(kind, context, question=question)


def fail(code, **extra):
    return ProviderError(code, **extra)


class Env:
    def __init__(self, app, client, tmp_path):
        self.app, self.client, self.tmp_path = app, client, tmp_path
        self.counter = 0

    def job(self, text=DEMO_TEXT):
        self.counter += 1
        response = self.client.post(
            "/api/v1/captures/text", json={"text": text}, headers={"Idempotency-Key": f"job-{self.counter}"},
        )
        assert response.status_code == 202, response.text
        return response.json()["id"]

    def worker(self, primary, relay=None, speech=None, fallback_speech=None, **kwargs):
        return Worker(
            self.app.state.sessions, self.app.state.settings, primary,
            fallback_provider=relay.provider() if relay is not None else None, speech_provider=speech,
            fallback_speech=fallback_speech, **kwargs,
        )

    def row(self, job_id):
        with self.app.state.sessions() as db:
            job = db.get(Job, job_id)
            db.expunge(job)
            return job

    def receipts(self):
        with self.app.state.sessions() as db:
            rows = db.scalars(select(ProviderUsage).order_by(ProviderUsage.occurred_at, ProviderUsage.request_id))
            return list(rows)

    def route(self):
        with self.app.state.sessions() as db:
            row = db.get(ProviderRoute, PRIMARY)
            if row is None:
                return None
            db.expunge(row)
            return row

    def set_route(self, **values):
        with self.app.state.sessions() as db:
            row = db.get(ProviderRoute, PRIMARY)
            if row is None:
                db.add(ProviderRoute(id=PRIMARY, **values))
            else:
                for name, value in values.items():
                    setattr(row, name, value)
            db.commit()

    def committed(self):
        with self.app.state.sessions() as db:
            return db.scalar(select(FallbackSpend.committed_micro_usd)) or 0

    def events(self):
        path = self.tmp_path / "errors.log"
        if not path.exists():
            return []
        return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def env(app_factory, tmp_path):
    opened = []

    def build(**changes):
        settings = {**FALLBACK_SETTINGS, "error_log_file": str(tmp_path / "errors.log"), **changes}
        app = app_factory(**settings)
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        register(client)
        return Env(app, client, tmp_path)

    yield build
    for client in opened:
        client.__exit__(None, None, None)


# Настройки и клиенты резерва

def test_fallback_is_off_by_default_and_checks_its_settings(monkeypatch, tmp_path):
    from app.config import Settings

    assert Settings().fallback_enabled is False and Settings().fallback_daily_usd == Decimal("10")
    assert Settings().fallback_model == "gpt-4o" and Settings().fallback_stt_model == "whisper-1"
    assert Settings().fallback_price_input_per_m == Decimal("2.50")
    assert RELAY_TOKEN not in repr(Settings(**FALLBACK_SETTINGS))
    for bad in (
        {"fallback_base_url": ""}, {"fallback_base_url": "ftp://10.77.0.2/v1"},
        {"fallback_base_url": "http://openai.example/v1"}, {"fallback_base_url": "http://8.8.8.8:18100/v1"},
        {"fallback_base_url": "http://user:pass@10.77.0.2/v1"}, {"fallback_token": "short"},
        {"fallback_model": ""},
    ):
        with pytest.raises(ValueError):
            Settings(**{**FALLBACK_SETTINGS, **bad})
    Settings(**{**FALLBACK_SETTINGS, "fallback_base_url": "https://relay.example/v1"})
    with pytest.raises(ValueError):
        Settings(fallback_daily_usd=Decimal("-1"))
    with pytest.raises(ValueError):
        Settings(fallback_probe_seconds=5)
    token_file = tmp_path / "relay-token"
    token_file.write_text(RELAY_TOKEN + "\n", encoding="utf-8")
    for name, value in {
        "NOTES_FALLBACK_ENABLED": "true", "NOTES_FALLBACK_BASE_URL": BASE + "/",
        "NOTES_FALLBACK_TOKEN_FILE": str(token_file), "NOTES_FALLBACK_DAILY_USD": "7.5",
        "NOTES_FALLBACK_MODEL": "gpt-4o-2", "NOTES_FALLBACK_PROBE_SECONDS": "120",
    }.items():
        monkeypatch.setenv(name, value)
    loaded = Settings.from_env()
    assert loaded.fallback_enabled and loaded.fallback_token == RELAY_TOKEN and loaded.fallback_base_url == BASE
    assert loaded.fallback_daily_usd == Decimal("7.5") and loaded.fallback_model == "gpt-4o-2"
    assert loaded.fallback_probe_seconds == 120
    monkeypatch.setenv("NOTES_FALLBACK_TOKEN", RELAY_TOKEN)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_fallback_clients_exist_only_in_live_mode_with_the_switch_on():
    base = Settings_for_live()
    assert make_fallback_provider(replace(base, fallback_enabled=False)) is None
    assert make_fallback_provider(replace(base, provider="mock")) is None
    assert make_fallback_provider(replace(base, allow_live_requests=False)) is None
    provider = make_fallback_provider(base, transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    assert provider.provider_label == "openai" and provider.model_name == "gpt-4o"
    assert provider.endpoint == BASE + "/chat/completions"
    # Обычный клиент основного шлюза по-прежнему не принимает адрес посредника.
    with pytest.raises(ValueError):
        CloudRuProvider("fake-key", "model", base_url=BASE)
    speech = make_fallback_speech_provider(base)
    assert speech is None  # Распознавание речи выключено в окружении.


def test_fallback_speech_follows_the_main_speech_switch(monkeypatch):
    base = Settings_for_live()
    monkeypatch.setenv("NOTES_STT_ENABLED", "true")
    speech = make_fallback_speech_provider(base)
    assert speech.provider_label == "openai" and speech.model_name == "whisper-1"
    assert speech.endpoint == BASE + "/audio/transcriptions"
    assert make_fallback_speech_provider(replace(base, fallback_enabled=False)) is None


def Settings_for_live():
    from app.config import Settings

    return Settings(
        **FALLBACK_SETTINGS, provider="cloudru", allow_live_requests=True, cloudru_model="synthetic",
        live_call_limit=10, live_user_call_limit=10,
    )


def test_the_relay_token_is_a_guarded_secret(app):
    settings = replace(app.state.settings, **FALLBACK_SETTINGS)
    assert RELAY_TOKEN in loaded_secrets(settings)
    provider = Relay().provider()
    assert RELAY_TOKEN in loaded_secrets(None, None, provider)


# Запрос к посреднику

def test_fallback_request_goes_to_the_relay_with_the_same_prompts(env):
    context = env()
    relay, primary = Relay(), Script(fail("provider_unavailable"))
    worker = context.worker(primary, relay)
    job_id = context.job()
    assert worker.run_once() and worker.run_once()
    request = relay.requests[0]
    body = json.loads(request.content)
    assert str(request.url) == BASE + "/chat/completions"
    assert request.headers["authorization"] == "Bearer " + RELAY_TOKEN
    assert body["model"] == "gpt-4o" and body["max_tokens"] == 4000 and body["response_format"] == {"type": "json_object"}
    from app.providers import SYSTEM_PROMPT

    assert body["messages"][0]["content"] == SYSTEM_PROMPT
    assert json.loads(body["messages"][2]["content"]) == {"note_text": DEMO_TEXT}
    assert context.row(job_id).status == "succeeded"


# Перенос на резерв после сбоя

def test_gateway_failure_is_retried_on_the_fallback_at_once(env):
    context = env()
    relay, primary = Relay(), Script(fail("provider_unavailable"))
    worker = context.worker(primary, relay)
    job_id = context.job()
    before = time.time()
    assert worker.run_once()
    job = context.row(job_id)
    assert (job.status, job.attempts, job.last_error_code) == ("queued", 1, "provider_unavailable")
    assert job.next_route == FALLBACK and job.provider_used == PRIMARY and job.last_error_provider == PRIMARY
    assert job.not_before <= time.time() + 1 and job.not_before >= before - 1  # Без обычной паузы в минуту.
    assert relay.calls == 0
    assert worker.run_once()  # Следующий шаг сразу, без due().
    job = context.row(job_id)
    assert (job.status, job.attempts, job.provider_used, job.next_route) == ("succeeded", 1, FALLBACK, None)
    assert primary.calls == 1 and relay.calls == 1  # Один сервис на попытку, два сервиса не работают вместе.
    first, second = context.receipts()
    assert (first.request_id, first.provider) == (job_id + ":llm", "injected")
    assert (second.request_id, second.provider, second.model) == (job_id + ":llm:r1", "openai", "gpt-4o")
    assert (second.input_tokens, second.output_tokens, second.status) == (1000, 500, "succeeded")
    assert second.estimated_cost == COST_OF_ONE_CALL and second.tariff_version == "openai-estimate-v1"
    assert context.committed() == to_micro(COST_OF_ONE_CALL)  # Резерв заменён настоящей оценкой.
    assert len(context.client.get("/api/v1/notes").json()) == 1


@pytest.mark.parametrize("code", [
    "provider_rate_limit", "provider_unavailable", "provider_connect_failed",
    "provider_timeout_unknown", "provider_connection_unknown",
])
def test_every_gateway_class_error_fails_over(env, code):
    context = env()
    relay = Relay()
    worker = context.worker(Script(fail(code)), relay)
    job_id = context.job()
    assert worker.run_once() and worker.run_once()
    assert context.row(job_id).status == "succeeded" and relay.calls == 1


@pytest.mark.parametrize("code", [
    "provider_auth", "provider_bad_request", "provider_model_not_found", "provider_conflict", "budget_exhausted",
    "provider_response_too_large", "audio_invalid", "stt_not_configured", "internal_error", "synthetic_new_code",
])
def test_non_gateway_errors_never_reach_the_fallback(env, code):
    context = env()
    relay = Relay()
    worker = context.worker(Script(fail(code)), relay)
    job_id = context.job()
    assert worker.run_once()
    job = context.row(job_id)
    assert (job.status, job.error_code, job.next_route) == ("failed", code, None)
    assert relay.calls == 0 and context.committed() == 0
    assert context.route() is None or context.route().state == "up"


@pytest.mark.parametrize("code", ["provider_invalid_response", "provider_incomplete_response", "ungrounded_quote"])
def test_bad_output_follows_the_normal_policy_on_the_same_provider(env, code):
    context = env()
    relay, primary = Relay(), Script(fail(code))
    worker = context.worker(primary, relay)
    job_id = context.job()
    assert worker.run_once()
    job = context.row(job_id)
    assert (job.status, job.attempts, job.next_route) == ("queued", 1, PRIMARY)
    assert job.not_before > time.time() + 40  # Обычная пауза около минуты.
    assert not worker.run_once()
    with context.app.state.sessions() as db:
        db.execute(update(Job).values(not_before=0))
        db.commit()
    assert worker.run_once()
    assert context.row(job_id).status == "succeeded" and primary.calls == 2 and relay.calls == 0
    row = context.route()
    assert row is None or (row.state, row.failures) == ("up", 0)  # Шлюз ответил, плох был только ответ.


@pytest.mark.parametrize("changes", [
    {"retry_user_per_hour": 0}, {"retry_global_per_hour": 0}, {"ai_retries_enabled": False},
])
def test_failover_obeys_the_hourly_retry_caps_and_the_retry_switch(env, changes):
    context = env(**changes)
    relay = Relay()
    worker = context.worker(Script(fail("provider_unavailable")), relay)
    job_id = context.job()
    assert worker.run_once()
    job = context.row(job_id)
    assert (job.status, job.error_code) == ("failed", "provider_unavailable")
    assert relay.calls == 0


def test_failover_counts_as_one_attempt_and_is_bounded(env):
    context = env(retry_clear_delays=(60, 300, 300))
    relay = Relay(httpx.Response(503), httpx.Response(503), httpx.Response(503), httpx.Response(503))
    primary = Script(*[fail("provider_unavailable")] * 6)
    worker = context.worker(primary, relay)
    job_id = context.job()
    for _ in range(12):
        worker.run_once()
        with context.app.state.sessions() as db:
            db.execute(update(Job).where(Job.status == "queued").values(not_before=0))
            db.commit()
    job = context.row(job_id)
    assert job.status == "failed" and job.attempts <= 4
    assert primary.calls + relay.calls <= 6


# Состояние шлюза

def test_two_failures_in_a_row_mark_the_gateway_down_and_new_jobs_skip_it(env):
    context = env()
    relay = Relay()
    primary = Script(fail("provider_unavailable"), fail("provider_rate_limit"))
    worker = context.worker(primary, relay)
    first, second = context.job("Первая"), context.job("Вторая")
    assert worker.run_once() and context.route().state == "up" and context.route().failures == 1
    assert worker.run_once()  # Первое задание уже на резерве. Вторая запись ждёт своей очереди.
    assert context.row(first).status == "succeeded"
    assert worker.run_once()  # Вторая запись, второй сбой основного.
    assert context.route().state == "down" and context.route().failures == 2
    assert "provider_down" in context.events()
    assert worker.run_once() and context.row(second).status == "succeeded"
    third = context.job("Третья")
    calls_before = primary.calls
    assert worker.run_once()
    assert context.row(third).status == "succeeded" and context.row(third).provider_used == FALLBACK
    assert primary.calls == calls_before  # Пока шлюз упал, новое задание на него не идёт.


def test_failures_far_apart_do_not_add_up(env):
    context = env()
    worker = context.worker(Script(fail("provider_unavailable"), fail("provider_unavailable")), Relay())
    context.job("Первая")
    context.job("Вторая")
    assert worker.run_once()
    with context.app.state.sessions() as db:
        db.execute(update(ProviderRoute).values(last_failure_at=time.time() - 3600))
        db.commit()
    assert worker.run_once() and worker.run_once()
    row = context.route()
    assert (row.state, row.failures) == ("up", 1)


def test_the_gateway_breaker_also_marks_it_down(env):
    context = env(retry_breaker_failures=2, fallback_failures_to_down=9)
    worker = context.worker(Script(fail("provider_unavailable"), fail("provider_unavailable")), Relay())
    context.job("Первая")
    context.job("Вторая")
    assert worker.run_once() and context.route().state == "up"
    assert worker.run_once()  # Это уже первое задание на резерве, сбой ещё один.
    assert worker.run_once()
    assert context.route().state == "down" and context.route().failures == 2 < 9


def test_failures_on_the_fallback_do_not_touch_the_gateway_state_or_its_breaker(env):
    context = env(retry_breaker_failures=2)
    relay = Relay(httpx.Response(502), httpx.Response(502), httpx.Response(502))
    with_state = context.worker(Script(fail("provider_unavailable")), relay)
    job_id = context.job()
    assert with_state.run_once() and with_state.run_once()
    job = context.row(job_id)
    assert (job.status, job.last_error_provider) == ("queued", FALLBACK)
    row = context.route()
    assert (row.state, row.failures) == ("up", 1)
    with context.app.state.sessions() as db:
        assert breaker_open(db, context.app.state.settings, time.time()) is False


def test_a_success_on_the_main_gateway_resets_the_failure_count(env):
    context = env()
    worker = context.worker(Script(fail("provider_unavailable"), "ok", fail("provider_unavailable")), Relay())
    for index in range(3):
        context.job(f"Запись {index}")
    for _ in range(6):
        worker.run_once()
    row = context.route()
    assert (row.state, row.failures) == ("up", 1)


def go_down(context, *, probe_due=True):
    now = time.time()
    context.set_route(
        state="down", failures=2, last_failure_at=now, changed_at=now - 600,
        next_probe_at=now - 1 if probe_due else now + 300, probe_until=None,
    )


def test_while_down_one_job_probes_the_gateway_and_success_brings_it_back(env):
    context = env()
    relay, primary = Relay(), Script()
    worker = context.worker(primary, relay)
    go_down(context)
    first, second = context.job("Первая"), context.job("Вторая")
    assert worker.run_once()
    assert context.row(first).provider_used == PRIMARY and primary.calls == 1 and relay.calls == 0  # Это проба.
    row = context.route()
    assert (row.state, row.failures, row.probe_until, row.next_probe_at) == ("up", 0, None, None)
    assert "provider_up" in context.events()
    assert worker.run_once()
    assert context.row(second).provider_used == PRIMARY and primary.calls == 2  # Всё снова на основном.


def test_a_failed_probe_keeps_it_down_and_the_job_moves_to_the_fallback_without_waiting(env):
    context = env()
    relay, primary = Relay(), Script(fail("provider_timeout_unknown"))
    worker = context.worker(primary, relay)
    go_down(context)
    first = context.job("Первая")
    started = time.time()
    assert worker.run_once()
    job = context.row(first)
    assert (job.status, job.next_route) == ("queued", FALLBACK) and job.not_before <= time.time() + 1
    row = context.route()
    assert row.state == "down" and row.probe_until is None and row.next_probe_at > started + 200
    assert worker.run_once() and context.row(first).provider_used == FALLBACK
    second = context.job("Вторая")
    assert worker.run_once()
    assert context.row(second).provider_used == FALLBACK and primary.calls == 1  # Следующая проба позже.


def test_two_workers_never_probe_at_the_same_time(env):
    context = env()
    relay_a, relay_b = Relay(), Relay()
    other = context.worker(Script(), relay_b)
    go_down(context)
    first, second = context.job("Первая"), context.job("Вторая")
    seen = {}

    def second_worker_meanwhile():
        # Пока проба первого воркера в пути, второй берёт другое задание.
        assert other.run_once()
        seen["job"] = context.row(second)

    primary = Script()
    primary.during = second_worker_meanwhile
    worker = context.worker(primary, relay_a)
    assert worker.run_once()
    assert primary.calls == 1 and relay_b.calls == 1 and relay_a.calls == 0
    assert seen["job"].provider_used == FALLBACK and context.row(first).provider_used == PRIMARY
    assert context.row(second).status == "succeeded"


def test_probe_claim_goes_to_exactly_one_router(env):
    context = env()
    go_down(context)
    settings = context.app.state.settings
    routers = [ProviderRouter(context.app.state.sessions, settings) for _ in range(5)]
    answers = [router.choose() for router in routers]
    assert answers.count((PRIMARY, True)) == 1 and answers.count((FALLBACK, False)) == 4
    assert routers[0].choose(PRIMARY) == (PRIMARY, False) and routers[0].choose(FALLBACK) == (FALLBACK, False)
    row = context.route()
    assert row.probe_until > time.time() and row.next_probe_at > time.time() + 200


def test_a_probe_that_never_finishes_blocks_the_next_one_only_until_its_lease_ends(env):
    context = env()
    go_down(context)
    router = ProviderRouter(context.app.state.sessions, context.app.state.settings)
    assert router.choose() == (PRIMARY, True)
    context.set_route(next_probe_at=time.time() - 1)  # Интервал прошёл, но проба ещё держит аренду.
    assert router.choose() == (FALLBACK, False)
    context.set_route(probe_until=time.time() - 1)
    assert router.choose() == (PRIMARY, True)


def test_retries_waiting_during_an_outage_go_to_the_fallback_too(env):
    context = env()
    relay = Relay()
    worker = context.worker(Script(), relay)
    job_id = context.job()
    with context.app.state.sessions() as db:
        db.execute(update(Job).values(attempts=1, not_before=0, last_error_code="provider_unavailable",
                                      last_failed_at=time.time(), last_error_provider=PRIMARY, provider_used=PRIMARY))
        db.commit()
    go_down(context, probe_due=False)
    assert worker.run_once()
    assert context.row(job_id).provider_used == FALLBACK and relay.calls == 1


def test_the_breaker_pause_does_not_hold_retries_while_a_fallback_is_available(env):
    context = env(retry_breaker_failures=2)
    now = time.time()
    first, second, waiting = context.job("а"), context.job("б"), context.job("в")
    with context.app.state.sessions() as db:
        for job_id in (first, second):
            db.execute(update(Job).where(Job.id == job_id).values(
                status="failed", error_code="provider_unavailable", last_error_code="provider_unavailable",
                last_failed_at=now, last_error_provider=PRIMARY))
        db.execute(update(Job).where(Job.id == waiting).values(attempts=1, not_before=0, created_at=now + 1))
        db.commit()
    go_down(context, probe_due=False)
    with context.app.state.sessions() as db:
        assert breaker_open(db, context.app.state.settings, time.time()) is True
    relay = Relay()
    worker = context.worker(Script(), relay)
    assert worker.run_once() and relay.calls == 1
    # Без резерва та же картина оставляет повтор ждать, как и раньше.
    plain = Worker(context.app.state.sessions, context.app.state.settings, Script())
    other = context.job("г")
    with context.app.state.sessions() as db:
        db.execute(update(Job).where(Job.id == other).values(attempts=1, not_before=0, created_at=now + 2))
        db.commit()
    assert plain.claim() is None


# Потолок расхода

def test_daily_cap_reserves_before_the_call_and_settles_after(app):
    budget = FallbackBudget(app.state.sessions, replace(app.state.settings, fallback_daily_usd=Decimal("0.10")))
    now = time.time()
    first = budget.reserve(Decimal("0.06"), now)
    assert first is not None
    assert budget.reserve(Decimal("0.06"), now) is None  # Вместе вышло бы больше потолка.
    budget.settle(first, Decimal("0.01"))
    second = budget.reserve(Decimal("0.06"), now)
    assert second is not None
    with app.state.sessions() as db:
        assert budget.spent(db, now) == to_micro("0.07")
    budget.settle(first, Decimal(0))  # Повторный возврат не уводит сумму ниже нуля.
    budget.settle(second, Decimal(0))
    with app.state.sessions() as db:
        assert budget.spent(db, now) == 0
    assert budget.reserve(Decimal("0.11"), now) is None  # Один вызов дороже потолка не пройдёт никогда.


def test_cap_counts_every_reservation_together(app):
    budget = FallbackBudget(app.state.sessions, replace(app.state.settings, fallback_daily_usd=Decimal("0.10")))
    now = time.time()
    taken = [budget.reserve(Decimal("0.03"), now) for _ in range(8)]
    assert sum(item is not None for item in taken) == 3
    with app.state.sessions() as db:
        assert budget.spent(db, now) == to_micro("0.09") and budget.exhausted(db, now) is False


def test_the_cap_resets_at_midnight_in_the_limit_time_zone(app):
    settings = replace(app.state.settings, fallback_daily_usd=Decimal("0.10"))
    budget = FallbackBudget(app.state.sessions, settings)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    moscow = ZoneInfo("Europe/Moscow")
    late = datetime(2026, 10, 12, 23, 59, 30, tzinfo=moscow).timestamp()
    early = datetime(2026, 10, 13, 0, 0, 30, tzinfo=moscow).timestamp()
    assert day_key(settings, late) == "2026-10-12" and day_key(settings, early) == "2026-10-13"
    assert budget.reserve(Decimal("0.09"), late) is not None
    assert budget.reserve(Decimal("0.05"), late) is None
    assert budget.reserve(Decimal("0.05"), early) is not None  # Новые сутки, новый счёт.
    with app.state.sessions() as db:
        assert budget.spent(db, late) == to_micro("0.09") and budget.spent(db, early) == to_micro("0.05")


def test_a_reached_cap_behaves_like_no_fallback_and_is_logged(env):
    context = env(fallback_daily_usd=Decimal("0.001"))
    relay, primary = Relay(), Script(fail("provider_unavailable"), fail("provider_unavailable"))
    worker = context.worker(primary, relay)
    job_id = context.job()
    before = time.time()
    assert worker.run_once()
    job = context.row(job_id)
    # Как раньше: обычная пауза около минуты, резерв не вызывается.
    assert (job.status, job.attempts, job.next_route) == ("queued", 1, None) and job.not_before > before + 40
    assert relay.calls == 0
    go_down(context)
    context.set_route(next_probe_at=time.time() + 300)
    with context.app.state.sessions() as db:
        db.execute(update(Job).values(not_before=0))
        db.commit()
    assert worker.run_once()  # Шлюз «упал», но резерв закрыт потолком, задание идёт на основной, как сегодня.
    assert relay.calls == 0 and context.row(job_id).provider_used == PRIMARY


def test_the_cap_is_checked_before_each_call_when_the_day_runs_out(env):
    # Наибольшая цена записи около 0,057 доллара, настоящая 0,0075. Две записи помещаются, третья уже нет.
    context = env(fallback_daily_usd=Decimal("0.07"))
    go_down(context, probe_due=False)
    relay, primary = Relay(), Script()
    worker = context.worker(primary, relay)
    for index in range(6):
        context.job(f"Запись {index}")
    for _ in range(6):
        worker.run_once()
    assert relay.calls == 2 and primary.calls == 4  # Когда место кончилось, записи пошли на основной шлюз.
    assert context.committed() == 2 * to_micro(COST_OF_ONE_CALL) <= to_micro("0.07")
    assert "fallback_cap_reached" in context.events()


def test_a_known_failure_gives_the_reservation_back_and_an_unknown_one_keeps_it(env):
    context = env()
    relay = Relay(httpx.Response(503), httpx.ReadTimeout("slow"))
    worker = context.worker(Script(fail("provider_unavailable")), relay)
    go_down(context, probe_due=False)
    first = context.job("Первая")
    assert worker.run_once()
    assert context.row(first).status == "queued" and context.committed() == 0
    sent = context.receipts()[0]
    assert (sent.provider, sent.estimated_cost) == ("openai", 0)
    second = context.job("Вторая")
    with context.app.state.sessions() as db:
        db.execute(update(Job).where(Job.id == first).values(status="failed"))
        db.commit()
    assert worker.run_once()
    unknown = [row for row in context.receipts() if row.request_id.startswith(second)][0]
    assert unknown.status == "unknown" and unknown.estimated_cost > 0  # Неизвестно, списали ли деньги.
    assert context.committed() == to_micro(unknown.estimated_cost)


def test_the_live_gateway_budget_is_not_spent_by_fallback_calls(app_factory, tmp_path):
    app = app_factory()
    with TestClient(app) as client:
        register(client)
        settings = replace(
            app.state.settings, **FALLBACK_SETTINGS, provider="cloudru", allow_live_requests=True,
            cloudru_model="synthetic", live_call_limit=5, live_user_call_limit=5,
            error_log_file=str(tmp_path / "errors.log"),
        )
        response = client.post("/api/v1/captures/text", json={"text": DEMO_TEXT}, headers={"Idempotency-Key": "k"})
        job_id = response.json()["id"]
        with app.state.sessions() as db:
            db.get(Job, job_id).provider = "cloudru"
            db.commit()
        relay = Relay()
        worker = Worker(app.state.sessions, settings, Script(fail("provider_unavailable")),
                        fallback_provider=relay.provider())
        assert worker.run_once() and worker.run_once()
        assert relay.calls == 1
        with app.state.sessions() as db:
            assert db.get(Job, job_id).status == "succeeded"
            assert db.get(ProviderBudget, "cloudru").reserved_calls == 1  # Только попытка основного шлюза.
            assert db.scalar(select(User.live_calls)) == 1


# Ответ резерва проходит те же проверки

def test_fallback_output_goes_through_the_guard_and_the_quote_checks(env):
    context = env()
    leaked = chat(edit={"markdown": "Токен посредника " + RELAY_TOKEN})
    relay = Relay(httpx.Response(200, json=leaked))
    worker = context.worker(Script(fail("provider_unavailable")), relay)
    job_id = context.job()
    assert worker.run_once() and worker.run_once()
    job = context.row(job_id)
    assert (job.status, job.error_code) == ("succeeded", BLOCKED_CODE)
    note = context.client.get("/api/v1/notes/" + job_note(context, job_id)).json()
    assert RELAY_TOKEN not in json.dumps(note, ensure_ascii=False)
    assert note["markdown"] == DEMO_TEXT  # Осталось то, что написал человек.


def test_fallback_answer_with_a_made_up_quote_is_dropped_like_from_the_main_gateway(env):
    context = env()
    made_up = chat(edit={"conclusions": [{"text": "Вывод", "source_quote": "этого нет в записи"}]})
    relay = Relay(httpx.Response(200, json=made_up))
    worker = context.worker(Script(fail("provider_unavailable")), relay)
    job_id = context.job()
    assert worker.run_once() and worker.run_once()
    job = context.row(job_id)
    assert job.status == "succeeded" and job.error_code == "ungrounded_details_omitted"
    note = context.client.get("/api/v1/notes/" + job_note(context, job_id)).json()
    assert note["conclusions"] == []


def test_fallback_answers_that_are_not_json_follow_the_bad_output_policy(env):
    context = env()
    relay = Relay(httpx.Response(200, json=chat(content="это не json")), httpx.Response(200, json=chat()))
    worker = context.worker(Script(fail("provider_unavailable")), relay)
    job_id = context.job()
    assert worker.run_once() and worker.run_once()
    # Перенос на резерв уже занял единственный повтор, поэтому плохой ответ окончателен, как и у основного шлюза.
    job = context.row(job_id)
    assert (job.status, job.error_code, job.attempts) == ("failed", "provider_invalid_response", 1)
    assert relay.calls == 1
    row = context.route()
    assert (row.state, row.failures) == ("up", 1)  # Плохой ответ резерва основной шлюз не касается.


def job_note(context, job_id):
    return context.client.get("/api/v1/jobs/" + job_id).json()["note_id"]


# Речь

def audio_job(context):
    response = context.client.post(
        "/api/v1/captures/audio", headers={"Idempotency-Key": "voice-1"},
        files={"audio": ("synthetic.wav", wav(), "audio/wav")}, data={"processing_mode": "ai"},
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_speech_fails_over_to_whisper_and_the_note_follows(env):
    context = env()
    relay = Relay(speech_text=DEMO_TEXT)
    primary_speech = MockSpeechProvider(DEMO_TEXT)

    class Down:
        calls = 0

        def transcribe(self, source, *, media_type):
            Down.calls += 1
            raise fail("stt_unavailable")

    worker = context.worker(
        Script(), relay, speech=Down(), fallback_speech=relay.speech(), audio_storage=context.app.state.audio_storage,
    )
    go_down(context, probe_due=False)
    job_id = audio_job(context)
    assert worker.run_once()
    job = context.row(job_id)
    assert job.status == "succeeded" and job.provider_used == FALLBACK
    assert Down.calls == 0 and primary_speech.calls == 0  # Шлюз упал, речь сразу ушла на резерв.
    paths = [request.url.path for request in relay.requests]
    assert paths == ["/v1/audio/transcriptions", "/v1/chat/completions"]
    assert b'name="model"' in relay.requests[0].content and b"whisper-1" in relay.requests[0].content
    stt, llm = context.receipts()
    assert (stt.kind, stt.provider, stt.model) == ("stt", "openai", "whisper-1")
    assert stt.estimated_cost > 0 and llm.provider == "openai"
    # Плата за речь считается по секундам записи, а резерв заменён оценкой.
    assert context.committed() == to_micro(stt.estimated_cost + llm.estimated_cost)
    with context.app.state.sessions() as db:
        assert db.scalar(select(Capture.transcript)) == DEMO_TEXT


def test_a_speech_failure_on_the_main_gateway_moves_the_job_to_the_fallback(env):
    context = env()
    relay = Relay()

    class Down:
        def transcribe(self, source, *, media_type):
            raise fail("stt_unavailable")

    worker = context.worker(
        Script(), relay, speech=Down(), fallback_speech=relay.speech(), audio_storage=context.app.state.audio_storage,
    )
    job_id = audio_job(context)
    assert worker.run_once()
    job = context.row(job_id)
    assert (job.status, job.next_route, job.last_error_code) == ("queued", FALLBACK, "stt_unavailable")
    assert worker.run_once()
    assert context.row(job_id).status == "succeeded" and relay.calls == 2


def test_without_a_fallback_speech_client_voice_stays_on_the_main_gateway(env):
    context = env()
    relay = Relay()

    class Down:
        def transcribe(self, source, *, media_type):
            raise fail("stt_unavailable")

    worker = context.worker(Script(), relay, speech=Down(), audio_storage=context.app.state.audio_storage)
    job_id = audio_job(context)
    assert worker.run_once()
    job = context.row(job_id)
    assert job.next_route is None and job.not_before > time.time() + 40 and relay.calls == 0


# Помощник

def test_assistant_fails_over_once_when_the_gateway_does_not_answer(env):
    context = env()
    with context.app.state.sessions() as db:
        user_id = db.scalar(select(User.id))
    add_note(context.app, user_id, "Запуск", "Дизайн готов.\nОплату ещё не сделали.")
    reply = {"answer_markdown": "Оплаты ещё нет.", "citations": [{"note_id": "x", "quote": "Оплату ещё не сделали."}]}
    with context.app.state.sessions() as db:
        from app.models import Note

        reply["citations"][0]["note_id"] = db.scalar(select(Note.id))
    relay = Relay(httpx.Response(200, json=chat(content=json.dumps(reply, ensure_ascii=False), prompt=2000)))
    primary = Script(fail("provider_unavailable"))
    worker = context.worker(primary, relay)
    request_id = post(context.client, "/ask", "ask-1", {"question": "Что с оплатой?"}).json()["id"]
    assert worker.run_once()
    view = read(context.client, request_id)
    assert view["status"] == "succeeded" and view["units"] == 1
    assert primary.calls == 1 and relay.calls == 1
    body = json.loads(relay.requests[0].content)
    assert body["model"] == "gpt-4o" and "Что с оплатой?" in body["messages"][1]["content"]
    first, second = context.receipts()
    assert (first.request_id, first.status) == (request_id + ":assistant", "unknown")
    assert (second.request_id, second.provider, second.input_tokens) == (request_id + ":assistant:fb", "openai", 2000)
    assert context.committed() == to_micro(second.estimated_cost) > 0
    assert context.route().failures == 1


def test_assistant_goes_straight_to_the_fallback_while_the_gateway_is_down(env):
    context = env()
    with context.app.state.sessions() as db:
        user_id = db.scalar(select(User.id))
    add_note(context.app, user_id, "Запуск", "Дизайн готов.")
    go_down(context, probe_due=False)
    with context.app.state.sessions() as db:
        from app.models import Note

        note_id = db.scalar(select(Note.id))
    reply = {"summary_markdown": "Итоги", "highlights": [{"note_id": note_id, "quote": "Дизайн готов."}],
             "open_tasks": [], "themes": ["дизайн"]}
    relay = Relay(httpx.Response(200, json=chat(content=json.dumps(reply, ensure_ascii=False))))
    primary = Script()
    worker = context.worker(primary, relay)
    request_id = post(context.client, "/digest", "digest-1", {}).json()["id"]
    assert worker.run_once()
    assert read(context.client, request_id)["status"] == "succeeded"
    assert primary.calls == 0 and relay.calls == 1


@pytest.mark.parametrize("code", ["provider_bad_request", "provider_auth", "ungrounded_quote"])
def test_assistant_does_not_fail_over_on_other_errors(env, code):
    context = env()
    with context.app.state.sessions() as db:
        user_id = db.scalar(select(User.id))
    add_note(context.app, user_id, "Запуск", "Дизайн готов.")
    relay = Relay()
    worker = context.worker(Script(fail(code)), relay)
    request_id = post(context.client, "/digest", "digest-1", {}).json()["id"]
    assert worker.run_once()
    view = read(context.client, request_id)
    assert (view["status"], view["error_code"]) == ("failed", code) and relay.calls == 0


def test_assistant_stops_when_the_fallback_fails_too(env):
    context = env()
    with context.app.state.sessions() as db:
        user_id = db.scalar(select(User.id))
    add_note(context.app, user_id, "Запуск", "Дизайн готов.")
    relay = Relay(httpx.Response(503))
    worker = context.worker(Script(fail("provider_unavailable")), relay)
    request_id = post(context.client, "/digest", "digest-1", {}).json()["id"]
    assert worker.run_once()
    view = read(context.client, request_id)
    assert (view["status"], view["error_code"]) == ("failed", "provider_unavailable") and relay.calls == 1
    with context.app.state.sessions() as db:
        assert db.get(AssistantRequest, request_id).status == "failed"
    assert context.committed() == 0  # Известный отказ ничего не стоил.


# Вспомогательное

def test_the_router_is_absent_when_the_switch_is_off_or_there_is_no_client(app_factory):
    app = app_factory()
    with TestClient(app):
        relay = Relay()
        off = Worker(app.state.sessions, app.state.settings, Script(), fallback_provider=relay.provider())
        assert off.router is None
        on = Worker(app.state.sessions, replace(app.state.settings, **FALLBACK_SETTINGS), Script())
        assert on.router is None


def test_lease_expiry_of_a_fallback_attempt_does_not_blame_the_main_gateway(env):
    context = env()
    relay = Relay()
    worker = context.worker(Script(), relay)
    go_down(context, probe_due=False)
    job_id = context.job()
    assert worker.claim() == job_id
    attempt = worker.begin_attempt(job_id)
    assert attempt.fallback
    failures_before = context.route().failures
    with context.app.state.sessions() as db:
        db.get(Job, job_id).lease_until = time.time() - 1
        db.commit()
    assert worker.claim() is None
    assert context.route().failures == failures_before and context.route().state == "down"
    attempt.finish()
    assert context.committed() == 0
    job = context.row(job_id)
    assert job.last_error_provider == FALLBACK and job.status == "queued"
