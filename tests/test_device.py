import fakehid
import pytest

from logiswitch.hidpp import HidppDevice
from logiswitch.hidpp import protocol as p


def keyboard(transport):
    return HidppDevice(transport, fakehid.MX_KEYS_INDEX)


def test_feature_resolution_is_cached(receiver, transport):
    device = keyboard(transport)
    assert device.feature_index(p.FEATURE_MULTIPLATFORM) == fakehid.MULTIPLATFORM_INDEX
    before = receiver.writes
    for _ in range(5):
        device.feature_index(p.FEATURE_MULTIPLATFORM)
    assert receiver.writes == before, "a resolved feature index must never be re-queried"


def test_unsupported_feature_is_cached_too(receiver, transport):
    """A mouse must be asked once, then left alone forever."""
    mouse = HidppDevice(transport, fakehid.MX_MASTER_INDEX)
    with pytest.raises(p.UnsupportedFeature):
        mouse.feature_index(p.FEATURE_MULTIPLATFORM)
    before = receiver.writes
    for _ in range(5):
        with pytest.raises(p.UnsupportedFeature):
            mouse.feature_index(p.FEATURE_MULTIPLATFORM)
    assert receiver.writes == before


def test_probe_reports_the_recorded_platform_table(transport):
    info = keyboard(transport).probe()
    assert info.name == "MX Keys S"
    assert info.supported
    assert info.kind == "MULTIPLATFORM 0x4531"
    assert [(o.index, o.os_names) for o in info.options] == [
        (0, ("android", "linux", "windows")),
        (1, ("macos",)),
        (2, ("ios",)),
        (3, ("chrome",)),
    ]


def test_probe_marks_a_mouse_unsupported(transport):
    info = HidppDevice(transport, fakehid.MX_MASTER_INDEX).probe()
    assert not info.supported
    assert info.kind == "unsupported"


def test_probe_is_cached(receiver, transport):
    device = keyboard(transport)
    device.probe()
    before = receiver.writes
    device.probe()
    assert receiver.writes == before


def test_ensure_os_switches_and_is_then_idempotent(receiver, transport):
    device = keyboard(transport)
    fake = receiver.devices[fakehid.MX_KEYS_INDEX]
    fake.platform = 0

    changed, option = device.ensure_os("mac")
    assert changed and option.index == 1
    assert fake.platform == 1
    assert fake.set_calls == [1]

    changed, option = device.ensure_os("mac")
    assert not changed
    assert fake.set_calls == [1], "no write when the platform already matches"


def test_steady_state_costs_one_round_trip(receiver, transport):
    """The v1 regression: re-reading the static table on every assert."""
    device = keyboard(transport)
    device.ensure_os("windows")  # warms the caches
    before = receiver.writes
    device.ensure_os("windows")
    assert receiver.writes - before == 1


def test_unknown_os_for_this_device_is_rejected(transport):
    device = keyboard(transport)
    with pytest.raises(p.UnsupportedFeature):
        device.ensure_os("tizen")


def test_dualplatform_devices_are_supported(monkeypatch):
    craft = fakehid.craft_dualplatform(index=2)
    receiver = fakehid.install(monkeypatch, fakehid.FakeReceiver([craft]))
    from logiswitch import hidpp

    transport = hidpp.open_transport(hidpp.find_groups()[0])
    try:
        device = HidppDevice(transport, 2)
        info = device.probe()
        assert info.supported
        assert info.kind == "DUALPLATFORM 0x4530"
        changed, option = device.ensure_os("mac")
        assert changed and option.index == 0
        assert craft.platform == 0
        assert receiver.devices[2].set_calls == [0]
    finally:
        transport.close()


def test_device_name_is_read_once(receiver, transport):
    device = keyboard(transport)
    assert device.name == "MX Keys S"
    before = receiver.writes
    assert device.name == "MX Keys S"
    assert receiver.writes == before
