"""Deterministic Russian deadline parser. It turns the quoted `due_text` of a task into a proposed
reminder time. No AI is involved, nothing is stored and nothing is sent: the user confirms the time
through the existing reminder form.

Rules in short.
- A date without time means 09:00. Dayparts: утром 09:00, днём 13:00, вечером 19:00.
- An explicit day (сегодня, завтра, weekday, date) that is already past gives None.
- A bare time or daypart ("в 10", "вечером") that is already past rolls to tomorrow.
- A weekday means the nearest such day after today, never today itself.
- «к обеду» 13:00, «после обеда» 15:00, «на выходных» the nearest Saturday or Sunday 10:00,
  «в конце недели» the nearest Friday 18:00. A spoken «в 14.30» and «в 9 часов 30 минут» are clock times.
- A date without year that passed more than 60 days ago means next year, a recent one gives None.
- Spoken times in words and colloquial forms are read too, see docs/time-parsing-v2.md. A spoken time is never
  dropped: if it cannot be read, `parse_due_text_detailed` says `time_spoken` and gives no result instead of 09:00.
- An hour 1-11 with no daypart and no 24h form («в четыре», «в 4:30») has two readings and is `certain: False`.
"""

import calendar
import re
from datetime import UTC, datetime, time, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

DEFAULT_HOUR = 9
DAYPART_HOUR = {"утром": 9, "утра": 9, "утро": 9, "днем": 13, "дня": 13, "вечером": 19, "вечера": 19, "вечер": 19}
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MONTH_NAMES = {number: name for name, number in MONTHS.items()}
WEEKDAYS = {}
for _index, _forms in enumerate([
    "понедельник понедельнику понедельника пн", "вторник вторнику вторника вт",
    "среда среду среде среды ср", "четверг четвергу четверга чт", "пятница пятницу пятнице пятницы пт",
    "суббота субботу субботе субботы сб", "воскресенье воскресенью воскресенья вс",
]):
    for _form in _forms.split():
        WEEKDAYS[_form] = _index
WEEKDAY_LABEL = [
    "в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье",
]
ZERO_WORDS = {"ноль", "нуль"}
UNIT_WORDS = {
    "один": 1, "одна": 1, "одну": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
}
TEEN_WORDS = {
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
    "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
}
TENS_WORDS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
# Forms after «к», «до», «около»: «к четырём», «до шестнадцати», «около двадцати двух».
UNIT_OBLIQUE = {
    "одному": 1, "двум": 2, "двух": 2, "трем": 3, "трех": 3, "четырем": 4, "четырех": 4, "пяти": 5,
    "шести": 6, "семи": 7, "восьми": 8, "девяти": 9,
}
TEEN_OBLIQUE = {
    "десяти": 10, "одиннадцати": 11, "двенадцати": 12, "тринадцати": 13, "четырнадцати": 14,
    "пятнадцати": 15, "шестнадцати": 16, "семнадцати": 17, "восемнадцати": 18, "девятнадцати": 19,
}
TENS_OBLIQUE = {"двадцати": 20, "тридцати": 30, "сорока": 40, "пятидесяти": 50}
OBLIQUE_AFTER = {"к", "до", "около"}
ORDINAL_GENITIVE = {
    "первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5, "шестого": 6, "седьмого": 7,
    "восьмого": 8, "девятого": 9, "десятого": 10, "одиннадцатого": 11, "двенадцатого": 12,
}
MINUTES_BEFORE = {
    "четверти": 15, "пяти": 5, "десяти": 10, "пятнадцати": 15, "двадцати": 20, "двадцати пяти": 25,
}
HOUR_CARDINAL = {**{w: v for w, v in UNIT_WORDS.items() if v > 1}, "десять": 10, "одиннадцать": 11,
                 "двенадцать": 12, "час": 1}
MAX_AHEAD = timedelta(days=366 * 2)

