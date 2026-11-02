"""The Telegram buttons of a record that may continue a note (docs/attach-to-note-v1.md)."""

from fastapi import HTTPException
from sqlalchemy import select

from app import attach
from app.deliveries import link_note_urls
from app.integration import IntegrationRejection
from app.models import Capture

EXPIRED = "Кнопка устарела, запись осталась отдельной заметкой"


def attach_answer(db, user_id, body, settings):
    """Run the button and answer with the text the bot puts in place of the message. Pressing again answers the same."""
    capture = db.scalar(select(Capture).where(Capture.id == body.capture_id, Capture.user_id == user_id))
    if capture is None:
        raise IntegrationRejection(409, "attach_expired", EXPIRED)
    try:
        if body.action == "attach":
            if capture.attach_note_id is None:
                raise HTTPException(409, EXPIRED)
            done = attach.choose_attach(db, user_id, capture.id, capture.attach_note_id, settings, commit=False)
        else:
            done = attach.choose_separate(db, user_id, capture.id, settings, commit=False)
    except HTTPException:
        raise IntegrationRejection(409, "attach_expired", EXPIRED) from None
    link = settings.public_origin + "/?capture=" + capture.id
    if done["state"] == "attached":
        title = attach.clip_title(done["note_title"], 60)
        head = f"Добавил в «{title}»."
    elif body.action == "undo":
        head = "Отменил добавление. Запись осталась отдельной заметкой."
    else:
        head = "Оставил отдельной заметкой."
    text, links = link_note_urls(head + "\n\n" + link, [], settings.public_origin)
    return {
        "status": done["state"], "capture_id": capture.id, "text": text, "links": links,
        "undo": done["state"] == "attached" and attach.can_undo(capture),
    }
