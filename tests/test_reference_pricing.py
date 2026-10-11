from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.admin_metrics import costs
from app.reference_pricing import estimate


def row(**changes):
    return SimpleNamespace(**({"kind": "llm", "model": "deepseek-v4-flash", "status": "succeeded",
                              "input_tokens": 1_000_000, "output_tokens": 500_000, "cache_tokens": 900_000,
                              "stt_seconds": None, "cost": None} | changes))


def test_reference_price_is_separate_from_unknown_bill_and_preserves_coverage():
    known = row()
    missing = row(input_tokens=None, output_tokens=None, cache_tokens=None, status="unknown")
    result = costs([known, missing], users=2, sessions=4, dau_sum=3)
    assert result["llm_cost"] is None and result["known_llm_cost"] == "0.00000000"
    assert result["reference_llm_cost"] == "86.58950000"
    assert result["reference_llm_calls"] == 1 and result["llm_calls"] == 2
    assert result["input_tokens"] is None and result["known_input_tokens"] == 1_000_000
    assert result["input_tokens_calls"] == 1 and result["llm_cost_calls"] == 0
    assert result["reference_cost_per_user"] == "43.29475000"
    # Incomplete billing never becomes a measured zero or alters provider receipts.
    assert known.cost is None and missing.cost is None


@pytest.mark.parametrize("changes", [
    {"model": "legacy-untracked"}, {"model": "unconfirmed-alias"}, {"input_tokens": None},
    {"output_tokens": None}, {"input_tokens": -1}, {"input_tokens": True},
])
def test_unmeasured_or_unrecognized_llm_is_not_priced(changes):
    assert estimate(row(**changes)) is None


def test_stt_price_uses_seconds_and_only_confirmed_success():
    audio = row(kind="stt", model="whisper-large-v3", stt_seconds=60)
    assert estimate(audio) == Decimal("0.3660")
    for changes in ({"status": "failed"}, {"status": "unknown"}, {"stt_seconds": None},
                    {"stt_seconds": float("nan")}, {"stt_seconds": -1}):
        assert estimate(row(**({"kind": "stt", "model": "whisper-large-v3", "stt_seconds": 60} | changes))) is None


def test_no_measurements_are_not_reported_as_known_zero_tokens():
    unknown = row(input_tokens=None, output_tokens=None, cache_tokens=None)
    result = costs([unknown], users=1, sessions=1, dau_sum=1)
    assert result["known_input_tokens"] is None and result["input_tokens_calls"] == 0
    assert result["reference_llm_cost"] is None and result["reference_cost_per_user"] is None
    empty = costs([], users=0, sessions=0, dau_sum=0)
    assert empty["known_input_tokens"] == 0 and empty["llm_cost"] == "0.00000000"
