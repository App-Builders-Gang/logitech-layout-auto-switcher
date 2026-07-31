import fakehid

from logiswitch import hidpp
from logiswitch.hidpp import protocol as p


def test_short_and_long_collections_group_into_one_endpoint(receiver):
    groups = hidpp.find_groups()
    assert len(groups) == 1
    usages = {usage for usage, _ in groups[0].paths}
    assert usages == {p.USAGE_SHORT, p.USAGE_LONG}
    assert groups[0].label == "Logi Bolt receiver"


def test_a_single_collection_is_aliased_to_both_usages(monkeypatch):
    """macOS can expose one interface backing both report ids."""
    fakehid.install(
        monkeypatch, fakehid.FakeReceiver([fakehid.mx_keys_s()]), single_collection=True
    )
    group = hidpp.find_groups()[0]
    paths = dict(group.paths)
    assert paths[p.USAGE_SHORT] == paths[p.USAGE_LONG]


def test_unknown_receivers_still_get_a_usable_label(monkeypatch):
    fakehid.install(
        monkeypatch, fakehid.FakeReceiver([fakehid.mx_keys_s()], product_id=0xABCD)
    )
    assert hidpp.find_groups()[0].label == "USB Receiver"


def test_discovery_finds_both_devices(transport):
    devices = hidpp.discover_devices(transport)
    assert [d.index for d in devices] == [fakehid.MX_MASTER_INDEX, fakehid.MX_KEYS_INDEX]


def test_a_correct_hint_skips_the_scan(receiver, transport):
    before = receiver.writes
    devices = hidpp.discover_devices(transport, hint=fakehid.MX_KEYS_INDEX)
    assert [d.index for d in devices] == [fakehid.MX_KEYS_INDEX]
    assert receiver.writes - before == 1, "a hit should cost exactly one ping"


def test_a_stale_hint_falls_back_to_the_scan(transport):
    devices = hidpp.discover_devices(transport, hint=4)
    assert [d.index for d in devices] == [fakehid.MX_MASTER_INDEX, fakehid.MX_KEYS_INDEX]


def test_direct_connect_index_is_probed(monkeypatch):
    """Bluetooth and cable-attached devices answer on 0xFF, not a receiver slot."""
    fakehid.install(
        monkeypatch, fakehid.FakeReceiver([fakehid.mx_keys_s(index=p.INDEX_DIRECT)])
    )
    transport = hidpp.open_transport(hidpp.find_groups()[0])
    try:
        devices = hidpp.discover_devices(transport)
        assert [d.index for d in devices] == [p.INDEX_DIRECT]
        assert hidpp.probe_devices(devices)[0][1].supported
    finally:
        transport.close()


def test_probe_devices_pairs_each_device_with_its_capability(transport):
    probed = hidpp.probe_devices(hidpp.discover_devices(transport))
    supported = {info.name: info.supported for _, info in probed}
    assert supported == {"MX Master 3S": False, "MX Keys S": True}
