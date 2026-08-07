import fakehid
import pytest

from logiswitch import trace
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

    result = device.ensure_os("mac")
    assert result.changed and result.option.index == 1
    assert result.confirmed is True, "the write should have been read back"
    assert fake.platform == 1
    assert fake.set_calls == [1]

    result = device.ensure_os("mac")
    assert not result.changed
    assert result.confirmed is None, "nothing was written, so there is nothing to confirm"
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
        result = device.ensure_os("mac")
        assert result.changed and result.option.index == 0
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


# -- a read that fails must not become a blind write --------------------------


def test_a_failed_read_does_not_trigger_a_write(receiver, transport):
    """The bug: a timeout used to read as "unknown platform, so write anyway".

    That reported ``changed=True`` on every timeout, manufacturing corrections that
    never happened and driving the "something is fighting us" warning purely from a
    sleeping keyboard.
    """
    device = keyboard(transport)
    device.probe()  # warm the caches while the device is still awake
    fake = receiver.devices[fakehid.MX_KEYS_INDEX]
    fake.set_calls.clear()
    fake.asleep = True

    with pytest.raises(p.HidppTimeout):
        device.ensure_os("mac")
    assert fake.set_calls == [], "a device that will not answer must not be written to"


def test_an_error_reply_also_propagates(receiver, transport):
    device = keyboard(transport)
    device.probe()
    fake = receiver.devices[fakehid.MX_KEYS_INDEX]
    fake.set_calls.clear()
    # Answer getHostPlatform with an error rather than a reading.
    monkey = receiver._multiplatform

    def erroring(frame, dev, function, params):
        if function == p.MP_GET_HOST_PLATFORM:
            return receiver._error20(frame, 8)  # busy
        return monkey(frame, dev, function, params)

    receiver._multiplatform = erroring  # type: ignore[method-assign]
    with pytest.raises(p.HidppError):
        device.ensure_os("mac")
    assert fake.set_calls == []


# -- the write is checked ------------------------------------------------------


def test_a_write_that_takes_is_confirmed(receiver, transport):
    device = keyboard(transport)
    receiver.devices[fakehid.MX_KEYS_INDEX].platform = 0
    result = device.ensure_os("mac")
    assert result.changed and result.option.index == 1
    assert result.confirmed is True
    assert device.verify_platform(1) is True


def test_a_write_that_does_not_take_is_reported(receiver, transport, caplog):
    """A device can accept setHostPlatform and still not change mode."""
    device = keyboard(transport)
    fake = receiver.devices[fakehid.MX_KEYS_INDEX]
    fake.platform = 0
    real_multiplatform = receiver._multiplatform

    def ignore_the_write(frame, dev, function, params):
        if function == p.MP_SET_HOST_PLATFORM:
            dev.set_calls.append(params[1])
            return receiver._pad(frame, b"")  # acknowledged, but nothing changes
        return real_multiplatform(frame, dev, function, params)

    receiver._multiplatform = ignore_the_write  # type: ignore[method-assign]
    with caplog.at_level("WARNING", logger="logiswitch.hidpp.device"):
        result = device.ensure_os("mac")
    assert result.changed
    assert result.confirmed is False, "a contradicted write must not read as success"
    assert "did not take" in caplog.text
    assert trace.HEALTH.get("platform_mismatches") == 1


def test_an_unconfirmable_write_is_not_treated_as_a_failure(receiver, transport):
    """A device re-establishing its link answers nothing; that is not a failure."""
    device = keyboard(transport)
    device.probe()
    receiver.devices[fakehid.MX_KEYS_INDEX].asleep = True
    assert device.verify_platform(1) is None
    assert trace.HEALTH.get("platform_mismatches") == 0


# -- the platform table is not taken on trust ---------------------------------


def test_an_ambiguous_platform_table_is_flagged(monkeypatch, caplog):
    """A single platform claiming both macOS and Windows cannot give both layouts."""
    monkeypatch.setattr(fakehid, "PLATFORM_TABLE", [(0, 0x2100), (1, 0x4000)])
    receiver = fakehid.install(monkeypatch, fakehid.FakeReceiver([fakehid.mx_keys_s()]))
    from logiswitch import hidpp

    transport = hidpp.open_transport(hidpp.find_groups()[0])
    try:
        with caplog.at_level("WARNING", logger="logiswitch.hidpp.device"):
            HidppDevice(transport, fakehid.MX_KEYS_INDEX).probe()
        assert "claims both macOS and windows" in caplog.text
    finally:
        transport.close()
    assert receiver.handles == []


def test_the_platform_table_is_recorded_in_the_trace(transport):
    keyboard(transport).probe()
    rendered = trace.render()
    assert "mask=0x2000 (macos)" in rendered


# -- who changed the platform --------------------------------------------------


def test_a_changed_host_record_is_logged_once(receiver, transport, caplog):
    device = keyboard(transport)
    with caplog.at_level("INFO", logger="logiswitch.hidpp.device"):
        device.current_platform()  # first reading: nothing to compare against
        assert "platform record changed" not in caplog.text
        receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1
        device.current_platform()
        assert "platform record changed" in caplog.text
        caplog.clear()
        device.current_platform()  # unchanged, so silent
        assert "platform record changed" not in caplog.text


