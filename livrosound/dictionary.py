"""Look Up: a word's definition, offline, from the dictionaries macOS ships.

macOS's Dictionary app comes with the New Oxford American Dictionary (and
whatever else the user turned on in its Settings); DictionaryServices'
DCSCopyTextDefinition returns the first active dictionary's entry as plain
text. It is reached through ctypes, so no extra package is needed. Merriam-
Webster has no offline edition on the Mac: LivroSound links to its website
instead (merriam_webster_url), which is the only part of Look Up that uses
the internet, and only when the user clicks it.

Off macOS (and in tests) define() returns None and the popover says so.
"""

from __future__ import annotations

import ctypes
import sys
from urllib.parse import quote

_CF_UTF8 = 0x08000100  # kCFStringEncodingUTF8


class _CFRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


_libs: tuple | None = None


def _frameworks():
    global _libs
    if _libs is None:
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cs = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cs.DCSCopyTextDefinition.restype = ctypes.c_void_p
        cs.DCSCopyTextDefinition.argtypes = [ctypes.c_void_p, ctypes.c_void_p, _CFRange]
        _libs = (cf, cs)
    return _libs


def available() -> bool:
    return sys.platform == "darwin"


def define(word: str) -> str | None:
    """The definition of word (or a short phrase) from the Mac's dictionary, or None."""
    word = " ".join(word.split()).strip(" .,;:!?\"'“”‘’()[]")
    if not word or not available():
        return None
    try:
        cf, cs = _frameworks()
    except OSError:
        return None
    data = word.encode("utf-8")
    text_ref = cf.CFStringCreateWithCString(None, data, _CF_UTF8)
    if not text_ref:
        return None
    try:
        length = cf.CFStringGetLength(text_ref)
        result = cs.DCSCopyTextDefinition(None, text_ref, _CFRange(0, length))
        if not result:
            return None
        try:
            size = cf.CFStringGetLength(result) * 4 + 1
            buffer = ctypes.create_string_buffer(size)
            if not cf.CFStringGetCString(result, buffer, size, _CF_UTF8):
                return None
            return buffer.value.decode("utf-8", errors="replace")
        finally:
            cf.CFRelease(result)
    finally:
        cf.CFRelease(text_ref)


def dictionary_app_url(word: str) -> str:
    """Opens the word in macOS's Dictionary app (all its dictionaries)."""
    return "dict://" + quote(word.strip())


def merriam_webster_url(word: str) -> str:
    return "https://www.merriam-webster.com/dictionary/" + quote(word.strip().lower())


def oxford_url(word: str) -> str:
    """Oxford's free online dictionary (the Mac's offline one is the New Oxford American)."""
    return "https://www.oxfordlearnersdictionaries.com/definition/english/" + quote(word.strip().lower().replace(" ", "-"))