RELATIVE = re.compile(
    r"\bчерез\s+(?:(?P<n>\d{1,3})\s*)?"
    r"(?P<unit>полчаса|пару\s+[а-я]+|мин[а-я]*|час[а-я]*|ден[а-я]*|дн[а-я]*|сутки|суток|недел[а-я]*|месяц[а-я]*)\b"
)
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DOT_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?![\d:])")
SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b")
NAMED_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b")
MONTH_DAY = re.compile(r"\b(\d{1,2})(?:\s*-?\s*го)?\s+числа\b")
CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\b")
HOUR = re.compile(r"\b(?:в|к|до|около|ровно)\s+(\d{1,2})(?:\s*(?:часов|часа|час|ч))?\b(?!\s*\d)")
DAYPART = re.compile(r"\b(утром|утра|утро|днем|дня|вечером|вечера|вечер|ночью|ночи|ночь)\b")
NOON_MIDNIGHT = re.compile(r"\b(?:в|к)\s+(полдень|полночь)\b")
PAST_DAY = re.compile(r"\b(?:вчера|позавчера)\b")
LUNCH = re.compile(r"\b(?:(?:к|в)\s+обед[а-я]*|после\s+обеда)\b")
AFTER_LUNCH = re.compile(r"\bпосле\s+обеда\b")
WEEKEND = re.compile(r"\b(?:на\s+выходных|в\s+выходные|на\s+выходные)\b")
WEEK_END = re.compile(r"\b(?:в\s+конце\s+недели|к\s+концу\s+недели|на\s+конец\s+недели)\b")
SPOKEN_DOT_TIME = re.compile(r"\b(в|около|ровно)\s+(\d{1,2})\.([0-5]\d)\b(?!\.\d)")
DAY_WORD = re.compile(r"\b(послезавтра|завтра|сегодня|" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b")
_ORDINALS = "|".join(ORDINAL_GENITIVE)
HALF_PAST = re.compile(r"\b(?:в\s+)?(?:половин[аеуы]\s+|пол\s*)(" + _ORDINALS + r")\b")
QUARTER_PAST = re.compile(r"\b(?:в\s+)?четверть\s+(" + _ORDINALS + r")\b")
MINUTES_TO = re.compile(
    r"\b(?:в\s+)?без\s+(четверти|двадцати\s+пяти|двадцати|пятнадцати|десяти|пяти|\d{1,2})(?:\s+минут[ы]?)?"
    r"\s+(" + "|".join(sorted(HOUR_CARDINAL, key=len, reverse=True)) + r"|\d{1,2})\b"
)
O_CLOCK_ONE = re.compile(r"\b(в|к|около|до|ровно)\s+(?:час|часу)\b(?!\s+пик)")
_ANCHOR = r"(?:в|к|до|около|ровно|" + DAY_WORD.pattern[2:-2].replace("(", "(?:", 1) + "|" + "|".join(MONTHS) + ")"
_NOT_PART_OF_DATE = r"(?!\d|\s*(?:минут|мин\b|числа|дн|" + "|".join(MONTHS) + "))"
ANCHORED_PAIR = re.compile(r"\b(" + _ANCHOR + r")\s+(\d{1,2})\s+(\d{2})\b" + _NOT_PART_OF_DATE)
ANCHORED_DASH = re.compile(r"\b(в|к|до|около|ровно)\s+(\d{1,2})-(\d{2})\b(?!\d|\s+числа)")
HOURS_THEN_MINUTES = re.compile(
    r"\b(в|к|около|до|ровно)\s+(\d{1,2})\s*(?:часов|часа|час|ч)\s+(\d{1,2})(?:\s*(?:минут[а-я]*|мин))?\b"
    + _NOT_PART_OF_DATE
)
# A time that is clearly spoken but was not turned into a clock time. Never ignored silently.
UNREAD_TIME = re.compile(
    r"\b(?:(?:в|к|до|около|ровно)\s+(?:\d{1,2}\b|шестьдесят|семьдесят|восемьдесят|девяносто|сто\b)"
    r"|половин|пол(?:перв|втор|трет|четв|пят|шест|седьм|восьм|девят|десят|одиннадцат|двенадцат)"
    r"|четверть\b|без\s+(?:\d|четверт|пят|десят|двадцат|пятнадц))"
)


def read_number(words, index, oblique):
    """Number words at `index` as (text, consumed). A «ноль»-pair is «05»/«00», or «0:05»/«0:00» in the hour place."""
    word = words[index]
    following = words[index + 1] if index + 1 < len(words) else None
    if word in ZERO_WORDS:
        if following in ZERO_WORDS:
            return "00", 2
        if following in UNIT_WORDS:
            return f"0{UNIT_WORDS[following]}", 2
        return "0", 1
    tens = TENS_WORDS.get(word) or (TENS_OBLIQUE.get(word) if oblique else None)
    if tens:
        unit = UNIT_WORDS.get(following) or (UNIT_OBLIQUE.get(following) if oblique else None)
        return (str(tens + unit), 2) if unit else (str(tens), 1)
    value = UNIT_WORDS.get(word) or TEEN_WORDS.get(word)
    if value is None and oblique:
        value = UNIT_OBLIQUE.get(word) or TEEN_OBLIQUE.get(word)
    return (str(value), 1) if value is not None else (None, 0)


def convert_numbers(words):
    out, index = [], 0
    while index < len(words):
        text, used = read_number(words, index, bool(out) and out[-1] in OBLIQUE_AFTER)
        if not used:
            out.append(words[index])
            index += 1
            continue
        if used == 2 and text[0] == "0" and not (out and out[-1].isdigit()):
            text = "0:" + text  # «ноль пять» in the hour place is 00:05, not 05 o'clock
        out.append(text)
        index += used
    return out


def colloquial_times(text):
    """«половина пятого», «четверть пятого», «без четверти пять» become plain clock times like «в 4:30»."""
    def hour_before(ordinal):
        number = ORDINAL_GENITIVE[ordinal]
        return number - 1 if number > 1 else 12

    text = HALF_PAST.sub(lambda m: f"в {hour_before(m[1])}:30", text)
    text = QUARTER_PAST.sub(lambda m: f"в {hour_before(m[1])}:15", text)

    def minutes_to(match):
        minutes_text = re.sub(r"\s+", " ", match[1])
        minutes = int(minutes_text) if minutes_text.isdigit() else MINUTES_BEFORE[minutes_text]
        hour = HOUR_CARDINAL[match[2]] if match[2] in HOUR_CARDINAL else int(match[2])
        if not (1 <= minutes <= 59 and 1 <= hour <= 24):
            return match[0]
        return f"в {hour - 1 if hour > 1 else 12}:{60 - minutes:02d}"

    return MINUTES_TO.sub(minutes_to, text)


def normalize(value):
    text = value.lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9:./\- ]+", " ", text)
    text = re.sub(r"(?<!\d)[:./-]|[:./-](?!\d)", " ", text)
    text = re.sub(r"(\d)(ч(?:ас[а-я]*)?)\b", r"\1 \2", text)
    text = " ".join(text.split())
    text = colloquial_times(text)
    text = O_CLOCK_ONE.sub(lambda m: f"{m[1]} 1", text)
    text = " ".join(convert_numbers(text.split()))

    def pair(match):
        if int(match[2]) > 23 or int(match[3]) > 59:
            return match[0]
        return f"{match[1]} {match[2]}:{match[3].zfill(2)}"

    text = ANCHORED_DASH.sub(pair, text)
    text = HOURS_THEN_MINUTES.sub(pair, text)
    text = ANCHORED_PAIR.sub(pair, text)
    return SPOKEN_DOT_TIME.sub(lambda m: f"{m[1]} {m[2]}:{m[3]}", text)


