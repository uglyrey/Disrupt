"""How a note reads, and how long a calendar event lasts (docs/note-style-v1.md).

The prompt rules for the structuring model live here (`STYLE_PROMPT`) and are pasted into `providers.SYSTEM_PROMPT`.
The same module holds the deterministic safety net that runs on whatever the model returned: it strips reminder
verbs and talk to the system from titles, tasks and the note text, renders the «Напоминание: …» line from the real
reminder data, and reads or estimates the length of an event. No model is called here.
"""

import re

from app.reminder_proposals import MONTH_NAMES

MIN_MINUTES, MAX_MINUTES = 5, 720
DEFAULT_MINUTES = 60
MINUTE_CHOICES = (15, 30, 60, 90, 120)  # What the person can pick as their default in the profile.
REMINDER_PREFIX = "Напоминание"
TITLE_CHARS = 60

STYLE_PROMPT = """Пиши заметку от первого лица, как мысль самого человека: естественно, коротко, не дословно.
Не пиши о человеке в третьем лице («пользователь решил», «автор хочет»).
Убирай «ну», «э», повторы и обращения к системе. Смысл, имена, названия, числа и термины сохраняй его словами.
Никогда не упоминай ИИ, бота и приложение. Не оставляй просьб к системе: «записать», «запиши», «сказать ИИ», «напомни».
Напоминание описывает событие, а не просьбу. title и text задачи без слов «напомнить», «напомни», «не забыть напомнить»
и без времени: время идёт только в due_text, due_at, строку «Напоминание: …» сервер добавит сам. Несколько дел это несколько задач.
duration_minutes у задачи со сроком: длина события, целое 5..720. Названную длину или конец бери точно
(«с 14 до 16» это 120, «на два часа» это 120). Иначе оцени: короткое дело 15, тренировка 60, встреча, пара, созвон 60-90.
Не уверен или срока нет: null.
Примеры, было и стало.
«Запиши, что я решил перейти на новую CRM» даёт «Решил перейти на новую CRM.»
«Скажи ИИ, что надо подумать про цены на тариф» даёт «Подумать про цены на тариф.»
«Пользователь хочет купить молоко» даёт «Купить молоко.»
«Завтра в 10 напомни позвонить маме» даёт title «Позвонить маме», task «Позвонить маме», due_text «Завтра в 10», duration_minutes 15.
«Напомни в пятницу в 14 про встречу с Лидой на два часа» даёт task «Встреча с Лидой», due_text «в пятницу в 14», duration_minutes 120.
«Напомни завтра в 9 про тренировку, потом в 18 про созвон» даёт две задачи «Тренировка» (60) и «Созвон» (60).
"""

# ---- Words to strip ----

_REMINDER_WORDS = re.compile(
    r"(?:не\s+забыть\s+|не\s+забудь(?:те)?\s+)?\bнапомни(?:ть|те)?\b(?:\s+мне)?(?:\s+(?:что|чтобы|о\s+том,?\s+что))?"
    r"|\bremind\s+me(?:\s+to)?\b"
    r"|\b(?:постав\w*|созда\w*|сдела\w*|добав\w*)\s+(?:мне\s+)?напоминани[еяю]\b(?:\s+(?:о\s+том,?\s+что|что|чтобы))?",
    re.IGNORECASE,
)
_SYSTEM_TALK = re.compile(
    r"\b(?:скажи|сказать|передай|передать|спроси|спросить)\s+(?:ии|ai|боту|бересте)\b[\s,]*(?:что|чтобы)?"
    r"|\b(?:запиши(?:те)?|записать)\b(?=[\s,]+(?:что|чтобы|мне|пожалуйста|в\s+заметк|на\s+\d|на\s+(?:сегодня|завтра|утро|вечер)))"
    r"(?:\s+(?:мне|пожалуйста|в\s+заметки?))*[\s,]*(?:что|чтобы)?",
    re.IGNORECASE,
)
_FILLER = re.compile(r"\b(?:пожалуйста|плиз|please)\b|^(?:что|чтобы|to)\s+", re.IGNORECASE)

_DAY = r"(?:сегодня|завтра|послезавтра|в\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье))"
_AT = r"(?:в\s+)?\d{1,2}(?::\d{2})?(?:\s+(?:утра|вечера|дня|ночи|часов|часа|час))?"
TIME_PHRASE = re.compile(
    rf"\b{_DAY}(?:\s+(?:утром|днём|днем|вечером))?(?:\s+в\s+\d{{1,2}}(?::\d{{2}})?(?:\s+(?:утра|вечера|дня|ночи|часов))?)?"
    r"|\bчерез\s+(?:\d+|час|полчаса|пару\s+минут|пару\s+часов)(?:\s+(?:минут\w*|часа|часов|час|дня|дней|день))?"
    rf"|\bв\s+\d{{1,2}}(?::\d{{2}})?(?:\s+(?:утра|вечера|дня|ночи|часов))?(?!\d)",
    re.IGNORECASE,
)


