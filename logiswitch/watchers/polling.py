"""Deprecation shim; import from :mod:`logiswitch.platform.watchers.polling` instead."""

from __future__ import annotations

import warnings

from ..platform.watchers.polling import (  # noqa: F401
    DEFAULT_INTERVAL,
    PollingWatcher,
)

__all__ = ["DEFAULT_INTERVAL", "PollingWatcher"]

warnings.warn(
    "logiswitch.watchers.polling has moved to "
    "logiswitch.platform.watchers.polling; import from the new location instead.",
    DeprecationWarning,
    stacklevel=2,
)
