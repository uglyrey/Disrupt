"""Выбор между основным ИИ-шлюзом (Cloud.ru) и резервом (OpenAI через Хельсинки).

Состояние лежит в базе, поэтому несколько воркеров видят одно и то же. Подробности в docs/ai-failover-v1.md.

* up: всё идёт на основной шлюз. Сбой класса «шлюз» (CLEAR и AMBIGUOUS из app.retry_policy) только копится.
* down: наступает после N сбоев подряд за окно или когда сработал предохранитель шлюза. Новые задания и
  повторы идут на резерв. Раз в `fallback_probe_seconds` ровно одно настоящее задание уходит на основной шлюз
  как проба. Удача возвращает состояние up, неудача оставляет down, и это задание сразу повторяется на резерве.
* Резерв вызывается только после сбоя шлюза. Ошибки запроса, ключа и самого ответа его не запускают.
"""

import time
from dataclasses import dataclass

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.error_logging import log_error
from app.fallback_budget import AttemptSpend, FallbackBudget, day_key
from app.models import FallbackSpend, ProviderRoute
from app.retry_policy import BAD_OUTPUT_CODES, FALLBACK_PROVIDER, GATEWAY_CODES, breaker_open

PRIMARY = "cloudru"
FALLBACK = FALLBACK_PROVIDER
# Сколько попыток задания ещё допускает немедленный перенос на резерв. Дальше работают обычные повторы.
MAX_FAILOVER_ATTEMPTS = 3


@dataclass
class Attempt:
    """Куда идёт одна попытка и сколько под неё зарезервировано на резерве."""

    target: str = PRIMARY
    probe: bool = False
    spend: AttemptSpend | None = None

    @property
    def fallback(self):
        return self.target == FALLBACK

    def finish(self):
        if self.spend is not None:
            self.spend.close()


class ProviderRouter:
    def __init__(self, sessions, settings, budget: FallbackBudget | None = None):
        self.sessions, self.settings = sessions, settings
        self.budget = budget or FallbackBudget(sessions, settings)
        self._cap_logged = None

    # Состояние

    def row(self, db, *, lock=False):
        row = db.get(ProviderRoute, PRIMARY)
        if row is None:
            try:
                with db.begin_nested():
                    db.add(ProviderRoute(id=PRIMARY, state="up", failures=0))
                    db.flush()
            except IntegrityError:
                pass  # Другой воркер создал строку раньше.
        if lock:
            return db.scalar(
                select(ProviderRoute).where(ProviderRoute.id == PRIMARY).with_for_update()
                .execution_options(populate_existing=True)
            )
        return db.get(ProviderRoute, PRIMARY)

    def log(self, event, code):
        log_error(event, code, log_file=self.settings.error_log_file)

    def fallback_open(self, db, now=None):
        """Резерв доступен, пока потолок дня не исчерпан."""
        return not self.budget.exhausted(db, now)

    # Выбор маршрута

    def choose(self, next_route=None, now=None):
        """(куда, проба ли). Проба достаётся ровно одному воркеру за интервал, это решает один условный UPDATE."""
        if next_route in (PRIMARY, FALLBACK):
            return next_route, False
        now = time.time() if now is None else now
        with self.sessions() as db:
            row = self.row(db)
            if row.state != "down" or self.budget.exhausted(db, now):
                db.commit()
                return PRIMARY, False
            claimed = db.execute(
                update(ProviderRoute)
                .where(
                    ProviderRoute.id == PRIMARY, ProviderRoute.state == "down",
                    or_(ProviderRoute.next_probe_at.is_(None), ProviderRoute.next_probe_at <= now),
                    or_(ProviderRoute.probe_until.is_(None), ProviderRoute.probe_until < now),
                )
                .values(
                    probe_until=now + self.settings.lease_seconds,
                    next_probe_at=now + self.settings.fallback_probe_seconds,
                )
            ).rowcount
            db.commit()
        return (PRIMARY, True) if claimed else (FALLBACK, False)

    def note_cap_reached(self, now=None):
        day = day_key(self.settings, now)
        if self._cap_logged != day:
            self._cap_logged = day
            self.log("fallback_cap_reached", "daily_usd_cap")

    # Итоги попыток

    def mark_up(self, row, now):
        if row.state == "down":
            self.log("provider_up", "gateway_recovered")
            row.state, row.changed_at = "up", now
        row.failures, row.probe_until, row.next_probe_at = 0, None, None

    def on_success(self, now=None):
        """Основной шлюз ответил. Свой короткий сеанс."""
        now = time.time() if now is None else now
        with self.sessions() as db:
            self.mark_up(self.row(db, lock=True), now)
            db.commit()

    def on_failure(self, db, provider_used, code, now):
        """Учесть сбой попытки в сеансе вызывающего. Сбои резерва основной шлюз не касаются."""
        if provider_used != PRIMARY:
            return
        row = self.row(db, lock=True)
        if code in BAD_OUTPUT_CODES:
            self.mark_up(row, now)  # Шлюз ответил, плох только сам ответ.
            return
        if code not in GATEWAY_CODES:
            return
        window = self.settings.fallback_failure_window_seconds
        recent = row.last_failure_at is not None and now - row.last_failure_at <= window
        row.failures = (row.failures + 1) if recent else 1
        row.last_failure_at = now
        if row.state == "up":
            if row.failures >= self.settings.fallback_failures_to_down or breaker_open(db, self.settings, now):
                row.state, row.changed_at = "down", now
                row.next_probe_at, row.probe_until = now + self.settings.fallback_probe_seconds, None
                self.log("provider_down", "gateway_failures")
        else:
            row.probe_until = None  # Проба провалилась, следующая через интервал от её начала.

    def failover_allowed(self, db, job, code, now):
        """Нужно ли сразу повторить задание на резерве, без обычной паузы."""
        return (
            code in GATEWAY_CODES and job.provider_used == PRIMARY
            and (job.attempts or 0) < MAX_FAILOVER_ATTEMPTS and self.fallback_open(db, now)
        )


def provider_status(db, settings, now=None):
    """Состояние для админки. Ничего не создаёт и не меняет."""
    now = time.time() if now is None else now
    row = db.get(ProviderRoute, PRIMARY)
    spent = db.scalar(select(FallbackSpend.committed_micro_usd).where(FallbackSpend.day == day_key(settings, now))) or 0
    return {
        "fallback_enabled": bool(settings.fallback_enabled),
        "primary_state": row.state if row is not None else "up",
        "primary_changed_at": row.changed_at if row is not None else None,
        "primary_failures": row.failures if row is not None else 0,
        "next_probe_at": row.next_probe_at if row is not None and row.state == "down" else None,
        "fallback_spent_micro_usd": spent,
        "fallback_cap_micro_usd": FallbackBudget(None, settings).cap,
    }
