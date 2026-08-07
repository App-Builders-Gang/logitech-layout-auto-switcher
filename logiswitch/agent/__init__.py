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
import logging
import queue
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# ``activity`` is re-exported: no Core method uses it directly now that
# ``_idle_seconds`` lives on the arbitration mixin, but the test suite reaches it
# as ``agent_module.activity`` (and patches attributes on that module object).
from .. import activity as activity
from .. import diagnostics, hidpp, keystate, notify, trace
from ..hidpp import protocol as p
from ..platform import DeviceEvent, Watcher, create_watcher

log = logging.getLogger(__name__)

#: Window over which reconnects are counted when deciding the link is unstable.
CHURN_WINDOW = 60.0
#: Reconnects within that window before the log says the link is the problem. A
#: KVM hop or an Easy-Switch press is one or two; a keyboard dropping and repeating
#: characters because the RF link keeps collapsing is many more.
CHURN_THRESHOLD = 6
#: How often the agent says it is alive and well, even when nothing changes.
STEADY_SUMMARY_INTERVAL = 600.0

#: How long to wait before looking again when a modifier is held down.
#:
#: Changing the platform remaps the bottom row -- the key left of the space bar is
#: Command on macOS and Alt on Windows. Remap it between a key's press and its
#: release and the host never sees the release for the modifier it registered, so
#: the modifier sticks down. That is a genuinely nasty thing to do to someone who
#: is mid-sentence, and the layout being briefly wrong is much the lesser evil, so
#: a correction waits for the chord to finish.
DEFER_WHILE_HELD = 0.4
#: ... but not forever. A modifier still down after this long is stuck or jammed,
#: not in use, and refusing to correct the layout on its account helps nobody.
#:
#: Deliberately generous. People hold Command for a long time on purpose -- cycling
#: windows with Command-Tab, Command-clicking a list of files, dragging. A short
#: ceiling here would remap the keys in the middle of exactly those actions, which
#: is the failure this guard exists to prevent. The layout being briefly wrong is
#: cheap; stranding a modifier is not.
MAX_DEFER = 30.0

#: Window over which platform corrections are counted, and how many of them mean
#: something is pushing the layout back.
#:
#: Counted over a window rather than consecutively, and that distinction is the
#: whole point. Every correction is followed three seconds later by a check that
#: reads the platform back as right, so a "changes in a row" counter is reset by
#: the agent's own success and can never climb. A keyboard being reverted every
#: twelve seconds, for days, kept that counter oscillating between 0 and 1 and the
#: warning never fired once.
#:
#: Switching machines on a KVM legitimately produces a correction or two; nothing
#: legitimate produces five within five minutes.
REVERT_WINDOW = 300.0
REVERT_THRESHOLD = 5

#: How soon to look again after correcting a platform, while the device is proving
#: unreliable.
#:
#: Some firmware accepts a platform write and then quietly drops it seconds later.
#: While that is happening the keyboard is in the wrong mode -- on a Mac the key you
#: press as Command sends Option -- so the only thing that matters is how quickly it
#: is put back. Measured on hardware that reverts every twelve seconds: re-checking
#: at this interval keeps it correct 96% of the time instead of about half.
#:
#: This is not the polling the rest of the agent avoids. It engages only after a
#: correction and switches itself off again after :data:`SETTLED_CHECKS` consecutive
#: clean reads, so a well-behaved device never pays for it.
FAST_RECHECK = 0.5
#: Consecutive clean checks before trusting the device again.
#:
#: This has to span longer than the fault it is watching for, or it defeats itself:
#: a device that reverts every twelve seconds would pass a three-second probation,
#: settle, and then revert unobserved until the next heartbeat. Twenty seconds of
#: uninterrupted correctness is a reasonable bar for calling a device trustworthy.
SETTLED_CHECKS = 40

# -- sharing one keyboard between several machines ----------------------------

