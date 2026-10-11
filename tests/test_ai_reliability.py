import json
import time

import httpx
import pytest

from app.config import Settings
from app.providers import (
    CloudRuProvider,
    ProviderError,
    make_provider,
    repair_structure_quotes,
    validate_result,
)
from app.schemas import StructuredNote


def structured(**changes):
    return StructuredNote.model_validate({"title": "Список", "markdown": "Купить хлеб.",
                                         "conclusions": [], "items": [], **changes})


def quote_of(original, quote):
    result = repair_structure_quotes(structured(items=[
        {"kind": "note", "text": "Мысль", "source_quote": quote},
    ]), original)
    if not result.items:
        return None
    assert result.items[0].source_quote in original  # always an exact original substring
    assert validate_result(result, original).items[0].source_quote == result.items[0].source_quote
    return result.items[0].source_quote


@pytest.mark.parametrize("original,quote,expected", [
    ("Купить Хлеб завтра.", "купить хлеб завтра.", "Купить Хлеб завтра."),
    ("Всё ещё ЁЖИК у меня", "все еще ежик у меня", "Всё ещё ЁЖИК у меня"),
    ("Он сказал «да» и ушёл", 'Он сказал "да" и ушёл', "Он сказал «да» и ушёл"),
    ("Он сказал “да” и ушёл", "Он сказал «да» и ушёл", "Он сказал “да” и ушёл"),
    ("Это бизнес-план на год", "Это бизнес—план на год", "Это бизнес-план на год"),
    ("Срок — пятница", "Срок - пятница", "Срок — пятница"),
    ("Срок - пятница", "Срок − пятница", "Срок - пятница"),
    ("Купить\n\tхлеб завтра", "Купить хлеб завтра", "Купить\n\tхлеб завтра"),
    ("Подумать… потом решить", "Подумать... потом решить", "Подумать… потом решить"),
    ("Подумать... потом решить", "Подумать… потом решить", "Подумать... потом решить"),
])
def test_normalised_quote_returns_the_exact_original_span(original, quote, expected):
    assert quote_of(original + " Конец.", quote) == expected


def test_extra_trailing_punctuation_and_surrounding_quotes_are_trimmed_to_the_original():
    original = "Нужно позвонить маме в пятницу и узнать про билеты"
    assert quote_of(original, "позвонить маме в пятницу.") == "позвонить маме в пятницу"
    assert quote_of(original, "«позвонить маме в пятницу»") == "позвонить маме в пятницу"
    assert quote_of(original, '"позвонить маме в пятницу!"') == "позвонить маме в пятницу"
    assert quote_of(original, "позвонить маме в пятницу;,") == "позвонить маме в пятницу"


def test_original_closing_quote_is_kept_when_the_model_kept_it():
    original = "Он сказал «да». Потом ушёл."
    assert quote_of(original, "Он сказал «да»") == "Он сказал «да»"
    assert quote_of(original, "Он сказал «да».") == "Он сказал «да»."
    assert quote_of(original, "Он сказал нет") is None


def test_first_match_wins():
    original = "Позвонить маме. Потом позвонить маме."
    result = repair_structure_quotes(structured(items=[
        {"kind": "task", "text": "Позвонить", "source_quote": "ПОЗВОНИТЬ МАМЕ"},
    ]), original)
    assert result.items[0].source_quote == "Позвонить маме"


@pytest.mark.parametrize("quote", [
    "Позвонить маме завтра",           # paraphrase
    "маме позвонить",                   # words reordered
    "Купить хлеб и позвонить маме",     # glued from two places
    "Купить ... позвонить маме",        # fragments joined by an ellipsis
    "Купить молоко",                    # invented
])
def test_paraphrase_reordered_and_glued_fragments_are_still_rejected(quote):
    original = "Купить хлеб. Много разных дел. Позвонить маме в пятницу."
    assert quote_of(original, quote) is None


def test_due_text_is_matched_inside_the_repaired_quote_the_same_way():
    original = "Позвонить  маме в ПЯТНИЦУ вечером."
    result = repair_structure_quotes(structured(items=[
        {"kind": "task", "text": "Позвонить маме", "source_quote": "позвонить маме в пятницу",
         "due_text": "в пятницу"},
        {"kind": "task", "text": "Позвонить маме", "source_quote": "позвонить маме в пятницу",
         "due_text": "в субботу"},
    ]), original)
    assert result.items[0].source_quote == "Позвонить  маме в ПЯТНИЦУ"
    assert result.items[0].due_text == "в ПЯТНИЦУ"
    assert result.items[1].due_text is None and result._grounding_details_omitted
    validate_result(result, original)


