"""Продолжение заметки: поиск, вопрос, добавление, отмена, напоминания и кнопки Telegram (docs/attach-to-note-v1.md)."""

import time

import pytest
from sqlalchemy import func, select

from app import attach
from app.models import Capture, Item, Note, Outbox, ProductEvent, Reminder
from app.providers import MockProvider
from app.schemas import ContinuesNote, StructuredNote
from app.worker import Worker
from tests.conftest import register
from tests.test_internal import message
from tests.test_reminders import auth, claim, result
from tests.test_reminders import reminders as reminders  # noqa: F401

TARGET = "Курсовая\nГлавы один и два готовы, надо писать введение и оформлять список литературы"
REPAIR = "Ремонт\nВыбрать обои для комнаты и заказать плитку в ванную"
_keys = iter(range(10**6))


def manual(client, text):
    r = client.post("/api/v1/captures/text", json={"text": text, "processing_mode": "manual"},
                    headers={"Idempotency-Key": f"m{next(_keys)}"})
    assert r.status_code == 202, r.text
    return r.json()["note_id"]


def ai(app, client, text, provider=None):
    r = client.post("/api/v1/captures/text", json={"text": text, "processing_mode": "ai"},
                    headers={"Idempotency-Key": f"a{next(_keys)}"})
    assert r.status_code == 202, r.text
    assert Worker(app.state.sessions, app.state.settings, provider=provider or MockProvider()).run_once()
    return client.get("/api/v1/jobs/" + r.json()["id"]).json()


def note_of(client, job):
    return client.get("/api/v1/notes/" + job["note_id"]).json()


def trash_all_but(app, keep):
    with app.state.sessions.begin() as db:
        for note in db.scalars(select(Note).where(Note.id != keep)):
            note.deleted_at = time.time()


def titles(client):
    return [n["title"] for n in client.get("/api/v1/notes").json()]


@pytest.fixture
def web(app, client):
    register(client)
    return app, client


def test_hints_are_found_deterministically():
    assert attach.hint_of("Добавь к заметке про курсовую: глава 3") == ("курсовую", True)
    assert attach.hint_of("допиши в заметку «Ремонт квартиры», купить краску") == ("Ремонт квартиры", True)
    assert attach.hint_of("Ещё по курсовой: добавить главу 3") == ("курсовой", False)
    assert attach.hint_of("К заметке про ремонт: купить краску") == ("ремонт", False)
    assert attach.hint_of("Купить молоко и хлеб, потом позвонить маме") == (None, False)


def test_explicit_request_adds_at_once_and_keeps_the_original(web):
    app, client = web
    target = manual(client, TARGET)
    job = ai(app, client, "Добавь к заметке про курсовую: глава три готова, осталось оформление")
    assert job["status"] == "succeeded" and job["attached"] is True and job["note_id"] == target
    note = client.get("/api/v1/notes/" + target).json()
    assert note["version"] == 2 and "глава три готова" in note["markdown"].lower() or "Глава три" in note["markdown"]
    assert note["markdown"].startswith(TARGET) and "\n\n## " in note["markdown"]
    [source] = note["attached"]
    assert source["capture_id"] == job["capture_id"] and source["can_undo"] is True
    assert "осталось оформление" in source["original_text"]
    assert titles(client) == ["Курсовая"]  # The record has no note of its own in the list.
    assert client.get("/api/v1/trash").json()["notes"] == []
    assert client.get("/api/v1/captures/" + job["capture_id"]).json()["note_id"] == target
    with app.state.sessions() as db:  # A capture still counts, a note of its own does not exist.
        assert db.scalar(select(func.count()).select_from(Capture)) == 2
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "capture_attached")) == 1


def test_implicit_continuation_asks_then_adds_and_undo_makes_a_separate_note(web):
    app, client = web
    target = manual(client, TARGET)
    job = ai(app, client, "Ещё по курсовой: добавить главу 3 и список источников")
    own = note_of(client, job)
    assert own["id"] != target and own["attach_offer"]["note_id"] == target
    assert own["attach_offer"]["title"] == "Курсовая"
    assert len(titles(client)) == 2  # Nothing is lost while nobody answers.
    url = f"/api/v1/captures/{job['capture_id']}/attach"
    first = client.post(url, json={"note_id": target})
    assert first.status_code == 200 and first.json()["state"] == "attached" and first.json()["note_id"] == target
    assert client.post(url, json={"note_id": target}).json() == first.json()  # Idempotent.
    assert titles(client) == ["Курсовая"]
    assert "список источников" in client.get("/api/v1/notes/" + target).json()["markdown"]
    undone = client.post(f"/api/v1/captures/{job['capture_id']}/detach")
    assert undone.status_code == 200 and undone.json()["state"] == "separate" and undone.json()["note_id"] == own["id"]
    assert client.post(f"/api/v1/captures/{job['capture_id']}/detach").json() == undone.json()
    back = client.get("/api/v1/notes/" + target).json()
    assert back["markdown"].strip() == TARGET and back["attached"] == [] and back["version"] == 3
    assert sorted(titles(client)) == sorted([own["title"], "Курсовая"])
    assert client.get("/api/v1/notes/" + own["id"]).json()["attach_offer"] is None


