"""Анкета «Знакомство»: API, задание воркера, проверка ответа модели и то, как профиль доходит до ИИ.

Только синтетические данные, MockProvider и httpx.MockTransport. Живой сервис не нужен и не используется.
"""

import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app import data_crypto
from app import profile as service
from app.assistant_retrieval import AssistantContext
from app.limits import units_used
from app.models import ProviderUsage, User, UserProfile
from app.profile_jobs import KEPT_UNITS
from app.profile_questions import INTRO, PRIVACY, QUESTION_IDS, QUESTIONS
from app.providers import (
    PROFILE_PROMPT,
    SYSTEM_PROMPT,
    CloudRuProvider,
    MockProvider,
    ProviderError,
    assistant_input,
    structure_context,
)
from app.worker import Worker
from tests.conftest import register
from tests.test_notes import submit

PRIVACY_LINE = (
    "Ответы хранятся в зашифрованном виде, в админке мы их не видим. ИИ использует их только для разбора ваших заметок. "
    "Всё можно посмотреть, исправить или удалить в разделе «Что beresta знает обо мне»."
)
ANSWERS = {
    "q1": "Учусь в ГУАП, 3 курс. Подрабатываю.",
    "q2": "ГУАП = университет, ОС = операционные системы",
    "q3": "Встаю в 7:30, ложусь в 23. Не беспокоить с 23:00 до 7:00",
    "q4": "Утром это в 7:30, вечером 20:00, после пар 15:10, на выходных в 11:00",
    "q5": "Часто откладываю лабы. Лучше напоминать дважды: заранее и в сам момент.",
    "q6": "коротко по пунктам",
}
SECRET_WORD = "Фиолетовыйслон"


def api(client, method, path, **kwargs):
    return client.request(method, "/api/v1/profile" + path, **kwargs)


def work(app, provider=None, **settings):
    worker = Worker(app.state.sessions, replace(app.state.settings, **settings), provider=provider or MockProvider())
    return worker.run_once()


def row_of(app, user_id):
    with app.state.sessions() as db:
        row = db.get(UserProfile, user_id)
        db.expunge(row) if row is not None else None
        return row


def make_ready(app, user_id, *, facts=("Учится в ГУАП, 3 курс",), prefs=None, answers=None):
    """A finished profile put straight into the database: for tests of what the profile changes."""
    base = {"dayparts": {}, "quiet_hours": None, "wake": None, "sleep": None, "reminder_style": None,
            "note_style": None, "vocabulary": []}
    with app.state.sessions.begin() as db:
        row = db.get(UserProfile, user_id) or UserProfile(user_id=user_id, state="ready", updated_at=time.time())
        row.state, row.answers = "ready", answers or {"q1": "x"}
        row.facts = service.fact_rows(list(facts))
        row.prefs = {**base, **(prefs or {})}
        db.merge(row)


def second_client(app, name="other"):
    client = TestClient(app)
    register(client, name)
    return client


class Recording(MockProvider):
    """Remembers what the note and assistant calls received."""

    def __init__(self):
        self.structure_calls, self.assistant_calls = [], []

    def structure(self, text, *, categories=(), timezone=None, profile=None):
        self.structure_calls.append({"text": text, "profile": profile})
        return super().structure(text, categories=categories, timezone=timezone)

    def assistant(self, kind, context, *, question=None):
        self.assistant_calls.append({"kind": kind, "profile": context.profile, "input": assistant_input(kind, context, question)})
        return super().assistant(kind, context, question=question)


class Scripted(MockProvider):
    """Answers the questionnaire with whatever the test wants, or fails."""

    def __init__(self, answer=None, error=None, before=None):
        self.answer, self.error, self.before, self.calls = answer, error, before, []

    def profile(self, data, *, on_usage=None):
        self.calls.append(data)
        if self.before:
            self.before()
        if self.error:
            raise self.error
        return self.answer


# ---- Вопросы и состояния ----


def test_a_new_account_gets_the_questions_and_no_profile(client):
    user = register(client)
    view = api(client, "GET", "").json()
    assert view["state"] == "new" and view["answers"] is None and view["facts"] == [] and view["updated_at"] is None
    assert view["calendar_offer"] is True and view["error"] is None
    assert view["suggested_categories"] == [] and view["categories_confirmed"] is False
    assert "reminder_channel" not in view
    assert view["questions"] == [{"id": qid, "text": text} for qid, text in QUESTIONS] and len(view["questions"]) == 7
    assert view["intro"] == INTRO and view["privacy"] == PRIVACY == PRIVACY_LINE
    me = client.get("/api/v1/auth/me").json()
    assert me["profile_state"] == "new" and me["calendar_offer"] is True and me["id"] == user["id"]
    assert "reminder_channel" not in me


def test_the_questions_live_in_one_module_and_have_the_agreed_wording():
    assert QUESTION_IDS == ("q1", "q2", "q3", "q4", "q5", "q6", "q7")
    assert dict(QUESTIONS)["q1"] == "Чем вы сейчас занимаетесь? Учёба, работа, свои проекты, что угодно."
    assert dict(QUESTIONS)["q7"] == "Что ещё beresta стоит знать о вас, чтобы помогать лучше?"
    assert "мы ничего не видим" not in PRIVACY