def add_months(day, months):
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def safe_date(year, month, day):
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def full_year(value):
    if value is None:
        return None
    year = int(value)
    return year + 2000 if year < 100 else year


def clock_time(hour, minute, daypart):
    """Apply a daypart word to a spoken hour. Returns (hour, minute) or None when out of range."""
    if daypart in {"вечером", "вечера", "вечер"} and 1 <= hour < 12:
        hour += 12
    elif daypart in {"днем", "дня"} and 1 <= hour <= 6:
        hour += 12
    elif daypart in {"ночью", "ночи", "ночь"}:
        if hour == 12:
            hour = 0
        elif 6 <= hour < 12:
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def parse_relative(text, now):
    match = RELATIVE.search(text)
    if not match:
        return None, text
    unit, count = match["unit"], int(match["n"]) if match["n"] else 1
    if unit == "полчаса":
        count, unit = 30, "мин"
    elif unit.startswith("пару"):
        count, unit = 2, unit.split()[1]
    if not 1 <= count <= 400:
        return None, text
    rest = (text[:match.start()] + " " + text[match.end():]).strip()
    if unit.startswith("мин"):
        return now + timedelta(minutes=count), rest
    if unit.startswith("час"):
        return now + timedelta(hours=count), rest
    if unit.startswith(("ден", "дн", "сут")):
        day = now.date() + timedelta(days=count)
    elif unit.startswith("недел"):
        day = now.date() + timedelta(weeks=count)
    elif unit.startswith("месяц"):
        day = add_months(now.date(), count)
    else:
        return None, text
    return day, rest


