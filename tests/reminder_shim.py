"""Stand-in for `parse_due_text_detailed` until agent P2's parser is merged (docs/reminder-confirm-v1.md).

It only wraps the existing `parse_due_text`: every result it finds is certain, nothing has alternatives. It is a
test helper and is never imported by the application. Once the real function exists the fixture does nothing.
"""

from datetime import datetime

from app import reminder_proposals


def detailed_from_plain(text, now):
    result = reminder_proposals.parse_due_text(text, now)
    return {"result": result, "certain": result is not None, "time_spoken": False, "alternatives": []}


def fake_parser(table):
    """A `parse_due_text_detailed` that answers from a table {phrase: dict or datetime or None}. Others give nothing."""

    def parse(text, now):
        value = table.get(text)
        if isinstance(value, datetime):
            value = {"result": value, "certain": True, "time_spoken": True, "alternatives": []}
        base = {"result": None, "certain": False, "time_spoken": False, "alternatives": []}
        return base if value is None else {**base, **value}

    return parse
