"""Deprecation shim; import from :mod:`logiswitch.platform.watchers.base` instead."""

from __future__ import annotations

import warnings

from ..platform.watchers.base import (  # noqa: F401
    DeviceEvent,
    Watcher,
    WatcherCallback,
    create_watcher,
)

__all__ = ["DeviceEvent", "Watcher", "WatcherCallback", "create_watcher"]

warnings.warn(
    "logiswitch.watchers.base has moved to logiswitch.platform.watchers.base; "
    "import from the new location instead.",
    DeprecationWarning,
    stacklevel=2,
)
