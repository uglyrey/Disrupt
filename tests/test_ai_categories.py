import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app.models import Category
from app.providers import (
    DEMO_TEXT,
    SYSTEM_PROMPT,
    CloudRuProvider,
    MockProvider,
    mock_category,
    structure_context,
)
from app.services import MAX_AI_CATEGORIES, clean_ai_category_name
from app.worker import Worker
from tests.conftest import register
from tests.test_notes import submit


class Proposes(MockProvider):
    def __init__(self, name):
        self.name = name

    def structure(self, text, *, categories=()):
        return super().structure(text, categories=categories).model_copy(update={"category_name": self.name})


def process(app, client, provider, key="ai-category-1"):
    response = submit(client, key=key)
    assert response.status_code == 202, response.text
    assert Worker(app.state.sessions, app.state.settings, provider).run_once()
    job = client.get("/api/v1/jobs/" + response.json()["id"]).json()
    assert job["status"] == "succeeded", job
    return client.get("/api/v1/notes/" + job["note_id"]).json()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("работа", "Работа"),
        ("  Здоровье  ", "Здоровье"),
        ("идеи   для\tотпуска", "Идеи для отпуска"),
        ("Учёба", "Учёба"),
        ("A" * 40, "A" * 40),
        ("2024 год", "2024 год"),
    ],
)
def test_clean_ai_category_name_accepts_and_normalises(raw, expected):
    assert clean_ai_category_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "12345", "2024 - 2025", "A" * 41, "Работа\nДом", "«Работа»", 'Работа "срочно"', "it's", "https://x.ru",
     "site.ru", "www.example", None, 5],
)
def test_clean_ai_category_name_rejects_unusable_names(raw):
    assert clean_ai_category_name(raw) is None


def test_ai_category_is_normalised_and_matches_existing_ignoring_case_and_yo(app, client):
    register(client)
    existing = client.post("/api/v1/categories", json={"name": "Учёба"}).json()
    note = process(app, client, Proposes("учеба"))
    assert note["category_id"] == existing["id"]
    assert [c["name"] for c in client.get("/api/v1/categories").json()] == ["Общее", "Учёба"]
    note = process(app, client, Proposes("  финансы  "), key="ai-category-2")
    assert note["category_name"] == "Финансы"


def test_ai_category_rejects_bad_names_and_keeps_the_note(app, client):
    register(client)
    for index, name in enumerate(["12345", "«Работа»", "A" * 41, "https://example.com"]):
        note = process(app, client, Proposes(name), key=f"ai-bad-{index}")
        assert note["category_name"] == "Общее"  # A name that cannot be used leaves the note in «Общее»
    assert [c["name"] for c in client.get("/api/v1/categories").json()] == ["Общее"]


def test_ai_category_stops_creating_new_ones_at_fifteen_but_reuses(app, client):
    owner = register(client)["id"]
    with app.state.sessions.begin() as db:
        db.add_all([Category(user_id=owner, name=f"Тема {chr(1040 + i)}") for i in range(MAX_AI_CATEGORIES)])
    note = process(app, client, Proposes("Новая сфера"))
    assert note["category_name"] == "Общее"
    assert len(client.get("/api/v1/categories").json()) == MAX_AI_CATEGORIES + 1  # «Общее» is not counted
    reused = process(app, client, Proposes("тема б"), key="ai-category-2")
    assert reused["category_name"] == "Тема Б"
    assert len(client.get("/api/v1/categories").json()) == MAX_AI_CATEGORIES + 1


def test_ai_category_below_fifteen_creates_a_new_broad_category(app, client):
    owner = register(client)["id"]
    with app.state.sessions.begin() as db:
        db.add_all([Category(user_id=owner, name=f"Тема {chr(1040 + i)}") for i in range(MAX_AI_CATEGORIES - 1)])
    note = process(app, client, Proposes("путешествия"))
    assert note["category_name"] == "Путешествия"
    with app.state.sessions() as db:
        assert len(db.scalars(select(Category).where(Category.user_id == owner)).all()) == MAX_AI_CATEGORIES + 1


