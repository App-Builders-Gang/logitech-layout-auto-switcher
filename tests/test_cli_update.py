"""The update and selfupdate commands through the CLI surface."""

from __future__ import annotations

import pytest

from logiswitch import cli, service, updater
from logiswitch.updater import Release


def run(capsys, *args) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def fake_update(monkeypatch):
    monkeypatch.setattr(updater, "installed_version", lambda: "2.0.3")
    monkeypatch.setattr(updater, "is_managed_environment", lambda: True)
    return monkeypatch


def test_update_check_reports_an_available_update(fake_update, capsys):
    fake_update.setattr(
        updater,
        "latest_release",
        lambda: Release(version="2.0.4", wheel_url="https://x/p.whl"),
    )

    code, out, _err = run(capsys, "update", "--check")

    assert code == 0
    assert "2.0.3 -> 2.0.4" in out
    assert "https://x/p.whl" in out


def test_update_check_says_already_latest(fake_update, capsys):
    fake_update.setattr(
        updater,
        "latest_release",
        lambda: Release(version="2.0.3", wheel_url="https://x/p.whl"),
    )

    code, out, _err = run(capsys, "update", "--check")

    assert code == 0
    assert "already on the latest" in out


def test_update_check_fails_gracefully_when_offline(fake_update, capsys):
    def boom():
        raise updater.UpdateError("offline")

    fake_update.setattr(updater, "latest_release", boom)

    code, out, err = run(capsys, "update", "--check")

    assert code == 1
    msg = (out + err).lower()
    assert "could not determine" in msg or "network" in msg


def test_update_applies_and_reports_the_new_version(fake_update, capsys):
    calls = {"upgrade": False}
    fake_update.setattr(service, "status", lambda: {"installed": False})
    fake_update.setattr(
        updater, "upgrade", lambda **kw: calls.__setitem__("upgrade", True) or "2.0.4"
    )

    code, out, _err = run(capsys, "update")

    assert code == 0
    assert calls["upgrade"] is True
    assert "logiswitch is now 2.0.4" in out


def test_update_refuses_in_a_development_checkout(fake_update, capsys):
    fake_update.setattr(updater, "is_managed_environment", lambda: False)

    code, _out, err = run(capsys, "update")

    assert code == 1
    assert "development" in err.lower() or "editable" in err.lower()


def test_a_failed_update_restarts_the_agent_and_reports_the_error(fake_update, capsys):
    fake_update.setattr(service, "status", lambda: {"installed": True})
    restarted = {"yes": False}
    fake_update.setattr(service, "start", lambda: restarted.__setitem__("yes", True))
    fake_update.setattr(
        updater, "upgrade", lambda **kw: (_ for _ in ()).throw(updater.UpdateError("pip broke"))
    )

    code, _out, err = run(capsys, "update")

    assert code == 1
    assert restarted["yes"] is True, "the agent must be brought back on failure"
    assert "pip broke" in err


def test_selfupdate_is_an_alias_of_update(fake_update, capsys):
    calls = {"n": 0}

    def fake_upgrade(**kw):
        calls["n"] += 1
        return "2.0.4"

    fake_update.setattr(service, "status", lambda: {"installed": False})
    fake_update.setattr(updater, "upgrade", fake_upgrade)

    code, _out, _err = run(capsys, "selfupdate")

    assert code == 0 and calls["n"] == 1
