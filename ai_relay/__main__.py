"""Запуск посредника. Все настройки берутся из переменных окружения AI_RELAY_*."""

import os
import sys

from ai_relay.relay import Relay, RelayConfig, read_secret
from ai_relay.server import configure_logging, make_server


def split(value):
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def config_from_env(env=os.environ):
    return RelayConfig(
        token=read_secret(env["AI_RELAY_TOKEN_FILE"]),
        api_key=read_secret(env["AI_RELAY_OPENAI_KEY_FILE"]),
        chat_models=split(env.get("AI_RELAY_CHAT_MODELS", "gpt-4o")),
        stt_models=split(env.get("AI_RELAY_STT_MODELS", "whisper-1")),
        per_minute=int(env.get("AI_RELAY_PER_MINUTE", "60")),
    )


def main():
    configure_logging()
    try:
        config = config_from_env()
    except (KeyError, OSError, ValueError) as error:
        # Сообщение об ошибке не содержит значений секретов.
        sys.exit("Посредник не запущен, проверьте настройки: " + type(error).__name__)
    host = os.environ.get("AI_RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("AI_RELAY_PORT", "18100"))
    server = make_server(Relay(config), host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
