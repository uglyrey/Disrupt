"""Личный профиль «Знакомство»: ответы человека, выводы ИИ и то, что из них берёт разбор заметок.

API-процесс только хранит ответы и ставит профиль в очередь (`state = pending`). Выводы делает воркер
(`app/profile_jobs.py`). Здесь же проверка ответа модели: всё лишнее и чувствительное отбрасывается без ошибки.
Ответы, выводы и подсказки лежат в базе зашифрованными и в админке не читаются. См. docs/profile-v1.md.
"""

import hashlib
import json
import re
import time

from fastapi import HTTPException
from sqlalchemy import select

from app.limits import PROFILE_UNIT_COST, day_window, within_daily_limit
from app.models import Category, User, UserProfile
from app.profile_questions import INTRO, PRIVACY, QUESTION_IDS, question_views

NEW, DECLINED, PENDING, READY, FAILED = "new", "declined", "pending", "ready", "failed"
LIMIT_CODE = "daily_limit"

MAX_FACTS = 12
MAX_FACT_CHARS = 200
MAX_VOCABULARY = 30
MAX_TERM_CHARS = 60
MAX_MEANING_CHARS = 200
DAYPART_KEYS = ("morning", "day", "evening", "after_study", "after_work", "weekend")
REMINDER_STYLES = ("once", "twice")
MAX_SUGGESTED = 6
MAX_NEW_CATEGORIES = 10
NOTE_STYLES = ("short", "detailed")
LIMIT_MESSAGE = "На сегодня лимит ИИ закончился. Он обновится завтра."

ADVANCE_SECONDS = 3600  # «twice»: one more reminder an hour before the main one

_CLOCK = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def lock_account(db, user_id):
    """The account lock of every write (services.py imports this module, so the import waits for the call)."""
    from app.services import lock_account as lock

    lock(db, user_id)


class ProfileLimitReached(Exception):
    def __init__(self, resets_at):
        self.resets_at = resets_at
        super().__init__(LIMIT_MESSAGE)


# ---- Малые помощники ----

def one_line(value, limit):
    """Строка без управляющих символов и лишних пробелов, не длиннее limit. Иначе None."""
    if not isinstance(value, str) or _CONTROL.search(value):
        return None
    text = " ".join(value.split())
    return text if 1 <= len(text) <= limit else None


def parse_clock(value):
    """«HH:MM» как (час, минута) или None."""
    match = _CLOCK.fullmatch(value) if isinstance(value, str) else None
    return (int(match[1]), int(match[2])) if match else None


def normalize_answers(answers):
    """Только известные вопросы с непустым ответом, текст без лишних пробелов по краям."""
    return {qid: answers[qid].strip() for qid in QUESTION_IDS if qid in answers and answers[qid].strip()}


