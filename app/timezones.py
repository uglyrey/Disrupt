"""Time zones of an account: the list, Russian names, validation, the effective zone and the nearest city.

Everything here is built in. There is no IP lookup and no external service. The list comes from the
IANA database that ships with Python (`zoneinfo.available_timezones()`), the names for Russia, the CIS
and the most common world cities are written below, the rest show the city part of the IANA name.

The nearest city for a Telegram location is found in memory from the small table CITIES. The
coordinates are never stored and never logged. The table is coarse: close to a border the nearest
city may lie in the neighbour zone, so the bot always shows the result and the person can change it.
"""

import math
import re
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from app.config import Settings

# Not real places, even though the database knows the names.
NOT_A_PLACE = {"Factory", "posixrules", "localtime"}
REGIONS = ("Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe", "Indian", "Pacific")
SOURCES = ("auto", "manual", "telegram")

# Zone to the Russian name. Russia first, one zone by its main city.
RUSSIA = {
    "Europe/Kaliningrad": "Калининград",
    "Europe/Moscow": "Москва",
    "Europe/Simferopol": "Симферополь",
    "Europe/Kirov": "Киров",
    "Europe/Volgograd": "Волгоград",
    "Europe/Astrakhan": "Астрахань",
    "Europe/Saratov": "Саратов",
    "Europe/Ulyanovsk": "Ульяновск",
    "Europe/Samara": "Самара",
    "Asia/Yekaterinburg": "Екатеринбург",
    "Asia/Omsk": "Омск",
    "Asia/Novosibirsk": "Новосибирск",
    "Asia/Barnaul": "Барнаул",
    "Asia/Tomsk": "Томск",
    "Asia/Novokuznetsk": "Новокузнецк",
    "Asia/Krasnoyarsk": "Красноярск",
    "Asia/Irkutsk": "Иркутск",
    "Asia/Chita": "Чита",
    "Asia/Yakutsk": "Якутск",
    "Asia/Khandyga": "Хандыга",
    "Asia/Vladivostok": "Владивосток",
    "Asia/Ust-Nera": "Усть-Нера",
    "Asia/Magadan": "Магадан",
    "Asia/Sakhalin": "Сахалин",
    "Asia/Srednekolymsk": "Среднеколымск",
    "Asia/Kamchatka": "Камчатка",
    "Asia/Anadyr": "Анадырь",
}
CIS = {
    "Europe/Kyiv": "Киев",
    "Europe/Minsk": "Минск",
    "Europe/Chisinau": "Кишинёв",
    "Asia/Tbilisi": "Тбилиси",
    "Asia/Yerevan": "Ереван",
    "Asia/Baku": "Баку",
    "Asia/Tashkent": "Ташкент",
    "Asia/Samarkand": "Самарканд",
    "Asia/Almaty": "Алматы",
    "Asia/Qostanay": "Костанай",
    "Asia/Qyzylorda": "Кызылорда",
    "Asia/Aqtobe": "Актобе",
    "Asia/Aqtau": "Актау",
    "Asia/Atyrau": "Атырау",
    "Asia/Oral": "Уральск",
    "Asia/Bishkek": "Бишкек",
    "Asia/Dushanbe": "Душанбе",
    "Asia/Ashgabat": "Ашхабад",
    "Europe/Riga": "Рига",
    "Europe/Vilnius": "Вильнюс",
    "Europe/Tallinn": "Таллин",
    "Asia/Ulaanbaatar": "Улан-Батор",
}
WORLD = {
    "UTC": "UTC (всемирное время)",
    "Europe/London": "Лондон",
    "Europe/Dublin": "Дублин",
    "Europe/Lisbon": "Лиссабон",
    "Europe/Paris": "Париж",
    "Europe/Berlin": "Берлин",
    "Europe/Madrid": "Мадрид",
    "Europe/Rome": "Рим",
    "Europe/Amsterdam": "Амстердам",
    "Europe/Brussels": "Брюссель",
    "Europe/Vienna": "Вена",
    "Europe/Prague": "Прага",
    "Europe/Warsaw": "Варшава",
    "Europe/Budapest": "Будапешт",
    "Europe/Belgrade": "Белград",
    "Europe/Zurich": "Цюрих",
    "Europe/Stockholm": "Стокгольм",
    "Europe/Oslo": "Осло",
    "Europe/Copenhagen": "Копенгаген",
    "Europe/Helsinki": "Хельсинки",
    "Europe/Athens": "Афины",
    "Europe/Bucharest": "Бухарест",
    "Europe/Sofia": "София",
    "Europe/Istanbul": "Стамбул",
    "Atlantic/Reykjavik": "Рейкьявик",
    "Asia/Jerusalem": "Иерусалим",
    "Asia/Beirut": "Бейрут",
    "Asia/Amman": "Амман",
    "Asia/Baghdad": "Багдад",
    "Asia/Riyadh": "Эр-Рияд",
    "Asia/Tehran": "Тегеран",
    "Asia/Dubai": "Дубай",
    "Asia/Kabul": "Кабул",
    "Asia/Karachi": "Карачи",
    "Asia/Kolkata": "Дели",
    "Asia/Kathmandu": "Катманду",
    "Asia/Dhaka": "Дакка",
    "Asia/Bangkok": "Бангкок",
    "Asia/Ho_Chi_Minh": "Хошимин",
    "Asia/Jakarta": "Джакарта",
    "Asia/Shanghai": "Шанхай",
    "Asia/Hong_Kong": "Гонконг",
    "Asia/Singapore": "Сингапур",
    "Asia/Manila": "Манила",
    "Asia/Seoul": "Сеул",
    "Asia/Tokyo": "Токио",
    "Australia/Perth": "Перт",
    "Australia/Sydney": "Сидней",
    "Pacific/Auckland": "Окленд",
    "Pacific/Honolulu": "Гонолулу",
    "America/Anchorage": "Анкоридж",
    "America/Los_Angeles": "Лос-Анджелес",
    "America/Denver": "Денвер",
    "America/Chicago": "Чикаго",
    "America/New_York": "Нью-Йорк",
    "America/Toronto": "Торонто",
    "America/Halifax": "Галифакс",
    "America/Mexico_City": "Мехико",
    "America/Bogota": "Богота",
    "America/Lima": "Лима",
    "America/Sao_Paulo": "Сан-Паулу",
    "America/Argentina/Buenos_Aires": "Буэнос-Айрес",
    "Africa/Cairo": "Каир",
    "Africa/Lagos": "Лагос",
    "Africa/Nairobi": "Найроби",
    "Africa/Johannesburg": "Йоханнесбург",
    "Africa/Casablanca": "Касабланка",
}
LABELS = {**RUSSIA, **CIS, **WORLD}

