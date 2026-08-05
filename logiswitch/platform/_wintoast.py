"""Windows toast notifications through the WinRT COM API, via raw ``ctypes``.

No new dependency, and no process to spawn: the toast is raised by calling
``Windows.UI.Notifications`` through its own COM vtables -- the same call path
PowerShell's ``-Command`` script was making for us, only without launching
PowerShell to make it. The title and body go into toast XML built here, so there
is no command line for an untrusted device name to escape into, and nothing for a
shell to be asked to parse.

Every interface GUID and vtable offset below is copied verbatim from the Windows
SDK IDL (``winrt/windows.*.idl``), not from memory. A GUID that is off by one
digit makes ``RoGetActivationFactory`` return ``E_NOINTERFACE`` and the toast
fails with no visible reason -- exactly how the PowerShell version once shipped
broken while its tests, which only inspected the script text, passed.

Any failure raises :class:`OSError`; :mod:`logiswitch.notify` swallows that, on
the principle that a notification able to take the agent down is worse than none.

Requires Windows 8 or newer.
"""

from __future__ import annotations

import ctypes
import threading
import xml.sax.saxutils
from typing import Any

from ._comtypes import _GUID, _guid

# Interface GUIDs, copied from the Windows SDK IDL (see module docstring).
_IID_IXML_DOCUMENT_IO = _guid("6CD0E74E-EE65-4489-9EBF-CA43E87BA637")
_IID_ITOAST_NOTIFICATION_MANAGER_STATICS = _guid("50AC103F-D235-4598-BBEF-98FE4D1A3AD4")
_IID_ITOAST_NOTIFICATION_FACTORY = _guid("04124B20-82C6-4229-B109-FD9ED4662B53")

#: Windows will not display a toast with no Application User Model ID. Of the ids
#: certain to exist on every machine, PowerShell's own is the safest, so the
#: toast is attributed to it -- the same identity the previous PowerShell-based
#: notifier relied on, now reached directly.
_APP_USER_MODEL_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# ToastTemplateType.ToastText01: any template does, the content is replaced by
# LoadXml; this one is the cheapest.
_TEMPLATE_TOAST_TEXT_01 = 0

# ``combase`` is loaded lazily by _load_com_once, so importing this module on a
# non-Windows host -- which the tests do, to exercise _toast_xml and the AUMID --
# never touches a DLL that only exists on Windows.
_com: Any = None

_PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)

# WinRT interfaces derive from IInspectable (3 IUnknown + 3 IInspectable slots),
# so the first method declared on each interface is vtable slot 6.
#
# WINFUNCTYPE exists only on Windows; the prototypes are never invoked anywhere
# else (there is no COM to call), so fall back to CFUNCTYPE and let the module be
# imported on any platform.
_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_HRESULT = ctypes.c_long
_OUT_PTR = ctypes.POINTER(ctypes.c_void_p)
_GUID_PTR = ctypes.POINTER(_GUID)

_QUERY_INTERFACE = _WINFUNCTYPE(_HRESULT, ctypes.c_void_p, _GUID_PTR, _OUT_PTR)
_RELEASE = _WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_GET_TEMPLATE_CONTENT = _WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.c_uint, _OUT_PTR)
_CREATE_TOAST_NOTIFIER_WITH_ID = _WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.c_void_p, _OUT_PTR)
_LOAD_XML = _WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.c_void_p)
_CREATE_TOAST_NOTIFICATION = _WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.c_void_p, _OUT_PTR)
_SHOW = _WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.c_void_p)

# ManagerStatics vtable: CreateToastNotifier=6, CreateToastNotifierWithId=7,
# GetTemplateContent=8. The id-less CreateToastNotifier (slot 6) only resolves
# the caller's AUMID for a packaged app, so the unpackaged agent must use slot 7.

# RoInitialize initialises COM for the *calling thread*, so the "have we done
# this?" flag must be per-thread too: the agent delivers notifications from a
# dedicated thread, and a flag shared across threads would let one thread skip
# initialisation another thread still needs.
_thread_state = threading.local()


def _load_com_once() -> None:
    """Load combase and pin its signatures, the first time COM is needed.

    Process-wide and idempotent: the DLL load is the part that fails on non-Windows
    hosts, so it is deferred here rather than run at import, and never runs unless
    a toast is actually being raised.
    """
    global _com
    if _com is not None:
        return
    lib = ctypes.WinDLL("combase")
    lib.WindowsCreateString.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.WindowsCreateString.restype = ctypes.c_long
    lib.WindowsDeleteString.argtypes = [ctypes.c_void_p]
    lib.WindowsDeleteString.restype = ctypes.c_long
    lib.RoInitialize.argtypes = [ctypes.c_int]
    lib.RoInitialize.restype = ctypes.c_long
    lib.RoGetActivationFactory.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.RoGetActivationFactory.restype = ctypes.c_long
    _com = lib


def _ensure_runtime_initialized() -> None:
    """Load combase and call RoInitialize for the calling thread. Re-entrant-safe."""
    _load_com_once()
    if getattr(_thread_state, "initialized", False):
        return
    # RO_INIT_SINGLETHREADED. S_FALSE (already initialised on this thread) is fine,
    # as is RPC_E_CHANGED_CONTEXT -- the thread already has an apartment.
    hr = _com.RoInitialize(1)
    if hr not in (0, 1, -2147417850):  # S_OK, S_FALSE, RPC_E_CHANGED_CONTEXT
        raise OSError(f"RoInitialize failed: 0x{hr & 0xFFFFFFFF:08X}")
    _thread_state.initialized = True