def _tidy(text):
    text = " ".join(text.split())
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"^[\s,.;:!?\-–—]+", "", text)
    text = re.sub(r"[\s,;:\-–—]+$", "", text)
    return text


def capital(text):
    return text[:1].upper() + text[1:]


def has_reminder_words(text):
    return bool(_REMINDER_WORDS.search(text or ""))


def has_system_talk(text):
    return bool(_SYSTEM_TALK.search(text or ""))


def strip_reminder_words(text, due_text=None):
    """The event without the request to be reminded and without its quoted time.

    «Завтра в 10 напомни позвонить маме» with `due_text` «Завтра в 10» gives «Позвонить маме»; without `due_text`
    the time stays. Returns the text unchanged when nothing would be left."""
    if not text:
        return text
    cleaned = _REMINDER_WORDS.sub(" ", text)
    if due_text and due_text.strip():
        cleaned = re.sub(re.escape(due_text.strip()), " ", cleaned, count=1, flags=re.IGNORECASE)
    cleaned = _FILLER.sub(" ", _tidy(cleaned))
    cleaned = _tidy(cleaned)
    return capital(cleaned) if cleaned else text


def strip_time_phrase(text):
    """The text without its first spoken time («завтра в 10», «через час»). Unchanged when nothing would be left."""
    cleaned = _tidy(TIME_PHRASE.sub(" ", text, count=1))
    return cleaned if cleaned else text


def strip_system_talk(text):
    """Drop «запиши», «сказать ИИ» and the like. Unchanged when nothing would be left."""
    if not text or not has_system_talk(text):
        return text
    cleaned = _tidy(_FILLER.sub(" ", _SYSTEM_TALK.sub(" ", text)))
    return capital(cleaned) if cleaned else text


def clean_line(line, due_texts=()):
    """One line of the note body: reminder verbs, the request to the system and the quoted times of reminders out."""
    if not line.strip():
        return line
    marker = re.match(r"^(\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|#{1,6}\s+)?)(.*)$", line)
    prefix, body = marker.group(1), marker.group(2)
    if not has_reminder_words(body) and not has_system_talk(body):
        return line
    trailing = body[-1] if body and body[-1] in ".!?" else ""
    due = next((d for d in due_texts if d and re.search(re.escape(d), body, re.IGNORECASE)), None)
    body = strip_reminder_words(strip_system_talk(body), due)
    return prefix + body.rstrip(".!?") + (trailing or ("." if not prefix.startswith("#") else ""))


def clean_title(title, due_texts=()):
    title = strip_system_talk(title)
    due = next((d for d in due_texts if d and re.search(re.escape(d), title, re.IGNORECASE)), None)
    if has_reminder_words(title):
        title = strip_reminder_words(title, due)
    title = title.rstrip(".")
    if len(title) > 200:
        title = title[:199].rstrip() + "…"
    return title


def restyle(result):
    """A structured note with the reminder verbs and the talk to the system stripped from title, text and tasks.

    The quotes (`source_quote`) and everything else stay as the model gave them: the originals are not touched."""
    due_texts = [item.due_text for item in result.items if item.due_text]
    items = []
    for item in result.items:
        if item.kind == "task" and (has_reminder_words(item.text) or has_system_talk(item.text)):
            text = strip_reminder_words(strip_system_talk(item.text), item.due_text)
            item = item.model_copy(update={"text": text.rstrip(".")[:1500] or text[:1500]})
        items.append(item)
    markdown = "\n".join(clean_line(line, due_texts) for line in result.markdown.split("\n"))
    changes = {"title": clean_title(result.title, due_texts) or result.title, "markdown": markdown.strip() or result.markdown,
               "items": items}
    cleaned = result.model_copy(update=changes)
    cleaned._grounding_details_omitted = result._grounding_details_omitted
    return cleaned


# ---- «Напоминание: …» ----


WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def human_when(local_time, year):
    """«пн, 3 ноября, 10:00». The date is absolute, so the line does not go stale when the note is read later.

    `local_time` is a naive local datetime of the person, `year` the year of the moment the line is written: the
    year is added only when it differs («пн, 3 ноября 2027, 10:00»)."""
    day = f"{local_time.day} {MONTH_NAMES[local_time.month]}" + (f" {local_time.year}" if local_time.year != year else "")
    return f"{WEEKDAY_SHORT[local_time.weekday()]}, {day}, {local_time:%H:%M}"


NAMED_PREFIX = REMINDER_PREFIX + " «"


def reminder_line(event, when, *, named):
    if named:
        return f"{REMINDER_PREFIX} «{capital(event)}»: {when}"
    return f"{REMINDER_PREFIX}: {when}"


def line_pattern(line):
    """A whole line of the note that is exactly `line`, with the two trailing spaces of a Markdown line break allowed."""
    return re.compile(r"(?m)^" + re.escape(line) + r"( {2})?$")


def swap_reminder_line(markdown, old, new):
    """`markdown` with the line `old` replaced by `new`, or None when `old` is not there as a whole line (edited or removed)."""
    found = line_pattern(old).search(markdown)
    if found is None:
        return None
    return markdown[:found.start()] + new + (found.group(1) or "") + markdown[found.end():]


