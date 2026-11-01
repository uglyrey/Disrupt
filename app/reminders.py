"""User-confirmed reminders. The scheduler and Telegram sender live elsewhere."""

import hashlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from datetime import time as clock
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.analytics import record_event
from app.calendar_link import DEFAULT_MINUTES, calendar_url, note_link
from app.config import Settings
from app.models import Capture, Item, Note, Outbox, Reminder, User
from app.note_style import (
    explicit_duration,
    human_when,
    strip_time_phrase,
    valid_minutes,
    with_reminder_lines,
)
from app.profile import time_prefs
from app.reminder_proposals import mentions_time, sentence_spans
from app.services import cancel_reminder_deliveries, lock_account, owned_note
from app.time_check import ASK, CORRECTED, NEEDS_CHECK, UNCLEAR, Reading, decide
from app.timezones import effective_zone, zone_guessed

AUTO_KEY_PREFIX = "ai:"
ADVANCE_KEY_PREFIX = "adv:"
ADVANCE_TEXT = "Через час: "
ADVANCE_SECONDS = 3600
EVENING_DEFAULT = (20, 0)
MAX_REMINDER_TEXT = 1000


def calendar_link_for(db, reminder, settings, *, offer=None):
    """The «Добавить в Google Календарь» link of a reminder that is still ahead, or None.

    None when the account switched the offer off, for an advance reminder («Через час: ...») and for a reminder that
    is no longer pending in the future."""
    row = db.execute(select(User.calendar_offer, User.calendar_default_minutes).where(User.id == reminder.user_id)).first()
    if offer is None:
        offer = bool(row and row[0])
    default_minutes = row[1] if row else DEFAULT_MINUTES
    if (
        not offer or reminder.advance_of is not None or reminder.status != "confirmed"
        or reminder.scheduled_at <= time.time()
    ):
        return None
    capture_id = db.scalar(select(Note.capture_id).where(Note.id == reminder.note_id, Note.user_id == reminder.user_id))
    if capture_id is None:
        return None
    origin = (settings or Settings()).public_origin
    minutes = reminder.duration_minutes or default_minutes
    return calendar_url(reminder.text, reminder.scheduled_at, note_link(origin, capture_id, reminder.id), minutes)


def reminder_view(db, reminder, settings=None):
    delivery = db.scalar(select(Outbox.status).where(
        Outbox.reminder_id == reminder.id, Outbox.user_id == reminder.user_id,
        Outbox.generation == reminder.generation,
    ))
    previous_unknown = db.scalar(select(Outbox.id).where(
        Outbox.reminder_id == reminder.id, Outbox.user_id == reminder.user_id,
        Outbox.generation < reminder.generation, Outbox.status == "unknown",
        Outbox.authorized_at.is_not(None),
    ).limit(1)) is not None
    return {
        "id": reminder.id, "note_id": reminder.note_id, "item_id": reminder.item_id,
        "scheduled_at": datetime.fromtimestamp(reminder.scheduled_at, UTC).isoformat(),
        "timezone": reminder.timezone, "text": reminder.text, "status": reminder.status,
        "generation": reminder.generation,
        "confirmed_at": datetime.fromtimestamp(reminder.confirmed_at, UTC).isoformat(),
        "delivery_status": delivery,
        "previous_attempt_unknown": previous_unknown,
        "time_check": reminder.time_check,
        "calendar_url": calendar_link_for(db, reminder, settings),
        "duration_minutes": reminder.duration_minutes,
        "advance_of": reminder.advance_of,
    }


def owned_reminder(db, reminder_id, user_id):
    if "\x00" in reminder_id:
        raise HTTPException(404, "Напоминание не найдено")
    row = db.scalar(select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id))
    if row is None:
        raise HTTPException(404, "Напоминание не найдено")
    return row


def validate_target(db, note_id, item_id, user_id):
    owned_note(db, note_id, user_id)
    if item_id is not None:
        if "\x00" in item_id:
            raise HTTPException(404, "Задача не найдена")
        item = db.scalar(select(Item).where(
            Item.id == item_id, Item.note_id == note_id, Item.user_id == user_id, Item.deleted_at.is_(None),
        ))
        if item is None:
            raise HTTPException(404, "Задача не найдена")
        if item.kind != "task" or item.status != "open":
            raise HTTPException(409, "Напоминание можно назначить только открытой задаче")


def future_time(value):
    scheduled_at = datetime.fromisoformat(value).timestamp()
    if scheduled_at <= time.time():
        raise HTTPException(422, "Выберите точное время в будущем")
    return scheduled_at