def _hstring(text: str) -> int:
    handle = ctypes.c_void_p()
    hr = _com.WindowsCreateString(text, len(text), ctypes.byref(handle))
    if hr:
        raise OSError(f"WindowsCreateString failed: 0x{hr & 0xFFFFFFFF:08X}")
    value = handle.value
    return value if value is not None else 0


def _delete_string(handle: int) -> None:
    if handle:
        _com.WindowsDeleteString(handle)


def _activation_factory(class_name: str, iid: _GUID) -> int:
    class_handle = _hstring(class_name)
    try:
        out = ctypes.c_void_p()
        hr = _com.RoGetActivationFactory(class_handle, ctypes.byref(iid), ctypes.byref(out))
        if hr:
            raise OSError(f"RoGetActivationFactory({class_name}) failed: 0x{hr & 0xFFFFFFFF:08X}")
        value = out.value
        return value if value is not None else 0
    finally:
        _delete_string(class_handle)


def _invoke(this: int, slot: int, function_type: Any, *args: Any) -> int:
    """Call ``slot`` on the COM object at address ``this``; return its HRESULT."""
    vtable = ctypes.c_void_p.from_address(this).value
    assert vtable is not None, "a live COM interface always has a vtable"
    function = ctypes.c_void_p.from_address(vtable + slot * _PTR_SIZE).value
    assert function is not None, "an occupied vtable slot is a real function pointer"
    return function_type(function)(this, *args)


def _query_interface(this: int, iid: _GUID) -> int:
    out = ctypes.c_void_p()
    hr = _invoke(this, 0, _QUERY_INTERFACE, ctypes.byref(iid), ctypes.byref(out))
    if hr:
        raise OSError(f"QueryInterface failed: 0x{hr & 0xFFFFFFFF:08X}")
    value = out.value
    return value if value is not None else 0


def _release(this: int) -> None:
    if this:
        _invoke(this, 2, _RELEASE)


def _toast_xml(title: str, body: str) -> str:
    """The toast payload, with ``title`` and ``body`` XML-escaped in place.

    Escaping happens here, on the Python side, because there is no command line
    any more for the text to travel down -- the previous design passed it through
    the environment precisely because a PowerShell command line could not be made
    safe. Either way the rule is the same: a device called ``He said "hi" & <smiled>``
    must reach the OS as text, not as markup.
    """
    escaped_title = xml.sax.saxutils.escape(title)
    escaped_body = xml.sax.saxutils.escape(body)
    return (
        '<toast><visual><binding template="ToastText02">'
        f'<text id="1">{escaped_title}</text>'
        f'<text id="2">{escaped_body}</text>'
        "</binding></visual></toast>"
    )


def show_toast(title: str, body: str) -> None:
    """Raise one Windows toast. Raises :class:`OSError` if any COM step fails.

    Every COM reference and HSTRING is released on the way out, even on failure,
    so a burst of notifications does not leak handles the way a per-call process
    would leak nothing-but-CPU.
    """
    _ensure_runtime_initialized()

    content = _toast_xml(title, body)

    manager = 0
    document = 0
    document_io = 0
    factory = 0
    notification = 0
    notifier = 0
    xml_handle = 0
    aumid_handle = 0
    try:
        manager = _activation_factory(
            "Windows.UI.Notifications.ToastNotificationManager",
            _IID_ITOAST_NOTIFICATION_MANAGER_STATICS,
        )

        out = ctypes.c_void_p()
        hr = _invoke(manager, 8, _GET_TEMPLATE_CONTENT, _TEMPLATE_TOAST_TEXT_01, ctypes.byref(out))
        if hr:
            raise OSError(f"GetTemplateContent failed: 0x{hr & 0xFFFFFFFF:08X}")
        document = out.value or 0

        document_io = _query_interface(document, _IID_IXML_DOCUMENT_IO)
        xml_handle = _hstring(content)
        hr = _invoke(document_io, 6, _LOAD_XML, xml_handle)
        if hr:
            raise OSError(f"LoadXml failed: 0x{hr & 0xFFFFFFFF:08X}")

        factory = _activation_factory(
            "Windows.UI.Notifications.ToastNotification", _IID_ITOAST_NOTIFICATION_FACTORY
        )
        out = ctypes.c_void_p()
        hr = _invoke(factory, 6, _CREATE_TOAST_NOTIFICATION, document, ctypes.byref(out))
        if hr:
            raise OSError(f"CreateToastNotification failed: 0x{hr & 0xFFFFFFFF:08X}")
        notification = out.value or 0

        aumid_handle = _hstring(_APP_USER_MODEL_ID)
        out = ctypes.c_void_p()
        hr = _invoke(manager, 7, _CREATE_TOAST_NOTIFIER_WITH_ID, aumid_handle, ctypes.byref(out))
        if hr:
            raise OSError(f"CreateToastNotifierWithId failed: 0x{hr & 0xFFFFFFFF:08X}")
        notifier = out.value or 0

        hr = _invoke(notifier, 6, _SHOW, notification)
        if hr:
            raise OSError(f"Show failed: 0x{hr & 0xFFFFFFFF:08X}")
    finally:
        for pointer in (notifier, notification, document_io, document, factory, manager):
            _release(pointer)
        _delete_string(aumid_handle)
        _delete_string(xml_handle)
