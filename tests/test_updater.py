"""Self-update.

Network and pip are the two things that must not run in a test, so both are
swapped for fakes. What remains -- the version comparison, the asset selection,
and the stop/upgrade/restart ordering -- is exactly the logic worth pinning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from logiswitch import service, updater
from logiswitch.updater import Release

# -- version comparison -------------------------------------------------------


@pytest.mark.parametrize(
    ("installed", "latest", "expected"),
    [
        ("2.0.3", "2.0.3", False),
        ("2.0.3", "2.0.4", True),
        ("2.0.9", "2.1.0", True),
        ("2.9.9", "3.0.0", True),
        ("2.0.3", "2.0.3rc1", False),  # same core version; pre-release does not win
        ("2.0.3", "2.0.2", False),
        ("1.0.0", "1.0.0", False),
    ],
)
def test_update_availability_compares_versions(monkeypatch, installed, latest, expected):
    monkeypatch.setattr(updater, "installed_version", lambda: installed)
    release = Release(version=latest, wheel_url="https://example/x.whl")
    assert updater.is_update_available(release) is expected


def test_version_parser_ignores_a_leading_v_and_trailing_junk():
    assert updater._parse_version("v2.0.3") == (2, 0, 3)
    assert updater._parse_version("2.1.0+local") == (2, 1, 0)
    assert updater._parse_version("2") == (2, 0, 0)


# -- asset selection ----------------------------------------------------------


def test_find_wheel_picks_the_right_asset():
    assets = [
        {
            "name": "logitech_layout_auto_switcher-2.0.3-py3-none-any.whl",
            "browser_download_url": "https://x/whl",
        },
        {
            "name": "logitech_layout_auto_switcher-2.0.3.tar.gz",
            "browser_download_url": "https://x/src",
        },
        {
            "name": "logitech_layout_auto_switcher-2.0.4-py3-none-any.whl",
            "browser_download_url": "https://x/wrong",
        },
    ]
    assert updater._find_wheel(assets, "2.0.3") == "https://x/whl"


def test_find_wheel_returns_none_when_only_a_different_version_is_present():
    assets = [
        {
            "name": "logitech_layout_auto_switcher-2.0.4-py3-none-any.whl",
            "browser_download_url": "https://x",
        }
    ]
    assert updater._find_wheel(assets, "2.0.3") is None


def test_find_wheel_survives_a_malformed_asset():
    assert updater._find_wheel([{"name": "junk"}, {}, None], "2.0.3") is None  # type: ignore[list-item]


def test_release_wheel_is_downloaded_under_its_real_filename(scripted_upgrade, monkeypatch):
    """pip rejects a wheel whose filename is not a valid PEP 427 name.

    An earlier version saved the download as ``logiswitch-2.0.3.whl`` and pip
    threw 'Invalid wheel filename (wrong number of parts)'. The local copy must
    keep the asset's own name.
    """
    seen = {}
    monkeypatch.setattr(
        updater,
        "_download",
        lambda url, dest, label="": seen.__setitem__("name", dest.name),
    )
    updater.upgrade()
    assert seen["name"] == "logitech_layout_auto_switcher-2.0.4-py3-none-any.whl"


def test_basename_decodes_a_percent_encoded_filename():
    assert updater._basename("https://x/a%20b-1.0-py3-none-any.whl") == "a b-1.0-py3-none-any.whl"
    assert updater._basename("https://x/wheel.whl") == "wheel.whl"


# -- latest_release from a scripted response ----------------------------------


def _github_response(tag: str, with_wheel: bool = True) -> bytes:
    import json

    assets = []
    if with_wheel:
        assets.append(
            {
                "name": f"logitech_layout_auto_switcher-{tag.lstrip('v')}-py3-none-any.whl",
                "browser_download_url": f"https://github.com/x/{tag}.whl",
            }
        )
    return json.dumps({"tag_name": tag, "assets": assets}).encode()


def test_latest_release_parses_tag_and_wheel(monkeypatch):
    monkeypatch.setattr(updater, "_http_get", lambda url: _github_response("v2.0.4"))
    release = updater.latest_release()
    assert release.version == "2.0.4"
    assert release.tag == "v2.0.4"
    assert release.wheel_url.endswith("2.0.4.whl")


def test_latest_release_rejects_a_release_with_no_wheel(monkeypatch):
    monkeypatch.setattr(
        updater, "_http_get", lambda url: _github_response("v2.0.4", with_wheel=False)
    )
    with pytest.raises(updater.UpdateError, match="no wheel asset"):
        updater.latest_release()


def test_latest_release_raises_on_a_network_failure(monkeypatch):
    def boom(_url):
        raise OSError("DNS is on fire")

    monkeypatch.setattr(updater, "_http_get", boom)
    with pytest.raises(updater.UpdateError, match="could not reach"):
        updater.latest_release()


def test_latest_release_raises_on_a_malformed_body(monkeypatch):
    monkeypatch.setattr(updater, "_http_get", lambda url: b"not json at all")
    with pytest.raises(updater.UpdateError, match="unexpected response"):
        updater.latest_release()


# -- the full upgrade flow, scripted end to end -------------------------------


@pytest.fixture
def scripted_upgrade(monkeypatch, tmp_path):
    """Replace every side effect of upgrade() with a recordable fake.

    Everything records into one shared timeline so the order of stop/install/start
    is comparable -- cross-list index comparisons are meaningless, and ordering is
    the whole point of the upgrade sequence.
    """
    timeline: list[str] = []
    state = {"installed_version": "2.0.3", "release_version": "2.0.4", "installed": True}

    monkeypatch.setattr(updater, "installed_version", lambda: state["installed_version"])
    monkeypatch.setattr(updater, "_read_installed_version", lambda: state["release_version"])

    monkeypatch.setattr(
        updater,
        "latest_release",
        lambda: Release(
            version=state["release_version"],
            wheel_url=(
                "https://github.com/x/releases/download/"
                f"v{state['release_version']}/logitech_layout_auto_switcher-"
                f"{state['release_version']}-py3-none-any.whl"
            ),
        ),
    )
    monkeypatch.setattr(
        updater, "_download", lambda url, dest, label="": timeline.append("download")
    )
    monkeypatch.setattr(updater, "_pip_install", lambda wheel: timeline.append("install"))

    monkeypatch.setattr(service, "status", lambda: {"installed": state["installed"]})
    monkeypatch.setattr(service, "stop", lambda: timeline.append("stop") or True)
    monkeypatch.setattr(service, "start", lambda: timeline.append("start") or True)
    return timeline, state


def test_upgrade_stops_installs_and_restarts_in_order(scripted_upgrade):
    timeline, _state = scripted_upgrade

    updater.upgrade()

    assert timeline == ["stop", "download", "install", "start"]


def test_upgrade_is_a_noop_when_already_current(scripted_upgrade):
    timeline, state = scripted_upgrade
    state["release_version"] = "2.0.3"  # same as installed

    version = updater.upgrade()

    assert version == "2.0.3"
    assert timeline == [], "nothing should have happened"


def test_force_reinstalls_even_when_current(scripted_upgrade):
    timeline, state = scripted_upgrade
    state["release_version"] = "2.0.3"

    updater.upgrade(force=True)

    assert timeline == ["stop", "download", "install", "start"]


def test_a_failed_install_still_ran_the_stop(scripted_upgrade):
    timeline, _state = scripted_upgrade

    def fail(_wheel):
        timeline.append("install")
        raise updater.UpdateError("pip said no")

    import logiswitch.updater as u

    u._pip_install = fail  # noqa: SLF001 -- the point is to make it fail
    with pytest.raises(updater.UpdateError):
        updater.upgrade()
    assert timeline == ["stop", "download", "install"], (
        "stop ran before the failed install; start did not"
    )


def test_upgrade_never_touches_the_service_when_uninstalled(scripted_upgrade):
    timeline, state = scripted_upgrade
    state["installed"] = False

    updater.upgrade()

    assert timeline == ["download", "install"], "no service means no stop or start"


def test_check_does_not_touch_the_service(scripted_upgrade, monkeypatch):
    service.start = lambda: (_ for _ in ()).throw(AssertionError("check must not mutate"))  # type: ignore[assignment]
    service.stop = lambda: (_ for _ in ()).throw(AssertionError("check must not mutate"))  # type: ignore[assignment]

    available, release = updater.check()

    assert available is True
    assert release is not None and release.version == "2.0.4"


def test_check_returns_false_when_the_network_is_down(scripted_upgrade, monkeypatch):
    def boom():
        raise updater.UpdateError("offline")

    monkeypatch.setattr(updater, "latest_release", boom)
    available, release = updater.check()
    assert available is False and release is None


def test_download_writes_the_bytes_to_disk(monkeypatch, tmp_path):
    chunks = [b"abc", b"defg"]

    class FakeResponse:
        headers = {"Content-Length": str(sum(len(c) for c in chunks))}

        def read(self, n):
            return chunks.pop(0) if chunks else b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=30: FakeResponse())
    dest = tmp_path / "out.whl"
    updater._download("https://x/p.whl", dest)
    assert dest.read_bytes() == b"abcdefg"


# -- environment detection ----------------------------------------------------


def test_a_real_venv_is_detected_as_managed():
    # The test runner is inside a venv (the project's dev environment).
    assert updater.is_managed_environment() is True


def test_is_managed_environment_flags_a_system_install(monkeypatch):
    same = Path("/usr").resolve()
    monkeypatch.setattr(updater.sys, "prefix", str(same))
    monkeypatch.setattr(updater.sys, "base_prefix", str(same))

    assert updater.is_managed_environment() is False