def test_separate_choice_keeps_the_note_and_is_idempotent(web):
    app, client = web
    manual(client, TARGET)
    job = ai(app, client, "Ещё по курсовой: добавить главу 3 и список источников")
    for _ in range(2):
        r = client.post(f"/api/v1/captures/{job['capture_id']}/separate")
        assert r.status_code == 200 and r.json()["state"] == "separate" and r.json()["note_id"] == job["note_id"]
    assert note_of(client, job)["attach_offer"] is None and len(titles(client)) == 2


def test_no_candidate_short_record_and_trash_make_a_plain_note(web):
    app, client = web
    first = ai(app, client, "Ещё по курсовой: добавить главу 3 и список источников")
    assert note_of(client, first)["attach_offer"] is None  # No such note yet.
    with app.state.sessions.begin() as db:
        db.get(Note, first["note_id"]).deleted_at = time.time()
    target = manual(client, TARGET)
    assert note_of(client, ai(app, client, "Ещё по курсовой: да"))["attach_offer"] is None  # Too short.
    assert client.delete(f"/api/v1/notes/{target}?version=1").status_code == 204
    trash_all_but(app, target)
    job = ai(app, client, "Добавь к заметке про курсовую: глава три готова, осталось оформление")
    assert job["attached"] is False and note_of(client, job)["attach_offer"] is None


def test_ambiguous_explicit_request_asks_instead_of_adding(web):
    app, client = web
    manual(client, TARGET)
    manual(client, "Курсовая вторая\nДругая курсовая по экономике, нужно собрать данные по отрасли")
    job = ai(app, client, "Добавь к заметке про курсовую: глава три готова, осталось оформление")
    assert job["attached"] is False


class Hesitant(MockProvider):
    def __init__(self, ref, confidence):
        self.ref, self.confidence = ref, confidence

    def structure(self, text, *, categories=(), timezone=None, profile=None, candidates=None):
        note = self.structure_plain(text, categories=categories)
        link = {"ref": self.ref, "hint": "x", "confidence": self.confidence} if candidates else None
        return note.model_copy(update={"continues_note": link and ContinuesNote(**link)})


def test_model_hint_needs_confidence_and_a_real_candidate(web):
    app, client = web
    target = manual(client, TARGET)
    text = "Продолжаю про курсовую, дописал введение и оформил список литературы"
    low = ai(app, client, text, Hesitant("n1", 0.5))
    assert note_of(client, low)["attach_offer"] is None
    wrong = ai(app, client, text + " ещё", Hesitant("n9", 0.95))
    assert note_of(client, wrong)["attach_offer"] is None
    sure = ai(app, client, text + " снова", Hesitant("n1", 0.95))
    assert note_of(client, sure)["attach_offer"] is not None
    trash_all_but(app, target)
    mock = ai(app, client, text + " и ещё немного")  # The mock model recognises «Продолжаю про …».
    assert note_of(client, mock)["attach_offer"]["title"] == "Курсовая"


def test_candidates_reach_the_model_as_refs_and_titles_only(web):
    app, client = web
    manual(client, TARGET)
    seen = []

    class Spy(MockProvider):
        def structure(self, text, *, categories=(), timezone=None, profile=None, candidates=None):
            seen.append(candidates)
            return super().structure(text, categories=categories)

    ai(app, client, "Ещё по курсовой: добавить главу 3 и список источников", Spy())
    assert seen == [[{"ref": "n1", "title": "Курсовая"}]]


def test_malformed_model_hint_does_not_break_the_note():
    note = StructuredNote.model_validate({"title": "T", "markdown": "m", "conclusions": [],
                                          "continues_note": {"ref": "bad", "confidence": 7}})
    assert note.continues_note is None


