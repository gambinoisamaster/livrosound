"""Audio helpers: resampling, speech trimming, declicking, PCM and speed changes.

The stitching test at the bottom is the one that matters most: the user hears
even a faint tick where two chunks meet.
"""

import numpy as np
import pytest

from livrosound.dsp import (
    DECLICK_MS,
    SAMPLE_RATE,
    declick,
    ffmpeg_binary,
    from_pcm16,
    ms_of,
    resample,
    samples_of,
    silence,
    time_stretch,
    to_pcm16,
    trim_speech,
)
from tests.fakes import FakeEngine

SR = SAMPLE_RATE
WIN = SR // 100  # trim_speech's 10 ms window
PAD = samples_of(30)


def tone(freq: float, seconds: float, sr: int = SR, amplitude: float = 0.5, phase: float = 0.0) -> np.ndarray:
    t = np.arange(round(seconds * sr)) / sr
    return (amplitude * np.sin(2 * np.pi * freq * t + phase)).astype(np.float32)


def quiet(seconds: float, sr: int = SR) -> np.ndarray:
    return np.zeros(round(seconds * sr), np.float32)


def dominant_hz(x: np.ndarray, sr: int) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / sr)[spectrum.argmax()])


def needs_ffmpeg():
    try:
        ffmpeg_binary()
    except FileNotFoundError:
        pytest.skip("ffmpeg isn't installed")


# --- resample ---------------------------------------------------------------------------


