"""Platform-specific code: per-platform locations, device watchers, native toast.

Co-locates everything that knows about an operating system -- where files live
(:mod:`logiswitch.platform.paths`), how device arrival is observed
(:mod:`logiswitch.platform.watchers`), and how a desktop notification is raised
(:mod:`logiswitch.platform._wintoast`). The rest of the package depends on the
abstractions re-exported here, never on a specific platform.
"""

from __future__ import annotations

from .paths import (
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
from .watchers import DeviceEvent, Watcher, WatcherCallback, create_watcher

__all__ = [
    "APP_NAME",
    "LEGACY_TASK_NAME",
    "MANAGED_ENV_VAR",
    "SERVICE_LABEL",
    "DeviceEvent",
    "Watcher",
    "WatcherCallback",
    "create_watcher",
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
