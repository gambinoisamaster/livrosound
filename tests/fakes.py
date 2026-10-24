"""A fake TTS engine: fast, deterministic "speech" for generation, player and UI tests.

Each word becomes a 120 ms tone burst followed by a 40 ms gap, so audio length
grows with the text the way real speech does, there are real pauses between
words, and every clip starts and ends on silence-free samples (like a TTS clip
that needs declicking).
"""

from __future__ import annotations

import numpy as np

from livrosound.engines.base import TTSEngine

FAKE_RATE = 24000


class FakeEngine(TTSEngine):
    name = "Fake"
    install_hint = ""

    def __init__(self, voices=("Aaron", "Ana")):
        self._voices = list(voices)
        self.calls: list[tuple[str, str]] = []

    def available(self):
        return True, ""

    def voices(self):
        return list(self._voices)

    def synthesize(self, voice, text):
        self.calls.append((voice, text))
        words = max(1, len(text.split()))
        t = np.arange(int(0.12 * FAKE_RATE)) / FAKE_RATE
        burst = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        gap = np.zeros(int(0.04 * FAKE_RATE), np.float32)
        out = np.concatenate([np.concatenate([burst, gap]) for _ in range(words)])
        out[-1] = 0.25  # ends on a non-zero sample, as a raw TTS clip can
        return out, FAKE_RATE
