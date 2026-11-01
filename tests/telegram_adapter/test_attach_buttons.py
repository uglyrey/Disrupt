"""«Добавить к «X»», «Отдельная заметка» и «Отменить» в Telegram. Только подменённые транспорты (docs/attach-to-note-v1.md)."""

import asyncio
import json

import httpx
import pytest

from telegram_adapter.clients import ATTACH_DATA, REJECTIONS, CoreClient, TelegramClient, attach_rows
from telegram_adapter.delivery import DeliverySender
from tests.telegram_adapter.test_adapter import SAVED, TOKEN, message, session
from tests.telegram_adapter.test_adapter import settings as settings  # noqa: F401

CID = "11111111-2222-4333-8444-555555555555"
URL = "https://example.test/?capture=" + CID
ASK = "Заметка готова.\n\nГлава\n\nПохоже, это продолжение «Курсовая». Добавить туда?"


def press(data, update_id=60, message_id=9):
    rows = attach_rows({"capture_id": CID, "mode": "ask", "title": "Курсовая"}) + [[{"text": "Открыть Beresta", "url": URL}]]
    return {"update_id": update_id, "callback_query": {
        "id": "cb-" + str(update_id), "from": {"id": 101, "is_bot": False},
        "message": {"message_id": message_id, "chat": {"id": 101, "type": "private"}, "text": ASK,
                    "reply_markup": {"inline_keyboard": rows}},
        "data": data,
    }}


class Core:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, request):
        path = request.url.path.removeprefix("/internal/v1")
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        if path == "/telegram/updates":
            return httpx.Response(200, json=SAVED)
        if path == "/telegram/actions":  # Unknown button data is taken for an old reminder token.
            return httpx.Response(409, json={"error": {"code": "action_expired", "message": "x"}, "operation_id": "o"})
        answer = self.answers.pop(0)
        if isinstance(answer, str):
            return httpx.Response(409, json={"error": {"code": answer, "message": "private"}, "operation_id": "o"})
        return httpx.Response(200, json=answer)


def run(settings, updates, core):
    async def scenario():
        bot, c, t, _, replies, _ = await session(settings, updates, core)
        async with c, t:
            await bot.poll_once()
            return replies

    return asyncio.run(scenario())


def added(**extra):
    text = "Добавил в «Курсовая».\n\nОткрыть заметку"
    return {"status": "attached", "capture_id": CID, "text": text, "undo": True,
            "links": [{"offset": text.index("Открыть"), "length": 15, "url": URL}], **extra}


def test_two_choices_and_one_undo_button_with_short_data():
    rows = attach_rows({"capture_id": CID, "mode": "ask", "title": "Очень длинное название курсовой работы по экономике"})
    assert [r[0]["text"] for r in rows][1] == "Отдельная заметка"
    assert rows[0][0]["text"].startswith("Добавить к «Очень длинное") and len(rows[0][0]["text"]) < 45
    assert [r[0]["callback_data"] for r in rows] == ["at:" + CID, "as:" + CID]
    undo = attach_rows({"capture_id": CID, "mode": "undo", "title": "x"})
    assert undo == [[{"text": "Отменить", "callback_data": "au:" + CID}]]
    assert all(len(r[0]["callback_data"].encode()) <= 64 and ATTACH_DATA.fullmatch(r[0]["callback_data"]) for r in rows + undo)
    assert attach_rows(None) == [] and attach_rows({"capture_id": "bad", "mode": "ask"}) == []


def test_the_delivery_draws_the_choices_above_the_link():
    posted = []

    def handler(request):
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9, "chat": {"id": 42}}})

    item = {"chat_id": 42, "text": ASK, "note_url": URL, "callback_token": None,
            "attach": {"capture_id": CID, "mode": "ask", "title": "Курсовая"}}

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TelegramClient(client, TOKEN).send_reminder(item)

    assert asyncio.run(go())["status"] == "sent"
    rows = posted[0]["reply_markup"]["inline_keyboard"]
    assert [r[0]["text"] for r in rows] == ["Добавить к «Курсовая»", "Отдельная заметка", "Открыть Beresta"]


