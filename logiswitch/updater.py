"""Self-update: pull the latest release wheel and install it into this venv.

The command is invoked manually and is a *separate process* from the background
``watch`` agent, so it can stop that agent, replace the package, and restart it
without ever replacing itself mid-flight.

Network access uses only the standard library so this adds no dependency. The
wheel is fetched from the project's GitHub release -- the same artifact the
release workflow already publishes -- so no PyPI account or token is needed and
there is a single source of truth for "latest".

Order matters on Windows: a running process holds its package directory's files,
so the service is stopped *before* pip writes the new wheel and started again
afterwards. If the install fails the old version is still on disk and the restart
brings it back, so a botched update never leaves the machine without an agent.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__, service

log = logging.getLogger(__name__)

REPO = "App-Builders-Gang/logitech-layout-auto-switcher"
#: Normalised package name, as PEP 503 spells it in wheel filenames.
PACKAGE = "logitech_layout_auto_switcher"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
#: A browser user-agent; GitHub rejects requests with the urllib default on some
#: networks. Matches what curl sends, roughly.
USER_AGENT = "logiswitch-selfupdate"
TIMEOUT = 30


class UpdateError(RuntimeError):
    """A network or install failure that leaves the installed build unchanged."""


@dataclass(frozen=True)
class Release:
    version: str
    wheel_url: str

    @property
    def tag(self) -> str:
        return f"v{self.version}"


def _parse_version(text: str) -> tuple[int, ...]:
    """Turn a dotted version into a comparable tuple, ignoring pre-release suffixes.

    ``packaging`` is not a guaranteed dependency (pip pulls it in transitively, but
    a fresh interpreter need not have it), so this is a small parser good enough
    for the project's own ``major.minor.micro`` scheme.
    """
    core = re.split(r"[^0-9]+", text.strip().lstrip("v"))[0:3]
    parts = [int(p) for p in core if p.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def installed_version() -> str:
    return __version__


def _basename(url: str) -> str:
    """The final path segment of a URL, percent-decoded.

    GitHub release asset URLs are not encoded, but decoding keeps this correct if
    a filename ever contains a space or other reserved character.
    """
    return urllib.parse.unquote(url.rsplit("/", 1)[-1])


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        return response.read()


def latest_release() -> Release:
    """The newest published release.

    Raises :class:`UpdateError` on any network or parse failure. Unauthenticated
    GitHub API calls are rate-limited to 60/hour per IP, which is far more than a
    self-update check will ever need.
    """
    try:
        body = _http_get(API_LATEST)
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"could not reach {API_LATEST}: {exc}") from exc
    try:
        data = json.loads(body)
        tag = data["tag_name"]
    except (ValueError, KeyError) as exc:
        raise UpdateError(f"unexpected response from the releases API: {exc}") from exc

    version = tag.lstrip("v")
    wheel_url = _find_wheel(data.get("assets", []), version)
    if wheel_url is None:
        raise UpdateError(
            f"release {tag} has no wheel asset; only pre-releases or source-only "
            "releases would do that, and they are not self-installable"
        )
    return Release(version=version, wheel_url=wheel_url)


def _find_wheel(assets: list[dict], version: str) -> str | None:
    """Pick the wheel asset for this version, regardless of package-name spelling.

    The normalised name in the filename is ``logitech_layout_auto_switcher`` today,
    but matching on the suffix keeps this correct if the project is ever renamed.
    """
    suffix = f"-{version}-py3-none-any.whl"
    candidates = [
        asset["browser_download_url"]
        for asset in assets
        if isinstance(asset, dict)
        and str(asset.get("name", "")).endswith(suffix)
        and asset.get("browser_download_url")
    ]
    return candidates[0] if candidates else None


def is_update_available(release: Release | None = None) -> bool:
    release = release if release is not None else latest_release()
    return _parse_version(release.version) > _parse_version(installed_version())


def _download(url: str, dest: Path, label: str = "") -> None:
    log.debug("downloading %s", url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        total = int(response.headers.get("Content-Length") or 0)
        with dest.open("wb") as handle:
            copied = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                copied += len(chunk)
                if total and label:
                    pct = copied * 100 // total
                    print(f"\r  {label} … {pct:3d}%", end="", flush=True)
        if label:
            print()


def _venv_python() -> str:
    """The interpreter pip should install into -- the one running this command.

    ``logiswitch update`` always runs from the installed venv (the entry point
    lives there), so ``sys.executable`` is exactly the right target.
    """
    return sys.executable


def _pip_install(wheel: Path) -> None:
    """Install the wheel into the running interpreter's environment.

    ``--no-index`` plus the file path means pip never consults PyPI: the wheel we
    just downloaded is the only candidate, so there is no chance of a name clash
    pulling a different distribution. ``--disable-pip-version-check`` keeps the
    output clean; ``--no-input`` refuses to prompt if something is unexpected.
    """
    cmd = [
        _venv_python(),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-index",
        "--disable-pip-version-check",
        "--no-input",
        str(wheel),
    ]
    log.debug("running %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise UpdateError("pip install failed:\n" + (result.stderr or result.stdout).strip())


def upgrade(restart_service: bool = True, force: bool = False) -> str:
    """Bring this installation up to the latest release. Returns the new version.

    Returns the *current* version unchanged when already up to date (unless
    ``force``), so calling this on a schedule is harmless. This function logs but
    does not print user-facing messages -- the CLI owns stdout -- so it can be
    driven from anywhere without spurious output.
    """
    release = latest_release()
    if not force and _parse_version(release.version) <= _parse_version(installed_version()):
        log.info("already on %s (latest is %s)", installed_version(), release.version)
        return installed_version()

    had_service = restart_service and bool(service.status().get("installed", False))
    if had_service:
        # Stop the background agent before we touch its files; on Windows a
        # running process locks the package directory. Restart happens after the
        # install, even on failure, so the machine is never left without an agent.
        log.info("stopping the running agent before the upgrade")
        service.stop()

    tmpdir = Path(tempfile.mkdtemp(prefix="logiswitch-update-"))
    # pip validates a wheel by its filename, so the local copy must keep the
    # asset's real name (``…-py3-none-any.whl``), not a name we invented.
    wheel = tmpdir / _basename(release.wheel_url)
    try:
        _download(release.wheel_url, wheel, label=f"downloading {release.version}")
        _pip_install(wheel)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if had_service:
        log.info("restarting the agent on the new build")
        service.start()

    new_version = _read_installed_version()
    log.info("updated to %s", new_version)
    return new_version


def _read_installed_version() -> str:
    """Re-import the version after the upgrade rather than trusting the old one.

    The module-level ``__version__`` was read at import time, before the wheel was
    replaced, so it still reports the previous build. Re-reading the file is cheap
    and avoids a subprocess. Falls back to the in-memory value if the file moved.
    """
    try:
        init = Path(__file__).with_name("__init__.py")
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)', init.read_text("utf-8"))
        if match:
            return match.group(1)
    except OSError:
        pass
    return __version__


def check() -> tuple[bool, Release | None]:
    """Non-mutating: is an update available, and what is it?"""
    try:
        release = latest_release()
    except UpdateError as exc:
        log.warning("%s", exc)
        return False, None
    return is_update_available(release), release


def is_managed_environment() -> bool:
    """True when running inside the venv the installer created.

    A development checkout (``pip install -e .``) should not be self-updated this
    way -- it would clobber the editable install with a wheel -- so the command
    warns and exits unless it sees the tell-tale site-packages layout.
    """
    prefix = Path(sys.prefix).resolve()
    base = Path(sys.base_prefix).resolve()
    return prefix != base
