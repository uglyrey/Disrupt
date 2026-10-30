"""Текстовая ссылка «Добавить в Google Календарь» в сообщении бота. Mock transport, настоящий Telegram и ядро не нужны."""

import asyncio
import json

import httpx
import pytest

from telegram_adapter.clients import CoreClient, RemoteFailure, TelegramClient
from telegram_adapter.delivery import CALENDAR_PREFIX, DeliverySender
from telegram_adapter.limit_text import saved_reply
from tests.telegram_adapter.test_adapter import TOKEN
from tests.telegram_adapter.test_adapter import settings as settings  # noqa: F401

RID = "11111111-2222-4333-8444-555555555555"
URL = "https://example.test/?capture=c1"
GOOD = CALENDAR_PREFIX + "text=%D0%A2%D0%B5%D1%81%D1%82&dates=20261014T174640Z/20261014T180140Z"
LABEL = "Добавить в Google Календарь"
TEXT = "Напоминание поставлено: завтра в 10:00, «позвонить маме». " + LABEL


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


def base(**extra):
    return {"chat_id": 42, "text": TEXT, "note_url": URL, "callback_token": None, **extra}


def units(text):
    return len(text.encode("utf-16-le")) // 2


def test_the_link_is_an_entity_of_the_text_and_no_new_button_appears():
    offset = units(TEXT) - units(LABEL)
    body = sending(base(links=[{"offset": offset, "length": units(LABEL), "url": GOOD}]))
    assert body["entities"] == [{"type": "text_link", "offset": offset, "length": units(LABEL), "url": GOOD}]
    assert body["reply_markup"]["inline_keyboard"] == [[{"text": "Открыть Beresta", "url": URL}]]
    assert body["text"] == TEXT and "parse_mode" not in body  # Plain text: nothing the person wrote is interpreted.


def test_the_buttons_stay_as_they_were_with_a_link():
    entry = {"reminder_id": RID, "generation": 1, "alt_label": None}
    body = sending(base(confirm=[entry], links=[{"offset": 0, "length": 5, "url": GOOD}]))
    rows = body["reply_markup"]["inline_keyboard"]
    assert [button["text"] for row in rows for button in row if "url" in button] == ["Открыть Beresta"]
    assert all("calendar" not in json.dumps(row) for row in rows)


def test_without_links_or_with_an_old_core_nothing_changes():
    assert "entities" not in sending(base(links=[]))
    assert "entities" not in sending(base())


def test_offsets_count_utf16_units_after_emoji():
    text = "😀 Поставил: «x». " + LABEL
    offset = units(text) - units(LABEL)
    assert offset == len(text) - len(LABEL) + 1  # The emoji takes two units
    body = sending(base(text=text, links=[{"offset": offset, "length": units(LABEL), "url": GOOD}]))
    assert body["entities"][0]["offset"] == offset


def claimed(**extra):
    return {"delivery_id": RID, "generation": 1, "lease_token": "a" * 43, "chat_id": 101, "text": TEXT,
            "callback_token": None, "note_url": "https://example.test/", **extra}


def link(**changes):
    return {"offset": 3, "length": 5, "url": GOOD, **changes}


@pytest.mark.parametrize("links", [
    "x", [GOOD], [link()] * 7, [link(url="https://evil.test/calendar/render?action=TEMPLATE&")],
    [link(url="http://calendar.google.com/calendar/render?action=TEMPLATE&text=x")],
    [link(url="https://calendar.google.com.evil.test/calendar/render?action=TEMPLATE&text=x")],
    [link(url=GOOD + "a" * 2000)], [link(url=GOOD + " x")], [link(url=GOOD + "\n")], [link(url=GOOD + "Ж")],
    [link(url=5)], [link(extra=1)], [{}], [None], [link(offset=-1)], [link(offset=True)], [link(length=0)],
    [link(offset=units(TEXT))], [link(offset=units(TEXT) - 2, length=5)],
    [link(url="https://evil.test/?capture=c1")], [link(url="https://example.test.evil.test/?capture=c1")],
    [link(url="http://example.test/?capture=c1")], [link(url="https://example.test/other?capture=c1")],
    [link(url="https://user@example.test/?capture=c1")], [link(url="https://example.test/")],
])
def test_a_link_list_that_is_not_ours_stops_the_sender(settings, links):  # noqa: F811
    sender = DeliverySender(settings, CoreClient(None, settings), None, None)
    with pytest.raises(RemoteFailure):
        sender.validate_claim(claimed(links=links))


def test_good_link_lists_pass(settings):  # noqa: F811
    sender = DeliverySender(settings, CoreClient(None, settings), None, None)
    sender.validate_claim(claimed(links=[]))
    sender.validate_claim(claimed(links=[link()]))
    sender.validate_claim(claimed(links=[link()] * 5))
    sender.validate_claim(claimed())


def test_a_note_saved_without_ai_names_its_category_in_the_reply():
    assert saved_reply({"category_name": "Общее"}).endswith("\nКатегория: Общее")
    assert "Категория" not in saved_reply({})
    assert "Категория" not in saved_reply({"category_name": "  "})
    limited = saved_reply({"ai_limit_exceeded": True, "category_name": "Общее"})
    assert limited.endswith("Категория: Общее") and "Лимит ИИ" in limited


NOTE = "https://example.test/?capture=c1&reminder=" + RID
NOTE_TEXT = "Напоминание поставлено: завтра, «x». Открыть заметку"


def test_the_note_address_is_a_text_link_and_the_text_has_no_raw_address():
    offset = units(NOTE_TEXT) - units("Открыть заметку")
    body = sending(base(text=NOTE_TEXT, note_url=NOTE, links=[{"offset": offset, "length": units("Открыть заметку"), "url": NOTE}]))
    assert body["entities"] == [{"type": "text_link", "offset": offset, "length": units("Открыть заметку"), "url": NOTE}]
    assert "http" not in body["text"] and body["text"].endswith("Открыть заметку")
    assert "parse_mode" not in body


def test_note_links_and_calendar_links_go_together(settings):  # noqa: F811
    sender = DeliverySender(settings, CoreClient(None, settings), None, None)
    text = "x " + LABEL + "\nОткрыть заметку"
    sender.validate_claim(claimed(text=text, links=[link(), {"offset": units(text) - 15, "length": 15, "url": NOTE}]))
    body = sending(base(text=text, links=[
        {"offset": 2, "length": units(LABEL), "url": GOOD}, {"offset": units(text) - 15, "length": 15, "url": NOTE},
    ]))
    assert [entity["type"] for entity in body["entities"]] == ["text_link", "text_link"]
