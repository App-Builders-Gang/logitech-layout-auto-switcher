"""macOS device notifications via IOKit service matching.

Uses ``IOServiceAddMatchingNotification`` on ``IOHIDDevice`` filtered to the
vendor id, driven by a ``CFRunLoop`` on a dedicated thread.

Deliberately *not* IOHIDManager: opening a HID manager that matches a keyboard
can trigger the Input Monitoring permission prompt on macOS 10.15+. Service
matching is pure registry discovery -- it never opens a device, so it needs no
privacy permission at all.

The run loop lives on its own thread so the main thread stays free to block on a
stop event and remain interruptible by SIGINT/SIGTERM (Python cannot run signal
handlers while the main thread is inside a blocking C call such as CFRunLoopRun).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading

from .base import DeviceEvent, WatcherCallback

log = logging.getLogger(__name__)

KERN_SUCCESS = 0
kCFStringEncodingUTF8 = 0x08000100
kCFNumberSInt32Type = 3
kIOMatchedNotification = b"IOServiceMatched"
#: IOKitKeys.h spells this "IOServiceTerminate" -- no trailing "d". Anything else makes
#: IOServiceAddMatchingNotification return kIOReturnUnsupported (0xE00002C7).
kIOTerminatedNotification = b"IOServiceTerminate"


def _load(framework: str, path: str) -> ctypes.CDLL:
    try:
        return ctypes.cdll.LoadLibrary(path)
    except OSError:
        found = ctypes.util.find_library(framework)
        if not found:
            raise
        return ctypes.cdll.LoadLibrary(found)


class _Frameworks:
    """Lazily bound CoreFoundation + IOKit entry points.

    Every pointer-returning function gets an explicit ``restype``; without it
    ctypes truncates 64-bit pointers to int and the whole thing crashes subtly.
    """

    def __init__(self) -> None:
        self.cf = _load(
            "CoreFoundation", "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self.iokit = _load("IOKit", "/System/Library/Frameworks/IOKit.framework/IOKit")

        cf, io = self.cf, self.iokit

        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFNumberCreate.restype = ctypes.c_void_p
        cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cf.CFDictionarySetValue.restype = None
        cf.CFDictionarySetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopGetCurrent.argtypes = []
        cf.CFRunLoopRun.restype = None
        cf.CFRunLoopRun.argtypes = []
        cf.CFRunLoopStop.restype = None
        cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]
        cf.CFRunLoopAddSource.restype = None
        cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRunLoopRemoveSource.restype = None
        cf.CFRunLoopRemoveSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

        io.IONotificationPortCreate.restype = ctypes.c_void_p
        io.IONotificationPortCreate.argtypes = [ctypes.c_uint32]
        io.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
        io.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
        io.IONotificationPortDestroy.restype = None
        io.IONotificationPortDestroy.argtypes = [ctypes.c_void_p]
        io.IOServiceMatching.restype = ctypes.c_void_p
        io.IOServiceMatching.argtypes = [ctypes.c_char_p]
        io.IOServiceAddMatchingNotification.restype = ctypes.c_int
        io.IOServiceAddMatchingNotification.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        io.IOIteratorNext.restype = ctypes.c_uint32
        io.IOIteratorNext.argtypes = [ctypes.c_uint32]
        io.IOObjectRelease.restype = ctypes.c_int
        io.IOObjectRelease.argtypes = [ctypes.c_uint32]
        io.IORegistryEntryGetName.restype = ctypes.c_int
        io.IORegistryEntryGetName.argtypes = [ctypes.c_uint32, ctypes.c_char_p]

        self.run_loop_default_mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")

    def cfstr(self, text: str) -> ctypes.c_void_p:
        return ctypes.c_void_p(
            self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), kCFStringEncodingUTF8)
        )

    def cfnum(self, value: int) -> ctypes.c_void_p:
        raw = ctypes.c_int32(value)
        return ctypes.c_void_p(
            self.cf.CFNumberCreate(None, kCFNumberSInt32Type, ctypes.byref(raw))
        )


_IOServiceMatchingCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32)


class DarwinWatcher:
    name = "iokit"

    def __init__(self, vendor_id: int):
        self._vendor_id = vendor_id
        self._fw = _Frameworks()
        self._callback: WatcherCallback | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._run_loop: int | None = None
        self._port: int | None = None
        self._iterators: list[ctypes.c_uint32] = []
        self._source: int | None = None
        self._primed: set[int] = set()

        # Pinned for the lifetime of the registration: IOKit holds raw pointers
        # to these trampolines. Letting Python collect them would crash the
        # process on the next device event.
        self._native_matched = _IOServiceMatchingCallback(self._on_matched)
        self._native_terminated = _IOServiceMatchingCallback(self._on_terminated)

    # -- Watcher protocol -----------------------------------------------------

    def start(self, callback: WatcherCallback) -> None:
        self._callback = callback
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="logiswitch-iokit", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            raise OSError("IOKit notification thread did not start")
        if self._error is not None:
            raise self._error
        log.debug("subscribed to IOHIDDevice service notifications (IOKit)")

    def stop(self) -> None:
        self._callback = None
        cf = self._fw.cf
        if self._run_loop:
            cf.CFRunLoopStop(ctypes.c_void_p(self._run_loop))
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(3.0)
            if thread.is_alive():  # pragma: no cover
                log.warning("IOKit watcher thread did not exit")

    # -- run loop thread ------------------------------------------------------

    def _matching_dict(self) -> ctypes.c_void_p:
        """A fresh IOHIDDevice matching dict filtered to our vendor.

        IOServiceAddMatchingNotification consumes one reference on the dict, so
        each registration needs its own.
        """
        fw = self._fw
        matching = ctypes.c_void_p(fw.iokit.IOServiceMatching(b"IOHIDDevice"))
        if not matching.value:
            raise OSError("IOServiceMatching(IOHIDDevice) returned NULL")
        key = fw.cfstr("VendorID")
        value = fw.cfnum(self._vendor_id)
        fw.cf.CFDictionarySetValue(matching, key, value)
        fw.cf.CFRelease(key)
        fw.cf.CFRelease(value)
        return matching

    def _run(self) -> None:
        fw = self._fw
        try:
            self._run_loop = fw.cf.CFRunLoopGetCurrent()
            self._port = fw.iokit.IONotificationPortCreate(0)  # kIOMainPortDefault
            if not self._port:
                raise OSError("IONotificationPortCreate failed")
            self._source = fw.iokit.IONotificationPortGetRunLoopSource(ctypes.c_void_p(self._port))
            fw.cf.CFRunLoopAddSource(
                ctypes.c_void_p(self._run_loop),
                ctypes.c_void_p(self._source),
                fw.run_loop_default_mode,
            )

            for notification_type, trampoline in (
                (kIOMatchedNotification, self._native_matched),
                (kIOTerminatedNotification, self._native_terminated),
            ):
                iterator = ctypes.c_uint32(0)
                matching = self._matching_dict()
                result = fw.iokit.IOServiceAddMatchingNotification(
                    ctypes.c_void_p(self._port),
                    notification_type,
                    matching,
                    ctypes.cast(trampoline, ctypes.c_void_p),
                    None,
                    ctypes.byref(iterator),
                )
                if result != KERN_SUCCESS:
                    # The reference is only consumed on success.
                    fw.cf.CFRelease(matching)
                    raise OSError(
                        f"IOServiceAddMatchingNotification({notification_type.decode()}) "
                        f"failed: 0x{result & 0xFFFFFFFF:08X}"
                    )
                self._iterators.append(iterator)
                # The iterator MUST be drained once to arm the notification. This
                # first pass reports devices that already exist, which the agent
                # already handles via its start-up assert, so it is swallowed.
                self._primed.add(iterator.value)
                self._drain(iterator.value)
        except Exception as exc:
            self._error = exc
            self._ready.set()
            return

        self._ready.set()
        try:
            fw.cf.CFRunLoopRun()
        finally:
            self._teardown()

    def _teardown(self) -> None:
        fw = self._fw
        try:
            if self._run_loop and self._source:
                fw.cf.CFRunLoopRemoveSource(
                    ctypes.c_void_p(self._run_loop),
                    ctypes.c_void_p(self._source),
                    fw.run_loop_default_mode,
                )
            for iterator in self._iterators:
                fw.iokit.IOObjectRelease(iterator.value)
            self._iterators.clear()
            if self._port:
                fw.iokit.IONotificationPortDestroy(ctypes.c_void_p(self._port))
        except Exception:  # pragma: no cover - shutdown best effort
            log.debug("IOKit teardown raised", exc_info=True)
        finally:
            self._port = None
            self._source = None
            self._run_loop = None

    # -- callbacks ------------------------------------------------------------

    def _drain(self, iterator: int) -> list[str]:
        """Consume the iterator. Leaving entries in it stops future notifications."""
        names = []
        io = self._fw.iokit
        while True:
            entry = io.IOIteratorNext(iterator)
            if not entry:
                break
            buffer = ctypes.create_string_buffer(128)
            if io.IORegistryEntryGetName(entry, buffer) == KERN_SUCCESS:
                names.append(buffer.value.decode("utf-8", "replace"))
            io.IOObjectRelease(entry)
        return names

    def _emit(self, iterator: int, event: DeviceEvent) -> None:
        try:
            names = self._drain(iterator)
            if iterator in self._primed:
                self._primed.discard(iterator)
                return
            if not names:
                return
            callback = self._callback
            if callback is not None:
                callback(event, ", ".join(names))
        except Exception:  # never unwind into IOKit
            log.exception("IOKit notification handler failed")

    def _on_matched(self, _refcon: int, iterator: int) -> None:
        self._emit(iterator, DeviceEvent.ARRIVED)

    def _on_terminated(self, _refcon: int, iterator: int) -> None:
        self._emit(iterator, DeviceEvent.REMOVED)