#: How long another machine is remembered after its last observed write.
#:
#: Deliberately long, and the reasoning is worth keeping. A short window looks safer
#: but oscillates: once the winning machine has the platform right it stops writing,
#: so every idle machine forgets the peer exists, starts writing again, collides,
#: detects, stands down -- on a cycle exactly one window long, forever.
#:
#: Long memory is safe because standing down only ever applies *while idle*, and an
#: idle machine's layout does not matter to anyone. Believing in a peer that has gone
#: therefore costs nothing: the moment somebody types, the machine is active and takes
#: the keyboard back regardless of what it believes.
PEER_MEMORY = 1800.0
#: Idle time beyond which this machine gives up the keyboard to whoever is using it.
#: Longer than a pause for thought, shorter than the time it takes to notice a wrong
#: layout after switching machines.
ACTIVE_WINDOW = 20.0
#: Spread of the random delay added to a write while a peer is around, so two machines
#: that both briefly think they are active cannot settle into a synchronised fight.
PEER_JITTER = 0.4
#: How often to re-sample whether this machine is in use, while a peer holds the
#: keyboard and this machine is idle.
#:
#: This is the replacement for polling the *keyboard* twice a second for half an hour
#: after one peer sighting. Nothing is sent to the device to take this reading: it asks
#: the host when it last saw input (``CGEventSourceSecondsSinceLastEventType`` on macOS,
#: ``GetLastInputInfo`` on Windows), so it costs no HID++ traffic at all and asks for no
#: Accessibility privilege. The interval bounds how quickly the layout is put right after
#: somebody returns to this machine -- a second or two reads as "the first keystroke or
#: two" rather than "the first twenty seconds".
IDLE_PEEK = 1.0
#: How often to re-check which Logitech software is running here. This shells out to
#: list processes, so it is deliberately infrequent -- rivals appear and disappear on
#: the timescale of somebody launching an app, not of a keystroke.
RIVAL_RECHECK = 120.0

# -- event-only mode: look when something happens, not otherwise ----------------

#: Heartbeat interval in event-only mode. The ordinary heartbeat exists to catch a
#: device coming back on hardware that announces nothing, and event-only mode's claim
#: is that an arriving device always *does* announce itself -- by the OS for a KVM hop,
#: by starting to talk for an Easy-Switch return. That claim is well founded and it is
#: still a claim, so a much slower heartbeat stays behind it: five minutes bounds
#: "silently wrong" rather than leaving it unbounded. 12 requests/hour against 180.
QUIET_REASSERT = 300.0
#: How many times to retry an absent device in event-only mode before giving up and
#: waiting for it to announce itself. Three attempts span about fourteen seconds
#: (2s, 4s, 8s), which covers a device that is genuinely mid-reconnect -- the only
#: thing retries were ever for. Beyond that the device is on another machine, and
#: asking every ten seconds for the rest of the day is the sustained traffic event-only
#: mode exists to remove. Unbounded retry is also the dominant source of timeouts on a
#: receiver shared with Logi Options+: a Bolt receiver stays enumerated across an
#: Easy-Switch move, so the transport stays live and each retry burns the full deadline.
QUIET_RETRY_ATTEMPTS = 3
#: How many corrections inside one arrival event-only mode tolerates before it stops
#: close re-checking and waits. One correction is normal; two is a device that dropped
#: the write once; three inside a single arrival is a fight re-checking cannot win, and
#: continuing would turn "event-only" into the poll loop it was added to remove.
QUIET_CORRECTION_BUDGET = 3


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
    #: Show desktop notifications. Throttled per kind, so a keyboard that keeps
    #: reverting produces one message and then a standing-condition one, not a
    #: notification every twelve seconds.
    notify: bool = True
    #: Never write, only observe and report. The right setting for a machine that
    #: should always yield the keyboard to another.
    observe: bool = False
    #: Idle seconds after which this machine gives the keyboard up to a competitor.
    active_window: float = ACTIVE_WINDOW
    #: Only ever write this Easy-Switch host index, and only while it is the active
    #: one. None means "whichever host the keyboard says it is talking to", which is
    #: right whenever each machine has its own receiver.
    claim_host: int | None = None
    #: Look when something happens, and not otherwise. A device arrives, the platform
    #: is read and corrected if wrong, and then the agent goes quiet until the next
    #: arrival. The right setting for a machine that shares its receiver with Logi
    #: Options+: the only thing that should ever compete for the receiver is a real
    #: platform switch, not a heartbeat or a retry or a peer poll. ``None`` (the
    #: default) resolves to event-only when local Logitech software is detected and to
    #: the ordinary duty cycle otherwise; ``True``/``False`` force either way.
    event_only: bool | None = None
    #: Safety re-check interval while event-only. :data:`QUIET_REASSERT` by default;
    #: 0 means "not one request until something happens", and accepts that an event
    #: the agent misses leaves the layout wrong until the user notices.
    event_only_reassert: float = QUIET_REASSERT


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


