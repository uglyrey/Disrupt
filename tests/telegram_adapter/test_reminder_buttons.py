"""Reminder message layout and its three buttons. Mock transports only, no real Telegram or core."""

import asyncio
import json

import httpx
import pytest

from telegram_adapter.clients import RemoteFailure, TelegramClient
from telegram_adapter.config import Settings
from tests.telegram_adapter.test_adapter import TOKEN, session

SECRET = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcde"  # 43 characters, like a real callback token.
URL = "https://example.test/?capture=c1&reminder=r1"
ORIGINAL = "Напоминание\n\nПозвонить маме\n\nИз заметки «План»"


@pytest.fixture
def settings(tmp_path):
    return Settings(TOKEN, "s" * 32, "http://core.test", "https://example.test", tmp_path / "offset.json")


def markup():
    return {"inline_keyboard": [
        [
            {"text": "Выполнено", "callback_data": "d:" + SECRET},
            {"text": "Через час", "callback_data": "h:" + SECRET},
            {"text": "Завтра", "callback_data": "t:" + SECRET},
        ],
        [{"text": "Открыть напоминание", "url": URL}],
    ]}


def press(data, update_id=30, message_id=77, **message):
    body = {"message_id": message_id, "chat": {"id": 101, "type": "private"}, "text": ORIGINAL,
            "reply_markup": markup(), **message}
    body = {k: v for k, v in body.items() if v is not None}
    return {"update_id": update_id, "callback_query": {
        "id": "cb-" + str(update_id), "from": {"id": 101, "is_bot": False}, "message": body, "data": data,
    }}


def drive(settings, update, core_reply, edit=None):
    """Run one update. Returns what reached the core and what reached Telegram, in order."""
    core_calls, log = [], []

    def core(request):
        core_calls.append(json.loads(request.content))
        return core_reply

    def telegram(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": [update]})
        log.append((method, body))
        if method == "editMessageText" and edit is not None:
            return edit
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    async def scenario():
        bot, c, t, _, _, _ = await session(settings, [update], core, telegram)
        async with c, t:
            await bot.poll_once()

    asyncio.run(scenario())
    return core_calls, log


def ok(**body):
    return httpx.Response(200, json=body)


def refused(code, status=409):
    return httpx.Response(status, json={"error": {"code": code, "message": "private"}, "operation_id": "o"})


# Layout

def sending(item):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9, "chat": {"id": item["chat_id"]}}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TelegramClient(client, TOKEN).send_reminder(item)

    result = asyncio.run(scenario())
    assert result["status"] == "sent"
    return sent[0]