# City, zone, latitude, longitude. A coarse table for the Telegram location button and for typed city names.
CITIES = (
    # Russia: every zone by its main city, then big cities that help near zone borders.
    ("Калининград", "Europe/Kaliningrad", 54.71, 20.51),
    ("Москва", "Europe/Moscow", 55.76, 37.62),
    ("Санкт-Петербург", "Europe/Moscow", 59.94, 30.31),
    ("Мурманск", "Europe/Moscow", 68.97, 33.09),
    ("Казань", "Europe/Moscow", 55.80, 49.11),
    ("Нижний Новгород", "Europe/Moscow", 56.33, 44.00),
    ("Ростов-на-Дону", "Europe/Moscow", 47.23, 39.72),
    ("Краснодар", "Europe/Moscow", 45.04, 38.98),
    ("Воронеж", "Europe/Moscow", 51.67, 39.18),
    ("Симферополь", "Europe/Simferopol", 44.95, 34.10),
    ("Киров", "Europe/Kirov", 58.60, 49.66),
    ("Волгоград", "Europe/Volgograd", 48.71, 44.51),
    ("Астрахань", "Europe/Astrakhan", 46.35, 48.04),
    ("Саратов", "Europe/Saratov", 51.53, 46.03),
    ("Ульяновск", "Europe/Ulyanovsk", 54.32, 48.40),
    ("Самара", "Europe/Samara", 53.20, 50.15),
    ("Ижевск", "Europe/Samara", 56.85, 53.20),
    ("Екатеринбург", "Asia/Yekaterinburg", 56.84, 60.65),
    ("Пермь", "Asia/Yekaterinburg", 58.01, 56.25),
    ("Уфа", "Asia/Yekaterinburg", 54.74, 55.97),
    ("Челябинск", "Asia/Yekaterinburg", 55.16, 61.40),
    ("Тюмень", "Asia/Yekaterinburg", 57.15, 65.53),
    ("Оренбург", "Asia/Yekaterinburg", 51.77, 55.10),
    ("Сургут", "Asia/Yekaterinburg", 61.25, 73.40),
    ("Омск", "Asia/Omsk", 54.99, 73.37),
    ("Новосибирск", "Asia/Novosibirsk", 55.03, 82.92),
    ("Барнаул", "Asia/Barnaul", 53.35, 83.77),
    ("Томск", "Asia/Tomsk", 56.50, 84.97),
    ("Новокузнецк", "Asia/Novokuznetsk", 53.76, 87.12),
    ("Красноярск", "Asia/Krasnoyarsk", 56.01, 92.85),
    ("Норильск", "Asia/Krasnoyarsk", 69.35, 88.20),
    ("Иркутск", "Asia/Irkutsk", 52.29, 104.30),
    ("Улан-Удэ", "Asia/Irkutsk", 51.83, 107.61),
    ("Чита", "Asia/Chita", 52.03, 113.50),
    ("Якутск", "Asia/Yakutsk", 62.03, 129.73),
    ("Хандыга", "Asia/Khandyga", 62.65, 135.57),
    ("Владивосток", "Asia/Vladivostok", 43.12, 131.89),
    ("Хабаровск", "Asia/Vladivostok", 48.48, 135.08),
    ("Усть-Нера", "Asia/Ust-Nera", 64.57, 143.23),
    ("Магадан", "Asia/Magadan", 59.56, 150.80),
    ("Южно-Сахалинск", "Asia/Sakhalin", 46.96, 142.74),
    ("Среднеколымск", "Asia/Srednekolymsk", 67.46, 153.71),
    ("Петропавловск-Камчатский", "Asia/Kamchatka", 53.04, 158.65),
    ("Анадырь", "Asia/Anadyr", 64.73, 177.51),
    # CIS capitals and big cities of Kazakhstan.
    ("Киев", "Europe/Kyiv", 50.45, 30.52),
    ("Минск", "Europe/Minsk", 53.90, 27.57),
    ("Кишинёв", "Europe/Chisinau", 47.01, 28.86),
    ("Тбилиси", "Asia/Tbilisi", 41.72, 44.79),
    ("Ереван", "Asia/Yerevan", 40.18, 44.51),
    ("Баку", "Asia/Baku", 40.41, 49.87),
    ("Ташкент", "Asia/Tashkent", 41.30, 69.24),
    ("Самарканд", "Asia/Samarkand", 39.65, 66.96),
    ("Алматы", "Asia/Almaty", 43.24, 76.89),
    ("Астана", "Asia/Almaty", 51.17, 71.43),
    ("Актау", "Asia/Aqtau", 43.65, 51.15),
    ("Атырау", "Asia/Atyrau", 47.11, 51.92),
    ("Уральск", "Asia/Oral", 51.23, 51.37),
    ("Актобе", "Asia/Aqtobe", 50.28, 57.21),
    ("Кызылорда", "Asia/Qyzylorda", 44.85, 65.51),
    ("Костанай", "Asia/Qostanay", 53.21, 63.63),
    ("Бишкек", "Asia/Bishkek", 42.87, 74.59),
    ("Душанбе", "Asia/Dushanbe", 38.56, 68.79),
    ("Ашхабад", "Asia/Ashgabat", 37.96, 58.33),
    ("Рига", "Europe/Riga", 56.95, 24.11),
    ("Вильнюс", "Europe/Vilnius", 54.69, 25.28),
    ("Таллин", "Europe/Tallinn", 59.44, 24.75),
    ("Улан-Батор", "Asia/Ulaanbaatar", 47.89, 106.91),
    # Common world cities.
    ("Лондон", "Europe/London", 51.51, -0.13),
    ("Париж", "Europe/Paris", 48.86, 2.35),
    ("Берлин", "Europe/Berlin", 52.52, 13.40),
    ("Мадрид", "Europe/Madrid", 40.42, -3.70),
    ("Рим", "Europe/Rome", 41.90, 12.50),
    ("Варшава", "Europe/Warsaw", 52.23, 21.01),
    ("Хельсинки", "Europe/Helsinki", 60.17, 24.94),
    ("Афины", "Europe/Athens", 37.98, 23.73),
    ("Лиссабон", "Europe/Lisbon", 38.72, -9.14),
    ("Стамбул", "Europe/Istanbul", 41.01, 28.98),
    ("Иерусалим", "Asia/Jerusalem", 31.77, 35.21),
    ("Тель-Авив", "Asia/Jerusalem", 32.09, 34.78),
    ("Каир", "Africa/Cairo", 30.04, 31.24),
    ("Эр-Рияд", "Asia/Riyadh", 24.71, 46.68),
    ("Тегеран", "Asia/Tehran", 35.69, 51.39),
    ("Дубай", "Asia/Dubai", 25.20, 55.27),
    ("Кабул", "Asia/Kabul", 34.53, 69.17),
    ("Карачи", "Asia/Karachi", 24.86, 67.01),
    ("Дели", "Asia/Kolkata", 28.61, 77.21),
    ("Дакка", "Asia/Dhaka", 23.81, 90.41),
    ("Бангкок", "Asia/Bangkok", 13.76, 100.50),
    ("Джакарта", "Asia/Jakarta", -6.21, 106.85),
    ("Шанхай", "Asia/Shanghai", 31.23, 121.47),
    ("Пекин", "Asia/Shanghai", 39.90, 116.41),
    ("Гонконг", "Asia/Hong_Kong", 22.32, 114.17),
    ("Сингапур", "Asia/Singapore", 1.35, 103.82),
    ("Сеул", "Asia/Seoul", 37.57, 126.98),
    ("Токио", "Asia/Tokyo", 35.68, 139.69),
    ("Сидней", "Australia/Sydney", -33.87, 151.21),
    ("Окленд", "Pacific/Auckland", -36.85, 174.76),
    ("Нью-Йорк", "America/New_York", 40.71, -74.01),
    ("Чикаго", "America/Chicago", 41.88, -87.63),
    ("Денвер", "America/Denver", 39.74, -104.99),
    ("Лос-Анджелес", "America/Los_Angeles", 34.05, -118.24),
    ("Торонто", "America/Toronto", 43.65, -79.38),
    ("Мехико", "America/Mexico_City", 19.43, -99.13),
    ("Сан-Паулу", "America/Sao_Paulo", -23.55, -46.63),
    ("Буэнос-Айрес", "America/Argentina/Buenos_Aires", -34.60, -58.38),
    ("Йоханнесбург", "Africa/Johannesburg", -26.20, 28.05),
    ("Лагос", "Africa/Lagos", 6.52, 3.38),
    ("Найроби", "Africa/Nairobi", -1.29, 36.82),
)
# Farther than this from every city of the table the place is not resolved (sea, desert, pole).
MAX_DISTANCE_KM = 1200