# Imported after the constants, AgentConfig and Session are defined above: each
# mixin pulls the names it needs (module-level constants, and ``Session`` for
# ``_build_sessions``) from this package's namespace at import time. E402
# (import not at top of file) is inherent to this split.
from ._arbitration import _ArbitrationMixin  # noqa: E402
from ._sessions import _SessionMixin  # noqa: E402


class Agent(_ArbitrationMixin, _SessionMixin):
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
        #: Completed passes of :meth:`_apply`. Observability only; never a decision.
        self._apply_count = 0
        self._hints: dict[str, list[int]] = self._load_hints()
        self._retry = 0.0
        self._changes_in_a_row = 0
        self._contention_warned = False
        self._last_summary: str | None = None
        #: When the devices stopped answering, so the log can say how long a
        #: KVM/Easy-Switch round trip actually took to recover -- and, being the only
        #: record that anything was ever away, what decides whether a device's chatter
        #: means it came back. Written by the worker, read from reader threads:
        #: rebound, never mutated, exactly like ``_driven``.
        self._absent_since: float | None = None
        #: Whether the last completed pass reached anything we drive. A different
        #: question from "did the pass succeed" -- a write that is accepted and will
        #: not confirm fails the pass, but the keyboard plainly answered. Reading one
        #: for the other put "nothing is answering" in the log about a device sitting
        #: on the desk, and left ``_absent_since`` set on it.
        self._reached = False
        #: Whether this machine was idle (past :attr:`active_window`) at the last
        #: input sample. The peer-watch samples the host clock without touching the
        #: device and reclaims the layout only on the idle-to-in-use *edge* -- so a
        #: machine that is continuously in use is not polled, only one that went away
        #: and came back. Starts False so a machine that has been in use since start
        #: does not reclaim until it has actually been away.
        self._was_idle = False
        #: Next time the agent says out loud that it is alive. Without this a
        #: healthy log falls silent after one line, and "it went wrong at 14:32"
        #: has nothing to be checked against.
        self._next_summary = 0.0
        self._churn_warned = False
        #: When a platform write was first held back because keys were down.
        self._deferred_since: float | None = None
        #: Consecutive passes that found the platform already correct. Starts at the
        #: settled value so a well-behaved device is never polled: close watch begins
        #: only once something has actually had to be corrected.
        self._clean_checks = SETTLED_CHECKS
        #: Retry attempts since the device last answered, for event-only's bounded
        #: retry. Reset on any device event that implies presence returned.
        self._retry_attempts = 0
        #: Corrections made inside the current arrival, for event-only's correction
        #: budget. Reset whenever a device arrives (DEVICE_CHANGED) or wakes from
        #: absence, so the budget is per-arrival rather than lifetime.
        self._arrival_corrections = 0
        #: Whether the correction budget has been exhausted this arrival, so the
        #: "giving up" log line fires once rather than once per pass.
        self._quiet_gave_up = False
        #: Feature index of MULTIPLATFORM per driven device, so an inbound frame can
        #: be recognised as a platform write rather than any old reply.
        self._platform_features: dict[int, int] = {}
        self._foreign_warned = False
        self._no_arbitration_warned = False
        #: Software id of the competing machine, for diagnosis.
        self._peer_sw_id: int | None = None
        #: When another machine was last seen writing. Per-agent, not global.
        self._peer_last_seen: float | None = None
        #: Foreign-write total already adopted, so each is counted once.
        self._foreign_seen = 0
        #: Platform this agent last wrote per device, kept across session
        #: rebuilds so a fresh device object still knows its own history.
        self._last_written: dict[int, int] = {}
        #: Whether we are currently letting another machine have the keyboard.
        self._stood_down = False
        #: Logitech software running on *this* machine, and when we last looked.
        #: Cached because finding out means listing every process.
        self._rivals: list[str] = []
        self._rivals_checked = 0.0
        self._rival_warned = False
        self.notifier = notify.Notifier(enabled=config.notify)

    # -- public API -----------------------------------------------------------

    def start(self) -> None:
        log.info(
            "logiswitch agent starting on %s: target=%s reassert=%s%s%s%s",
            # Two machines sharing a keyboard produce two identical-looking logs;
            # naming the host is what makes a pair of them readable side by side.
            socket.gethostname(),
            self.cfg.target_os,
            f"{self.cfg.reassert_interval:.0f}s" if self.cfg.reassert_interval else "off",
            # event_only may still resolve to the auto choice once the rival scan runs,
            # so name the configured value rather than the resolved one at start-up.
            " event-only" if self.cfg.event_only else "",
            " observe-only" if self.cfg.observe else "",
            f" host={self.cfg.claim_host}" if self.cfg.claim_host is not None else "",
        )
        host = diagnostics.host_summary()
        log.info("host: %s", diagnostics.describe_host(host))
        # Reuse the process list we just paid for. Without this the first foreign
        # frame would shell out from a *reader* thread -- ``_note_foreign_write``
        # runs there -- and stall frame reading for as long as `ps` takes.
        self._rivals = list(host["competing_software"])
        self._rivals_checked = time.monotonic()
        if host["non_latin_script"]:
            # Worth saying plainly: this is not something logiswitch can correct, and
            # it produces wrong characters that look exactly like a layout fault.
            log.warning(
                "this host's keyboard input is set to %s (%s). That decides which "
                "characters appear and logiswitch does not manage it -- if the output "
                "is the wrong alphabet, change the input source, not the platform.",
                host["non_latin_script"],
                host["input_source"],
            )
            shortcut = "Ctrl+Space" if diagnostics.is_macos() else "Alt+Shift"
            self.notifier.send(
                notify.INPUT_SOURCE,
                f"This host's keyboard input types {host['non_latin_script']}, not "
                f"Latin. logiswitch does not manage that -- change it with {shortcut}.",
            )
        self._stop.clear()
        self.notifier.start()
        self._worker = threading.Thread(target=self._run, name="logiswitch-worker", daemon=True)
        self._worker.start()
        self._watcher = create_watcher(self.cfg.vendor_id, self.cfg.force_polling)
        try:
            self._watcher.start(self._on_device_event)
            log.info("watching for device changes via %s", self._watcher.name)
        except Exception as exc:
            log.warning(
                "%s watcher failed to start (%s); falling back to polling", self._watcher.name, exc
            )
            from ..platform.watchers.polling import PollingWatcher

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
        self.notifier.stop()
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
        if p.is_connection_notification(frame):
            # HID++ 1.0: the receiver itself announces a device connecting. Testing
            # byte 2 alone would also catch any 2.0 reply whose feature happens to
            # sit at index 0x41, which is why this goes through the full check.
            established, _encrypted = p.connection_flags(frame)
            self._note_link(index, established)
            self._put((_Event.DEVICE_WOKE, index))
            return
        if (
            self._platform_features.get(index) == frame[2]
            and (frame[3] >> 4) == p.MP_SET_HOST_PLATFORM
            and (frame[3] & 0x0F) != 0
        ):
            # A setHostPlatform reply nobody here was waiting for: somebody else
            # asked. Our own would have been claimed by its sink before reaching us.
            self._note_foreign_write(frame)
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
            if not self._might_have_been_away():
                # Present, answering, and talking: chatter, not a return. Counted
                # rather than dropped silently, so a report that a return went
                # unnoticed can be checked against a number instead of argued about.
                trace.HEALTH.bump("settled_chatter")
                return
            self._put((_Event.DEVICE_WOKE, index))

    def _might_have_been_away(self) -> bool:
        """Could a device have left and come back since one of them last answered?

        The gate on treating unsolicited chatter as a reconnect, and it exists because
        the frame itself cannot answer the question. What a returning device says first
        is device-specific, which is why :meth:`_on_hidpp_frame` trusts the sender
        rather than the message -- but trusting the sender *alone* made every
        notification a return, and a keyboard sitting on the desk emits plenty.

        A key Logi Options+ has diverted emits one *instead* of a keystroke, and
        Options+ has to receive it and act. Answering that with a request of our own
        200ms later put two programs on one receiver's transaction slots at the moment
        one of them was busy; the receiver said it had no room -- 462 times, in replies
        addressed to Options+ -- and the calculator key did nothing at all. Stopping
        this agent made it work immediately, in both directions, on demand.

        So ask whether a return is *possible* rather than whether one happened. It is
        not, while a device we drive is answering us: it never left, and the frame is a
        lock-key state, a battery reading, or a key somebody else has diverted. It is
        possible when the last completed pass found nothing answering -- a keyboard on
        another Easy-Switch channel answers nothing, which is what sets
        ``_absent_since`` -- or before the first pass has established what we drive.

        No new threshold: "long enough to have plausibly been away" is already defined
        and already tuned as :attr:`AgentConfig.reassert_interval`, because absence is
        discovered by the heartbeat and only by the heartbeat. A second constant would
        be a second definition of the same thing, and the two would drift. Turn the
        heartbeat off and nothing is left to notice a device has gone, so chatter
        becomes the only signal there is and is trusted exactly as it was before.

        Deliberately not per-device. Two driven devices, one asleep, keeps this open on
        the other's chatter -- but a pass is already failing in that state and the retry
        band is already transmitting, so being cleverer buys nothing and a per-device
        ledger would have to be kept in step from two threads.
        """
        if not self._heartbeat_interval():
            # No heartbeat of any kind is running -- not the ordinary one, and not
            # event-only's slow backstop. With nothing left to discover absence,
            # chatter is the only signal there is and is trusted exactly as before.
            return True
        if not self._driven:
            return True
        return self._absent_since is not None

    def _can_reclaim_on_input(self) -> bool:
        """Can this machine notice it is being used again, without touching the device?

        When another machine shares the keyboard, the right trigger for taking it back is
        not "a peer was once seen, so poll the keyboard forever" but "somebody just sat
        down here". The host already knows when it was last used, and asking it costs no
        HID++ traffic and no Accessibility prompt -- which is why it is the basis of
        :meth:`_idle_seconds` already. This is only a capability check; the edge itself is
        detected in :meth:`_run` by sampling at :data:`IDLE_PEEK` while idle.

        Where the host cannot report input activity at all, this returns False and the
        caller keeps the old behaviour -- a machine that can never prove it is in use must
        not be left permanently silent.
        """
        return activity.available()

    def _event_only(self) -> bool:
        """Is this agent in event-only mode?

        ``None`` (the default) resolves to event-only when local Logitech software is
        detected, because a machine sharing its receiver with Logi Options+ is the one
        case where the ordinary heartbeat and retry duty cycle competes for transaction
        slots the other program needs -- and a diverted key, which Options+ has to
        receive and act on, is starved by exactly that competition. ``True`` or
        ``False`` force the choice regardless of what else is installed, for machines
        that know better than the heuristic.

        Resolved fresh on each call: a process-list scan is exactly what
        :meth:`_local_rival` already caches for :data:`RIVAL_RECHECK` seconds, so this
        rides that cache rather than holding a second opinion that could disagree.
        """
        if self.cfg.event_only is not None:
            return self.cfg.event_only
        return bool(self._local_rival())

    def _heartbeat_interval(self) -> float:
        """Seconds between backstop re-checks, or 0 to disable the backstop.

        Event-only mode rests on the claim that an arriving device always announces
        itself; the heartbeat there is a slow safety net, not the primary trigger.
        """
        if self._event_only():
            return self.cfg.event_only_reassert
        return self.cfg.reassert_interval

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
        next_idle_peek: float | None = None  # set while watching for the user's return
        self._next_summary = time.monotonic() + STEADY_SUMMARY_INTERVAL

        while not self._stop.is_set():
            now = time.monotonic()
            deadlines = [d for d in (next_assert, next_heartbeat, next_idle_peek) if d is not None]
            deadlines.append(self._next_summary)
            timeout = max(0.0, min(deadlines) - now)

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
                next_idle_peek = None  # the peer-watch is meaningless across a reconnect
                self._retry = 0.0
                self._retry_attempts = 0
                # A new arrival: the correction budget and its give-up flag are per-arrival.
                self._arrival_corrections = 0
                self._quiet_gave_up = False
                continue
            if kind is _Event.DEVICE_WOKE:
                log.debug("device %s spoke unprompted -- treating it as a reconnect", payload)
                # A device announces itself before it will answer requests: a scan
                # one second after the frame still finds nothing. Restart the
                # backoff so the retries that follow are 0.2s, 2s, 4s, 8s instead
                # of inheriting the 30s ceiling reached while it was away -- that
                # inheritance is what made a return take half a minute to correct.
                self._retry = 0.0
                self._retry_attempts = 0
                self._arrival_corrections = 0
                self._quiet_gave_up = False
                next_assert = min(next_assert or float("inf"), time.monotonic() + 0.2)
                continue

            now = time.monotonic()
            if next_assert is not None and now >= next_assert:
                if self._standing_down():
                    # Someone else is using this keyboard. _apply_once enforces this
                    # too; skipping the pass here just avoids the pointless work.
                    self._note_arbitration(True)
                    next_assert = now + max(self.cfg.active_window / 4, 1.0)
                    continue
                held = self._hold_off()
                if held is not None:
                    # A chord is in progress. Come back in a moment rather than
                    # remapping the keys out from under it.
                    next_assert = now + DEFER_WHILE_HELD
                    continue
                next_assert = None
                try:
                    ok = self._apply()
                except Exception:
                    log.exception("unexpected failure while applying the platform")
                    ok = False
                    # A pass that blew up proves nothing about who answered, and the
                    # away-record is now load-bearing: leaving a stale True here would
                    # keep the chatter gate shut on a keyboard that really had gone.
                    self._reached = False
                # Not `ok`: that is "the pass did what it set out to do", and a write
                # the device accepts but will not confirm fails it while answering
                # every request. See :attr:`_reached`.
                self._note_presence(self._reached)
                if ok:
                    self._retry = 0.0
                    self._retry_attempts = 0
                    if self._unsettled():
                        # Keep looking until it has stayed put several times over.
                        # One re-check was not enough: firmware that drops the write
                        # a few seconds later passed that check and then reverted
                        # unobserved until the next heartbeat.
                        next_assert = time.monotonic() + FAST_RECHECK
                    elif self._peer_present() and self._can_reclaim_on_input():
                        # Another machine is sharing this keyboard, but this one can
                        # tell when it is being used again without sending anything to
                        # the device. Polling the *keyboard* twice a second for thirty
                        # minutes after one sighting was what starved a receiver this
                        # machine shares with Logi Options+ of transaction slots; so
                        # while this machine is idle we send nothing at all and instead
                        # watch for the user's return. The moment they are back, one
                        # pass puts the layout right; while they are away, the keyboard
                        # is the other machine's to set.
                        next_idle_peek = time.monotonic() + IDLE_PEEK
                    elif self._peer_present():
                        # A peer is here and this machine cannot measure its own input
                        # activity. The old bargain is the safe one: re-check on a
                        # jittered fast cadence so two machines that both briefly think
                        # they are in use cannot settle into a lock-step fight.
                        next_assert = (
                            time.monotonic() + FAST_RECHECK + random.uniform(0.0, PEER_JITTER)
                        )
                    next_heartbeat = (
                        time.monotonic() + interval
                        if (interval := self._heartbeat_interval())
                        else None
                    )
                else:
                    self._retry_attempts += 1
                    if self._event_only() and self._retry_attempts > QUIET_RETRY_ATTEMPTS:
                        # Event-only: a device that has not answered in a few tries is on
                        # another machine, not mid-reconnect. Asking every few seconds for
                        # as long as it is away is the sustained traffic this mode removes
                        # -- and on a receiver shared with Logi Options+ it is the dominant
                        # source of timeouts, since a Bolt receiver stays enumerated across
                        # an Easy-Switch move and each retry burns the full deadline. Stop,
                        # and wait for the device to announce itself; the slow heartbeat
                        # stays armed as the backstop.
                        self._retry = 0.0
                        trace.HEALTH.bump("quiet_retries_abandoned")
                        log.debug(
                            "device absent after %d tries; waiting for it to return",
                            QUIET_RETRY_ATTEMPTS,
                        )
                        continue
                    self._retry = min(
                        self.cfg.retry_max, max(self.cfg.retry_initial, self._retry * 2)
                    )
                    next_assert = time.monotonic() + self._retry
                    log.debug("retrying in %.1fs", self._retry)
                continue

            if next_idle_peek is not None and now >= next_idle_peek:
                # A peer holds the keyboard and this machine was idle. Re-sample the
                # host's own input clock -- not the device -- and only do real work on
                # the idle-to-in-use *edge*. The peer can only set the platform while
                # the keyboard is on the other machine, so one pass when the user
                # returns is enough: there is nothing to defend against while they
                # keep typing here, and polling for it is exactly the traffic this
                # watch exists to avoid.
                if not self._peer_present():
                    # The peer is gone (its memory expired). Nothing to reclaim from,
                    # so stop watching and let the heartbeat and events take over.
                    next_idle_peek = None
                    self._was_idle = False
                    continue
                idle = self._idle_seconds()
                in_use = idle is not None and idle <= self.cfg.active_window
                if in_use and self._was_idle:
                    # Was away, now back: the edge. One pass reclaims the layout.
                    # The watch re-arms after that pass (peer still present) to catch
                    # the user leaving and returning again.
                    self._was_idle = False
                    next_idle_peek = None
                    next_assert = now
                    trace.HEALTH.bump("quiet_arrivals")
                else:
                    self._was_idle = not in_use
                    next_idle_peek = now + IDLE_PEEK
                continue

            if now >= self._next_summary:
                self._next_summary = now + STEADY_SUMMARY_INTERVAL
                self._log_steady_summary()

            if next_heartbeat is not None and now >= next_heartbeat:
                next_heartbeat = None
                log.debug("heartbeat: re-checking the platform")
                next_assert = now

        self._teardown_sessions("worker exiting")

    def _hold_off(self) -> set[str] | None:
        """Modifiers to wait for, or None to go ahead now.

        Returns None once :data:`MAX_DEFER` has elapsed even if keys are still down:
        at that point the modifier is stuck rather than being used, and continuing
        to defer would leave the layout wrong indefinitely for the sake of a key
        nobody is pressing.

        Lives on ``Agent`` (not ``_ArbitrationMixin``) because its body reads
        ``MAX_DEFER`` as a module global and the test suite rebinds that name on
        the ``logiswitch.agent`` module; a ``LOAD_GLOBAL`` only consults the dict
        of the module the function was defined in.
        """
        held = keystate.modifiers_held()
        if not held:
            if self._deferred_since is not None:
                log.debug(
                    "modifiers released after %.1fs; correcting now",
                    time.monotonic() - self._deferred_since,
                )
                self._deferred_since = None
            return None

        now = time.monotonic()
        if self._deferred_since is None:
            self._deferred_since = now
            log.debug("holding off: %s held", keystate.describe(held))
            trace.note(f"deferring platform write, {keystate.describe(held)} held")
            return held

        waited = now - self._deferred_since
        if waited < MAX_DEFER:
            return held

        self._deferred_since = None
        trace.HEALTH.bump("stuck_modifiers")
        log.warning(
            "%s has been held for %.0fs -- that is a stuck modifier, not typing. "
            "Correcting the layout anyway; tap the key to clear it.",
            keystate.describe(held),
            waited,
        )
        trace.anomaly(f"modifier stuck down for {waited:.0f}s: {keystate.describe(held)}")
        self.notifier.send(
            notify.STUCK_MODIFIER,
            f"The {keystate.describe(held)} key has been held down for "
            f"{waited:.0f} seconds. Tap it once to release it.",
        )
        return None

    def _peer_present(self) -> bool:
        """Has another machine written to this keyboard recently?

        Per-agent state on purpose. The obvious implementation reads the process-wide
        ``trace.HEALTH`` counter, which is fine for the one-agent-per-machine reality
        but makes every agent in a shared process believe it saw what its neighbours
        saw -- which is precisely the situation the multi-machine tests create.

        Lives on ``Agent`` (not ``_ArbitrationMixin``) because its body reads
        ``PEER_MEMORY`` as a module global and the test suite rebinds that name on
        the ``logiswitch.agent`` module; a ``LOAD_GLOBAL`` only consults the dict
        of the module the function was defined in.
        """
        if self._peer_last_seen is None:
            return False
        return (time.monotonic() - self._peer_last_seen) < PEER_MEMORY

    def _log_steady_summary(self) -> None:
        """Say what is true right now, whether or not anything changed.

        The per-device INFO line is de-duplicated so a healthy agent does not repeat
        itself, which leaves the log with no evidence it was even running. This is
        that evidence, and it carries the counters and the host input source, so a
        report of "wrong characters at 14:32" can be read straight off the timeline.
        """
        state = self._last_summary or ("no device answering" if self._sessions else "no device")
        peer = ""
        if self._peer_present():
            peer = f" | peer sw0x{self._peer_sw_id:02X}" if self._peer_sw_id else " | peer present"
            if self._local_rival():
                # Could equally be the software running here, and this line is read
                # as a record of what was true at the time. Do not let it assert one.
                peer += " (or local software)"
            if self._stood_down:
                peer += " (standing down)"
        log.info(
            "steady on %s: %s | %s | %s%s",
            socket.gethostname(),
            state,
            trace.HEALTH.summary(),
            diagnostics.describe_host(),
            peer,
        )

    # -- the actual work ------------------------------------------------------

    def _apply(self) -> bool:
        """Run one full check-and-correct pass over every attached device.

        The counter is bumped on every exit path, successful or not. It is the only
        way to observe from outside that a pass has *finished* rather than merely
        started -- ``_driven`` is populated while the session is still being built,
        which is a whole device scan before any platform is read.
        """
        try:
            return self._apply_once()
        finally:
            self._apply_count += 1

    def _apply_once(self) -> bool:
        if not self._sessions:
            self._build_sessions()
        if not self._sessions:
            log.debug("no Logitech HID++ endpoint present")
            self._last_summary = None
            self._reached = False
            return False

        if self._standing_down():
            # Another machine is using this keyboard. Keep the session open -- we
            # still want to see what it does -- but write nothing.
            #
            # Enforced here rather than in the scheduling loop on purpose: this is
            # the function that writes, and every other way in (`--once`, a test,
            # anything added later) would otherwise sail straight past the rule.
            self._note_arbitration(True)
            return True
        self._note_arbitration(False)

        applied = 0
        changed = 0
        failed = 0
        for session in list(self._sessions):
            for device, info in session.supported:
                try:
                    result = device.ensure_os(self.cfg.target_os)
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
                    self._reached = False
                    return False
                applied += 1
                option = result.option
                if result.changed:
                    self._last_written[device.index] = option.index
                if result.changed:
                    changed += 1
                    if result.confirmed is False:
                        # The read-back contradicted the write. Saying "switched"
                        # here is how a log ends up insisting everything is fine
                        # while the wrong characters keep appearing.
                        failed += 1
                        log.error(
                            "%s did NOT switch to %s (platform %d): the write was "
                            "accepted and the device still reads something else",
                            info.name,
                            option.label,
                            option.index,
                        )
                        self.notifier.send(
                            notify.FAILED,
                            f"{info.name} would not switch to {option.label}. "
                            f"The keyboard accepted the change and ignored it.",
                        )
                    else:
                        # Record what was held across the remap. The guard in
                        # _hold_off should make this always "none"; if it ever is
                        # not, that line is the evidence for a stuck modifier.
                        across = keystate.modifiers_held()
                        log.info(
                            "switched %s to %s (platform %d)%s",
                            info.name,
                            option.label,
                            option.index,
                            "" if result.confirmed else " (unconfirmed)",
                        )
                        if across:
                            trace.HEALTH.bump("switched_while_held")
                            log.warning(
                                "the layout changed while %s was held down -- that "
                                "can strand the modifier; tap it to clear it",
                                keystate.describe(across),
                            )
                        self.notifier.send(
                            notify.SWITCHED,
                            f"{info.name} switched to the {option.label} layout.",
                        )
                    self._last_summary = None
                else:
                    summary = f"{info.name}={option.label}"
                    log.debug("%s reads %s, nothing to do", info.name, option.label)
                    if summary != self._last_summary:
                        log.info("%s already on %s", info.name, option.label)
                        self._last_summary = summary

        # "Did anything we drive answer at all?" -- deliberately a different question
        # from the one this function returns. A pass fails when a write would not
        # confirm, and reading that as "nothing is answering" put a sentence in the
        # log that was not true and left the away-record set on a keyboard sitting
        # right there -- which is the record that now decides whether its next
        # notification means it came back.
        self._reached = applied > 0

        self._adopt_foreign_observations()

        if changed:
            self._changes_in_a_row += 1
            self._clean_checks = 0
            self._arrival_corrections += 1
            trace.HEALTH.mark("platform_corrections")
            # Deliberately NOT tearing the session down here any more. Re-enumeration
            # after a platform change is real, and a stale handle is handled: the
            # next request raises TransportClosed and the session is rebuilt. What
            # tearing down cost was a full rediscovery -- about a second, and a scan
            # window on top -- before the platform could be read again, which on
            # firmware that reverts within six seconds meant the keyboard sat in the
            # wrong mode for most of every cycle.
            recent = trace.HEALTH.rate("platform_corrections", REVERT_WINDOW)
            if recent >= REVERT_THRESHOLD and not self._contention_warned:
                self._contention_warned = True
                self._warn_about_contention(recent)
        elif applied:
            self._changes_in_a_row = 0
            self._clean_checks += 1
            if self._clean_checks == SETTLED_CHECKS:
                log.debug("platform has held for %d checks; easing off", SETTLED_CHECKS)
            # Deliberately *not* clearing _contention_warned here. A correction is
            # always followed by a successful check, so resetting on success is what
            # stopped this warning ever being reached; the window above is what says
            # the trouble is over.
            if trace.HEALTH.rate("platform_corrections", REVERT_WINDOW) == 0:
                self._contention_warned = False

        return applied > 0 and failed == 0
