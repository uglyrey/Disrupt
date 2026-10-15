"""Spoken and written times of day (docs/time-parsing-v2.md). Fixed "now" is Thursday 2026-10-15 05:47 local time."""

from datetime import UTC, datetime

import pytest

from app.reminder_proposals import (
    mentions_time,
    normalize,
    parse_due_text,
    parse_due_text_detailed,
    propose_reminder,
)

NOW = datetime(2026, 10, 15, 5, 47)
ZONE = "Europe/Moscow"

# phrase, expected result, certain, time_spoken, alternatives
TABLE = [
    ("сегодня в 16", "2026-10-15T16:00", True, True, []),
    ("сегодня в 16:00", "2026-10-15T16:00", True, True, []),
    ("сегодня в 4 часа дня", "2026-10-15T16:00", True, True, []),
    ("сегодня в 4 вечера", "2026-10-15T16:00", True, True, []),
    ("сегодня к 16", "2026-10-15T16:00", True, True, []),
    ("сегодня в шестнадцать часов", "2026-10-15T16:00", True, True, []),
    ("сегодня в шестнадцать", "2026-10-15T16:00", True, True, []),
    ("сегодня в шестнадцать ноль ноль", "2026-10-15T16:00", True, True, []),
    ("сегодня в 16 00", "2026-10-15T16:00", True, True, []),
    ("сегодня в шестнадцать тридцать", "2026-10-15T16:30", True, True, []),
    ("сегодня в полпятого", "2026-10-15T16:30", False, True, []),
    ("сегодня в половине пятого", "2026-10-15T16:30", False, True, []),
    ("сегодня в четыре", "2026-10-15T16:00", False, True, []),
    ("сегодня в четыре часа", "2026-10-15T16:00", False, True, []),
    ("сегодня в 16 30", "2026-10-15T16:30", True, True, []),
    ("сегодня в 16-00", "2026-10-15T16:00", True, True, []),
    ("сегодня в 16 ч", "2026-10-15T16:00", True, True, []),
    ("сегодня в 16ч", "2026-10-15T16:00", True, True, []),
    ("сегодня до 16", "2026-10-15T16:00", True, True, []),
    ("сегодня до шестнадцати", "2026-10-15T16:00", True, True, []),
    ("сегодня к четырем", "2026-10-15T16:00", False, True, []),
    ("сегодня к 17 часам", "2026-10-15T17:00", True, True, []),
    ("сегодня в семнадцать пятнадцать", "2026-10-15T17:15", True, True, []),
    ("сегодня в двадцать один ноль пять", "2026-10-15T21:05", True, True, []),
    ("сегодня в двадцать три сорок пять", "2026-10-15T23:45", True, True, []),
    ("сегодня в двадцать два часа", "2026-10-15T22:00", True, True, []),
    ("сегодня в пятнадцать пятьдесят", "2026-10-15T15:50", True, True, []),
    ("сегодня в 21 30", "2026-10-15T21:30", True, True, []),
    ("сегодня в ноль ноль", "2026-10-16T00:00", True, True, []),
    ("сегодня в полночь", "2026-10-16T00:00", True, True, []),
    ("сегодня в полдень", "2026-10-15T12:00", True, True, []),
    ("сегодня в двенадцать", "2026-10-15T12:00", True, True, []),
    ("сегодня в час дня", "2026-10-15T13:00", True, True, []),
    ("сегодня в 1", "2026-10-15T13:00", False, True, []),
    ("сегодня в 3", "2026-10-15T15:00", False, True, []),
    ("сегодня в 4", "2026-10-15T16:00", False, True, []),
    ("сегодня в 5", "2026-10-15T17:00", False, True, []),
    ("сегодня в 6", "2026-10-15T06:00", False, True, ["2026-10-15T18:00"]),
    ("сегодня в 7", "2026-10-15T07:00", False, True, ["2026-10-15T19:00"]),
    ("сегодня в 8", "2026-10-15T08:00", False, True, ["2026-10-15T20:00"]),
    ("сегодня в 11", "2026-10-15T11:00", False, True, ["2026-10-15T23:00"]),
    ("сегодня в 5:30", "2026-10-15T17:30", False, True, []),
    ("сегодня в 05:30", None, False, True, []),
    ("сегодня в 05:00", None, False, True, []),
    ("сегодня в 03:00", None, False, True, []),
    ("сегодня в 4 утра", None, False, True, []),
    ("сегодня в 6 утра", "2026-10-15T06:00", True, True, []),
    ("сегодня в 7 утра", "2026-10-15T07:00", True, True, []),
    ("сегодня в 10 вечера", "2026-10-15T22:00", True, True, []),
    ("сегодня в одиннадцать вечера", "2026-10-15T23:00", True, True, []),
    ("сегодня в 4 часа ночи", None, False, True, []),
    ("сегодня без четверти пять", "2026-10-15T16:45", False, True, []),
    ("сегодня без пятнадцати пять", "2026-10-15T16:45", False, True, []),
    ("сегодня без десяти пять", "2026-10-15T16:50", False, True, []),
    ("сегодня без пяти шесть", "2026-10-15T05:55", False, True, ["2026-10-15T17:55"]),
    ("сегодня четверть пятого", "2026-10-15T16:15", False, True, []),
    ("сегодня в четверть шестого", "2026-10-15T17:15", False, True, []),
    ("сегодня в половине пятого вечера", "2026-10-15T16:30", True, True, []),
    ("сегодня в половину седьмого", "2026-10-15T06:30", False, True, ["2026-10-15T18:30"]),
    ("сегодня в полседьмого", "2026-10-15T06:30", False, True, ["2026-10-15T18:30"]),
    ("завтра в 7", "2026-10-16T07:00", False, True, ["2026-10-16T19:00"]),
    ("завтра в 4", "2026-10-16T16:00", False, True, ["2026-10-16T04:00"]),
    ("завтра в 11", "2026-10-16T11:00", False, True, ["2026-10-16T23:00"]),
    ("завтра в 10", "2026-10-16T10:00", False, True, ["2026-10-16T22:00"]),
    ("завтра в 3", "2026-10-16T15:00", False, True, ["2026-10-16T03:00"]),
    ("завтра в 12", "2026-10-16T12:00", True, True, []),
    ("завтра в семь тридцать", "2026-10-16T07:30", False, True, ["2026-10-16T19:30"]),
    ("завтра в девять утра", "2026-10-16T09:00", True, True, []),
    ("завтра в пять вечера", "2026-10-16T17:00", True, True, []),
    ("завтра в 9:30", "2026-10-16T09:30", False, True, ["2026-10-16T21:30"]),
    ("завтра в 8", "2026-10-16T08:00", False, True, ["2026-10-16T20:00"]),
    ("завтра в 08:00", "2026-10-16T08:00", True, True, []),
    ("завтра в полпервого", "2026-10-16T12:30", True, True, []),
    ("завтра в час", "2026-10-16T13:00", False, True, ["2026-10-16T01:00"]),
    ("завтра в час ночи", "2026-10-16T01:00", True, True, []),
    ("завтра в ноль пять", "2026-10-16T00:05", True, True, []),
    ("завтра в ноль часов", "2026-10-16T00:00", True, True, []),
    ("завтра в два часа", "2026-10-16T14:00", False, True, ["2026-10-16T02:00"]),
    ("завтра в 14 ноль ноль", "2026-10-16T14:00", True, True, []),
    ("завтра 16 30", "2026-10-16T16:30", True, True, []),
    ("завтра", "2026-10-16T09:00", True, False, []),
    ("завтра утром", "2026-10-16T09:00", True, True, []),
    ("завтра вечером", "2026-10-16T19:00", True, True, []),
    ("послезавтра в 8", "2026-10-17T08:00", False, True, ["2026-10-17T20:00"]),
    ("послезавтра в пять", "2026-10-17T17:00", False, True, ["2026-10-17T05:00"]),
    ("в четыре", "2026-10-15T16:00", False, True, ["2026-10-16T04:00", "2026-10-16T16:00"]),
    ("в 4", "2026-10-15T16:00", False, True, ["2026-10-16T04:00", "2026-10-16T16:00"]),
    ("в 7", "2026-10-15T07:00", False, True, ["2026-10-15T19:00", "2026-10-16T07:00"]),
    ("в 16", "2026-10-15T16:00", True, True, []),
    ("в 10", "2026-10-15T10:00", False, True, ["2026-10-15T22:00", "2026-10-16T10:00"]),
    ("в 17 30", "2026-10-15T17:30", True, True, []),
    ("в пятницу в шестнадцать тридцать", "2026-10-16T16:30", True, True, []),
    ("в пятницу в 5", "2026-10-16T17:00", False, True, ["2026-10-16T05:00"]),
    ("в пятницу", "2026-10-16T09:00", True, False, []),
    ("в понедельник в семь утра", "2026-10-19T07:00", True, True, []),
    ("в среду в 3 дня", "2026-10-21T15:00", True, True, []),
    ("во вторник в 6", "2026-10-20T18:00", False, True, ["2026-10-20T06:00"]),
    ("15 октября в 14:00", "2026-10-15T14:00", True, True, []),
    ("15 октября в 2", "2026-10-15T14:00", False, True, []),
    ("15 октября в 14 00", "2026-10-15T14:00", True, True, []),
    ("16 октября в 9:30", "2026-10-16T09:30", False, True, ["2026-10-16T21:30"]),
    ("20 октября в семнадцать ноль ноль", "2026-10-20T17:00", True, True, []),
    ("20 октября в 7", "2026-10-20T07:00", False, True, ["2026-10-20T19:00"]),
    ("через два часа", "2026-10-15T07:47", True, True, []),
    ("через 15 минут", "2026-10-15T06:02", True, True, []),
    ("через пятнадцать минут", "2026-10-15T06:02", True, True, []),
    ("через двадцать пять минут", "2026-10-15T06:12", True, True, []),
    ("через 3 дня в 4", "2026-10-18T16:00", False, True, ["2026-10-18T04:00"]),
    ("через час", "2026-10-15T06:47", True, True, []),
    ("сегодня в 25", None, False, True, []),
    ("сегодня в 17 61", None, False, True, []),
    ("завтра в 3 5", None, False, True, []),
    ("сегодня в шестьдесят", None, False, True, []),
    ("сегодня в 24:00", None, False, True, []),
    ("в 25", None, False, True, []),
    ("вчера в 16", None, False, True, []),
    ("вчера", None, False, False, []),
    ("скоро", None, False, False, []),
    ("сегодня", "2026-10-15T09:00", True, False, []),
    ("на выходных в 5", "2026-10-17T17:00", False, True, ["2026-10-17T05:00"]),
    ("ЗАВТРА В ШЕСТНАДЦАТЬ ТРИДЦАТЬ", "2026-10-16T16:30", True, True, []),
    ("сегодня в шестнадцать часов тридцать минут", "2026-10-15T16:30", True, True, []),
    ("сегодня в 4 часа 30 минут", "2026-10-15T16:30", False, True, []),
    ("сегодня в 4 часа 30", "2026-10-15T16:30", False, True, []),
    ("сегодня в 16 часов 15", "2026-10-15T16:15", True, True, []),
    ("сегодня в шестнадцать ч", "2026-10-15T16:00", True, True, []),
    ("к семнадцати ноль ноль", "2026-10-15T17:00", True, True, []),
    ("завтра в двенадцать ноль пять", "2026-10-16T12:05", True, True, []),

]