# «+5», «UTC+5», «gmt +5:30». An offset names the main Russian zone with that offset, nothing else.
MAIN_BY_OFFSET = {
    2: "Europe/Kaliningrad", 3: "Europe/Moscow", 4: "Europe/Samara", 5: "Asia/Yekaterinburg", 6: "Asia/Omsk",
    7: "Asia/Novosibirsk", 8: "Asia/Irkutsk", 9: "Asia/Yakutsk", 10: "Asia/Vladivostok",
    11: "Asia/Magadan", 12: "Asia/Kamchatka",
}
OFFSET_TEXT = re.compile(r"^(?:utc|gmt|мск|msk)?\s*([+-])\s*(\d{1,2})(?::?00)?$")


@lru_cache(maxsize=1)
def known_zones():
    return frozenset(available_timezones()) - NOT_A_PLACE


def valid_zone(name):
    """True for a name of the IANA database. The names `Factory` and `posixrules` are not places."""
    return isinstance(name, str) and 0 < len(name) <= 64 and name in known_zones()


def effective_zone(user, settings=None):
    """The zone of the account, else the service zone. Quotas keep using `settings.limit_timezone`."""
    zone = getattr(user, "timezone", None)
    if valid_zone(zone):
        return zone
    return settings.limit_timezone if settings is not None else Settings.limit_timezone


