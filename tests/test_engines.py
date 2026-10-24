"""TTS engines: only the cloned voices, numpy out, one model per engine shared across threads.

The fast tests swap the engine's model for a fake; the slow ones load the real
Pocket-TTS model (cached locally, no network) and print how fast it reads
(x realtime).
"""

import sys
import threading
import time
import types
from importlib.machinery import ModuleSpec

import numpy as np
import pytest

from livrosound import dsp, voices
from livrosound.engines import PocketTTSEngine, engine_named, load_engines
from tests.test_voices import CLONED

PREDEFINED = ["alba", "javert", "marius", "cosette"]


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__spec__ = ModuleSpec(name, None)
    module.__dict__.update(attrs)
    return module


# ---------- registry ----------


def test_engines_registry():
    engines = load_engines()
    assert [e.name for e in engines] == ["Pocket-TTS"]
    assert isinstance(engines[0], PocketTTSEngine)
    assert load_engines()[0] is engines[0], "one instance: previews and generation share its model"
    assert engine_named("Pocket-TTS") is engines[0]
    assert engine_named("OmniVoice") is None
    assert engines[0].install_hint == "Install with: uv sync"


def test_voices_are_the_cloned_catalog_only():
    names = PocketTTSEngine().voices()
    assert names == CLONED
    assert not set(names) & set(PREDEFINED), "never Pocket-TTS's built-in voices"


def test_voices_follow_the_folder(tmp_path, monkeypatch):
    (tmp_path / "Solo.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(voices, "VOICES_DIR", tmp_path)
    assert PocketTTSEngine().voices() == ["Solo"]


def test_availability_reports_how_to_install(monkeypatch):
    assert PocketTTSEngine().available() == (True, "")
    monkeypatch.setitem(sys.modules, "pocket_tts", None)  # find_spec -> None: not installed
    ok, why = PocketTTSEngine().available()
    assert not ok and "pocket_tts" in why and why.endswith("Install with: uv sync")


# ---------- Pocket-TTS ----------


class FakeTensor:
    def __init__(self, values):
        self._values = values

    def numpy(self):
        return np.asarray(self._values)


class FakePocketModel:
    sample_rate = 24000
    has_voice_cloning = True

    def __init__(self):
        self.prompts = []
        self.texts = []
        self.busy = 0
        self.overlaps = 0

    def get_state_for_audio_prompt(self, prompt):
        self.prompts.append(prompt)
        return f"state:{prompt}"

    def generate_audio(self, state, text):
        self.busy += 1
        if self.busy > 1:
            self.overlaps += 1
        time.sleep(0.005)
        self.texts.append((state, text))
        self.busy -= 1
        return FakeTensor(np.array([0.0, 0.5, -1.7, 2.0, 0.25], np.float64))


def test_pocket_clones_each_voice_once_and_returns_numpy():
    engine = PocketTTSEngine()
    engine._model = model = FakePocketModel()
    x, rate = engine.synthesize("Ana", "Hello there.")
    assert rate == 24000
    assert x.dtype == np.float32 and x.ndim == 1
    assert x.tolist() == [0.0, 0.5, -1.0, 1.0, 0.25], "clipped to [-1, 1]"
    engine.synthesize("Ana", "Again.")
    engine.synthesize("Aaron", "Me too.")
    assert model.prompts == [voices.voice_named("Ana").wav, voices.voice_named("Aaron").wav]
    assert [t for _, t in model.texts] == ["Hello there.", "Again.", "Me too."]


@pytest.mark.parametrize("voice", ["alba", "Auto", "Default_Narrator_Male"])
def test_pocket_refuses_voices_that_are_not_cloned(voice):
    engine = PocketTTSEngine()
    engine._model = model = FakePocketModel()
    with pytest.raises(ValueError, match="No voice named"):
        engine.synthesize(voice, "Hello.")
    assert model.prompts == [] and model.texts == []


def test_pocket_one_request_at_a_time():
    engine = PocketTTSEngine()
    engine._model = model = FakePocketModel()
    threads = [
        threading.Thread(target=lambda v=v: [engine.synthesize(v, "Hi.") for _ in range(5)])
        for v in ("Ana", "Aaron", "Ana", "Kyle")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(model.texts) == 20 and model.overlaps == 0
    assert len(model.prompts) == 3, "each voice's state is made once"


def test_pocket_loads_the_model_once_and_rejects_a_model_that_cannot_clone(monkeypatch):
    loads = []

    class TTSModel:
        @staticmethod
        def load_model():
            loads.append(1)
            return FakePocketModel()

    monkeypatch.setitem(sys.modules, "pocket_tts", _module("pocket_tts", TTSModel=TTSModel))
    engine = PocketTTSEngine()
    engine.synthesize("Ana", "One.")
    engine.synthesize("Ana", "Two.")
    assert len(loads) == 1

    class NoCloning(FakePocketModel):
        has_voice_cloning = False

    TTSModel.load_model = staticmethod(lambda: NoCloning())
    with pytest.raises(RuntimeError, match="voice-cloning weights"):
        PocketTTSEngine().synthesize("Ana", "Hello.")


# ---------- real models ----------

SENTENCE = "It was a bright cold day in April, and the clocks were striking thirteen."


def _speed(engine, voice, text=SENTENCE):
    engine.synthesize(voice, "Warm up.")  # model load and voice cloning aren't reading speed
    start = time.perf_counter()
    x, rate = engine.synthesize(voice, text)
    took = time.perf_counter() - start
    seconds = len(x) / rate
    print(f"\n{engine.name} {voice}: {seconds:.2f} s of speech in {took:.2f} s = {seconds / took:.2f}x realtime")
    return x, rate, seconds


def _assert_speech(x, rate, seconds):
    assert rate == dsp.SAMPLE_RATE
    assert x.dtype == np.float32 and x.ndim == 1
    assert 2.0 < seconds < 9.0, "about 14 words of speech"
    assert np.abs(x).max() > 0.05 and np.sqrt(np.mean(x**2)) > 0.005, "not silent"
    assert np.abs(x).max() <= 1.0


@pytest.mark.slow
@pytest.mark.parametrize("voice", ["Aaron", "Ana"])
def test_real_pocket_tts(voice):
    engine = engine_named("Pocket-TTS")
    assert engine.available() == (True, "")
    _assert_speech(*_speed(engine, voice))
