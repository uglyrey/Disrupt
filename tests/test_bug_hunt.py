"""Regressions found in the bug hunt of 11 October 2026. See docs/bug-hunt-2026-10-11.md."""

from sqlalchemy import event

from tests.conftest import register
from tests.test_notes import submit
from tests.test_structure import manual

HUGE = 10**30


def test_a_huge_version_is_refused_not_a_server_error(client):
    register(client)
    note = manual(client)
    path = f"/api/v1/notes/{note['id']}"
    edit = {"title": "Новое", "markdown": "Текст", "version": HUGE}
    assert client.patch(path, json=edit).status_code == 422
    assert client.patch(path, json={**edit, "version": 2**31}).status_code == 422
    assert client.post(path + "/confirm-structure", json={"version": HUGE}).status_code == 422
    assert client.patch(path + "/category", json={"category_id": None, "version": HUGE}).status_code == 422
    assert client.post(path + "/items", json={"version": HUGE, "kind": "task", "text": "Дело"}).status_code == 422
    # The largest value the column holds is still an ordinary conflict.
    assert client.patch(path, json={**edit, "version": 2**31 - 1}).status_code == 409
    # Nothing was changed by the refused requests.
    assert client.get(path).json()["version"] == note["version"]


def test_a_huge_generation_or_category_version_is_refused(client):
    register(client)
    category = client.post("/api/v1/categories", json={"name": "Работа"}).json()
    assert client.patch(f"/api/v1/categories/{category['id']}", json={"name": "Дом", "version": HUGE}).status_code == 422
    cancel = client.post("/api/v1/reminders/unknown/cancel", json={"generation": HUGE})
    assert cancel.status_code == 422, cancel.text
    change = {"scheduled_at": "2030-01-01T10:00:00+03:00", "timezone": "Europe/Moscow", "text": "Позвонить", "generation": HUGE}
    assert client.patch("/api/v1/reminders/unknown", json=change).status_code == 422
    # With a normal number the same request reaches the lookup and answers 404.
    assert client.patch("/api/v1/reminders/unknown", json={**change, "generation": 1}).status_code == 404


def test_a_huge_offset_gives_an_empty_page(client):
    register(client)
    note = manual(client)
    for offset in (10**12, 2**63, 10**25):
        listed = client.get("/api/v1/notes", params={"offset": offset})
        assert listed.status_code == 200, listed.text
        assert listed.json() == [] and "x-next-notes-offset" not in listed.headers
        searched = client.get("/api/v1/notes", params={"offset": offset, "q": "запись"})
        assert searched.status_code == 200 and searched.json() == []
        reminders = client.get(f"/api/v1/notes/{note['id']}/reminders", params={"offset": offset})
        assert reminders.status_code == 200 and reminders.json() == []
    assert [row["id"] for row in client.get("/api/v1/notes").json()] == [note["id"]]


def test_the_jobs_list_does_not_query_per_job(app, client):
    register(client)
    counted = []

    def count(*args, **kwargs):
        counted.append(1)

    def jobs_queries():
        counted.clear()
        event.listen(app.state.engine, "before_cursor_execute", count)
        try:
            response = client.get("/api/v1/jobs")
        finally:
            event.remove(app.state.engine, "before_cursor_execute", count)
        assert response.status_code == 200, response.text
        return len(counted), response.json()

    assert submit(client, key="job-0").status_code == 202
    one, rows = jobs_queries()
    assert len(rows) == 1 and rows[0]["original_text"] and rows[0]["note_id"] is None
    for number in range(1, 8):
        assert submit(client, text=f"Запись {number}", key=f"job-{number}").status_code == 202
    many, rows = jobs_queries()
    assert len(rows) == 8
    assert many == one, "a longer list must not cost more queries"
    assert {row["original_text"] for row in rows} >= {"Запись 1", "Запись 7"}


def test_a_nul_character_in_the_address_is_answered_before_any_query(app, client):
    """PostgreSQL refuses NUL inside a string, so such ids must never reach a query (a 500 there)."""
    register(client)
    counted = []

    def count(*args, **kwargs):
        counted.append(1)

    event.listen(app.state.engine, "before_cursor_execute", count)
    try:
        for path in ("/api/v1/jobs/%00", "/api/v1/captures/a%00b", "/api/v1/captures/%00/audio",
                     "/api/v1/assistant/requests/%00", "/api/v1/telegram/links/%00/confirm"):
            response = client.get(path) if "confirm" not in path else client.post(path)
            assert response.status_code == 404, (path, response.text)
            assert response.headers["x-content-type-options"] == "nosniff"
    finally:
        event.remove(app.state.engine, "before_cursor_execute", count)
    assert counted == []
