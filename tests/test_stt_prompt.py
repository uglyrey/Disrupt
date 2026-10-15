import io
from dataclasses import replace

import httpx
import pytest

from app.config import PROGRAM_BASE_URL, STT_PROMPT_DEFAULT, STT_PROMPT_MAX, Settings
from app.speech import CloudRuSpeechProvider, make_fallback_speech_provider, make_speech_provider


def field(request, name):
    """Value of a multipart form field of a captured request."""
    body = request.content.decode("utf-8", "ignore")
    marker = f'name="{name}"\r\n\r\n'
    if marker not in body:
        return None
    return body.split(marker, 1)[1].split("\r\n", 1)[0]


def run(provider_kwargs):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "Сегодня в 16:00"})

    provider = CloudRuSpeechProvider("synthetic-secret", "synthetic-stt", base_url=PROGRAM_BASE_URL,
                                     transport=httpx.MockTransport(handler), **provider_kwargs)
    assert provider.transcribe(io.BytesIO(b"fixture"), media_type="audio/ogg") == "Сегодня в 16:00"
    assert len(seen) == 1
    return seen[0]


def test_prompt_and_language_are_sent():
    request = run({"prompt": STT_PROMPT_DEFAULT})
    assert field(request, "prompt") == STT_PROMPT_DEFAULT
    assert field(request, "language") == "ru"
    assert field(request, "model") == "synthetic-stt"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_prompt_disables_the_field(value):
    request = run({"prompt": value})
    assert field(request, "prompt") is None
    assert field(request, "language") == "ru"


def test_prompt_is_not_sent_unless_asked():
    assert field(run({}), "prompt") is None


def test_default_prompt_is_short_and_has_digit_times():
    assert 0 < len(STT_PROMPT_DEFAULT) <= STT_PROMPT_MAX == 200
    assert "16:00" in STT_PROMPT_DEFAULT and "9:30" in STT_PROMPT_DEFAULT


def test_prompt_is_cut_to_the_cap():
    request = run({"prompt": "я" * 500})
    assert field(request, "prompt") == "я" * STT_PROMPT_MAX


def test_setting_from_environment(monkeypatch):
    monkeypatch.delenv("NOTES_STT_PROMPT", raising=False)
    assert Settings.from_env().stt_prompt == STT_PROMPT_DEFAULT
    monkeypatch.setenv("NOTES_STT_PROMPT", "")
    assert Settings.from_env().stt_prompt == ""
    monkeypatch.setenv("NOTES_STT_PROMPT", "Звонок в 10:15.")
    assert Settings.from_env().stt_prompt == "Звонок в 10:15."
    monkeypatch.setenv("NOTES_STT_PROMPT", "я" * (STT_PROMPT_MAX + 1))
    with pytest.raises(ValueError, match="NOTES_STT_PROMPT"):
        Settings.from_env()


def test_main_provider_gets_the_setting_and_fallback_does_not(monkeypatch):
    monkeypatch.setenv("NOTES_STT_ENABLED", "true")
    monkeypatch.setenv("NOTES_STT_PROTOCOL_CONFIRMED", "true")
    monkeypatch.setenv("NOTES_STT_MODEL", "synthetic-stt")
    monkeypatch.setenv("NOTES_CLOUDRU_API_KEY", "synthetic-secret")
    monkeypatch.delenv("NOTES_CLOUDRU_API_KEY_FILE", raising=False)
    settings = Settings(provider="cloudru", auto_worker=False, cloudru_model="synthetic-model",
                        live_call_limit=1, live_user_call_limit=1, allow_live_requests=True)
    assert make_speech_provider(settings).prompt == STT_PROMPT_DEFAULT
    assert make_speech_provider(replace(settings, stt_prompt="")).prompt == ""
    fallback = make_fallback_speech_provider(replace(
        settings, fallback_enabled=True, fallback_base_url="https://relay.example.invalid/v1",
        fallback_token="t" * 40), transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    assert fallback is not None and fallback.prompt == ""
