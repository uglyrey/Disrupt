"""Настоящий бот из этого checkout против настоящего API в одном процессе. Telegram подменён, сети нет."""

import asyncio

import httpx
from sqlalchemy import select

from app.models import Reminder, User
from telegram_adapter.bot import MAIN_MENU, ZONE_ASK, Bot
from telegram_adapter.clients import CoreClient
from telegram_adapter.config import Settings
from telegram_adapter.state import OffsetStore
from tests.test_internal import BASE, SERVICE_TOKEN
from tests.test_reminders import reminders as reminders
from tests.test_timezone import guessed_reminder


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def reply(self, chat_id, text, url=None, **extra):
        self.sent.append({"text": text, **extra})

    async def call(self, method, payload):
        return True


def update(update_id, **fields):
    return {"update_id": update_id, "message": {
        "from": {"id": BASE["telegram_user_id"], "is_bot": False},
        "chat": {"type": "private", "id": BASE["chat_id"]}, **fields,
    }}


def press(update_id, data):
    return {"update_id": update_id, "callback_query": {
        "id": "cb" + str(update_id), "from": {"id": BASE["telegram_user_id"], "is_bot": False}, "data": data,
        "message": {"chat": {"type": "private", "id": BASE["chat_id"]}, "message_id": 5},
    }}


def drive(app, tmp_path, updates):
    config = Settings(
        str(BASE["bot_id"]) + ":synthetic-test-only-token", SERVICE_TOKEN, "http://127.0.0.1:8000",
        "http://127.0.0.1:8000", tmp_path / "offset.json",
    )
    telegram = FakeTelegram()

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            bot = Bot(config, CoreClient(client, config), telegram, OffsetStore(config.state_file, config.bot_id))
            for item in updates:
                await bot.handle(item)

    asyncio.run(run())
    return telegram.sent


def account(app, ids):
    with app.state.sessions() as db:
        user = db.get(User, ids["user"])
        return user.timezone, user.timezone_source, user.timezone_asked_at


def test_real_bot_asks_once_then_parses_an_offset_and_moves_guessed_reminders(reminders, tmp_path):
    app, client, ids = reminders
    rid = guessed_reminder(app, ids)
    with app.state.sessions() as db:
        assert db.get(User, ids["user"]).timezone_asked_at is not None  # The fixture's own note was the first one.
        db.get(User, ids["user"]).timezone_asked_at = None
        db.commit()
    sent = drive(app, tmp_path, [
        update(10, text="Первая заметка"), update(11, text="Вторая заметка"),
        press(12, "tz:other"), update(13, text="UTC+7"),
    ])
    texts = [item["text"] for item in sent]
    assert texts.count(ZONE_ASK) == 1  # Once, with the first note.
    assert texts[1] == ZONE_ASK and len(sent[1]["reply_markup"]["inline_keyboard"]) == 6
    assert texts[-1] == (
        "Часовой пояс: Новосибирск (UTC+7). Напоминания будут приходить по этому времени.\n"
        "Перенёс 1 напоминание на это время."
    )
    assert sent[-1]["reply_markup"] == MAIN_MENU
    assert account(app, ids)[:2] == ("Asia/Novosibirsk", "telegram") and account(app, ids)[2] is not None
    with app.state.sessions() as db:
        row = db.get(Reminder, rid)
        assert row.timezone == "Asia/Novosibirsk" and row.zone_guessed is False and row.generation == 2


def test_real_bot_saves_a_non_city_as_a_note_and_sets_a_known_city(reminders, tmp_path):
    app, client, ids = reminders
    sent = drive(app, tmp_path, [press(10, "tz:other"), update(11, text="Купить хлеб")])
    assert sent[2]["text"].startswith("Это не похоже на город, поэтому сохранил как запись.")
    assert account(app, ids)[0] is None
    sent = drive(app, tmp_path, [press(12, "tz:other"), update(13, text="Омск")])
    assert sent[1]["text"].startswith("Часовой пояс: Омск (UTC+6).")
    assert account(app, ids)[0] == "Asia/Omsk"


def test_real_bot_location_flow_with_confirmation(reminders, tmp_path):
    app, client, ids = reminders
    sent = drive(app, tmp_path, [update(10, location={"latitude": 55.03, "longitude": 82.92})])
    assert sent[0]["text"].startswith("Похоже, вы рядом с городом Новосибирск.")
    assert account(app, ids)[0] is None
    sent = drive(app, tmp_path, [press(11, "tzy:Asia/Novosibirsk")])
    assert sent[0]["text"].startswith("Часовой пояс: Новосибирск (UTC+7).")
    assert account(app, ids)[:2] == ("Asia/Novosibirsk", "telegram")


def test_real_bot_timezone_command_and_replayed_update(reminders, tmp_path):
    app, client, ids = reminders
    sent = drive(app, tmp_path, [update(10, text="/timezone"), update(11, text="/timezone Томск"), update(11, text="/timezone Томск")])
    assert "Часовой пояс не выбран" in sent[0]["text"]
    assert sent[1]["text"] == sent[2]["text"] and "Томск (UTC+7)" in sent[1]["text"]
    assert account(app, ids)[0] == "Asia/Tomsk"
    with app.state.sessions() as db:
        assert db.scalars(select(Reminder)).all() == []


def test_the_question_is_not_asked_again_after_the_web_set_a_zone(reminders, tmp_path):
    app, client, ids = reminders
    assert client.patch("/api/v1/account/timezone", json={"timezone": "Asia/Omsk", "source": "auto"}).status_code == 200
    sent = drive(app, tmp_path, [update(10, text="Заметка")])
    assert [item["text"] for item in sent if item["text"] == ZONE_ASK] == []
