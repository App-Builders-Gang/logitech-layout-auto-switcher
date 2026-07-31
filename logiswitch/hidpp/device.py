"""One logical HID++ device, with everything static about it cached.

The platform table (which platform index means macOS, which means Windows) is
firmware-constant, so it is read once per session. That turns a steady-state
"make sure this keyboard is on Windows" from ~6 round trips down to one read and,
only when it actually differs, one write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import protocol as p
from .transport import Transport

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlatformOption:
    """One platform the device can be switched to."""

    index: int
    os_names: tuple[str, ...]
    os_mask: int = 0

    @property
    def label(self) -> str:
        return "/".join(self.os_names) if self.os_names else f"platform {self.index}"


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

    def __init__(self, transport: Transport, index: int, protocol_version: tuple[int, int] = (0, 0)):
        self.transport = transport
        self.index = index
        self.protocol_version = protocol_version
        self._features: dict[int, int | None] = {}
        self._name: str | None = None
        self._platform_feature: int | None = None
        self._options: tuple[PlatformOption, ...] | None = None
        self._probed = False

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<HidppDevice index={self.index} name={self._name!r}>"

    # -- root -----------------------------------------------------------------

    def ping(self, timeout: float = 0.6) -> tuple[int, int]:
        reply = self.transport.request(
            self.index, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, b"\x00\x00\xaa", timeout=timeout
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
            r = self.transport.request(
                self.index, fi, p.MP_GET_PLATFORM_DESCRIPTOR, bytes([i])
            )
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
        for option in self.options:
            if key in option.os_names:
                return option
        raise p.UnsupportedFeature(
            f"device {self.index} advertises no platform for {key} "
            f"(has: {', '.join(o.label for o in self.options) or 'none'})"
        )

    # -- read / write the live platform ---------------------------------------

    def current_platform(self) -> int | None:
        """Platform index the *active* host is currently set to, or None."""
        self.probe()
        if self._platform_feature == p.FEATURE_MULTIPLATFORM:
            fi = self.feature_index(p.FEATURE_MULTIPLATFORM)
            r = self.transport.request(
                self.index, fi, p.MP_GET_HOST_PLATFORM, bytes([p.HOST_CURRENT])
            )
            return r[2] if len(r) > 2 else None
        if self._platform_feature == p.FEATURE_DUALPLATFORM:
            fi = self.feature_index(p.FEATURE_DUALPLATFORM)
            r = self.transport.request(self.index, fi, p.DP_GET_PLATFORM)
            return r[0] if r else None
        return None

    def host_platform_detail(self, host_index: int = p.HOST_CURRENT) -> dict:
        """Full getHostPlatform record; MULTIPLATFORM only. For `status` and probing."""
        fi = self.feature_index(p.FEATURE_MULTIPLATFORM)
        r = self.transport.request(self.index, fi, p.MP_GET_HOST_PLATFORM, bytes([host_index]))
        return {
            "host_index": r[0],
            "status": r[1],
            "platform_index": r[2],
            "platform_source": r[3],
            "raw": r.hex(),
        }

    def set_platform(self, platform_index: int) -> None:
        """The programmatic equivalent of holding Fn+O / Fn+P."""
        self.probe()
        if self._platform_feature == p.FEATURE_MULTIPLATFORM:
            fi = self.feature_index(p.FEATURE_MULTIPLATFORM)
            self.transport.request(
                self.index, fi, p.MP_SET_HOST_PLATFORM, bytes([p.HOST_CURRENT, platform_index])
            )
            return
        if self._platform_feature == p.FEATURE_DUALPLATFORM:
            fi = self.feature_index(p.FEATURE_DUALPLATFORM)
            self.transport.request(self.index, fi, p.DP_SET_PLATFORM, bytes([platform_index]))
            return
        raise p.UnsupportedFeature(f"device {self.index} cannot switch platform")

    def ensure_os(self, os_name: str) -> tuple[bool, PlatformOption]:
        """Idempotently point this device at `os_name`.

        Returns ``(changed, option)``. The read comes first so the common case --
        already correct -- costs exactly one round trip and touches nothing.
        """
        option = self.option_for_os(os_name)
        try:
            current = self.current_platform()
        except (p.HidppError, p.HidppTimeout):
            current = None
        if current == option.index:
            return False, option
        self.set_platform(option.index)
        return True, option
