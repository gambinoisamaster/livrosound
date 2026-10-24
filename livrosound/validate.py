"""Check generated speech against its text with Whisper ("Whisper check").

TTS models occasionally drop, repeat or mangle words. When the user turns the
check on, every chunk is transcribed locally right after it's synthesized and
compared word by word with the text it was asked to say; the generator
re-synthesizes a chunk that fails (up to MAX_ATTEMPTS in all), keeps the best
attempt, and reports the chunks that never passed as Issues.

Backends (both offline, nothing leaves the machine):
- mlx-whisper on Apple Silicon (fast, on the GPU), whisper-large-v3-turbo
- faster-whisper everywhere else (CPU)

Install with: uv sync --extra validate. The model loads from the local Hugging
Face cache (downloaded on first use if it isn't there).

Whisper writes what it hears its own way ("Chapter 12", "Mr. Smith", "5%") while
the narrator was given words ("Chapter twelve", "Mister Smith", "five percent"),
so both sides go through speech.speakable (the same spelling-out the narrator's
text got) and are compared as plain lowercase words.
"""

from __future__ import annotations

import difflib
import importlib.util
import platform
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import dsp
from .speech import speakable

WHISPER_SAMPLE_RATE = 16000
MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
FASTER_WHISPER_MODEL = "large-v3-turbo"

# A chunk fails when its word error rate exceeds this. Short chunks get no
# slack: a two-word heading with one wrong word is half wrong.
MAX_ERROR_RATE = 0.15
MAX_ATTEMPTS = 3

_PUNCT_RE = re.compile(r"[^\w\s']", re.UNICODE)


def local_model(repo_id: str) -> str:
    """The cached snapshot folder of a Hugging Face model, or repo_id itself (which
    the loader then downloads) when it isn't cached. Looking up the cache directly
    skips the network check a loader makes before using a cached copy."""
    from huggingface_hub import try_to_load_from_cache

    config = try_to_load_from_cache(repo_id, "config.json")
    return str(Path(config).parent) if isinstance(config, str) else repo_id


@dataclass
class Verdict:
    ok: bool
    error_rate: float
    transcript: str


@dataclass
class Issue:
    """A chunk that never passed; the best attempt was kept."""

    block: int
    text: str
    transcript: str
    error_rate: float


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def normalize(text: str) -> list[str]:
    """The words of text as the narrator says them, lowercase, punctuation dropped —
    so 'Room 2!' and 'room two' compare equal."""
    text = _PUNCT_RE.sub(" ", speakable(text).lower().replace("-", " "))
    return [w for w in (token.strip("'") for token in text.split()) if w]


def word_error_rate(source: str, transcript: str) -> float:
    """Words inserted, dropped or changed, as a share of the source's words."""
    src, hyp = normalize(source), normalize(transcript)
    if not src:
        return 0.0
    errors = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=src, b=hyp, autojunk=False).get_opcodes():
        if tag != "equal":
            errors += max(i2 - i1, j2 - j1)
    return errors / len(src)


class Transcriber:
    """Whisper, loaded on first use (importing it alone takes seconds)."""

    def __init__(self) -> None:
        self._backend = "mlx" if _is_apple_silicon() else "faster"
        package = "mlx_whisper" if self._backend == "mlx" else "faster_whisper"
        self._installed = importlib.util.find_spec(package) is not None
        self._model = None  # faster-whisper's model (mlx-whisper keeps its own)
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        if not self._installed:
            return False, "Whisper is not installed. Install with: uv sync --extra validate"
        return True, ""

    def transcribe(self, x: np.ndarray, sr: int) -> str:
        audio = np.clip(dsp.resample(np.asarray(x, np.float32), sr, WHISPER_SAMPLE_RATE), -1.0, 1.0)
        with self._lock:
            if self._backend == "mlx":
                import mlx_whisper

                result = mlx_whisper.transcribe(audio, path_or_hf_repo=local_model(MLX_MODEL), language="en")
                return result.get("text", "").strip()
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(FASTER_WHISPER_MODEL, device="cpu", compute_type="int8")
            segments, _ = self._model.transcribe(audio, language="en")
            return " ".join(s.text.strip() for s in segments)

    def check(self, x: np.ndarray, sr: int, text: str) -> Verdict:
        """Does this audio say text? (float32 mono samples at sr)"""
        transcript = self.transcribe(x, sr)
        rate = word_error_rate(text, transcript)
        return Verdict(ok=rate <= MAX_ERROR_RATE, error_rate=rate, transcript=transcript)
