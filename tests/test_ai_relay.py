"""Посредник к OpenAI. Только выдуманные ключи и httpx.MockTransport, настоящий сервис не вызывается."""

import io
import json
import logging

import httpx
import pytest

from ai_relay.__main__ import config_from_env
from ai_relay.relay import AUDIO_LIMIT, CHAT_LIMIT, Relay, RelayConfig
from ai_relay.server import make_handler

TOKEN = "relay-token-for-tests-0123456789abcdef"
OPENAI_KEY = "sk-test-not-a-real-key-0123456789"
CHAT = {"model": "gpt-4o", "max_tokens": 100, "messages": [{"role": "user", "content": "Секретный текст заметки"}]}


class Upstream:
    """Подмена OpenAI. Запоминает всё, что пришло, и отвечает по сценарию."""

    def __init__(self, *responses):
        self.requests, self.responses = [], list(responses)

    def __call__(self, request):
        self.requests.append(request)
        if self.responses:
            answer = self.responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})


def make_relay(upstream=None, **changes):
    upstream = upstream or Upstream()
    config = RelayConfig(token=TOKEN, api_key=OPENAI_KEY, **changes)
    return Relay(config, transport=httpx.MockTransport(upstream)), upstream


def headers(**extra):
    base = {"authorization": "Bearer " + TOKEN, "content-type": "application/json"}
    return {**base, **{name.replace("_", "-"): value for name, value in extra.items()}}


def post(relay, path, payload, *, extra=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    given = headers(**(extra or {}))
    given.setdefault("content-length", str(len(body)))
    return relay.handle("POST", path, given, io.BytesIO(body))


def multipart(fields, boundary="testboundary123"):
    out = b""
    for name, value in fields:
        out += b"--" + boundary.encode() + b"\r\n"
        if name == "file":
            out += b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n'
        else:
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += (value if isinstance(value, bytes) else value.encode()) + b"\r\n"
    return out + b"--" + boundary.encode() + b"--\r\n", "multipart/form-data; boundary=" + boundary


def audio(relay, fields, **kwargs):
    body, content_type = multipart(fields)
    return relay.handle(
        "POST", "/v1/audio/transcriptions",
        headers(content_type=content_type, content_length=str(len(body))), io.BytesIO(body),
    )


def body_of(reply):
    return reply.body if isinstance(reply.body, bytes) else b"".join(reply.body)


class Unread(io.RawIOBase):
    def read(self, size=-1):
        raise AssertionError("Тело нельзя читать до проверки токена")


# Настройки

def test_config_needs_a_long_distinct_token_and_a_key():
    with pytest.raises(ValueError):
        RelayConfig(token="short", api_key=OPENAI_KEY)
    with pytest.raises(ValueError):
        RelayConfig(token=TOKEN, api_key="")
    with pytest.raises(ValueError):
        RelayConfig(token=TOKEN, api_key=TOKEN)
    with pytest.raises(ValueError):
        RelayConfig(token=TOKEN, api_key=OPENAI_KEY, chat_models=frozenset())
    assert OPENAI_KEY not in repr(RelayConfig(token=TOKEN, api_key=OPENAI_KEY))
    assert TOKEN not in repr(RelayConfig(token=TOKEN, api_key=OPENAI_KEY))


def test_config_is_read_from_secret_files(tmp_path):
    (tmp_path / "token").write_text(TOKEN + "\n", encoding="utf-8")
    (tmp_path / "key").write_text(OPENAI_KEY + "\n", encoding="utf-8")
    config = config_from_env({
        "AI_RELAY_TOKEN_FILE": str(tmp_path / "token"), "AI_RELAY_OPENAI_KEY_FILE": str(tmp_path / "key"),
        "AI_RELAY_CHAT_MODELS": "gpt-4o, gpt-4o-mini",
    })
    assert config.token == TOKEN and config.api_key == OPENAI_KEY
    assert config.chat_models == {"gpt-4o", "gpt-4o-mini"} and config.stt_models == {"whisper-1"}


# Адреса, методы и токен

def test_health_answers_without_token_and_without_upstream():
    relay, upstream = make_relay()
    reply = relay.handle("GET", "/health", {}, Unread())
    assert reply.status == 200 and json.loads(body_of(reply)) == {"status": "ok"}
    assert upstream.requests == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/chat/completions"), ("PUT", "/v1/chat/completions"), ("POST", "/v1/models"),
    ("POST", "/v1/embeddings"), ("POST", "/v1/images/generations"), ("POST", "/"), ("POST", "/v1/chat/completions/x"),
    ("POST", "/v1/chat/completions?stream=true"), ("DELETE", "/v1/files/abc"), ("POST", "/health"),
    ("GET", "/health?x=1"), ("GET", "/v1/audio/transcriptions"),
])
def test_other_paths_and_methods_are_404(method, path):
    relay, upstream = make_relay()
    reply = relay.handle(method, path, headers(content_length="2"), Unread())
    assert reply.status == 404 and upstream.requests == []


