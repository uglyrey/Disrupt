"""Дневной потолок расхода на резервный ИИ. Подробности в docs/ai-failover-v1.md.

Перед каждым обращением к резерву в базе резервируется наибольшая возможная цена вызова, а после ответа
резерв заменяется настоящей оценкой. Резерв занимается одним условным UPDATE, поэтому два воркера
вместе не могут превысить потолок. Деньги хранятся целыми миллионными долями доллара.
Сутки считаются в часовом поясе лимитов (NOTES_LIMIT_TIMEZONE, по умолчанию Europe/Moscow).
"""

import math
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError

from app.models import FallbackSpend

MICRO = 1_000_000
ESTIMATE_VERSION = "openai-estimate-v1"
# Меньше этого остатка в дне резерв считается исчерпанным: вызов с таким остатком всё равно не поместится.
MIN_CALL_MICRO = 10_000
# Если число символов во входе неизвестно, берём предел текста после распознавания речи.
LONGEST_NOTE_CHARS = 12000
# Запас на служебные части запроса: системное правило, дата, категории.
PROMPT_OVERHEAD_CHARS = 4000


def to_micro(usd) -> int:
    """Доллары в миллионные доли, всегда вверх, чтобы оценка не оказалась меньше."""
    return int(math.ceil(Decimal(usd) * MICRO))


def day_key(settings, now=None) -> str:
    zone = ZoneInfo(settings.limit_timezone)
    return datetime.fromtimestamp(time.time() if now is None else now, zone).date().isoformat()


def llm_cost(settings, input_tokens, output_tokens) -> Decimal:
    """Цена вызова текстовой модели по числу токенов. Неизвестное число считается нулём."""
    return (
        Decimal(input_tokens or 0) * settings.fallback_price_input_per_m
        + Decimal(output_tokens or 0) * settings.fallback_price_output_per_m
    ) / Decimal(MICRO)


def llm_max_cost(settings, input_chars, max_tokens) -> Decimal:
    """Наибольшая возможная цена. Токенов во входе не больше, чем символов, а в ответе не больше max_tokens."""
    return llm_cost(settings, input_chars, max_tokens)


def stt_cost(settings, seconds) -> Decimal:
    return Decimal(str(max(0.0, float(seconds or 0)))) / Decimal(60) * settings.fallback_price_stt_per_minute


def stt_max_cost(settings, seconds, longest) -> Decimal:
    """Распознавание речи считается по полным минутам. Неизвестная длина считается самой длинной."""
    length = float(seconds) if seconds else float(longest)
    return Decimal(max(1, math.ceil(length / 60))) * settings.fallback_price_stt_per_minute


@dataclass(frozen=True)
class Reservation:
    day: str
    micro: int


class FallbackBudget:
    def __init__(self, sessions, settings):
        self.sessions, self.settings = sessions, settings

    @property
    def cap(self) -> int:
        return to_micro(self.settings.fallback_daily_usd)

    def spent(self, db, now=None) -> int:
        """Расход и резервы за сегодня в миллионных долях доллара."""
        return db.scalar(
            select(FallbackSpend.committed_micro_usd).where(FallbackSpend.day == day_key(self.settings, now))
        ) or 0

    def exhausted(self, db, now=None) -> bool:
        return self.spent(db, now) + MIN_CALL_MICRO > self.cap

    def fits(self, db, usd, now=None) -> bool:
        """Поместится ли вызов с такой наибольшей ценой в остаток дня. Подсказка, решает reserve."""
        return self.spent(db, now) + to_micro(usd) <= self.cap

    def reserve(self, usd, now=None) -> Reservation | None:
        """Занять место под вызов. None, если потолок дня не позволяет."""
        now = time.time() if now is None else now
        day, amount = day_key(self.settings, now), to_micro(usd)
        with self.sessions() as db:
            if db.get(FallbackSpend, day) is None:
                try:
                    with db.begin_nested():
                        db.add(FallbackSpend(day=day, committed_micro_usd=0, updated_at=now))
                        db.flush()
                except IntegrityError:
                    pass  # Другой воркер создал строку раньше.
            taken = db.execute(
                update(FallbackSpend)
                .where(FallbackSpend.day == day, FallbackSpend.committed_micro_usd + amount <= self.cap)
                .values(committed_micro_usd=FallbackSpend.committed_micro_usd + amount, updated_at=now)
            ).rowcount
            db.commit()
        return Reservation(day, amount) if taken else None

    def settle(self, reservation: Reservation, actual_usd) -> None:
        """Заменить резерв настоящей оценкой. Нулевая оценка полностью возвращает резерв."""
        delta = to_micro(actual_usd) - reservation.micro
        if delta == 0:
            return
        with self.sessions() as db:
            total = FallbackSpend.committed_micro_usd + delta
            db.execute(
                update(FallbackSpend).where(FallbackSpend.day == reservation.day)
                .values(committed_micro_usd=case((total < 0, 0), else_=total), updated_at=time.time())
            )
            db.commit()


class AttemptSpend:
    """Резерв одной попытки обработки. Подсчитывает, что фактически потрачено на её этапах."""

    def __init__(self, budget: FallbackBudget, reservation: Reservation, estimates: dict):
        self.budget, self.reservation, self.estimates = budget, reservation, estimates
        self.actual = Decimal(0)
        self.closed = False

    def stage_done(self, kind, actual_usd) -> None:
        self.actual += Decimal(actual_usd)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.budget.settle(self.reservation, self.actual)
