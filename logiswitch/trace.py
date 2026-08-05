"""A bounded, always-on record of every HID++ frame, for after-the-fact diagnosis.

The failure this exists for is silent. When the keyboard ends up in the wrong
platform mode nothing crashes and nothing is logged as wrong: the agent reads a
value, believes it, and reports success. By the time someone notices the characters
are wrong the cause is minutes in the past and was never written down.

So every frame lands in a ring that costs nothing to keep, and an anomaly -- or
``logiswitch doctor`` -- flushes it to disk with the health counters attached. A
ring rather than a log file because tracing every frame into the main log would
rotate a real diagnosis out of it within minutes.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

#: Frames kept in memory. At the ~10 frames/s a busy receiver produces this is a
#: couple of minutes of history for a few tens of KiB.
RING_SIZE = 512

#: Bytes the dumped trace file may reach before the previous one is rolled aside.
TRACE_MAX_BYTES = 1024 * 1024

# Direction markers, one character so a dumped trace stays scannable.
OUT = ">"  # request we wrote
IN = "<"  # reply that matched a waiting request
ORPHAN = "!"  # reply that matched nothing -- the interesting one
NOTIFY = "*"  # unsolicited device event
NOTE = "#"  # our own annotation, not a frame


class Record(NamedTuple):
    monotonic: float
    wall: float
    direction: str
    label: str
    hexbytes: str
    summary: str

    def render(self) -> str:
        stamp = datetime.fromtimestamp(self.wall).strftime("%H:%M:%S.%f")[:-3]
        if self.direction == NOTE:
            return f"{stamp} {self.direction} {self.summary}"
        return f"{stamp} {self.direction} {self.label:<12} {self.hexbytes:<40} {self.summary}"


class Health:
    """Counters that say whether the link is behaving, not just whether it worked.

    Every one of these is a number the log could not previously produce. ``orphans``
    in particular is the direct signature of a reply arriving for a request that had
    already timed out -- the race that lets a stale platform value be believed.
    """

    __slots__ = ("_lock", "_counts", "_stamps", "started")

    #: Timestamps kept per windowed event.
    WINDOW_MEMORY = 128

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._stamps: dict[str, deque[float]] = {}
        self.started = time.monotonic()

    def bump(self, name: str, amount: int = 1) -> int:
        with self._lock:
            total = self._counts.get(name, 0) + amount
            self._counts[name] = total
            return total

    def get(self, name: str) -> int:
        with self._lock:
            return self._counts.get(name, 0)

    def mark(self, name: str) -> None:
        """Count an event *and* remember when it happened.

        The "when" is the part that matters. A counter that only knows "three in a
        row" is defeated by anything that succeeds in between -- which is exactly
        what a correction is -- so a fault that recurs every twelve seconds forever
        can look, to a consecutive counter, like it never happened twice.
        """
        self.bump(name)
        with self._lock:
            stamps = self._stamps.setdefault(name, deque(maxlen=self.WINDOW_MEMORY))
            stamps.append(time.monotonic())

    def rate(self, name: str, window: float) -> int:
        """How many `name` events landed in the last `window` seconds."""
        cutoff = time.monotonic() - window
        with self._lock:
            stamps = self._stamps.get(name)
            return sum(1 for stamp in stamps if stamp >= cutoff) if stamps else 0

    def note_reconnect(self) -> None:
        self.mark("reconnects")

    def churn(self, window: float = 60.0) -> int:
        """How many reconnects landed in the last `window` seconds.

        A keyboard that reconnects repeatedly is a keyboard dropping and repeating
        keystrokes, which is the one cause of genuinely garbled output that this
        daemon can observe at all.
        """
        return self.rate("reconnects", window)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def summary(self) -> str:
        counts = self.snapshot()
        if not counts:
            return "no HID++ activity yet"
        parts = [f"{name}={value}" for name, value in sorted(counts.items()) if value]
        parts.append(f"uptime={time.monotonic() - self.started:.0f}s")
        return " ".join(parts)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._stamps.clear()
            self.started = time.monotonic()


HEALTH = Health()

_lock = threading.Lock()
_records: deque[Record] = deque(maxlen=RING_SIZE)
_echo = False
_dump_path: Path | None = None


def set_dump_path(path: Path | None) -> None:
    """Where :func:`anomaly` flushes to. Unset means keep the ring in memory only.

    The CLI points this at :func:`logiswitch.platform.trace_path`; leaving it unset
    keeps the protocol layer free of any opinion about the filesystem, which is
    also what lets the tests exercise it without writing anywhere.
    """
    global _dump_path
    _dump_path = path


def anomaly(reason: str, limit: int | None = None) -> Path | None:
    """Something happened that should not have. Flush the ring while it still holds it."""
    if _dump_path is None:
        return None
    return dump(reason, _dump_path, limit)


def set_echo(enabled: bool) -> None:
    """Also stream every frame to the log at DEBUG (the ``--trace`` flag)."""
    global _echo
    _echo = enabled
    if enabled:
        log.setLevel(logging.DEBUG)


def echoing() -> bool:
    return _echo


def record(direction: str, label: str, frame: bytes, summary: str = "") -> None:
    entry = Record(
        monotonic=time.monotonic(),
        wall=time.time(),
        direction=direction,
        label=label,
        hexbytes=bytes(frame).hex(),
        summary=summary,
    )
    with _lock:
        _records.append(entry)
    if _echo:
        log.debug("%s", entry.render())


def note(text: str) -> None:
    """Drop a non-frame marker into the ring so frames can be tied to decisions."""
    entry = Record(time.monotonic(), time.time(), NOTE, "", "", text)
    with _lock:
        _records.append(entry)
    if _echo:
        log.debug("%s", entry.render())


def snapshot(limit: int | None = None) -> list[Record]:
    with _lock:
        entries = list(_records)
    return entries[-limit:] if limit else entries


def clear() -> None:
    with _lock:
        _records.clear()


def render(limit: int | None = None) -> str:
    entries = snapshot(limit)
    if not entries:
        return "(no frames recorded)"
    return "\n".join(entry.render() for entry in entries)


def dump(reason: str, path: Path, limit: int | None = None) -> Path | None:
    """Append the ring to `path`, newest history last. Never raises.

    Called when something anomalous happens, so it must not itself become a new
    failure: a read-only home directory means no trace, not a crashed agent.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {stamp}  {reason} =====\n")
            handle.write(f"health: {HEALTH.summary()}\n")
            handle.write(render(limit))
            handle.write("\n")
        return path
    except OSError as exc:
        log.debug("could not write the trace to %s: %s", path, exc)
        return None


def _rotate(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.exists() and path.stat().st_size > TRACE_MAX_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
