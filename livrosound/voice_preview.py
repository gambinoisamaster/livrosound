"""Short audition clips, so a narrator can be heard before it's chosen.

Every voice reads the same line, rendered by the engine that will read the
book and finished like generated audio (resampled to 24 kHz, trimmed of its
end-of-clip tick, declicked, leveled), so clips compare fairly. Clips are
cached in paths.voice_previews_dir(); the file name carries a fingerprint of
the voice sample (and transcript) it was cloned from, so a re-recorded voice
gets a fresh clip and the old one is deleted.
"""

from __future__ import annotations

import hashlib
import re
import threading
import wave
from pathlib import Path

from . import dsp, loudness, paths, voices
from .engines.base import TTSEngine

PREVIEW_LINE = "It was the best of times, it was the worst of times."


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "voice"


def _prefix(engine_name: str, voice: str) -> str:
    return f"{_slug(engine_name)}--{_slug(voice)}--"


def preview_path(engine_name: str, voice: str) -> Path:
    """Where this voice's clip lives (it may not exist yet)."""
    digest = hashlib.sha1(f"{engine_name}\0{voice}\0{PREVIEW_LINE}".encode())
    sample = voices.voice_named(voice)
    if sample is not None and sample.wav.is_file():
        stat = sample.wav.stat()
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\0{sample.transcript}".encode())
    return paths.voice_previews_dir() / f"{_prefix(engine_name, voice)}{digest.hexdigest()[:10]}.wav"


def make_preview(engine: TTSEngine, voice: str) -> Path:
    """Render this voice's clip if it isn't cached yet; returns its path."""
    path = preview_path(engine.name, voice)
    if path.is_file():
        return path
    x, rate = engine.synthesize(voice, PREVIEW_LINE)
    x = dsp.resample(x, rate, dsp.SAMPLE_RATE)
    x = dsp.declick(dsp.trim_speech(x, dsp.SAMPLE_RATE), dsp.SAMPLE_RATE)
    pcm = dsp.to_pcm16(loudness.level(x, dsp.SAMPLE_RATE))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Not *.wav, so the stale-clip sweep below never takes another thread's half-written file.
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.partial")
    with wave.open(str(tmp), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(dsp.SAMPLE_RATE)
        f.writeframes(pcm)
    tmp.replace(path)
    for stale in path.parent.glob(f"{_prefix(engine.name, voice)}*.wav"):
        if stale != path:
            stale.unlink(missing_ok=True)  # the clip of an older recording
    return path
