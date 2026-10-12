"""Посредник к фиксированному AI API. Пропускает только два маршрута и общий секрет.

Ключ поставщика лежит в файле на этом сервере. Ядро его не видит, оно знает лишь общий токен посредника.
Тела запросов и ответов нигде не пишутся, в журнал идут путь, метод, задержка и название модели.
Ключ уходит только на фиксированный адрес из UPSTREAMS.
"""

import hmac
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx

UPSTREAMS = {
    "openai": "https://api.openai.com/v1",
    "purpleapi": "https://purpleapi.lol/v1",
}
CHAT_PATH = "/v1/chat/completions"
AUDIO_PATH = "/v1/audio/transcriptions"
HEALTH_PATH = "/health"
CHAT_LIMIT = 512 * 1024
AUDIO_LIMIT = 12 * 1024 * 1024
MIN_TOKEN_LENGTH = 32
RESPONSE_LIMIT = 4 * 1024 * 1024
MAX_TOKENS_CAP = 8000
# Полей, которых нет в списке, у запроса быть не должно. Так модель не получит инструменты и обходной лимит токенов.
CHAT_FIELDS = frozenset({
    "model", "messages", "temperature", "max_tokens", "response_format", "stream", "stream_options",
    "n", "top_p", "seed", "stop",
})
AUDIO_FIELDS = frozenset({"model", "file", "language", "prompt", "response_format", "temperature"})
# Из ответа поставщика в ядро уходят только эти заголовки.
PASS_HEADERS = ("content-type", "retry-after")
BOUNDARY = re.compile(r"^[A-Za-z0-9'()+_,./:=?-]{1,70}$")
FIELD_NAME = re.compile(r'name="([^"\r\n]{1,64})"')
logger = logging.getLogger("ai_relay")


def read_secret(path, *, maximum=4096):
    value = Path(path).read_text(encoding="utf-8").strip("\r\n")
    if not value or len(value) > maximum or "\x00" in value:
        raise ValueError("Файл секрета пуст или слишком большой")
    return value


@dataclass(frozen=True)
class RelayConfig:
    token: str = field(repr=False)
    api_key: str = field(repr=False)
    provider: str = "openai"
    chat_models: frozenset = frozenset({"gpt-4o"})
    stt_models: frozenset = frozenset({"whisper-1"})
    per_minute: int = 60
    max_concurrent: int = 8
    max_tokens_cap: int = MAX_TOKENS_CAP
    connect_timeout: float = 10.0
    read_timeout: float = 120.0

    def __post_init__(self):
        if self.provider not in UPSTREAMS:
            raise ValueError("Неизвестный поставщик посредника")
        if len(self.token) < MIN_TOKEN_LENGTH:
            raise ValueError("Токен посредника короче 32 символов")
        if not self.api_key.strip():
            raise ValueError("Не задан ключ поставщика")
        if self.token == self.api_key:
            raise ValueError("Токен посредника не должен совпадать с ключом поставщика")
        if not self.chat_models:
            raise ValueError("Нужна хотя бы одна текстовая модель")
        if self.per_minute < 1 or self.max_concurrent < 1 or not 1 <= self.max_tokens_cap <= 32000:
            raise ValueError("Лимиты посредника заданы неверно")


class Reply:
    """Ответ посредника. Тело это bytes или поток кусков, который нужно закрыть через close."""

    def __init__(self, status, body=b"", headers=(), *, close=None):
        self.status, self.body, self.headers, self.close = status, body, list(headers), close


def error_reply(status, code, *, headers=()):
    body = json.dumps({"error": {"code": code}}).encode()
    return Reply(status, body, [("Content-Type", "application/json"), *headers])


