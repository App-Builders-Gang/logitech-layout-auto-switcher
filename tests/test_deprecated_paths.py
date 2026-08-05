"""The pre-refactor import paths still work, but warn at import time.

These shims exist only for external/migration callers; the package itself (and
its own tests) import from ``logiswitch.platform.*`` directly. Each shim fires a
``DeprecationWarning`` naming the new location the first time it is imported.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def test_paths_shim_imports_and_warns() -> None:
    with pytest.warns(DeprecationWarning, match=r"logiswitch\.paths has moved"):
        module = importlib.import_module("logiswitch.paths")
    assert callable(module.is_windows)


def test_watchers_shim_imports_and_warns() -> None:
    with pytest.warns(DeprecationWarning, match=r"logiswitch\.watchers has moved"):
        module = importlib.import_module("logiswitch.watchers")
    assert callable(module.create_watcher)
    for name in ("Watcher", "DeviceEvent", "WatcherCallback"):
        assert hasattr(module, name)


@pytest.mark.skipif(sys.platform != "win32", reason="WindowsWatcher needs ctypes.WINFUNCTYPE")
def test_watchers_windows_shim_imports_and_warns() -> None:
    with pytest.warns(DeprecationWarning, match=r"logiswitch\.watchers\.windows has moved"):
        module = importlib.import_module("logiswitch.watchers.windows")
    assert hasattr(module, "WindowsWatcher")
