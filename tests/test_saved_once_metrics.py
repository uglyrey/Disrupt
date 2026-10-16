"""A first use is a real save, not a registration, repeat, background task or test account."""

from app.analytics import record_event
from app.contest_metrics import first_targets, report
from app.models import User, new_id
from tests.test_core_scenario import at


def account(db, name, *, test=False, source="direct"):
    row = User(username=name, password_hash="synthetic", is_test=test, source=source, created_at=at("2026-10-04"))
    db.add(row)
    db.flush()
    return row


def test_first_use_deduplicates_channels_and_excludes_registration_and_background(app):
    with app.state.sessions() as db:
        used = account(db, "saved-user", source="campaign")
        registered = account(db, "registered-only")
        background = account(db, "background-only")
        record_event(db, registered.id, "registered", registered.id, occurred_at=at("2026-10-04"))
        record_event(db, background.id, "processing_completed", new_id(), occurred_at=at("2026-10-05"))
        operation = new_id()
        record_event(db, used.id, "capture_saved", operation, "web", occurred_at=at("2026-10-05"))
        record_event(db, used.id, "capture_saved", operation, "web", occurred_at=at("2026-10-06"))
        record_event(db, used.id, "capture_saved", new_id(), "telegram", occurred_at=at("2026-10-06"))
        db.commit()
        assert first_targets(db, at("2026-10-07")) == {used.id: at("2026-10-05")}
        assert first_targets(db, at("2026-10-07"), channel="telegram") == {used.id: at("2026-10-06")}
        assert first_targets(db, at("2026-10-07"), source="organic") == {}
        assert first_targets(db, at("2026-10-07"), source="campaign") == {used.id: at("2026-10-05")}
        assert first_targets(db, at("2026-10-05")) == {}


def test_test_snapshots_stay_excluded_even_after_unmarking_and_future_saves_wait(app):
    with app.state.sessions() as db:
        team = account(db, "team-user", test=True)
        former_test = account(db, "former-test", test=True)
        future = account(db, "future-user")
        record_event(db, team.id, "capture_saved", new_id(), occurred_at=at("2026-10-04"))
        record_event(db, former_test.id, "capture_saved", new_id(), occurred_at=at("2026-10-04"))
        former_test.is_test = False
        db.flush()
        record_event(db, future.id, "capture_saved", new_id(), occurred_at=at("2026-10-08"))
        db.commit()
        assert first_targets(db, at("2026-10-07")) == {}
        record_event(db, former_test.id, "capture_saved", new_id(), occurred_at=at("2026-10-06"))
        db.commit()
        assert first_targets(db, at("2026-10-07")) == {former_test.id: at("2026-10-06")}


def test_cumulative_save_and_period_activity_are_separate_from_viewed_result(app):
    with app.state.sessions() as db:
        used = account(db, "old-save")
        record_event(db, used.id, "capture_saved", new_id(), occurred_at=at("2026-10-04"))
        record_event(db, used.id, "reminder_confirmed", new_id(), occurred_at=at("2026-10-06"))
        db.commit()
        value = report(db, "2026-10-05", "2026-10-06", now=at("2026-10-07"))
        assert value["real_users"] == 1
        assert value["saved_and_viewed_users"] == 0
        assert [day["dau"] for day in value["daily"]] == [0, 1]
        assert sum(row["real_users"] for row in value["sources"]) == 1
