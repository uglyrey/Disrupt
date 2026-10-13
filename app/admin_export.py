"""Streaming, content-free evidence with versioned methodology and bounded ZIP size."""

import io
import math
import time
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import Integer, cast, literal, select, union_all

from app.admin_metrics import calendar_day, midnight, money, pseudonym, safe_cell, utc
from app.contest_metrics import ACTIVE, METHOD_VERSION, first_targets, report
from app.csv_export import excel_csv_writer
from app.models import (
    AssistantRequest,
    Capture,
    ComponentCall,
    Job,
    Note,
    ProductEvent,
    ProviderUsage,
    RequestError,
    User,
)

MAX_DAYS = 92
MAX_ROWS = 250_000  # Per CSV part, not a refusal threshold.
MAX_BYTES = 64 * 1024 * 1024


def archive_period(start, end, now):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(422, "Проверьте даты периода") from None
    if first > last or (last - first).days + 1 > MAX_DAYS or last > calendar_day(now):
        raise HTTPException(422, f"Выберите период до сегодняшнего дня, не длиннее {MAX_DAYS} дней")
    return first, last


def too_large():
    return HTTPException(413, "Архив слишком большой. Выберите период короче.")


def make_csv(header, rows):
    out = io.StringIO(newline="")
    writer = excel_csv_writer(out)
    writer.writerow(header)
    for row in rows:
        writer.writerow([safe_cell(cell) for cell in row])
    return out.getvalue()


def readme(first, last, generated, stable_key):
    stability = (
        "один и тот же псевдоним во всех файлах и во всех архивах"
        if stable_key
        else "псевдонимы меняются после перезапуска сервиса"
    )
    return "\n".join(
        [
            "Архив метрик beresta",
            f"Период {first.isoformat()} - {last.isoformat()}, Europe/Moscow.",
            f"Архив собран {generated} (UTC).",
            f"Методика {METHOD_VERSION}.",
            "",
            "Реальный пользователь выполнил хотя бы один целевой сценарий: сохранил запись и просмотрел готовую заметку.",
            "Критерий одинаков для записей с ИИ и без ИИ. Подтверждать структуру или менять заметку не требуется.",
            "Отсутствующие исторические просмотры не восстанавливаются предположениями.",
            "DAU включает реальные пользовательские действия и запросы помощнику, начиная с дня первого целевого сценария.",
            "Тестовые аккаунты и события тестирования исключены. Веб и Telegram одного аккаунта считаются одним пользователем.",
            "Для отношения обращений к DAU используется одна группа пользователей в числителе и знаменателе.",
            "Журналы показывают все нетестовые обращения. Поле counted показывает включение в показатель.",
            "Вызовы LLM и STT учитываются по квитанциям. Неподтверждённые этапы навыков, инструментов и фоновой работы",
            "записываются отдельно, но не включаются в зачёт без явного согласования методики компонентов.",
            "Опрос статуса, проверки доступности, обновление баланса и отладочные запросы не считаются компонентными обращениями.",
            "Среднесуточные значения включают нулевые дни. Незавершённый период помечен preliminary.",
            "",
            "daily.csv - дни и счётчики; users.csv - регистрация, первый сценарий, активные дни и источник;",
            "events.csv - события с обезличенными связями; ai_calls.csv - модельные квитанции;",
            "component_calls.csv - фактически выполненные прочие этапы; errors.csv и errors_by_day.csv - ошибки;",
            "sources.csv - источники привлечения; contest.json - выбранный и настроенный зачётный периоды.",
            "Большие CSV продолжаются в файлах .part-0002.csv и следующих. Все части содержат заголовок.",
            "CSV имеют UTF-8 BOM, разделитель ; и CRLF. Пустое значение означает неизвестное.",
            "",
            "Пользователи заменены псевдонимами HMAC-SHA256, используются " + stability + ".",
            "В архиве нет имён, контактов, паролей, текстов заметок, вопросов помощнику, расшифровок или аудио.",
            "Источники unknown сохраняются, если сведения о привлечении не были записаны.",
            "",
        ]
    )