def test_answers_become_a_profile_through_the_worker(app, client):
    user = register(client)
    reply = api(client, "POST", "/answers", json={"answers": ANSWERS})
    assert reply.status_code == 202 and reply.json() == {"state": "pending"}
    pending = api(client, "GET", "").json()
    assert pending["state"] == "pending" and pending["answers"]["q1"] == ANSWERS["q1"] and pending["facts"] == []
    assert client.get("/api/v1/auth/me").json()["profile_state"] == "pending"
    assert work(app) is True
    ready = api(client, "GET", "").json()
    assert ready["state"] == "ready" and ready["answers"] == {k: v.strip() for k, v in ANSWERS.items()}
    assert [f["id"] for f in ready["facts"]] == ["f1", "f2", "f3"] and ready["facts"][0]["source"] == "ai"
    assert ready["facts"][0]["text"] == "Учусь в ГУАП, 3 курс."
    assert ready["updated_at"] >= pending["updated_at"]
    row = row_of(app, user["id"])
    assert row.prefs["dayparts"] == {
        "morning": "07:30", "evening": "20:00", "after_study": "15:10", "weekend": "11:00",
    }
    assert (row.prefs["wake"], row.prefs["sleep"], row.prefs["note_style"]) == ("07:30", "23:00", "short")
    assert row.prefs["quiet_hours"] == {"from": "23:00", "to": "07:00"} and row.prefs["reminder_style"] == "twice"
    assert row.prefs["vocabulary"] == [
        {"term": "ГУАП", "meaning": "университет"}, {"term": "ОС", "meaning": "операционные системы"},
    ]
    assert client.get("/api/v1/auth/me").json()["profile_state"] == "ready"
    assert work(app) is False  # Nothing else is waiting.


def test_the_run_costs_one_unit_and_leaves_a_receipt(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    with app.state.sessions() as db:
        assert units_used(db, app.state.settings, user["id"]) == 0  # Nothing is paid until the model is asked.
    work(app)
    with app.state.sessions() as db:
        assert units_used(db, app.state.settings, user["id"]) == 1
        (receipt,) = db.scalars(select(ProviderUsage)).all()
        assert receipt.channel == "web" and receipt.kind == "llm" and receipt.status == "succeeded"
        assert receipt.operation_id.startswith("profile:" + user["id"])
    assert client.get("/api/v1/provider/usage").json()["daily_units_used"] == 1


def test_the_same_answers_are_never_paid_twice(app, client):
    register(client)
    body = {"answers": ANSWERS}
    assert api(client, "POST", "/answers", json=body).json() == {"state": "pending"}
    assert api(client, "POST", "/answers", json=body).json() == {"state": "pending"}  # Replayed while waiting.
    work(app)
    assert api(client, "POST", "/answers", json=body).json() == {"state": "ready"}  # Replayed after the result.
    assert work(app) is False
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProviderUsage)) == 1


def test_other_answers_wait_until_the_first_ones_are_read(app, client):
    register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    other = api(client, "POST", "/answers", json={"answers": {"q1": "Другое"}})
    assert other.status_code == 409 and api(client, "GET", "").json()["answers"]["q1"] == ANSWERS["q1"]


@pytest.mark.parametrize("body", [
    {"answers": {}},
    {"answers": {"q1": "   ", "q2": ""}},
    {"answers": {"q8": "Что-то"}},
    {"answers": {"q1": "я" * 1001}},
    {"answers": {"q1": 5}},
    {"answers": {"q1": "ok\x00"}},
    {"answers": ["q1"]},
    {"answers": {"q1": "ok"}, "reminder_channel": "telegram"},
    {"answers": {"q1": "ok"}, "extra": True},
    {},
])
def test_bad_answers_are_refused(client, body):
    register(client)
    assert api(client, "POST", "/answers", json=body).status_code == 422
    assert api(client, "GET", "").json()["state"] == "new"


def test_the_limits_of_an_answer_are_exact(client):
    register(client)
    one_thousand = {"q1": "я" * 1000, "q7": " Одна строка "}
    assert api(client, "POST", "/answers", json={"answers": one_thousand}).status_code == 202
    assert api(client, "GET", "").json()["answers"] == {"q1": "я" * 1000, "q7": "Одна строка"}


def test_decline_is_forever_but_the_questionnaire_stays_open(app, client):
    register(client)
    assert api(client, "POST", "/decline").json() == {"state": "declined"}
    assert api(client, "POST", "/decline").json() == {"state": "declined"}
    assert client.get("/api/v1/auth/me").json()["profile_state"] == "declined"
    view = api(client, "GET", "").json()
    assert view["state"] == "declined" and view["answers"] is None and len(view["questions"]) == 7
    assert api(client, "POST", "/answers", json={"answers": ANSWERS}).json() == {"state": "pending"}
    work(app)
    assert api(client, "GET", "").json()["state"] == "ready"


def test_a_finished_profile_is_not_wiped_by_decline(app, client):
    register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    assert api(client, "POST", "/decline").status_code == 409  # Waiting for the worker.
    work(app)
    assert api(client, "POST", "/decline").status_code == 409
    assert api(client, "GET", "").json()["state"] == "ready"


