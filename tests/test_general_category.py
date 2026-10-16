"""Категория «Общее»: есть у каждого, не удаляется и не переименовывается, принимает всё без категории."""

import pytest
from sqlalchemy import select

from app.models import Category, Item, Note, User
from tests.conftest import register
from tests.test_ai_categories import Proposes, process
from tests.test_notes import ready_note, submit


def general_of(client):
    rows = client.get("/api/v1/categories").json()
    general = [row for row in rows if row["is_general"]]
    assert len(general) == 1 and rows[0] == general[0]  # Exactly one, listed first
    return general[0]


def test_every_account_has_the_general_category_listed_first(client):
    register(client)
    assert client.get("/api/v1/categories").json() == [
        {"id": general_of(client)["id"], "name": "Общее", "version": 1, "is_general": True}
    ]
    client.post("/api/v1/categories", json={"name": "Аааа"})
    client.post("/api/v1/categories", json={"name": "Яяяя"})
    names = [row["name"] for row in client.get("/api/v1/categories").json()]
    assert names == ["Общее", "Аааа", "Яяяя"]


def test_accounts_made_in_any_way_get_it(app):
    with app.state.sessions.begin() as db:
        db.add(User(username="direct", password_hash="x"))
    with app.state.sessions() as db:
        user = db.scalar(select(User).where(User.username == "direct"))
        (category,) = db.scalars(select(Category).where(Category.user_id == user.id)).all()
        assert category.name == "Общее" and category.is_general is True


def test_it_cannot_be_renamed_or_deleted(client):
    register(client)
    general = general_of(client)
    renamed = client.patch("/api/v1/categories/" + general["id"], json={"name": "Другое", "version": 1})
    assert renamed.status_code == 409 and "Общее" in renamed.json()["detail"]
    deleted = client.delete("/api/v1/categories/" + general["id"], params={"version": 1})
    assert deleted.status_code == 409 and "Общее" in deleted.json()["detail"]
    assert general_of(client) == general


def test_no_second_general_by_a_look_alike_name(client):
    register(client)
    general = general_of(client)
    again = client.post("/api/v1/categories", json={"name": "общее"})
    assert again.status_code == 201 and again.json()["id"] == general["id"]
    other = client.post("/api/v1/categories", json={"name": "Другая"}).json()
    clash = client.patch("/api/v1/categories/" + other["id"], json={"name": "ОБЩЕЕ", "version": 1})
    assert clash.status_code == 409
    assert len(client.get("/api/v1/categories").json()) == 2


def test_a_manual_and_an_ai_note_without_a_category_go_to_general(app, client):
    register(client)
    general = general_of(client)
    note = process(app, client, Proposes(None), key="k-first")
    assert note["category_id"] == general["id"] and note["category_name"] == "Общее"
    no_category = process(app, client, Proposes(None), key="k-none")
    assert no_category["category_id"] == general["id"]
    for index, bad in enumerate(["12345", "«Работа»", "https://example.com"]):
        assert process(app, client, Proposes(bad), key=f"k-bad-{index}")["category_id"] == general["id"]
    assert client.post(
        "/api/v1/captures/text", json={"text": "заметка без ИИ", "processing_mode": "manual"},
        headers={"Idempotency-Key": "manual-1"},
    ).status_code in (200, 201, 202)
    with app.state.sessions() as db:
        assert db.scalars(select(Note).where(Note.category_id.is_(None))).all() == []


def test_a_category_the_model_names_is_still_used(app, client):
    register(client)
    note = process(app, client, Proposes("Финансы"))
    assert note["category_name"] == "Финансы"
    chosen = process(app, client, Proposes("общее"), key="k-general")
    assert chosen["category_name"] == "Общее"


def test_removing_the_category_of_a_note_puts_it_in_general(app, client):
    register(client)
    category = client.post("/api/v1/categories", json={"name": "Работа"}).json()
    note = ready_note(app, client)
    moved = client.patch(
        "/api/v1/notes/" + note["id"] + "/category", json={"category_id": category["id"], "version": note["version"]},
    ).json()
    assert moved["category_name"] == "Работа"
    cleared = client.patch(
        "/api/v1/notes/" + note["id"] + "/category", json={"category_id": None, "version": moved["version"]},
    ).json()
    assert cleared["category_id"] == general_of(client)["id"]