class Refusal(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def multipart_fields(body, content_type):
    """Имена полей формы и значение model. Файл не разбирается, нужно лишь убедиться, что он один."""
    media, _, rest = content_type.partition(";")
    if media.strip().lower() != "multipart/form-data":
        raise Refusal(415, "multipart_required")
    boundary = None
    for part in rest.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "boundary":
            boundary = value.strip().strip('"')
    if not boundary or not BOUNDARY.fullmatch(boundary):
        raise Refusal(400, "bad_boundary")
    chunks = body.split(b"--" + boundary.encode("ascii"))
    if len(chunks) < 3 or not chunks[-1].startswith(b"--"):
        raise Refusal(400, "bad_multipart")
    names, model = [], None
    for chunk in chunks[1:-1]:
        head, separator, content = chunk.lstrip(b"\r\n").partition(b"\r\n\r\n")
        if not separator:
            raise Refusal(400, "bad_multipart")
        match = FIELD_NAME.search(head.decode("latin-1"))
        if match is None:
            raise Refusal(400, "bad_multipart")
        name = match.group(1)
        names.append(name)
        if name == "model":
            model = content[:-2] if content.endswith(b"\r\n") else content
    return names, (model.decode("utf-8", "replace").strip() if model is not None else None)


class Relay:
    def __init__(self, config, *, transport=None, clock=time.monotonic):
        self.config = config
        self.transport = transport
        self.clock = clock
        self._stamps = deque()
        self._lock = threading.Lock()
        self._busy = threading.BoundedSemaphore(config.max_concurrent)
        self._token = config.token.encode()

    def allowed_now(self):
        """Не больше per_minute запросов в минуту. Считаются только запросы с верным токеном."""
        with self._lock:
            now = self.clock()
            while self._stamps and now - self._stamps[0] >= 60:
                self._stamps.popleft()
            if len(self._stamps) >= self.config.per_minute:
                return False
            self._stamps.append(now)
            return True

    def authorized(self, headers):
        value = headers.get("authorization", "")
        scheme, _, given = value.partition(" ")
        if scheme.lower() != "bearer" or not given:
            return False
        return hmac.compare_digest(given.strip().encode("utf-8", "replace"), self._token)

    def handle(self, method, target, headers, rfile):
        """Один запрос. headers с ключами в нижнем регистре, rfile читается только после проверки токена."""
        started = time.monotonic()
        path = target.split("?", 1)[0]
        model = "-"
        try:
            if path == HEALTH_PATH and method == "GET" and "?" not in target:
                return Reply(200, b'{"status":"ok"}', [("Content-Type", "application/json")])
            if method != "POST" or path not in (CHAT_PATH, AUDIO_PATH) or "?" in target:
                return error_reply(404, "not_found")
            if not self.authorized(headers):
                return error_reply(401, "unauthorized")
            if not self.allowed_now():
                return error_reply(429, "relay_rate_limit", headers=[("Retry-After", "30")])
            chat = path == CHAT_PATH
            if not chat and not self.config.stt_models:
                return error_reply(404, "not_found")
            request, model = self.read_request(headers, rfile, chat)
            if not self._busy.acquire(blocking=False):
                return error_reply(503, "relay_busy", headers=[("Retry-After", "5")])
            try:
                reply = self.forward(chat, request)
            except BaseException:
                self._busy.release()
                raise
            if isinstance(reply.body, bytes):
                self._busy.release()
                return reply
            close = reply.close

            def release():
                try:
                    if close:
                        close()
                finally:
                    self._busy.release()

            reply.close = release
            return reply
        except Refusal as refusal:
            return error_reply(refusal.status, refusal.code)
        finally:
            known = path in (CHAT_PATH, AUDIO_PATH, HEALTH_PATH)
            logger.info(
                "path=%s method=%s model=%s ms=%d", path if known else "other",
                method if method in ("GET", "POST") else "other", model, int((time.monotonic() - started) * 1000),
            )

    def read_request(self, headers, rfile, chat):
        if "transfer-encoding" in headers:
            raise Refusal(411, "length_required")
        raw = headers.get("content-length", "")
        if not raw.isascii() or not raw.isdigit():
            raise Refusal(411, "length_required")
        length = int(raw)
        if length > (CHAT_LIMIT if chat else AUDIO_LIMIT):
            raise Refusal(413, "body_too_large")
        body = rfile.read(length)
        if len(body) != length:
            raise Refusal(400, "body_incomplete")
        content_type = headers.get("content-type", "")
        if chat:
            return self.check_chat(body, content_type)
        names, model = multipart_fields(body, content_type)
        if len(set(names)) != len(names) or "file" not in names or "model" not in names:
            raise Refusal(400, "bad_fields")
        if any(name not in AUDIO_FIELDS for name in names):
            raise Refusal(400, "field_not_allowed")
        if model not in self.config.stt_models:
            raise Refusal(400, "model_not_allowed")
        return (body, content_type, False), model

    def check_chat(self, body, content_type):
        if content_type.partition(";")[0].strip().lower() != "application/json":
            raise Refusal(415, "json_required")
        try:
            data = json.loads(body)
        except ValueError:
            raise Refusal(400, "bad_json") from None
        if not isinstance(data, dict):
            raise Refusal(400, "bad_json")
        if any(name not in CHAT_FIELDS for name in data):
            raise Refusal(400, "field_not_allowed")
        model = data.get("model")
        if not isinstance(model, str) or model not in self.config.chat_models:
            raise Refusal(400, "model_not_allowed")
        limit = data.get("max_tokens")
        if type(limit) is not int or not 1 <= limit <= self.config.max_tokens_cap:
            raise Refusal(400, "max_tokens_required")
        if data.get("n", 1) != 1 or not isinstance(data.get("messages"), list):
            raise Refusal(400, "bad_fields")
        if "stream" in data and not isinstance(data["stream"], bool):
            raise Refusal(400, "bad_fields")
        # Дальше уходит то, что мы проверили, а не исходные байты. Повтор ключей в JSON ничего не обходит.
        # Sol uses the completion-token limit, which includes reasoning tokens. The core
        # still sends max_tokens; translate only after enforcing the relay's hard cap.
        if model.startswith("gpt-6") and model.endswith("-sol"):
            data["max_completion_tokens"] = data.pop("max_tokens")
            data.pop("temperature", None)
        return (json.dumps(data, ensure_ascii=False).encode(), "application/json", data.get("stream") is True), model

    def forward(self, chat, request):
        content, content_type, stream = request
        url = UPSTREAMS[self.config.provider] + (CHAT_PATH if chat else AUDIO_PATH)[len("/v1"):]
        # Заголовки собираются с нуля. Из запроса ядра не уходит ничего, в том числе его Authorization.
        outgoing = {
            "Authorization": "Bearer " + self.config.api_key, "Content-Type": content_type,
            "Accept": "text/event-stream" if stream else "application/json", "User-Agent": "beresta-ai-relay",
        }
        client = httpx.Client(
            timeout=httpx.Timeout(self.config.read_timeout, connect=self.config.connect_timeout, write=30, pool=10),
            follow_redirects=False, trust_env=False, transport=self.transport,
        )
        try:
            upstream = client.send(client.build_request("POST", url, headers=outgoing, content=content), stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            client.close()
            return error_reply(502, "upstream_unreachable")
        except httpx.HTTPError:
            client.close()
            return error_reply(504, "upstream_unknown")

        def close():
            upstream.close()
            client.close()

        extra = [(name.title(), upstream.headers[name]) for name in PASS_HEADERS if name in upstream.headers]
        if upstream.status_code != 200:
            # Тело ошибки ядру не нужно. Передаём только код и Retry-After.
            # Коды вне 400..599 (например, перенаправление) ядру не нужны, это просто сбой шлюза.
            status = upstream.status_code if 400 <= upstream.status_code <= 599 else 502
            try:
                reply = error_reply(
                    status, f"upstream_{upstream.status_code}",
                    headers=[item for item in extra if item[0] == "Retry-After"],
                )
            finally:
                close()
            return reply
        if stream:
            return Reply(200, self.chunks(upstream), extra, close=close)
        try:
            body = bytearray()
            for chunk in upstream.iter_bytes():
                body.extend(chunk)
                if len(body) > RESPONSE_LIMIT:
                    return error_reply(502, "response_too_large")
        except httpx.HTTPError:
            return error_reply(504, "upstream_unknown")
        finally:
            close()
        return Reply(200, bytes(body), extra)

    @staticmethod
    def chunks(upstream):
        total = 0
        for chunk in upstream.iter_bytes():
            total += len(chunk)
            if total > RESPONSE_LIMIT:
                return
            yield chunk
