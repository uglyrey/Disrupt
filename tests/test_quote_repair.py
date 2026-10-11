import json

import httpx
from sqlalchemy import select

from app.models import Capture, Item, Job, Note, ProviderUsage
from app.providers import CloudRuProvider, repair_structure_quotes, validate_result
from app.schemas import StructuredNote
from app.worker import Worker
from tests.conftest import register
from tests.test_notes import submit


def structured(**changes):
    return StructuredNote.model_validate({"title": "Список", "markdown": "Купить хлеб.",
                                         "conclusions": [], "items": [], **changes})


def test_whitespace_is_recovered_as_an_exact_span_not_a_fabricated_quote():
    original = "Купить\n\tхлеб\u00a0завтра. Не звонить."
    result = repair_structure_quotes(structured(items=[
        {"kind": "task", "text": "Купить хлеб", "source_quote": "Купить хлеб завтра.", "due_text": "завтра"},
    ]), original)
    assert result.items[0].source_quote == "Купить\n\tхлеб\u00a0завтра."
    assert result.items[0].due_text == "завтра"
    assert not result._grounding_details_omitted
    assert validate_result(result, original).items[0].source_quote in original


def test_invalid_details_are_omitted_without_disabling_grounding(caplog):
    original = "SYNTHETIC PRIVATE: Купить хлеб завтра."
    result = repair_structure_quotes(structured(
        conclusions=[{"text": "Неподтверждённый вывод", "source_quote": "Выдуманная цитата"}],
        items=[{"kind": "task", "text": "Купить хлеб", "source_quote": "Купить хлеб завтра.", "due_text": "через месяц"},
               {"kind": "task", "text": "Неподтверждённая задача", "source_quote": "Заплатить миллион"}],
    ), original)
    assert result.conclusions == [] and len(result.items) == 1
    assert result.items[0].due_text is None and result.items[0].source_quote in original
    assert result._grounding_details_omitted and validate_result(result, original)._grounding_details_omitted
    assert "_grounding_details_omitted" not in result.model_dump()
    assert "SYNTHETIC PRIVATE" not in caplog.text and "Заплатить миллион" not in caplog.text
    assert "conclusions_omitted=1 items_omitted=1 due_text_omitted=1" in caplog.text


def test_case_is_recovered_but_paraphrase_and_invented_facts_do_not_become_quotes():
    result = repair_structure_quotes(structured(items=[
        {"kind": "task", "text": "Купить хлеб", "source_quote": "купить хлеб"},
        {"kind": "task", "text": "Позвонить", "source_quote": "Позвонить завтра"},
    ]), "Купить хлеб. Не звонить завтра.")
    assert [item.source_quote for item in result.items] == ["Купить хлеб"]
    assert result._grounding_details_omitted


def test_worker_keeps_note_and_valid_items_with_one_dispatch_and_visible_warning(app, client):
    register(client)
    original = "Купить хлеб завтра."
    job = submit(client, text=original).json()
    calls = []

    def transport(request):
        calls.append(request)
        content = structured(conclusions=[{"text": "Не подтверждено", "source_quote": "Другой текст"}],
                             items=[{"kind": "task", "text": "Купить хлеб", "source_quote": original}]).model_dump()
        return httpx.Response(200, json={"usage": {"prompt_tokens": 50, "completion_tokens": 20},
                                        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(transport))
    worker = Worker(app.state.sessions, app.state.settings, provider)
    assert worker.run_once() and not worker.run_once()
    assert len(calls) == 1
    view = client.get('/api/v1/jobs/' + job['id']).json()
    assert view['status'] == 'succeeded' and view['error_code'] == 'ungrounded_details_omitted'
    with app.state.sessions() as db:
        stored = db.get(Job, job['id'])
        assert db.get(Capture, stored.capture_id).original_text == original
        note = db.scalar(select(Note))
        assert note.conclusions == [] and note.markdown == 'Купить хлеб.'
        assert db.scalar(select(Item)).source_quote == original
        receipts = list(db.scalars(select(ProviderUsage)))
        assert len(receipts) == 1 and receipts[0].status == 'succeeded' and receipts[0].input_tokens == 50
