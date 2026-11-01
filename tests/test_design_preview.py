"""Просмотр дизайна без сервера: отдаёт страницу, стили и тестовые данные."""
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "design_preview.py"


@pytest.fixture(autouse=True)
def no_network():
    # Общий запрет сети из conftest здесь снят: тест говорит только с сервером, который сам поднял на 127.0.0.1.
    yield


def load():
    spec = importlib.util.spec_from_file_location("design_preview", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch(url, data=None):
    request = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def test_preview_serves_pages_and_fixture_api():
    module = load()
    server = module.build_server(0)  # порт 0: свободный порт выбирает система
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        status, headers, body = fetch(base + "/")
        assert status == 200 and "text/html" in headers["Content-Type"]
        assert b'data-source-token="demo"' in body and b"/static/design.css" in body
        status, headers, body = fetch(base + "/static/design.css")
        assert status == 200 and b"--c-green" in body
        assert fetch(base + "/static/fonts/onest-cyrillic-400-normal.woff2")[1]["Content-Type"] == "font/woff2"
        assert fetch(base + "/static/../main.py")[0] == 404
        assert fetch(base + "/health")[0] == 200
        # Без входа заметок нет, вход любым способом открывает тестового пользователя.
        assert fetch(base + "/api/v1/notes")[0] == 401
        assert fetch(base + "/api/v1/auth/me")[0] == 401
        status, _, body = fetch(base + "/api/v1/auth/email/code/confirm", {"code": "000000"})
        assert status == 200 and json.loads(body)["username"] == "demo"
        status, _, body = fetch(base + "/api/v1/notes")
        notes = json.loads(body)
        assert status == 200 and 8 <= len(notes) <= 12
        categories = {c["name"] for c in json.loads(fetch(base + "/api/v1/categories")[2])}
        assert {"Общее", "Учёба", "Стартап", "Работа"} <= categories
        note = json.loads(fetch(base + "/api/v1/notes/" + notes[0]["id"])[2])
        assert note["items"] and note["markdown"]
        tasks = json.loads(fetch(base + "/api/v1/tasks?status=open")[2])
        assert tasks["counts"]["open"] > 0
        # Запись принимается в память.
        fetch(base + "/api/v1/captures/text", {"text": "Новая мысль про берёзу", "processing_mode": "manual"})
        assert len(json.loads(fetch(base + "/api/v1/notes?limit=50")[2])) == len(notes) + 1
        # Неизвестный путь не роняет страницу.
        assert fetch(base + "/api/v1/something-new")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