def find_date(text, today):
    """Return (date or None, found, rest). found is True when a date phrase was present."""
    for pattern, order in ((ISO_DATE, "ymd"), (NAMED_DATE, "named"), (DOT_DATE, "dmy"), (SLASH_DATE, "dmy")):
        match = pattern.search(text)
        if not match:
            continue
        rest = (text[:match.start()] + " " + text[match.end():]).strip()
        if order == "ymd":
            return safe_date(int(match[1]), int(match[2]), int(match[3])), True, rest
        if order == "named":
            day, month, year = int(match[1]), MONTHS[match[2]], full_year(match[3])
        else:
            day, month, year = int(match[1]), int(match[2]), full_year(match[3])
        if year is None:
            date = safe_date(today.year, month, day)
            if date is not None and date < today and (today - date).days > 60:
                date = safe_date(today.year + 1, month, day)
            return date, True, rest
        return safe_date(year, month, day), True, rest
    match = MONTH_DAY.search(text)
    if match:
        rest = (text[:match.start()] + " " + text[match.end():]).strip()
        return int(match[1]), "month_day", rest
    return None, False, text


class Spec(NamedTuple):
    """A time of day found in a phrase. `hour` None with `found` means a spoken time that cannot be used."""
    found: bool
    hour: int | None = None
    minute: int = 0
    daypart: str | None = None
    midnight: bool = False
    ambiguous: bool = False

    def readings(self):
        """Possible hours, the default reading first. One reading unless the hour is ambiguous."""
        if not self.ambiguous:
            return [self.hour]
        morning, evening = self.hour, self.hour + 12
        return [morning, evening] if self.hour >= 7 else [evening, morning]


def read_time(text):
    daypart_match = DAYPART.search(text)
    daypart = daypart_match[1] if daypart_match else None
    special = NOON_MIDNIGHT.search(text)
    if special:
        return Spec(True, 12 if special[1] == "полдень" else 0, 0, daypart, special[1] == "полночь")
    clock = CLOCK.search(text)
    spoken = None if clock else HOUR.search(text)
    if clock or spoken:
        raw, minute = (clock[1], int(clock[2])) if clock else (spoken[1], 0)
        fixed = clock_time(int(raw), minute, daypart)
        if fixed is None:
            return Spec(True, None, 0, daypart)
        leading_zero = len(raw) == 2 and raw[0] == "0"
        ambiguous = daypart is None and 1 <= fixed[0] <= 11 and not leading_zero
        return Spec(True, fixed[0], fixed[1], daypart, False, ambiguous)
    lunch = LUNCH.search(text)
    if lunch:
        return Spec(True, 15 if AFTER_LUNCH.search(text) else 13, 0, daypart)
    if daypart in DAYPART_HOUR:
        return Spec(True, DAYPART_HOUR[daypart], 0, daypart)
    if daypart or UNREAD_TIME.search(text):
        return Spec(True, None, 0, daypart)  # "ночью" without an hour is too vague; unread words are not dropped
    return Spec(False, None, 0, daypart)


