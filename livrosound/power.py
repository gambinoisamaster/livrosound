"""Keep the Mac working at full speed while audio is generated.

macOS's App Nap slows an app whose window is hidden or behind others (timers
throttled, the process at a lower priority) and lets an idle Mac go to sleep;
a book takes hours to generate, and the user may well switch to other work
meanwhile. NSProcessInfo.beginActivityWithOptions:reason: with
NSActivityUserInitiated opts out of both for as long as the activity is held.
Reached through the Objective-C runtime with ctypes (no extra package).
Elsewhere (and in tests) it does nothing.
"""

from __future__ import annotations

import ctypes
import sys

NS_ACTIVITY_USER_INITIATED = 0x00FFFFFF | (1 << 20)  # | NSActivityIdleSystemSleepDisabled


class _Objc:
    def __init__(self):
        lib = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        lib.objc_getClass.restype = ctypes.c_void_p
        lib.objc_getClass.argtypes = [ctypes.c_char_p]
        lib.sel_registerName.restype = ctypes.c_void_p
        lib.sel_registerName.argtypes = [ctypes.c_char_p]
        self.lib = lib

    def send(self, receiver, selector: bytes, *args, argtypes=(), restype=ctypes.c_void_p):
        """objc_msgSend with this call's own prototype (arm64 needs the exact one)."""
        fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(
            ctypes.cast(self.lib.objc_msgSend, ctypes.c_void_p).value
        )
        return fn(receiver, self.lib.sel_registerName(selector), *args)

    def cls(self, name: bytes):
        return self.lib.objc_getClass(name)


class KeepAwake:
    """While held: no App Nap, no idle sleep. hold() and release() may be called any number of times."""

    def __init__(self, reason: str = "Generating audio"):
        self._reason = reason
        self._token = None
        self._objc: _Objc | None = None

    def held(self) -> bool:
        return self._token is not None

    def hold(self) -> None:
        if self._token is not None or sys.platform != "darwin":
            return
        try:
            objc = self._objc = self._objc or _Objc()
            info = objc.send(objc.cls(b"NSProcessInfo"), b"processInfo")
            reason = objc.send(objc.cls(b"NSString"), b"stringWithUTF8String:", self._reason.encode(),
                               argtypes=(ctypes.c_char_p,))
            token = objc.send(info, b"beginActivityWithOptions:reason:", NS_ACTIVITY_USER_INITIATED, reason,
                              argtypes=(ctypes.c_uint64, ctypes.c_void_p))
            self._token = objc.send(token, b"retain")
        except (OSError, AttributeError, ValueError):
            self._token = None

    def release(self) -> None:
        token, self._token = self._token, None
        if token is None or self._objc is None:
            return
        objc = self._objc
        info = objc.send(objc.cls(b"NSProcessInfo"), b"processInfo")
        objc.send(info, b"endActivity:", token, argtypes=(ctypes.c_void_p,), restype=None)
        objc.send(token, b"release", restype=None)