@pytest.mark.parametrize("authorization", [None, "", "Bearer", "Bearer wrong", "Basic " + TOKEN, TOKEN, "Bearer " + TOKEN + "x"])
def test_a_missing_or_wrong_token_is_401_and_the_body_stays_unread(authorization):
    relay, upstream = make_relay()
    given = {"content-type": "application/json", "content-length": "10"}
    if authorization is not None:
        given["authorization"] = authorization
    reply = relay.handle("POST", "/v1/chat/completions", given, Unread())
    assert reply.status == 401 and upstream.requests == []
    assert TOKEN not in body_of(reply).decode()


def test_token_is_compared_in_constant_time(monkeypatch):
    calls = []
    import ai_relay.relay as module

    real = module.hmac.compare_digest
    monkeypatch.setattr(module.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    relay, _ = make_relay()
    assert post(relay, "/v1/chat/completions", CHAT).status == 200
    assert calls


# Ключ

def test_the_key_is_added_and_nothing_from_the_client_is_forwarded():
    relay, upstream = make_relay()
    reply = post(relay, "/v1/chat/completions", CHAT, extra={
        "cookie": "session=abc", "x_forwarded_for": "10.0.0.9", "connection": "keep-alive", "proxy_authorization": "x",
        "keep_alive": "timeout=5",
    })
    assert reply.status == 200
    sent = upstream.requests[0]
    assert str(sent.url) == "https://api.openai.com/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer " + OPENAI_KEY
    assert TOKEN not in str(sent.headers)
    for name in ("cookie", "x-forwarded-for", "proxy-authorization", "keep-alive"):
        assert name not in sent.headers
    assert sent.method == "POST" and len(upstream.requests) == 1


def test_the_relay_token_never_reaches_the_client_answer_and_the_key_never_does_either():
    relay, _ = make_relay(Upstream(httpx.Response(401, json={"error": {"message": "bad key " + OPENAI_KEY}})))
    reply = post(relay, "/v1/chat/completions", CHAT)
    text = body_of(reply).decode() + str(reply.headers)
    assert reply.status == 401 and OPENAI_KEY not in text and TOKEN not in text and "bad key" not in text


def test_redirects_are_not_followed():
    upstream = Upstream(httpx.Response(307, headers={"location": "https://evil.example/steal"}))
    relay, _ = make_relay(upstream)
    reply = post(relay, "/v1/chat/completions", CHAT)
    assert reply.status == 502 and len(upstream.requests) == 1
    assert "evil.example" not in str(reply.headers) + body_of(reply).decode()


# Проверка тела: текст

def test_chat_model_allowlist():
    relay, upstream = make_relay()
    for model in ("gpt-4", "o1", "gpt-4o-mini", "", None, 5, ["gpt-4o"]):
        assert post(relay, "/v1/chat/completions", {**CHAT, "model": model}).status == 400
    no_model = {key: value for key, value in CHAT.items() if key != "model"}
    assert post(relay, "/v1/chat/completions", no_model).status == 400
    assert upstream.requests == []
    wider, upstream = make_relay(chat_models=frozenset({"gpt-4o", "gpt-4o-mini"}))
    assert post(wider, "/v1/chat/completions", {**CHAT, "model": "gpt-4o-mini"}).status == 200


@pytest.mark.parametrize("change", [
    {"max_tokens": None}, {"max_tokens": 0}, {"max_tokens": 8001}, {"max_tokens": "100"}, {"max_tokens": True},
    {"max_tokens": 1.5}, {"n": 2}, {"stream": "yes"}, {"messages": "x"},
    {"tools": []}, {"max_completion_tokens": 100000}, {"logprobs": True}, {"user": "x"},
])
def test_chat_fields_are_checked(change):
    relay, upstream = make_relay()
    payload = {**CHAT, **change}
    payload = {key: value for key, value in payload.items() if value is not None}
    assert post(relay, "/v1/chat/completions", payload).status == 400
    assert upstream.requests == []


def test_chat_without_max_tokens_is_refused():
    relay, upstream = make_relay()
    payload = {key: value for key, value in CHAT.items() if key != "max_tokens"}
    assert post(relay, "/v1/chat/completions", payload).status == 400 and upstream.requests == []


def test_chat_needs_json_and_valid_json():
    relay, upstream = make_relay()
    assert post(relay, "/v1/chat/completions", None, raw=b"[1]").status == 400
    assert post(relay, "/v1/chat/completions", None, raw=b"{").status == 400
    assert post(relay, "/v1/chat/completions", None, raw=b"{}", extra={"content_type": "text/plain"}).status == 415
    assert upstream.requests == []


def test_duplicate_json_keys_cannot_smuggle_another_model():
    relay, upstream = make_relay()
    raw = (b'{"model":"o1-pro","model":"gpt-4o","max_tokens":10,"max_tokens":9999999,"messages":[]}')
    # Python берёт последнее значение, поэтому проверка и отправка видят одно и то же.
    assert post(relay, "/v1/chat/completions", None, raw=raw).status == 400
    raw = b'{"model":"o1-pro","model":"gpt-4o","max_tokens":10,"messages":[]}'
    assert post(relay, "/v1/chat/completions", None, raw=raw).status == 200
    sent = upstream.requests[0].content
    assert sent.count(b'"model"') == 1 and json.loads(sent)["model"] == "gpt-4o"


# Размеры

def test_chat_body_limit_is_512_kib_and_checked_before_reading():
    relay, upstream = make_relay()
    given = headers(content_length=str(CHAT_LIMIT + 1))
    assert relay.handle("POST", "/v1/chat/completions", given, Unread()).status == 413
    assert upstream.requests == []
    body = json.dumps({**CHAT, "messages": [{"role": "user", "content": "я" * 100}]}).encode()
    assert post(relay, "/v1/chat/completions", None, raw=body).status == 200


def test_audio_body_limit_is_12_mib_and_checked_before_reading():
    relay, upstream = make_relay()
    given = headers(content_type="multipart/form-data; boundary=b", content_length=str(AUDIO_LIMIT + 1))
    assert relay.handle("POST", "/v1/audio/transcriptions", given, Unread()).status == 413
    assert upstream.requests == []


def test_length_is_required_and_chunked_bodies_are_refused():
    relay, _ = make_relay()
    no_length = {"authorization": "Bearer " + TOKEN, "content-type": "application/json"}
    assert relay.handle("POST", "/v1/chat/completions", no_length, Unread()).status == 411
    chunked = {**no_length, "transfer-encoding": "chunked", "content-length": "5"}
    assert relay.handle("POST", "/v1/chat/completions", chunked, Unread()).status == 411
    junk = {**no_length, "content-length": "-5"}
    assert relay.handle("POST", "/v1/chat/completions", junk, Unread()).status == 411
    short = {**no_length, "content-length": "50"}
    assert relay.handle("POST", "/v1/chat/completions", short, io.BytesIO(b"{}")).status == 400


# Размеры и поля: речь

def test_audio_is_forwarded_with_the_allowed_model():
    relay, upstream = make_relay(Upstream(httpx.Response(200, json={"text": "привет"})))
    reply = audio(relay, [("model", "whisper-1"), ("language", "ru"), ("file", b"RIFF....")])
    assert reply.status == 200 and json.loads(body_of(reply)) == {"text": "привет"}
    sent = upstream.requests[0]
    assert str(sent.url) == "https://api.openai.com/v1/audio/transcriptions"
    assert sent.headers["authorization"] == "Bearer " + OPENAI_KEY
    assert sent.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b"RIFF...." in sent.content


@pytest.mark.parametrize("fields", [
    [("model", "whisper-2"), ("file", b"x")],
    [("model", "gpt-4o"), ("file", b"x")],
    [("file", b"x")],
    [("model", "whisper-1")],
    [("model", "whisper-1"), ("model", "whisper-1"), ("file", b"x")],
    [("model", "whisper-1"), ("file", b"x"), ("timestamp_granularities[]", "word")],
    [("model", "whisper-1"), ("file", b"x"), ("file", b"y")],
])
def test_audio_fields_are_checked(fields):
    relay, upstream = make_relay()
    assert audio(relay, fields).status == 400
    assert upstream.requests == []


def test_audio_needs_a_multipart_body():
    relay, upstream = make_relay()
    assert post(relay, "/v1/audio/transcriptions", None, raw=b"abc", extra={"content_type": "audio/wav"}).status == 415
    bad = headers(content_type="multipart/form-data; boundary=!!bad!!", content_length="3")
    assert relay.handle("POST", "/v1/audio/transcriptions", bad, io.BytesIO(b"abc")).status == 400
    assert upstream.requests == []


# Потоки и ошибки OpenAI

def test_stream_is_passed_through_chunk_by_chunk_and_released():
    events = [b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n', b"data: [DONE]\n\n"]

    class Chunked(httpx.SyncByteStream):
        def __iter__(self):
            yield from events

    relay, upstream = make_relay(Upstream(httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunked())),
                                 max_concurrent=1)
    reply = post(relay, "/v1/chat/completions", {**CHAT, "stream": True})
    assert reply.status == 200 and not isinstance(reply.body, bytes)
    assert upstream.requests[0].headers["accept"] == "text/event-stream"
    assert json.loads(upstream.requests[0].content)["stream"] is True
    assert list(reply.body) == events
    assert post(relay, "/v1/chat/completions", CHAT).status == 503  # Единственное место занято, пока поток открыт.
    reply.close()
    assert post(relay, "/v1/chat/completions", CHAT).status == 200


def test_without_stream_the_answer_is_json():
    relay, upstream = make_relay()
    post(relay, "/v1/chat/completions", CHAT)
    assert upstream.requests[0].headers["accept"] == "application/json"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503, 504])
