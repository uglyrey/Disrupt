"""Task statistics keep history: deleting a note never lowers «выполнено», reopening a task does."""

import time

from app.analytics import record_event
from app.models import Item, ProductEvent
from tests.conftest import register
from tests.test_second_brain import manual


def new_task(client, text="Позвонить"):
    note = manual(client, "Дела на день " + text)
    added = client.post(f"/api/v1/notes/{note['id']}/items", json={"version": note["version"], "kind": "task", "text": text}).json()
    return note, added, added["items"][0]


def toggle(client, note, current, item, status):
    reply = client.patch(
        f"/api/v1/notes/{note['id']}/items/{item['id']}",
        json={"version": current["version"], "kind": "task", "text": item["text"], "status": status},
    )
    assert reply.status_code == 200, reply.text
    return reply.json()


def tasks(client, days=7):
    return client.get("/api/v1/dashboard", params={"days": days}).json()["tasks"]


def test_reopen_lowers_and_second_completion_counts_once(client):
    register(client)
    note, added, item = new_task(client)
    assert tasks(client) == {"open": 1, "completed": 0, "completed_in_period": 0, "completion_rate": 0.0}
    current = toggle(client, note, added, item, "completed")
    assert tasks(client)["completed"] == 1 and tasks(client)["completed_in_period"] == 1 and tasks(client)["open"] == 0
    current = toggle(client, note, current, item, "open")
    assert tasks(client) == {"open": 1, "completed": 0, "completed_in_period": 0, "completion_rate": 0.0}
    current = toggle(client, note, current, item, "completed")
    assert tasks(client)["completed"] == 1 and tasks(client)["completed_in_period"] == 1
    # The same event replayed (a retry of the same version) is not counted twice.
    toggle(client, note, current, item, "open")
    assert tasks(client)["completed"] == 0


def test_deleting_the_note_keeps_completed_history_and_drops_open(app, client):
    register(client)
    note, added, item = new_task(client, "Сделанная")
    done = toggle(client, note, added, item, "completed")
    other = manual(client, "Вторая заметка")
    client.post(f"/api/v1/notes/{other['id']}/items", json={"version": other["version"], "kind": "task", "text": "Открытая"})
    assert tasks(client)["completed"] == 1 and tasks(client)["open"] == 1
    assert client.delete(f"/api/v1/notes/{note['id']}", params={"version": done["version"]}).status_code == 204
    assert tasks(client)["completed"] == 1 and tasks(client)["completed_in_period"] == 1
    # Forever: the item rows are gone, the history stays.
    assert client.delete(f"/api/v1/trash/{note['id']}").status_code == 204
    with app.state.sessions() as db:
        assert db.get(Item, item["id"]) is None
    assert tasks(client)["completed"] == 1 and tasks(client)["completed_in_period"] == 1
    after_other = client.get(f"/api/v1/notes/{other['id']}").json()
    assert client.delete(f"/api/v1/notes/{other['id']}", params={"version": after_other["version"]}).status_code == 204
    data = tasks(client)
    assert data["open"] == 0 and data["completed"] == 1 and data["completion_rate"] == 1.0


def test_restoring_a_note_does_not_count_twice(client):
    register(client)
    note, added, item = new_task(client)
    done = toggle(client, note, added, item, "completed")
    assert client.delete(f"/api/v1/notes/{note['id']}", params={"version": done["version"]}).status_code == 204
    assert client.post(f"/api/v1/trash/{note['id']}/restore").status_code == 200
    assert tasks(client)["completed"] == 1


def test_period_boundary_uses_the_latest_completion(app, client):
    owner = register(client)["id"]
    note, added, item = new_task(client)
    done = toggle(client, note, added, item, "completed")
    day = 86400
    with app.state.sessions.begin() as db:
        # Move the completion 10 days back: the week no longer has it, the month still does.
        event = db.query(ProductEvent).filter_by(user_id=owner, name="task_completed").one()
        event.occurred_at = time.time() - 10 * day
    assert tasks(client, 7)["completed"] == 1 and tasks(client, 7)["completed_in_period"] == 0
    assert tasks(client, 30)["completed_in_period"] == 1
    # Reopen and complete again today: the latest completion is in the week.
    again = toggle(client, note, toggle(client, note, done, item, "open"), item, "completed")
    assert again["version"] > done["version"]
    assert tasks(client, 7)["completed"] == 1 and tasks(client, 7)["completed_in_period"] == 1


def test_completed_before_events_still_count(app, client):
    owner = register(client)["id"]
    note = manual(client, "Старая заметка")
    with app.state.sessions.begin() as db:
        db.add(Item(user_id=owner, note_id=note["id"], kind="task", status="completed", text="Давно сделано"))
        db.add(Item(user_id=owner, note_id=note["id"], kind="task", status="open", text="Ещё нет"))
    data = tasks(client)
    assert data["completed"] == 1 and data["open"] == 1 and data["completed_in_period"] == 0


def test_reopen_event_is_idempotent_per_version(app, client):
    owner = register(client)["id"]
    with app.state.sessions.begin() as db:
        for _ in range(2):
            record_event(db, owner, "task_reopened", "item-1:3")
        assert db.query(ProductEvent).filter_by(user_id=owner, name="task_reopened").count() == 1
