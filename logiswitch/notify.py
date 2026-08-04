"""Desktop notifications, on the two platforms this agent runs on.

Two constraints shape everything here.

**It must not run on the worker thread.** ``osascript`` costs about a tenth of a
second and PowerShell start-up is closer to a whole one. The worker thread also
services device events, so notifying inline would stall event handling every time
the layout changed -- and on hardware that keeps reverting, permanently. Delivery
therefore happens on a thread of its own, fed by a queue that drops rather than
blocks.

**It must not shout.** The keyboard this was written against corrects its platform
every twelve seconds, which is 300 notifications an hour. So each kind of message
has a cooldown, and a fault that keeps recurring is reported once, as a standing
condition, rather than once per occurrence.

No new dependencies: the text reaches the OS through a channel that cannot be quoted
wrong -- ``osascript`` argv on macOS, the WinRT toast API called in-process on Windows
-- so there is no command line to escape, and nothing here can break the agent if it
fails.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import subprocess
import threading
import time
from typing import Callable, NamedTuple

from .paths import is_macos, is_windows

log = logging.getLogger(__name__)

#: Shown as the notification title.
APP_TITLE = "logiswitch"

#: Default seconds before the same kind of message may be shown again.
COOLDOWN = 300.0
#: Standing conditions describe a situation rather than an event, so they repeat far
#: less often; being told hourly that the layout keeps reverting is enough.
STANDING_COOLDOWN = 1800.0
#: Queued notifications. Small on purpose: if delivery has fallen this far behind,
#: the backlog is stale and dropping it is better than showing it late.
QUEUE_SIZE = 16
#: How long a single send may take before it is abandoned.
SEND_TIMEOUT = 10.0

#: Message kinds. Each throttles independently, so a flapping layout cannot drown
#: out an unrelated warning about the wireless link.
SWITCHED = "switched"
FAILED = "failed"
FLAPPING = "flapping"
LINK = "link"
INPUT_SOURCE = "input-source"
STUCK_MODIFIER = "stuck-modifier"
PEER = "peer"

#: Kinds that describe an ongoing situation rather than a single event.
STANDING = frozenset({FLAPPING, LINK, INPUT_SOURCE, PEER})

#: Windows toasts are raised in-process through the WinRT COM API (see
#: ``_wintoast``): no ``powershell`` is spawned, and the title and body go into XML
#: built on the Python side, so there is no command line for them to escape into.

#: Constant AppleScript reading its text from ``argv``. Passing the message as an
#: argument rather than interpolating it into the script is what makes a device
#: called ``He said "hi"`` harmless instead of a syntax error or worse.
_APPLESCRIPT = (
    "on run argv",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    "end run",
)


class Notification(NamedTuple):
    kind: str
    body: str
    title: str = APP_TITLE

    @property
    def cooldown(self) -> float:
        return STANDING_COOLDOWN if self.kind in STANDING else COOLDOWN


#: A sender takes one notification and delivers it. Injectable so tests never spawn
#: a process -- the same shape as ``diagnostics.competing_software(runner=...)``.
Sender = Callable[[Notification], None]


def macos_command(note: Notification) -> list[str]:
    """The ``osascript`` argv for `note`, with the text as arguments."""
    command = ["osascript"]
    for line in _APPLESCRIPT:
        command += ["-e", line]
    # "--" keeps a body that begins with a hyphen from being read as an option.
    return [*command, "--", note.body, note.title]


def _send_macos(note: Notification) -> None:
    subprocess.run(
        macos_command(note),
        capture_output=True,
        timeout=SEND_TIMEOUT,
        check=True,
    )


def _send_windows(note: Notification) -> None:
    # Imported lazily so non-Windows platforms never touch ``ctypes``'s WinDLL and
    # the COM plumbing stays in one module.
    from ._wintoast import show_toast

    show_toast(note.title, note.body)


def default_sender() -> Sender | None:
    """The sender for this platform, or None where we have no way to notify."""
    if is_macos():
        return _send_macos
    if is_windows():
        return _send_windows
    return None


def backend_name() -> str:
    """What ``doctor`` and ``notify-test`` report."""
    if is_macos():
        return "macOS osascript"
    if is_windows():
        return "Windows toast (native)"
    return "unsupported on this platform"


class Notifier:
    """Throttled, off-thread desktop notifications.

    Not started until :meth:`start`; ``send`` before that (or when disabled) is a
    no-op, so callers never have to check.
    """

    def __init__(
        self,
        enabled: bool = True,
        sender: Sender | None = None,
        cooldown: float | None = None,
    ):
        self._sender = sender if sender is not None else default_sender()
        self.enabled = enabled and self._sender is not None
        self._cooldown_override = cooldown
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        #: kind -> when it may next be shown.
        self._muted_until: dict[str, float] = {}
        #: kind -> how many were suppressed since the last one got through. Reported
        #: with the next message of that kind, so the count is never simply lost.
        self._suppressed: dict[str, int] = {}

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="logiswitch-notifier", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Idempotent. Waits briefly for an in-flight send, then gives up on it."""
        thread, self._thread = self._thread, None
        self._stop.set()
        if thread is None:
            return
        # Wake the thread immediately rather than waiting out its poll interval.
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        thread.join(SEND_TIMEOUT + 1.0)
        if thread.is_alive():  # pragma: no cover - a wedged osascript
            log.debug("notifier thread did not stop; leaving it to the interpreter")

    # -- sending --------------------------------------------------------------

    def send(self, kind: str, body: str, title: str = APP_TITLE) -> bool:
        """Queue a notification. Returns whether it passed the throttle.

        Never raises and never blocks: this is called from the worker thread, in the
        middle of correcting a keyboard, and must not be able to interfere with that.
        """
        if not self.enabled:
            return False
        if not self._allow(kind):
            return False
        suppressed = self._take_suppressed(kind)
        if suppressed:
            body = f"{body} ({suppressed} similar hidden)"
        try:
            self._queue.put_nowait(Notification(kind, body, title))
        except queue.Full:
            log.debug("notification queue is full, dropping %s", kind)
            return False
        return True

    def _allow(self, kind: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if now < self._muted_until.get(kind, 0.0):
                self._suppressed[kind] = self._suppressed.get(kind, 0) + 1
                return False
            cooldown = self._cooldown_override
            if cooldown is None:
                cooldown = STANDING_COOLDOWN if kind in STANDING else COOLDOWN
            self._muted_until[kind] = now + cooldown
            return True

    def _take_suppressed(self, kind: str) -> int:
        with self._lock:
            return self._suppressed.pop(kind, 0)

    # -- delivery -------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                note = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if note is None:
                return
            self.deliver(note)

    def _drain(self) -> int:
        """Deliver everything queued, synchronously. A test seam.

        The delivery thread is the real path; this lets a test assert on what would
        have been shown without racing it.
        """
        delivered = 0
        while True:
            try:
                note = self._queue.get_nowait()
            except queue.Empty:
                return delivered
            if note is not None and self.deliver(note):
                delivered += 1

    def deliver(self, note: Notification) -> bool:
        """Hand one notification to the OS. Swallows every failure by design."""
        if self._sender is None:
            return False
        try:
            self._sender(note)
            return True
        except Exception as exc:
            # A notification that can take the agent down is worse than none at all.
            log.debug("could not show a notification (%s): %s", note.kind, exc)
            return False
