"""Putting the command on PATH.

The decision logic is pure and fully testable on any OS; the OS-specific writes
(winreg / symlink) are exercised on their own platform, against the real registry
on Windows and a throwaway HOME on macOS, with cleanup so nothing is left behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from logiswitch import service

win = pytest.mark.skipif(not service.is_windows(), reason="Windows only")
mac = pytest.mark.skipif(not service.is_macos(), reason="macOS only")


# -- pure decision logic (every OS) -------------------------------------------


def test_add_to_list_appends_a_missing_entry():
    parts, changed = service._add_to_list(["/a", "/b"], "/c")
    assert changed is True
    assert parts == ["/a", "/b", "/c"]


def test_add_to_list_is_case_insensitive():
    parts, changed = service._add_to_list(["C:\\VenV\\Scripts"], "c:\\venv\\scripts")
    assert changed is False
    assert parts == ["C:\\VenV\\Scripts"]


def test_add_to_list_handles_an_empty_path():
    assert service._add_to_list([], "/x") == (["/x"], True)


def test_entry_point_dir_points_at_the_running_venv(monkeypatch):
    # Compare with the same join the function uses, so the assertion holds on
    # every host OS rather than assuming a platform's separator.
    monkeypatch.setattr(service, "is_windows", lambda: True)
    monkeypatch.setattr(service.sys, "prefix", "C:\\venv")
    assert service._entry_point_dir() == Path("C:\\venv") / "Scripts"

    monkeypatch.setattr(service, "is_windows", lambda: False)
    monkeypatch.setattr(service.sys, "prefix", "/opt/venv")
    assert service._entry_point_dir() == Path("/opt/venv") / "bin"


# -- Windows: real registry, cleaned up ---------------------------------------


@win
def test_windows_ensure_on_path_round_trip_is_idempotent():
    """Add the entry to the real user PATH, then prove a second call is a no-op,
    then restore the original value so the test leaves the host unchanged."""
    import winreg

    target = str(service._entry_point_dir())
    parts_before, reg_type = service._windows_path_parts()
    already = target.lower() in [p.lower() for p in parts_before]
    try:
        first = service._windows_ensure_on_path()
        assert first is not already
        parts_after, _ = service._windows_path_parts()
        assert target.lower() in [p.lower() for p in parts_after]

        assert service._windows_ensure_on_path() is False, "a second call must not write again"
    finally:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(parts_before))


# -- macOS: throwaway HOME ----------------------------------------------------


@mac
def test_macos_ensure_on_path_symlinks_into_local_bin(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    venv_bin = tmp_path / "v" / "bin"
    venv_bin.mkdir(parents=True)
    target = venv_bin / "logiswitch"
    target.touch()
    monkeypatch.setattr(service.sys, "prefix", str(venv_bin.parent))

    assert service._macos_ensure_on_path() is True

    link = tmp_path / ".local" / "bin" / "logiswitch"
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


@mac
def test_macos_ensure_on_path_is_idempotent_when_link_already_points_there(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    venv_bin = tmp_path / "v" / "bin"
    venv_bin.mkdir(parents=True)
    target = venv_bin / "logiswitch"
    target.touch()
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    (tmp_path / ".local" / "bin" / "logiswitch").symlink_to(target)
    monkeypatch.setattr(service.sys, "prefix", str(venv_bin.parent))

    assert service._macos_ensure_on_path() is False