@pytest.mark.parametrize("attach", [
    "x", {"capture_id": "bad", "mode": "ask", "title": "t"}, {"capture_id": CID, "mode": "other", "title": "t"},
    {"capture_id": CID, "mode": "ask", "title": "t" * 61}, {"capture_id": CID, "mode": "ask"},
    {"capture_id": CID, "mode": "ask", "title": "t", "x": 1},
])
def test_a_malformed_attach_entry_stops_the_sender(settings, attach):  # noqa: F811
    from telegram_adapter.clients import RemoteFailure

    sender = DeliverySender(settings, CoreClient(None, settings), None, None)
    item = {"delivery_id": CID, "generation": 1, "lease_token": "a" * 43, "chat_id": 101, "text": "x",
            "callback_token": None, "note_url": "https://example.test/"}
    with pytest.raises(RemoteFailure):
        sender.validate_claim({**item, "attach": attach})
    sender.validate_claim({**item, "attach": {"capture_id": CID, "mode": "undo", "title": "t"}})
    sender.validate_claim({**item, "attach": None})
    sender.validate_claim(item)


def test_add_edits_the_message_to_the_outcome_with_the_link_and_an_undo_button(settings):  # noqa: F811
    core = Core([added()])
    replies = run(settings, [press("at:" + CID)], core)
    path, body = core.calls[0]
    assert path == "/telegram/attach" and (body["capture_id"], body["action"], body["update_id"]) == (CID, "attach", 60)
    answer, edit = replies
    assert "callback_query_id" in answer
    assert edit["text"] == "Добавил в «Курсовая».\n\nОткрыть заметку" and edit["message_id"] == 9
    assert edit["entities"] == [{"type": "text_link", "offset": 22, "length": 15, "url": URL}] or edit["entities"][0]["url"] == URL
    assert edit["reply_markup"] == {"inline_keyboard": [[{"text": "Отменить", "callback_data": "au:" + CID}]]}
    assert "parse_mode" not in edit


def test_separate_and_undo_use_their_actions_and_leave_no_buttons(settings):  # noqa: F811
    text = "Оставил отдельной заметкой.\n\nОткрыть заметку"
    out = {"status": "separate", "capture_id": CID, "text": text, "undo": False,
           "links": [{"offset": text.index("Открыть"), "length": 15, "url": URL}]}
    core = Core([out, out])
    replies = run(settings, [press("as:" + CID, 60), press("au:" + CID, 61)], core)
    assert [b["action"] for _, b in core.calls] == ["separate", "undo"]
    edits = [r for r in replies if "text" in r]
    assert [e["reply_markup"] for e in edits] == [{"inline_keyboard": []}] * 2


def test_a_stale_button_alerts_and_changes_nothing(settings):  # noqa: F811
    replies = run(settings, [press("at:" + CID)], Core(["attach_expired"]))
    (answer,) = replies
    assert answer["text"] == REJECTIONS["attach_expired"] and answer["show_alert"] is True


def test_a_foreign_link_in_the_answer_is_refused(settings):  # noqa: F811
    bad = added(links=[{"offset": 0, "length": 5, "url": "https://evil.test/?capture=" + CID}])
    from telegram_adapter.clients import RemoteFailure

    with pytest.raises(RemoteFailure):
        run(settings, [press("at:" + CID)], Core([bad]))


@pytest.mark.parametrize("data", ["at:not-an-id", "ax:" + CID, "at:" + CID + ":1", "at:" + "A" * 8 + CID[8:]])
def test_garbage_button_data_never_reaches_the_attach_route(settings, data):  # noqa: F811
    core = Core([])
    run(settings, [press(data)], core)
    assert "/telegram/attach" not in [p for p, _ in core.calls]


def test_message_helper_is_still_importable():
    assert message(1, "x")["update_id"] == 1