def iso(value):
    return None if value is None else f"{value:%Y-%m-%dT%H:%M}"


@pytest.mark.parametrize(("text", "result", "certain", "spoken", "alternatives"), TABLE)
def test_table(text, result, certain, spoken, alternatives):
    detailed = parse_due_text_detailed(text, NOW)
    assert iso(detailed["result"]) == result
    assert detailed["certain"] is certain
    assert detailed["time_spoken"] is spoken
    assert [iso(moment) for moment in detailed["alternatives"]] == alternatives
    assert parse_due_text(text, NOW) == detailed["result"]


def test_table_is_big_enough():
    assert len(TABLE) >= 60


def test_spoken_time_is_never_replaced_by_the_day_default():
    for text in ("сегодня в шестнадцать ноль ноль", "сегодня в полпятого", "сегодня в четыре", "завтра в 3 5"):
        result = parse_due_text(text, NOW)
        assert result is None or result.hour != 9, text


def test_unreadable_time_gives_no_default_and_says_so():
    for text in ("сегодня в шестьдесят", "завтра в 3 5", "сегодня в 17 61", "в пятницу в 25"):
        detailed = parse_due_text_detailed(text, NOW)
        assert detailed == {"result": None, "certain": False, "time_spoken": True, "alternatives": []}


def test_day_word_alone_is_nine_and_certain():
    detailed = parse_due_text_detailed("завтра", NOW)
    assert detailed == {"result": datetime(2026, 10, 16, 9, 0), "certain": True, "time_spoken": False, "alternatives": []}