def add_reminder_line(markdown, line, limit=24000):
    """`markdown` with the line added at the end (in the same block as an earlier reminder line), or None if it does not fit."""
    last = markdown.rstrip().rsplit("\n", 1)[-1]
    glue = "  \n" if last.startswith(REMINDER_PREFIX) else "\n\n"
    joined = markdown.rstrip() + glue + line
    return joined if len(joined) <= limit else None


def with_reminder_lines(markdown, entries, limit=24000):
    """The note text with one «Напоминание: …» line per event added at the end.

    `entries` is a list of (event, when) in time order. With one event the line is «Напоминание: завтра, 10:00»,
    with several every line names its event. A line that is already in the text is not added twice."""
    if not entries:
        return markdown
    named = len(entries) > 1
    lines = [reminder_line(event, when, named=named) for event, when in entries]
    lines = [line for line in lines if line not in markdown]
    if not lines:
        return markdown
    joined = markdown.rstrip() + "\n\n" + "  \n".join(lines)
    return joined if len(joined) <= limit else markdown


# ---- Event length ----

_WORD_NUMBERS = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
                 "восемь": 8, "девять": 9, "десять": 10, "двенадцать": 12}
_NUMBER = r"(\d+(?:[.,]\d+)?|" + "|".join(_WORD_NUMBERS) + r")"
_RANGE = re.compile(r"\bс\s+(\d{1,2})(?::(\d{2}))?\s*(?:до|по)\s+(\d{1,2})(?::(\d{2}))?\b", re.IGNORECASE)
_HOURS = re.compile(rf"\bна\s+{_NUMBER}\s+(?:час(?:а|ов)?)\b(?!\s+(?:утра|вечера|дня|ночи))", re.IGNORECASE)
_MINUTES = re.compile(rf"\bна\s+{_NUMBER}\s+мин\w*", re.IGNORECASE)
_ONE_HOUR = re.compile(r"\bна\s+час\b", re.IGNORECASE)
_HALF_HOUR = re.compile(r"\bна\s+полчаса\b", re.IGNORECASE)
_ONE_AND_HALF = re.compile(r"\bна\s+полтора\s+часа\b", re.IGNORECASE)


def strip_duration_phrase(text):
    """The text without a spoken length («на два часа», «с 14 до 16»). Unchanged when nothing would be left."""
    cleaned = text
    for pattern in (_RANGE, _HOURS, _MINUTES, _ONE_AND_HALF, _HALF_HOUR, _ONE_HOUR):
        cleaned = pattern.sub(" ", cleaned, count=1)
    cleaned = _tidy(cleaned)
    return cleaned if cleaned else text


def valid_minutes(value):
    """The length in minutes if it is a whole number from 5 to 720, otherwise None. Never raises."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and MIN_MINUTES <= value <= MAX_MINUTES:
        return value
    return None


def _number(raw):
    raw = raw.casefold()
    return _WORD_NUMBERS[raw] if raw in _WORD_NUMBERS else float(raw.replace(",", "."))


def explicit_duration(text):
    """A length the person named («с 14 до 16», «на два часа», «на 30 минут», «на час»), in minutes, or None."""
    if not text:
        return None
    found = _RANGE.search(text)
    if found:
        start = int(found.group(1)) * 60 + int(found.group(2) or 0)
        end = int(found.group(3)) * 60 + int(found.group(4) or 0)
        if end <= start:
            end += 24 * 60  # Over midnight: «с 22 до 2».
        if int(found.group(1)) < 24 and int(found.group(3)) < 24 and end > start:
            return valid_minutes(end - start)
    found = _HOURS.search(text)
    if found:
        return valid_minutes(round(_number(found.group(1)) * 60))
    found = _MINUTES.search(text)
    if found:
        return valid_minutes(round(_number(found.group(1))))
    if _ONE_AND_HALF.search(text):
        return 90
    if _HALF_HOUR.search(text):
        return 30
    if _ONE_HOUR.search(text):
        return 60
    return None


_ESTIMATES = (
    (re.compile(r"пар[аыу]\b|лекци|семинар|практик"), 90),
    (re.compile(r"созвон|встреч|совещан|планёрк|планерк|собеседован|консультац|переговор|презентац"), 60),
    (re.compile(r"тренировк|спортзал|йог|пробежк|плаван"), 60),
    (re.compile(r"врач|стоматолог|приём у|прием у|massage|массаж"), 60),
    (re.compile(r"позвони|звонок|написа|отправи|оплати|купи|выпить|принять таблет"), 15),
)


def estimate_duration(text):
    """A rough length for an event by its words (used by the offline mock; the real model estimates by itself)."""
    lowered = (text or "").casefold()
    return next((minutes for pattern, minutes in _ESTIMATES if pattern.search(lowered)), None)


def duration_of(item_duration, quote):
    """The length stored for a reminder: the model's value, else a length spelled out in the quote, else None."""
    return valid_minutes(item_duration) or explicit_duration(quote)
