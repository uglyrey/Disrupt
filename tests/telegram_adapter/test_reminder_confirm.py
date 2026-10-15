"""«Верно?» under a reminder with a guessed time: the buttons, the wait for a written time, the answers.

Mock transports only. The core is a small fake that answers the two routes the bot uses. No real Telegram or core.
"""

import asyncio
import json

import httpx
import pytest

from telegram_adapter.bot import MANUAL_TTL, NOT_A_TIME, TIME_ASK, TIME_GONE, TIME_PENDING, TIME_UNCLEAR
from telegram_adapter.clients import CONFIRM_DATA, REJECTIONS, CoreClient, TelegramClient, confirm_rows
from telegram_adapter.delivery import DeliverySender
from tests.telegram_adapter.test_adapter import SAVED, TOKEN, message, session
from tests.telegram_adapter.test_adapter import settings as settings

RID = "11111111-2222-4333-8444-555555555555"
OTHER = "66666666-7777-4888-9999-000000000000"
URL = "https://example.test/?capture=c1"
TEXT = "Заметка готова.\n\nНапоминание поставлено: сегодня в 16:00, «позвонить маме». Верно?"


def entry(reminder_id=RID, generation=1, alt="В 04:00"):
    return {"reminder_id": reminder_id, "generation": generation, "alt_label": alt}


def press(data, update_id=40, message_id=7, entries=None, text=TEXT):
    rows = confirm_rows(entries if entries is not None else [entry()]) + [[{"text": "Открыть Beresta", "url": URL}]]
    return {"update_id": update_id, "callback_query": {
        "id": "cb-" + str(update_id), "from": {"id": 101, "is_bot": False},
        "message": {"message_id": message_id, "chat": {"id": 101, "type": "private"}, "text": text,
                    "reply_markup": {"inline_keyboard": rows}},
        "data": data,
    }}


class Core:
    """The two routes of the core as the bot sees them, with scripted answers."""

    def __init__(self, confirm=None, time=None):
        self.confirm, self.time = list(confirm or []), list(time or [])
        self.calls = []

    def paths(self):
        return [path for path, _ in self.calls]

    def bodies(self, path):
        return [body for p, body in self.calls if p == path]

    def __call__(self, request):
        path = request.url.path.removeprefix("/internal/v1")
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        if path == "/telegram/updates":
            return httpx.Response(200, json=SAVED)
        if path == "/telegram/actions":  # Unknown button data is taken for an old reminder token.
            return httpx.Response(409, json={"error": {"code": "action_expired", "message": "x"}, "operation_id": "o"})
        queue = self.confirm if path == "/telegram/reminder-confirm" else self.time
        answer = queue.pop(0)
        if isinstance(answer, str):
            return httpx.Response(409, json={"error": {"code": answer, "message": "private"}, "operation_id": "o"})
        return httpx.Response(200, json=answer)


def run(settings, updates, core, clock=None):
    async def scenario():
        bot, c, t, _, replies, _ = await session(settings, updates, core)
        if clock is not None:
            bot.clock = clock
        async with c, t:
            await bot.poll_once()
            return bot, replies

    return asyncio.run(scenario())


def sent(replies):
    return [r for r in replies if "text" in r and "callback_query_id" not in r]


# The buttons under the message

def test_one_pair_and_the_other_reading_in_the_message():
    rows = confirm_rows([entry()])
    assert [[b["text"] for b in row] for row in rows] == [["Верно", "Изменить время"], ["В 04:00"]]
    assert [b["callback_data"] for row in rows for b in row] == [f"rv:{RID}:1", f"re:{RID}:1", f"ra:{RID}:1"]
    assert all(len(b["callback_data"].encode()) <= 64 for row in rows for b in row)
    assert all(CONFIRM_DATA.fullmatch(b["callback_data"]) for row in rows for b in row)


def test_without_another_reading_there_is_no_third_button_and_several_pairs_are_numbered():
    rows = confirm_rows([entry(alt=None), entry(OTHER, 3, alt=None)])
    assert [[b["text"] for b in row] for row in rows] == [
        ["Верно 1", "Изменить время 1"], ["Верно 2", "Изменить время 2"]]


