"""Public comparison prices, never the accelerator gateway's measured bill.

Snapshot checked 2026-10-11. No HTTP or credentials. Cached input is priced at
the ordinary input rate; no gateway cache discount or alias routing is assumed.
"""

from decimal import Decimal

DATE = "2026-10-11"
SOURCE = "https://cloud.ru/products/evolution-ai-factory/catalog-foundation-models"
# Cloud.ru catalogue: DeepSeek-V4-Flash RUB / million input and output tokens.
INPUT_PER_MILLION = Decimal("43.2978")
OUTPUT_PER_MILLION = Decimal("86.5834")
# Official service tariff: APP Whisper RUB / second, including VAT.
STT_PER_SECOND = Decimal("0.0061")


def estimate(row):
    if row.kind == "llm" and row.model == "deepseek-v4-flash":
        if any(type(value) is not int or value < 0 for value in (row.input_tokens, row.output_tokens)):
            return None
        return (row.input_tokens * INPUT_PER_MILLION + row.output_tokens * OUTPUT_PER_MILLION) / 1_000_000
    if row.kind == "stt" and row.model == "whisper-large-v3" and row.status == "succeeded":
        if row.stt_seconds is None:
            return None
        seconds = Decimal(str(row.stt_seconds))
        if not seconds.is_finite() or seconds < 0:
            return None
        return seconds * STT_PER_SECOND
    return None
