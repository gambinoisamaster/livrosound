"""Audio helpers shared by generation, previews, playback and export.

After synthesis every sound in LivroSound is 16-bit mono PCM at SAMPLE_RATE;
in between it's a float32 numpy array in [-1, 1]. Each chunk an engine returns
is resampled to SAMPLE_RATE, trimmed to its speech and declicked before it is
stitched to the next one, because a raw chunk can end in two ways the ear
hears as a "tick" at the join:

- Pocket-TTS sometimes ends a clip on a short, cut-off half-sound after the
  last word. trim_speech drops it (but never a short sound at the *start* of a
  clip: Whisper showed those are real words like "A" or "No").
- A clip can start or stop on a non-zero sample, so butting two clips together
  makes the waveform jump. declick ramps both edges to zero over a few ms, far
  shorter than a syllable.

resample is a windowed-sinc polyphase filter in numpy alone (no scipy): clips
are short, the rates are standard ones (22.05/44.1/48 kHz), and the signal is
zero-padded rather than treated as periodic, so nothing wraps from a clip's
end onto its start. time_stretch hands playback speed changes to ffmpeg's
atempo, which keeps the narrator's pitch.
"""

from __future__ import annotations

import functools
import math
import os
import shutil
import subprocess

import numpy as np

SAMPLE_RATE = 24000
DECLICK_MS = 8

# resample: a Kaiser-windowed sinc reaching 32 zero crossings either side, cut off at 92% of
# the lower Nyquist, holds everything above that Nyquist 80 dB down or more (no aliasing).
_SINC_ZEROS = 32
_ROLLOFF = 0.92
_KAISER_BETA = 7.9
_RESAMPLE_BLOCK = 8192  # outputs per pass; bounds memory on long clips

# trim_speech, as in yt-dub, looks at 10 ms windows. A window is sound when it peaks within
# 40 dB of the clip's loudest one and above -50 dBFS. (yt-dub's flat -40 dBFS suits a loud
# voice, but cloned voices peak anywhere from -22 to -2 dBFS and a flat floor clipped quieter
# narrators' final consonants: the "t" of "quiet", the "p" of "Stop!". Pocket-TTS's silence
# sits near -75 dBFS, with a -57 dBFS blip in a clip's first 10 ms.)
_TRIM_WINDOW_MS = 10
_TRIM_RANGE_DB = 40.0
_TRIM_FLOOR_DB = -50.0
_TRIM_PAD_MS = 30  # kept around the speech so soft onsets and endings aren't clipped
_TICK_MAX_MS = 120  # a final sound this short, running into the last sample...
_TICK_GAP_MS = 30  # ...after at least this much silence, is a cut-off tick, not a word

