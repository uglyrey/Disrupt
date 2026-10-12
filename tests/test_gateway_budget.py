"""Billing, privacy and budget protection using only synthetic HTTP and accounts."""

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import gateway_budget as billing
from app.config import Settings
from app.models import ProviderBudget, User
from app.providers import NOT_FOUND_ANSWER, CloudRuProvider, ProviderError
from app.worker import Worker
from tests.conftest import ADMIN_ADDRESS, admin_login, make_admin, register
from tests.test_providers import completion


def snapshot(now=None, **changes):
    return {"version": 1, "currency": "RUB", "currency_source": "owner_confirmed",
            "total": "75000", "spent": "5.131431605299997", "checked_at": time.time() if now is None else now,
            "reserved_calls": 10, "unsettled_calls": 0, **changes}


def test_billing_queries_own_key_without_secret_in_url_or_snapshot():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://shared1.multitool.works:4000/key/info"
        assert request.headers["authorization"] == "Bearer synthetic-secret"
        return httpx.Response(200, json={"key": "synthetic-secret", "info": {
            "token": "synthetic-secret", "metadata": {"private": "do not retain"},
            "spend": 5.1314316053, "max_budget": 75000,
        }})
    result = billing.query("synthetic-secret", reserved_calls=10, unsettled_calls=2,
                           transport=httpx.MockTransport(handler), now=1234)
    assert len(calls) == 1
    assert result["spent"] == "5.1314316053"
    assert result["currency"] == "RUB"
    assert "secret" not in json.dumps(result) and "private" not in json.dumps(result)


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://unsafe.invalid"}),
    httpx.Response(403, json={"key": "synthetic-secret"}),
    httpx.Response(200, json={"info": {"spend": "NaN", "max_budget": 75000}}),
    httpx.Response(200, json={"info": {"spend": 1, "max_budget": 75000, "currency": "USD"}}),
    httpx.Response(200, json={"info": {"spend": 1, "max_budget": 75000, "budget_duration": "30d"}}),
])
def test_invalid_billing_never_follows_redirect_or_exposes_response(response):
    calls = []
    def handler(request):
        calls.append(request)
        return response
    with pytest.raises(billing.BudgetError) as error:
        billing.query("synthetic-secret", reserved_calls=0, unsettled_calls=0,
                      transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert "synthetic-secret" not in str(error.value)


def test_actual_balance_is_not_a_filtered_estimate_and_stale_data_blocks_spending(tmp_path):
    path = tmp_path / "budget.json"
    billing.save(snapshot(now=1791780000), path)
    settings = Settings(gateway_budget_file=str(path))
    result = billing.view(settings, 10, now=1791780010)
    assert result["total"] == "75000.00" and result["spent"] == "5.13"
    assert result["remaining"] == "74994.87" and result["pilot_remaining"] == "49994.87"
    assert result["fresh"] and not result["processing_paused"]
    assert not billing.available(settings, 10, now=1791781000)
    assert billing.view(settings, 10, now=1791781000)["spent"] == "5.13"


def test_pending_calls_and_protected_reserve_are_conservative(tmp_path):
    path = tmp_path / "budget.json"
    settings = Settings(gateway_budget_file=str(path))
    billing.save(snapshot(spent="49500", reserved_calls=10, unsettled_calls=1), path)
    assert billing.available(settings, 10)
    assert not billing.available(settings, 11)  # Pending + new call would cross the pilot cap.
    billing.save(snapshot(spent="50000"), path)
    assert not billing.available(settings, 10)
    path.unlink()
    assert not billing.available(settings, 10)
    assert billing.available(Settings(), 100)  # Mock and existing deployments remain opt-in.


def test_opus_structure_uses_supported_sampling_and_native_json_schema():
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == "claude-opus-4.7"
        assert "temperature" not in payload
        response_format = payload["response_format"]
        assert response_format["type"] == "json_schema"
        schema = response_format["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "minLength" not in json.dumps(schema)
        return httpx.Response(200, json=completion())
    provider = CloudRuProvider("synthetic", "claude-opus-4.7", transport=httpx.MockTransport(handler))
    assert provider.structure("Мысль").title == "Мысль"
    assert len(calls) == 1


def test_budget_snapshot_is_visible_only_to_admin(app_factory, tmp_path):
    path = tmp_path / "budget.json"
    billing.save(snapshot(), path)
    app = app_factory(gateway_budget_file=str(path))
    with TestClient(app, client=ADMIN_ADDRESS) as client:
        assert client.get('/admin-api/v1/summary', params={'from': '2026-10-01', 'to': '2026-10-02'}).status_code == 401
        secret = make_admin(app)
        assert admin_login(app, client, secret).status_code == 200
        result = client.get('/admin-api/v1/summary', params={'from': '2026-10-01', 'to': '2026-10-02'})
        assert result.status_code == 200
        assert result.json()["budget"]["spent"] == "5.13"
        assert result.json()["budget"]["remaining"] == "74994.87"
    with TestClient(app, client=("198.51.100.10", 50000)) as public:
        assert public.get('/admin-api/v1/summary').status_code == 404


@pytest.mark.parametrize("kind,result", [
    ("ask", {"answer_markdown": NOT_FOUND_ANSWER, "citations": []}),
    ("recommend", {"suggestions": []}),
    ("digest", {"summary_markdown": "За период нет заметок", "highlights": [], "open_tasks": [], "themes": []}),
])
def test_opus_assistant_has_schema_and_validates_each_result(kind, result):
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert "temperature" not in payload
        assert payload["response_format"]["type"] == "json_schema"
        return httpx.Response(200, json=completion(result))
    provider = CloudRuProvider("synthetic", "claude-opus-4.7", transport=httpx.MockTransport(handler))
    context = SimpleNamespace(notes=[], corpus={}, open_task_ids=set())
    assert provider.assistant(kind, context, question="Есть заметки?") == result
    assert len(calls) == 1


def test_money_guard_does_not_consume_call_slots_or_dispatch(app, client, tmp_path):
    account = register(client)
    path = tmp_path / "budget.json"
    billing.save(snapshot(spent="74900"), path)
    settings = replace(app.state.settings, provider="cloudru", cloudru_model="claude-opus-4.7",
                       live_call_limit=100000, live_user_call_limit=100000, gateway_budget_file=str(path))
    worker = Worker(app.state.sessions, settings, provider=object())
    with app.state.sessions() as db:
        db.get(ProviderBudget, "cloudru").reserved_calls = 0
        db.commit()
        with pytest.raises(ProviderError, match="budget_exhausted"):
            worker.reserve_budget(db, account["id"])
        assert db.get(ProviderBudget, "cloudru").reserved_calls == 0
        assert db.get(User, account["id"]).live_calls == 0
    assert not worker.budget_available()


def test_daily_defaults_give_new_accounts_one_hundred_units(app_factory):
    assert Settings().daily_unit_limit == Settings().new_account_unit_limit == 100
    app = app_factory(daily_unit_limit=100, new_account_unit_limit=100)
    with TestClient(app) as client:
        register(client)
        usage = client.get('/api/v1/provider/usage').json()
        assert usage["daily_unit_limit"] == usage["daily_units_remaining"] == 100
