import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import fakehid  # noqa: E402


@pytest.fixture
def receiver(monkeypatch):
    """A Bolt receiver with an MX Master 3S and an MX Keys S, as on real hardware."""
    return fakehid.install(
        monkeypatch, fakehid.FakeReceiver([fakehid.mx_master_3s(), fakehid.mx_keys_s()])
    )


@pytest.fixture
def transport(receiver):
    from logiswitch import hidpp

    groups = hidpp.find_groups()
    assert len(groups) == 1
    transport = hidpp.open_transport(groups[0])
    yield transport
    transport.close()
