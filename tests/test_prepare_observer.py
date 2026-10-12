"""Only synthetic enrollments. Credentials must be written privately, never printed."""

import time

import pytest
from sqlalchemy import select

from app.admin_models import AdminAccount
from scripts import prepare_observer


def test_prepare_creates_only_an_inactive_expiring_observer(app, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prepare_observer, "EXPORT_DIRECTORY", tmp_path)
    assert prepare_observer.main(["create", "organizer-1", "--role", "observer"]) == 0
    with app.state.sessions() as db:
        row = db.scalar(select(AdminAccount))
        assert row.role == 'observer' and row.access_expires_at > time.time()
        assert not row.totp_enabled
        secret = row.totp_secret
    output = capsys.readouterr().out
    assert secret not in output
    bundle = tmp_path / 'organizer-1.txt'
    assert bundle.stat().st_mode & 0o777 == 0o600
    contents = bundle.read_text()
    assert secret in contents and 'Password:' in contents
    password = contents.split('Password: ')[1].splitlines()[0]
    assert password not in output and len(password) >= 32
    with pytest.raises(FileExistsError):
        prepare_observer.main(["create", "organizer-1", "--role", "observer"])
    assert bundle.read_text() == contents


def test_prepare_refuses_owner_or_traversal_and_cleans_failed_reservation(app, tmp_path, monkeypatch):
    monkeypatch.setattr(prepare_observer, "EXPORT_DIRECTORY", tmp_path)
    for args in [["create", "owner"], ["create", "../other", "--role", "observer"],
                 ["create", "observer", "--role", "observer", "--expires", "2020-01-01T00:00:00Z"]]:
        with pytest.raises(SystemExit):
            prepare_observer.main(args)
    assert list(tmp_path.glob('*.txt')) == []
    with app.state.sessions() as db:
        assert db.scalar(select(AdminAccount)) is None