def test_retaking_replaces_answers_and_facts(app, client):
    register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    work(app)
    again = api(client, "POST", "/answers", json={"answers": {"q1": "Работаю в студии звука."}})
    assert again.json() == {"state": "pending"}
    view = api(client, "GET", "").json()
    assert view["facts"] == [] and view["answers"] == {"q1": "Работаю в студии звука."}
    work(app)
    view = api(client, "GET", "").json()
    assert view["state"] == "ready" and [f["text"] for f in view["facts"]] == ["Работаю в студии звука."]
    assert [f["id"] for f in view["facts"]] == ["f1"]


def test_facts_can_be_edited_and_deleted(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    work(app)
    edited = api(client, "PATCH", "/facts/f2", json={"text": "  Учится\nв   ГУАП  "})
    assert edited.status_code == 200
    facts = {f["id"]: f for f in edited.json()["facts"]}
    assert facts["f2"]["text"] == "Учится в ГУАП" and facts["f2"]["source"] == "edited" and facts["f1"]["source"] == "ai"
    removed = api(client, "DELETE", "/facts/f1")
    assert removed.status_code == 200 and [f["id"] for f in removed.json()["facts"]] == ["f2", "f3"]
    assert row_of(app, user["id"]).state == "ready"
    for call in (("PATCH", "/facts/f1", {"json": {"text": "x"}}), ("DELETE", "/facts/f1", {}),
                 ("PATCH", "/facts/f99", {"json": {"text": "x"}}), ("DELETE", "/facts/f1%2F..", {}),
                 ("PATCH", "/facts/x1", {"json": {"text": "x"}})):
        assert api(client, call[0], call[1], **call[2]).status_code == 404
    for bad in ("", "   ", "я" * 201, "a\x00b"):
        assert api(client, "PATCH", "/facts/f2", json={"text": bad}).status_code == 422
    assert api(client, "PATCH", "/facts/f2", json={"text": "я" * 200}).status_code == 200


def test_delete_all_removes_answers_and_facts_and_keeps_the_popup_away(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    work(app)
    view = api(client, "DELETE", "").json()
    assert view["state"] == "declined" and view["answers"] is None and view["facts"] == []
    row = row_of(app, user["id"])
    assert (row.state, row.answers, row.facts, row.prefs, row.answers_hash) == ("declined", None, None, None, None)
    assert client.get("/api/v1/auth/me").json()["profile_state"] == "declined"
    assert api(client, "DELETE", "").json()["state"] == "declined"  # Nothing to delete is fine.


def test_delete_while_waiting_drops_a_late_result(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    provider = Scripted(
        answer={"facts": ["Учится в ГУАП"]},
        before=lambda: api(client, "DELETE", ""),
    )
    assert work(app, provider) is True
    row = row_of(app, user["id"])
    assert row.state == "declined" and row.facts is None  # The person deleted it meanwhile: the result is stale.


def test_the_calendar_offer_can_be_switched_any_time(client):
    register(client)
    for value in (False, True, False):
        reply = api(client, "PATCH", "/calendar-offer", json={"calendar_offer": value})
        assert reply.status_code == 200 and reply.json() == {"calendar_offer": value}
        assert client.get("/api/v1/auth/me").json()["calendar_offer"] is value
        assert api(client, "GET", "").json()["calendar_offer"] is value
    assert api(client, "PATCH", "/calendar-offer", json={"calendar_offer": "sms"}).status_code == 422
    assert api(client, "PATCH", "/calendar-offer", json={}).status_code == 422
    assert api(client, "PATCH", "/reminder-channel", json={"reminder_channel": "calendar"}).status_code in (404, 405)


# ---- Доступ ----

WRITES = [
    ("POST", "/answers", {"json": {"answers": {"q1": "x"}}}), ("POST", "/decline", {}), ("POST", "/retry", {}),
    ("PATCH", "/calendar-offer", {"json": {"calendar_offer": False}}),
    ("POST", "/categories", {"json": {"names": ["Учёба"]}}), ("POST", "/categories/skip", {}),
    ("PATCH", "/facts/f1", {"json": {"text": "x"}}), ("DELETE", "/facts/f1", {}), ("DELETE", "", {}),
]


@pytest.mark.parametrize("method,path,extra", WRITES)
def test_every_write_needs_the_csrf_token_and_a_session(app, client, method, path, extra):
    register(client)
    token = client.headers.pop("X-CSRF-Token")
    assert api(client, method, path, **extra).status_code == 403
    assert api(client, method, path, headers={"X-CSRF-Token": "wrong"}, **extra).status_code == 403
    client.headers["X-CSRF-Token"] = token
    stranger = TestClient(app)
    assert stranger.request(method, "/api/v1/profile" + path, **extra).status_code == 401
    assert stranger.get("/api/v1/profile").status_code == 401


def test_profiles_belong_to_their_owners(app, client):
    mine = register(client)
    api(client, "POST", "/answers", json={"answers": {"q1": SECRET_WORD}})
    work(app)
    other = second_client(app)
    view = other.get("/api/v1/profile").json()
    assert view["state"] == "new" and view["facts"] == [] and SECRET_WORD not in json.dumps(view)
    assert other.patch("/api/v1/profile/facts/f1", json={"text": "чужое"}).status_code == 404
    assert other.delete("/api/v1/profile/facts/f1").status_code == 404
    assert other.post("/api/v1/profile/retry").status_code == 409
    assert other.delete("/api/v1/profile").json()["state"] == "declined"  # Only their own (empty) profile.
    mine_view = api(client, "GET", "").json()
    assert mine_view["state"] == "ready" and mine_view["facts"][0]["text"] == SECRET_WORD
    assert row_of(app, mine["id"]).state == "ready"


def test_submitting_is_rate_limited(client):
    register(client)
    codes = [api(client, "POST", "/answers", json={"answers": {"q1": f"ответ {n}"}}).status_code for n in range(8)]
    assert codes[0] == 202 and codes.count(429) >= 1 and 409 in codes  # The first is kept waiting, later ones refused.


# ---- Лимит ИИ и сбои ----


def test_a_spent_daily_limit_keeps_the_answers_and_allows_a_retry_later(app_factory):
    app = app_factory(daily_unit_limit=1)
    with TestClient(app) as client:
        user = register(client)
        api(client, "POST", "/answers", json={"answers": {"q1": "Первые"}})
        work(app)  # The only unit of the day.
        reply = api(client, "POST", "/answers", json={"answers": {"q1": "Вторые"}})
        assert reply.status_code == 429
        assert reply.json()["detail"] == "На сегодня лимит ИИ закончился. Он обновится завтра."
        assert "limit_resets_at" in reply.json()
        view = api(client, "GET", "").json()
        assert view["state"] == "failed" and view["answers"] == {"q1": "Вторые"} and view["facts"] == []
        assert view["error"] == "На сегодня лимит ИИ закончился. Он обновится завтра."
        assert api(client, "POST", "/retry").status_code == 429
        assert work(app) is False
        with app.state.sessions.begin() as db:  # A new day: yesterday's paid run no longer counts.
            db.get(UserProfile, user["id"]).unit_times = [time.time() - 3 * 86400]
        assert api(client, "POST", "/retry").json() == {"state": "pending"}
        work(app)
        assert api(client, "GET", "").json()["state"] == "ready"


def test_a_failed_call_can_be_retried_and_is_paid_when_the_model_was_asked(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    assert work(app, Scripted(error=ProviderError("provider_unavailable"))) is True
    view = api(client, "GET", "").json()
    assert view["state"] == "failed" and view["answers"]["q1"] == ANSWERS["q1"]
    assert view["error"] == "ИИ не ответил. Попробуйте ещё раз позже."
    assert client.get("/api/v1/auth/me").json()["profile_state"] == "failed"
    assert api(client, "POST", "/retry").json() == {"state": "pending"}
    assert api(client, "POST", "/retry").status_code == 409  # It is waiting again: nothing to retry.
    work(app)
    assert api(client, "GET", "").json()["state"] == "ready"
    assert len(row_of(app, user["id"]).unit_times) == 2


def test_nothing_to_retry_without_a_failure(client):
    register(client)
    assert api(client, "POST", "/retry").status_code == 409


def test_a_call_that_never_left_gives_the_unit_back(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    work(app, Scripted(error=ProviderError("budget_exhausted")))
    row = row_of(app, user["id"])
    assert row.state == "failed" and row.error_code == "budget_exhausted" and row.unit_times == []
    assert api(client, "GET", "").json()["error"] == "Общий лимит запросов к ИИ закончился. Попробуйте позже."


def test_a_provider_without_the_call_fails_cleanly(app, client):
    register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})

    class Plain:
        def structure(self, *a, **k):
            raise AssertionError

    assert work(app, Plain()) is True
    assert api(client, "GET", "").json()["state"] == "failed"


def test_a_lease_that_ran_out_fails_the_profile_for_a_retry(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    with app.state.sessions.begin() as db:
        db.get(UserProfile, user["id"]).lease_until = time.time() - 5
    assert work(app) is False
    row = row_of(app, user["id"])
    assert (row.state, row.error_code, row.lease_until) == ("failed", "execution_unknown", None)


def test_only_a_few_paid_moments_are_remembered(app, client):
    user = register(client)
    with app.state.sessions.begin() as db:
        db.add(UserProfile(user_id=user["id"], state="pending", answers={"q1": "x"}, answers_hash="h",
                           unit_times=[1.0] * 80, updated_at=time.time()))
    work(app)
    assert len(row_of(app, user["id"]).unit_times) == KEPT_UNITS


# ---- Проверка ответа модели ----


def test_the_model_answer_is_cleaned_strictly():
    facts, prefs = service.clean_profile({
        "facts": [
            "Учится в ГУАП", "Учится в ГУАП", "x" * 201, "", 7, "У него депрессия", "Верит в бога",
            "Голосует за партию", "Имеет ипотеку и долги", "Ориентация такая-то", "Любит гейм-дизайн",
            "Пишет\x00плохо", "Работает над проектом «Береста»",
        ] + [f"Пункт {n}" for n in range(30)],
        "dayparts": {"morning": "7:30", "day": "13:00", "evening": "25:00", "after_study": "15:10", "night": "03:00",
                     "weekend": 5, "after_work": None},
        "quiet_hours": {"from": "23:00", "to": "07:00"}, "wake": "07:30", "sleep": "later",
        "reminder_style": "often", "note_style": "poetic",
        "suggested_categories": ["Учёба", "учёба", "Работа", "Общее", "Здоровье и депрессия", "x" * 41, 5, "Стартап"]
        + [f"Тема{n}" for n in range(9)],
        "vocabulary": [
            {"term": "ГУАП", "meaning": "университет"}, {"term": "ГУАП", "meaning": "повтор"},
            {"term": "ОС", "meaning": "операционные системы"}, {"term": "ДД", "meaning": "лекарства от болезни"},
            {"term": "x" * 61, "meaning": "длинный"}, {"term": "а", "meaning": "б", "extra": 1}, "строка",
        ] + [{"term": f"т{n}", "meaning": "м"} for n in range(60)],
    })
    assert facts[:3] == ["Учится в ГУАП", "Любит гейм-дизайн", "Работает над проектом «Береста»"]
    assert len(facts) == service.MAX_FACTS and all(len(f) <= 200 for f in facts) and len(set(facts)) == len(facts)
    assert not any(word in " ".join(facts) for word in ("депресс", "бог", "партию", "ипотек", "Ориентац"))
    assert prefs["dayparts"] == {"day": "13:00", "after_study": "15:10"}
    assert prefs["quiet_hours"] == {"from": "23:00", "to": "07:00"} and prefs["wake"] == "07:30"
    assert prefs["sleep"] is None and prefs["note_style"] is None and prefs["reminder_style"] is None
    assert prefs["suggested_categories"] == ["Учёба", "Работа", "Стартап", "Тема0", "Тема1", "Тема2"]
    terms = [v["term"] for v in prefs["vocabulary"]]
    assert terms[:2] == ["ГУАП", "ОС"] and len(terms) == service.MAX_VOCABULARY and "ДД" not in terms


def test_nothing_usable_is_an_empty_profile_and_junk_is_a_bad_answer():
    facts, prefs = service.clean_profile({})
    assert facts == [] and prefs["dayparts"] == {} and prefs["quiet_hours"] is None and prefs["vocabulary"] == []
    facts, prefs = service.clean_profile({"facts": "text", "dayparts": [], "quiet_hours": {"from": "23:00"}})
    assert facts == [] and prefs["quiet_hours"] is None
    assert service.clean_profile({"quiet_hours": {"from": "23:00", "to": "23:00"}})[1]["quiet_hours"] is None
    with pytest.raises(ValueError):
        service.clean_profile(["facts"])


@pytest.mark.parametrize("word", [
    "диагноз", "лечение", "религия", "православный", "политика", "оппозиция", "сексуальность", "ЛГБТ", "долги",
    "кредит", "зарплата", "ипотека", "беременность", "депрессия", "Диагноз", "ДОЛГ",
])
def test_sensitive_topics_are_never_kept(word):
    assert service.clean_facts([f"Человек пишет про {word} и учится"]) == []


@pytest.mark.parametrize("fact", ["Любит долго спать", "Играет в гейм-дизайн", "Учится на врача", "Ходит в храмовую школу?"])
def test_ordinary_words_are_not_mistaken_for_sensitive_ones(fact):
    kept = service.clean_facts([fact])
    assert kept == ([] if "храм" in fact else [fact])


def test_a_dirty_model_answer_reaches_the_profile_only_cleaned(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    answer = {
        "facts": ["Учится в ГУАП", "Лечится от диабета", "Не любит ранние пары"] + [f"Лишнее {n}" for n in range(20)],
        "dayparts": {"morning": "07:30", "evening": "вечером"}, "wake": "07:00", "sleep": None,
        "quiet_hours": None, "reminder_style": "once", "note_style": "short", "vocabulary": [],
    }
    work(app, Scripted(answer=answer))
    view = api(client, "GET", "").json()
    texts = [f["text"] for f in view["facts"]]
    assert texts[:2] == ["Учится в ГУАП", "Не любит ранние пары"] and len(texts) == 12 and "диабет" not in " ".join(texts)
    row = row_of(app, user["id"])
    assert row.prefs["dayparts"] == {"morning": "07:30"} and row.prefs["wake"] == "07:00"


def test_the_model_gets_the_questions_and_answers_as_data_and_nothing_else(app, client):
    register(client)
    api(client, "POST", "/answers", json={"answers": {"q2": "ГУАП = университет", "q6": "коротко"}})
    provider = Scripted(answer={"facts": []})
    work(app, provider)
    (data,) = provider.calls
    assert set(data) == {"questions", "answers"} and data["answers"] == {"q2": "ГУАП = университет", "q6": "коротко"}
    assert [q["id"] for q in data["questions"]] == ["q2", "q6"]


def test_output_that_repeats_a_secret_or_our_prompt_is_blocked(app, client, monkeypatch, tmp_path):
    register(client)
    key = tmp_path / "service.key"
    key.write_text("profile-test-secret-value-1234567890", encoding="ascii")
    monkeypatch.setenv("NOTES_INTERNAL_API_TOKEN_FILE", str(key))
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    work(app, Scripted(answer={"facts": ["Токен profile-test-secret-value-1234567890"]}))
    view = api(client, "GET", "").json()
    assert view["state"] == "failed" and view["facts"] == []
    assert view["error"] == "ИИ ответил странно, поэтому ответ не показан."
    api(client, "POST", "/retry")
    leaked = " ".join(PROFILE_PROMPT.split())[40:120]
    work(app, Scripted(answer={"facts": [leaked]}))
    assert api(client, "GET", "").json()["state"] == "failed"
    api(client, "POST", "/retry")
    work(app, Scripted(answer={"facts": ["Ключ sk-a1b2c3d4e5f6g7h8i9j0k1l2"]}))
    assert api(client, "GET", "").json()["state"] == "failed"


def test_a_bad_answer_from_the_model_is_repeated_once(app, client):
    register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    outcomes = iter([["not an object"], {"facts": ["Работает"]}])

    class Flaky(MockProvider):
        def profile(self, data, *, on_usage=None):
            return next(outcomes)

    work(app, Flaky())
    assert [f["text"] for f in api(client, "GET", "").json()["facts"]] == ["Работает"]


# ---- Профиль в запросах к ИИ ----


def test_the_note_call_gets_the_profile_but_never_the_raw_answers(app, client):
    user = register(client)
    make_ready(
        app, user["id"], facts=["Учится в ГУАП, 3 курс"],
        prefs={"dayparts": {"morning": "07:30"}, "note_style": "short", "reminder_style": "early", "wake": "07:00",
               "vocabulary": [{"term": "ОС", "meaning": "операционные системы"}]},
        answers={"q1": SECRET_WORD},
    )
    provider = Recording()
    assert submit(client).status_code == 202
    Worker(app.state.sessions, app.state.settings, provider=provider).run_once()
    (call,) = provider.structure_calls
    assert call["profile"] == {
        "facts": ["Учится в ГУАП, 3 курс"], "vocabulary": [{"term": "ОС", "meaning": "операционные системы"}],
        "note_style": "short", "dayparts": {"morning": "07:30"},
    }
    assert SECRET_WORD not in json.dumps(call, ensure_ascii=False)


def test_a_profile_that_is_not_ready_is_not_used(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": {"q1": SECRET_WORD}})
    provider = Recording()
    submit(client)
    Worker(app.state.sessions, app.state.settings, provider=provider).run_once()
    assert provider.structure_calls[0]["profile"] is None
    assert service.prompt_profile.__name__ and row_of(app, user["id"]).state == "pending"


def test_a_provider_that_does_not_know_profiles_still_works(app, client):
    user = register(client)
    make_ready(app, user["id"])

    class Old(MockProvider):
        def structure(self, text, *, categories=(), timezone=None):
            return super().structure(text, categories=categories, timezone=timezone)

    submit(client)
    Worker(app.state.sessions, app.state.settings, provider=Old()).run_once()
    assert client.get("/api/v1/jobs").json()[0]["status"] == "succeeded"


def captured_gateway(requests):
    def handle(request):
        requests.append(json.loads(request.content))
        body = {"title": "T", "markdown": "М", "conclusions": [], "items": [], "category_name": None}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}, "finish_reason": "stop"}]})

    return httpx.MockTransport(handle)


def test_the_gateway_request_carries_the_profile_as_data_and_the_prompt_stays_short():
    sent = []
    provider = CloudRuProvider("k" * 16, "claude-opus-4.7", transport=captured_gateway(sent))
    profile = {"facts": ["Учится в ГУАП"], "note_style": "short", "dayparts": {"morning": "07:30"}}
    provider.structure("Купить хлеб", categories=("Дом",), timezone="Europe/Moscow", profile=profile)
    provider.structure("Купить хлеб", categories=("Дом",), timezone="Europe/Moscow")
    with_profile, without = sent
    context = json.loads(with_profile["messages"][1]["content"])
    assert context["user_profile"] == profile and context["existing_categories"] == ["Дом"]
    assert "user_profile" not in json.loads(without["messages"][1]["content"])
    assert with_profile["messages"][0]["content"] == SYSTEM_PROMPT
    assert "user_profile" in SYSTEM_PROMPT and "note_style" in SYSTEM_PROMPT
    # The rule for the profile is short: the prompt grew by less than a tenth.
    assert len(SYSTEM_PROMPT) <= 5700  # 4037 before the note style rules (docs/note-style-v1.md)


def test_structure_context_has_no_profile_key_without_one():
    assert "user_profile" not in json.loads(structure_context(["Дом"], "UTC"))
    assert json.loads(structure_context([], "UTC", profile={"facts": ["a"]}))["user_profile"] == {"facts": ["a"]}


def test_the_assistant_gets_the_profile_next_to_the_notes(app, client):
    user = register(client)
    make_ready(app, user["id"], facts=["Пишет диплом"], prefs={"note_style": "detailed"}, answers={"q1": SECRET_WORD})
    from tests.test_assistant import add_note, post

    add_note(app, user["id"], "Диплом", "Глава про базы данных готова.")
    request = post(client, "/ask", "ask-profile", {"question": "Что с дипломом?"})
    assert request.status_code == 202
    provider = Recording()
    Worker(app.state.sessions, app.state.settings, provider=provider).run_once()
    (call,) = provider.assistant_calls
    assert call["profile"] == {"facts": ["Пишет диплом"], "note_style": "detailed"}
    sent = json.loads(call["input"])
    assert sent["user_profile"] == call["profile"] and "notes" in sent and SECRET_WORD not in call["input"]
    detail = client.get("/api/v1/assistant/requests/" + request.json()["id"]).json()
    assert detail["status"] == "succeeded"


def test_assistant_input_without_a_profile_is_unchanged():
    context = AssistantContext(notes=[], items=[])
    assert json.loads(assistant_input("ask", context, "вопрос")) == {"notes": [], "question": "вопрос"}


# ---- Хранение ----


def test_answers_facts_and_hints_are_encrypted_at_rest(app, client):
    register(client)
    api(client, "POST", "/answers", json={"answers": {"q1": SECRET_WORD + " учусь. Вот так", "q2": "АБВ = Тестовыйтермин"}})
    work(app)
    with app.state.engine.connect() as connection:
        ((answers, facts, prefs),) = connection.execute(text("SELECT answers, facts, prefs FROM user_profiles")).all()
    for value in (answers, facts, prefs):
        assert "enc:v1:" in value
    blob = answers + facts + prefs
    assert SECRET_WORD not in blob and "Тестовыйтермин" not in blob and "учусь" not in blob
    for label, counts in data_crypto.database_status(app.state.sessions).items():
        assert counts["plain"] == 0 and counts["old"] == 0, label
    report = data_crypto.encrypt_database(app.state.sessions)
    assert report["user_profiles.answers"]["errors"] == 0 and report["user_profiles.facts"]["skipped"] == 1


def test_a_plain_old_value_is_encrypted_by_the_tool_that_encrypts_existing_data(app, client):
    from app.encrypted_types import raw_values

    user = register(client)
    with raw_values(), app.state.sessions.begin() as db:
        db.add(UserProfile(user_id=user["id"], state="ready", answers={"q1": "открытый"}, facts=[{"id": "f1", "text": "x", "source": "ai"}],
                           prefs={"dayparts": {}}, updated_at=time.time()))
    assert data_crypto.database_status(app.state.sessions)["user_profiles.answers"]["plain"] == 1
    data_crypto.encrypt_database(app.state.sessions)
    assert data_crypto.database_status(app.state.sessions)["user_profiles.answers"] == {"plain": 0, "current": 1, "old": 0}
    assert api(client, "GET", "").json()["answers"] == {"q1": "открытый"}


def test_the_operator_can_erase_a_profile_with_the_account(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    work(app)
    with app.state.sessions.begin() as db:
        service.erase_profile(db, user["id"])
    assert row_of(app, user["id"]) is None
    assert api(client, "GET", "").json()["state"] == "new"


def test_the_profile_view_is_the_export_of_the_persons_own_data(app, client):
    register(client)
    api(client, "POST", "/answers", json={"answers": ANSWERS})
    work(app)
    view = api(client, "GET", "").json()
    assert view["answers"] and view["facts"] and set(view) >= {"state", "answers", "facts", "calendar_offer", "suggested_categories"}


# ---- Админка ----


def test_the_admin_side_never_reads_profile_content(app, client, admin_client):
    register(client)
    api(client, "POST", "/answers", json={"answers": {"q1": SECRET_WORD, "q2": "ЗАМЕТНЫЙ = термин"}})
    work(app)
    seen = [admin_client.get(path).text for path in (
        "/admin-api/v1/summary", "/admin-api/v1/users", "/admin-api/v1/ai-providers",
    )]
    assert all(SECRET_WORD not in page and "ЗАМЕТНЫЙ" not in page for page in seen)


def test_no_admin_module_touches_the_profile_table():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    names = [root / "admin_export.py", root / "admin_metrics.py", root / "admin_models.py", root / "admin_cli.py",
             root / "admin_providers.py", root / "admin_auth.py", root / "routes" / "admin.py", root / "dashboard.py"]
    for path in names:
        source = path.read_text(encoding="utf-8")
        assert "UserProfile" not in source and "user_profiles" not in source and "app.profile" not in source, path.name


def test_a_user_row_offers_the_calendar_by_default(app, client):
    user = register(client)
    with app.state.sessions() as db:
        assert db.get(User, user["id"]).calendar_offer is True


# ---- Образец владельца, предложенные категории, ранние напоминания ----

from app.models import Category  # noqa: E402
from app.profile_example import EXAMPLE_ANSWERS, EXAMPLE_PROFILE  # noqa: E402


def test_the_owners_example_gives_the_expected_conclusions(app, client):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": EXAMPLE_ANSWERS})
    work(app)
    view = api(client, "GET", "").json()
    assert [f["text"] for f in view["facts"]] == EXAMPLE_PROFILE["facts"] == [
        "Учится на инженера ИИ",
        "Развивает свой стартап, ведёт ещё один проект и работает",
        "Главные темы: учёба, стартап, работа",
        "С 02:00 до 10:00 не беспокоить",
        "Часто забывает дела, напоминать дважды: заранее и в нужное время",
        "Заметки удобнее короткими пунктами",
    ]
    assert view["suggested_categories"] == ["Учёба", "Стартап", "Работа"] and view["categories_confirmed"] is False
    prefs = row_of(app, user["id"]).prefs
    assert prefs["dayparts"] == {
        "morning": "10:00", "day": "16:00", "evening": "20:00", "after_study": "17:00", "after_work": "18:00",
    }  # «Выходных нет»: no weekend time
    assert prefs["quiet_hours"] == {"from": "02:00", "to": "10:00"} and (prefs["wake"], prefs["sleep"]) == ("10:00", "02:00")
    assert (prefs["reminder_style"], prefs["note_style"]) == ("twice", "short")


def test_the_regex_mock_reads_twice_and_once():
    twice = MockProvider().profile({"answers": {"q5": "Напомните дважды, заранее и вовремя"}})
    once = MockProvider().profile({"answers": {"q5": "Хватит одного раза, лучше в сам момент"}})
    none = MockProvider().profile({"answers": {"q5": "Не знаю"}})
    assert (twice["reminder_style"], once["reminder_style"], none["reminder_style"]) == ("twice", "once", None)


def ready_with_suggestions(app, client, names=("Учёба", "Стартап", "Работа")):
    user = register(client)
    api(client, "POST", "/answers", json={"answers": EXAMPLE_ANSWERS})
    work(app)
    return user


def names_of(client):
    return [row["name"] for row in client.get("/api/v1/categories").json()]


def test_confirmed_categories_are_created_once_and_never_twice(app, client):
    ready_with_suggestions(app, client)
    client.post("/api/v1/categories", json={"name": "работа"})
    reply = api(client, "POST", "/categories", json={"names": ["учёба", "Стартап", "Работа", "Своя тема", "СТАРТАП"]})
    assert reply.status_code == 200 and reply.json()["created"] == ["Учёба", "Стартап", "Своя тема"]
    assert reply.json()["categories_confirmed"] is True
    assert sorted(names_of(client)) == sorted(["Общее", "работа", "Учёба", "Стартап", "Своя тема"])
    again = api(client, "POST", "/categories", json={"names": ["Другая"]})  # A repeated request changes nothing
    assert again.status_code == 200 and again.json()["created"] == [] and "Другая" not in names_of(client)


def test_skipping_the_suggestions_creates_nothing(app, client):
    ready_with_suggestions(app, client)
    reply = api(client, "POST", "/categories/skip")
    assert reply.status_code == 200 and reply.json()["categories_confirmed"] is True
    assert names_of(client) == ["Общее"]


def test_categories_only_when_the_profile_is_ready_and_names_are_valid(app, client):
    register(client)
    assert api(client, "POST", "/categories", json={"names": ["Учёба"]}).status_code == 409
    assert api(client, "POST", "/categories/skip").status_code == 409
    api(client, "POST", "/answers", json={"answers": EXAMPLE_ANSWERS})
    assert api(client, "POST", "/categories", json={"names": ["Учёба"]}).status_code == 409  # Still pending
    work(app)
    for body in ({"names": []}, {"names": ["  ", ""]}, {"names": ["x"] * 11}, {"names": "Учёба"}, {}, {"names": ["a"], "x": 1}):
        assert api(client, "POST", "/categories", json=body).status_code == 422, body
    assert names_of(client) == ["Общее"]


def test_a_retake_asks_about_categories_again(app, client):
    ready_with_suggestions(app, client)
    api(client, "POST", "/categories/skip")
    api(client, "DELETE", "")
    assert api(client, "GET", "").json()["categories_confirmed"] is False
    api(client, "POST", "/answers", json={"answers": EXAMPLE_ANSWERS})
    work(app)
    view = api(client, "GET", "").json()
    assert view["categories_confirmed"] is False and view["suggested_categories"]


def test_categories_follow_the_normal_limits_and_stay_with_the_owner(app, client):
    owner = ready_with_suggestions(app, client)
    with app.state.sessions.begin() as db:
        db.add_all([Category(user_id=owner["id"], name=f"Т{n}") for n in range(98)])
    assert api(client, "POST", "/categories", json={"names": ["Раз", "Два", "Три"]}).status_code == 429
    other = second_client(app)
    assert api(other, "POST", "/categories", json={"names": ["Раз"]}).status_code == 409  # Nothing of the owner's is theirs
    assert names_of(other) == ["Общее"]


def test_the_model_answer_may_not_suggest_sensitive_or_general_names():
    _, prefs = service.clean_profile({"suggested_categories": ["Общее", "Лечение", "Религия", "Учёба", "x" * 50]})
    assert prefs["suggested_categories"] == ["Учёба"]


def test_a_dirty_suggestion_list_is_ignored_not_fatal():
    assert service.clean_profile({"suggested_categories": "Учёба"})[1]["suggested_categories"] == []