def zone_guessed(user):
    """True while the account has no zone of its own, so a new reminder uses the service zone by guess."""
    return not valid_zone(getattr(user, "timezone", None))


def label_for_zone(name):
    if name in LABELS:
        return LABELS[name]
    return name.rsplit("/", 1)[-1].replace("_", " ")


def by_zone(name):
    """«по Москве» for a time read in a zone by guess. Other zones are named by their label."""
    if name == "Europe/Moscow":
        return "по Москве"
    return "по поясу " + label_for_zone(name)


def offset_minutes(name, now=None):
    moment = now or datetime.now(UTC)
    offset = moment.astimezone(ZoneInfo(name)).utcoffset()
    return int(offset.total_seconds() // 60)


def offset_string(minutes):
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def utc_text(minutes):
    """«UTC+7», «UTC+5:30», «UTC-3», «UTC»."""
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def zone_summary(name, now=None):
    """What the clients show about a zone: id, Russian label, current offset."""
    minutes = offset_minutes(name, now)
    return {"id": name, "label": label_for_zone(name), "offset": offset_string(minutes), "offset_minutes": minutes}


def _zone_tab_names():
    candidates = []
    try:
        candidates.append(resources.files("tzdata.zoneinfo").joinpath("zone.tab").read_text(encoding="utf-8"))
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass
    if not candidates:
        for base in ("/usr/share/zoneinfo", "/usr/lib/zoneinfo", "/etc/zoneinfo"):
            path = Path(base) / "zone.tab"
            if path.is_file():
                candidates.append(path.read_text(encoding="utf-8"))
                break
    names = set()
    for text in candidates:
        for line in text.splitlines():
            parts = line.split("\t")
            if line and not line.startswith("#") and len(parts) >= 3:
                names.add(parts[2].strip())
    return names


@lru_cache(maxsize=1)
def canonical_zones():
    """Places people choose from: one name per place, no legacy aliases (`Asia/Calcutta`, `US/*`, `Etc/*`).

    The base is the `zone.tab` of the database. Names of the label tables are always added, and `UTC`.
    Without `zone.tab` the list falls back to every name with a region prefix.
    """
    known = known_zones()
    base = _zone_tab_names() or {
        name for name in known if name.split("/", 1)[0] in REGIONS and not name.startswith("Etc/")
    }
    return frozenset(name for name in base | set(LABELS) if name in known)


def zone_list(now=None):
    """All canonical zones with the current offset, sorted by offset, then label."""
    now = now or datetime.now(UTC)
    rows = [zone_summary(name, now) for name in canonical_zones()]
    rows.sort(key=lambda row: (row["offset_minutes"], row["label"].lower(), row["id"]))
    return rows


def normalize(text):
    text = str(text).lower().replace("ё", "е")
    text = re.sub(r"[\s_]+", " ", text.replace("–", "-").replace("—", "-"))
    return text.strip(" .,;:!?\"'«»()")


@lru_cache(maxsize=1)
def _name_index():
    """Normalized name to zone. Russian labels first, then city names, then IANA names and their city part."""
    index = {}
    for name, label in LABELS.items():
        index.setdefault(normalize(label), name)
    for city, zone, _lat, _lon in CITIES:
        index.setdefault(normalize(city), zone)
    for name in canonical_zones():
        index.setdefault(normalize(name), name)
    seen = {}
    for name in canonical_zones():
        seen.setdefault(normalize(name.rsplit("/", 1)[-1]), set()).add(name)
    for key, names in seen.items():
        if len(names) == 1:
            index.setdefault(key, next(iter(names)))
    for alias, zone in (("спб", "Europe/Moscow"), ("питер", "Europe/Moscow"), ("мск", "Europe/Moscow"),
                        ("utc", "UTC"), ("gmt", "UTC")):
        index.setdefault(alias, zone)
    return index


def find_zone(text):
    """A zone from what a person typed: a Russian city, an IANA name, or an offset like «+5». None if unknown.

    An offset gives the main Russian zone with that offset, never an `Etc/GMT` name.
    """
    if not isinstance(text, str):
        return None
    cleaned = normalize(text)
    if not cleaned or len(cleaned) > 64:
        return None
    match = OFFSET_TEXT.fullmatch(cleaned)
    if match:
        hours = int(match.group(2)) * (1 if match.group(1) == "+" else -1)
        return MAIN_BY_OFFSET.get(hours)
    index = _name_index()
    if cleaned in index:
        return index[cleaned]
    # A name inside a longer phrase, «г. Омск, Россия». Only when every hit points to one zone.
    tokens = [t.strip(".") for t in re.split(r"[\s,;/()]+", cleaned) if t.strip(".")]
    hits = set()
    for size in (3, 2, 1):
        for start in range(len(tokens) - size + 1):
            phrase = " ".join(tokens[start:start + size])
            if phrase in index:
                hits.add(index[phrase])
    return next(iter(hits)) if len(hits) == 1 else None


def distance_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta = math.radians(lon2 - lon1)
    a = math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def nearest_city(latitude, longitude):
    """The closest city of the table as (city, zone, distance in km), or None when none is within reach.

    The arguments live only in this call. They are not stored and not logged.
    """
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (latitude, longitude)):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    best = min(CITIES, key=lambda row: distance_km(latitude, longitude, row[2], row[3]))
    km = distance_km(latitude, longitude, best[2], best[3])
    if km > MAX_DISTANCE_KM:
        return None
    return best[0], best[1], km