def test_conclusion_quotes_are_repaired_and_unsupported_ones_dropped():
    original = "Оплату ещё не сделали, хотели запускаться в пятницу."
    result = repair_structure_quotes(structured(conclusions=[
        {"text": "Риск", "source_quote": "оплату еще не сделали"},
        {"text": "Выдумка", "source_quote": "оплата прошла"},
    ]), original)
    assert [c.source_quote for c in result.conclusions] == ["Оплату ещё не сделали"]
    assert result._grounding_details_omitted


def test_span_longer_than_the_limit_is_dropped():
    original = "слово" + " " * 3500 + "дело"
    assert quote_of(original, "слово дело") is None


# ---- settings ----

def test_read_timeout_is_configurable_within_range_and_lease_follows(monkeypatch):
    monkeypatch.delenv("NOTES_LLM_READ_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("NOTES_LLM_STREAM", raising=False)
    assert Settings.from_env().llm_read_timeout_seconds == 90
    assert Settings.from_env().lease_seconds == 120
    monkeypatch.setenv("NOTES_LLM_READ_TIMEOUT_SECONDS", "240")
    settings = Settings.from_env()
    assert settings.llm_read_timeout_seconds == 240 and settings.lease_seconds == 270
    for bad in ("29", "241"):
        monkeypatch.setenv("NOTES_LLM_READ_TIMEOUT_SECONDS", bad)
        with pytest.raises(ValueError, match="NOTES_LLM_READ_TIMEOUT_SECONDS"):
            Settings.from_env()


def test_lease_must_exceed_the_timeout_and_streaming_deadline():
    with pytest.raises(ValueError, match="Worker lease"):
        Settings(llm_read_timeout_seconds=120, lease_seconds=140)
    assert Settings(llm_read_timeout_seconds=120, lease_seconds=150).lease_seconds == 150
    with pytest.raises(ValueError, match="Worker lease"):
        Settings(llm_stream=True, lease_seconds=120)  # deadline is 180 s
    assert Settings(llm_stream=True, lease_seconds=210).llm_stream


def test_stream_flag_derives_a_longer_lease(monkeypatch):
    monkeypatch.setenv("NOTES_LLM_STREAM", "true")
    monkeypatch.setenv("NOTES_LLM_READ_TIMEOUT_SECONDS", "200")
    settings = Settings.from_env()
    assert settings.llm_stream and settings.lease_seconds == 330  # deadline capped at 300 s
    monkeypatch.setenv("NOTES_LLM_STREAM", "maybe")
    with pytest.raises(ValueError, match="NOTES_LLM_STREAM"):
        Settings.from_env()


def test_make_provider_passes_timeout_and_stream(monkeypatch):
    monkeypatch.setenv("NOTES_CLOUDRU_API_KEY", "fake-test-key")
    settings = Settings(
        provider="cloudru", allow_live_requests=True, cloudru_model="test-model",
        live_call_limit=1, live_user_call_limit=1, llm_read_timeout_seconds=60, llm_stream=True, lease_seconds=200,
    )
    provider = make_provider(settings)
    assert provider._read_timeout == 60 and provider._stream is True


# ---- non-streaming timeout value ----

def test_configured_read_timeout_reaches_the_http_client():
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
            {"title": "Т", "markdown": "М", "conclusions": []})}}]})

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(handler), read_timeout=150)
    provider.structure("Текст")
    assert seen == [{"connect": 10, "read": 150, "write": 10, "pool": 10}]


# ---- streaming ----

ANSWER = {"title": "Мысль", "markdown": "## Мысль\n\nТекст", "conclusions": [], "items": [], "category_name": None}


def sse(*events, done=True):
    lines = [f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def pieces(text, size=7):
    return [{"choices": [{"delta": {"content": text[i:i + size]}, "finish_reason": None}]}
            for i in range(0, len(text), size)]


def stream_provider(handler, **kwargs):
    return CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(handler),
                           stream=True, **kwargs)


def sse_response(body, status=200):
    return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})


def test_stream_request_asks_for_sse_with_usage_and_collects_text_and_tokens():
    bodies, tokens = [], []
    text = json.dumps(ANSWER, ensure_ascii=False)
    events = [*pieces(text),
              {"choices": [{"delta": {}, "finish_reason": "stop"}]},
              {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 20,
                                         "prompt_tokens_details": {"cached_tokens": 10}}}]

    def handler(request):
        bodies.append(json.loads(request.content))
        return sse_response(sse(*events))

    result = stream_provider(handler).structure_with_usage("Мысль", on_usage=tokens.append)
    assert result.title == "Мысль"
    assert bodies[0]["stream"] is True and bodies[0]["stream_options"] == {"include_usage": True}
    assert bodies[0]["max_tokens"] == 4000
    assert (tokens[0].input_tokens, tokens[0].output_tokens, tokens[0].cache_tokens) == (50, 20, 10)


