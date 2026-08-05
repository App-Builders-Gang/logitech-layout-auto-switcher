"""Shared COM GUID ``ctypes`` struct.

Both the WinRT toast backend (``_wintoast``) and the cfgmgr32 device watcher
need a GUID laid out the way COM expects -- ``Data1``-``Data2``-``Data3``-
``Data4``. Defining it once keeps the two from drifting apart: a one-byte
difference in this struct is exactly the kind of bug that makes a COM call fail
with ``E_NOINTERFACE`` and no further clue, so there is one source of truth.

Uses only :mod:`ctypes`, so it imports cleanly on any platform.
"""

from __future__ import annotations

import ctypes


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(literal: str) -> _GUID:
    """Parse a ``D1-D2-D3-D4-D5`` string into the struct COM expects."""
    parts = literal.split("-")
    rest = parts[3] + parts[4]  # 16 hex chars = the 8 bytes of Data4
    data4 = bytes(int(rest[i : i + 2], 16) for i in range(0, 16, 2))
    return _GUID(
        int(parts[0], 16),
        int(parts[1], 16),
        int(parts[2], 16),
        (ctypes.c_ubyte * 8)(*data4),
    )
