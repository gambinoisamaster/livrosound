"""The TTS engine LivroSound reads with: Pocket-TTS.

There is one instance for the whole app: Choose a Voice previews and audio
generation share the loaded model (and its lock) instead of loading a second copy.
The TTSEngine interface stays so another local engine can be added later.
"""

from __future__ import annotations

import functools

from .base import TTSEngine
from .pocket import PocketTTSEngine

__all__ = ["PocketTTSEngine", "TTSEngine", "engine_named", "load_engines"]


@functools.cache
def _engines() -> tuple[TTSEngine, ...]:
    return (PocketTTSEngine(),)


def load_engines() -> list[TTSEngine]:
    """Every engine, available or not (the UI says why one can't be used)."""
    return list(_engines())


def engine_named(name: str) -> TTSEngine | None:
    return next((e for e in _engines() if e.name == name), None)