def test_reminder_has_three_actions_and_a_link_to_the_reminder():
    sent = sending({"chat_id": 42, "text": ORIGINAL, "note_url": URL, "callback_token": SECRET})
    assert sent["text"] == ORIGINAL and "parse_mode" not in sent
    actions, link = sent["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in actions] == ["Выполнено", "Через час", "Завтра"]
    assert [b["callback_data"] for b in actions] == ["d:" + SECRET, "h:" + SECRET, "t:" + SECRET]
    assert all(len(b["callback_data"].encode()) <= 64 for b in actions)
    assert link == [{"text": "Открыть напоминание", "url": URL}]


def test_other_messages_keep_one_link_button():
    sent = sending({"chat_id": 42, "text": "Заметка готова.", "note_url": URL, "callback_token": None})
    assert sent["reply_markup"]["inline_keyboard"] == [[{"text": "Открыть Beresta", "url": URL}]]


# Callbacks

@pytest.mark.parametrize(("prefix", "action"), [("d:", "done"), ("h:", "snooze_hour"), ("t:", "snooze_tomorrow")])
def test_prefix_becomes_an_action_and_the_core_sees_only_the_secret(settings, prefix, action):
    core_calls, log = drive(settings, press(prefix + SECRET), ok(status="completed"))
    assert len(core_calls) == 1
    assert core_calls[0]["callback_token"] == SECRET and core_calls[0]["action"] == action
    assert core_calls[0]["telegram_user_id"] == 101 and "chat_id" not in core_calls[0]
    assert [method for method, _ in log] == ["answerCallbackQuery", "editMessageText"]


def test_old_bare_token_means_done(settings):
    core_calls, log = drive(settings, press("opaque-token-from-before"), ok(status="completed"))
    assert core_calls[0]["callback_token"] == "opaque-token-from-before" and core_calls[0]["action"] == "done"
    assert [method for method, _ in log] == ["answerCallbackQuery", "editMessageText"]


def test_done_edits_the_message_and_keeps_only_the_link(settings):
    _, log = drive(settings, press("d:" + SECRET), ok(status="completed"))
    answer, edit = log
    assert answer[1] == {"callback_query_id": "cb-30", "text": "Выполнено"}
    assert edit[1] == {
        "chat_id": 101, "message_id": 77, "text": ORIGINAL + "\n\nГотово, отметил выполненным",
        "link_preview_options": {"is_disabled": True},
        "reply_markup": {"inline_keyboard": [[{"text": "Открыть напоминание", "url": URL}]]},
    }
    assert "parse_mode" not in edit[1]


def test_second_press_says_it_is_done_already(settings):
    _, log = drive(settings, press("d:" + SECRET), ok(status="already_completed"))
    assert log[0][1]["text"] == "Уже выполнено"
    assert log[1][1]["text"].endswith("\n\nУже выполнено")


@pytest.mark.parametrize(("prefix", "line"), [
    ("h:", "Напомню через час, в 15:40"), ("t:", "Напомню завтра в 15:40"),
])
def test_snooze_tells_the_new_time(settings, prefix, line):
    _, log = drive(settings, press(prefix + SECRET), ok(status="snoozed", local_time="15:40"))
    assert log[0][1]["text"] == line
    assert log[1][1]["text"] == ORIGINAL + "\n\n" + line
    assert log[1][1]["reply_markup"]["inline_keyboard"] == [[{"text": "Открыть напоминание", "url": URL}]]


@pytest.mark.parametrize("failure", [
    httpx.Response(400, json={"ok": False, "error_code": 400, "description": "message can't be edited"}),
    httpx.Response(200, text="not json"),
    httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}),
])
def test_if_the_edit_fails_a_short_new_message_is_sent(settings, failure):
    _, log = drive(settings, press("t:" + SECRET), ok(status="snoozed", local_time="09:00"), edit=failure)
    assert [method for method, _ in log] == ["answerCallbackQuery", "editMessageText", "sendMessage"]
    sent = log[2][1]
    assert sent["chat_id"] == 101 and sent["text"] == "Напомню завтра в 09:00" and "parse_mode" not in sent


def test_a_message_without_an_id_gets_a_new_short_message(settings):
    _, log = drive(settings, press("h:" + SECRET, message_id=None), ok(status="snoozed", local_time="10:05"))
    assert [method for method, _ in log] == ["answerCallbackQuery", "sendMessage"]
    assert log[1][1]["text"] == "Напомню через час, в 10:05"


def test_a_long_original_is_replaced_by_the_outcome(settings):
    long_text = "я" * 4090
    _, log = drive(settings, press("d:" + SECRET, text=long_text), ok(status="completed"))
    assert log[1][1]["text"] == "Готово, отметил выполненным"


def test_a_fatal_telegram_error_on_edit_stops_the_bot(settings):
    fatal = httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
    with pytest.raises(RemoteFailure) as error:
        drive(settings, press("d:" + SECRET), ok(status="completed"), edit=fatal)
    assert error.value.fatal


@pytest.mark.parametrize("code", ["action_expired", "action_forbidden"])
def test_a_refusal_is_shown_as_an_alert_and_nothing_else_is_sent(settings, code):
    _, log = drive(settings, press("h:" + SECRET), refused(code, 409 if code == "action_expired" else 403))
    assert [method for method, _ in log] == ["answerCallbackQuery"]
    body = log[0][1]
    assert body["show_alert"] is True and body["callback_query_id"] == "cb-30"
    assert "напоминание" in body["text"].lower() or "аккаунт" in body["text"].lower()
    assert len(body["text"]) <= 200 and "private" not in json.dumps(body)


@pytest.mark.parametrize("reply", [
    ok(status="snoozed"), ok(status="snoozed", local_time="9:00"), ok(status="snoozed", local_time=900),
    ok(status="unknown"), ok(),
])
def test_an_invalid_core_answer_changes_nothing(settings, reply):
    with pytest.raises(RemoteFailure, match="core_invalid_response"):
        drive(settings, press("h:" + SECRET), reply)


def test_a_broken_prefixed_token_is_ignored(settings):
    core_calls, log = drive(settings, press("h:" + "x y"), ok(status="completed"))
    assert core_calls == [] and log == []


def test_login_and_link_buttons_still_go_their_own_way(settings):
    core_calls, log = drive(settings, press("link-no"), ok(status="completed"))
    assert core_calls == [] and [method for method, _ in log] == ["answerCallbackQuery", "sendMessage"]