def build_archive(
    db,
    start,
    end,
    key,
    *,
    stable_key=True,
    now=None,
    counted_components=("llm", "stt"),
    contest_period=("2026-11-03", "2026-11-30"),
):
    now = time.time() if now is None else now
    first, last = archive_period(start, end, now)
    low, high = midnight(first), min(midnight(last + timedelta(days=1)), math.nextafter(now, math.inf))
    selected = report(db, start, end, counted_components=counted_components, now=now)
    official = report(db, *contest_period, counted_components=counted_components, now=now)
    qualified = first_targets(db, high)
    accounts = {
        r.id: r
        for r in db.execute(
            select(User.id, User.created_at, User.first_login_at, User.source).where(User.is_test.is_(False))
        )
    }
    note_links = {
        row.id: (row.capture_id, row.user_id)
        for row in db.execute(
            select(Note.id, Note.capture_id, Note.user_id).where(Note.user_id.in_(accounts))
        )
    }
    job_links = {
        row.id: (row.capture_id, row.user_id)
        for row in db.execute(select(Job.id, Job.capture_id, Job.user_id).where(Job.user_id.in_(accounts)))
    }

    def capture_link(uid, operation_id, subject_id=None, name=None):
        if name == "capture_saved":
            return pseudonym(key, "capture", operation_id)
        note_id = subject_id or (operation_id.split(":", 1)[0] if ":" in operation_id else None)
        capture = note_links.get(note_id) or job_links.get(operation_id)
        return pseudonym(key, "capture", capture[0]) if capture and capture[1] == uid else None

    active_days, mix, touched = defaultdict(set), defaultdict(Counter), set()
    event_query = (
        select(ProductEvent)
        .join(User)
        .where(
            User.is_test.is_(False),
            ProductEvent.is_test.is_(False),
            ProductEvent.occurred_at >= low,
            ProductEvent.occurred_at < high,
        )
        .order_by(ProductEvent.occurred_at, ProductEvent.id)
    )
    for e in db.scalars(event_query.execution_options(yield_per=2000)):
        touched.add(e.user_id)
        if e.name in ACTIVE:
            active_days[e.user_id].add(calendar_day(e.occurred_at))
            mix[e.user_id][e.channel] += 1

    def counted(uid, at, component):
        d = calendar_day(at)
        return (
            uid in qualified
            and d >= calendar_day(qualified[uid])
            and d in active_days[uid]
            and component in counted_components
        )

    capture_counts = defaultdict(Counter)
    for at, kind, channel in db.execute(
        select(Capture.created_at, Capture.input_kind, Capture.channel)
        .join(User)
        .where(
            User.is_test.is_(False),
            Capture.created_at >= low,
            Capture.created_at < high,
        )
        .execution_options(yield_per=2000)
    ):
        capture_counts[calendar_day(at)]["captures_" + ("audio" if kind == "audio" else "text")] += 1
        capture_counts[calendar_day(at)]["captures_" + channel] += 1
    error_queries = []
    for model, label in ((Job, "job"), (AssistantRequest, "assistant")):
        error_queries.append(
            select(
                model.finished_at.label("at"),
                literal(label).label("source"),
                model.error_code.label("code"),
                model.user_id.label("uid"),
                model.id.label("operation"),
                literal(None).label("route"),
                literal(None).label("method"),
                cast(literal(None), Integer).label("status_code"),
            )
            .join(User)
            .where(
                User.is_test.is_(False),
                model.status == "failed",
                model.finished_at >= low,
                model.finished_at < high,
            )
        )
    error_queries.append(
        select(
            RequestError.occurred_at.label("at"),
            literal("http").label("source"),
            RequestError.error_code.label("code"),
            RequestError.user_id.label("uid"),
            RequestError.id.label("operation"),
            RequestError.route.label("route"),
            RequestError.method.label("method"),
            RequestError.status_code.label("status_code"),
        )
        .join(User)
        .where(
            User.is_test.is_(False),
            RequestError.is_test.is_(False),
            RequestError.occurred_at >= low,
            RequestError.occurred_at < high,
        )
    )
    error_query = union_all(*error_queries).order_by("at", "source", "operation")
    error_days = Counter()
    for r in db.execute(error_query.execution_options(yield_per=2000)):
        error_days[(calendar_day(r.at), r.source, r.code or "unknown")] += 1
    model_query = (
        select(ProviderUsage)
        .join(User)
        .where(
            User.is_test.is_(False),
            ProviderUsage.is_test.is_(False),
            ProviderUsage.occurred_at >= low,
            ProviderUsage.occurred_at < high,
        )
        .order_by(ProviderUsage.occurred_at, ProviderUsage.id)
    )
    component_query = (
        select(ComponentCall)
        .join(User)
        .where(
            User.is_test.is_(False),
            ComponentCall.is_test.is_(False),
            ComponentCall.occurred_at >= low,
            ComponentCall.occurred_at < high,
        )
        .order_by(ComponentCall.occurred_at, ComponentCall.id)
    )
    stats = {
        "days": len(selected["daily"]),
        "users": 0,
        "events": 0,
        "ai_calls": 0,
        "components": 0,
        "errors": 0,
    }
    buffer = io.BytesIO()

    def who(uid):
        return pseudonym(key, "user", uid)

    def operation(uid):
        return pseudonym(key, "operation", uid)

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:

        def write(name, header, rows):
            part, count, stream, text = 1, 0, None, None
            try:
                for row in rows:
                    if stream is None or count >= MAX_ROWS:
                        if text is not None:
                            text.flush()
                            text.close()
                        filename = name if part == 1 else name[:-4] + f".part-{part:04d}.csv"
                        stream = archive.open(filename, "w", force_zip64=True)
                        text = io.TextIOWrapper(stream, encoding="utf-8", newline="")
                        writer = excel_csv_writer(text)
                        writer.writerow(header)
                        count = 0
                        part += 1
                    writer.writerow([safe_cell(cell) for cell in row])
                    count += 1
                    if buffer.tell() > MAX_BYTES:
                        raise too_large()
                if stream is None:
                    with archive.open(name, "w") as empty:
                        empty.write(make_csv(header, []).encode("utf-8"))
            finally:
                if text is not None:
                    text.flush()
                    text.close()

        def user_rows():
            for uid, a in sorted(accounts.items()):
                at = a.created_at if a.created_at is not None else a.first_login_at
                if not (at is not None and at < high or uid in touched):
                    continue
                stats["users"] += 1
                yield (
                    who(uid),
                    calendar_day(at).isoformat() if at is not None else None,
                    calendar_day(qualified[uid]).isoformat() if uid in qualified else None,
                    len(
                        [
                            d
                            for d in active_days[uid]
                            if uid in qualified and d >= calendar_day(qualified[uid])
                        ]
                    ),
                    "|".join(f"{c}:{n}" for c, n in sorted(mix[uid].items())) or None,
                    a.source,
                )

        def events():
            for e in db.scalars(event_query.execution_options(yield_per=2000)):
                stats["events"] += 1
                yield (
                    who(e.user_id),
                    utc(e.occurred_at),
                    e.name,
                    e.channel,
                    e.source,
                    operation(e.operation_id),
                    pseudonym(key, "session", e.session_id),
                    e.outcome,
                    pseudonym(key, "note", e.subject_id) if e.subject_id else None,
                    capture_link(e.user_id, e.operation_id, e.subject_id, e.name),
                )

        def models():
            for r in db.scalars(model_query.execution_options(yield_per=2000)):
                stats["ai_calls"] += 1
                yield (
                    utc(r.occurred_at),
                    who(r.user_id),
                    "assistant" if r.request_id.split(":")[1:2] == ["assistant"] else r.kind,
                    r.model,
                    r.status,
                    r.latency_ms,
                    r.input_tokens,
                    r.output_tokens,
                    money(r.cost),
                    operation(r.operation_id),
                    counted(r.user_id, r.occurred_at, r.kind) and r.model != "mock",
                    capture_link(r.user_id, r.operation_id),
                    r.provider, "USD" if r.provider == "openai" else "RUB", money(r.estimated_cost),
                )

        def components():
            for r in db.scalars(component_query.execution_options(yield_per=2000)):
                stats["components"] += 1
                yield (
                    utc(r.occurred_at),
                    who(r.user_id),
                    r.kind,
                    r.component,
                    r.status,
                    r.error_code,
                    operation(r.operation_id),
                    r.channel,
                    counted(r.user_id, r.occurred_at, r.component),
                )

        def errors():
            for r in db.execute(error_query.execution_options(yield_per=2000)):
                stats["errors"] += 1
                yield (
                    utc(r.at),
                    calendar_day(r.at).isoformat(),
                    r.source,
                    r.code or "unknown",
                    who(r.uid),
                    operation(r.operation),
                    r.route,
                    r.method,
                    r.status_code,
                )

        def daily():
            for d in selected["daily"]:
                day = date.fromisoformat(d["date"])
                counts = capture_counts[day]
                new = sum(
                    calendar_day(a.created_at if a.created_at is not None else a.first_login_at) == day
                    for uid, a in accounts.items()
                    if uid in qualified and (a.created_at or a.first_login_at) is not None
                )
                ratio = d["model_calls"] / d["dau"] if d["dau"] else None
                yield (
                    d["date"],
                    d["dau"],
                    new,
                    counts["captures_text"],
                    counts["captures_audio"],
                    counts["captures_web"],
                    counts["captures_telegram"],
                    d["model_calls"],
                    f"{ratio:.4f}" if ratio is not None else None,
                    sum(n for (at, _, _), n in error_days.items() if at == day),
                    d["calls"],
                    d["recorded_calls"],
                    f"{d['calls_per_dau']:.4f}" if d["calls_per_dau"] is not None else None,
                )

        archive.writestr("README.txt", readme(first, last, utc(now), stable_key))
        import json

        archive.writestr(
            "contest.json",
            json.dumps(
                {"selected": selected, "official_period": official}, ensure_ascii=False, sort_keys=True
            ),
        )
        write(
            "daily.csv",
            (
                "date",
                "dau",
                "new_users",
                "captures_text",
                "captures_audio",
                "captures_web",
                "captures_telegram",
                "ai_calls",
                "ai_calls_per_dau",
                "errors",
                "counted_calls",
                "recorded_calls",
                "calls_per_dau",
            ),
            daily(),
        )
        write(
            "users.csv",
            (
                "user_id",
                "registered_date",
                "first_target_scenario_date",
                "active_days",
                "channel_mix",
                "source",
            ),
            user_rows(),
        )
        write(
            "events.csv",
            (
                "user_id",
                "timestamp_utc",
                "event",
                "channel",
                "source",
                "operation_id",
                "session_id",
                "outcome",
                "subject_id",
                "capture_id",
            ),
            events(),
        )
        write(
            "ai_calls.csv",
            (
                "timestamp_utc",
                "user_id",
                "kind",
                "model",
                "status",
                "latency_ms",
                "input_tokens",
                "output_tokens",
                "cost",
                "operation_id",
                "counted",
                "capture_id", "provider", "currency", "estimated_cost",
            ),
            models(),
        )
        write(
            "component_calls.csv",
            (
                "timestamp_utc",
                "user_id",
                "kind",
                "component",
                "status",
                "error_code",
                "operation_id",
                "channel",
                "counted",
            ),
            components(),
        )
        write(
            "errors.csv",
            (
                "timestamp_utc",
                "date_msk",
                "source",
                "error_code",
                "user_id",
                "operation_id",
                "route",
                "method",
                "status_code",
            ),
            errors(),
        )
        write(
            "errors_by_day.csv",
            ("date_msk", "source", "error_code", "count"),
            ((d.isoformat(), s, c, n) for (d, s, c), n in sorted(error_days.items())),
        )
        write(
            "sources.csv",
            ("source", "registrations", "real_users"),
            ((r["source"], r["registrations"], r["real_users"]) for r in selected["sources"]),
        )
    if len(buffer.getvalue()) > MAX_BYTES:
        raise too_large()
    return buffer.getvalue(), stats