def create_reminder(db, user_id, note_id, body, key):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", key):
        raise HTTPException(422, "Некорректный Idempotency-Key")
    lock_account(db, user_id)
    owned_note(db, note_id, user_id)
    digest = hashlib.sha256(json.dumps(
        {"note_id": note_id, **body.model_dump()}, sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    previous = db.scalar(select(Reminder).where(
        Reminder.user_id == user_id, Reminder.idempotency_key == key,
    ))
    if previous:
        if previous.creation_hash != digest:
            raise HTTPException(409, "Этот Idempotency-Key уже использован для другого напоминания")
        return previous
    validate_target(db, note_id, body.item_id, user_id)
    row = Reminder(
        user_id=user_id, note_id=note_id, item_id=body.item_id,
        scheduled_at=future_time(body.scheduled_at), timezone=body.timezone, text=body.text,
        idempotency_key=key, creation_hash=digest,
    )
    db.add(row)
    db.flush()
    record_event(db, user_id, "reminder_confirmed", f"{row.id}:1", subject_id=row.note_id)
    sync_advance(db, row)
    return row


def edit_reminder(db, user_id, reminder_id, body):
    lock_account(db, user_id)
    row = owned_reminder(db, reminder_id, user_id)
    if row.generation != body.generation:
        raise HTTPException(409, "Напоминание уже изменено. Обновите его")
    validate_target(db, row.note_id, row.item_id, user_id)
    reschedule(db, row, future_time(body.scheduled_at), body.timezone, body.text)
    return row


def reschedule(db, row, scheduled_at, timezone, text, channel="web"):
    """The one way to move a reminder. The site edit and the Telegram snooze buttons both come here."""
    cancel_reminder_deliveries(db, row.user_id, [row.id])
    row.scheduled_at, row.timezone, row.text = scheduled_at, timezone, text
    row.status, row.confirmed_at = "confirmed", time.time()
    row.generation += 1
    # The person (or a snooze) chose this time, so a pending question about the old one is settled.
    row.time_check, row.time_alt = (CORRECTED if row.time_check in NEEDS_CHECK else row.time_check), None
    record_event(db, row.user_id, "reminder_confirmed", f"{row.id}:{row.generation}", channel, subject_id=row.note_id)
    sync_advance(db, row)


def advance_moment(main_at, zone, prefs, now):
    """When the advance reminder («Через час: ...») for a main reminder at `main_at` goes out, or None to skip it.

    One hour before. Only a time beresta chose moves: if it falls into the person's quiet hours it goes to the end of
    the quiet hours of that day when that is still before the main time, otherwise to the person's evening of the day
    before. If neither fits (already past, or inside quiet hours again) there is no advance reminder.
    """
    zone = ZoneInfo(zone)
    when = datetime.fromtimestamp(main_at - ADVANCE_SECONDS, zone)
    if when.timestamp() <= now:
        return None
    quiet = prefs.get("quiet")
    if not quiet:
        return when.timestamp()
    start, end = minutes_of(quiet[0]), minutes_of(quiet[1])
    minute = when.hour * 60 + when.minute
    if not in_interval(minute, start, end):
        return when.timestamp()
    over_midnight = start > end
    day = when.date() + timedelta(days=1) if over_midnight and minute >= start else when.date()
    opens = datetime.combine(day, clock(*quiet[1]), tzinfo=zone)
    if now < opens.timestamp() < main_at:
        return opens.timestamp()
    evening = (prefs.get("dayparts") or {}).get("evening", EVENING_DEFAULT)
    before = datetime.combine(datetime.fromtimestamp(main_at, zone).date() - timedelta(days=1), clock(*evening), tzinfo=zone)
    quiet_then = in_interval(before.hour * 60 + before.minute, start, end)
    if now < before.timestamp() < main_at and not quiet_then:
        return before.timestamp()
    return None


def sync_advance(db, main, now=None):
    """Keep the advance reminder of `main` in step with it (docs/profile-v1.md).

    For a person whose profile says `reminder_style = twice` every main reminder has a second one an hour earlier.
    Moving the main one moves it, cancelling or finishing the main one cancels it. The advance reminder is a row of
    its own (`advance_of` = the main id) so it is delivered like any other. The caller owns the transaction.
    Returns the advance reminder or None.
    """
    if main.advance_of is not None:
        return None
    now = time.time() if now is None else now
    key = ADVANCE_KEY_PREFIX + main.id
    existing = db.scalar(select(Reminder).where(Reminder.user_id == main.user_id, Reminder.idempotency_key == key))
    prefs = time_prefs(db, main.user_id)
    when = None
    if prefs and prefs.get("style") == "twice" and main.status == "confirmed":
        when = advance_moment(main.scheduled_at, main.timezone, prefs, now)
    if when is None:
        if existing is not None and existing.status == "confirmed":
            cancel_deliveries_and_close(db, existing)
        return None
    text = (ADVANCE_TEXT + main.text)[:MAX_REMINDER_TEXT]
    if existing is None:
        existing = Reminder(
            user_id=main.user_id, note_id=main.note_id, item_id=main.item_id, scheduled_at=when,
            timezone=main.timezone, text=text, idempotency_key=key, advance_of=main.id,
            creation_hash=hashlib.sha256(f"advance:{main.id}".encode()).hexdigest(), zone_guessed=main.zone_guessed,
        )
        db.add(existing)
        db.flush()
        return existing
    if existing.status == "confirmed" and existing.scheduled_at == when and existing.text == text:
        return existing
    cancel_reminder_deliveries(db, existing.user_id, [existing.id])
    existing.scheduled_at, existing.timezone, existing.text = when, main.timezone, text
    existing.note_id, existing.item_id, existing.zone_guessed = main.note_id, main.item_id, main.zone_guessed
    existing.status, existing.confirmed_at = "confirmed", time.time()
    existing.generation += 1
    return existing


def cancel_deliveries_and_close(db, row):
    cancel_reminder_deliveries(db, row.user_id, [row.id])
    row.status = "cancelled"
    row.generation += 1


def complete_reminder(db, reminder):
    """«Выполнено» on a reminder that has no task: it is finished together with its advance (or main) twin."""
    reminder.status = "completed"
    if reminder.advance_of is not None:
        main = db.get(Reminder, reminder.advance_of)
        if main is not None and main.user_id == reminder.user_id and main.status in {"confirmed", "sent", "unknown"}:
            main.status = "completed"
            sync_advance(db, main)
    else:
        sync_advance(db, reminder)


def snooze_time(row, action, now):
    """New UTC timestamp for a Telegram snooze button.

    snooze_hour is now plus one hour. snooze_tomorrow is the next calendar day after today in the
    reminder's own zone, at the wall-clock time of the current schedule. Zoneinfo keeps it right across DST;
    a wall time that does not exist on that day moves forward with the clock.
    """
    if action == "snooze_hour":
        return now + 3600
    zone = ZoneInfo(row.timezone)
    wall = datetime.fromtimestamp(row.scheduled_at, zone)
    day = datetime.fromtimestamp(now, zone).date() + timedelta(days=1)
    return datetime.combine(day, clock(wall.hour, wall.minute), tzinfo=zone).timestamp()


def local_clock(row):
    return datetime.fromtimestamp(row.scheduled_at, ZoneInfo(row.timezone)).strftime("%H:%M")


def instant_of(local, zone):
    """UTC timestamp of a naive local wall time in `zone`, or None if that time does not exist (a DST gap)."""
    aware = local.replace(tzinfo=ZoneInfo(zone))
    instant = aware.astimezone(UTC)
    if instant.astimezone(ZoneInfo(zone)).replace(tzinfo=None) != local:
        return None
    return instant.timestamp()


def reading_instants(reading, zone, stamp):
    """(scheduled_at, alternative) as UTC timestamps for a reading. scheduled_at is None unless it is ahead of `stamp`."""
    when = instant_of(reading.when, zone) if reading.when is not None else None
    if when is None or when <= stamp:
        return None, None
    alt = instant_of(reading.alt, zone) if reading.alt is not None else None
    return when, alt if alt is not None and alt > stamp and alt != when else None


def add_auto_reminder(db, note, item, text, scheduled_at, timezone, guessed=False, time_check=None, time_alt=None,
                      duration=None):
    """One confirmed reminder for a task, keyed by the task so it is never made twice. None if it already exists."""
    key = AUTO_KEY_PREFIX + item.id
    digest = hashlib.sha256(json.dumps(
        {"note_id": note.id, "item_id": item.id, "scheduled_at": scheduled_at,
         "timezone": timezone, "text": text, "auto": True}, sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    row = Reminder(
        user_id=note.user_id, note_id=note.id, item_id=item.id, scheduled_at=scheduled_at,
        timezone=timezone, text=text, idempotency_key=key, creation_hash=digest, zone_guessed=guessed,
        time_check=time_check, time_alt=time_alt, duration_minutes=valid_minutes(duration),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None  # Another process made it first.
    return row


def auto_reminders(db, note, settings=None, now=None, model_times=None, model_durations=None):
    """Set a confirmed reminder for every open task of a fresh note whose deadline can be read.

    The time is the model's `due_at` (`model_times`: item id to local `YYYY-MM-DDTHH:MM`) cross-checked with the
    parser reading `due_text`, see `app.time_check.decide`. A reminder whose time is a guess is marked
    `time_check = ask`. The reminder is tied to the task and never made twice: its idempotency key is built
    from the task. A time in the past gives nothing. The caller holds the account lock and owns the transaction.
    Returns the created reminders.
    """
    user = db.get(User, note.user_id)
    if user is None or not user.ai_reminders_enabled:
        return []
    items = db.scalars(select(Item).where(
        Item.note_id == note.id, Item.user_id == note.user_id, Item.kind == "task", Item.status == "open",
        Item.deleted_at.is_(None),
    ).order_by(Item.position, Item.id)).all()
    if not items:
        return []
    taken = set(db.scalars(select(Reminder.item_id).where(
        Reminder.user_id == note.user_id, Reminder.item_id.in_([i.id for i in items]), Reminder.status != "cancelled",
    )))
    zone = effective_zone(user, settings)
    moment = now or datetime.now(UTC)
    local_now = moment.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    prefs = time_prefs(db, note.user_id)
    dayparts = prefs["dayparts"] if prefs else None  # The person's own «утром», «после пар» and so on.
    created = []
    for item in items:
        text = " ".join(item.text.split())[:MAX_REMINDER_TEXT].strip()
        if not item.due_text or not text or item.id in taken:
            continue
        key = AUTO_KEY_PREFIX + item.id
        if db.scalar(select(Reminder.id).where(Reminder.user_id == note.user_id, Reminder.idempotency_key == key)):
            continue
        reading = decide(item.due_text, (model_times or {}).get(item.id), local_now, dayparts)
        when, alt = reading_instants(reading, zone, moment.timestamp())
        if when is None:
            continue
        row = add_auto_reminder(
            db, note, item, text, when, zone, zone_guessed(user), reading.check, alt if reading.check == ASK else None,
            (model_durations or {}).get(item.id),
        )
        if row is not None:
            created.append(row)
            sync_advance(db, row, moment.timestamp())
    return created


def add_reminder_lines(db, note, reminders):
    """Write «Напоминание: завтра, 10:00» into a fresh note, one line per event, from the real reminder data.

    The day is counted from the moment the reminder was made, in the zone of the reminder. A reminder whose time is
    still a guess (`time_check` ask or unclear) gets no line: the «Верно?» question settles it first, and the text of
    a note the person may already be editing is never rewritten afterwards (docs/note-style-v1.md). Returns the
    reminders that got a line."""
    rows = sorted(
        (r for r in reminders if r.advance_of is None and r.status == "confirmed" and r.time_check not in NEEDS_CHECK),
        key=lambda r: (r.scheduled_at, r.id),
    )
    entries = []
    for row in rows:
        zone = ZoneInfo(row.timezone)
        local = datetime.fromtimestamp(row.scheduled_at, zone).replace(tzinfo=None)
        today = datetime.fromtimestamp(row.confirmed_at, zone).replace(tzinfo=None)
        entries.append((row.text, human_when(local, today)))
    if entries:
        note.markdown = with_reminder_lines(note.markdown, entries)
    return rows if entries else []


# Explicit requests to be reminded. They are the person's own instruction, so they are honoured
# even when «ИИ сам ставит напоминания» is off, and they do not depend on what the model returned.
REQUEST = re.compile(
    r"\b(?:напомни(?:те)?|напомнить|не\s+забыть|не\s+забудь(?:те)?|remind\s+me"
    r"|(?:постав\w*|созда\w*|сдела\w*|добав\w*)\s+(?:мне\s+)?(?:напоминани[еяю]|будильник|таймер)"
    r"|(?:нужно|надо|нужен|нужна)\s+(?:мне\s+)?(?:напоминани[еяю]|будильник|таймер))\b",
    re.IGNORECASE,
)
FILLER = re.compile(r"\b(?:мне|пожалуйста|плиз|please)\b|^(?:что|чтобы|to)\s+", re.IGNORECASE)
MAX_ITEMS = 30
DAY_START, DAY_END, MORNING_HOUR = 8, 21, 9
NOT_UNDERSTOOD = "Не понял, когда напомнить. Укажите время, например «завтра в 10»"


def request_task_text(sentence, match):
    """The action of a request: the sentence without the trigger words, first letter capital."""
    rest = sentence[:match.start()] + " " + sentence[match.end():]
    for _ in range(2):
        rest = " ".join(FILLER.sub(" ", rest).split()).strip(" ,.;:!?-–")
    rest = strip_time_phrase(rest)[:MAX_REMINDER_TEXT].strip()
    if not rest:
        rest = " ".join(sentence.split())[:MAX_REMINDER_TEXT]
    return rest[:1].upper() + rest[1:]


def in_interval(minute, start, end):
    """Is the minute of the day in [start, end)? An interval with end <= start runs over midnight."""
    return start <= minute < end if start < end else minute >= start or minute < end


def minutes_of(clock_pair):
    return clock_pair[0] * 60 + clock_pair[1]


def awake_at(moment, prefs):
    """Whether a reminder may reach the person at this local time, by their wake, sleep and quiet hours."""
    minute = moment.hour * 60 + moment.minute
    quiet = prefs.get("quiet")
    start = minutes_of(prefs["wake"]) if prefs.get("wake") else minutes_of(quiet[1]) if quiet and quiet[0] > quiet[1] else DAY_START * 60
    end = minutes_of(prefs["sleep"]) if prefs.get("sleep") else minutes_of(quiet[0]) if quiet and quiet[0] > quiet[1] else DAY_END * 60
    if quiet and in_interval(minute, minutes_of(quiet[0]), minutes_of(quiet[1])):
        return False
    return in_interval(minute, start, end)


def next_awake(moment, prefs):
    """The first time from `moment` on when the person may be reminded. A day that opens late starts at their morning."""
    probe = moment.replace(second=0, microsecond=0)
    for _ in range(2 * 1440):
        if awake_at(probe, prefs):
            break
        probe += timedelta(minutes=1)
    else:
        return None
    hour, minute = prefs["dayparts"].get("morning", (MORNING_HOUR, 0)) if prefs.get("dayparts") else (MORNING_HOUR, 0)
    morning = probe.replace(hour=hour, minute=minute)
    # The window has just opened: the reminder waits for the person's morning when that is still in the window.
    if not awake_at(probe - timedelta(minutes=1), prefs) and probe < morning and awake_at(morning, prefs):
        return morning
    return probe


def default_reminder_time(now, settings, zone=None, prefs=None):
    """When to remind about a request that names no time, as an aware datetime in the account zone.

    Between 08:00 and 21:00 local time it is now plus NOTES_REMINDER_DEFAULT_DELAY (an hour by default).
    Earlier it is 09:00 the same morning, later it is 09:00 the next day. A delay of 0 turns this off.
    With the person's profile (`prefs`, see app.profile.time_prefs) the waking hours are theirs: wake and sleep or quiet
    hours instead of 08:00-21:00, and their morning instead of 09:00. The delay never lands in quiet hours.
    """
    delay = settings.reminder_default_delay_seconds
    if delay <= 0:
        return None
    zone = ZoneInfo(zone or settings.limit_timezone)
    local = now.astimezone(zone)
    if prefs:
        if awake_at(local, prefs):
            target = local + timedelta(seconds=delay)
            if target.second or target.microsecond:
                target = target.replace(second=0, microsecond=0) + timedelta(minutes=1)
            return target if awake_at(target, prefs) else next_awake(target, prefs)
        return next_awake(local, prefs)
    if DAY_START <= local.hour < DAY_END:
        target = local + timedelta(seconds=delay)
        if target.second or target.microsecond:
            target = target.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return target
    day = local.date() + timedelta(days=1 if local.hour >= DAY_END else 0)
    return datetime.combine(day, clock(MORNING_HOUR, 0), tzinfo=zone)


def reminder_requests(db, note, original):
    """Explicit requests of the original, sentence by sentence, with the task and reminder that cover each.

    Each entry is a dict with sentence, match, task (an open task whose quote touches the sentence, or None)
    and covered (that task already has a reminder, even a cancelled one, so it is never made again).
    """
    items = db.scalars(select(Item).where(
        Item.note_id == note.id, Item.user_id == note.user_id, Item.deleted_at.is_(None),
    ).order_by(Item.position, Item.id)).all()
    taken = set(db.scalars(select(Reminder.item_id).where(
        Reminder.note_id == note.id, Reminder.user_id == note.user_id, Reminder.item_id.is_not(None),
    )))
    located = []
    for item in items:
        at = original.find(item.source_quote) if item.source_quote else -1
        if at >= 0:
            located.append((item, at, at + len(item.source_quote)))
    found = []
    for start, end in sentence_spans(original):
        sentence = original[start:end]
        match = REQUEST.search(sentence)
        if match is None:
            continue
        touching = [item for item, a, b in located if a < end and start < b]
        task = next((i for i in touching if i.kind == "task" and i.status == "open"), None)
        covered = any(i.id in taken for i in touching if i.kind == "task")
        found.append({"sentence": sentence, "match": match, "task": task, "covered": covered})
    return found


def request_reminders(db, note, original, settings=None, now=None, model_times=None, model_durations=None):
    """Make sure every explicit «напомни ...» of the original has a reminder. No AI call is made.

    The time is read from the task's quoted deadline first (with the model's `due_at` from `model_times`, see
    `app.time_check.decide`) and from the whole sentence second. A sentence without any time gets the default
    time, marked `ask`. A sentence that names a time nobody could read (already past, for example) gets the
    default time too, marked `unclear`, so the person is asked instead of left without a reminder. Only when
    the default time is switched off (delay 0) such a sentence stays without a reminder and is reported by
    `unscheduled_requests`. When the model made no task for the sentence, a task is added with the sentence as
    its quote. Idempotent through the `ai:<item id>` key. The caller owns the transaction and the account lock.
    Returns the created reminders.
    """
    settings = settings or Settings()
    now = now or datetime.now(UTC)
    user = db.get(User, note.user_id)
    zone, guessed = effective_zone(user, settings), zone_guessed(user)
    local_now = now.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    prefs = time_prefs(db, note.user_id)
    dayparts = prefs["dayparts"] if prefs else None
    created = []
    for request in reminder_requests(db, note, original):
        if request["covered"]:
            continue
        sentence, task = request["sentence"], request["task"]
        reading = Reading(None)
        if task and task.due_text:
            reading = decide(task.due_text, (model_times or {}).get(task.id), local_now, dayparts)
        if reading.when is None:
            reading = decide(sentence, None, local_now, dayparts)
        when, alt = reading_instants(reading, zone, now.timestamp())
        check = reading.check
        if when is None:
            named = bool(task and task.due_text) or mentions_time(sentence, dayparts)
            default = default_reminder_time(now, settings, zone, prefs)
            if default is None or default.timestamp() <= now.timestamp():
                continue
            when, alt, check = default.timestamp(), None, UNCLEAR if named else ASK
        if task is None:
            count = db.scalar(select(func.count()).select_from(Item).where(
                Item.note_id == note.id, Item.deleted_at.is_(None),
            ))
            if count >= MAX_ITEMS:
                continue
            position = db.scalar(select(func.max(Item.position)).where(Item.note_id == note.id))
            task = Item(
                user_id=note.user_id, note_id=note.id, kind="task", status="open", version=1,
                text=request_task_text(sentence, request["match"]), source_quote=sentence,
                due_text=None, due_at=None, position=0 if position is None else position + 1,
            )
            db.add(task)
            db.flush()
        text = " ".join(task.text.split())[:MAX_REMINDER_TEXT].strip()
        length = (model_durations or {}).get(task.id) or explicit_duration(sentence)
        row = add_auto_reminder(db, note, task, text, when, zone, guessed, check, alt if check == ASK else None, length)
        if row is not None:
            created.append(row)
            sync_advance(db, row, now.timestamp())
    return created


def unscheduled_requests(db, note):
    """Explicit requests that ended without a reminder, for the «Не понял, когда напомнить» line."""
    capture = db.get(Capture, note.capture_id)
    original = (capture.original_text or capture.transcript or "") if capture is not None else ""
    if not original:
        return []
    return [r["sentence"] for r in reminder_requests(db, note, original) if not r["covered"]]


def cancel_reminder(db, user_id, reminder_id, generation):
    lock_account(db, user_id)
    row = owned_reminder(db, reminder_id, user_id)
    if row.status == "cancelled" and row.generation in {generation, generation + 1}:
        return row
    if row.generation != generation:
        raise HTTPException(409, "Напоминание уже изменено. Обновите его")
    cancel_reminder_deliveries(db, user_id, [row.id])
    row.status = "cancelled"
    row.generation += 1
    record_event(db, user_id, "reminder_cancelled", f"{row.id}:{row.generation}")
    sync_advance(db, row)
    return row
