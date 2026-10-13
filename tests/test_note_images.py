import io
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select, text

from app import crypto, data_crypto
from app.models import NoteImage
from tests.conftest import register
from tests.test_structure import manual


def picture(fmt="PNG", size=(24, 18)):
    output = io.BytesIO()
    Image.new("RGB", size, (100, 150, 200)).save(output, format=fmt)
    return output.getvalue()


def upload(client, note, source=None, key=None, **kwargs):
    return client.post("/api/v1/notes/" + note["id"] + "/images",
                       headers={"Idempotency-Key": key or str(uuid.uuid4()), **kwargs},
                       files={"image": ("untrusted.png", source or picture(), "image/png")})


def test_upload_private_normalized_encrypted_and_idempotent(app, client):
    register(client)
    note = manual(client)
    key = str(uuid.uuid4())
    first = upload(client, note, key=key)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["width"] == 24 and body["height"] == 18
    replay = upload(client, note, key=key)
    assert replay.json() == body
    assert upload(client, note, picture("JPEG"), key=key).status_code == 409
    response = client.get(body["url"])
    assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"
    with Image.open(io.BytesIO(response.content)) as restored:
        assert restored.size == (24, 18)
    with app.state.engine.connect() as db:
        stored = db.execute(text("SELECT data FROM note_images WHERE id=:id"), {"id": body["id"]}).scalar()
        assert crypto.is_encrypted_bytes(stored) and stored != response.content
    assert data_crypto.database_status(app.state.sessions)["note_images.data"] == {"plain": 0, "current": 1, "old": 0}
    assert data_crypto.encrypt_database(app.state.sessions)["note_images.data"]["skipped"] == 1
    with TestClient(app) as outsider:
        assert outsider.get(body["url"]).status_code == 401
        register(outsider, "other_owner")
        assert outsider.get(body["url"]).status_code == 404
        assert upload(outsider, note).status_code == 404


@pytest.mark.parametrize("source", [b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"<html>bad</html>", b"invalid", picture()[:30]])
def test_reject_untrusted_or_truncated_files_without_rows(app, client, source):
    register(client)
    note = manual(client)
    assert upload(client, note, source).status_code == 422
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(NoteImage)) == 0


def test_body_limit_csrf_key_and_quota(app, client, monkeypatch):
    register(client)
    note = manual(client)
    assert upload(client, note, b"x" * (5 * 1024 * 1024 + 65537)).status_code == 413
    assert upload(client, note, key="short").status_code == 422
    assert upload(client, note, **{"X-CSRF-Token": "wrong"}).status_code == 403
    monkeypatch.setattr("app.note_images.MAX_IMAGES_PER_NOTE", 1)
    assert upload(client, note).status_code == 201
    assert upload(client, note).status_code == 429


def test_trash_restore_and_permanent_delete(app, client):
    register(client)
    note = manual(client)
    asset = upload(client, note).json()
    assert client.delete(f'/api/v1/notes/{note["id"]}?version={note["version"]}').status_code == 204
    assert client.get(asset["url"]).status_code == 404
    assert client.post('/api/v1/trash/'+note["id"]+'/restore').status_code == 200
    assert client.get(asset["url"]).status_code == 200
    current = client.get('/api/v1/notes/'+note["id"]).json()
    assert client.delete(f'/api/v1/notes/{note["id"]}?version={current["version"]}').status_code == 204
    assert client.delete('/api/v1/trash/'+note["id"]).status_code == 204
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(NoteImage)) == 0


def test_image_rotation_keeps_content(app, client, tmp_path):
    register(client)
    note = manual(client)
    asset = upload(client, note).json()
    original = client.get(asset["url"]).content
    old = app.state.settings.data_key_file
    new = tmp_path / "new-test-key"
    new.write_text(crypto.generate_key())
    crypto.configure("required", str(new), old)
    report = data_crypto.encrypt_database(app.state.sessions)
    assert report["note_images.data"]["rotated"] == 1
    crypto.configure("required", str(new))
    assert client.get(asset["url"]).content == original


def test_normalization_resize_orientation_and_metadata():
    from app.note_images import normalize_image
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "Private metadata must disappear"
    output = io.BytesIO()
    Image.new("RGB", (3000, 1000)).save(output, "JPEG", exif=exif)
    data, width, height = normalize_image(output.getvalue())
    assert (width, height) == (640, 1920)
    with Image.open(io.BytesIO(data)) as image:
        assert not image.getexif()
        assert b"Private metadata" not in data


def test_user_quota_pixels_and_animation(app, client, monkeypatch):
    register(client)
    note = manual(client)
    monkeypatch.setattr("app.note_images.MAX_USER_BYTES", 1)
    assert upload(client, note).status_code == 429
    monkeypatch.setattr("app.note_images.MAX_USER_BYTES", 50 * 1024 * 1024)
    monkeypatch.setattr("app.note_images.MAX_PIXELS", 200)
    assert upload(client, note).status_code == 413
    monkeypatch.setattr("app.note_images.MAX_PIXELS", 24_000_000)
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, "WEBP", save_all=True,
                                        append_images=[Image.new("RGB", (8, 8), "blue")], duration=100)
    assert upload(client, note, output.getvalue()).status_code == 422
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(NoteImage)) == 0
