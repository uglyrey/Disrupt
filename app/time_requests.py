"""The queue for written reminder times that only the model can read, and the worker side that answers them.

The API process only queues (`enqueue`). The worker takes a request, asks the model through `TimeResolver` (same
provider stack, budgets and guard as the notes), changes the reminder and puts the reply into the outbox, so it
reaches the person through the ordinary Telegram delivery. See docs/reminder-confirm-v1.md.
"""

import time

from sqlalchemy import func, select, update

from app.error_logging import log_error
from app.integration import IntegrationRejection
from app.models import Item, Outbox, TimeRequest
from app.profile import prompt_dayparts, time_dayparts
from app.providers import WEEKDAYS
from app.reminder_confirm import answer_time, local_now_of, waiting_reminder
from app.services import lock_account
from app.time_ai import TimeResolver
from app.time_check import parse_words

MAX_WAITING = 3  # Per person. More than that is answered "не понял" at once, without a model call.
MAX_AGE = 600  # A request that nobody took in ten minutes is dropped: the person has long moved on.

KIND_REPLY, KIND_RETRY = "reminder_time", "reminder_time_edit"
RETRY_TEXT = "Не понял время. Нажмите «Изменить время» ещё раз и напишите, например «сегодня в 16»."
GONE_TEXT = "Напоминание уже изменено или прошло, поэтому время не менял."


def enqueue(db, body, user_id, reminder_id, generation, text):
    """Queue one answer. False when the person already has too many waiting. Idempotent per Telegram update."""
    waiting = db.scalar(
        select(func.count()).select_from(TimeRequest)
        .where(TimeRequest.user_id == user_id, TimeRequest.status.in_(("queued", "running")))
    )
    if waiting >= MAX_WAITING:
        return False
    db.add(TimeRequest(
        user_id=user_id, reminder_id=reminder_id, generation=generation, bot_id=body.bot_id, chat_id=body.chat_id,
        update_id=body.update_id, answer=text,
    ))
    db.flush()
    return True


def add_reply(db, request, kind, text, reminder_ids=None):
    db.add(Outbox(
        user_id=request.user_id, bot_id=request.bot_id, chat_id=request.chat_id, generation=1, status="pending",
        message_kind=kind, message_text=text, message_reminders=reminder_ids or None,
    ))


def finish(db, request, status, code=None):
    request.status, request.error_code, request.finished_at = status, code, time.time()
    request.answer, request.lease_until = None, None  # What the person wrote is not kept.


def claim(worker):
    now = time.time()
    with worker.sessions() as db:
        stale = db.scalars(
            select(TimeRequest).where(TimeRequest.status == "running", TimeRequest.lease_until < now)
        ).all()
        for request in stale:
            # The outcome of the model call is unknown and nothing was changed, so the person tries again.
            lock_account(db, request.user_id)
            add_reply(db, request, KIND_RETRY, RETRY_TEXT, [request.reminder_id])
            finish(db, request, "failed", "execution_unknown")
            log_error("time_request_lease_expired", "execution_unknown", log_file=worker.settings.error_log_file)
        for request in db.scalars(select(TimeRequest).where(
            TimeRequest.status == "queued", TimeRequest.created_at < now - MAX_AGE,
        )).all():
            finish(db, request, "failed", "expired")
        request_id = db.scalar(
            select(TimeRequest.id).where(TimeRequest.status == "queued")
            .order_by(TimeRequest.created_at, TimeRequest.id).limit(1)
        )
        if request_id is None:
            db.commit()
            return None
        claimed = db.execute(
            update(TimeRequest).where(TimeRequest.id == request_id, TimeRequest.status == "queued")
            .values(status="running", lease_until=now + worker.settings.lease_seconds)
        ).rowcount
        db.commit()
        return request_id if claimed else None


def prompt_data(db, row, text, local_now):
    item = db.get(Item, row.item_id) if row.item_id else None
    data = {
        "answer": text, "sentence": item.source_quote if item is not None else row.text,
        "reminder_text": row.text, "today": local_now.date().isoformat(), "weekday": WEEKDAYS[local_now.weekday()],
        "now": local_now.strftime("%H:%M"), "timezone": row.timezone,
    }
    profile = prompt_dayparts(db, row.user_id)  # Only the person's own times of day, nothing else of the profile.
    if profile:
        data["user_profile"] = profile
    return data


def run_time_request_once(worker):
    """Answer one queued request. Returns False when nothing was queued. Never raises."""
    request_id = claim(worker)
    if request_id is None:
        return False
    try:
        data = None
        with worker.sessions() as db:
            request = db.get(TimeRequest, request_id)
            user_id, reminder_id, generation, text = request.user_id, request.reminder_id, request.generation, request.answer
            row = waiting_reminder(db, user_id, reminder_id, time.time())
            if row is not None and row.generation == generation:
                local_now = local_now_of(row, time.time())
                if parse_words(text, local_now, time_dayparts(db, user_id))[0] is None:  # Our parser may read it by now: then no call is needed.
                    data = prompt_data(db, row, text, local_now)
        ai_value = TimeResolver(worker).resolve(user_id, "rt:" + request_id, data) if data is not None else None
        with worker.sessions() as db:
            lock_account(db, user_id)
            request = db.scalar(
                select(TimeRequest).where(TimeRequest.id == request_id, TimeRequest.status == "running")
            )
            if request is None:
                db.rollback()
                return True
            try:
                result = answer_time(db, user_id, reminder_id, generation, text, ai_value)
            except IntegrationRejection:
                result = {"status": "gone"}
            status = result["status"]
            if status == "set":
                add_reply(db, request, KIND_REPLY, f"Готово, напомню {result['label']}.")
            elif status == "ask":
                add_reply(db, request, KIND_REPLY, f"Поставил на {result['label']}. Верно?", [reminder_id])
            elif status == "gone":
                add_reply(db, request, KIND_REPLY, GONE_TEXT)
            else:
                add_reply(db, request, KIND_RETRY, RETRY_TEXT, [reminder_id])
            finish(db, request, "done")
            db.commit()
    except Exception as exc:
        log_error("time_request_failed", "internal_error", log_file=worker.settings.error_log_file, exception=exc)
        _give_up(worker, request_id)
    return True


def _give_up(worker, request_id):
    """Something broke before the answer was recorded. Nothing changed, so tell the person to try again."""
    try:
        with worker.sessions() as db:
            request = db.get(TimeRequest, request_id)
            if request is not None and request.status == "running":
                lock_account(db, request.user_id)
                add_reply(db, request, KIND_RETRY, RETRY_TEXT, [request.reminder_id])
                finish(db, request, "failed", "internal_error")
            db.commit()
    except Exception as exc:
        log_error("time_request_failed", "internal_error", log_file=worker.settings.error_log_file, exception=exc)