# A Mac app opened from Finder gets a bare PATH without Homebrew's folders.
_FFMPEG_FALLBACKS = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """x converted from sr_in to sr_out (float32), same duration, pitch and level."""
    x = np.asarray(x, dtype=np.float32)
    if sr_in == sr_out or len(x) == 0:
        return x
    g = math.gcd(sr_in, sr_out)
    up, down = sr_out // g, sr_in // g
    n_out = round(len(x) * up / down)
    # Output k sits at input position t = k*down/up = base + phase/up; it is the sum of
    # x[base + j] * kernel(phase/up - j) over the taps j in [-reach, reach].
    cutoff = _ROLLOFF * 0.5 * min(1.0, up / down)  # cycles per input sample
    half_width = _SINC_ZEROS / (2 * cutoff)  # in input samples
    reach = math.ceil(half_width)
    tau = np.arange(up)[:, None] / up - np.arange(-reach, reach + 1)[None, :]  # (phase, tap)
    inside = np.clip(1 - (tau / half_width) ** 2, 0, None)
    window = np.where(inside > 0, np.i0(_KAISER_BETA * np.sqrt(inside)) / np.i0(_KAISER_BETA), 0)
    table = (2 * cutoff * np.sinc(2 * cutoff * tau) * window).astype(np.float32)

    padded = np.concatenate((np.zeros(reach, np.float32), x, np.zeros(reach + 1, np.float32)))
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * reach + 1)
    out = np.empty(n_out, np.float32)
    for first in range(0, n_out, _RESAMPLE_BLOCK):
        pos = np.arange(first, min(first + _RESAMPLE_BLOCK, n_out)) * down
        out[first : first + len(pos)] = np.einsum("ij,ij->i", windows[pos // up], table[pos % up])
    return out


def trim_speech(x: np.ndarray, sr: int) -> np.ndarray:
    """x cut down to its speech, plus 30 ms either side, without a cut-off tick at the end.

    Sound is any 10 ms window peaking within 40 dB of the clip's loudest and above
    -50 dBFS. A short sound (<= 120 ms) that runs into the clip's last sample after
    >= 30 ms of silence is Pocket-TTS's end-of-clip tick and is dropped. A short sound
    at the start is kept. A clip with no sound comes back unchanged."""
    win = max(1, round(sr * _TRIM_WINDOW_MS / 1000))
    n = -(-len(x) // win)  # the last, partial window counts: that's where a tick ends
    if n == 0:
        return x
    padded = np.zeros(n * win, np.float32)
    padded[: len(x)] = np.abs(x)
    peaks = padded.reshape(n, win).max(axis=1)
    loud = peaks > max(peaks.max() * 10 ** (-_TRIM_RANGE_DB / 20), 10 ** (_TRIM_FLOOR_DB / 20))
    runs = np.flatnonzero(np.diff(np.concatenate(([0], loud.astype(np.int8), [0])))).reshape(-1, 2)
    if len(runs) == 0:
        return x
    if len(runs) > 1:
        (_, prev_end), (start, end) = runs[-2], runs[-1]
        if (end >= n - 1 and (end - start) * _TRIM_WINDOW_MS <= _TICK_MAX_MS
                and (start - prev_end) * _TRIM_WINDOW_MS >= _TICK_GAP_MS):
            runs = runs[:-1]
    pad = round(sr * _TRIM_PAD_MS / 1000)
    return x[max(0, runs[0, 0] * win - pad) : min(len(x), runs[-1, 1] * win + pad)]


def declick(x: np.ndarray, sr: int, ms: float = DECLICK_MS) -> np.ndarray:
    """A copy of x whose first and last ms ramp linearly from/to exactly zero."""
    out = np.array(x, dtype=np.float32)
    n = min(int(sr * ms / 1000), len(out) // 2)
    if n > 0:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        out[:n] *= ramp
        out[-n:] *= ramp[::-1]
    return out


def silence(ms: int, sr: int = SAMPLE_RATE) -> np.ndarray:
    return np.zeros(max(0, samples_of(ms, sr)), np.float32)


def to_pcm16(x: np.ndarray) -> bytes:
    """16-bit little-endian PCM; samples outside [-1, 1) are clipped, not wrapped."""
    return np.clip(np.round(np.asarray(x, np.float64) * 32768), -32768, 32767).astype("<i2").tobytes()


def from_pcm16(data: bytes) -> np.ndarray:
    """The inverse of to_pcm16 (exact for samples on the 16-bit grid)."""
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768


def ms_of(n_samples: int, sr: int = SAMPLE_RATE) -> int:
    return round(1000 * n_samples / sr)


def samples_of(ms: float, sr: int = SAMPLE_RATE) -> int:
    return round(ms * sr / 1000)


@functools.cache
def ffmpeg_binary() -> str:
    """Path of the ffmpeg executable (FileNotFoundError, with how to install it, if missing)."""
    found = shutil.which("ffmpeg") or next((p for p in _FFMPEG_FALLBACKS if os.access(p, os.X_OK)), None)
    if not found:
        raise FileNotFoundError("ffmpeg isn't installed. Install it with: brew install ffmpeg")
    return found


def time_stretch(pcm: bytes, rate: float, sr: int = SAMPLE_RATE) -> bytes:
    """16-bit mono pcm played rate times as fast, pitch unchanged (rate 1 returns pcm itself).

    One atempo stage covers 0.5..2.0; rates beyond that chain stages, each within
    that range, which sounds better than one big step."""
    if rate <= 0:
        raise ValueError(f"rate must be positive, not {rate}")
    if rate == 1 or not pcm:
        return pcm
    stages = []
    while rate > 2.0:
        stages.append(2.0)
        rate /= 2.0
    while rate < 0.5:
        stages.append(0.5)
        rate /= 0.5
    stages.append(rate)
    fmt = ["-f", "s16le", "-ar", str(sr), "-ac", "1"]
    result = subprocess.run(
        [ffmpeg_binary(), "-v", "error", *fmt, "-i", "pipe:0",
         "-filter:a", ",".join(f"atempo={s:.6f}" for s in stages), *fmt, "pipe:1"],
        input=pcm, capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"ffmpeg atempo failed: {result.stderr.decode(errors='replace')[-500:]}")
    return result.stdout