# -- the MX Keys S "current host" firmware bug --------------------------------


def _open(monkeypatch, *devices):
    from logiswitch import hidpp

    fakehid.install(monkeypatch, fakehid.FakeReceiver(list(devices)))
    return hidpp.open_transport(hidpp.find_groups()[0])


def test_the_real_host_index_is_resolved_not_assumed(monkeypatch):
    """Solaar: "can't just use the first byte = 0xFF ... bug in the MX Keys S"."""
    keys = fakehid.mx_keys_s()
    keys.current_host = 1
    transport = _open(monkeypatch, keys)
    try:
        assert HidppDevice(transport, fakehid.MX_KEYS_INDEX).current_host() == 1
    finally:
        transport.close()


def test_a_write_addressed_to_0xff_is_silently_dropped_by_this_firmware(monkeypatch):
    """Pin the fault itself, so the fix below is demonstrably fixing something."""
    keys = fakehid.mx_keys_s(buggy_current_host=True)
    keys.platform = 0
    transport = _open(monkeypatch, keys)
    try:
        device = HidppDevice(transport, fakehid.MX_KEYS_INDEX)
        device.probe()
        # Address 0xFF the way the code used to, and watch it do nothing.
        fi = device.feature_index(p.FEATURE_MULTIPLATFORM)
        transport.request(device.index, fi, p.MP_SET_HOST_PLATFORM, bytes([p.HOST_CURRENT, 1]))
        assert keys.platform == 0, "the firmware acknowledged the write and dropped it"
    finally:
        transport.close()


def test_ensure_os_sticks_on_firmware_that_mishandles_0xff(monkeypatch):
    """The regression test for the live fault: one write, and it holds.

    Before resolving the host index this looped forever -- write, read back wrong,
    write again -- which is exactly the twelve-second cycle seen on real hardware.
    """
    keys = fakehid.mx_keys_s(buggy_current_host=True)
    keys.platform = 0
    transport = _open(monkeypatch, keys)
    try:
        device = HidppDevice(transport, fakehid.MX_KEYS_INDEX)
        result = device.ensure_os("mac")
        assert result.changed
        assert result.confirmed is True, "the write must actually take"
        assert keys.platform == 1
        assert keys.set_hosts == [0], "must address the concrete host, never 0xFF"

        # ... and a second pass finds nothing to do, instead of correcting forever.
        again = device.ensure_os("mac")
        assert not again.changed
        assert keys.set_calls == [1], "exactly one write, not one per pass"
    finally:
        transport.close()


def test_firmware_without_0x1815_still_uses_0xff(monkeypatch):
    """Older keyboards have no HOSTS_INFO and must keep working exactly as before."""
    keys = fakehid.keyboard_without_hosts_info()
    keys.platform = 0
    transport = _open(monkeypatch, keys)
    try:
        device = HidppDevice(transport, fakehid.MX_KEYS_INDEX)
        assert device.current_host() == p.HOST_CURRENT
        assert device.ensure_os("mac").changed
        assert keys.set_hosts == [p.HOST_CURRENT]
        assert keys.platform == 1
    finally:
        transport.close()


def test_the_host_index_is_resolved_once_per_session(monkeypatch):
    keys = fakehid.mx_keys_s()
    receiver = fakehid.install(monkeypatch, fakehid.FakeReceiver([keys]))
    from logiswitch import hidpp

    transport = hidpp.open_transport(hidpp.find_groups()[0])
    try:
        device = HidppDevice(transport, fakehid.MX_KEYS_INDEX)
        assert device.current_host() == fakehid.CURRENT_HOST
        writes = receiver.writes
        for _ in range(5):
            device.current_host()
        assert receiver.writes == writes, "a resolved host index must not be re-queried"
    finally:
        transport.close()


def test_a_device_that_will_not_answer_0x1815_does_not_write_to_0xff(monkeypatch):
    """A timeout resolving the host must not fall back to 0xFF at all.

    Earlier this returned 0xFF without caching it -- "not pinned to the fallback for
    the rest of the session". That is too weak for the MX Keys S, whose firmware
    acknowledges a write to 0xFF and then silently drops it: the write looks exactly
    like another program reverting the setting, arms the close re-checker, and on a
    receiver shared with Logi Options+ each such write is a collision. A timeout is now
    allowed to propagate and fail the pass, so nothing is written until the host can be
    resolved again.
    """
    keys = fakehid.mx_keys_s()
    transport = _open(monkeypatch, keys)
    try:
        device = HidppDevice(transport, fakehid.MX_KEYS_INDEX)
        keys.asleep = True
        with pytest.raises(p.HidppTimeout):
            device.current_host()
        keys.asleep = False
        assert device.current_host() == fakehid.CURRENT_HOST, "must retry once reachable"
    finally:
        transport.close()
