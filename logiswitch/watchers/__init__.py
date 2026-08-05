"""Deprecation shim; import from :mod:`logiswitch.platform.watchers` instead."""

from __future__ import annotations

import warnings

from ..platform.watchers import (  # noqa: F401
    DeviceEvent,
    Watcher,
    WatcherCallback,
    create_watcher,
)

__all__ = ["DeviceEvent", "Watcher", "WatcherCallback", "create_watcher"]

warnings.warn(
    "logiswitch.watchers has moved to logiswitch.platform.watchers; "
    "import from the new location instead.",
    DeprecationWarning,
    stacklevel=2,
)