def test_upstream_errors_keep_their_code_but_not_their_text(status):
    relay, _ = make_relay(Upstream(httpx.Response(
        status, json={"error": {"message": "Секретный текст заметки"}}, headers={"Retry-After": "7", "x-secret": "1"})))
    reply = post(relay, "/v1/chat/completions", CHAT)
    assert reply.status == status
    assert "Секретный текст" not in body_of(reply).decode()
    assert "x-secret" not in str(reply.headers).lower()
    if status == 429:
        assert ("Retry-After", "7") in reply.headers


def test_connect_errors_are_502_and_other_errors_504():
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    for error, status in ((httpx.ConnectError("x", request=request), 502), (httpx.ConnectTimeout("x", request=request), 502),
                          (httpx.ReadTimeout("x", request=request), 504), (httpx.ReadError("x", request=request), 504)):
        relay, _ = make_relay(Upstream(error))
        assert post(relay, "/v1/chat/completions", CHAT).status == status


def test_oversized_upstream_answer_is_refused():
    relay, _ = make_relay(Upstream(httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 10))))
    assert post(relay, "/v1/chat/completions", CHAT).status == 502


# Частота

def test_rate_limit_per_minute_counts_only_valid_requests():
    now = [1000.0]
    config = RelayConfig(token=TOKEN, api_key=OPENAI_KEY, per_minute=3)
    relay = Relay(config, transport=httpx.MockTransport(Upstream()), clock=lambda: now[0])
    wrong = {"authorization": "Bearer nope", "content-length": "0"}
    for _ in range(10):
        assert relay.handle("POST", "/v1/chat/completions", wrong, Unread()).status == 401
    assert [post(relay, "/v1/chat/completions", CHAT).status for _ in range(3)] == [200, 200, 200]
    limited = post(relay, "/v1/chat/completions", CHAT)
    assert limited.status == 429 and ("Retry-After", "30") in limited.headers
    now[0] += 61
    assert post(relay, "/v1/chat/completions", CHAT).status == 200