def test_offer_expires_after_a_day_and_the_note_stays_separate(web):
    app, client = web
    target = manual(client, TARGET)
    job = ai(app, client, "Ещё по курсовой: добавить главу 3 и список источников")
    with app.state.sessions.begin() as db:
        db.get(Capture, job["capture_id"]).attach_offered_at = time.time() - attach.OFFER_TTL - 5
    assert note_of(client, job)["attach_offer"] is None
    r = client.post(f"/api/v1/captures/{job['capture_id']}/attach", json={"note_id": target})
    assert r.status_code == 409 and len(titles(client)) == 2


def test_other_users_note_and_capture_are_forbidden(app, client):
    register(client, "alice")
    manual(client, TARGET)
    job = ai(app, client, "Ещё по курсовой: добавить главу 3 и список источников")
    own = note_of(client, job)
    other = manual_user_note(app)
    stolen = client.post(f"/api/v1/captures/{job['capture_id']}/attach", json={"note_id": other})
    assert stolen.status_code == 404
    client.cookies.clear()
    register(client, "bob")
    assert client.post(f"/api/v1/captures/{job['capture_id']}/attach", json={"note_id": own["attach_offer"]["note_id"]}).status_code == 404
    assert client.post(f"/api/v1/captures/{job['capture_id']}/separate").status_code == 404


def manual_user_note(app):
    """A note of some other account, straight in the database."""
    from app.models import User, new_id
    with app.state.sessions.begin() as db:
        user = User(username="stranger", password_hash="x")
        db.add(user)
        db.flush()
        capture = Capture(id=new_id(), user_id=user.id, original_text="x" * 30, idempotency_key="k")
        db.add(capture)
        db.flush()
        note = Note(id=new_id(), capture_id=capture.id, user_id=user.id, title="Чужая", markdown="y" * 40,
                    conclusions=[], provider="manual")
        db.add(note)
        return note.id


def test_write_calls_need_the_csrf_token(web):
    app, client = web
    manual(client, TARGET)
    job = ai(app, client, "Ещё по курсовой: добавить главу 3 и список источников")
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(f"/api/v1/captures/{job['capture_id']}/separate").status_code in (401, 403)
    client.headers["X-CSRF-Token"] = token


def test_reminders_and_tasks_follow_the_record_both_ways(web):
    app, client = web
    target = manual(client, TARGET)
    job = ai(app, client, "Добавь к заметке про курсовую: завтра в 10 напомни сдать главу научруку")
    assert job["attached"] is True
    with app.state.sessions() as db:
        reminders = db.scalars(select(Reminder)).all()
        assert reminders and {r.note_id for r in reminders} == {target}
        tasks = db.scalars(select(Item).where(Item.kind == "task")).all()
        assert tasks and {t.note_id for t in tasks} == {target}
    note = client.get("/api/v1/notes/" + target).json()
    assert len(note["items"]) == len(tasks)
    undone = client.post(f"/api/v1/captures/{job['capture_id']}/detach").json()
    with app.state.sessions() as db:
        assert {r.note_id for r in db.scalars(select(Reminder))} == {undone["note_id"]}
        assert {t.note_id for t in db.scalars(select(Item).where(Item.kind == "task"))} == {undone["note_id"]}
    assert client.get("/api/v1/notes/" + target).json()["markdown"].strip() == TARGET


def test_undo_after_the_section_was_edited_keeps_the_text(web):
    app, client = web
    target = manual(client, TARGET)
    job = ai(app, client, "Добавь к заметке про курсовую: глава три готова, осталось оформление")
    note = client.get("/api/v1/notes/" + target).json()
    edited = note["markdown"].replace("осталось", "осталось ещё")
    assert client.patch("/api/v1/notes/" + target, json={"version": note["version"], "title": "Курсовая",
                                                          "markdown": edited}).status_code == 200
    r = client.post(f"/api/v1/captures/{job['capture_id']}/detach").json()
    assert r["state"] == "separate" and r["target_text_kept"] is True
    assert "осталось ещё" in client.get("/api/v1/notes/" + target).json()["markdown"]


def test_undo_window_is_a_day_and_a_hidden_note_cannot_be_restored(web):
    app, client = web
    manual(client, TARGET)
    job = ai(app, client, "Добавь к заметке про курсовую: глава три готова, осталось оформление")
    with app.state.sessions() as db:
        hidden = db.scalar(select(Note).where(Note.capture_id == job["capture_id"])).id
    assert client.post(f"/api/v1/trash/{hidden}/restore").status_code == 404
    with app.state.sessions.begin() as db:
        db.get(Capture, job["capture_id"]).attach_decided_at = time.time() - attach.UNDO_TTL - 5
    assert client.post(f"/api/v1/captures/{job['capture_id']}/detach").status_code == 409


