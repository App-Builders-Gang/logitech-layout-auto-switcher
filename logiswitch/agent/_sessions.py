"""Session construction and device-index hint persistence, split out of the agent.

These methods build and tear down the live HID++ sessions behind each receiver
and persist the device indices discovered last time, so a cold start does not pay
for a full probe of every receiver. They are mixed into
:class:`logiswitch.agent.Agent` via :class:`_SessionMixin`.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .. import hidpp
from ..hidpp import protocol as p
from . import Session

log = logging.getLogger("logiswitch.agent")

if TYPE_CHECKING:
    # Type only -- never imported at runtime, so there is no import cycle.
    from . import AgentConfig


class _SessionMixin:
    # Declared here solely for mypy: every attribute and method below is defined
    # on ``Agent`` (in __init__.py), not on this mixin.
    if TYPE_CHECKING:
        cfg: AgentConfig
        _sessions: list[Session]
        _hints: dict[str, list[int]]
        _last_written: dict[int, int]
        _driven: frozenset[int]
        _platform_features: dict[int, int]

        def _on_hidpp_frame(self, frame: bytes) -> None: ...

    def _build_sessions(self) -> None:
        groups = hidpp.find_groups(self.cfg.vendor_id)
        if not groups:
            return
        for group in groups:
            try:
                transport = hidpp.open_transport(group)
            except Exception as exc:
                log.debug("cannot open %s: %s", group, exc)
                continue
            session = Session(group=group, transport=transport)
            transport.on_notification = self._on_hidpp_frame
            try:
                devices = hidpp.discover_devices(transport, hint=self._hints.get(group.label))
                session.devices = hidpp.probe_devices(devices)
            except Exception as exc:
                log.debug("discovery failed on %s: %s", group, exc)
                transport.close()
                continue
            if not session.supported:
                # Nothing here can switch platform (a mouse-only receiver, say).
                # Keep no handle open for it.
                names = ", ".join(i.name for _, i in session.devices) or "no devices"
                log.debug("%s has nothing to drive (%s)", group, names)
                transport.close()
                continue
            self._hints[group.label] = [d.index for d, _ in session.supported]
            for device, info in session.supported:
                device.claim_host(self.cfg.claim_host)
                device.remember_last_write(self._last_written.get(device.index))
                log.info(
                    "found %s on %s at index %d via %s",
                    info.name,
                    group.label,
                    device.index,
                    info.kind,
                )
            self._sessions.append(session)
        self._refresh_driven()
        self._save_hints()

    def _refresh_driven(self) -> None:
        self._driven = frozenset(
            device.index for session in self._sessions for device, _info in session.supported
        )
        features: dict[int, int] = {}
        for session in self._sessions:
            for device, info in session.supported:
                if info.feature != p.FEATURE_MULTIPLATFORM:
                    continue
                try:
                    features[device.index] = device.feature_index(p.FEATURE_MULTIPLATFORM)
                except Exception:  # cached lookup; a failure here is not worth raising
                    log.debug("no platform feature index for device %d", device.index)
        self._platform_features = features

    def _teardown_sessions(self, reason: str) -> None:
        if not self._sessions:
            return
        # _driven deliberately survives: a frame already in flight when we close
        # arrives just after, and dropping it loses a real platform-change event.
        # Device indices are stable per receiver, so a stale entry is harmless.
        log.debug("closing %d session(s): %s", len(self._sessions), reason)
        for session in self._sessions:
            try:
                session.close()
            except Exception:
                log.debug("error closing session", exc_info=True)
        self._sessions.clear()

    # -- device index hints ---------------------------------------------------

    def _load_hints(self) -> dict[str, list[int]]:
        """Device indices seen last time, per receiver.

        Tolerates the older on-disk form, a bare integer per receiver, so an upgrade
        does not throw away the fast path or crash on the state file.
        """
        path = self.cfg.state_file
        if not path or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
            hints = {}
            for label, value in data.get("hints", {}).items():
                indices = [value] if isinstance(value, int) else list(value)
                hints[str(label)] = [int(i) for i in indices]
            return hints
        except Exception:
            return {}

    def _save_hints(self) -> None:
        path = self.cfg.state_file
        if not path or not self._hints:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"hints": self._hints}, indent=2), "utf-8")
        except Exception as exc:
            log.debug("could not save hints: %s", exc)