def find_time(text):
    """Return (hour, minute, found, daypart, is_midnight). found with hour None means an unusable time."""
    spec = read_time(text)
    hour = spec.readings()[0] if spec.hour is not None else None
    return hour, spec.minute, spec.found, spec.daypart, spec.midnight


SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")


def sentence_spans(text):
    """(start, end) of every non-empty sentence of text, trimmed. A newline also ends a sentence."""
    spans, position = [], 0
    for match in list(SENTENCE_END.finditer(text)) + [None]:
        end = len(text) if match is None else match.start()
        start = position
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            spans.append((start, end))
        if match is not None:
            position = match.end()
    return spans


def nearest_day(now, hour, minute, weekdays):
    """First day, today included, on one of `weekdays` whose time is still ahead."""
    for offset in range(9):
        day = now.date() + timedelta(days=offset)
        if day.weekday() in weekdays and datetime.combine(day, time(hour, minute)) > now:
            return day
    return None


def mentions_time(value):
    """True when the text names some time, usable or not. Tells «no time given» from «time that cannot be used»."""
    if not isinstance(value, str) or not value.strip():
        return False
    text = normalize(value)
    patterns = (RELATIVE, ISO_DATE, NAMED_DATE, DOT_DATE, SLASH_DATE, MONTH_DAY, CLOCK, HOUR, DAYPART,
                NOON_MIDNIGHT, LUNCH, WEEKEND, WEEK_END, DAY_WORD, PAST_DAY, UNREAD_TIME)
    return any(pattern.search(text) for pattern in patterns)


def month_day_candidate(number, hour, minute, now):
    first = now.date().replace(day=1)
    for offset in range(13):
        month = add_months(first, offset)
        date = safe_date(month.year, month.month, number)
        if date is not None and datetime.combine(date, time(hour, minute)) > now:
            return date
    return None


def detailed_answer(result=None, certain=False, time_spoken=False, alternatives=()):
    return {"result": result, "certain": certain, "time_spoken": time_spoken, "alternatives": list(alternatives)}


def pick_moment(day, spec, hour, minute, now):
    """(result, alternatives) for a spoken time on `day`. Today takes the nearest future reading, another day the default."""
    hours = spec.readings() if spec.hour is not None else [hour]
    moments = [datetime.combine(day, time(value, minute)) for value in hours]
    if day == now.date():
        moments = sorted(moment for moment in moments if moment > now)
        if not moments and spec.hour == 0:
            # «Сегодня в полночь» said during the day is the coming midnight, which is tomorrow's 00:00.
            moments = [datetime.combine(day + timedelta(days=1), time(0, minute))]
    elif day < now.date():
        moments = []
    if not moments:
        return None, []
    return moments[0], moments[1:]


