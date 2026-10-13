"""Trash, deleted tasks, AI merge, manual links and guide state (docs/notes-manage-v1.md)."""

import time
from dataclasses import replace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.assistant_retrieval import context_for_ask
from app.audio_storage import AudioStorage
from app.config import Settings
from app.db import make_database
from app.models import (
    Capture,
    Category,
    Item,
    Job,
    Note,
    NoteLink,
    Outbox,
    Reminder,
    Revision,
    TranscriptRevision,
    new_id,
)
from app.notes_manage import purge_expired
from app.providers import MERGE_MARKER, MockProvider, ProviderError
from app.scheduler import Scheduler
from app.schemas import ProposedItem, StructuredNote
from app.worker import Worker
from tests.conftest import register
from tests.test_notes import ready_note
from tests.test_processing_replies import processed
from tests.test_reminders import add as add_reminder
from tests.test_reminders import claim, payload
from tests.test_reminders import reminders as reminders
from tests.test_structure import add_item, manual

DAY = 86400


def fresh(client, note):
    return client.get("/api/v1/notes/" + note["id"]).json()


def table_count(app, model):
    with app.state.sessions() as db:
        return db.scalar(select(func.count()).select_from(model))


class MergeProvider(MockProvider):
    """Answers a merge with one task and no category, like a careful model."""

    def __init__(self):
        self.calls = 0

    def structure(self, text, *, categories=()):
        self.calls += 1
        assert text.startswith(MERGE_MARKER)
        return StructuredNote(
            title="Единый план", markdown="## План\n\nКупить молоко и позвонить Оле.", conclusions=[],
            items=[ProposedItem(kind="task", text="Купить молоко", source_quote="Купить молоко")],
            category_name=None,
        )


class FailingProvider(MockProvider):
    def structure(self, text, *, categories=()):
        raise ProviderError("provider_bad_request")


def run_worker(app, provider=None):
    return Worker(app.state.sessions, app.state.settings, provider or MergeProvider()).run_once()


def merge(client, ids, key="merge-1"):
    return client.post("/api/v1/notes/merge", json={"note_ids": ids, "idempotency_key": key})


# ---- Trash ----

