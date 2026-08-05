"""Deprecation shim; import from :mod:`logiswitch.platform.paths` instead."""

from __future__ import annotations

import warnings

from .platform.paths import (  # noqa: F401
    APP_NAME,
    LEGACY_TASK_NAME,
    MANAGED_ENV_VAR,
    SERVICE_LABEL,
    data_dir,
    default_target_os,
    doctor_report_path,
    is_macos,
    is_managed,
    is_windows,
    launchd_stdio_path,
    log_path,
    python_executable,
    setup_logging,
    state_path,
    trace_path,
)

__all__ = [
    "APP_NAME",
    "LEGACY_TASK_NAME",
    "MANAGED_ENV_VAR",
    "SERVICE_LABEL",
    "data_dir",
    "default_target_os",
    "doctor_report_path",
    "is_macos",
    "is_managed",
    "is_windows",
    "launchd_stdio_path",
    "log_path",
    "python_executable",
    "setup_logging",
    "state_path",
    "trace_path",
]

warnings.warn(
    "logiswitch.paths has moved to logiswitch.platform.paths; "
    "import from the new location instead.",
    DeprecationWarning,
    stacklevel=2,
)
