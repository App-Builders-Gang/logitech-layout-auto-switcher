"""Deprecation shim; import from :mod:`logiswitch.platform.watchers.darwin` instead."""

from __future__ import annotations

import warnings

from ..platform.watchers.darwin import DarwinWatcher  # noqa: F401

__all__ = ["DarwinWatcher"]

warnings.warn(
    "logiswitch.watchers.darwin has moved to "
    "logiswitch.platform.watchers.darwin; import from the new location instead.",
    DeprecationWarning,
    stacklevel=2,
)