def test_deleting_a_category_moves_its_notes_and_items_to_general(app, client):
    register(client)
    category = client.post("/api/v1/categories", json={"name": "Работа"}).json()
    note = ready_note(app, client)
    client.patch("/api/v1/notes/" + note["id"] + "/category", json={"category_id": category["id"], "version": note["version"]})
    with app.state.sessions.begin() as db:
        for item in db.scalars(select(Item).where(Item.note_id == note["id"])):
            item.category_id = category["id"]
    assert client.delete("/api/v1/categories/" + category["id"], params={"version": 1}).status_code == 204
    general = general_of(client)
    shown = client.get("/api/v1/notes/" + note["id"]).json()
    assert shown["category_id"] == general["id"]
    with app.state.sessions() as db:
        assert {item.category_id for item in db.scalars(select(Item).where(Item.note_id == note["id"]))} <= {general["id"]}
        assert db.scalar(select(Note.category_id).where(Note.id == note["id"])) == general["id"]


def test_the_old_filter_without_category_is_an_alias_of_general(app, client):
    register(client)
    category = client.post("/api/v1/categories", json={"name": "Работа"}).json()
    first = process(app, client, Proposes(None), key="k-1")
    second = process(app, client, Proposes(None), key="k-2")
    client.patch(
        "/api/v1/notes/" + second["id"] + "/category", json={"category_id": category["id"], "version": second["version"]},
    )
    general = general_of(client)
    by_alias = {n["id"] for n in client.get("/api/v1/notes", params={"category_id": "none"}).json()}
    by_id = {n["id"] for n in client.get("/api/v1/notes", params={"category_id": general["id"]}).json()}
    assert by_alias == by_id and first["id"] in by_alias and second["id"] not in by_alias


def test_other_accounts_have_their_own_general(app, client):
    register(client, "one")
    mine = general_of(client)
    from fastapi.testclient import TestClient

    stranger = TestClient(app)
    register(stranger, "two")
    theirs = general_of(stranger)
    assert theirs["id"] != mine["id"]
    assert stranger.patch("/api/v1/categories/" + mine["id"], json={"name": "x", "version": 1}).status_code == 404


@pytest.mark.parametrize("path", ["/api/v1/captures/text"])
def test_submitting_still_works(client, path):
    register(client)
    assert submit(client).status_code == 202


# ---- Ответ бота: «Категория: ...» ----


def claim_all(client):
    """The fixture's own manual note left a message in the queue: take it out of the way."""
    from tests.test_reminders import auth, claim, result

    for item in claim(client).json()["items"]:
        auth(client, item)
        result(client, item)


def telegram_reply(app, client, text, provider, update_id):
    from app.worker import Worker
    from tests.test_internal import message
    from tests.test_reminders import claim

    assert message(client, update_id=update_id, text=text).status_code == 200
    assert Worker(app.state.sessions, app.state.settings, provider).run_once()
    (item,) = claim(client).json()["items"]
    return item["text"]


def test_the_bot_reply_names_the_category_at_the_end(reminders):
    app, client, ids = reminders
    claim_all(client)
    named = telegram_reply(app, client, "Заметка про отчёт", Proposes("Работа"), 301)
    assert named.endswith("\n\nКатегория: Работа") and named.count("Категория:") == 1
    general = telegram_reply(app, client, "Просто мысль", Proposes(None), 302)
    assert general.endswith("\n\nКатегория: Общее")


def test_a_note_saved_without_ai_names_general_in_the_core_answer(reminders):
    from tests.test_internal import message

    app, client, ids = reminders
    saved = message(client, update_id=303, text="Просто запись", mode="manual").json()
    assert saved["category_name"] == "Общее"
    ai = message(client, update_id=304, text="Ещё запись", mode="ai").json()
    assert ai["category_name"] is None  # The category is known only after the model has read the note


from tests.test_reminders import reminders as reminders  # noqa: E402