def test_purging_the_target_purges_the_added_records(web):
    app, client = web
    from app.notes_manage import purge_expired
    target = manual(client, TARGET)
    ai(app, client, "Добавь к заметке про курсовую: глава три готова, осталось оформление")
    assert client.delete(f"/api/v1/notes/{target}?version=2").status_code == 204
    with app.state.sessions.begin() as db:
        for note in db.scalars(select(Note)):
            note.deleted_at = time.time() - 40 * 86400
    purge_expired(app.state.sessions, app.state.settings, None)
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Note)) == 0


# ---- Telegram ----

def telegram_reply(client):
    items = claim(client).json()["items"]
    [reply] = [i for i in items if i["text"].startswith(("Заметка готова", "Добавил"))]
    return reply


def process_telegram(app, client, text, update_id):
    saved = message(client, update_id=update_id, text=text).json()
    assert Worker(app.state.sessions, app.state.settings, provider=MockProvider()).run_once()
    return saved


def test_bot_reply_asks_with_two_buttons_and_the_choice_edits_the_message(reminders):
    app, client, ids = reminders
    target = manual(client, TARGET)
    saved = process_telegram(app, client, "Ещё по курсовой: добавить главу 3 и список источников", 300)
    reply = telegram_reply(client)
    assert reply["text"].endswith("Похоже, это продолжение «Курсовая». Добавить туда?")
    assert reply["attach"] == {"capture_id": saved["capture_id"], "mode": "ask", "title": "Курсовая"}
    base = {"bot_id": 1234567890123, "telegram_user_id": 2345678901234, "chat_id": 2345678901234}
    headers = {"Authorization": "Bearer test-service-secret-" + "a" * 32}
    body = {**base, "update_id": 301, "capture_id": saved["capture_id"], "action": "attach"}
    done = client.post("/internal/v1/telegram/attach", headers=headers, json=body)
    assert done.status_code == 200, done.text
    data = done.json()
    assert data["status"] == "attached" and data["undo"] is True
    assert data["text"].startswith("Добавил в «Курсовая».") and data["text"].endswith("Открыть заметку")
    assert data["links"][0]["url"].endswith("/?capture=" + saved["capture_id"])
    again = client.post("/internal/v1/telegram/attach", headers=headers, json={**body, "update_id": 302})
    assert again.json()["status"] == "attached"
    assert client.get("/api/v1/notes/" + target).json()["attached"][0]["capture_id"] == saved["capture_id"]
    undo = client.post("/internal/v1/telegram/attach", headers=headers, json={**body, "update_id": 303, "action": "undo"})
    assert undo.json()["status"] == "separate" and undo.json()["undo"] is False
    assert undo.json()["text"].startswith("Отменил добавление.")
    gone = client.post("/internal/v1/telegram/attach", headers=headers, json={
        **body, "update_id": 304, "capture_id": "00000000-0000-4000-8000-000000000000"})
    assert gone.status_code == 409 and gone.json()["error"]["code"] == "attach_expired"
    sep = process_telegram(app, client, "Ещё по курсовой: добавить главу 4 и приложения", 305)
    chosen = client.post("/internal/v1/telegram/attach", headers=headers, json={
        **base, "update_id": 306, "capture_id": sep["capture_id"], "action": "separate"})
    assert chosen.json()["text"].startswith("Оставил отдельной заметкой.")


def test_bot_reply_for_an_explicit_request_says_added_with_undo(reminders):
    app, client, ids = reminders
    manual(client, TARGET)
    saved = process_telegram(app, client, "Добавь к заметке про курсовую: завтра в 10 напомни сдать главу", 310)
    reply = telegram_reply(client)
    assert reply["text"].startswith("Добавил в «Курсовая».") and "Открыть заметку" in reply["text"]
    assert reply["attach"]["mode"] == "undo" and reply["attach"]["capture_id"] == saved["capture_id"]
    assert auth(client, reply).json() == {"send": True}
    assert result(client, reply).status_code == 200


def test_bot_reply_stays_plain_when_the_offer_lapsed(reminders):
    app, client, ids = reminders
    manual(client, TARGET)
    saved = process_telegram(app, client, "Ещё по курсовой: добавить главу 3 и список источников", 320)
    with app.state.sessions.begin() as db:
        db.get(Capture, saved["capture_id"]).attach_offered_at = time.time() - attach.OFFER_TTL - 1
    reply = telegram_reply(client)
    assert reply["attach"] is None and "Добавить туда" not in reply["text"]
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Outbox)) >= 1
