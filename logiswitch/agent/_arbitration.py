"""Multihost / peer-arbitration methods, split out of the agent for readability.

These are the methods that decide whether this machine should be writing to a
shared keyboard right now or yield it to a competing machine. They are mixed into
:class:`logiswitch.agent.Agent` via :class:`_ArbitrationMixin`.

Note: :meth:`_hold_off` and :meth:`_peer_present` live on ``Agent`` itself (in
``agent/__init__.py``) rather than here. Their bodies read ``MAX_DEFER`` and
``PEER_MEMORY`` as module globals, and the test suite rebinds those names on the
``logiswitch.agent`` module via ``monkeypatch.setattr``. A ``LOAD_GLOBAL`` only
consults the module dict of the file a function was defined in, so for the
patched values to reach them they must be defined in ``agent/__init__.py``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from .. import activity, diagnostics, notify, trace
from ..hidpp import protocol as p
from . import CHURN_THRESHOLD, CHURN_WINDOW, REVERT_WINDOW, RIVAL_RECHECK, SETTLED_CHECKS

log = logging.getLogger("logiswitch.agent")

if TYPE_CHECKING:
    # Types only -- never imported at runtime, so there is no import cycle. The
    # class-level annotations below let mypy see the attributes ``Agent`` defines.
    from . import AgentConfig, Session


class _ArbitrationMixin:
    # Declared here solely for mypy: every attribute and method below is defined
    # on ``Agent`` (in __init__.py), not on this mixin. Without these annotations
    # mypy flags ``self.<attr>`` accesses as missing from ``_ArbitrationMixin``.
    if TYPE_CHECKING:
        cfg: AgentConfig
        notifier: notify.Notifier
        _sessions: list[Session]
        _peer_last_seen: float | None
        _peer_sw_id: int | None
        _foreign_warned: bool
        _foreign_seen: int
        _churn_warned: bool
        _absent_since: float | None
        _stood_down: bool
        _rival_warned: bool
        _no_arbitration_warned: bool
        _rivals_checked: float
        _rivals: list[str]
        _clean_checks: int

        def _peer_present(self) -> bool: ...

    def _note_foreign_write(self, frame: bytes) -> None:
        """Another host is setting this keyboard's platform too.

        A ``setHostPlatform`` reply that no request of ours was waiting for did not
        come from us -- and because a shared receiver delivers device traffic to
        whoever is listening, it is visible even though the writer is on a different
        machine. Two hosts that want different platforms for one keyboard cannot
        both win, and the loser is whoever typed last.

        Crucially this *stops* the close re-checking. Racing an opponent that writes
        every few seconds would turn a slow tug-of-war into a fast one, hammering the
        RF link for a fight neither side can win. Say so plainly instead.
        """
        trace.HEALTH.mark("foreign_platform_writes")  # observability
        self._peer_last_seen = time.monotonic()  # the decision reads this
        sw_id = frame[3] & 0x0F
        self._peer_sw_id = sw_id
        if self._foreign_warned:
            return
        self._foreign_warned = True
        rivals = self._local_rival()
        if rivals:
            # Do not assert "another machine" when a program on this one explains it
            # just as well. Naming both possibilities is the honest report, and the
            # advice is the same either way.
            log.warning(
                "this keyboard's platform is being set by software that is not us "
                "(software id 0x%02X). %s %s running here, so it may be that rather "
                "than another machine. Correcting it back to %s.",
                sw_id,
                " and ".join(rivals),
                "is" if len(rivals) == 1 else "are",
                self.cfg.target_os,
            )
            trace.anomaly(f"foreign setHostPlatform, swId 0x{sw_id:X}, local rivals present")
            return
        if sw_id == p.SW_ID:
            # Our own former fixed software id: the peer is an old build of this very
            # tool, which will not stand down for anyone. Saying so is the single most
            # useful sentence this log can print during a staged upgrade.
            log.warning(
                "another machine is running an OLD logiswitch (software id 0x%02X) and "
                "setting this keyboard's platform. Old builds do not take turns -- "
                "update logiswitch on that machine and the two will share properly.",
                sw_id,
            )
        else:
            log.warning(
                "another machine is setting this keyboard's platform (software id "
                "0x%02X). Whichever of you is being typed on should win; if the layout "
                "still fights, run `logiswitch doctor` on both.",
                sw_id,
            )
        trace.anomaly(f"foreign setHostPlatform, swId 0x{sw_id:X}")
        self.notifier.send(
            notify.PEER,
            "Another computer is also setting this keyboard's layout. Whichever machine "
            "you are typing on should win -- if it keeps flapping, update logiswitch "
            "on the other machine.",
        )

    def _adopt_foreign_observations(self) -> None:
        """Treat "the platform changed under us" as a peer sighting.

        The frame-level detector only fires when another host's setHostPlatform
        *reply* happens to reach us. On a shared receiver that is unreliable -- each
        machine sees the other's reads far more often than its writes -- so the peer
        went unnoticed and neither side ever stood down. The device layer already
        works out that the platform was set by software that is not us; this adopts
        that conclusion.
        """
        seen = sum(
            device.foreign_writes for session in self._sessions for device, _ in session.supported
        )
        if seen <= self._foreign_seen:
            return
        self._foreign_seen = seen
        self._peer_last_seen = time.monotonic()
        trace.HEALTH.mark("foreign_platform_writes")
        if self._foreign_warned:
            return
        self._foreign_warned = True
        rivals = self._local_rival()
        if rivals:
            log.warning(
                "this keyboard's platform was set by software that is not us. %s %s "
                "running here, so it may be that rather than another machine. "
                "Correcting it back to %s.",
                " and ".join(rivals),
                "is" if len(rivals) == 1 else "are",
                self.cfg.target_os,
            )
            return
        log.warning(
            "another machine is setting this keyboard's platform. Whichever of you is "
            "being typed on should win; if it keeps flapping, run `logiswitch doctor` "
            "on both and check they are the same version."
        )
        self.notifier.send(
            notify.PEER,
            "Another computer is also setting this keyboard's layout. Whichever machine "
            "you are typing on should win.",
        )

    def _idle_seconds(self) -> float | None:
        """Seconds since this machine was last used, or None if unknowable.

        A seam: the module function answers for the whole process, so several agents
        sharing one interpreter need a per-instance override to model separate
        machines. Also the single place any future ownership policy would hook into.
        """
        return activity.seconds_since_input()

    def _note_link(self, index: int, established: bool) -> None:
        """Track how often the wireless link comes and goes.

        This is the only handle the daemon has on garbled or repeated characters:
        it never sees a keystroke, but a link that keeps collapsing and
        re-establishing is a link dropping and repeating them. Counting the churn
        turns "the keyboard sometimes types nonsense" into a number.
        """
        log.debug("device %d link %s", index, "up" if established else "down")
        if not established:
            trace.HEALTH.bump("link_drops")
            return
        trace.HEALTH.note_reconnect()
        recent = trace.HEALTH.churn(CHURN_WINDOW)
        if recent < CHURN_THRESHOLD:
            self._churn_warned = False
            return
        if not self._churn_warned:
            self._churn_warned = True
            log.warning(
                "the wireless link has re-established %d times in %.0fs -- that is "
                "interference, a low battery or a failing receiver, and it drops and "
                "repeats keystrokes regardless of which layout the keyboard is in",
                recent,
                CHURN_WINDOW,
            )
            trace.anomaly(f"link churn: {recent} reconnects in {CHURN_WINDOW:.0f}s")
            self.notifier.send(
                notify.LINK,
                f"The keyboard's wireless link is unstable -- {recent} reconnects in "
                f"{CHURN_WINDOW:.0f}s. That drops and repeats keystrokes.",
            )

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

    def _note_arbitration(self, standing_down: bool) -> None:
        """Log the change of turn once, not once per pass."""
        if standing_down == self._stood_down:
            return
        self._stood_down = standing_down
        if standing_down:
            log.info(
                "standing down: another machine is using this keyboard and this one "
                "has been idle for over %.0fs",
                self.cfg.active_window,
            )
            self.notifier.send(
                notify.PEER,
                "Another computer is using this keyboard, so its layout is being left "
                "alone here. Type on this machine to take it back.",
            )
        else:
            log.info("taking the keyboard back: this machine is in use again")

    def _standing_down(self) -> bool:
        """Should this machine leave the keyboard alone right now?

        Only ever true when *all* of these hold: another machine is competing for the
        keyboard, this platform can actually measure whether it is being used, and it
        has not been used for a while. Any one of them missing means behave exactly as
        a single machine would -- a lone agent must never stop correcting just because
        nobody has typed for a minute.

        And never on evidence a program on *this* machine could have produced. Taking
        turns is a bargain between machines: yielding is right because somebody is
        typing on the other one. No such person exists behind Logi Options+, so
        yielding to it means going quiet and staying quiet for as long as nobody
        touches this keyboard -- which is exactly when the layout needs to be right
        for the next person who does. The protocol cannot tell the two apart: a
        platform set by "host software" says only that, and the peer detectors
        (:meth:`_note_foreign_write`, :meth:`_adopt_foreign_observations`) conclude
        "another machine" from evidence a local rival satisfies equally well. So when
        one is running, ambiguity resolves to correcting rather than surrendering --
        and sustained fighting is reported by :meth:`_warn_about_contention`, which
        is the response that actually helps.
        """
        if self.cfg.observe:
            return True
        if not self._peer_present():
            return False
        rivals = self._local_rival()
        if rivals:
            if not self._rival_warned:
                self._rival_warned = True
                log.info(
                    "not standing down: the platform is being set by software that is "
                    "not us, but %s %s running here, so this cannot be attributed to "
                    "another machine. Holding %s and correcting as usual.",
                    " and ".join(rivals),
                    "is" if len(rivals) == 1 else "are",
                    self.cfg.target_os,
                )
            return False
        idle = self._idle_seconds()
        if idle is None:
            # No way to prove we are in use; gating here would stand down forever.
            if not self._no_arbitration_warned:
                self._no_arbitration_warned = True
                log.warning(
                    "another machine is competing for this keyboard, but this platform "
                    "cannot report input activity -- taking turns automatically is not "
                    "possible here. Use --observe on whichever machine should yield."
                )
            return False
        return idle > self.cfg.active_window

    def _local_rival(self) -> list[str]:
        """Logitech software running on this machine, cached.

        Listing processes is a subprocess call, so the answer is reused for
        :data:`RIVAL_RECHECK`. Nothing here is time-critical: this only ever decides
        whether ambiguous evidence is allowed to make us yield.
        """
        now = time.monotonic()
        if not self._rivals_checked or now - self._rivals_checked >= RIVAL_RECHECK:
            self._rivals_checked = now
            self._rivals = diagnostics.competing_software()
        return self._rivals

    def _warn_about_contention(self, recent: int) -> None:
        """Name the other process rather than assuming which one it is.

        This warning used to assert Logi Options+ was the culprit without ever
        looking, which sent people uninstalling software that was not running while
        the real cause went unexamined.
        """
        rivals = diagnostics.competing_software()
        if rivals:
            log.warning(
                "corrected the platform %d times in the last %.0f minutes, and %s %s "
                "running -- that software enforces its own host OS on this collection. "
                "Quit or uninstall it if the layout will not stay on %s.",
                recent,
                REVERT_WINDOW / 60,
                ", ".join(rivals),
                "is" if len(rivals) == 1 else "are",
                self.cfg.target_os,
            )
        else:
            log.warning(
                "corrected the platform %d times in the last %.0f minutes and no other "
                "Logitech software is running. Run `logiswitch doctor`, and see the "
                "frame trace for what the keyboard reports between writes.",
                recent,
                REVERT_WINDOW / 60,
            )
        trace.anomaly(f"platform corrected {recent} times in {REVERT_WINDOW:.0f}s")
        # Deliberately hung off the same branch as the warning above, so the desktop
        # and the log can never tell different stories about the same condition.
        self.notifier.send(
            notify.FLAPPING,
            f"The keyboard layout keeps reverting -- corrected {recent} times in "
            f"{REVERT_WINDOW / 60:.0f} minutes. Run: logiswitch doctor",
        )

    def _unsettled(self) -> bool:
        """Is the device still proving it will keep what it was told?

        True right after a correction and until it has read back correctly
        :data:`SETTLED_CHECKS` times running. While true the agent re-checks at
        :data:`FAST_RECHECK`, which is what keeps a keyboard that drops the setting
        every few seconds usable rather than wrong half the time.
        """
        if self._peer_present() and not self._local_rival():
            # Another machine is writing too. Re-checking twice a second would just
            # make the tug-of-war faster; neither of us can win it that way.
            #
            # Only when it really is another machine. If Logi Options+ is running
            # here, the same evidence is equally explained by it -- and easing off
            # against a device that keeps dropping the setting is precisely backwards:
            # close re-checking is the only thing that keeps such a keyboard usable.
            return False
        return self._clean_checks < SETTLED_CHECKS
