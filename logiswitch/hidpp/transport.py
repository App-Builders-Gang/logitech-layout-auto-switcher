"""HID++ transport: owns the open handles, one reader thread per handle, and the
dispatch that routes each inbound frame either to a waiting request or to the
notification callback.

The reader threads are the *only* readers. That removes the v1 race where a
pre-request drain and a notification poll fought over the same queue and silently
ate device-wake events.

Idle cost is one blocking ``hid_read`` per handle. The 1 s timeout exists purely
so shutdown is bounded; the wait itself is a kernel wait and burns no CPU.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import threading
import time
from collections.abc import Iterable, Sequence
from typing import Callable

from .. import trace
from . import backend
from . import protocol as p

log = logging.getLogger(__name__)

#: How long a reader thread blocks per read. This is the only knob that trades
#: shutdown latency against idle wake-ups, and both are tiny: 500 ms means the
#: agent exits in about half a second and each handle wakes twice a second to do
#: nothing, which does not register as CPU use.
READ_TIMEOUT_MS = 500
#: Default deadline for a single request/response exchange.
DEFAULT_TIMEOUT = 1.2
#: A sleeping device answers late; discovery gives every slot this long.
SCAN_WINDOW = 1.4
#: Gap between fan-out pings so the receiver does not reply "busy" to all of them.
SCAN_STAGGER = 0.02
#: Seconds between orphan-frame warnings. The first one always logs; the rest are
#: throttled so a receiver in a bad mood cannot flood the log it is evidence in.
ORPHAN_WARN_INTERVAL = 30.0
#: Escape hatch. HID++ 2.0 requires a device to echo the software id and rotating
#: it is what stops a stale reply being mistaken for a fresh one, but firmware that
#: got that wrong would answer nothing at all -- so leave a way back.
FIXED_SWID_ENV = "LOGISWITCH_FIXED_SWID"
#: How long a request that gave up is remembered, so its answer can still be
#: recognised as belonging to it when it eventually turns up.
ABANDONED_MEMORY = 3.0


class _ResponseSink:
    """Waits for the reply to exactly one in-flight request."""

    __slots__ = ("device_index", "feature_index", "func_byte", "event", "frame", "error", "closed")

    def __init__(self, device_index: int, feature_index: int, func_byte: int):
        self.device_index = device_index
        self.feature_index = feature_index
        self.func_byte = func_byte
        self.event = threading.Event()
        self.frame: bytes | None = None
        self.error: Exception | None = None
        self.closed = False

    def accept(self, frame: bytes) -> bool:
        proto = p.is_error_for(frame, self.device_index, self.feature_index, self.func_byte)
        if proto is not None:
            self.error = p.error_from(frame, proto, f" on feature 0x{self.feature_index:02X}")
            self.event.set()
            return True
        if p.is_response_to(frame, self.device_index, self.feature_index, self.func_byte):
            self.frame = frame
            self.event.set()
            return True
        return False

    def fail(self, exc: Exception) -> None:
        self.error = exc
        self.closed = True
        self.event.set()


class _ScanSink:
    """Collects replies to a fan-out ping across several device indices."""

    __slots__ = ("wanted", "func_byte", "frames", "event")

    def __init__(self, wanted: Iterable[int], func_byte: int):
        self.wanted = set(wanted)
        self.func_byte = func_byte
        self.frames: dict[int, bytes] = {}
        self.event = threading.Event()

    def accept(self, frame: bytes) -> bool:
        index = frame[1]
        if index not in self.wanted:
            return False
        # Either a ping reply or an error for the ping counts as "slot answered".
        # Both are matched on the software id this scan stamped, so a straggler
        # from the previous scan cannot be counted as this one's answer.
        ping_reply = frame[2] == p.FEATURE_ROOT and frame[3] == self.func_byte
        is_error = frame[2] in (p.ERROR_HIDPP20, p.ERROR_HIDPP10) and (
            len(frame) >= 5 and frame[4] == self.func_byte
        )
        if not (ping_reply or is_error):
            return False
        self.frames.setdefault(index, frame)
        if len(self.frames) >= len(self.wanted):
            self.event.set()
        return True

    def fail(self, exc: Exception) -> None:  # noqa: ARG002 - scan tolerates loss
        self.event.set()


class Transport:
    """Open handles onto one receiver's (or one direct device's) HID++ collections."""

    def __init__(self, paths: Sequence[tuple[int, bytes]], label: str = "HID++"):
        """`paths` is a sequence of ``(usage, path)``; usage is USAGE_SHORT/USAGE_LONG."""
        if not paths:
            raise ValueError("a transport needs at least one HID++ collection")
        self._paths = list(paths)
        self.label = label
        self._handles: list[tuple[int, backend.HidHandle]] = []
        self._readers: list[threading.Thread] = []
        self._stop = threading.Event()
        self._sink_lock = threading.Lock()
        self._sink: _ResponseSink | _ScanSink | None = None
        self._request_lock = threading.RLock()
        self._dead = False
        self.on_notification: Callable[[bytes], None] | None = None
        self._sw_ids = itertools.cycle(p.SW_IDS)
        self._fixed_sw_id = os.environ.get(FIXED_SWID_ENV) == "1"
        self._last_orphan_warning = 0.0
        #: Requests that timed out, keyed exactly as their reply would arrive, with
        #: an expiry. See :meth:`_abandon`.
        self._abandoned: dict[tuple[int, int, int], float] = {}

    def _next_sw_id(self) -> int:
        """Stamp the next request. Rotating is what makes a late reply detectable."""
        if self._fixed_sw_id:
            return p.SW_ID
        # itertools.cycle.__next__ is a single bytecode under the GIL, and requests
        # are serialised by _request_lock anyway, so no extra lock is warranted.
        return next(self._sw_ids)

    # -- lifecycle ------------------------------------------------------------

    def open(self) -> Transport:
        opened: list[tuple[int, backend.HidHandle]] = []
        seen: dict[bytes, backend.HidHandle] = {}
        try:
            for usage, path in self._paths:
                # macOS/Linux can expose one interface covering both report ids.
                handle = seen.get(path)
                if handle is None:
                    handle = backend.open_path(path)
                    seen[path] = handle
                opened.append((usage, handle))
        except Exception:
            # Partial-open must not leak the handles already acquired.
            for handle in seen.values():
                with contextlib.suppress(Exception):
                    handle.close()
            raise
        self._handles = opened
        self._stop.clear()
        self._dead = False
        for handle in seen.values():
            thread = threading.Thread(
                target=self._read_loop,
                args=(handle,),
                name=f"hidpp-reader-{self.label}",
                daemon=True,
            )
            thread.start()
            self._readers.append(thread)
        return self

    def close(self) -> None:
        """Stop readers, then close handles.

        Order matters: closing a handle while a reader is blocked inside hidapi
        can fault, so the readers are joined first. They exit within one read
        timeout.
        """
        self._stop.set()
        self._fail_pending(p.TransportClosed(f"{self.label} closed"))
        deadline = time.monotonic() + (READ_TIMEOUT_MS / 1000.0) + 0.5
        for thread in self._readers:
            thread.join(max(0.0, deadline - time.monotonic()))
        stuck = [t.name for t in self._readers if t.is_alive()]
        self._readers.clear()
        if stuck:
            # Leave the handles open rather than fault a live reader thread.
            log.warning("reader thread(s) %s did not stop; leaving handles to the OS", stuck)
            self._handles.clear()
            return
        closed: set[int] = set()
        for _usage, handle in self._handles:
            if id(handle) in closed:
                continue
            closed.add(id(handle))
            try:
                handle.close()
            except Exception as exc:
                log.debug("error closing %s: %s", self.label, exc)
        self._handles.clear()

    def __enter__(self) -> Transport:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def alive(self) -> bool:
        return bool(self._handles) and not self._dead

    # -- reader ---------------------------------------------------------------

    def _read_loop(self, handle: backend.HidHandle) -> None:
        while not self._stop.is_set():
            try:
                data = handle.read(p.LEN_LONG, READ_TIMEOUT_MS)
            except Exception as exc:
                if not self._stop.is_set():
                    log.info("%s read failed, transport is gone: %s", self.label, exc)
                    self._dead = True
                    self._fail_pending(p.TransportClosed(str(exc)))
                return
            if not data or not p.is_hidpp_frame(data):
                continue
            self._dispatch(data)

    @staticmethod
    def _reply_key(frame: bytes) -> tuple[int, int, int] | None:
        """The (device, feature, funcByte) a frame answers, or None if it answers nothing.

        An error frame carries the feature and function one byte further along than a
        normal reply does, so the two layouts are read separately.
        """
        if len(frame) < 4:
            return None
        if frame[2] in (p.ERROR_HIDPP20, p.ERROR_HIDPP10):
            return (frame[1], frame[3], frame[4]) if len(frame) >= 5 else None
        return (frame[1], frame[2], frame[3])

    def _abandon(self, device_index: int, feature_index: int, func_byte: int) -> None:
        """Remember a request that gave up waiting.

        Its answer may still be in flight. Because the software id rotates, that
        answer carries a key no later request will reuse for a long time, so when it
        finally lands it can be attributed to the request that gave up rather than
        handed to whoever happens to be waiting. Rotation makes the straggler
        *distinguishable*; this makes it *rejected*.
        """
        now = time.monotonic()
        self._abandoned = {key: due for key, due in self._abandoned.items() if due > now}
        self._abandoned[(device_index, feature_index, func_byte)] = now + ABANDONED_MEMORY

    def _is_abandoned(self, frame: bytes) -> bool:
        key = self._reply_key(frame)
        if key is None:
            return False
        due = self._abandoned.get(key)
        if due is None:
            return False
        if due <= time.monotonic():
            self._abandoned.pop(key, None)
            return False
        self._abandoned.pop(key, None)  # it has arrived; stop watching for it
        return True

    def _dispatch(self, frame: bytes) -> None:
        summary = p.describe_frame(frame)
        if self._is_abandoned(frame):
            # The answer to a request that already gave up. Never offer it to the
            # sink that happens to be installed now -- that is the whole bug.
            self._note_orphan(frame, f"late answer to an abandoned request: {summary}")
            return
        with self._sink_lock:
            sink = self._sink
        if sink is not None and sink.accept(frame):
            trace.HEALTH.bump("replies")
            trace.record(trace.IN, self.label, frame, summary)
            return

        if p.is_unsolicited(frame) or p.is_connection_notification(frame):
            trace.HEALTH.bump("notifications")
            trace.record(trace.NOTIFY, self.label, frame, summary)
        else:
            self._note_orphan(frame, summary)

        callback = self.on_notification
        if callback is not None:
            try:
                callback(frame)
            except Exception:
                log.exception("notification callback raised")

    def _note_orphan(self, frame: bytes, summary: str) -> None:
        """A reply nobody was waiting for.

        Almost always a straggler: the request it answers gave up at its deadline
        and the reply turned up afterwards. Harmless now that the software id is
        rotated -- it can no longer be handed to whichever request came next -- but
        worth counting and saying out loud, because it is the fingerprint of a
        receiver answering slowly enough for that race to have been possible, and
        it is the thing to look for in the trace next to a wrong-layout report.
        """
        total = trace.HEALTH.bump("orphans")
        trace.record(trace.ORPHAN, self.label, frame, summary)
        now = time.monotonic()
        if total == 1 or now - self._last_orphan_warning >= ORPHAN_WARN_INTERVAL:
            self._last_orphan_warning = now
            log.warning(
                "%s: reply arrived with nothing waiting for it (%d so far): %s -- "
                "the device is answering later than the %.1fs deadline",
                self.label,
                total,
                summary,
                DEFAULT_TIMEOUT,
            )
            trace.anomaly(f"orphan reply on {self.label}: {summary}")

    def _fail_pending(self, exc: Exception) -> None:
        with self._sink_lock:
            sink = self._sink
        if sink is not None:
            sink.fail(exc)

    def _handle_for(self, long_report: bool) -> backend.HidHandle:
        wanted = p.USAGE_LONG if long_report else p.USAGE_SHORT
        for usage, handle in self._handles:
            if usage == wanted:
                return handle
        return self._handles[0][1]

    # -- requests -------------------------------------------------------------

    def request(
        self,
        device_index: int,
        feature_index: int,
        function: int,
        params: bytes = b"",
        timeout: float = DEFAULT_TIMEOUT,
        long_report: bool | None = None,
    ) -> bytes:
        """Send one request, return the payload after the function byte."""
        if not self._handles:
            raise p.TransportClosed(f"{self.label} is not open")
        if self._dead:
            raise p.TransportClosed(f"{self.label} is gone")
        if long_report is None:
            long_report = len(params) > 3
        with self._request_lock:
            sw_id = self._next_sw_id()
            frame = p.build_frame(
                device_index, feature_index, function, params, sw_id=sw_id, long_report=long_report
            )
            sink = _ResponseSink(device_index, feature_index, p.function_byte(function, sw_id))
            with self._sink_lock:
                self._sink = sink
            try:
                trace.HEALTH.bump("requests")
                trace.record(trace.OUT, self.label, frame, p.describe_frame(frame))
                self._handle_for(long_report).write(frame)
                if not sink.event.wait(timeout):
                    trace.HEALTH.bump("timeouts")
                    self._abandon(device_index, feature_index, sink.func_byte)
                    raise p.HidppTimeout(
                        f"no response from device {device_index} "
                        f"feature 0x{feature_index:02X} fn {function}"
                    )
            except OSError as exc:
                self._dead = True
                trace.HEALTH.bump("transport_losses")
                raise p.TransportClosed(str(exc)) from exc
            finally:
                with self._sink_lock:
                    self._sink = None
        if sink.error is not None:
            trace.HEALTH.bump("errors")
            raise sink.error
        assert sink.frame is not None
        return sink.frame[4:]

    def scan(
        self, indices: Iterable[int], window: float = SCAN_WINDOW
    ) -> dict[int, tuple[int, int]]:
        """Fan out root pings and return ``{device_index: (major, minor)}``.

        Every slot is pinged in one window instead of serially, so discovering a
        device parked in slot 6 costs one window rather than six timeouts.
        """
        wanted = list(indices)
        if not self._handles:
            raise p.TransportClosed(f"{self.label} is not open")
        ping = bytes([0x00, 0x00, 0xAA])
        with self._request_lock:
            # One software id for the whole fan-out: every ping in this window is
            # the same question, and stamping them alike is what lets replies from
            # a *previous* window be told apart and discarded.
            sw_id = self._next_sw_id()
            sink = _ScanSink(wanted, p.function_byte(p.ROOT_GET_PROTOCOL_VERSION, sw_id))
            with self._sink_lock:
                self._sink = sink
            try:
                handle = self._handle_for(False)
                for index in wanted:
                    frame = p.build_frame(
                        index,
                        p.FEATURE_ROOT,
                        p.ROOT_GET_PROTOCOL_VERSION,
                        ping,
                        sw_id=sw_id,
                        long_report=False,
                    )
                    try:
                        trace.HEALTH.bump("requests")
                        trace.record(trace.OUT, self.label, frame, p.describe_frame(frame))
                        handle.write(frame)
                    except OSError as exc:
                        self._dead = True
                        trace.HEALTH.bump("transport_losses")
                        raise p.TransportClosed(str(exc)) from exc
                    time.sleep(SCAN_STAGGER)
                sink.event.wait(window)
            finally:
                with self._sink_lock:
                    self._sink = None

        found: dict[int, tuple[int, int]] = {}
        busy: list[int] = []
        for index, frame in sink.frames.items():
            if frame[2] in (p.ERROR_HIDPP20, p.ERROR_HIDPP10):
                # 0x08 "busy" means the slot exists but the receiver was saturated
                # by the fan-out; those get a second, serial chance below.
                if len(frame) >= 6 and frame[5] == 0x08:
                    busy.append(index)
                continue
            if len(frame) >= 7 and frame[6] == 0xAA:
                found[index] = (frame[4], frame[5])

        for index in busy + [i for i in wanted if i not in sink.frames]:
            if index in found:
                continue
            try:
                reply = self.request(
                    index, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, ping, timeout=0.6
                )
            except (p.HidppTimeout, p.HidppError):
                continue
            except p.TransportClosed:
                break
            if len(reply) >= 3 and reply[2] == 0xAA:
                found[index] = (reply[0], reply[1])
        return found