def test_nearest_future_reading_depends_on_the_clock():
    early = datetime(2026, 10, 15, 3, 0)
    assert parse_due_text_detailed("сегодня в четыре", early)["result"] == datetime(2026, 10, 15, 4, 0)
    assert parse_due_text_detailed("сегодня в четыре", early)["alternatives"] == [datetime(2026, 10, 15, 16, 0)]
    late = datetime(2026, 10, 15, 17, 0)
    detailed = parse_due_text_detailed("сегодня в четыре", late)
    assert detailed["result"] is None and detailed["time_spoken"] is True and detailed["certain"] is False
    assert parse_due_text_detailed("в четыре", late)["result"] == datetime(2026, 10, 16, 16, 0)
    night = datetime(2026, 10, 15, 23, 30)
    assert parse_due_text_detailed("сегодня в 7", night)["result"] is None
    assert parse_due_text_detailed("в 7", night)["result"] == datetime(2026, 10, 16, 7, 0)


def test_telegram_voice_like_text():
    for text in ("напомни сегодня в шестнадцать ноль ноль позвонить маме", "сегодня в 16 00 позвонить маме",
                 "позвонить маме сегодня к шестнадцати нуль нуль", "ё ё сегодня в шестнадцать ноль-ноль"):
        assert parse_due_text(text, NOW) == datetime(2026, 10, 15, 16, 0), text


def test_propose_reminder_carries_certainty_and_local_alternatives():
    now = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)  # 06:00 in Moscow
    sure = propose_reminder("сегодня в 16:00", now, ZONE)
    assert sure["certain"] is True and sure["alternatives"] == []
    guess = propose_reminder("сегодня в семь", now, ZONE)
    assert guess["local_time"] == "2026-10-15T07:00:00" and guess["certain"] is False
    assert guess["alternatives"] == ["2026-10-15T19:00:00"]
    assert propose_reminder("сегодня в шестьдесят", now, ZONE) is None


def test_spoken_times_count_as_time_for_the_old_helper():
    for text in ("в шестнадцать ноль ноль", "в половине пятого", "без четверти пять", "завтра в 3 5"):
        assert mentions_time(text), text
    assert not mentions_time("купить молоко")


def test_normalized_forms():
    assert normalize("в шестнадцать ноль ноль") == "в 16:00"
    assert normalize("к четырём") == "к 4"
    assert normalize("в половине пятого") == "в 4:30"
    assert normalize("без четверти пять") == "в 4:45"
    assert normalize("через сорок пять минут") == "через 45 минут"
