"""Deprecation shim; import from :mod:`logiswitch.platform.watchers.windows` instead."""

from __future__ import annotations

import warnings

from ..platform.watchers.windows import WindowsWatcher  # noqa: F401

__all__ = ["WindowsWatcher"]

warnings.warn(
    "logiswitch.watchers.windows has moved to "
    "logiswitch.platform.watchers.windows; import from the new location instead.",
    DeprecationWarning,
    stacklevel=2,
)
