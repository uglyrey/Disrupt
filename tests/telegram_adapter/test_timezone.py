"""Часовой пояс в боте: один вопрос, кнопки, «Другой», /timezone, местоположение. Транспорты подменены, сети нет."""

import asyncio
import json
import logging

import httpx
import pytest

from telegram_adapter.bot import (
    BOT_COMMANDS,
    LOCATION_KEYBOARD,
    MAIN_MENU,
    ZONE_ASK,
    ZONE_LOCATION_MISS,
    ZONE_NOT_FOUND,
    ZONE_OTHER_TEXT,
    ZONE_PRESETS,
    plural,
)
from telegram_adapter.clients import REJECTIONS
from tests.telegram_adapter.test_adapter import SAVED, callback, message, session, texts
from tests.telegram_adapter.test_adapter import settings as settings

ZONES = {
    "Asia/Novosibirsk": ("Новосибирск", "UTC+7"), "Asia/Tomsk": ("Томск", "UTC+7"),
    "Europe/Moscow": ("Москва", "UTC+3"), "Asia/Yekaterinburg": ("Екатеринбург", "UTC+5"),
}
TYPED = {"томск": "Asia/Tomsk", "новосибирск": "Asia/Novosibirsk", "+5": "Asia/Yekaterinburg", "москва": "Europe/Moscow"}


class FakeCore:
    """The zone routes of the core as the bot sees them. It keeps its own small state."""

    def __init__(self, *, zone=None, asked=False, moved=0, linked=True):
        self.zone, self.asked, self.moved, self.linked = zone, asked, moved, linked
        self.calls = []

    def paths(self):
        return [(method, path) for method, path, _ in self.calls]

    def bodies(self, path):
        return [body for _, p, body in self.calls if p == path]

    def state(self):
        label, utc = ZONES.get(self.zone, (None, None))
        return {"timezone": self.zone, "source": "telegram" if self.zone else None, "label": label, "utc": utc,
                "asked": self.asked}

    def __call__(self, request):
        path = request.url.path.removeprefix("/internal/v1")
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        if path == "/telegram/updates":
            ask = self.zone is None and not self.asked
            self.asked = True
            return httpx.Response(200, json={**SAVED, "ask_timezone": ask})
        if not self.linked:
            if request.method == "GET":
                return httpx.Response(404, json={"detail": "x"})
            return httpx.Response(403, json={"error": {"code": "telegram_not_linked", "message": "x"}})
        if path == "/telegram/timezone" and request.method == "GET":
            return httpx.Response(200, json=self.state())
        if path == "/telegram/timezone":
            self.zone, self.asked = body["timezone"], True
            return httpx.Response(200, json={**self.state(), "moved": self.moved})
        if path == "/telegram/timezone-asked":
            first, self.asked = not self.asked, True
            return httpx.Response(200, json={"first": first})
        if path == "/telegram/timezone-find":
            if "text" in body:
                zone = TYPED.get(body["text"].lower())
                if zone is None:
                    return httpx.Response(200, json={"found": False})
                return httpx.Response(200, json={
                    "found": True, "timezone": zone, "label": ZONES[zone][0], "utc": ZONES[zone][1], "city": None,
                })
            if abs(body["latitude"] - 55.03) < 1 and abs(body["longitude"] - 82.92) < 1:
                return httpx.Response(200, json={
                    "found": True, "timezone": "Asia/Novosibirsk", "label": "Новосибирск", "utc": "UTC+7",
                    "city": "Новосибирск",
                })
            return httpx.Response(200, json={"found": False})
        return httpx.Response(404, json={"detail": "x"})


def located(update_id, lat, lon, chat_type="private"):
    return {"update_id": update_id, "message": {
        "from": {"id": 101, "is_bot": False}, "chat": {"id": 101 if chat_type == "private" else -5, "type": chat_type},
        "location": {"latitude": lat, "longitude": lon},
    }}


