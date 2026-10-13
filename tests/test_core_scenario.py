"""The basic value is saved input plus its viewed result. No extra product features required."""

from datetime import datetime

from sqlalchemy import select

from app.admin_metrics import aggregate
from app.analytics import record_event
from app.contest_metrics import first_targets, report
from app.models import Capture, Note, ProductEvent, ProviderUsage, User, new_id


def at(day):
    return datetime.fromisoformat(day + "T12:00:00+03:00").timestamp()


def seed(db, name, *, saved="2026-10-04", viewed=None, mode="manual", channel="web", test=False):
    user = User(
        username=name,
        password_hash="synthetic-hash",
        created_at=at(saved),
        first_login_at=at(saved),
        is_test=test,
    )
    db.add(user)
    db.flush()
    capture = Capture(
        user_id=user.id,
        original_text="Synthetic",
        idempotency_key=new_id(),
        processing_mode=mode,
        channel=channel,
        created_at=at(saved),
    )
    db.add(capture)
    db.flush()
    note = Note(
        user_id=user.id,
        capture_id=capture.id,
        title="Synthetic",
        markdown="Synthetic",
        provider="mock",
        created_at=at(saved),
    )
    db.add(note)
    db.flush()
    record_event(db, user.id, "capture_saved", capture.id, channel, occurred_at=at(saved))
    if viewed:
        record_event(db, user.id, "note_opened", new_id(), occurred_at=at(viewed), subject_id=note.id)
    return user, note


def test_simple_manual_and_ai_notes_qualify_without_any_other_feature(app):
    with app.state.sessions() as db:
        manual, _ = seed(db, "manual", viewed="2026-10-06")
        ai, _ = seed(db, "ai", mode="ai", viewed="2026-10-06")
        unfinished, _ = seed(db, "unfinished")
        team, _ = seed(db, "team", viewed="2026-10-06", test=True)
        db.commit()
        value, _ = aggregate(
            db, "2026-10-05", "2026-10-06", "all", "all", b"synthetic-key", now=at("2026-10-07")
        )
        assert value.cards.completed_scenario == 2  # Saving before the selected window is still valid.
        assert value.cards.dau == 2
        assert value.cards.saved_users == value.cards.ready_users == 0
        assert set(first_targets(db, at("2026-10-07"))) == {manual.id, ai.id}
        assert unfinished.id not in first_targets(db, at("2026-10-07"))
        assert team.id not in first_targets(db, at("2026-10-07"))


def test_viewing_a_different_unsaved_note_cannot_complete_the_saved_capture(app):
    with app.state.sessions() as db:
        user, _ = seed(db, "split-notes")
        other = Capture(
            user_id=user.id, original_text="Synthetic", idempotency_key=new_id(), created_at=at("2026-10-04")
        )
        db.add(other)
        db.flush()
        note = Note(
            user_id=user.id, capture_id=other.id, title="Synthetic", markdown="Synthetic", provider="mock"
        )
        db.add(note)
        db.flush()
        record_event(db, user.id, "note_opened", new_id(), subject_id=note.id, occurred_at=at("2026-10-06"))
        db.commit()
        assert first_targets(db, at("2026-10-07")) == {}


def test_day_ratio_uses_only_qualified_active_people_and_excludes_mock(app):
    with app.state.sessions() as db:
        active, _ = seed(db, "active", viewed="2026-10-06")
        other, _ = seed(db, "unqualified")
        for user, model in [
            (active, "synthetic-paid-model"),
            (active, "mock"),
            (other, "synthetic-paid-model"),
        ]:
            db.add(
                ProviderUsage(
                    user_id=user.id,
                    operation_id=new_id(),
                    request_id=new_id(),
                    model=model,
                    kind="llm",
                    channel="web",
                    status="succeeded",
                    occurred_at=at("2026-10-06"),
                )
            )
        db.commit()
        result = report(db, "2026-10-05", "2026-10-06", now=at("2026-10-07"))
        assert result["daily"][0]["dau"] == result["daily"][0]["calls"] == 0
        assert result["daily"][1]["dau"] == result["daily"][1]["calls"] == 1
        assert result["average_dau"] == result["average_calls_per_dau"] == 0.5


def test_view_operation_is_idempotent_and_cannot_be_reused_for_another_note(app, client):
    from tests.conftest import register

    register(client)
    notes = []
    for i in range(2):
        response = client.post(
            "/api/v1/captures/text",
            headers={"Idempotency-Key": str(i)},
            json={"text": "Synthetic note", "processing_mode": "manual"},
        )
        assert response.status_code == 202
        notes.append(response.json()["note_id"])
    operation = new_id()
    for _ in range(2):
        assert (
            client.post("/api/v1/notes/" + notes[0] + "/opened", json={"operation_id": operation}).status_code
            == 204
        )
    assert (
        client.post("/api/v1/notes/" + notes[1] + "/opened", json={"operation_id": operation}).status_code
        == 409
    )
    with app.state.sessions() as db:
        assert len(db.scalars(select(ProductEvent).where(ProductEvent.name == "note_opened")).all()) == 1


def test_authenticated_errors_record_templates_without_user_content(app, client):
    from app.models import RequestError
    from tests.conftest import register

    register(client)
    missing = new_id()
    assert client.get("/api/v1/notes/" + missing + "?q=private-question").status_code == 404
    with app.state.sessions() as db:
        error = db.scalar(select(RequestError))
        assert error.status_code == 404 and error.error_code == "http_404"
        assert error.route == "/api/v1/notes/{note_id}" and error.method == "GET"
        assert missing not in error.route and "private-question" not in error.route


def test_reserve_counts_as_a_call_but_never_as_a_ruble_cost_or_legacy_gap(app):
    from decimal import Decimal

    from app.models import Job

    with app.state.sessions() as db:
        user, note = seed(db, "mixed-providers", viewed="2026-10-06")
        job = Job(
            user_id=user.id,
            capture_id=note.capture_id,
            provider="cloudru",
            status="succeeded",
            created_at=at("2026-10-06"),
            finished_at=at("2026-10-06"),
        )
        db.add(job)
        db.flush()
        for provider, operation in [("cloudru", new_id()), ("openai", job.id)]:
            db.add(
                ProviderUsage(
                    user_id=user.id,
                    operation_id=operation,
                    request_id=new_id(),
                    kind="llm",
                    model="synthetic-paid-model",
                    provider=provider,
                    channel="web",
                    status="succeeded",
                    occurred_at=at("2026-10-06"),
                    input_tokens=10,
                    output_tokens=10,
                    cache_tokens=0,
                    cost=Decimal(".25") if provider == "cloudru" else None,
                    estimated_cost=Decimal(".02") if provider == "openai" else None,
                )
            )
        db.commit()
        contest = report(db, "2026-10-05", "2026-10-06", now=at("2026-10-07"))
        assert contest["daily"][1]["calls"] == 2
        stats, _ = aggregate(
            db, "2026-10-05", "2026-10-06", "all", "all", b"synthetic-key", now=at("2026-10-07")
        )
        assert stats.quality.costs.llm_calls == 1
        assert stats.quality.costs.llm_cost == "0.25000000"
