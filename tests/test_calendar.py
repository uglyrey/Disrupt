"""Ссылка «Добавить в Google Календарь»: адрес, поле calendar_url, переключатель calendar_offer и текстовая ссылка в Telegram."""

import time
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select

from app import calendar_link
from app.calendar_link import BASE, LABEL, MAX_URL_CHARS, calendar_url, clean_title, note_link, stamp
from app.deliveries import add_reminder_set_message, attach_links
from app.models import Note, Reminder, TelegramIdentity
from app.reminders import AUTO_KEY_PREFIX
from tests.test_reminders import add, auth, claim, due_delivery, payload, result
from tests.test_reminders import reminders as reminders  # noqa: F401

LINK = "https://beresta.example/?capture=c1&reminder=r1"
AT = 1_792_000_000  # 2026-10-14 17:46:40 UTC


def fields(url):
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def offer(client, value):
    reply = client.patch("/api/v1/profile/calendar-offer", json={"calendar_offer": value})
    assert reply.status_code == 200, reply.text


# ---- Адрес ----


def test_the_url_is_googles_template_page_with_a_fifteen_minute_event():
    url = calendar_url("Позвонить маме", AT, LINK)
    assert url.startswith(BASE + "&") and urlsplit(url).netloc == "calendar.google.com"
    data = fields(url)
    assert data["action"] == "TEMPLATE" and data["text"] == "Позвонить маме"
    assert data["dates"] == f"{stamp(AT)}/{stamp(AT + 900)}" == "20261014T174640Z/20261014T180140Z"
    assert data["details"] == "Напоминание из beresta\n" + LINK
    assert url.isascii() and " " not in url and "\n" not in url


def test_special_characters_are_encoded_and_cannot_add_fields():
    url = calendar_url("Купить хлеб&молоко #срочно=да ?x", AT, LINK + "&evil=1")
    data = fields(url)
    assert data["text"] == "Купить хлеб&молоко #срочно=да ?x" and set(data) == {"action", "text", "dates", "details"}
    assert "evil" not in data and data["details"].endswith("&evil=1")


def test_a_long_title_is_trimmed_and_blank_ones_get_a_default():
    assert len(clean_title("а" * 500)) == calendar_link.TITLE_CHARS and clean_title("а" * 500).endswith("…")
    assert clean_title("  много\n   пробелов\t") == "много пробелов"
    assert clean_title("") == clean_title(None) == clean_title(" \n ") == "Напоминание"
    assert clean_title("a\x00b\x07c") == "abc"


def test_the_url_never_goes_over_the_telegram_limit():
    for text in ("ж" * 5000, "Я" * 120, "😀" * 300):
        assert len(calendar_url(text, AT, LINK)) <= MAX_URL_CHARS
    huge = "https://beresta.example/?" + "q" * 4000
    url = calendar_url("Позвонить", AT, huge)
    assert len(url) <= MAX_URL_CHARS and fields(url)["text"] == "Позвонить" and "details" not in fields(url)


def test_the_note_link_is_the_same_page_as_the_telegram_button():
    assert note_link("https://beresta.example", "c1", "r1") == LINK


# ---- Поле calendar_url в ответах ----


def test_calendar_url_follows_the_account_switch(reminders):
    app, client, ids = reminders
    created = add(client, ids)
    assert created.status_code == 201
    path = "/api/v1/reminders/" + created.json()["id"]
    body = client.get(path).json()  # Offered by default
    data = fields(body["calendar_url"])
    assert data["text"] == "Позвонить" and data["dates"].startswith(stamp(datetime.fromisoformat(body["scheduled_at"]).timestamp()))
    assert f"reminder={body['id']}" in data["details"] and "capture=" in data["details"]
    listed = client.get("/api/v1/notes/" + ids["note"] + "/reminders").json()
    assert listed[0]["calendar_url"] == body["calendar_url"]
    assert created.json()["calendar_url"] == body["calendar_url"]
    offer(client, False)
    assert client.get(path).json()["calendar_url"] is None
    offer(client, True)
    assert client.get(path).json()["calendar_url"] == body["calendar_url"]


def test_a_change_of_time_changes_the_link_and_a_cancelled_reminder_has_none(reminders):
    app, client, ids = reminders
    created = add(client, ids).json()
    path = "/api/v1/reminders/" + created["id"]
    edit = {k: v for k, v in payload().items() if k != "item_id"}
    edit["scheduled_at"] = "2030-01-01T10:00:00+03:00"
    changed = client.patch(path, json={**edit, "generation": 1}).json()
    assert fields(changed["calendar_url"])["dates"].startswith("20300101T070000Z")
    cancelled = client.post(path + "/cancel", json={"generation": 2}).json()
    assert cancelled["status"] == "cancelled" and cancelled["calendar_url"] is None


def test_a_reminder_in_the_past_has_no_calendar_link(reminders):
    app, client, ids = reminders
    created = add(client, ids).json()
    with app.state.sessions.begin() as db:
        db.get(Reminder, created["id"]).scheduled_at = time.time() - 60
    assert client.get("/api/v1/reminders/" + created["id"]).json()["calendar_url"] is None


