"""Content-free evidence of executed stages. No polling, health checks or fabricated history."""

import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import ComponentCall, User, new_id

# The ledger is complete; admission of non-model stages remains explicit and reviewable.
MODEL_COMPONENTS = {"llm", "stt"}
EXTRA_COMPONENTS = {
    "structure_prompt": "skill",
    "assistant_ask_prompt": "skill",
    "assistant_recommend_prompt": "skill",
    "assistant_digest_prompt": "skill",
    "capture_worker": "background",
    "assistant_worker": "background",
    "telegram_send": "tool",
    "note_view": "target",
}


def record(
    db,
    user_id,
    operation_id,
    component,
    channel="web",
    *,
    status="succeeded",
    error_code=None,
    is_test=False,
    occurred_at=None,
):
    if component not in EXTRA_COMPONENTS:
        raise ValueError("Unknown component")
    user = db.get(User, user_id)
    if user is None:
        return
    db.refresh(user)
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    db.execute(
        insert(ComponentCall)
        .values(
            id=new_id(),
            user_id=user_id,
            operation_id=operation_id,
            component=component,
            kind=EXTRA_COMPONENTS[component],
            channel=channel,
            status=status,
            error_code=error_code,
            is_test=user.is_test or is_test,
            occurred_at=time.time() if occurred_at is None else occurred_at,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "operation_id", "component"])
    )


def finish(db, user_id, operation_id, component, status, error_code=None):
    row = db.scalar(
        select(ComponentCall).where(
            ComponentCall.user_id == user_id,
            ComponentCall.operation_id == operation_id,
            ComponentCall.component == component,
        )
    )
    if row is not None:
        row.status, row.error_code = status, error_code
