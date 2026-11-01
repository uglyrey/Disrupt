"""Source filters over notes made by the real entry points, not by editing captures."""

from fastapi.testclient import TestClient

from app.audio_storage import AudioStorage
from app.speech import MockSpeechProvider
from app.worker import Worker
from tests.test_audio_storage import wav
from tests.test_internal import BASE, HEADERS, SERVICE_TOKEN, linked


def listed(client, **params):
    response = client.get("/api/v1/notes", params=params)
    assert response.status_code == 200, response.text
    return {row["id"]: (row["channel"], row["input_kind"]) for row in response.json()}


def test_filters_over_real_entry_points(app_factory, tmp_path):
    app = app_factory(internal_api_token=SERVICE_TOKEN, audio_storage=AudioStorage(tmp_path / "audio"))
    with TestClient(app) as client:
        linked(client)
        client.post("/internal/v1/telegram/updates", headers=HEADERS,
                    json={**BASE, "update_id": 2, "text": "Заметка из бота про молоко", "processing_mode": "ai"})
        client.post("/internal/v1/telegram/voice", headers={"Authorization": "Bearer " + SERVICE_TOKEN},
                    data={**BASE, "update_id": 3, "processing_mode": "ai"}, files={"audio": ("a", wav())})
        client.post("/api/v1/captures/text", json={"text": "Запись с сайта про хлеб"},
                    headers={"Idempotency-Key": "web-1"})
        client.post("/api/v1/captures/audio", headers={"Idempotency-Key": "web-audio"},
                    files={"audio": ("a", wav(0.3))}, data={"processing_mode": "ai"})
        for _ in range(6):
            Worker(app.state.sessions, app.state.settings, audio_storage=app.state.audio_storage,
                   speech_provider=MockSpeechProvider("Голосовая запись про дела")).run_once()
        everything = listed(client)
        pairs = sorted(everything.values())
        assert pairs == [("telegram", "audio"), ("telegram", "text"), ("web", "audio"), ("web", "text")], pairs
        assert sorted(listed(client, channel="telegram").values()) == [("telegram", "audio"), ("telegram", "text")]
        assert sorted(listed(client, input_kind="audio").values()) == [("telegram", "audio"), ("web", "audio")]
        assert sorted(listed(client, input_kind="text").values()) == [("telegram", "text"), ("web", "text")]
