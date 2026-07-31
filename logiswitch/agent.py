"""The supervisor: keep every attached Logitech device matched to this host's OS.

Fully event-driven. There is no poll loop -- work happens only when the OS says a
Logitech HID interface appeared or went away, when a receiver reports that a
device woke up, or when the (long, optional) safety heartbeat fires.

Threads, all of which sit in kernel waits when idle:
  * the watcher's own thread (cfgmgr32 uses OS thread-pool callbacks instead)
  * one reader thread per open HID handle, blocked in ``hid_read``
  * one worker thread, blocked in ``queue.get``
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import hidpp
from .hidpp import protocol as p
from .watchers import DeviceEvent, Watcher, create_watcher

log = logging.getLogger(__name__)


class _Event(Enum):
    DEVICE_CHANGED = "device_changed"
    DEVICE_WOKE = "device_woke"
    STOP = "stop"


@dataclass
class AgentConfig:
    target_os: str
    #: Coalesce the burst of interface events a single KVM switch produces.
    debounce: float = 0.6
    #: Safety re-check, and the only thing that catches a device coming back on
    #: hardware that announces nothing. A Bolt receiver stays enumerated across an
    #: Easy-Switch move, so the OS reports no change and the receiver forwards no
    #: HID++ 1.0 connect notification: with a live session open, the agent has no
    #: other reason to talk to the device and would not notice it ever left. Cheap
    #: -- one read per device, features are cached -- so this can be frequent.
    #: 0 disables it, which limits the agent to what the OS and device announce.
    reassert_interval: float = 20.0
    retry_initial: float = 2.0
    #: Ceiling for the retry backoff while a device is away. This is the worst case
    #: for noticing it came back when it announces nothing, so it is deliberately
    #: short: a failed attempt only costs the receiver a timed-out request.
    retry_max: float = 10.0
    vendor_id: int = p.LOGITECH_VID
    force_polling: bool = False
    state_file: Path | None = None


@dataclass
class Session:
    """One open transport and the devices found behind it."""

    group: hidpp.InterfaceGroup
    transport: hidpp.Transport
    devices: list[tuple[hidpp.HidppDevice, hidpp.DeviceInfo]] = field(default_factory=list)

    @property
    def supported(self) -> list[tuple[hidpp.HidppDevice, hidpp.DeviceInfo]]:
        return [(d, i) for d, i in self.devices if i.supported]

    def close(self) -> None:
        self.transport.close()


class Agent:
    def __init__(self, config: AgentConfig):
        self.cfg = config
        self._queue: queue.Queue = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._watcher: Watcher | None = None
        self._sessions: list[Session] = []
        #: Device indices we drive. Read from reader threads, rebound (never
        #: mutated) from the worker, so no lock is needed.
        self._driven: frozenset[int] = frozenset()
        self._hints: dict[str, int] = self._load_hints()
        self._retry = 0.0
        self._changes_in_a_row = 0
        self._contention_warned = False
        self._last_summary: str | None = None
        #: When the devices stopped answering, so the log can say how long a
        #: KVM/Easy-Switch round trip actually took to recover.
        self._absent_since: float | None = None

    # -- public API -----------------------------------------------------------

    def start(self) -> None:
        log.info(
            "logiswitch agent starting: target=%s reassert=%s",
            self.cfg.target_os,
            f"{self.cfg.reassert_interval:.0f}s" if self.cfg.reassert_interval else "off",
        )
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="logiswitch-worker", daemon=True)
        self._worker.start()
        self._watcher = create_watcher(self.cfg.vendor_id, self.cfg.force_polling)
        try:
            self._watcher.start(self._on_device_event)
            log.info("watching for device changes via %s", self._watcher.name)
        except Exception as exc:
            log.warning("%s watcher failed to start (%s); falling back to polling",
                        self._watcher.name, exc)
            from .watchers.polling import PollingWatcher

            self._watcher = PollingWatcher(self.cfg.vendor_id)
            self._watcher.start(self._on_device_event)

    def stop(self) -> None:
        """Idempotent, and safe to call from a signal handler."""
        if self._stop.is_set():
            return
        self._stop.set()
        with contextlib.suppress(queue.Full):  # pragma: no cover
            self._queue.put_nowait((_Event.STOP, None))

    def wait(self) -> None:
        """Block the caller until :meth:`stop` is called. Interruptible by signals."""
        while not self._stop.wait(0.5):
            pass

    def shutdown(self) -> None:
        """Release everything. Called once, after :meth:`wait` returns."""
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                log.debug("watcher stop raised", exc_info=True)
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(5.0)
            if worker.is_alive():  # pragma: no cover
                log.warning("worker thread did not exit cleanly")
        self._teardown_sessions("shutdown")
        log.info("logiswitch agent stopped")

    def run_forever(self) -> None:
        self.start()
        try:
            self.wait()
        finally:
            self.shutdown()

    def assert_once(self) -> bool:
        """Build a session, apply the target OS, tear down. Used by ``--once``."""
        try:
            return self._apply()
        finally:
            self._teardown_sessions("one-shot done")

    # -- event intake ---------------------------------------------------------

    def _on_device_event(self, event: DeviceEvent, description: str) -> None:
        # Runs on an OS/watcher thread: enqueue and return, nothing more.
        log.debug("device %s: %s", event.value, description)
        self._put((_Event.DEVICE_CHANGED, event))

    def _on_hidpp_frame(self, frame: bytes) -> None:
        # Runs on a reader thread.
        if len(frame) < 4:
            return
        index = frame[1]
        if frame[2] == p.NOTIF_DEVICE_CONNECTION:
            # HID++ 1.0: the receiver itself announces a device connecting.
            self._put((_Event.DEVICE_WOKE, index))
            return
        if p.is_unsolicited(frame) and (not self._driven or index in self._driven):
            # A Bolt receiver stays enumerated across an Easy-Switch move and
            # forwards no HID++ 1.0 connect notification, so the only sign that the
            # keyboard came back is that it starts talking again. Which feature
            # speaks first is device-specific -- an MX Keys S sends 0x4220 lock-key
            # state, others send 0x1D4B or 0x0020 -- so trust the sender, not the
            # message. Once we have devices, chatter from ones we do not drive is
            # ignored (a mouse sprays movement events). With no devices we accept
            # anything: we are mid-retry precisely because the keyboard was away,
            # and that is the moment its return matters most.
            self._put((_Event.DEVICE_WOKE, index))

    def _put(self, item: tuple) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # A full queue already means "something changed"; dropping duplicates
            # is harmless because the worker re-reads real state anyway.
            log.debug("event queue full, dropping %s", item[0])

    # -- worker ---------------------------------------------------------------

    def _run(self) -> None:
        next_assert: float | None = time.monotonic()  # assert once at start-up
        next_heartbeat: float | None = None

        while not self._stop.is_set():
            now = time.monotonic()
            deadlines = [d for d in (next_assert, next_heartbeat) if d is not None]
            timeout = max(0.0, min(deadlines) - now) if deadlines else None

            try:
                kind, payload = self._queue.get(timeout=timeout)
            except queue.Empty:
                kind, payload = None, None

            if kind is _Event.STOP:
                break
            if kind is _Event.DEVICE_CHANGED:
                # The interface set changed; every open handle is suspect.
                self._teardown_sessions(f"device {getattr(payload, 'value', payload)}")
                next_assert = time.monotonic() + self.cfg.debounce
                self._retry = 0.0
                continue
            if kind is _Event.DEVICE_WOKE:
                log.debug("device %s spoke unprompted -- treating it as a reconnect", payload)
                # A device announces itself before it will answer requests: a scan
                # one second after the frame still finds nothing. Restart the
                # backoff so the retries that follow are 0.2s, 2s, 4s, 8s instead
                # of inheriting the 30s ceiling reached while it was away -- that
                # inheritance is what made a return take half a minute to correct.
                self._retry = 0.0
                next_assert = min(
                    next_assert or float("inf"), time.monotonic() + 0.2
                )
                continue

            now = time.monotonic()
            if next_assert is not None and now >= next_assert:
                next_assert = None
                try:
                    ok = self._apply()
                except Exception:
                    log.exception("unexpected failure while applying the platform")
                    ok = False
                self._note_presence(ok)
                if ok:
                    self._retry = 0.0
                    if self._changes_in_a_row:
                        # Something just changed -- re-check soon so a revert by
                        # other software is spotted quickly rather than in 10 min.
                        next_assert = time.monotonic() + 3.0
                    next_heartbeat = (
                        time.monotonic() + self.cfg.reassert_interval
                        if self.cfg.reassert_interval
                        else None
                    )
                else:
                    self._retry = min(
                        self.cfg.retry_max, max(self.cfg.retry_initial, self._retry * 2)
                    )
                    next_assert = time.monotonic() + self._retry
                    log.debug("retrying in %.1fs", self._retry)
                continue

            if next_heartbeat is not None and now >= next_heartbeat:
                next_heartbeat = None
                log.debug("heartbeat: re-checking the platform")
                next_assert = now

        self._teardown_sessions("worker exiting")

    def _note_presence(self, reachable: bool) -> None:
        """Log the gap while nothing answered.

        Without this the log shows a switch happening but never says how long the
        layout was wrong, which is the one number that matters when a KVM round
        trip feels slow.
        """
        if reachable:
            if self._absent_since is not None:
                log.info(
                    "device(s) answering again after %.1fs away",
                    time.monotonic() - self._absent_since,
                )
                self._absent_since = None
        elif self._absent_since is None:
            self._absent_since = time.monotonic()
            log.info("nothing is answering; waiting for a device to come back")

    # -- the actual work ------------------------------------------------------

    def _apply(self) -> bool:
        if not self._sessions:
            self._build_sessions()
        if not self._sessions:
            log.debug("no Logitech HID++ endpoint present")
            self._last_summary = None
            return False

        applied = 0
        changed = 0
        failed = 0
        for session in list(self._sessions):
            for device, info in session.supported:
                try:
                    did_change, option = device.ensure_os(self.cfg.target_os)
                except p.UnsupportedFeature as exc:
                    log.debug("%s: %s", info.name, exc)
                    continue
                except (p.HidppError, p.HidppTimeout) as exc:
                    # Almost always "asleep" or "on another Easy-Switch channel".
                    log.debug("%s not reachable: %s", info.name, exc)
                    failed += 1
                    continue
                except (p.TransportClosed, OSError) as exc:
                    log.info("transport lost while applying: %s", exc)
                    self._teardown_sessions("transport lost")
                    return False
                applied += 1
                if did_change:
                    changed += 1
                    log.info(
                        "switched %s to %s (platform %d)",
                        info.name,
                        option.label,
                        option.index,
                    )
                else:
                    summary = f"{info.name}={option.label}"
                    log.debug("%s reads %s, nothing to do", info.name, option.label)
                    if summary != self._last_summary:
                        log.info("%s already on %s", info.name, option.label)
                        self._last_summary = summary

        if changed:
            self._changes_in_a_row += 1
            # Re-enumeration follows a platform change; drop stale handles.
            self._teardown_sessions("platform changed")
            if self._changes_in_a_row >= 3 and not self._contention_warned:
                self._contention_warned = True
                log.warning(
                    "the platform keeps reverting -- another process is fighting us. "
                    "Logi Options+ enforces its own host OS on this collection; quit or "
                    "uninstall it if the layout will not stay on %s.",
                    self.cfg.target_os,
                )
        elif applied:
            self._changes_in_a_row = 0
            self._contention_warned = False

        return applied > 0 and failed == 0

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
            for device, info in session.supported:
                self._hints[group.label] = device.index
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

    def _load_hints(self) -> dict[str, int]:
        path = self.cfg.state_file
        if not path or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
            return {str(k): int(v) for k, v in data.get("hints", {}).items()}
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
