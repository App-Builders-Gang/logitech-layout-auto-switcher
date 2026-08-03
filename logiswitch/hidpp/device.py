"""One logical HID++ device, with everything static about it cached.

The platform table (which platform index means macOS, which means Windows) is
firmware-constant, so it is read once per session. That turns a steady-state
"make sure this keyboard is on Windows" from ~6 round trips down to one read and,
only when it actually differs, one write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import NamedTuple

from .. import trace
from . import protocol as p
from .transport import DEFAULT_TIMEOUT, Transport

log = logging.getLogger(__name__)

#: A platform write is followed by a read to confirm it took. The device is busy
#: re-establishing its link at that moment, so this is deliberately short and a
#: timeout here means "could not confirm", never "failed".
VERIFY_TIMEOUT = 0.6


@dataclass(frozen=True)
class PlatformOption:
    """One platform the device can be switched to."""

    index: int
    os_names: tuple[str, ...]
    os_mask: int = 0

    @property
    def label(self) -> str:
        return "/".join(self.os_names) if self.os_names else f"platform {self.index}"


class EnsureResult(NamedTuple):
    """What one check-and-correct pass over a device actually achieved.

    ``confirmed`` is the field worth having: ``True`` the device read back as asked,
    ``False`` it demonstrably did not, ``None`` nothing was written or the device did
    not answer the check in time. Without it a caller can only say "a write was
    accepted", which is what let the log announce a switch that never happened.
    """

    changed: bool
    option: PlatformOption
    confirmed: bool | None = None


@dataclass
class DeviceInfo:
    """What we know about a device after probing it once."""

    index: int
    protocol: tuple[int, int]
    name: str = "?"
    feature: int | None = None  # 0x4531 or 0x4530, whichever it implements
    options: tuple[PlatformOption, ...] = field(default_factory=tuple)

    @property
    def supported(self) -> bool:
        return self.feature is not None and bool(self.options)

    @property
    def kind(self) -> str:
        if self.feature == p.FEATURE_MULTIPLATFORM:
            return "MULTIPLATFORM 0x4531"
        if self.feature == p.FEATURE_DUALPLATFORM:
            return "DUALPLATFORM 0x4530"
        return "unsupported"


class HidppDevice:
    """A device reachable through a :class:`Transport` at one device index."""

    def __init__(
        self, transport: Transport, index: int, protocol_version: tuple[int, int] = (0, 0)
    ):
        self.transport = transport
        self.index = index
        self.protocol_version = protocol_version
        self._features: dict[int, int | None] = {}
        self._name: str | None = None
        self._platform_feature: int | None = None
        self._options: tuple[PlatformOption, ...] | None = None
        self._probed = False
        #: Resolved Easy-Switch host index, cached for the life of this session.
        self._host_index: int | None = None
        #: Host index pinned by configuration, which overrides what the device says.
        self._claimed_host: int | None = None
        #: Last getHostPlatform reading, so a change can be logged and a repeat cannot.
        self._last_host_summary: str | None = None
        #: The platform *we* last wrote, to tell our own change from someone else's.
        self._wrote_platform: int | None = None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<HidppDevice index={self.index} name={self._name!r}>"

    # -- root -----------------------------------------------------------------

    def ping(self, timeout: float = 0.6) -> tuple[int, int]:
        reply = self.transport.request(
            self.index,
            p.FEATURE_ROOT,
            p.ROOT_GET_PROTOCOL_VERSION,
            b"\x00\x00\xaa",
            timeout=timeout,
        )
        if len(reply) < 3 or reply[2] != 0xAA:
            raise p.HidppTimeout(f"bad ping echo from device {self.index}")
        self.protocol_version = (reply[0], reply[1])
        return self.protocol_version

    def feature_index(self, feature_id: int) -> int:
        """Resolve a feature id to this device's feature index, cached.

        A cached ``None`` means "asked once, not supported" -- the whole point is
        that an unsupported device (a mouse, say) is never re-queried.
        """
        if feature_id in self._features:
            index = self._features[feature_id]
            if index is None:
                raise p.UnsupportedFeature(
                    f"device {self.index} does not support feature 0x{feature_id:04X}"
                )
            return index
        reply = self.transport.request(
            self.index,
            p.FEATURE_ROOT,
            p.ROOT_GET_FEATURE,
            bytes([feature_id >> 8, feature_id & 0xFF, 0x00]),
        )
        index = reply[0] if reply else 0
        if index == 0:
            self._features[feature_id] = None
            raise p.UnsupportedFeature(
                f"device {self.index} does not support feature 0x{feature_id:04X}"
            )
        self._features[feature_id] = index
        return index

    @property
    def name(self) -> str:
        if self._name is not None:
            return self._name
        try:
            fi = self.feature_index(p.FEATURE_DEVICE_NAME)
            count = self.transport.request(self.index, fi, 0)[0]
            chunks: list[bytes] = []
            offset = 0
            while offset < count and len(chunks) < 8:
                chunk = self.transport.request(self.index, fi, 1, bytes([offset]))
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
            self._name = b"".join(chunks)[:count].decode("utf-8", "replace").strip() or "?"
        except (p.UnsupportedFeature, p.HidppError, p.HidppTimeout, p.TransportClosed):
            self._name = "?"
        return self._name

    # -- platform capability --------------------------------------------------

    def probe(self) -> DeviceInfo:
        """Work out how (or whether) this device can switch OS layout. Cached."""
        if not self._probed:
            self._probed = True
            try:
                self._probe_multiplatform()
            except (p.UnsupportedFeature, p.HidppError, p.HidppTimeout):
                try:
                    self._probe_dualplatform()
                except (p.UnsupportedFeature, p.HidppError, p.HidppTimeout):
                    self._platform_feature = None
                    self._options = ()
        return DeviceInfo(
            index=self.index,
            protocol=self.protocol_version,
            name=self.name,
            feature=self._platform_feature,
            options=self._options or (),
        )

    def _probe_multiplatform(self) -> None:
        fi = self.feature_index(p.FEATURE_MULTIPLATFORM)
        info = self.transport.request(self.index, fi, p.MP_GET_FEATURE_INFOS)
        count = info[3] if len(info) > 3 else 0
        options = []
        for i in range(count):
            r = self.transport.request(self.index, fi, p.MP_GET_PLATFORM_DESCRIPTOR, bytes([i]))
            if len(r) < 4:
                continue
            mask = (r[2] << 8) | r[3]
            options.append(
                PlatformOption(index=r[0], os_names=tuple(p.os_names_for_mask(mask)), os_mask=mask)
            )
        if not options:
            raise p.UnsupportedFeature(f"device {self.index} reported no platform descriptors")
        self._platform_feature = p.FEATURE_MULTIPLATFORM
        self._options = tuple(options)
        self._log_platform_table()

    def _log_platform_table(self) -> None:
        """Write the descriptor table out once, with the raw masks.

        Everything downstream is an index chosen from this table, so if a mask is
        misread every later log line is confidently wrong: "switched to macos" while
        writing the Windows index. Recording the raw bytes once per session is what
        makes that falsifiable after the fact instead of a theory.
        """
        for option in self._options or ():
            log.debug(
                "device %d platform %d: mask 0x%04X -> %s",
                self.index,
                option.index,
                option.os_mask,
                option.label,
            )
            trace.note(
                f"dev{self.index} platform {option.index} mask=0x{option.os_mask:04X} "
                f"({option.label})"
            )
        ambiguous = [
            o for o in self._options or () if len(o.os_names) > 1 and "macos" in o.os_names
        ]
        for option in ambiguous:
            # macOS and Windows differing is the entire reason this tool exists; a
            # descriptor that lumps them together cannot express the distinction.
            if {"windows", "linux", "android"} & set(option.os_names):
                log.warning(
                    "device %d platform %d claims both macOS and %s (mask 0x%04X) -- "
                    "one platform cannot give both layouts, so the layout may stay wrong",
                    self.index,
                    option.index,
                    "/".join(n for n in option.os_names if n != "macos"),
                    option.os_mask,
                )

    def _probe_dualplatform(self) -> None:
        # Older Craft / K-series hardware exposes only the two-bucket variant.
        self.feature_index(p.FEATURE_DUALPLATFORM)
        self._platform_feature = p.FEATURE_DUALPLATFORM
        self._options = tuple(
            PlatformOption(index=index, os_names=names)
            for index, names in sorted(p.DUALPLATFORM_CHOICES.items())
        )

    @property
    def options(self) -> tuple[PlatformOption, ...]:
        self.probe()
        return self._options or ()

    def option_for_os(self, os_name: str) -> PlatformOption:
        key = p.normalise_os(os_name)
        matches = [option for option in self.options if key in option.os_names]
        if not matches:
            raise p.UnsupportedFeature(
                f"device {self.index} advertises no platform for {key} "
                f"(has: {', '.join(o.label for o in self.options) or 'none'})"
            )
        if len(matches) > 1:
            # Taking the first is still the right guess, but say so: if this device
            # is the one typing wrong characters, which of the two we picked is the
            # first thing worth knowing.
            log.warning(
                "device %d advertises %d platforms for %s (%s); using platform %d",
                self.index,
                len(matches),
                key,
                ", ".join(f"{o.index}:0x{o.os_mask:04X}" for o in matches),
                matches[0].index,
            )
        return matches[0]

    # -- which host are we? ----------------------------------------------------

    def claim_host(self, host_index: int | None) -> None:
        """Pin the Easy-Switch host this device will be addressed on.

        For the topology where every machine has its own receiver, each one owns a
        different host slot and should only ever write that one. Left unset, the host
        is resolved from the device, which is right when the machines share a receiver
        and are therefore all the same host.
        """
        self._claimed_host = host_index

    def current_host(self) -> int:
        """The Easy-Switch host index to address, resolved rather than assumed.

        The spec says ``0xFF`` means "the currently active host", and for most
        devices it does. On the MX Keys S it does not: Solaar carries the comment
        that you "can't just use the first byte = 0xFF (for current host) because of
        a bug in the firmware of the MX Keys S", and resolves the concrete index
        first. A write addressed to a host the firmware mishandles is acknowledged
        and then quietly fails to stick -- which reads, from the outside, exactly
        like other software reverting the setting.

        So ask 0x1815 HOSTS_INFO which host this actually is, and address it by
        number. Devices without 0x1815 keep the old ``0xFF`` behaviour, and
        :meth:`feature_index` remembers a missing feature so that costs one query
        per session at most.

        Cached only on success: a timeout here must not pin the whole session to the
        fallback. The cache lives on the device object, which discovery recreates on
        every session, so an Easy-Switch hop cannot leave a stale index behind.
        """
        if self._claimed_host is not None:
            return self._claimed_host
        if self._host_index is not None:
            return self._host_index
        try:
            fi = self.feature_index(p.FEATURE_HOSTS_INFO)
            reply = self.transport.request(self.index, fi, p.HI_GET_FEATURE_INFO)
        except (p.UnsupportedFeature, p.HidppError, p.HidppTimeout):
            log.debug("device %d has no usable 0x1815; addressing host 0xFF", self.index)
            return p.HOST_CURRENT
        if len(reply) <= 3:
            return p.HOST_CURRENT
        self._host_index = reply[3]
        log.debug("device %d is on Easy-Switch host %d", self.index, self._host_index)
        trace.note(f"dev{self.index} current host = {self._host_index}")
        return self._host_index

    # -- read / write the live platform ---------------------------------------

    def current_platform(self, timeout: float = DEFAULT_TIMEOUT) -> int | None:
        """Platform index the *active* host is currently set to, or None."""
        self.probe()
        if self._platform_feature == p.FEATURE_MULTIPLATFORM:
            record = self.host_platform_detail(timeout=timeout)
            self._note_host_record(record)
            return record["platform_index"]
        if self._platform_feature == p.FEATURE_DUALPLATFORM:
            fi = self.feature_index(p.FEATURE_DUALPLATFORM)
            r = self.transport.request(self.index, fi, p.DP_GET_PLATFORM, timeout=timeout)
            return r[0] if r else None
        return None

    def _note_host_record(self, record: dict) -> None:
        """Log a getHostPlatform record when any field of it moved.

        Silence while nothing changes, a line the moment it does -- including *who*
        changed it. A platform that flips to `set-by=keyboard` is someone's Fn+O; one
        that flips to `set-by=host software` without us writing is other software.
        """
        summary = p.describe_host_platform(record)
        if summary == self._last_host_summary:
            return
        previous, self._last_host_summary = self._last_host_summary, summary
        trace.note(f"dev{self.index} {summary}")
        if previous is None:
            log.debug("device %d %s", self.index, summary)
            return
        log.info("device %d platform record changed: %s -> %s", self.index, previous, summary)
        if record["platform_source"] == 1 and record["platform_index"] != self._wrote_platform:
            log.warning(
                "device %d was switched to platform %s by hand (Fn+O / Fn+P); correcting it back",
                self.index,
                record["platform_index"],
            )

    def host_platform_detail(
        self, host_index: int | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> dict:
        """Full getHostPlatform record; MULTIPLATFORM only. For `status` and probing.

        `host_index` of ``None`` resolves the real host via :meth:`current_host`;
        pass an explicit index to interrogate a specific Easy-Switch channel, which
        is what ``probe`` does.
        """
        fi = self.feature_index(p.FEATURE_MULTIPLATFORM)
        host = self.current_host() if host_index is None else host_index
        r = self.transport.request(
            self.index, fi, p.MP_GET_HOST_PLATFORM, bytes([host]), timeout=timeout
        )
        return p.decode_host_platform(r)

    def set_platform(self, platform_index: int) -> None:
        """The programmatic equivalent of holding Fn+O / Fn+P."""
        self.probe()
        trace.note(f"dev{self.index} setHostPlatform -> {platform_index}")
        if self._platform_feature == p.FEATURE_MULTIPLATFORM:
            fi = self.feature_index(p.FEATURE_MULTIPLATFORM)
            self.transport.request(
                self.index,
                fi,
                p.MP_SET_HOST_PLATFORM,
                bytes([self.current_host(), platform_index]),
            )
            self._wrote_platform = platform_index
            trace.HEALTH.bump("platform_writes")
            return
        if self._platform_feature == p.FEATURE_DUALPLATFORM:
            fi = self.feature_index(p.FEATURE_DUALPLATFORM)
            self.transport.request(self.index, fi, p.DP_SET_PLATFORM, bytes([platform_index]))
            self._wrote_platform = platform_index
            trace.HEALTH.bump("platform_writes")
            return
        raise p.UnsupportedFeature(f"device {self.index} cannot switch platform")

    def verify_platform(self, expected: int) -> bool | None:
        """Read back after a write. ``None`` means the device did not answer in time.

        The write's own reply only says the request was accepted, not that the mode
        took, and until now nothing ever looked again. A device mid-reconnect
        legitimately says nothing here, so an unanswered check is not a failure --
        it is the agent's follow-up pass that settles it.
        """
        try:
            actual = self.current_platform(timeout=VERIFY_TIMEOUT)
        except (p.HidppError, p.HidppTimeout, p.TransportClosed):
            log.debug("device %d did not answer the post-write check", self.index)
            return None
        if actual == expected:
            log.debug("device %d confirmed on platform %d", self.index, expected)
            return True
        trace.HEALTH.bump("platform_mismatches")
        log.warning(
            "device %d still reads platform %s after being set to %d -- "
            "the write was accepted but did not take",
            self.index,
            actual,
            expected,
        )
        trace.anomaly(f"dev{self.index} platform write did not take ({actual} != {expected})")
        return False

    def ensure_os(self, os_name: str) -> EnsureResult:
        """Idempotently point this device at `os_name`.

        The read comes first so the common case -- already correct -- costs exactly
        one round trip and touches nothing.

        A failed read propagates rather than being treated as "unknown, so write
        anyway". Writing blind looked harmless but reported ``changed=True`` every
        time an asleep device timed out, which drove the "another process is fighting
        us" warning purely from timeouts. A device that will not answer is one to
        retry, and the caller's backoff already does exactly that.
        """
        option = self.option_for_os(os_name)
        current = self.current_platform()
        if current == option.index:
            return EnsureResult(changed=False, option=option)
        log.debug(
            "device %d reads platform %s, wants %d for %s",
            self.index,
            current,
            option.index,
            option.label,
        )
        self.set_platform(option.index)
        return EnsureResult(
            changed=True, option=option, confirmed=self.verify_platform(option.index)
        )
