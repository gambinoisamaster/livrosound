"""Whisper check: transcripts compared with the narrator's text, word by word.

The fast tests stand in fake mlx-whisper / faster-whisper modules; the slow one
transcribes a real Pocket-TTS clip with the cached whisper-large-v3-turbo.
"""

import sys
import time
import types
from importlib.machinery import ModuleSpec

import numpy as np
import pytest

from livrosound import validate
from livrosound.engines import engine_named
from livrosound.validate import (
    MAX_ATTEMPTS,
    MAX_ERROR_RATE,
    WHISPER_SAMPLE_RATE,
    Issue,
    Transcriber,
    Verdict,
    normalize,
    word_error_rate,
)


def test_normalize_ignores_case_punctuation_and_spells_numbers():
    assert normalize("Room 2, NOW!") == ["room", "two", "now"]
    assert normalize("It's bold.") == ["it's", "bold"]
    assert normalize("It’s bold.") == ["it's", "bold"], "curly apostrophes too"


def test_exact_match_is_zero():
    assert word_error_rate("For real? Not too out there?", "for real, not too out there") == 0.0


def test_dropped_word_counts():
    rate = word_error_rate("Excellent work, guys. This is bold.", "Excellent work. This is bold.")
    assert abs(rate - 1 / 6) < 1e-9


def test_garbage_is_high():
    assert word_error_rate("Hey, man. What you think?", "banana") > 0.5


@pytest.mark.parametrize(
    "said, heard",
    [
        ("Chapter twelve.", "Chapter 12."),
        ("In nineteen fourteen, the war began.", "In 1914, the war began."),
        ("It cost five dollars and fifty cents.", "It cost $5.50."),
        ("Mister Smith arrived at three o'clock.", "Mr. Smith arrived at 3 o'clock."),
        ("Nearly twenty-five percent of them left.", "Nearly 25% of them left."),
        ("He was the second son.", "He was the 2nd son."),
    ],
)
def test_whisper_digits_match_the_narrators_words(said, heard):
    assert word_error_rate(said, heard) == 0.0


def test_dataclasses_and_constants():
    assert MAX_ATTEMPTS == 3
    assert Verdict(ok=True, error_rate=0.0, transcript="Hi.").ok
    issue = Issue(block=7, text="Hello there.", transcript="Hello hair.", error_rate=0.5)
    assert (issue.block, issue.error_rate) == (7, 0.5)


# ---------- backends ----------


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__spec__ = ModuleSpec(name, None)
    module.__dict__.update(attrs)
    return module


def _tone(seconds=1.5, rate=24000):
    t = np.arange(int(seconds * rate)) / rate
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


@pytest.fixture
def fake_mlx(monkeypatch):
    calls = []

    def transcribe(audio, *, path_or_hf_repo, language):
        calls.append((audio, path_or_hf_repo, language))
        return {"text": " It was the best of times. "}

    monkeypatch.setattr(validate, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(validate, "local_model", lambda repo: f"/cache/{repo}")
    monkeypatch.setitem(sys.modules, "mlx_whisper", _module("mlx_whisper", transcribe=transcribe))
    return calls


def test_mlx_backend_hears_16k_audio(fake_mlx):
    t = Transcriber()
    assert t.available() == (True, "")
    x = _tone(1.5, 24000)
    x[100] = 3.0  # out of range: clipped before Whisper sees it
    verdict = t.check(x, 24000, "It was the best of times.")
    assert verdict == Verdict(ok=True, error_rate=0.0, transcript="It was the best of times.")
    audio, model, language = fake_mlx[0]
    assert audio.dtype == np.float32 and len(audio) == int(1.5 * WHISPER_SAMPLE_RATE)
    assert np.abs(audio).max() <= 1.0
    assert model == "/cache/mlx-community/whisper-large-v3-turbo" and language == "en"


def test_a_mangled_chunk_fails(fake_mlx):
    verdict = Transcriber().check(_tone(), 24000, "It was the worst of times, it was the age of wisdom.")
    assert not verdict.ok and verdict.error_rate > MAX_ERROR_RATE
    assert verdict.transcript == "It was the best of times."


def test_faster_whisper_elsewhere(monkeypatch):
    models = []

    class Segment:
        def __init__(self, text):
            self.text = text

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            models.append((name, device, compute_type))
            self.heard = []

        def transcribe(self, audio, language):
            self.heard.append((len(audio), language))
            return iter([Segment(" Hello"), Segment(" world. ")]), None

    monkeypatch.setattr(validate, "_is_apple_silicon", lambda: False)
    monkeypatch.setitem(sys.modules, "faster_whisper", _module("faster_whisper", WhisperModel=WhisperModel))
    t = Transcriber()
    assert t.available() == (True, "")
    assert t.check(_tone(1.0, 48000), 48000, "Hello, world!").ok
    assert t.transcribe(_tone(0.5, 16000), 16000) == "Hello world."
    assert models == [("large-v3-turbo", "cpu", "int8")], "loaded once"
    assert t._model.heard == [(16000, "en"), (8000, "en")]


def test_unavailable_without_whisper(monkeypatch):
    monkeypatch.setattr(validate, "_is_apple_silicon", lambda: True)
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)  # find_spec -> None: not installed
    ok, why = Transcriber().available()
    assert not ok and why.endswith("Install with: uv sync --extra validate")


@pytest.mark.slow
def test_real_whisper_passes_a_pocket_tts_clip(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    text = "It was the best of times, it was the worst of times, it was the age of wisdom."
    x, rate = engine_named("Pocket-TTS").synthesize("Aaron", text)
    t = Transcriber()
    assert t.available() == (True, "")
    t.check(x, rate, text)  # loads the model
    start = time.perf_counter()
    verdict = t.check(x, rate, text)
    print(f"\nWhisper: {len(x) / rate:.2f} s clip checked in {time.perf_counter() - start:.2f} s: {verdict}")
    assert verdict.ok and verdict.error_rate <= 0.1
    assert not t.check(x, rate, "Call me Ishmael. Some years ago, never mind how long precisely.").ok


def test_local_model_prefers_the_cache(monkeypatch, tmp_path):
    import huggingface_hub

    snapshot = tmp_path / "snapshots" / "abc"
    answers = {"cached/repo": str(snapshot / "config.json"), "missing/repo": None, "gone/repo": object()}
    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lambda repo, name: answers[repo])
    assert validate.local_model("cached/repo") == str(snapshot)
    assert validate.local_model("missing/repo") == "missing/repo"
    assert validate.local_model("gone/repo") == "gone/repo"  # huggingface_hub's "known not to exist" marker