# Журнал

def test_nothing_secret_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    relay, _ = make_relay()
    post(relay, "/v1/chat/completions", CHAT)
    post(relay, "/v1/chat/completions", {**CHAT, "model": "evil-model-name"})
    relay.handle("POST", "/v1/chat/completions", {"authorization": "Bearer " + "z" * 40, "content-length": "0"}, Unread())
    audio(relay, [("model", "whisper-1"), ("file", b"AUDIO-SECRET-BYTES")])
    text = caplog.text
    assert "path=/v1/chat/completions" in text and "model=gpt-4o" in text
    for secret in (OPENAI_KEY, TOKEN, "Секретный текст заметки", "AUDIO-SECRET-BYTES", "evil-model-name", "z" * 40):
        assert secret not in text


# Оболочка

class FakeSocket:
    def __init__(self, raw):
        self.raw, self.sent = raw, []

    def settimeout(self, value):
        pass

    def makefile(self, mode, buffering=None):
        return io.BytesIO(self.raw) if "r" in mode else io.BytesIO()

    def sendall(self, data):
        self.sent.append(bytes(data))


def serve(relay, raw):
    sock = FakeSocket(raw)
    make_handler(relay)(sock, ("10.77.0.1", 40000), object())
    return b"".join(sock.sent)


def test_http_wrapper_answers_health_and_forwards_a_chat_call():
    relay, upstream = make_relay()
    health = serve(relay, b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
    assert health.startswith(b"HTTP/1.0 200 OK") and health.endswith(b'{"status":"ok"}')
    body = json.dumps(CHAT).encode()
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
        + b"\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    answer = serve(relay, request)
    assert answer.startswith(b"HTTP/1.0 200 OK") and b"Connection: close" in answer and b'"choices"' in answer
    assert upstream.requests[0].headers["authorization"] == "Bearer " + OPENAI_KEY
    refused = serve(relay, b"GET /v1/models HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n\r\n")
    assert refused.startswith(b"HTTP/1.0 404")
    assert OPENAI_KEY.encode() not in health + answer + refused
