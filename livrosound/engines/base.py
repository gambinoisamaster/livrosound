from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class TTSEngine(ABC):
    """A local text-to-speech backend that clones the narrator voices in assets/voices/."""

    name: str
    install_hint: str = ""

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(usable, reason-if-not)."""

    @abstractmethod
    def voices(self) -> list[str]:
        """Names of the cloned voices this engine can read with."""

    @abstractmethod
    def synthesize(self, voice: str, text: str) -> tuple[np.ndarray, int]:
        """Speech for one chunk of text: float32 mono samples in [-1, 1] and their sample rate."""
