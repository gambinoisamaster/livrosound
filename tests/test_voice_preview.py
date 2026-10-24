"""Audition clips for Choose a Voice: rendered once per voice and engine, finished like
generated audio, cached by recording."""

import wave

import numpy as np
import pytest

from livrosound import dsp, loudness, paths, voices
from livrosound.engines import engine_named
from livrosound.voice_preview import PREVIEW_LINE, make_preview, preview_path
from tests.fakes import FakeEngine


def _read(path):
    with wave.open(str(path), "rb") as f:
        assert (f.getnchannels(), f.getsampwidth(), f.getframerate()) == (1, 2, dsp.SAMPLE_RATE)
        return dsp.from_pcm16(f.readframes(f.getnframes()))


class SlowRateEngine(FakeEngine):
    """Speaks at 22.05 kHz, like some TTS models."""

    name = "Fake-22k"

    def synthesize(self, voice, text):
        x, rate = super().synthesize(voice, text)
        return dsp.resample(x, rate, 22050), 22050


def test_every_voice_reads_the_same_line_once():
    assert PREVIEW_LINE == "It was the best of times, it was the worst of times."
    engine = FakeEngine()
    path = make_preview(engine, "Ana")
    assert path.is_file() and path.parent == paths.voice_previews_dir()
    assert engine.calls == [("Ana", PREVIEW_LINE)]
    assert make_preview(engine, "Ana") == path
    assert len(engine.calls) == 1, "a cached clip is never re-rendered"
    assert make_preview(engine, "Aaron") != path
    assert list(path.parent.glob("*.partial")) == []


def test_clip_is_finished_like_generated_audio():
    raw, _ = FakeEngine().synthesize("Ana", PREVIEW_LINE)
    clip = _read(make_preview(FakeEngine(), "Ana"))
    assert clip[0] == 0 and clip[-1] == 0, "declicked at both edges"
    assert len(clip) < len(raw), "the fake engine's end-of-clip tick is trimmed"
    assert loudness.loudness(clip, dsp.SAMPLE_RATE) == pytest.approx(loudness.TARGET_LUFS, abs=0.5)


def test_other_rates_are_resampled_to_24k():
    raw, _ = FakeEngine().synthesize("Ana", PREVIEW_LINE)
    clip = _read(make_preview(SlowRateEngine(), "Ana"))  # _read checks 24 kHz
    assert abs(len(clip) - len(raw)) < dsp.SAMPLE_RATE * 0.05, "same duration"


def test_clips_live_in_the_current_cache(tmp_path, monkeypatch):
    first = preview_path("Fake", "Ana")
    monkeypatch.setenv("LIVROSOUND_CACHE", str(tmp_path / "elsewhere"))
    second = make_preview(FakeEngine(), "Ana")
    assert second.parent == tmp_path / "elsewhere" / "voice_previews"
    assert second.name == first.name


def test_engines_have_separate_clips():
    assert preview_path("Pocket-TTS", "Ana") != preview_path("Another engine", "Ana")
    assert preview_path("Pocket-TTS", "Ana").name.startswith("Pocket-TTS--Ana--")


def test_re_recording_a_voice_makes_a_fresh_clip(tmp_path, monkeypatch):
    folder = tmp_path / "voices"
    folder.mkdir()
    monkeypatch.setattr(voices, "VOICES_DIR", folder)
    sample = folder / "Mare.wav"
    sample.write_bytes(b"first take")
    (folder / "Mare.wav.txt").write_text("First take.")
    engine = FakeEngine()
    first = make_preview(engine, "Mare")
    sample.write_bytes(b"a longer second take")
    assert preview_path(engine.name, "Mare") != first
    second = make_preview(engine, "Mare")
    assert second.is_file() and not first.exists(), "the old recording's clip is cleaned up"
    (folder / "Mare.wav.txt").write_text("A corrected transcript.")
    assert preview_path(engine.name, "Mare") != second, "a corrected transcript makes a fresh clip"


def test_a_failed_render_leaves_nothing_behind():
    class Broken(FakeEngine):
        def synthesize(self, voice, text):
            raise RuntimeError("model missing")

    with pytest.raises(RuntimeError):
        make_preview(Broken(), "Ana")
    folder = paths.voice_previews_dir()
    assert not folder.exists() or list(folder.iterdir()) == []


@pytest.mark.slow
def test_real_pocket_preview():
    clip = _read(make_preview(engine_named("Pocket-TTS"), "Ana"))
    seconds = len(clip) / dsp.SAMPLE_RATE
    assert 2.0 < seconds < 6.0
    assert clip[0] == 0 and clip[-1] == 0
    assert loudness.loudness(clip, dsp.SAMPLE_RATE) == pytest.approx(loudness.TARGET_LUFS, abs=0.5)
    assert np.abs(clip).max() <= 10 ** (loudness.PEAK_CEILING_DBFS / 20) + 1e-4
