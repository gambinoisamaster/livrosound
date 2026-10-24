"""Loudness leveling: every block comes out at the same perceived volume."""

import math
import re
import subprocess

import numpy as np
import pytest

from livrosound.dsp import ffmpeg_binary, from_pcm16, to_pcm16
from livrosound.loudness import (
    MAX_GAIN_DB,
    PEAK_CEILING_DBFS,
    TARGET_LUFS,
    _moving_mean,
    _moving_min,
    level,
    loudness,
)

RATE = 24000


def tone(amplitude: float, seconds: float = 2.0, freq: float = 997.0, rate: int = RATE) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def peak_dbfs(x: np.ndarray) -> float:
    return 20 * math.log10(np.abs(x).max())


def speechlike(rate: int, seconds: float = 8.0, seed: int = 0) -> np.ndarray:
    """Voiced "syllables" (a gliding 110-220 Hz pitch with falling harmonics) at ~4 a second,
    hissy consonants, and pauses, so both of the meter's gates have work to do."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(rate * seconds)) / rate
    pitch = 165 + 55 * np.sin(2 * np.pi * 0.3 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / rate
    voiced = sum(np.sin(k * phase) / k for k in range(1, 30) if k * 220 < rate / 2)
    syllables = np.clip(np.sin(2 * np.pi * 4 * t), 0, None) ** 2
    hiss = np.diff(rng.normal(size=len(t) + 1)) * (np.sin(2 * np.pi * 1.3 * t) > 0.9)
    pauses = (t % 2.5) < 2.0  # half a second of silence every 2.5 s
    x = (0.25 * voiced * syllables + 0.05 * hiss) * pauses
    return x.astype(np.float32)


def ffmpeg_integrated_lufs(x: np.ndarray, rate: int) -> float:
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-nostats", "-f", "s16le", "-ar", str(rate), "-ac", "1",
         "-i", "pipe:0", "-af", "ebur128", "-f", "null", "-"],
        input=to_pcm16(x), capture_output=True, check=True,
    )
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", result.stderr.decode())[-1])


@pytest.mark.parametrize("rate", [24000, 48000])
def test_meter_matches_the_bs1770_calibration_tone(rate):
    # The standard's reference: a full-scale 997 Hz sine in one channel reads -3.01 LUFS.
    assert abs(loudness(tone(1.0, rate=rate), rate) - -3.01) < 0.05
    assert abs(loudness(tone(0.1, rate=rate), rate) - -23.01) < 0.05


@pytest.mark.parametrize("rate", [24000, 44100])
def test_meter_agrees_with_ffmpeg(rate):
    try:
        ffmpeg_binary()
    except FileNotFoundError:
        pytest.skip("ffmpeg isn't installed")
    for seed, gain in ((0, 1.0), (1, 0.1)):
        x = from_pcm16(to_pcm16(speechlike(rate, seed=seed) * gain))  # exactly what ffmpeg reads
        assert abs(loudness(x, rate) - ffmpeg_integrated_lufs(x, rate)) < 0.2


def test_quiet_and_loud_voices_come_out_equally_loud():
    for amplitude in (0.02, 0.05, 0.5, 1.0):  # -37 to -3 LUFS: up to 18 dB of gain either way
        assert abs(loudness(level(tone(amplitude), RATE), RATE) - TARGET_LUFS) < 0.2


def test_other_targets():
    for target in (-16.0, -23.0):
        assert abs(loudness(level(speechlike(RATE), RATE, target), RATE) - target) < 0.2


def test_short_word_is_leveled_too():
    word = tone(0.01, seconds=0.25)  # shorter than one 400 ms block
    assert abs(loudness(level(word, RATE), RATE) - TARGET_LUFS) < 0.2


def test_peaks_held_under_the_ceiling_without_losing_loudness():
    # Quiet speech with sharp transients: reaching the target means limiting the spikes.
    samples = tone(0.02, seconds=3.0, freq=200)
    samples[RATE::RATE // 4] = 0.6
    out = level(samples, RATE)
    assert peak_dbfs(out) <= PEAK_CEILING_DBFS
    assert abs(loudness(out, RATE) - TARGET_LUFS) < 0.2
    # Still under the ceiling once rounded to 16 bits for storage.
    assert peak_dbfs(from_pcm16(to_pcm16(out))) <= PEAK_CEILING_DBFS


def test_silence_is_left_alone():
    silent = np.zeros(RATE // 2, np.float32)
    assert np.array_equal(level(silent, RATE), silent)
    assert len(level(np.zeros(0, np.float32), RATE)) == 0
    assert loudness(np.zeros(0, np.float32), RATE) == -math.inf


def test_very_quiet_recording_still_reaches_the_target():
    whisper = tone(10 ** (-50 / 20))  # about -53 LUFS: needs +34 dB
    assert abs(loudness(level(whisper, RATE), RATE) - TARGET_LUFS) < 0.2


def test_nearly_silent_take_is_not_blown_up_into_hiss():
    faint = tone(10 ** (-62 / 20))  # about -65 LUFS: would need +46 dB
    gained = loudness(level(faint, RATE), RATE) - loudness(faint, RATE)
    assert abs(gained - MAX_GAIN_DB) < 0.2


def test_limiter_never_turns_a_peak_down_less_than_needed():
    # Including peaks on the very first and last samples, where the smoothing runs out of room.
    rng = np.random.default_rng(0)
    for n, half in ((50, 7), (1000, 44), (5000, 441)):
        needed = np.ones(n)
        spikes = np.concatenate(([0, n - 1], rng.integers(0, n, 8)))
        needed[spikes] = rng.uniform(0.05, 0.95, len(spikes))
        gain = _moving_mean(_moving_min(needed, half), half // 2)
        assert (gain <= needed + 1e-12).all()


def test_moving_windows_match_brute_force():
    rng = np.random.default_rng(1)
    for n, half in ((1, 3), (9, 4), (200, 13)):
        x = rng.uniform(0, 1, n)
        ones = np.concatenate((np.ones(half), x, np.ones(half)))
        edges = np.pad(x, half, mode="edge")
        assert np.allclose(_moving_min(x, half), [ones[i : i + 2 * half + 1].min() for i in range(n)])
        assert np.allclose(_moving_mean(x, half), [edges[i : i + 2 * half + 1].mean() for i in range(n)])


def test_length_and_type_unchanged_input_untouched():
    x = tone(0.3, seconds=1.234)
    before = x.copy()
    out = level(x, RATE)
    assert len(out) == len(x) and out.dtype == np.float32
    assert np.array_equal(x, before)
    assert level(x.astype(np.float64), RATE).dtype == np.float32
