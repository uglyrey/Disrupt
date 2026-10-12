"""Одноразовая чистка старой истории заметок. Подробности в docs/note-revisions-v1.md.

python -m app.revisions_cli compact           только показывает, что будет объединено
python -m app.revisions_cli compact --apply   объединяет

Старые версии не помечены видом правки. Подряд идущие правки только названия и текста,
сделанные с паузами короче окна NOTES_REVISION_COALESCE_SECONDS, считаются одной сессией.
От сессии остаётся последняя версия. Первая версия, структура, категория, элементы и выводы не трогаются.
"""

import argparse
import sys

from sqlalchemy import select

from app import crypto
from app.config import Settings
from app.db import make_database
from app.models import Revision

# Эти ключи меняются при каждой правке текста и не говорят о смене структуры.
TEXT_KEYS = {"title", "markdown", "structure_confirmed_at"}


def _only_text_changed(older, newer):
    if older.get("title") == newer.get("title") and older.get("markdown") == newer.get("markdown"):
        return False
    keys = {k for k in set(older) | set(newer) if not k.startswith("_")} - TEXT_KEYS
    return all(older.get(k) == newer.get(k) for k in keys)


def plan_compaction(rows, coalesce, longest):
    """rows: версии одной заметки. Возвращает (список на удаление, {id: время начала сессии})."""
    rows = sorted(rows, key=lambda r: r.version)
    delete, started = [], {}
    previous = None  # Последняя версия, которая сама была правкой текста.
    chain_start = None
    before = None
    for row in rows:
        legacy = "_kind" not in row.snapshot
        is_edit = bool(before is not None and legacy and row.version > 1 and _only_text_changed(before.snapshot, row.snapshot))
        if (
            is_edit and previous is not None and coalesce > 0
            and row.created_at - previous.created_at < coalesce and row.created_at - chain_start < longest
        ):
            delete.append(previous.id)
            started[row.id] = chain_start
            previous = row
        elif is_edit:
            previous, chain_start = row, row.created_at
        else:
            previous = None
        before = row
    # В цепочке остаётся только последняя запись, поэтому начало берётся у неё.
    return delete, {k: v for k, v in started.items() if k not in delete}


def compact(db, settings, *, apply=False):
    removed = notes = 0
    for note_id in db.scalars(select(Revision.note_id).distinct()).all():
        rows = db.scalars(select(Revision).where(Revision.note_id == note_id)).all()
        delete, started = plan_compaction(rows, settings.revision_coalesce_seconds, settings.revision_session_max_seconds)
        if not delete:
            continue
        notes += 1
        removed += len(delete)
        if not apply:
            continue
        by_id = {r.id: r for r in rows}
        for revision_id in delete:
            db.delete(by_id[revision_id])
        for revision_id, start in started.items():
            row = by_id[revision_id]
            row.snapshot = {**row.snapshot, "_kind": "edit", "_closed": True, "_started": start, "_sid": None}
        db.commit()
    return notes, removed


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.revisions_cli", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("compact")
    command.add_argument("--apply", action="store_true", help="выполнить, без этого только показать")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    crypto.configure_from_settings(settings)
    engine, sessions = make_database(settings.database_url)
    try:
        with sessions() as db:
            notes, removed = compact(db, settings, apply=args.apply)
    finally:
        engine.dispose()
    print(f"Заметок затронуто {notes}, версий {'удалено' if args.apply else 'будет удалено'} {removed}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