@pytest.mark.parametrize("sr_in", [44100, 22050, 48000, 16000])
def test_resample_keeps_a_1khz_tone_and_its_duration(sr_in):
    x = tone(1000, 2.0, sr_in)
    y = resample(x, sr_in, SR)
    assert y.dtype == np.float32
    assert len(y) == round(len(x) * SR / sr_in)
    assert abs(len(y) / SR - len(x) / sr_in) < 1 / SR
    ideal = tone(1000, 2.0, SR)
    inner = slice(SR // 10, -SR // 10)  # away from the edges, where the filter sees the padding
    assert np.abs(y[inner] - ideal[inner]).max() < 1e-3
    assert dominant_hz(y, SR) == pytest.approx(1000, abs=1)


def test_resample_filters_out_what_the_new_rate_cannot_hold():
    # 15 kHz is above 24 kHz audio's 12 kHz Nyquist: it must vanish, not fold back to 9 kHz.
    y = resample(tone(15000, 1.0, 44100), 44100, SR)[SR // 10 : -SR // 10]  # past the onset clicks
    assert np.sqrt(np.mean(y**2)) < 0.5 * 1e-4  # more than 80 dB down


def test_resample_does_not_wrap_the_end_onto_the_start():
    x = np.concatenate([quiet(0.9, 44100), tone(500, 0.1, 44100, amplitude=0.9)])
    y = resample(x, 44100, SR)
    assert np.abs(y[: SR // 2]).max() < 1e-6


def test_resample_same_rate_and_empty():
    x = tone(300, 0.1)
    assert np.array_equal(resample(x, SR, SR), x)
    assert len(resample(np.zeros(0, np.float32), 44100, SR)) == 0


# --- trim_speech ------------------------------------------------------------------------


def test_trim_keeps_the_speech_and_30ms_either_side():
    speech = tone(200, 0.5, amplitude=0.3)
    x = np.concatenate([quiet(0.2), speech, quiet(0.3)])
    y = trim_speech(x, SR)
    start = round(0.2 * SR) - PAD
    assert len(y) == len(speech) + 2 * PAD
    assert np.array_equal(y, x[start : start + len(y)])


def test_trim_drops_a_short_tick_running_into_the_last_sample():
    speech = tone(200, 0.4, amplitude=0.3)
    tick = tone(900, 0.04, amplitude=0.6, phase=0.5)  # ends mid-wave, on a non-zero sample
    y = trim_speech(np.concatenate([speech, quiet(0.06), tick]), SR)
    assert len(y) == len(speech) + PAD
    assert np.abs(y[len(speech) :]).max() == 0


def test_trim_never_drops_a_short_word_at_the_start():
    word = tone(250, 0.08, amplitude=0.3)  # "A", "No"
    x = np.concatenate([quiet(0.1), word, quiet(0.06), tone(200, 0.5, amplitude=0.3), quiet(0.2)])
    y = trim_speech(x, SR)
    assert np.array_equal(y[:PAD], np.zeros(PAD, np.float32))
    assert np.array_equal(y[PAD : PAD + len(word)], word)


@pytest.mark.parametrize(
    "ending",
    [
        [quiet(0.06), tone(300, 0.2, amplitude=0.3)],  # a whole word (> 120 ms) cut off by the end
        [quiet(0.015), tone(300, 0.05, amplitude=0.3)],  # too close to the word before: part of it
        [quiet(0.06), tone(3000, 0.04, amplitude=0.1), quiet(0.1)],  # a final "t": stops before the end
    ],
)
def test_trim_keeps_real_final_sounds(ending):
    speech = tone(200, 0.5, amplitude=0.3)
    sound = ending[1]
    y = trim_speech(np.concatenate([speech, *ending]), SR)
    found = np.flatnonzero(np.abs(y) > 0.05)
    assert found[-1] >= len(speech) + len(ending[0]) + len(sound) - WIN


def test_trim_keeps_a_clip_that_is_one_short_sound():
    no = tone(220, 0.1, amplitude=0.3)
    assert np.array_equal(trim_speech(np.concatenate([quiet(0.2), no]), SR)[PAD:], no)


def test_trim_floor_follows_a_quiet_voice():
    # A narrator peaking at -26 dBFS whose final "t" (at -45 dBFS) comes after a short
    # closure: a flat -40 dBFS floor would cut the "t" off.
    vowel = tone(200, 0.4, amplitude=0.05)
    t_release = tone(2500, 0.03, amplitude=0.0056)
    x = np.concatenate([quiet(0.3), vowel, quiet(0.06), t_release, quiet(0.4)])
    y = trim_speech(x, SR)
    assert len(y) == len(vowel) + round(0.06 * SR) + len(t_release) + 2 * PAD


def test_trim_ignores_pocket_tts_background_noise():
    rng = np.random.default_rng(0)
    noise = (rng.uniform(-1, 1, round(1.2 * SR)) * 10 ** (-75 / 20)).astype(np.float32)
    noise[0:8] = 10 ** (-57 / 20)  # the faint blip a Pocket-TTS clip starts with
    speech_at = round(0.4 * SR)
    noise[speech_at : speech_at + round(0.5 * SR)] += tone(200, 0.5, amplitude=0.3)
    y = trim_speech(noise, SR)
    assert len(y) == round(0.5 * SR) + 2 * PAD


def test_trim_leaves_silence_alone():
    x = quiet(0.5)
    assert trim_speech(x, SR) is x
    hum = tone(100, 0.5, amplitude=10 ** (-55 / 20))  # below -50 dBFS: a near-silent take
    assert np.array_equal(trim_speech(hum, SR), hum)
    assert len(trim_speech(np.zeros(0, np.float32), SR)) == 0


# --- declick ----------------------------------------------------------------------------


def test_declick_ramps_both_edges_to_exactly_zero():
    x = np.full(SR, 0.5, np.float32)
    y = declick(x, SR)
    n = samples_of(DECLICK_MS)
    assert y[0] == 0 and y[-1] == 0
    assert np.allclose(y[:n], 0.5 * np.linspace(0, 1, n))
    assert np.allclose(y[-n:], 0.5 * np.linspace(1, 0, n))
    assert np.array_equal(y[n:-n], x[n:-n])
    assert np.all(x == 0.5), "the input must not be modified"


def test_declick_short_and_empty_clips():
    y = declick(np.ones(100, np.float32), SR)  # shorter than two 8 ms ramps
    assert len(y) == 100 and y[0] == 0 and y[-1] == 0 and y.max() == 1
    assert len(declick(np.zeros(0, np.float32), SR)) == 0


# --- stitching: the tick test -----------------------------------------------------------


def test_stitched_chunks_join_without_a_tick():
    """Chunks that start/end on non-zero samples, one with Pocket-TTS's tail tick, one at
    another sample rate and one from the fake engine, prepared the way generation prepares
    every chunk and butted together: no jump at any join, and the tick is gone."""
    t = np.arange(round(0.5 * SR)) / SR
    tail_tick_speech = tone(180, 0.4, amplitude=0.4, phase=1.0)
    fake, fake_sr = FakeEngine().synthesize("Aaron", "It was the best of times")
    chunks = [
        ((0.5 * np.cos(2 * np.pi * 230 * t)).astype(np.float32), SR),  # starts at 0.5, ends mid-wave
        (fake, fake_sr),  # ends on a stray 0.25 sample
        (np.concatenate([tail_tick_speech, quiet(0.06), tone(900, 0.04, amplitude=0.6, phase=0.5)]), SR),
        (tone(330, 0.3, 22050, amplitude=0.6, phase=2.0), 22050),
    ]
    prepared = [declick(trim_speech(resample(x, sr, SR), SR), SR) for x, sr in chunks]
    raw = [resample(x, sr, SR) for x, sr in chunks]

    def joins(clips):
        track = np.concatenate(clips + clips[:1])
        bounds = np.cumsum([len(c) for c in clips])
        return track, bounds

    track, bounds = joins(prepared)
    for j in bounds:
        assert np.abs(np.diff(track[j - 3 : j + 2])).max() < 0.005, f"tick at sample {j}"
    raw_track, raw_bounds = joins(raw)
    assert max(abs(raw_track[j] - raw_track[j - 1]) for j in raw_bounds) > 0.2  # the test has teeth

    with_tick = prepared[2]
    assert len(with_tick) == len(tail_tick_speech) + PAD
    assert np.abs(with_tick[len(tail_tick_speech) :]).max() == 0
    # The fake engine's stray last sample is a tick too: dropped with its 30 ms of silence.
    assert len(prepared[1]) == len(fake) - samples_of(10)
    assert prepared[1][-1] == 0


@pytest.mark.slow
def test_real_pocket_tts_chunks_trim_and_stitch_cleanly():
    """Real clips, including short ones that often end on a tick ("Yes.", "Kept."): nothing
    audible is cut except a short tail sound running into the end, and the joins are clean."""
    torch = pytest.importorskip("torch")
    pocket_tts = pytest.importorskip("pocket_tts")
    from livrosound.paths import VOICES_DIR

    torch.manual_seed(0)
    model = pocket_tts.TTSModel.load_model()
    prepared = []
    for voice in ("Aaron", "Susan", "Kyle"):
        state = model.get_state_for_audio_prompt(VOICES_DIR / f"{voice}.wav")
        for text in ("Yes.", "Kept.", "She said nothing, and the room went quiet."):
            x = resample(model.generate_audio(state, text).numpy(), model.sample_rate, SR)
            y = trim_speech(x, SR)
            start = (y.ctypes.data - x.ctypes.data) // x.itemsize  # y is a slice of x
            floor = max(np.abs(x).max() * 0.01, 10 ** (-50 / 20))  # trim_speech's, from the clip's peak
            assert samples_of(200) < len(y) < len(x) - samples_of(100), "speech kept, lead-in and tail cut"
            assert np.abs(x[:start]).max(initial=0) < floor
            cut = np.flatnonzero(np.abs(x[start + len(y) :]) >= floor)
            if len(cut):  # only a dropped tick: short, and running into the clip's end
                assert cut[-1] - cut[0] <= samples_of(130), (voice, text)
                assert cut[-1] >= len(x) - start - len(y) - 2 * WIN, (voice, text)
            prepared.append(declick(y, SR))
    track = np.concatenate(prepared)
    for j in np.cumsum([len(c) for c in prepared])[:-1]:
        assert np.abs(np.diff(track[j - 3 : j + 2])).max() < 0.005


# --- PCM, units, silence ----------------------------------------------------------------


def test_pcm16_round_trip_and_clipping():
    grid = np.array([-32768, -1, 0, 1, 12345, 32767], np.int16)
    x = grid.astype(np.float32) / 32768
    data = to_pcm16(x)
    assert data == grid.astype("<i2").tobytes()
    assert np.array_equal(from_pcm16(data), x)
    assert from_pcm16(data).dtype == np.float32
    loud = np.frombuffer(to_pcm16(np.array([1.0, 1.7, -1.0, -3.0])), "<i2")
    assert list(loud) == [32767, 32767, -32768, -32768]


def test_units_and_silence():
    assert ms_of(SR) == 1000 and ms_of(36) == 2 and ms_of(44100, 44100) == 1000
    assert samples_of(DECLICK_MS) == 192 and samples_of(1000, 22050) == 22050
    pause = silence(350)
    assert pause.dtype == np.float32 and len(pause) == 8400 and not pause.any()
    assert len(silence(0)) == 0 and len(silence(-5)) == 0


# --- time_stretch -----------------------------------------------------------------------


def test_time_stretch_at_normal_speed_returns_the_input():
    pcm = to_pcm16(tone(440, 0.2))
    assert time_stretch(pcm, 1.0) is pcm
    assert time_stretch(b"", 1.5) == b""
    with pytest.raises(ValueError):
        time_stretch(pcm, 0)


@pytest.mark.parametrize("rate", [0.5, 0.75, 1.25, 1.5, 1.75, 2.0, 3.0, 0.3])
def test_time_stretch_changes_speed_not_pitch(rate):
    needs_ffmpeg()
    x = tone(440, 2.0)
    y = from_pcm16(time_stretch(to_pcm16(x), rate))
    assert len(y) == pytest.approx(len(x) / rate, rel=0.02)
    assert dominant_hz(y[SR // 10 : -SR // 10], SR) == pytest.approx(440, abs=3)