def test_trash_hides_a_note_everywhere_and_restore_brings_it_back(app, client):
    owner = register(client)
    note = add_item(client, manual(client, "Купить молоко и хлеб"), text="Позвонить Оле")
    other = manual(client, "Вторая заметка")
    path = "/api/v1/notes/" + note["id"]
    assert client.delete(path, params={"version": note["version"] - 1}).status_code == 409
    assert client.delete(path, params={"version": note["version"]}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.delete(path, params={"version": 0}).status_code == 422
    assert client.delete(path, params={"version": note["version"]}).status_code == 204

    assert client.get(path).status_code == 404
    assert [n["id"] for n in client.get("/api/v1/notes").json()] == [other["id"]]
    assert client.get("/api/v1/notes", params={"q": "молоко"}).json() == []
    tasks = client.get("/api/v1/tasks", params={"status": "all"}).json()
    assert tasks["items"] == [] and tasks["counts"] == {"open": 0, "completed": 0}
    assert client.get(path + "/related").status_code == 404
    assert [n["id"] for n in client.get("/api/v1/graph").json()["nodes"]] == [other["id"]]
    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["totals"]["notes"] == 1 and dashboard["tasks"]["open"] == 0
    with app.state.sessions() as db:
        assert [n["id"] for n in context_for_ask(db, owner["id"], "Вторая заметка").notes] == [other["id"]]
    body = {"version": 99, "title": "Новое", "markdown": "Текст"}
    assert client.patch(path, json=body).status_code == 404
    assert client.post(path + "/items", json={"version": 99, "kind": "task", "text": "Ещё"}).status_code == 404
    assert client.get(path + "/reminders").status_code == 404

    trash = client.get("/api/v1/trash").json()
    assert trash["trash_days"] == 30 and [n["id"] for n in trash["notes"]] == [note["id"]]
    row = trash["notes"][0]
    assert row["title"] == note["title"] and row["merged_into"] is None
    assert row["purge_at"] == pytest.approx(row["deleted_at"] + 30 * DAY)

    restored = client.post(f"/api/v1/trash/{note['id']}/restore")
    assert restored.status_code == 200 and restored.json()["id"] == note["id"]
    assert restored.json()["items"][0]["text"] == "Позвонить Оле"
    assert client.get(path).status_code == 200
    assert client.get("/api/v1/trash").json()["notes"] == []
    assert client.post(f"/api/v1/trash/{note['id']}/restore").status_code == 404
    assert client.get("/api/v1/tasks").json()["counts"]["open"] == 1


def test_trash_belongs_to_its_owner(app, client):
    register(client)
    note = manual(client, "Личное")
    other = TestClient(app)
    register(other, "second")
    path = "/api/v1/notes/" + note["id"]
    assert other.delete(path, params={"version": 1}).status_code == 404
    assert client.delete(path, params={"version": note["version"]}).status_code == 204
    assert other.get("/api/v1/trash").json()["notes"] == []
    assert other.post(f"/api/v1/trash/{note['id']}/restore").status_code == 404
    assert other.delete(f"/api/v1/trash/{note['id']}").status_code == 404
    assert client.get("/api/v1/trash").json()["notes"][0]["id"] == note["id"]
    assert client.delete(f"/api/v1/trash/{note['id']}", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/v1/trash").status_code == 401


def test_reminders_wait_in_the_trash_and_late_ones_are_cancelled_on_restore(reminders):
    app, client, ids = reminders
    with_item = add_reminder(client, ids, payload(ids["item"]), "r-item").json()["id"]
    whole_note = add_reminder(client, ids, payload(None), "r-note").json()["id"]
    version = client.get("/api/v1/notes/" + ids["note"]).json()["version"]
    assert client.delete("/api/v1/notes/" + ids["note"], params={"version": version}).status_code == 204
    with app.state.sessions.begin() as db:
        for reminder_id in (with_item, whole_note):
            db.get(Reminder, reminder_id).scheduled_at = time.time() - 1  # Time passes in the trash.
    assert Scheduler(app.state.sessions).run_once() == 0
    assert table_count(app, Outbox) == 0
    assert client.post(f"/api/v1/trash/{ids['note']}/restore").status_code == 200
    statuses = {r["id"]: r["status"] for r in client.get("/api/v1/notes/" + ids["note"] + "/reminders").json()}
    assert statuses == {with_item: "cancelled", whole_note: "cancelled"}
    assert Scheduler(app.state.sessions).run_once() == 0


def test_reminder_still_ahead_resumes_after_restore(reminders):
    app, client, ids = reminders
    reminder_id = add_reminder(client, ids, payload(ids["item"]), "r-item").json()["id"]
    version = client.get("/api/v1/notes/" + ids["note"]).json()["version"]
    client.delete("/api/v1/notes/" + ids["note"], params={"version": version})
    client.post(f"/api/v1/trash/{ids['note']}/restore")
    assert client.get("/api/v1/reminders/" + reminder_id).json()["status"] == "confirmed"
    with app.state.sessions.begin() as db:
        db.get(Reminder, reminder_id).scheduled_at = time.time() - 1
    assert Scheduler(app.state.sessions).run_once() == 1


def test_telegram_reply_of_a_trashed_note_is_not_sent(reminders):
    app, client, ids = reminders
    processed(app, client)
    with app.state.sessions() as db:
        note = db.scalar(select(Note).where(Note.id != ids["note"]))
        note_id, version = note.id, note.version
    assert client.delete("/api/v1/notes/" + note_id, params={"version": version}).status_code == 204
    assert claim(client).json() == {"items": []}
    assert client.post(f"/api/v1/trash/{note_id}/restore").status_code == 200
    assert len(claim(client).json()["items"]) == 1


def seed_old_note(app, user_id, storage, *, age_days, voice=True):
    """A trashed note with all that can hang on one: audio file, transcript, job, items, reminders, outbox, links."""
    capture_id = new_id()
    key = capture_id + ".audio"
    if voice:
        (storage.root / key).write_bytes(b"audio")
    with app.state.sessions.begin() as db:
        db.add(Capture(
            id=capture_id, user_id=user_id, original_text="Старая запись", idempotency_key="old-" + capture_id,
            input_kind="audio" if voice else "text", audio_key=key if voice else None,
        ))
        db.flush()
        db.add(TranscriptRevision(capture_id=capture_id, version=1, text="Старая запись", origin="stt"))
        job = Job(capture_id=capture_id, user_id=user_id, status="succeeded", provider="mock")
        note = Note(
            capture_id=capture_id, user_id=user_id, title="Старая", markdown="Старая запись", conclusions=[],
            provider="mock", deleted_at=time.time() - age_days * DAY,
        )
        survivor_capture = Capture(user_id=user_id, original_text="Живая", idempotency_key="live-" + capture_id)
        db.add_all([job, note, survivor_capture])
        db.flush()
        live = Note(
            capture_id=survivor_capture.id, user_id=user_id, title="Живая", markdown="Живая", conclusions=[],
            provider="mock",
        )
        db.add(live)
        db.flush()
        item = Item(user_id=user_id, note_id=note.id, kind="task", text="Старая задача")
        db.add_all([item, Revision(note_id=note.id, version=1, snapshot={})])
        db.flush()
        reminder = Reminder(
            user_id=user_id, note_id=note.id, item_id=item.id, scheduled_at=time.time() + 100,
            timezone="UTC", text="Позвонить", idempotency_key="old-reminder-" + note.id,
        )
        db.add(reminder)
        db.flush()
        db.add_all([
            Outbox(reminder_id=reminder.id, user_id=user_id, bot_id=1, chat_id=1, generation=1),
            Outbox(job_id=job.id, user_id=user_id, bot_id=1, chat_id=1, generation=1),
        ])
        a, b = sorted([note.id, live.id])
        db.add(NoteLink(user_id=user_id, note_a=a, note_b=b))
        return note.id, live.id, key


def test_purge_erases_an_old_note_with_its_files_and_leaves_the_rest(app, client, tmp_path):
    owner = register(client)
    storage = AudioStorage(tmp_path / "audio")
    old_id, live_id, key = seed_old_note(app, owner["id"], storage, age_days=31)
    recent_id, _, recent_key = seed_old_note(app, owner["id"], storage, age_days=29)
    counts = purge_expired(app.state.sessions, app.state.settings, storage)
    assert counts == {"notes": 1, "items": 0, "skipped": 0}
    assert not (storage.root / key).exists() and (storage.root / recent_key).exists()
    with app.state.sessions() as db:
        assert db.get(Note, old_id) is None and db.get(Note, recent_id) is not None and db.get(Note, live_id) is not None
        assert db.scalar(select(func.count()).select_from(Item).where(Item.note_id == old_id)) == 0
        assert db.scalar(select(func.count()).select_from(Reminder).where(Reminder.note_id == old_id)) == 0
        assert db.scalar(select(func.count()).select_from(Revision).where(Revision.note_id == old_id)) == 0
        assert db.scalar(select(func.count()).select_from(NoteLink).where(NoteLink.note_a == old_id)) == 0
        assert db.scalar(select(func.count()).select_from(NoteLink).where(NoteLink.note_b == old_id)) == 0
        # One Capture, Job, transcript and two Outbox rows are left for the recent note only.
        assert db.scalar(select(func.count()).select_from(Outbox)) == 2
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.scalar(select(func.count()).select_from(TranscriptRevision)) == 1
    assert purge_expired(app.state.sessions, app.state.settings, storage) == {"notes": 0, "items": 0, "skipped": 0}


def test_purge_keeps_a_voice_note_when_the_audio_folder_is_not_available(app, client, tmp_path):
    owner = register(client)
    storage = AudioStorage(tmp_path / "audio")
    old_id, _, key = seed_old_note(app, owner["id"], storage, age_days=40)
    assert purge_expired(app.state.sessions, app.state.settings, None)["skipped"] == 1
    assert (storage.root / key).exists() and table_count(app, Note) == 2
    read_only = AudioStorage(tmp_path / "audio", read_only=True)
    assert purge_expired(app.state.sessions, app.state.settings, read_only)["skipped"] == 1
    assert purge_expired(app.state.sessions, app.state.settings, storage)["notes"] == 1
    assert not (storage.root / key).exists()


def test_purge_batches_and_trash_days_setting(app_factory):
    app = app_factory(trash_days=1)
    with TestClient(app) as client:
        owner = register(client)
        storage = AudioStorage(app.state.audio_storage.root)
        for _ in range(3):
            seed_old_note(app, owner["id"], storage, age_days=2, voice=False)
        assert purge_expired(app.state.sessions, app.state.settings, storage, batch=2)["notes"] == 2
        assert purge_expired(app.state.sessions, app.state.settings, storage, batch=2)["notes"] == 1
        assert client.get("/api/v1/trash").json()["trash_days"] == 1


def test_delete_forever_erases_a_trashed_note_and_its_audio(app, client):
    owner = register(client)
    storage = app.state.audio_storage
    old_id, _, key = seed_old_note(app, owner["id"], storage, age_days=1)
    assert client.delete(f"/api/v1/trash/{old_id}").status_code == 204
    assert not (storage.root / key).exists() and table_count(app, Note) == 1
    assert client.delete(f"/api/v1/trash/{old_id}").status_code == 404
    live = manual(client, "Живая заметка")
    assert client.delete(f"/api/v1/trash/{live['id']}").status_code == 404  # Only the trash can be emptied.
    assert client.get("/api/v1/notes/" + live["id"]).status_code == 200


def test_trash_settings_are_checked():
    assert Settings().trash_days == 30
    for value in (0, 4000):
        with pytest.raises(ValueError, match="NOTES_TRASH_DAYS"):
            Settings(trash_days=value)


# ---- Tasks and ideas ----

def test_deleting_a_task_can_be_undone_and_cancels_its_reminder(app, client):
    register(client)
    note = add_item(client, manual(client, "Дела"), text="Позвонить Оле")
    task = note["items"][0]
    created = client.post(
        "/api/v1/notes/" + note["id"] + "/reminders", json=payload(task["id"]), headers={"Idempotency-Key": "k1"},
    )
    assert created.status_code == 201
    url = f"/api/v1/notes/{note['id']}/items/{task['id']}"
    assert client.delete(url, params={"version": note["version"] - 1}).status_code == 409
    assert client.delete(url, params={"version": note["version"]}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    deleted = client.delete(url, params={"version": note["version"]})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["items"] == [] and deleted.json()["version"] == note["version"] + 1
    assert client.get("/api/v1/reminders/" + created.json()["id"]).json()["status"] == "cancelled"
    assert client.get("/api/v1/tasks", params={"status": "all"}).json()["items"] == []
    assert client.get("/api/v1/dashboard").json()["tasks"]["open"] == 0
    assert client.delete(url, params={"version": deleted.json()["version"]}).status_code == 404
    edit = {"version": deleted.json()["version"], "kind": "task", "text": "Другое", "status": "open"}
    assert client.patch(url, json=edit).status_code == 404
    revisions = client.get(f"/api/v1/notes/{note['id']}/revisions").json()
    assert revisions[0]["version"] == deleted.json()["version"] and revisions[0]["items"] == []

    back = client.post(url + "/restore")
    assert back.status_code == 200 and [i["id"] for i in back.json()["items"]] == [task["id"]]
    assert back.json()["version"] == deleted.json()["version"] + 1
    assert client.get("/api/v1/reminders/" + created.json()["id"]).json()["status"] == "cancelled"
    assert client.post(url + "/restore").status_code == 404  # Nothing deleted any more.
    assert client.get("/api/v1/tasks").json()["counts"]["open"] == 1


def test_task_undo_is_limited_and_old_deleted_tasks_are_purged(app, client):
    register(client)
    note = add_item(client, manual(client, "Дела"), text="Старая")
    task = note["items"][0]
    url = f"/api/v1/notes/{note['id']}/items/{task['id']}"
    assert client.delete(url, params={"version": note["version"]}).status_code == 200
    with app.state.sessions.begin() as db:
        db.get(Item, task["id"]).deleted_at = time.time() - 31 * DAY
        db.add(Reminder(
            user_id=db.get(Item, task["id"]).user_id, note_id=note["id"], item_id=task["id"], status="cancelled",
            scheduled_at=time.time() + 5, timezone="UTC", text="Позвонить",
        ))
    assert client.post(url + "/restore").status_code == 404
    counts = purge_expired(app.state.sessions, app.state.settings)
    assert counts == {"notes": 0, "items": 1, "skipped": 0}
    assert table_count(app, Item) == 0 and table_count(app, Reminder) == 0


def test_task_delete_is_private(app, client):
    register(client)
    note = add_item(client, manual(client, "Дела"))
    task = note["items"][0]
    other = TestClient(app)
    register(other, "second")
    url = f"/api/v1/notes/{note['id']}/items/{task['id']}"
    assert other.delete(url, params={"version": note["version"]}).status_code == 404
    assert other.post(url + "/restore").status_code == 404
    assert client.get("/api/v1/notes/" + note["id"]).json()["items"][0]["id"] == task["id"]


def test_ai_conclusion_can_be_removed(app, client):
    register(client)
    note = ready_note(app, client)
    conclusion = note["conclusions"][0]
    url = f"/api/v1/notes/{note['id']}/conclusions/{conclusion['id']}"
    assert client.delete(url, params={"version": note["version"] + 1}).status_code == 409
    removed = client.delete(url, params={"version": note["version"]})
    assert removed.status_code == 200 and removed.json()["conclusions"] == []
    assert removed.json()["version"] == note["version"] + 1
    assert client.delete(url, params={"version": removed.json()["version"]}).status_code == 404
    history = client.get(f"/api/v1/notes/{note['id']}/revisions").json()
    assert history[0]["conclusions"] == [] and history[-1]["conclusions"][0]["id"] == conclusion["id"]


# ---- Manual links ----

def test_manual_links_are_symmetric_private_and_hide_trashed_notes(app, client, monkeypatch):
    register(client)
    a, b, c = (manual(client, name) for name in ("Первая", "Вторая", "Третья"))
    base = "/api/v1/notes/"
    assert client.get(base + a["id"] + "/links").json() == {"note_id": a["id"], "links": []}
    made = client.post(base + a["id"] + "/links", json={"note_id": b["id"]})
    assert made.status_code == 201
    assert [link["note_id"] for link in made.json()["links"]] == [b["id"]]
    assert made.json()["links"][0]["title"] == "Вторая"
    again = client.post(base + b["id"] + "/links", json={"note_id": a["id"]})  # The same pair from the other side.
    assert [link["note_id"] for link in again.json()["links"]] == [a["id"]]
    assert table_count(app, NoteLink) == 1
    assert [link["note_id"] for link in client.get(base + a["id"] + "/links").json()["links"]] == [b["id"]]
    assert client.post(base + a["id"] + "/links", json={"note_id": a["id"]}).status_code == 422
    assert client.post(base + a["id"] + "/links", json={"note_id": "missing"}).status_code == 404
    assert client.post(base + a["id"] + "/links", json={"note_id": c["id"]}, headers={"X-CSRF-Token": "x"}).status_code == 403
    graph = client.get("/api/v1/graph").json()
    assert graph["manual_edges"] == [{"source": min(a["id"], b["id"]), "target": max(a["id"], b["id"]),
                                      "score": 1.0, "manual": True}]

    other = TestClient(app)
    register(other, "second")
    stranger = manual(other, "Чужая")
    assert other.get(base + a["id"] + "/links").status_code == 404
    assert client.post(base + a["id"] + "/links", json={"note_id": stranger["id"]}).status_code == 404
    assert other.delete(base + a["id"] + "/links/" + b["id"]).status_code == 404

    version = client.get(base + b["id"]).json()["version"]
    assert client.delete(base + b["id"], params={"version": version}).status_code == 204
    assert client.get(base + a["id"] + "/links").json()["links"] == []
    assert client.post(base + a["id"] + "/links", json={"note_id": b["id"]}).status_code == 404
    assert client.get("/api/v1/graph").json()["manual_edges"] == []
    client.post(f"/api/v1/trash/{b['id']}/restore")
    assert len(client.get(base + a["id"] + "/links").json()["links"]) == 1

    assert client.delete(base + a["id"] + "/links/" + b["id"]).status_code == 204
    assert client.delete(base + a["id"] + "/links/" + b["id"]).status_code == 404
    assert table_count(app, NoteLink) == 0
    monkeypatch.setattr("app.notes_manage.MAX_LINKS", 1)
    assert client.post(base + a["id"] + "/links", json={"note_id": b["id"]}).status_code == 201
    assert client.post(base + a["id"] + "/links", json={"note_id": c["id"]}).status_code == 429


# ---- Merge ----

def prepared_notes(app, client):
    """Three notes. A and B get merged, C only shares a manual link with A."""
    register(client)
    home = client.post("/api/v1/categories", json={"name": "Дом"}).json()
    a = add_item(client, manual(client, "Купить молоко\nИдея про отпуск", key="a"), text="Купить молоко")
    a = add_item(client, a, text="Забрать посылку")
    b = add_item(client, manual(client, "Позвонить Оле", key="b"), text="купить молоко!")
    b = add_item(client, b, text="Позвонить Оле")
    done = add_item(client, b, text="Старое дело")
    old = next(i for i in done["items"] if i["text"] == "Старое дело")
    finished = client.patch(
        f"/api/v1/notes/{b['id']}/items/{old['id']}",
        json={"version": done["version"], "kind": "task", "text": "Старое дело", "status": "completed"},
    )
    assert finished.status_code == 200
    c = manual(client, "Третья", key="c")
    for note in (a, b):
        current = fresh(client, note)
        assert client.patch(
            f"/api/v1/notes/{note['id']}/category", json={"version": current["version"], "category_id": home["id"]},
        ).status_code == 200
    assert client.post(f"/api/v1/notes/{a['id']}/links", json={"note_id": c["id"]}).status_code == 201
    assert client.post(f"/api/v1/notes/{a['id']}/links", json={"note_id": b["id"]}).status_code == 201
    return fresh(client, a), fresh(client, b), c, home


def item_id(note, text):
    return next(i["id"] for i in note["items"] if i["text"] == text)


def test_merge_creates_one_note_and_moves_tasks_reminders_and_links(app, client):
    a, b, c, home = prepared_notes(app, client)
    for note, task_text, key in ((a, "Забрать посылку", "ra"), (b, "купить молоко!", "rb")):
        created = client.post(
            "/api/v1/notes/" + note["id"] + "/reminders", json=payload(item_id(note, task_text)),
            headers={"Idempotency-Key": key},
        )
        assert created.status_code == 201
    queued = merge(client, [a["id"], b["id"]])
    assert queued.status_code == 202, queued.text
    job = queued.json()
    assert job["kind"] == "merge" and job["status"] == "queued" and job["merge_note_ids"] == [a["id"], b["id"]]
    assert job["original_text"].startswith(MERGE_MARKER) and "Купить молоко" in job["original_text"]
    provider = MergeProvider()
    assert run_worker(app, provider) and provider.calls == 1

    done = client.get("/api/v1/jobs/" + job["id"]).json()
    assert done["status"] == "succeeded" and done["note_id"]
    new = client.get("/api/v1/notes/" + done["note_id"]).json()
    assert new["title"] == "Единый план" and new["category_id"] == home["id"]
    texts = [i["text"] for i in new["items"]]
    assert texts == ["Купить молоко", "Забрать посылку", "Позвонить Оле"]  # The model's first, then carried over.
    assert all(i["status"] == "open" and i["kind"] == "task" for i in new["items"])
    reminders = client.get(f"/api/v1/notes/{new['id']}/reminders").json()
    assert sorted(r["item_id"] for r in reminders) == sorted([item_id(new, "Купить молоко"), item_id(new, "Забрать посылку")])
    assert all(r["status"] == "confirmed" for r in reminders)
    assert [link["note_id"] for link in client.get(f"/api/v1/notes/{new['id']}/links").json()["links"]] == [c["id"]]
    assert client.get(f"/api/v1/notes/{c['id']}/links").json()["links"][0]["note_id"] == new["id"]

    capture = client.get("/api/v1/captures/" + done["capture_id"]).json()
    assert capture["original_text"].startswith(MERGE_MARKER) and capture["note_id"] == new["id"]
    for original in (a, b):
        assert client.get("/api/v1/notes/" + original["id"]).status_code == 404
    trash = {row["id"]: row for row in client.get("/api/v1/trash").json()["notes"]}
    assert set(trash) == {a["id"], b["id"]}
    assert all(row["merged_into"] == new["id"] and row["merged_into_title"] == "Единый план" for row in trash.values())
    assert [n["id"] for n in client.get("/api/v1/notes").json() if n["id"] != c["id"]] == [new["id"]]
    assert client.get("/api/v1/tasks").json()["counts"] == {"open": 3, "completed": 0}
    assert client.get("/api/v1/provider/usage").json()["daily_units_used"] == 1
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reminder).where(Reminder.note_id == new["id"])) == 2
        assert db.scalar(select(func.count()).select_from(Reminder).where(Reminder.note_id.in_([a["id"], b["id"]]))) == 0

    restored = client.post(f"/api/v1/trash/{a['id']}/restore")
    assert restored.status_code == 200 and restored.json()["title"] == a["title"]


def test_merge_takes_the_most_common_category_when_the_model_names_none(app, client):
    a, b, c, home = prepared_notes(app, client)
    other = client.post("/api/v1/categories", json={"name": "Работа"}).json()
    current = fresh(client, c)
    client.patch(f"/api/v1/notes/{c['id']}/category", json={"version": current["version"], "category_id": other["id"]})
    queued = merge(client, [a["id"], b["id"], c["id"]]).json()
    run_worker(app)
    new = client.get("/api/v1/notes/" + client.get("/api/v1/jobs/" + queued["id"]).json()["note_id"]).json()
    assert new["category_id"] == home["id"]


def test_failed_merge_changes_nothing(app, client):
    a, b, c, _ = prepared_notes(app, client)
    queued = merge(client, [a["id"], b["id"]]).json()
    run_worker(app, FailingProvider())
    job = client.get("/api/v1/jobs/" + queued["id"]).json()
    assert job["status"] == "failed" and job["error_code"] == "provider_bad_request" and job["note_id"] is None
    assert job["kind"] == "merge" and job["merge_note_ids"] == [a["id"], b["id"]]
    for note in (a, b):
        assert client.get("/api/v1/notes/" + note["id"]).json()["version"] == note["version"]
    assert client.get("/api/v1/trash").json()["notes"] == []
    assert len(client.get("/api/v1/notes/" + a["id"] + "/links").json()["links"]) == 2
    assert table_count(app, Note) == 3


def test_merge_stops_without_a_model_call_when_an_original_disappears(app, client):
    a, b, _, _ = prepared_notes(app, client)
    queued = merge(client, [a["id"], b["id"]]).json()
    assert client.delete("/api/v1/notes/" + b["id"], params={"version": b["version"]}).status_code == 204
    provider = MergeProvider()
    run_worker(app, provider)
    job = client.get("/api/v1/jobs/" + queued["id"]).json()
    assert provider.calls == 0 and job["status"] == "failed" and job["error_code"] == "merge_source_missing"
    assert client.get("/api/v1/notes/" + a["id"]).status_code == 200


def test_merge_checks_what_it_is_given(app, client):
    a, b, c, _ = prepared_notes(app, client)
    assert merge(client, [a["id"]]).status_code == 422
    assert merge(client, [a["id"], a["id"]]).status_code == 422
    assert merge(client, [new_id() for _ in range(6)]).status_code == 422
    assert merge(client, [a["id"], "no-such-note"]).status_code == 404
    assert client.post("/api/v1/notes/merge", json={"note_ids": [a["id"], b["id"]]}).status_code == 422
    assert client.post("/api/v1/notes/merge", json={"note_ids": [a["id"], b["id"]], "idempotency_key": "x y"}).status_code == 422
    body = {"note_ids": [a["id"], b["id"]], "idempotency_key": "k"}
    assert client.post("/api/v1/notes/merge", json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    other = TestClient(app)
    register(other, "second")
    stranger = manual(other, "Чужая")
    assert merge(client, [a["id"], stranger["id"]]).status_code == 404
    assert other.post("/api/v1/notes/merge", json={"note_ids": [a["id"], stranger["id"]], "idempotency_key": "k"}).status_code == 404
    client.delete("/api/v1/notes/" + c["id"], params={"version": c["version"]})
    assert merge(client, [a["id"], c["id"]]).status_code == 404  # A trashed note cannot be merged.
    assert table_count(app, Job) == 0


def test_merge_is_idempotent_and_refuses_a_reused_key(app, client):
    a, b, c, _ = prepared_notes(app, client)
    first = merge(client, [a["id"], b["id"]], "same-key")
    again = merge(client, [a["id"], b["id"]], "same-key")
    assert first.status_code == again.status_code == 202 and first.json()["id"] == again.json()["id"]
    assert merge(client, [a["id"], c["id"]], "same-key").status_code == 409
    assert table_count(app, Job) == 1 and table_count(app, Capture) == 4


def test_merge_costs_one_unit_and_stops_at_the_daily_limit(app_factory):
    app = app_factory(daily_unit_limit=1)
    with TestClient(app) as client:
        a, b, _, _ = prepared_notes(app, client)
        assert merge(client, [a["id"], b["id"]], "one").status_code == 202
        spent = merge(client, [a["id"], b["id"]], "two")
        assert spent.status_code == 429 and "Лимит ИИ" in spent.json()["detail"]
        assert table_count(app, Job) == 1


def test_merge_refuses_notes_that_are_too_long_together(app, client):
    register(client)
    first = manual(client, "а" * 7000, key="long-1")
    second = manual(client, "б" * 7000, key="long-2")
    response = merge(client, [first["id"], second["id"]])
    assert response.status_code == 422 and "слишком длинные" in response.json()["detail"]


def test_default_mock_provider_can_merge(app, client):
    a, b, _, _ = prepared_notes(app, client)
    queued = merge(client, [a["id"], b["id"]]).json()
    Worker(app.state.sessions, app.state.settings).run_once()
    job = client.get("/api/v1/jobs/" + queued["id"]).json()
    assert job["status"] == "succeeded"
    new = client.get("/api/v1/notes/" + job["note_id"]).json()
    assert MERGE_MARKER not in new["markdown"] and "===" not in new["markdown"]
    # With no model tasks, every open task of the originals is carried over once.
    assert sorted(i["text"] for i in new["items"]) == ["Забрать посылку", "Позвонить Оле", "Купить молоко"][:0] + sorted(
        ["Купить молоко", "Забрать посылку", "Позвонить Оле"])


def test_job_lists_show_the_kind(app, client):
    a, b, _, _ = prepared_notes(app, client)
    queued = merge(client, [a["id"], b["id"]]).json()
    assert client.get("/api/v1/jobs").json()[0]["kind"] == "merge"
    capture = client.get("/api/v1/captures/" + queued["capture_id"]).json()
    assert capture["job"]["kind"] == "merge" and capture["job"]["merge_note_ids"] == [a["id"], b["id"]]
    ordinary = client.post("/api/v1/captures/text", json={"text": "Просто мысль"}, headers={"Idempotency-Key": "p1"})
    assert ordinary.json()["kind"] == "capture" and ordinary.json()["merge_note_ids"] is None


# ---- Guides ----

def test_guide_state_is_kept_per_account(app, client):
    register(client)
    start = {"guides": {"note_tools": {"step": 0, "done": False}, "categories": {"step": 0, "done": False}}}
    assert client.get("/api/v1/account/guides").json() == start
    moved = client.patch("/api/v1/account/guides", json={"guide": "note_tools", "step": 2, "done": False})
    assert moved.status_code == 200
    assert moved.json()["guides"]["note_tools"] == {"step": 2, "done": False}
    finished = client.patch("/api/v1/account/guides", json={"guide": "categories", "step": 3, "done": True})
    assert finished.json()["guides"] == {
        "note_tools": {"step": 2, "done": False}, "categories": {"step": 3, "done": True},
    }
    assert client.get("/api/v1/account/guides").json() == finished.json()
    for bad in (
        {"guide": "other", "step": 0, "done": False}, {"guide": "note_tools", "step": -1, "done": False},
        {"guide": "note_tools", "step": 101, "done": False}, {"guide": "note_tools", "step": 0},
        {"guide": "note_tools", "step": 0, "done": False, "extra": 1},
    ):
        assert client.patch("/api/v1/account/guides", json=bad).status_code == 422
    ok = {"guide": "note_tools", "step": 0, "done": False}
    assert client.patch("/api/v1/account/guides", json=ok, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    other = TestClient(app)
    register(other, "second")
    assert other.get("/api/v1/account/guides").json() == start
    client.cookies.clear()
    assert client.get("/api/v1/account/guides").status_code == 401


# ---- Migration ----

def test_migration_round_trip_keeps_notes(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "manage.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "6a1e4c8d9b20")
    engine, _ = make_database(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users (id,username,password_hash,live_calls) VALUES ('u','legacy','hash',0)"))
        db.execute(text("INSERT INTO captures (id,user_id,original_text,idempotency_key,created_at) VALUES ('c','u','original','key',1)"))
        db.execute(text(
            "INSERT INTO notes (id,capture_id,user_id,title,markdown,conclusions,version,provider,created_at,updated_at) "
            "VALUES ('n','c','u','title','body','[]',1,'mock',1,1)"
        ))
        db.execute(text("INSERT INTO jobs (id,capture_id,user_id,status,provider,created_at) VALUES ('j','c','u','succeeded','mock',1)"))
    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as db:
        assert tuple(db.execute(text("SELECT deleted_at, merged_into FROM notes")).one()) == (None, None)
        assert db.execute(text("SELECT kind FROM jobs")).scalar() == "capture"
        assert db.execute(text("SELECT guide_state FROM users")).scalar() is None
        assert db.execute(text("SELECT COUNT(*) FROM note_links")).scalar() == 0
    command.downgrade(config, "6a1e4c8d9b20")
    with engine.connect() as db:
        assert db.execute(text("SELECT title, markdown FROM notes")).one() == ("title", "body")
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        columns = {row[1] for row in db.execute(text("PRAGMA table_info(notes)"))}
        assert "deleted_at" not in columns and "merged_into" not in columns
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()


def test_settings_replace_keeps_working():
    assert replace(Settings(), trash_days=7).trash_days == 7
    assert Category.__tablename__ == "categories"