def run(settings, updates, core):
    async def scenario():
        bot, c, t, _, replies, _ = await session(settings, updates, core)
        async with c, t:
            await bot.poll_once()
            return bot, replies

    return asyncio.run(scenario())


def buttons(reply):
    return [button for row in reply["reply_markup"]["inline_keyboard"] for button in row]


# Один вопрос

def test_the_first_processed_note_asks_for_the_zone_once(settings):
    core = FakeCore()
    _, replies = run(settings, [message(10, "Первая мысль"), message(11, "Вторая мысль")], core)
    assert [r["text"] for r in texts(replies)] == [
        "Запись сохранена. Результат и статус обработки доступны в Beresta.", ZONE_ASK,
        "Запись сохранена. Результат и статус обработки доступны в Beresta.",
    ]
    assert ZONE_ASK == "Чтобы напоминания приходили вовремя, выберите ваш часовой пояс."
    # Not a single extra request is made for the question: the core says it in the answer to the note.
    assert core.paths() == [("POST", "/telegram/updates")] * 2


def test_question_has_ten_city_buttons_two_per_row_and_other(settings):
    _, replies = run(settings, [message(10, "Первая")], FakeCore())
    ask = texts(replies)[1]
    rows = ask["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 2, 2, 2, 2, 1]
    assert rows[-1] == [{"text": "Другой", "callback_data": "tz:other"}]
    labels = [b["text"] for row in rows[:-1] for b in row]
    assert labels == [label for label, _ in ZONE_PRESETS] and 8 <= len(labels) <= 10
    for label in ("Калининград", "Москва", "Самара", "Екатеринбург", "Омск", "Новосибирск", "Красноярск", "Иркутск",
                  "Владивосток"):
        assert label in labels
    for button in buttons(ask):
        assert len(button["callback_data"].encode()) <= 64
    assert {b["callback_data"] for b in buttons(ask)[:-1]} == {"tz:" + zone for _, zone in ZONE_PRESETS}


def test_no_question_when_the_core_does_not_ask(settings):
    _, replies = run(settings, [message(10, "Первая")], FakeCore(zone="Europe/Moscow"))
    assert len(texts(replies)) == 1


# Выбор

def test_choosing_a_city_sets_the_zone_and_confirms(settings):
    core = FakeCore(asked=True)
    _, replies = run(settings, [callback(20, "tz:Asia/Novosibirsk")], core)
    assert core.bodies("/telegram/timezone")[0]["timezone"] == "Asia/Novosibirsk"
    (reply,) = texts(replies)
    assert reply["text"] == "Часовой пояс: Новосибирск (UTC+7). Напоминания будут приходить по этому времени."
    assert reply["reply_markup"] == MAIN_MENU


@pytest.mark.parametrize("moved,word", [(1, "напоминание"), (2, "напоминания"), (5, "напоминаний"), (11, "напоминаний"),
                                        (21, "напоминание"), (24, "напоминания")])
def test_moved_reminders_are_reported(settings, moved, word):
    core = FakeCore(moved=moved)
    _, replies = run(settings, [callback(20, "tz:Asia/Novosibirsk")], core)
    assert texts(replies)[0]["text"].endswith(f"\nПеренёс {moved} {word} на это время.")


def test_no_moved_line_when_nothing_moved(settings):
    _, replies = run(settings, [callback(20, "tz:Europe/Moscow")], FakeCore(moved=0))
    assert "Перенёс" not in texts(replies)[0]["text"]


def test_plural_forms():
    got = [plural(n, "а", "б", "в") for n in (1, 2, 5, 11, 12, 21, 22, 25, 101, 111)]
    assert got == ["а", "б", "в", "в", "в", "а", "б", "в", "а", "в"]


def test_a_bad_button_is_refused_without_a_request(settings):
    core = FakeCore()
    _, replies = run(settings, [callback(20, "tz:../../etc"), callback(21, "tz:" + "x" * 80)], core)
    assert core.calls == []
    assert [r["text"] for r in texts(replies)] == [REJECTIONS["invalid_timezone"]]


# «Другой»

def test_other_explains_location_button_and_typing_then_a_city_sets_the_zone(settings):
    core = FakeCore(asked=True)
    _, replies = run(settings, [callback(20, "tz:other"), message(21, "Томск")], core)
    first, second = texts(replies)
    assert first["text"] == ZONE_OTHER_TEXT
    assert "местоположение" in first["text"] and "+5" in first["text"] and "не сохраняю" in first["text"]
    assert first["reply_markup"] == LOCATION_KEYBOARD
    assert LOCATION_KEYBOARD["keyboard"][0][0]["request_location"] is True
    assert LOCATION_KEYBOARD["one_time_keyboard"] is True
    assert core.bodies("/telegram/timezone-find")[0]["text"] == "Томск"
    assert "/telegram/updates" not in [p for _, p in core.paths()]  # A city is not saved as a note.
    assert second["text"] == "Часовой пояс: Томск (UTC+7). Напоминания будут приходить по этому времени."
    assert second["reply_markup"] == MAIN_MENU  # The temporary keyboard is replaced.


def test_offsets_are_passed_to_the_core_as_typed(settings):
    core = FakeCore()
    _, replies = run(settings, [callback(20, "tz:other"), message(21, "+5")], core)
    assert core.bodies("/telegram/timezone-find")[0]["text"] == "+5"
    assert core.zone == "Asia/Yekaterinburg"
    assert "Екатеринбург (UTC+5)" in texts(replies)[1]["text"]


def test_unknown_city_gets_the_hint_and_keeps_waiting(settings):
    core = FakeCore()
    _, replies = run(settings, [callback(20, "tz:other"), message(21, "Нигде"), message(22, "Томск")], core)
    texts_ = [r["text"] for r in texts(replies)]
    assert texts_[1] == ZONE_NOT_FOUND == "Не нашёл такой город. Напишите крупный город рядом."
    assert core.zone == "Asia/Tomsk"
    assert "/telegram/updates" not in [p for _, p in core.paths()]


def test_a_short_text_is_a_note_when_nobody_asked_for_a_city(settings):
    core = FakeCore(asked=True)
    run(settings, [message(10, "Томск")], core)
    assert core.paths() == [("POST", "/telegram/updates")]


def test_a_long_text_while_waiting_is_a_note(settings):
    core = FakeCore(asked=True)
    long = "Надо не забыть позвонить в банк и узнать про карту, а ещё купить билеты"
    run(settings, [callback(20, "tz:other"), message(21, long), message(22, "Томск")], core)
    assert core.paths() == [("POST", "/telegram/updates"), ("POST", "/telegram/updates")]  # The waiting was cancelled.


def test_waiting_ends_after_the_time_limit(settings):
    async def scenario():
        core = FakeCore(asked=True)
        bot, c, t, _, replies, _ = await session(settings, [callback(20, "tz:other")], core)
        async with c, t:
            now = [1000.0]
            bot.clock = lambda: now[0]
            await bot.poll_once()
            now[0] += 700
            await bot.handle(message(21, "Томск"))
        return core

    core = asyncio.run(scenario())
    assert core.paths() == [("POST", "/telegram/updates")]


# /timezone

def test_timezone_command_shows_the_zone_and_the_buttons_any_time(settings):
    core = FakeCore(zone="Asia/Novosibirsk", asked=True)
    _, replies = run(settings, [message(10, "/timezone"), message(11, "часовой пояс"), message(12, "Часовой пояс")], core)
    assert len(texts(replies)) == 3
    for reply in texts(replies):
        assert reply["text"].startswith("Ваш часовой пояс: Новосибирск (UTC+7).")
        assert len(buttons(reply)) == 11
    assert "/telegram/updates" not in [p for _, p in core.paths()]


def test_timezone_command_without_a_zone_says_moscow_and_counts_as_the_question(settings):
    core = FakeCore()
    _, replies = run(settings, [message(10, "/timezone")], core)
    assert "напоминания идут по Москве" in texts(replies)[0]["text"] and ZONE_ASK in texts(replies)[0]["text"]
    assert core.asked is True and ("POST", "/telegram/timezone-asked") in core.paths()


def test_timezone_command_with_a_city_sets_it(settings):
    core = FakeCore()
    _, replies = run(settings, [message(10, "/timezone Томск")], core)
    assert core.zone == "Asia/Tomsk" and "Томск (UTC+7)" in texts(replies)[0]["text"]


def test_timezone_command_for_an_unlinked_telegram(settings):
    _, replies = run(settings, [message(10, "/timezone")], FakeCore(linked=False))
    assert texts(replies)[0]["text"] == REJECTIONS["telegram_not_linked"]


def test_command_is_in_the_bot_command_list():
    assert {"command": "timezone", "description": "Выбрать часовой пояс"} in BOT_COMMANDS


# Местоположение

def test_location_after_the_question_sets_the_zone_and_drops_the_keyboard(settings):
    core = FakeCore(asked=True)
    _, replies = run(settings, [callback(20, "tz:other"), located(21, 55.03, 82.92)], core)
    last = texts(replies)[-1]
    assert last["text"].startswith("Часовой пояс: Новосибирск (UTC+7).")
    assert last["reply_markup"] == MAIN_MENU and "keyboard" in last["reply_markup"]
    assert core.zone == "Asia/Novosibirsk"


def test_location_from_a_user_who_did_not_ask_needs_a_confirmation(settings):
    core = FakeCore(asked=True)
    _, replies = run(settings, [located(20, 55.03, 82.92)], core)
    (reply,) = texts(replies)
    assert reply["text"] == "Похоже, вы рядом с городом Новосибирск. Сохранить часовой пояс Новосибирск (UTC+7)?"
    first, second = buttons(reply)
    assert first == {"text": "Да, сохранить", "callback_data": "tzy:Asia/Novosibirsk"}
    assert second["callback_data"] == "tz:no"
    assert core.zone is None and "/telegram/timezone" not in [p for _, p in core.paths()]
    _, replies = run(settings, [callback(21, "tzy:Asia/Novosibirsk")], core)
    assert core.zone == "Asia/Novosibirsk" and "Часовой пояс: Новосибирск" in texts(replies)[0]["text"]


def test_declining_the_confirmation_changes_nothing(settings):
    core = FakeCore(zone="Europe/Moscow", asked=True)
    _, replies = run(settings, [callback(20, "tz:no")], core)
    assert core.zone == "Europe/Moscow" and "не меняю" in texts(replies)[0]["text"]
    assert core.calls == []


def test_location_with_no_known_place_gives_a_hint(settings):
    core = FakeCore(asked=True)
    _, replies = run(settings, [located(20, 0.0, -150.0)], core)
    assert texts(replies)[0]["text"] == ZONE_LOCATION_MISS and core.zone is None


def test_location_in_a_group_chat_is_ignored(settings):
    core = FakeCore()
    _, replies = run(settings, [located(20, 55.03, 82.92, chat_type="group")], core)
    assert core.calls == [] and replies == []


def test_coordinates_are_sent_once_and_never_logged_or_kept(settings, caplog):
    caplog.set_level(logging.DEBUG)
    core = FakeCore(asked=True)
    bot, replies = run(settings, [callback(20, "tz:other"), located(21, 55.030199, 82.920430)], core)
    assert "55.030199" not in caplog.text and "82.920430" not in caplog.text
    assert "55.030199" not in json.dumps(replies) and "82.920430" not in json.dumps(replies)
    sent = [body for body in core.bodies("/telegram/timezone-find") if "latitude" in body]
    assert len(sent) == 1
    assert "55.030199" not in repr(vars(bot))
    # The coordinates are not part of the zone request that stores anything.
    assert all("latitude" not in body for body in core.bodies("/telegram/timezone"))
