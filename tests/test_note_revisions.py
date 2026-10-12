"""История заметки. Автосохранения одной сессии правок живут в одной версии, версия заметки растёт как раньше."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import services
from app.config import Settings
from app.models import Revision
from app.revisions_cli import compact
from tests.conftest import register
from tests.test_notes import ready_note

PASSWORD = "test-only-password-123"


class Clock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(services, "_now", clock)
    return clock


def setup(app, client):
    register(client)
    note = ready_note(app, client)
    return note, "/api/v1/notes/" + note["id"]


def save(client, path, text, *, editor="tab-a", checkpoint=False, title="Заголовок"):
    version = client.get(path).json()["version"]
    body = {"version": version, "title": title, "markdown": text, "editor": editor}
    if checkpoint:
        body["checkpoint"] = True
    return client.patch(path, json=body)


def history(client, path):
    return client.get(path + "/revisions").json()


def test_autosave_every_three_seconds_makes_one_revision(app, client, clock):
    note, path = setup(app, client)
    for step in range(100):  # Five minutes of typing.
        assert save(client, path, f"Текст {step}").status_code == 200
        clock.advance(3)
    rows = history(client, path)
    assert [r["kind"] for r in rows] == ["edit", "original"]
    assert rows[0]["markdown"] == "Текст 99"
    assert rows[0]["version"] == client.get(path).json()["version"] == 101  # Optimistic concurrency counts every save.
    assert rows[1]["version"] == 1 and rows[1]["markdown"] == note["markdown"]
    assert rows[0]["started_at"] < rows[0]["created_at"]
    assert rows[0]["changed_chars"] > 0


def test_pause_longer_than_window_starts_new_revision(app, client, clock):
    note, path = setup(app, client)
    save(client, path, "Первая")
    clock.advance(599)
    save(client, path, "Первая и ещё")  # Less than ten minutes: same session.
    assert len(history(client, path)) == 2
    clock.advance(601)  # The window counts from the last save.
    save(client, path, "После паузы")
    rows = history(client, path)
    assert [r["markdown"] for r in rows] == ["После паузы", "Первая и ещё", note["markdown"]]
    assert [r["kind"] for r in rows] == ["edit", "edit", "original"]


def test_session_never_longer_than_sixty_minutes(app, client, clock):
    _, path = setup(app, client)
    for step in range(15):  # Every five minutes for seventy minutes: the cap cuts it once.
        save(client, path, f"Правка {step}")
        clock.advance(300)
    rows = [r for r in history(client, path) if r["kind"] == "edit"]
    assert len(rows) == 2
    assert rows[1]["markdown"] == "Правка 11"  # Sixty minutes are twelve saves, the thirteenth starts a new one.
    assert rows[0]["markdown"] == "Правка 14"


def test_checkpoint_with_new_text_closes_session(app, client, clock):
    _, path = setup(app, client)
    save(client, path, "Одно")
    clock.advance(5)
    assert save(client, path, "Одно два", checkpoint=True).status_code == 200
    clock.advance(5)
    save(client, path, "Одно два три")
    rows = history(client, path)
    assert [r["markdown"] for r in rows[:2]] == ["Одно два три", "Одно два"]
    assert len(rows) == 3


def test_checkpoint_with_same_text_adds_no_version(app, client, clock):
    _, path = setup(app, client)
    save(client, path, "Текст")
    before = client.get(path).json()["version"]
    clock.advance(5)
    closed = save(client, path, "Текст", checkpoint=True)
    assert closed.status_code == 200
    assert closed.json()["version"] == before
    assert len(history(client, path)) == 2
    clock.advance(5)
    save(client, path, "Текст дальше")
    assert len(history(client, path)) == 3
    # A checkpoint with a stale version is still a conflict.
    stale = client.patch(path, json={"version": 1, "title": "Заголовок", "markdown": "Текст дальше", "checkpoint": True})
    assert stale.status_code == 409


def test_second_tab_after_conflict_keeps_both_sides(app, client, clock):
    _, path = setup(app, client)
    save(client, path, "Работа вкладки A", editor="tab-a")
    stale = client.patch(
        path, json={"version": 1, "title": "Заголовок", "markdown": "Работа вкладки B", "editor": "tab-b"}
    )
    assert stale.status_code == 409
    clock.advance(10)
    save(client, path, "Работа вкладки B", editor="tab-b")  # After reloading, within the window.
    clock.advance(10)
    save(client, path, "Вкладка A продолжает", editor="tab-a")
    rows = history(client, path)
    assert [r["markdown"] for r in rows[:3]] == ["Вкладка A продолжает", "Работа вкладки B", "Работа вкладки A"]
    assert client.get(path).json()["markdown"] == "Вкладка A продолжает"


def test_other_editor_without_conflict_is_not_merged(app, client, clock):
    _, path = setup(app, client)
    save(client, path, "A", editor="tab-a")
    clock.advance(1)
    save(client, path, "B", editor="tab-b")
    assert [r["markdown"] for r in history(client, path)[:2]] == ["B", "A"]


def test_other_browser_session_is_not_merged(app, client, clock):
    _, path = setup(app, client)
    save(client, path, "С компьютера")
    with TestClient(app) as phone:
        login = phone.post("/api/v1/auth/login", json={"username": "tester", "password": PASSWORD})
        assert login.status_code == 200, login.text
        phone.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        clock.advance(1)
        assert save(phone, path, "С телефона").status_code == 200
    assert [r["markdown"] for r in history(client, path)[:2]] == ["С телефона", "С компьютера"]


def test_ai_original_and_other_changes_never_merge(app, client, clock):
    note, path = setup(app, client)
    assert note["markdown"] != "Текст"
    save(client, path, "Текст")
    conclusion = note["conclusions"][0]["id"]
    clock.advance(1)
    version = client.get(path).json()["version"]
    assert client.patch(path + "/conclusions/" + conclusion, json={"version": version, "status": "accepted"}).status_code == 200
    clock.advance(1)
    save(client, path, "Текст ещё")  # The conclusion stands between, so this is a new revision.
    clock.advance(1)
    version = client.get(path).json()["version"]
    assert client.post(path + "/confirm-structure", json={"version": version}).status_code == 200
    clock.advance(1)
    save(client, path, "Текст и ещё")
    rows = history(client, path)
    assert [r["kind"] for r in rows] == ["edit", "change", "edit", "conclusion", "edit", "original"]
    assert rows[-1]["markdown"] == note["markdown"]
    assert rows[-1]["conclusions"][0]["status"] == "proposed"
    assert rows[-1]["version"] == 1


def test_item_and_category_changes_are_separate_revisions(app, client, clock):
    note, path = setup(app, client)
    save(client, path, "Текст")
    version = client.get(path).json()["version"]
    clock.advance(1)
    added = client.post(path + "/items", json={"version": version, "kind": "task", "text": "Новая задача"})
    assert added.status_code in (200, 201), added.text
    clock.advance(1)
    save(client, path, "Текст дальше")
    kinds = [r["kind"] for r in history(client, path)]
    assert kinds == ["edit", "change", "edit", "original"]


def test_retention_keeps_newest_and_the_original(app_factory, clock):
    app = app_factory(revision_keep=5)
    with TestClient(app) as client:
        note, path = setup(app, client)
        for step in range(12):
            assert save(client, path, f"Правка {step}", checkpoint=True).status_code == 200
            clock.advance(1)
        rows = history(client, path)
        assert len(rows) == 5
        assert rows[-1]["version"] == 1 and rows[-1]["markdown"] == note["markdown"]
        assert rows[0]["markdown"] == "Правка 11"
        assert [r["markdown"] for r in rows[1:4]] == ["Правка 10", "Правка 9", "Правка 8"]


def test_zero_window_turns_coalescing_off(app_factory, clock):
    app = app_factory(revision_coalesce_seconds=0)
    with TestClient(app) as client:
        _, path = setup(app, client)
        for step in range(3):
            save(client, path, f"Правка {step}")
        assert len(history(client, path)) == 4


def test_changed_chars_counts_only_the_changed_middle():
    assert services.changed_chars("abc", "abc") == 0
    assert services.changed_chars("Привет мир", "Привет, мир") == 1
    assert services.changed_chars("abc", "") == 3
    assert services.changed_chars("", "abcd") == 4


def test_history_response_has_no_internal_keys(app, client, clock):
    _, path = setup(app, client)
    save(client, path, "Текст")
    row = history(client, path)[0]
    assert not [key for key in row if key.startswith("_")]
    assert {"version", "created_at", "started_at", "kind", "changed_chars", "title", "markdown", "items"} <= set(row)


def test_settings_reject_silly_values():
    with pytest.raises(ValueError):
        Settings(revision_keep=3)
    with pytest.raises(ValueError):
        Settings(revision_session_max_seconds=10)
    assert replace(Settings(), revision_keep=0).revision_keep == 0


def test_cli_compacts_only_old_autosave_runs(app, client):
    note, path = setup(app, client)
    base = 1_700_000_000.0
    with app.state.sessions() as db:
        original = db.scalar(select(Revision).where(Revision.note_id == note["id"], Revision.version == 1))
        snapshot = {k: v for k, v in original.snapshot.items() if not k.startswith("_")}
        original.created_at = base
        original.snapshot = {**original.snapshot, "_kind": "change"}
        # Old rows carry no kind. Six quick autosaves, a pause, one more autosave and then a conclusion change.
        times = [10, 13, 16, 19, 22, 25, 2000]
        for index, offset in enumerate(times, start=2):
            db.add(Revision(
                note_id=note["id"], version=index, created_at=base + offset,
                snapshot={**snapshot, "markdown": f"Текст {index}", "structure_confirmed_at": base + offset},
            ))
        db.add(Revision(
            note_id=note["id"], version=9, created_at=base + 2001,
            snapshot={**snapshot, "markdown": "Текст 8", "conclusions": []},
        ))
        db.commit()
        settings = app.state.settings
        notes, removed = compact(db, settings)
        assert (notes, removed) == (1, 5)  # Dry run.
        assert db.scalar(select(Revision.id).where(Revision.note_id == note["id"], Revision.version == 2)) is not None
        compact(db, settings, apply=True)
        versions = [v for (v,) in db.execute(
            select(Revision.version).where(Revision.note_id == note["id"]).order_by(Revision.version)
        )]
        assert versions == [1, 7, 8, 9]