def parse_due_text_detailed(due_text, now):
    """Parse a quoted deadline against a naive local wall clock `now`.

    Returns {"result": naive datetime or None, "certain": bool, "time_spoken": bool, "alternatives": [datetime]}.
    `time_spoken` is True when the phrase names a time of day, even one that could not be read: then `result` is
    the best guess or None, never 09:00 taken from the day word alone. `certain` is False for an hour 1-11 with
    no daypart, in which case `alternatives` holds the other future readings.
    """
    if not isinstance(due_text, str) or not due_text.strip() or "\x00" in due_text:
        return detailed_answer()
    text = normalize(due_text)
    if PAST_DAY.search(text):
        return detailed_answer(time_spoken=read_time(text).found)
    today = now.date()
    base, rest = parse_relative(text, now)
    if isinstance(base, datetime):
        result = base.replace(second=0, microsecond=0)
        return detailed_answer(result if now < result <= now + MAX_AHEAD else None, result is not None, True)
    if base is not None:
        date_value, found_date = base, True
    else:
        date_value, found_date, rest = find_date(text, today)
    if found_date and date_value is None:
        return detailed_answer()
    spec = read_time(rest)
    if spec.found and spec.hour is None:
        return detailed_answer(time_spoken=True)
    weekend, week_end = WEEKEND.search(rest), WEEK_END.search(rest)
    if spec.found:
        hour, minute = spec.readings()[-1], spec.minute  # the latest reading decides which day is still possible
    else:
        hour, minute = (10, 0) if weekend else (18, 0) if week_end else (DEFAULT_HOUR, 0)
    day, explicit = None, True
    if isinstance(date_value, int):
        day = month_day_candidate(date_value, hour, minute, now)
        if day is None:
            return detailed_answer(time_spoken=spec.found)
    elif date_value is not None:
        day = date_value
    else:
        word = DAY_WORD.search(rest)
        if word is None and (weekend or week_end):
            day = nearest_day(now, hour, minute, {5, 6} if weekend else {4})
            if day is None:
                return detailed_answer(time_spoken=spec.found)
        elif word is None:
            if not spec.found:
                return detailed_answer()
            day, explicit = today, False
        elif word[1] == "сегодня":
            day = today
        elif word[1] == "завтра":
            day = today + timedelta(days=1)
        elif word[1] == "послезавтра":
            day = today + timedelta(days=2)
        else:
            day = today + timedelta(days=(WEEKDAYS[word[1]] - today.weekday()) % 7 or 7)
    if explicit:
        result, alternatives = pick_moment(day, spec, hour, minute, now)
    else:
        # A bare time that has passed rolls to tomorrow; the default reading of an ambiguous hour comes first.
        readings = spec.readings()
        options = [datetime.combine(today + timedelta(days=offset), time(value, minute))
                   for offset in (0, 1) for value in readings]
        result = datetime.combine(today, time(readings[0], minute))
        if result <= now:
            result += timedelta(days=1)
        alternatives = sorted(moment for moment in options if moment > now and moment != result)[:2]
    if result is None or result > now + MAX_AHEAD:
        return detailed_answer(time_spoken=spec.found)
    alternatives = [moment for moment in alternatives if moment <= now + MAX_AHEAD] if spec.ambiguous else []
    return detailed_answer(result, not spec.ambiguous, spec.found, alternatives)


def parse_due_text(due_text, now):
    """Parse a quoted deadline against a naive local wall clock `now`. Returns a naive datetime or None."""
    return parse_due_text_detailed(due_text, now)["result"]


def label_for(result, now):
    days = (result.date() - now.date()).days
    if days == 0:
        day = "сегодня"
    elif days == 1:
        day = "завтра"
    elif days == 2:
        day = "послезавтра"
    elif 3 <= days <= 6:
        day = WEEKDAY_LABEL[result.weekday()]
    else:
        day = f"{result.day} {MONTH_NAMES[result.month]}" + (f" {result.year}" if result.year != now.year else "")
    return f"{day} в {result:%H:%M}"


def propose_reminder(due_text, now, timezone):
    """Proposal for the confirmation form or None. `now` is an aware datetime, `timezone` an IANA name.

    `certain` is False when the time had two readings or was a guess; `alternatives` lists the other readings
    as local ISO strings. Wall times inside a DST gap do not exist, so they are not proposed.
    """
    zone = ZoneInfo(timezone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_now = now.astimezone(zone)
    detailed = parse_due_text_detailed(due_text, local_now.replace(tzinfo=None))
    result = detailed["result"]
    if result is None:
        return None

    def exists(moment):
        instant = moment.replace(tzinfo=zone).astimezone(UTC)
        return instant.astimezone(zone).replace(tzinfo=None) == moment and instant > now

    if not exists(result):
        return None
    label = label_for(result, local_now.replace(tzinfo=None))
    return {
        "local_time": f"{result:%Y-%m-%dT%H:%M}:00", "timezone": timezone, "label": label,
        "certain": detailed["certain"],
        "alternatives": [f"{moment:%Y-%m-%dT%H:%M}:00" for moment in detailed["alternatives"] if exists(moment)],
    }
