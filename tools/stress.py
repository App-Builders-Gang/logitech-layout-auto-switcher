"""Leak check: hammer the open/close cycle and watch threads, handles and RSS.

python tools/stress.py            # against real hardware, 20 cycles
python tools/stress.py 200 fake   # against the fake receiver, 200 cycles
"""

from __future__ import annotations

import gc
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from logiswitch import hidpp  # noqa: E402


def rss_mb() -> float:
    try:
        import ctypes
        from ctypes import wintypes

        class COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(COUNTERS),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        counters = COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), ctypes.sizeof(counters)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WorkingSetSize / 1_048_576
    except Exception:
        try:
            import resource

            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1_048_576
        except Exception:
            return float("nan")


def main() -> int:
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    use_fake = len(sys.argv) > 2 and sys.argv[2] == "fake"

    if use_fake:
        import fakehid

        class _Patch:
            def setattr(self, obj, name, value):
                setattr(obj, name, value)

        fakehid.install(
            _Patch(), fakehid.FakeReceiver([fakehid.mx_master_3s(), fakehid.mx_keys_s()])
        )

    groups = hidpp.find_groups()
    if not groups:
        print("no Logitech HID++ endpoint present")
        return 1

    baseline_threads = threading.active_count()
    baseline_rss = rss_mb()
    print(f"baseline: threads={baseline_threads} rss={baseline_rss:.1f} MB")

    started = time.monotonic()
    for cycle in range(1, cycles + 1):
        transport = hidpp.open_transport(groups[0])
        try:
            devices = hidpp.probe_devices(hidpp.discover_devices(transport, hint=5))
            for device, info in devices:
                if info.supported:
                    device.ensure_os("windows")
        finally:
            transport.close()
        if cycle % max(1, cycles // 10) == 0:
            gc.collect()
            print(f"  cycle {cycle:4d}: threads={threading.active_count()} rss={rss_mb():.1f} MB")

    gc.collect()
    time.sleep(0.5)
    threads = threading.active_count()
    rss = rss_mb()
    elapsed = time.monotonic() - started
    print(f"\n{cycles} cycles in {elapsed:.1f}s")
    print(f"threads: {baseline_threads} -> {threads}")
    print(f"rss:     {baseline_rss:.1f} MB -> {rss:.1f} MB (delta {rss - baseline_rss:+.1f} MB)")

    leaked = [t.name for t in threading.enumerate() if "hidpp-reader" in t.name]
    if leaked:
        print(f"LEAKED reader threads: {leaked}")
        return 1
    if threads != baseline_threads:
        print("FAIL: thread count changed")
        return 1
    print("OK: no leaked threads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