def answers_hash(answers):
    return hashlib.sha256(json.dumps(answers, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# Чувствительные темы. Главная защита это правило в запросе к модели, это вторая линия: такие пункты не сохраняются.
SENSITIVE = re.compile(
    r"\b(?:"
    r"диагноз|заболеван|болезн|болен|больн[аы]|лечени|лечит|лекарств|таблетк|депресс|тревожн|психиатр|психотерап"
    r"|расстройств|инвалид|беремен|аллерги|диабет|онколог|инфаркт|терапи[ия]"
    r"|религ|верующ|церков|молитв|православ|мусульман|христиан|иудей|буддист|атеист|ислам|храм"
    r"|политик|политич|оппозиц|митинг|партия|партии|партию|партией|выборы|выборах|голосует|голосуе|вероисповед|верит в|верю в"
    r"|гомосекс|(?:бог|бога|богу|богом|боге)\b"
    r"|сексуал|ориентаци|лгбт|лесби|бисексуал|трансгендер|секс"
    r"|кредит|ипотек|зарплат|налог|банкрот|финансов|сбережен|накоплен"
    r"|(?:гей|геи|геев|долг|долги|долгов|долгами|доход|дохода|доходы|доходов)\b"
    r")",
    re.IGNORECASE,
)


def sensitive(*texts):
    return any(SENSITIVE.search(text.replace("ё", "е").replace("Ё", "Е")) for text in texts if isinstance(text, str))


def clean_facts(raw):
    """Выводы модели: не больше 12, каждый до 200 знаков, без чувствительных тем и повторов."""
    if not isinstance(raw, list):
        return []
    found, seen = [], set()
    for entry in raw:
        text = one_line(entry, MAX_FACT_CHARS)
        if text is None or sensitive(text) or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        found.append(text)
        if len(found) == MAX_FACTS:
            break
    return found


def clean_vocabulary(raw):
    if not isinstance(raw, list):
        return []
    found, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"term", "meaning"}:
            continue
        term, meaning = one_line(entry["term"], MAX_TERM_CHARS), one_line(entry["meaning"], MAX_MEANING_CHARS)
        if term is None or meaning is None or sensitive(term, meaning) or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        found.append({"term": term, "meaning": meaning})
        if len(found) == MAX_VOCABULARY:
            break
    return found


def clean_categories(raw):
    """Names of categories the model suggests: up to 6, short, plain, no repeats."""
    from app.services import category_key, clean_ai_category_name  # services.py imports this module

    if not isinstance(raw, list):
        return []
    found, seen = [], set()
    for entry in raw:
        name = clean_ai_category_name(entry)
        if name is None or sensitive(name) or category_key(name) in seen or category_key(name) == category_key("Общее"):
            continue
        seen.add(category_key(name))
        found.append(name)
        if len(found) == MAX_SUGGESTED:
            break
    return found


def clean_clock(value):
    return value if parse_clock(value) else None


def clean_prefs(raw):
    """Подсказки из ответа модели. Неверная часть становится пустой, остальное остаётся."""
    raw = raw if isinstance(raw, dict) else {}
    dayparts = raw.get("dayparts")
    dayparts = dayparts if isinstance(dayparts, dict) else {}
    quiet = raw.get("quiet_hours")
    quiet = (
        {"from": quiet["from"], "to": quiet["to"]}
        if isinstance(quiet, dict) and set(quiet) == {"from", "to"} and parse_clock(quiet["from"])
        and parse_clock(quiet["to"]) and quiet["from"] != quiet["to"] else None
    )
    style, note = raw.get("reminder_style"), raw.get("note_style")
    return {
        "dayparts": {key: dayparts[key] for key in DAYPART_KEYS if clean_clock(dayparts.get(key))},
        "quiet_hours": quiet,
        "wake": clean_clock(raw.get("wake")),
        "sleep": clean_clock(raw.get("sleep")),
        "reminder_style": style if style in REMINDER_STYLES else None,
        "note_style": note if note in NOTE_STYLES else None,
        "vocabulary": clean_vocabulary(raw.get("vocabulary")),
        "suggested_categories": clean_categories(raw.get("suggested_categories")),
    }


def clean_profile(raw):
    """(facts, prefs) из ответа модели. Не словарь это негодный ответ: ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("profile answer is not an object")
    return clean_facts(raw.get("facts")), clean_prefs(raw)


def output_texts(facts, prefs):
    """Все тексты, которые модель придумала, для проверки OutputGuard."""
    texts = list(facts)
    for entry in prefs["vocabulary"]:
        texts += [entry["term"], entry["meaning"]]
    return texts + prefs["suggested_categories"]


def fact_rows(texts):
    return [{"id": f"f{number}", "text": text, "source": "ai"} for number, text in enumerate(texts, 1)]


# ---- Чтение ----

def get_row(db, user_id):
    return db.get(UserProfile, user_id)


def state_of(row):
    return row.state if row is not None else NEW


def profile_state(db, user_id):
    return state_of(get_row(db, user_id))


def error_message(code):
    from app.assistant import DEFAULT_ERROR_MESSAGE, ERROR_MESSAGES  # assistant.py reads the profile too

    if code == LIMIT_CODE:
        return LIMIT_MESSAGE
    return ERROR_MESSAGES.get(code, DEFAULT_ERROR_MESSAGE) if code else None


def profile_view(db, user):
    row = get_row(db, user.id)
    state = state_of(row)
    return {
        "state": state,
        "questions": question_views(),
        "answers": row.answers if row is not None and row.state != DECLINED and row.answers else None,
        "facts": [{"id": f["id"], "text": f["text"], "source": f.get("source", "ai")} for f in (row.facts or [])]
        if row is not None else [],
        "updated_at": int(row.updated_at) if row is not None else None,
        "calendar_offer": user.calendar_offer,
        "suggested_categories": suggested_categories(row),
        "categories_confirmed": bool(row.categories_confirmed) if row is not None else False,
        "intro": INTRO,
        "privacy": PRIVACY,
        "error": error_message(row.error_code) if row is not None and row.state == FAILED else None,
    }


def suggested_categories(row):
    if row is None or row.state != READY or not row.prefs:
        return []
    return list(row.prefs.get("suggested_categories") or [])


def prompt_profile(db, user_id):
    """Что видит модель при разборе заметок: выводы и подсказки, но не сами ответы. None, если профиля нет."""
    row = get_row(db, user_id)
    if row is None or row.state != READY:
        return None
    prefs = row.prefs or {}
    profile = {}
    facts = [fact["text"] for fact in row.facts or []]
    if facts:
        profile["facts"] = facts
    if prefs.get("vocabulary"):
        profile["vocabulary"] = prefs["vocabulary"]
    if prefs.get("note_style"):
        profile["note_style"] = prefs["note_style"]
    if prefs.get("dayparts"):
        profile["dayparts"] = prefs["dayparts"]
    return profile or None


def time_prefs(db, user_id):
    """Личные часы для разбора времени или None. Значения уже числа: {"dayparts": {ключ: (час, минута)}, ...}."""
    row = get_row(db, user_id)
    if row is None or row.state != READY or not row.prefs:
        return None
    prefs = row.prefs
    dayparts = {key: parse_clock(value) for key, value in (prefs.get("dayparts") or {}).items()}
    quiet = prefs.get("quiet_hours")
    found = {
        "dayparts": {key: value for key, value in dayparts.items() if value and key in DAYPART_KEYS},
        "quiet": (parse_clock(quiet["from"]), parse_clock(quiet["to"])) if quiet else None,
        "wake": parse_clock(prefs.get("wake")),
        "sleep": parse_clock(prefs.get("sleep")),
        "style": prefs.get("reminder_style"),
    }
    return found if found["dayparts"] or found["quiet"] or found["wake"] or found["sleep"] or found["style"] else None


def time_dayparts(db, user_id):
    """Только личное время слов «утром», «после пар» и так далее, для парсера. None, если его нет."""
    prefs = time_prefs(db, user_id)
    return prefs["dayparts"] if prefs and prefs["dayparts"] else None


def prompt_dayparts(db, user_id):
    """То же для запроса модели о времени: {"dayparts": {...}} или None."""
    row = get_row(db, user_id)
    if row is None or row.state != READY or not row.prefs or not row.prefs.get("dayparts"):
        return None
    return {"dayparts": row.prefs["dayparts"]}


# ---- Запись ----

def touch(row, now=None):
    row.updated_at = time.time() if now is None else now
    row.version = (row.version or 0) + 1


def new_row(db, user_id, state):
    row = UserProfile(user_id=user_id, state=state, updated_at=time.time(), version=1)
    db.add(row)
    return row


def reset_results(row):
    row.facts = None
    row.prefs = None
    row.error_code = None
    row.lease_until = None


def over_limit(db, settings, user_id):
    return not within_daily_limit(db, settings, user_id, PROFILE_UNIT_COST)


def queue_row(row, answers, digest):
    row.state, row.answers, row.answers_hash = PENDING, answers, digest
    row.categories_confirmed = False
    reset_results(row)
    touch(row)


def submit_answers(db, settings, user_id, answers):
    """Принять ответы. Возвращает состояние. Тот же ответ не стоит второй единицы.

    Если суточный лимит ИИ исчерпан, ответы сохраняются, состояние `failed`, наружу уходит ProfileLimitReached.
    Пока прошлые ответы разбираются, другие не принимаются (409).
    """
    lock_account(db, user_id)
    digest = answers_hash(answers)
    row = get_row(db, user_id)
    if row is not None and row.answers_hash == digest and row.state in (PENDING, READY):
        return row.state
    if row is not None and row.state == PENDING:
        raise HTTPException(409, "Ответы ещё разбираются. Подождите минуту и попробуйте снова")
    if row is None:
        row = new_row(db, user_id, PENDING)
    if over_limit(db, settings, user_id):
        queue_row(row, answers, digest)
        row.state, row.error_code = FAILED, LIMIT_CODE
        db.commit()
        raise ProfileLimitReached(day_window(settings)[1].isoformat())
    queue_row(row, answers, digest)
    return PENDING


def retry(db, settings, user_id):
    lock_account(db, user_id)
    row = get_row(db, user_id)
    if row is None or row.state != FAILED or not row.answers:
        raise HTTPException(409, "Повторять нечего: ответы уже разобраны или их нет")
    if over_limit(db, settings, user_id):
        row.error_code = LIMIT_CODE
        touch(row)
        db.commit()
        raise ProfileLimitReached(day_window(settings)[1].isoformat())
    queue_row(row, row.answers, row.answers_hash)
    return PENDING


def decline(db, user_id):
    """Человек отказался: окно больше не появляется. Уже собранный профиль так не стирается."""
    lock_account(db, user_id)
    row = get_row(db, user_id)
    if row is not None and row.state in (READY, PENDING):
        raise HTTPException(409, "Знакомство уже пройдено. Удалить данные можно в разделе «Что beresta знает обо мне»")
    if row is None:
        row = new_row(db, user_id, DECLINED)
    wipe(row)
    return DECLINED


def wipe(row):
    """Удалить ответы и выводы. Состояние `declined`: окно после этого не возвращается."""
    row.state, row.answers, row.answers_hash = DECLINED, None, None
    row.categories_confirmed = False
    reset_results(row)
    touch(row)


def delete_profile(db, user_id):
    lock_account(db, user_id)
    row = get_row(db, user_id)
    if row is None:
        row = new_row(db, user_id, DECLINED)
    wipe(row)


def erase_profile(db, user_id):
    """Строка профиля целиком, для удаления аккаунта оператором (docs/profile-v1.md)."""
    row = get_row(db, user_id)
    if row is not None:
        db.delete(row)


def owned_fact(row, fact_id):
    fact = next((f for f in (row.facts or []) if f["id"] == fact_id), None) if row is not None else None
    if fact is None:
        raise HTTPException(404, "Пункт не найден")
    return fact


def edit_fact(db, user_id, fact_id, text):
    lock_account(db, user_id)
    row = get_row(db, user_id)
    owned_fact(row, fact_id)
    clean = one_line(text, MAX_FACT_CHARS)
    if clean is None:
        raise HTTPException(422, f"Пункт должен быть от 1 до {MAX_FACT_CHARS} знаков, без спецсимволов")
    row.facts = [{**f, "text": clean, "source": "edited"} if f["id"] == fact_id else f for f in row.facts]
    touch(row)


def delete_fact(db, user_id, fact_id):
    lock_account(db, user_id)
    row = get_row(db, user_id)
    owned_fact(row, fact_id)
    row.facts = [f for f in row.facts if f["id"] != fact_id]
    touch(row)


def set_calendar_offer(db, user_id, value):
    lock_account(db, user_id)
    db.get(User, user_id).calendar_offer = value


def create_categories(db, user_id, names):
    """Create the categories the person confirmed. Names that exist (any case, ё/е) are not made twice.

    Allowed once the profile is ready. The normal category rules apply (100 at most). Returns the created names.
    """
    from app.services import category_key, ensure_category  # services.py imports this module

    lock_account(db, user_id)
    row = get_row(db, user_id)
    if row is None or row.state != READY:
        raise HTTPException(409, "Категории можно создать, когда профиль готов")
    if row.categories_confirmed:
        return []  # The same request again (a retry after a lost answer): nothing more to do.
    wanted, seen = [], set()
    for name in names:
        clean = one_line(name, 100)
        if clean is None or category_key(clean) in seen:
            continue
        seen.add(category_key(clean))
        wanted.append(clean[:1].upper() + clean[1:])
    if not wanted or len(wanted) > MAX_NEW_CATEGORIES:
        raise HTTPException(422, f"Выберите от 1 до {MAX_NEW_CATEGORIES} категорий")
    existing = {category_key(c.name) for c in db.scalars(select(Category).where(Category.user_id == user_id))}
    created = []
    for name in wanted:
        if category_key(name) in existing:
            continue
        ensure_category(db, user_id, name)
        created.append(name)
    row.categories_confirmed = True
    touch(row)
    return created


def skip_categories(db, user_id):
    lock_account(db, user_id)
    row = get_row(db, user_id)
    if row is None or row.state != READY:
        raise HTTPException(409, "Предложения категорий сейчас нет")
    row.categories_confirmed = True
    touch(row)