def test_stream_usage_is_unknown_when_the_gateway_sends_none():
    tokens = []
    text = json.dumps(ANSWER, ensure_ascii=False)
    events = [*pieces(text), {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    provider = stream_provider(lambda request: sse_response(sse(*events)))
    assert provider.structure_with_usage("Мысль", on_usage=tokens.append).title == "Мысль"
    assert (tokens[0].input_tokens, tokens[0].output_tokens) == (None, None)


def test_stream_without_done_marker_and_with_crlf_still_works():
    text = json.dumps(ANSWER, ensure_ascii=False)
    events = [*pieces(text), {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]}]
    body = sse(*events, done=False).replace(b"\n", b"\r\n")
    provider = stream_provider(lambda request: sse_response(body))
    assert provider.structure("Мысль").title == "Мысль"


def test_stream_finish_reason_length_is_incomplete():
    text = json.dumps(ANSWER, ensure_ascii=False)[:20]
    events = [*pieces(text), {"choices": [{"delta": {}, "finish_reason": "length"}]}]
    with pytest.raises(ProviderError, match="provider_incomplete_response"):
        stream_provider(lambda request: sse_response(sse(*events))).structure("Мысль")


def test_stream_cut_before_finish_reason_is_incomplete():
    text = json.dumps(ANSWER, ensure_ascii=False)
    with pytest.raises(ProviderError, match="provider_incomplete_response"):
        stream_provider(lambda request: sse_response(sse(*pieces(text), done=False))).structure("Мысль")


@pytest.mark.parametrize("body", [
    b"data: {not json}\n\n",
    b"hello world\n\n",
    b"data: [1, 2]\n\n",
    b'data: {"error": {"message": "secret text"}}\n\n',
    b'data: {"choices": [{"delta": {"content": 5}}]}\n\n',
    b"data: [DONE]\n\n",  # no choices at all
    b"\xff\xfe\n\n",
])
def test_stream_malformed_lines_are_invalid_responses(body):
    with pytest.raises(ProviderError, match="provider_invalid_response"):
        stream_provider(lambda request: sse_response(body)).structure("Мысль")


def test_stream_non_json_answer_is_invalid_response():
    events = [*pieces("not json at all"), {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    with pytest.raises(ProviderError, match="provider_invalid_response"):
        stream_provider(lambda request: sse_response(sse(*events))).structure("Мысль")


def test_stream_oversize_is_rejected():
    provider = stream_provider(lambda request: sse_response(b"data: " + b"x" * 256001))
    with pytest.raises(ProviderError, match="provider_response_too_large"):
        provider.structure("Текст")


def test_stream_http_errors_and_timeouts_map_to_the_same_codes():
    with pytest.raises(ProviderError) as unavailable:
        stream_provider(lambda request: httpx.Response(503, text="secret")).structure("Текст")
    assert unavailable.value.code == "provider_unavailable" and unavailable.value.http_status == 503

    def slow(request):
        raise httpx.ReadTimeout("secret", request=request)

    with pytest.raises(ProviderError, match="provider_timeout_unknown"):
        stream_provider(slow).structure("Текст")


def test_stream_total_deadline_stops_a_trickling_answer(monkeypatch):
    text = json.dumps(ANSWER, ensure_ascii=False)
    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            for event in pieces(text, 3):
                clock["now"] += 40  # each piece is "late", the per-read timeout never fires
                yield sse(event, done=False)

    provider = stream_provider(lambda request: httpx.Response(200, stream=Slow()), read_timeout=60)
    with pytest.raises(ProviderError, match="provider_timeout_unknown"):
        provider.structure("Текст")  # deadline is 120 s


def test_stream_repairs_quotes_like_the_plain_call():
    original = "Купить хлеб завтра."
    answer = {**ANSWER, "items": [{"kind": "task", "text": "Купить хлеб", "source_quote": "купить хлеб завтра"}]}
    events = [*pieces(json.dumps(answer, ensure_ascii=False)),
              {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    result = stream_provider(lambda request: sse_response(sse(*events))).structure(original)
    assert result.items[0].source_quote == "Купить хлеб завтра"


def test_assistant_call_can_stream_too_and_is_still_strict():
    class Context:
        notes = [{"id": "n1", "title": "Т", "date": "2026-10-11", "text": "Купить хлеб", "items": []}]
        corpus = {"n1": ["Т", "Купить хлеб"]}
        open_task_ids = set()

    good = {"answer_markdown": "Хлеб.", "citations": [{"note_id": "n1", "quote": "Купить хлеб"}]}
    bad = {"answer_markdown": "Хлеб.", "citations": [{"note_id": "n1", "quote": "купить хлеб"}]}

    def run(answer):
        events = [*pieces(json.dumps(answer, ensure_ascii=False)),
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        return stream_provider(lambda request: sse_response(sse(*events))).assistant(
            "ask", Context(), question="Что купить?")

    assert run(good)["citations"][0]["quote"] == "Купить хлеб"
    with pytest.raises(ProviderError, match="ungrounded_quote"):
        run(bad)