def test_the_reminder_channel_is_gone_and_the_switch_is_in_the_account_data(reminders):
    app, client, ids = reminders
    assert client.patch("/api/v1/profile/reminder-channel", json={"reminder_channel": "calendar"}).status_code in (404, 405)
    assert client.get("/api/v1/auth/me").json()["calendar_offer"] is True
    assert "reminder_channel" not in client.get("/api/v1/auth/me").json()
    assert "reminder_channel" not in client.get("/api/v1/profile").json()
    offer(client, False)
    assert client.get("/api/v1/auth/me").json()["calendar_offer"] is False
    assert client.get("/api/v1/profile").json()["calendar_offer"] is False
    bad = client.patch("/api/v1/profile/calendar-offer", json={"calendar_offer": "yes", "x": 1})
    assert bad.status_code == 422


def test_the_link_is_never_shown_to_other_accounts(reminders):
    app, client, ids = reminders
    created = add(client, ids).json()
    from fastapi.testclient import TestClient

    from tests.conftest import register

    stranger = TestClient(app)
    register(stranger, "stranger")
    assert stranger.get("/api/v1/reminders/" + created["id"]).status_code == 404


# ---- Текстовая ссылка в сообщениях ----


def reminder_set(app, client, ids):
    """A short «reminder is set» message for a note saved without AI, with one reminder the AI-less flow created."""
    created = add(client, ids, payload(ids["item"]), AUTO_KEY_PREFIX + "key1")
    assert created.status_code == 201, created.text
    with app.state.sessions.begin() as db:
        note = db.get(Note, ids["note"])
        identity = db.scalar(select(TelegramIdentity))
        add_reminder_set_message(db, note, identity, app.state.settings)
        return created.json()["id"]


def clicked(item, link):
    units = item["text"].encode("utf-16-le")
    return units[link["offset"] * 2:(link["offset"] + link["length"]) * 2].decode("utf-16-le")


def test_the_confirmation_ends_with_a_text_link_and_has_no_new_buttons(reminders):
    app, client, ids = reminders
    reminder_id = reminder_set(app, client, ids)
    (item,) = claim(client).json()["items"]
    assert item["text"].startswith("Напоминание поставлено: ")
    first = item["text"].split("\n")[0]
    assert first.endswith("«Позвонить». " + LABEL)
    (link,) = item["links"]
    assert clicked(item, link) == LABEL and link["url"].startswith(BASE + "&")
    assert f"reminder={reminder_id}" in fields(link["url"])["details"]
    assert "calendar" not in item and item["confirm"] == []  # No button of any kind was added
    assert auth(client, item).status_code == 200 and result(client, item).status_code == 200


def test_with_the_offer_off_the_confirmation_has_no_link_and_no_phrase(reminders):
    app, client, ids = reminders
    offer(client, False)
    reminder_set(app, client, ids)
    (item,) = claim(client).json()["items"]
    assert item["links"] == [] and "Google" not in item["text"]
    assert item["text"].split("\n")[0].endswith("«Позвонить». Изменить: " + item["text"].split("Изменить: ")[1])


def test_a_cancelled_reminder_leaves_no_dead_phrase(reminders):
    app, client, ids = reminders
    reminder_id = reminder_set(app, client, ids)
    client.post("/api/v1/reminders/" + reminder_id + "/cancel", json={"generation": 1})
    for item in claim(client).json()["items"]:
        assert item["links"] == [] and "Google" not in item["text"]


def test_the_message_that_fires_has_no_link_because_the_time_has_come(reminders):
    app, client, ids = reminders
    due_delivery(app, client, ids)
    (item,) = claim(client).json()["items"]
    assert item["links"] == [] and "Google" not in item["text"]
    assert auth(client, item).status_code == 200 and result(client, item).status_code == 200


def test_attach_links_counts_utf16_and_drops_the_phrase_without_an_address():
    text = "Заголовок\n😀 первая. " + LABEL + "\nвторая. " + LABEL + "\nИзменить: x"
    first, second = BASE + "&text=a", BASE + "&text=b"
    full, links = attach_links(text, [first, second])
    assert full == text and [link["url"] for link in links] == [first, second]
    units = full.encode("utf-16-le")
    assert all(units[link["offset"] * 2:(link["offset"] + link["length"]) * 2].decode("utf-16-le") == LABEL for link in links)
    lone, links = attach_links(text, [first, None])
    assert lone == text.replace("вторая. " + LABEL, "вторая.") and len(links) == 1
    assert attach_links("без ссылок", [first]) == ("без ссылок", [])


def test_a_phrase_inside_the_text_of_a_task_is_not_a_link():
    text = "Напоминание поставлено: завтра, «купить " + LABEL + " зря». " + LABEL
    full, links = attach_links(text, [BASE + "&text=a"])
    assert len(links) == 1 and links[0]["offset"] == len(text) - len(LABEL) and full == text
