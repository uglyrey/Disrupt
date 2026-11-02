"""After a button press the bot edits the message. Its text links must stay links."""

import asyncio

from telegram_adapter.bot import Bot


class Recorder:
    def __init__(self):
        self.calls = []

    async def call(self, method, body):
        self.calls.append((method, body))
        return {}


def edit(message, outcome="Готово."):
    bot = Bot.__new__(Bot)
    bot.telegram = Recorder()
    asyncio.run(bot.show_outcome(message, 7, outcome))
    return bot.telegram.calls


def test_text_links_survive_the_edit_with_the_outcome_added_below():
    original = "Напоминание поставлено: завтра, «Позвонить». Изменить в Бересте"
    link = {"type": "text_link", "offset": original.index("Изменить"), "length": len("Изменить в Бересте"),
            "url": "https://example.test/?capture=c1&reminder=r1"}
    message = {"message_id": 5, "text": original, "entities": [link, {"type": "bold", "offset": 0, "length": 3}]}
    ((method, body),) = edit(message)
    assert method == "editMessageText" and body["text"] == original + "\n\nГотово."
    assert body["entities"] == [link]  # Only text links are carried over; offsets stay valid
    units = body["text"].encode("utf-16-le")
    assert units[link["offset"] * 2:(link["offset"] + link["length"]) * 2].decode("utf-16-le") == "Изменить в Бересте"


def test_no_entities_when_the_text_is_replaced_by_the_outcome():
    original = "я" * 4090
    message = {"message_id": 5, "text": original, "entities": [
        {"type": "text_link", "offset": 0, "length": 3, "url": "https://example.test/?capture=c1"}]}
    ((_, body),) = edit(message, "Готово, это длинный итог.")
    assert body["text"] == "Готово, это длинный итог." and "entities" not in body
