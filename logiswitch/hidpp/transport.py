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
import logging
import threading
import time
from collections.abc import Iterable, Sequence
from typing import Callable

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
            self.error = p.error_from(
                frame, proto, f" on feature 0x{self.feature_index:02X}"
            )
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

    __slots__ = ("wanted", "frames", "event")

    def __init__(self, wanted: Iterable[int]):
        self.wanted = set(wanted)
        self.frames: dict[int, bytes] = {}
        self.event = threading.Event()

    def accept(self, frame: bytes) -> bool:
        index = frame[1]
        if index not in self.wanted:
            return False
        # Either a ping reply or an error for the ping counts as "slot answered".
        ping_reply = frame[2] == p.FEATURE_ROOT and frame[3] == p.function_byte(
            p.ROOT_GET_PROTOCOL_VERSION
        )
        is_error = frame[2] in (p.ERROR_HIDPP20, p.ERROR_HIDPP10)
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
                target=self._read_loop, args=(handle,), name=f"hidpp-reader-{self.label}", daemon=True
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

    def _dispatch(self, frame: bytes) -> None:
        with self._sink_lock:
            sink = self._sink
        if sink is not None and sink.accept(frame):
            return
        callback = self.on_notification
        if callback is not None:
            try:
                callback(frame)
            except Exception:
                log.exception("notification callback raised")

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
        frame = p.build_frame(
            device_index, feature_index, function, params, long_report=long_report
        )
        sink = _ResponseSink(device_index, feature_index, p.function_byte(function))
        with self._request_lock:
            with self._sink_lock:
                self._sink = sink
            try:
                self._handle_for(long_report).write(frame)
                if not sink.event.wait(timeout):
                    raise p.HidppTimeout(
                        f"no response from device {device_index} "
                        f"feature 0x{feature_index:02X} fn {function}"
                    )
            except OSError as exc:
                self._dead = True
                raise p.TransportClosed(str(exc)) from exc
            finally:
                with self._sink_lock:
                    self._sink = None
        if sink.error is not None:
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
        sink = _ScanSink(wanted)
        ping = bytes([0x00, 0x00, 0xAA])
        with self._request_lock:
            with self._sink_lock:
                self._sink = sink
            try:
                handle = self._handle_for(False)
                for index in wanted:
                    frame = p.build_frame(
                        index, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, ping, long_report=False
                    )
                    try:
                        handle.write(frame)
                    except OSError as exc:
                        self._dead = True
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