def test_ai_category_survives_deleting_the_category_during_the_model_call(app, client):
    register(client)
    category = client.post("/api/v1/categories", json={"name": "Работа"}).json()

    class Deleting(MockProvider):
        def structure(self, text, *, categories=()):
            assert client.delete(
                "/api/v1/categories/" + category["id"], params={"version": category["version"]}
            ).status_code == 204
            return super().structure(text, categories=categories)

    note = process(app, client, Deleting())
    assert note["category_name"] == "Общее"
    assert [c["name"] for c in client.get("/api/v1/categories").json()] == ["Общее"]


# Mock provider: simple keyword guess for the offline demo

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Завтра встреча с клиентом, нужно подготовить отчёт", "Работа"),
        ("Купить молоко и заказать продукты", "Покупки"),
        ("Записаться к врачу, сдать анализы", "Здоровье"),
        ("Позвонить маме и бабушке", "Семья"),
        ("Подготовиться к экзамену, прочитать лекцию", "Учёба"),
        ("Оплатить налог и проверить бюджет", "Финансы"),
        ("Просто мысль о погоде", None),
        ("", None),
    ],
)
def test_mock_category_guess_is_deterministic(text, expected):
    assert mock_category(text) == expected
    assert mock_category(text) == expected
    assert MockProvider().structure(text or "x").category_name == expected


def test_mock_category_reuses_the_users_spelling():
    assert mock_category("Нужно сдать экзамен", ("УЧЕБА", "Дом")) == "УЧЕБА"


def test_mock_demo_text_keeps_its_work_category():
    assert MockProvider().structure(DEMO_TEXT).category_name == "Работа"


# Prompt and date context

def test_prompt_has_the_new_rules():
    for fragment in [
        "task: действие, которое автор должен сделать",
        "idea: мысль, которую стоит обдумать",
        "Один item на одно конкретное действие",
        "не дроби",
        "не склеивай разные",
        "не длиннее 60 символов",
        "ничего не теряй",
        "Не выдумывай людей, даты, числа",
        "на языке оригинала",
        "existing_categories",
        "1-2 слова",
        "Спорт",
        "null только для пустого или непонятного текста",
        "точной непустой цитатой",
        "due_text содержит точную цитату срока",
    ]:
        assert fragment in SYSTEM_PROMPT, fragment
    assert len(SYSTEM_PROMPT) <= 5700  # 4037 before the note style rules (docs/note-style-v1.md)


def test_structure_context_is_json_data_with_local_date():
    moment = datetime(2026, 10, 11, 22, 30, tzinfo=ZoneInfo("UTC"))  # Already Monday in Moscow.
    data = json.loads(structure_context(("Работа", "Дом"), "Europe/Moscow", moment))
    assert data == {
        "today": "2026-10-12",
        "weekday": "понедельник",
        "now": "01:30",
        "timezone": "Europe/Moscow",
        "existing_categories": ["Работа", "Дом"],
    }
    assert json.loads(structure_context((), "No/Such_Zone", moment))["timezone"] == "UTC"


def test_cloudru_sends_date_context_as_data_before_the_text():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        content = {"title": "Мысль", "markdown": "## Мысль\n\nТекст", "conclusions": [], "items": []}
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}
        )

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(handler),
                               timezone="Asia/Yekaterinburg")
    provider.structure("Купить хлеб завтра", categories=("Покупки",))
    messages = bodies[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "user"]
    assert messages[0]["content"] == SYSTEM_PROMPT
    context = json.loads(messages[1]["content"])
    assert set(context) == {"today", "weekday", "now", "timezone", "existing_categories"}
    assert context["timezone"] == "Asia/Yekaterinburg" and context["existing_categories"] == ["Покупки"]
    datetime.strptime(context["today"], "%Y-%m-%d")
    assert messages[2] == {"role": "user", "content": json.dumps({"note_text": "Купить хлеб завтра"}, ensure_ascii=False)}
