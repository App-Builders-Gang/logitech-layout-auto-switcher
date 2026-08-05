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
#: How often to re-check which Logitech software is running here. This shells out to
#: list processes, so it is deliberately infrequent -- rivals appear and disappear on
#: the timescale of somebody launching an app, not of a keystroke.
RIVAL_RECHECK = 120.0


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
        #: KVM/Easy-Switch round trip actually took to recover.
        self._absent_since: float | None = None
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
            "logiswitch agent starting on %s: target=%s reassert=%s%s%s",
            # Two machines sharing a keyboard produce two identical-looking logs;
            # naming the host is what makes a pair of them readable side by side.
            socket.gethostname(),
            self.cfg.target_os,
            f"{self.cfg.reassert_interval:.0f}s" if self.cfg.reassert_interval else "off",
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
        self._next_summary = time.monotonic() + STEADY_SUMMARY_INTERVAL

        while not self._stop.is_set():
            now = time.monotonic()
            deadlines = [d for d in (next_assert, next_heartbeat) if d is not None]
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
                self._note_presence(ok)
                if ok:
                    self._retry = 0.0
                    if self._unsettled():
                        # Keep looking until it has stayed put several times over.
                        # One re-check was not enough: firmware that drops the write
                        # a few seconds later passed that check and then reverted
                        # unobserved until the next heartbeat.
                        next_assert = time.monotonic() + FAST_RECHECK
                    elif self._peer_present():
                        # Spread the next look so two machines that both briefly think
                        # they are in use cannot settle into a lock-step fight.
                        next_assert = (
                            time.monotonic() + FAST_RECHECK + random.uniform(0.0, PEER_JITTER)
                        )
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

        self._adopt_foreign_observations()

        if changed:
            self._changes_in_a_row += 1
            self._clean_checks = 0
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
