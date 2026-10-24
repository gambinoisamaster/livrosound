"""Pocket-TTS (Kyutai, MIT license): the default engine. Runs on the CPU.

Every narrator is cloned from its sample in assets/voices/ (voices.py).
Pocket-TTS conditions on the sample's speaker, style and prosody, so a clean
5-20 s recording is enough. Its own predefined voices (alba, javert, ...) are
never offered: LivroSound reads only with the user's cloned voices.

Loading the model and turning a sample into a voice state are slow, so both
happen once, on first use, and every voice state is kept for the session.
Importing pocket_tts (and torch) takes seconds, so available() only checks the
packages are installed; the import happens with the first synthesis.

The clip comes back raw. A Pocket-TTS clip can end on a short cut-off sound
after the last word, heard as a "tick" where two chunks meet; the generator
removes it (dsp.trim_speech) and ramps the edges (dsp.declick) before joining.

The cloning weights are gated on Hugging Face. When they can't be loaded,
pocket_tts quietly falls back to a model that can't clone; that is reported as
an error rather than reading with a voice the user never chose.

Requires torch>=2.5, which has no Intel-Mac wheels: there this engine reports
itself unavailable.
"""

from __future__ import annotations

import importlib.util
import threading

import numpy as np

from .. import voices
from .base import TTSEngine


class PocketTTSEngine(TTSEngine):
    name = "Pocket-TTS"
    install_hint = "Install with: uv sync"

    def __init__(self) -> None:
        self._model = None
        self._voice_states: dict[str, object] = {}
        # Generation and Choose a Voice previews can ask for speech at the same
        # time from different threads; the model takes one request at a time.
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        missing = [m for m in ("pocket_tts", "torch") if importlib.util.find_spec(m) is None]
        if missing:
            return False, (
                f"Pocket-TTS is not installed ({', '.join(missing)} missing; it needs "
                f"torch>=2.5, which isn't available on Intel Macs). {self.install_hint}"
            )
        return True, ""

    def voices(self) -> list[str]:
        return [v.name for v in voices.catalog()]

    def synthesize(self, voice: str, text: str) -> tuple[np.ndarray, int]:
        with self._lock:
            model = self._ensure_model()
            audio = model.generate_audio(self._state_for(voice), text)
            rate = int(model.sample_rate)
        samples = np.asarray(audio.numpy(), dtype=np.float32).reshape(-1)
        return np.clip(samples, -1.0, 1.0), rate

    def _ensure_model(self):
        if self._model is None:
            from pocket_tts import TTSModel

            model = TTSModel.load_model()
            if not model.has_voice_cloning:
                raise RuntimeError(
                    "Pocket-TTS couldn't load its voice-cloning weights. Accept the terms at "
                    "https://huggingface.co/kyutai/pocket-tts, sign in with `uvx hf auth login`, "
                    "then open LivroSound once while online to download them."
                )
            self._model = model
        return self._model

    def _state_for(self, name: str):
        if name not in self._voice_states:
            voice = voices.voice_named(name)
            if voice is None:
                raise ValueError(f"No voice named {name!r} in {voices.VOICES_DIR}")
            self._voice_states[name] = self._model.get_state_for_audio_prompt(voice.wav)
        return self._voice_states[name]