def test_an_edit_only_entry_draws_just_the_edit_button():
    rows = confirm_rows([{**entry(alt=None), "edit_only": True}])
    assert [[b["text"] for b in row] for row in rows] == [["Изменить время"]]
    assert [b["callback_data"] for row in rows for b in row] == [f"re:{RID}:1"]
    assert all(CONFIRM_DATA.fullmatch(b["callback_data"]) for row in rows for b in row)


def sending(item):
    posted = []

    def handler(request):
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9, "chat": {"id": item["chat_id"]}}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TelegramClient(client, TOKEN).send_reminder(item)

    assert asyncio.run(scenario())["status"] == "sent"
    return posted[0]


def test_the_delivery_draws_the_pairs_above_the_link():
    sent_body = sending({"chat_id": 42, "text": TEXT, "note_url": URL, "callback_token": None, "confirm": [entry()]})
    rows = sent_body["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["Верно", "Изменить время"] and rows[1][0]["text"] == "В 04:00"
    assert rows[-1] == [{"text": "Открыть Beresta", "url": URL}]
    plain = sending({"chat_id": 42, "text": "x", "note_url": URL, "callback_token": None, "confirm": []})
    assert plain["reply_markup"]["inline_keyboard"] == [[{"text": "Открыть Beresta", "url": URL}]]
    old_core = sending({"chat_id": 42, "text": "x", "note_url": URL, "callback_token": None})
    assert old_core["reply_markup"]["inline_keyboard"] == [[{"text": "Открыть Beresta", "url": URL}]]


@pytest.mark.parametrize("confirm", [
    "x", [entry()] * 4, [{"reminder_id": "not-a-uuid", "generation": 1}], [entry(generation=0)],
    [entry(generation="1")], [{**entry(), "edit_only": "yes"}], [entry(alt="a" * 41)], [{**entry(), "extra": 1}], [entry(alt="a\nb")], ["x"],
])
def test_a_malformed_confirm_list_stops_the_sender(settings, confirm):
    from telegram_adapter.clients import RemoteFailure

    sender = DeliverySender(settings, CoreClient(None, settings), None, None)
    item = {"delivery_id": RID, "generation": 1, "lease_token": "a" * 43, "chat_id": 101, "text": "x",
            "callback_token": None, "note_url": "https://example.test/", "confirm": confirm}
    with pytest.raises(RemoteFailure):
        sender.validate_claim(item)
    sender.validate_claim({**item, "confirm": [entry()]})
    sender.validate_claim({**item, "confirm": [{**entry(alt=None), "edit_only": True}]})
    sender.validate_claim({k: v for k, v in item.items() if k != "confirm"})


# «Верно»

def test_verno_confirms_and_edits_the_message_with_a_tick_and_no_buttons(settings):
    core = Core(confirm=[{"status": "confirmed", "label": "сегодня в 16:00"}])
    _, replies = run(settings, [press(f"rv:{RID}:1")], core)
    assert core.calls[0][0] == "/telegram/reminder-confirm"
    body = core.calls[0][1]
    assert (body["reminder_id"], body["generation"], body["action"]) == (RID, 1, "ok")
    assert body["telegram_user_id"] == 101 and body["update_id"] == 40
    answer, edit = replies
    assert answer["text"] == "Верно"
    assert edit["text"] == TEXT + " ✓" and edit["message_id"] == 7
    assert edit["reply_markup"] == {"inline_keyboard": [[{"text": "Открыть Beresta", "url": URL}]]}
    assert "parse_mode" not in edit


def test_a_stale_button_says_so_and_changes_the_message_not(settings):
    core = Core(confirm=["action_expired"])
    _, replies = run(settings, [press(f"rv:{RID}:1")], core)
    (answer,) = replies
    assert answer["text"] == REJECTIONS["action_expired"] and answer["show_alert"] is True


def test_two_pairs_only_the_pressed_one_goes_away(settings):
    entries = [entry(alt=None), entry(OTHER, 2, alt=None)]
    core = Core(confirm=[{"status": "confirmed", "label": "x"}])
    _, replies = run(settings, [press(f"rv:{OTHER}:2", entries=entries)], core)
    edit = replies[1]
    assert edit["text"] == TEXT + "\n✓ 2"
    left = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert left == [f"rv:{RID}:1", f"re:{RID}:1"]


def test_the_other_reading_button_reports_the_new_time(settings):
    core = Core(confirm=[{"status": "corrected", "label": "сегодня в 04:00"}])
    _, replies = run(settings, [press(f"ra:{RID}:1")], core)
    assert core.bodies("/telegram/reminder-confirm")[0]["action"] == "alt"
    assert replies[0]["text"] == "Готово, напомню сегодня в 04:00."
    assert replies[1]["text"] == TEXT + "\n\nГотово, напомню сегодня в 04:00."
    assert replies[1]["reply_markup"] == {"inline_keyboard": [[{"text": "Открыть Beresta", "url": URL}]]}


def test_a_time_that_passed_is_refused_with_its_own_text(settings):
    core = Core(confirm=["time_passed"])
    _, replies = run(settings, [press(f"ra:{RID}:1")], core)
    assert replies[0]["text"] == REJECTIONS["time_passed"] and "новое" in REJECTIONS["time_passed"]


@pytest.mark.parametrize("data", ["rv:not-an-id:1", f"rv:{RID}:0", f"rx:{RID}:1", f"rv:{RID}:1:2", f"rv:{RID}:01"])
def test_garbage_button_data_never_reaches_the_confirm_route(settings, data):
    core = Core()
    run(settings, [press(data)], core)
    assert "/telegram/reminder-confirm" not in core.paths()


# «Изменить время» and the written answer

def test_edit_asks_for_a_time_and_the_next_text_is_the_time_not_a_note(settings):
    core = Core(confirm=[{"status": "ask_time", "label": "сегодня в 16:00"}],
                time=[{"status": "set", "label": "завтра в 09:30"}])
    _, replies = run(settings, [press(f"re:{RID}:1"), message(41, "завтра в 9:30")], core)
    assert core.paths() == ["/telegram/reminder-confirm", "/telegram/reminder-time"]
    body = core.bodies("/telegram/reminder-time")[0]
    assert (body["reminder_id"], body["generation"], body["text"], body["update_id"]) == (RID, 1, "завтра в 9:30", 41)
    said = [r["text"] for r in sent(replies)]
    assert said == [TIME_ASK, "Готово, напомню завтра в 09:30."]
    assert TIME_ASK == "Напишите, когда напомнить. Например «сегодня в 16» или «завтра в 9:30»."
    markup_edit = [r for r in replies if "reply_markup" in r and "text" not in r]
    assert markup_edit and markup_edit[0]["message_id"] == 7
    assert markup_edit[0]["reply_markup"] == {"inline_keyboard": [[{"text": "Открыть Beresta", "url": URL}]]}


def test_a_pending_answer_says_one_moment_ends_the_wait_and_takes_the_old_buttons_away(settings):
    core = Core(confirm=[{"status": "ask_time", "label": "x"}], time=[{"status": "pending"}])
    bot, replies = run(settings, [press(f"re:{RID}:1"), message(41, "после обеда часа в три")], core)
    assert [r["text"] for r in sent(replies)] == [TIME_ASK, TIME_PENDING]
    assert TIME_PENDING == "Секунду, разбираю время…"
    assert bot.awaiting_time == {}  # The worker answers later through an ordinary delivery.
    assert "/telegram/updates" not in core.paths()
    edited = [r for r in replies if "reply_markup" in r and "text" not in r]
    assert edited and edited[0]["reply_markup"] == {"inline_keyboard": [[{"text": "Открыть Beresta", "url": URL}]]}


def test_an_unclear_answer_keeps_waiting_and_the_next_one_works(settings):
    core = Core(confirm=[{"status": "ask_time", "label": "x"}],
                time=[{"status": "unclear"}, {"status": "set", "label": "сегодня в 18:00"}])
    _, replies = run(settings, [press(f"re:{RID}:1"), message(41, "после дождичка"), message(42, "в 18")], core)
    assert [r["text"] for r in sent(replies)] == [TIME_ASK, TIME_UNCLEAR, "Готово, напомню сегодня в 18:00."]
    assert TIME_UNCLEAR == "Не понял время. Напишите, например «сегодня в 16»."
    assert "/telegram/updates" not in core.paths()


def test_an_unsure_answer_is_set_and_asked_again_with_buttons(settings):
    core = Core(confirm=[{"status": "ask_time", "label": "x"}], time=[
        {"status": "ask", "label": "завтра в 16:00", "reminder_id": RID, "generation": 2, "alt_label": "В 04:00"}])
    _, replies = run(settings, [press(f"re:{RID}:1"), message(41, "в четыре")], core)
    asked = sent(replies)[1]
    assert asked["text"] == "Поставил на завтра в 16:00. Верно?"
    data = [b["callback_data"] for row in asked["reply_markup"]["inline_keyboard"] for b in row]
    assert data == [f"rv:{RID}:2", f"re:{RID}:2", f"ra:{RID}:2"]


def test_a_text_that_is_not_a_time_is_saved_as_a_note_once_with_one_line(settings):
    core = Core(confirm=[{"status": "ask_time", "label": "x"}], time=[{"status": "not_time"}])
    _, replies = run(settings, [press(f"re:{RID}:1"), message(41, "Купить молоко"), message(42, "Купить хлеб")], core)
    assert core.paths() == [
        "/telegram/reminder-confirm", "/telegram/reminder-time", "/telegram/updates", "/telegram/updates"]
    said = [r["text"] for r in sent(replies)]
    assert said.count(NOT_A_TIME) == 1 and said[0] == TIME_ASK
    assert NOT_A_TIME == "Это не похоже на время, поэтому сохранил как запись. Время можно поменять кнопкой «Изменить время»."
    assert core.bodies("/telegram/updates")[0]["text"] == "Купить молоко"
    assert said[-1] != NOT_A_TIME  # The second note is an ordinary note again.


def test_a_long_text_is_a_note_and_never_goes_to_the_time_route(settings):
    core = Core(confirm=[{"status": "ask_time", "label": "x"}])
    _, replies = run(settings, [press(f"re:{RID}:1"), message(41, "Длинная мысль " * 20)], core)
    assert core.paths() == ["/telegram/reminder-confirm", "/telegram/updates"]
    assert NOT_A_TIME in [r["text"] for r in sent(replies)]


def test_the_wait_ends_after_ten_minutes(settings):
    core = Core(confirm=[{"status": "ask_time", "label": "x"}])
    now = [1000.0]

    async def scenario():
        bot, c, t, _, _, _ = await session(settings, [], core)
        bot.clock = lambda: now[0]
        async with c, t:
            await bot.handle(press(f"re:{RID}:1"))
            assert 101 in bot.awaiting_time
            now[0] += MANUAL_TTL + 1
            await bot.handle(message(41, "завтра в 10"))

    asyncio.run(scenario())
    assert core.paths() == ["/telegram/reminder-confirm", "/telegram/updates"]  # Too late: an ordinary note.


def test_a_gone_reminder_turns_the_answer_into_a_note_with_a_line(settings):
    core = Core(confirm=[{"status": "ask_time", "label": "x"}], time=["action_expired"])
    _, replies = run(settings, [press(f"re:{RID}:1"), message(41, "завтра в 10")], core)
    assert core.paths() == ["/telegram/reminder-confirm", "/telegram/reminder-time", "/telegram/updates"]
    assert TIME_GONE in [r["text"] for r in sent(replies)]


def test_a_command_or_menu_button_during_the_wait_is_not_taken_for_a_time(settings):
    core = Core(confirm=[{"status": "ask_time", "label": "x"}])
    _, replies = run(settings, [press(f"re:{RID}:1"), message(41, "/help"), message(42, "🏠 Меню")], core)
    assert "/telegram/reminder-time" not in core.paths()


def test_a_bad_core_answer_is_refused_not_shown(settings):
    from telegram_adapter.clients import RemoteFailure

    core = Core(confirm=[{"status": "ask_time", "label": "x"}], time=[{"status": "set", "label": "\x00bad"}])

    async def scenario():
        bot, c, t, _, _, _ = await session(settings, [press(f"re:{RID}:1"), message(41, "завтра в 10")], core)
        async with c, t:
            await bot.poll_once()

    with pytest.raises(RemoteFailure):
        asyncio.run(scenario())
