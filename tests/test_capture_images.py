import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Capture, Job, Note, NoteImage, Revision
from tests.conftest import register
from tests.test_note_images import picture


def create(client, *, text="Мои мысли", mode="manual", key=None, sources=None):
    return client.post("/api/v1/captures/with-images", headers={"Idempotency-Key": key or str(uuid.uuid4())},
                       data={"text": text, "processing_mode": mode},
                       files=[("images", ("photo.png", source, "image/png")) for source in (sources or [picture()])])


def test_creation_original_revision_and_replay(app, client):
    register(client)
    key = str(uuid.uuid4())
    first = create(client, key=key)
    assert first.status_code == 202, first.text
    assert create(client, key=key).json() == first.json()
    assert create(client, key=key, sources=[picture("JPEG")]).status_code == 409
    note = client.get("/api/v1/notes/" + first.json()["note_id"]).json()
    capture = client.get("/api/v1/captures/" + first.json()["capture_id"]).json()
    assert capture["original_text"] == "Мои мысли"
    assert len(capture["images"]) == 1
    assert capture["images"][0]["url"] in note["markdown"]
    with app.state.sessions() as db:
        revision = db.scalar(select(Revision).where(Revision.note_id == note["id"]))
        assert capture["images"][0]["url"] in revision.snapshot["markdown"]
        assert db.scalar(select(func.count()).select_from(Capture)) == 1
    with TestClient(app) as outsider:
        register(outsider, "outsider")
        assert outsider.get(capture["images"][0]["url"]).status_code == 404


def test_invalid_image_or_quota_rolls_back_whole_capture(app, client, monkeypatch):
    register(client)
    assert create(client, sources=[picture(), b"invalid"]).status_code == 422
    monkeypatch.setattr("app.note_images.MAX_USER_BYTES", 1)
    assert create(client, mode="ai").status_code == 429
    with app.state.sessions() as db:
        for model in (Capture, Job, Note, NoteImage, Revision):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_image_only_never_queues_ai(app, client):
    register(client)
    result = create(client, text="", mode="ai")
    assert result.status_code == 202, result.text
    assert result.json()["status"] == "saved"
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(Note)).provider == "manual"


def test_images_survive_queue_failure_and_are_adopted_once(app, client):
    from app.providers import MockProvider
    from app.services import save_revision
    from tests.test_notes_manage import run_worker

    register(client)
    first = create(client, mode="ai").json()
    source = client.get("/api/v1/captures/" + first["capture_id"]).json()
    url = source["images"][0]["url"]
    assert client.get(url).status_code == 200
    with app.state.sessions() as db:
        assert db.scalar(select(NoteImage)).note_id is None
        job = db.get(Job, first["id"])
        job.status = "failed"
        db.commit()
    assert client.get(url).status_code == 200
    with app.state.sessions() as db:
        job = db.get(Job, first["id"])
        job.status = "queued"
        db.commit()
    assert run_worker(app, MockProvider())
    job = client.get("/api/v1/jobs/" + first["id"]).json()
    note = client.get("/api/v1/notes/" + job["note_id"]).json()
    assert url in note["markdown"]
    with app.state.sessions() as db:
        stored = db.get(Note, note["id"])
        stored.version += 1
        save_revision(db, stored)
        db.commit()
        assert stored.markdown.count(url) == 1


def test_csrf_large_body_and_no_files(client):
    register(client)
    assert client.post("/api/v1/captures/with-images", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert create(client, sources=[b"x" * (10 * 1024 * 1024 + 65537)]).status_code == 413
    response = client.post("/api/v1/captures/with-images", headers={"Idempotency-Key": "empty"}, data={"text": "abc"})
    assert response.status_code == 422
    policy = client.get("/").headers["content-security-policy"]
    assert "img-src 'self' blob:;" in policy
    assert "img-src *" not in policy and "data:" not in policy
