"""The narrator voices: the user's own cloned recordings in assets/voices/.

Each voice is NAME.wav (a clean 5-20 s recording of one speaker) plus
NAME.wav.txt (what that recording says, kept with it for engines that clone from text too).
The file name is the voice's name everywhere in the app, so a voice is added by
dropping in the two files. Any recording level works: generated audio is
leveled on output (loudness.py).

voices.json adds what a .wav can't say: gender and age, so Choose a Voice can
group the narrators (Women, Men) and order each group young -> adult -> mature.
FEATURED (Aaron, the default, and Ana) head the list in a group of their own
(user requirement: "Aaron and Ana must be at top of the list").
A sample without an entry still appears, under "Other". Keys starting with "_"
are comments.

Only these cloned voices are ever offered. Pocket-TTS ships voices of its own
(alba, javert, ...); the engine never lists them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import paths

DEFAULT_VOICE = "Aaron"

VOICES_DIR = paths.VOICES_DIR  # read at call time, so a test can point it elsewhere
METADATA_FILE = "voices.json"

FEATURED = ("Aaron", "Ana")
FEATURED_GROUP = "Recommended"
GROUPS = (("female", "Women"), ("male", "Men"))
OTHER_GROUP = "Other"
AGE_ORDER = ("young", "adult", "mature")  # unknown ages sort after these


@dataclass
class Voice:
    name: str
    gender: str  # "female" | "male" | "" (no metadata)
    age: str  # "young" | "adult" | "mature" | ""
    wav: Path
    transcript: str  # what the sample says ("" if NAME.wav.txt is missing)


def catalog() -> list[Voice]:
    """Every NAME.wav in VOICES_DIR, by name."""
    folder = VOICES_DIR
    if not folder.is_dir():
        return []
    meta = _metadata(folder / METADATA_FILE)
    voices = []
    for wav in sorted(folder.glob("*.wav"), key=lambda p: p.stem.lower()):
        if wav.name.startswith("."):
            continue
        info = meta.get(wav.stem, {})
        voices.append(
            Voice(
                name=wav.stem,
                gender=str(info.get("gender", "")).strip().lower(),
                age=str(info.get("age", "")).strip().lower(),
                wav=wav,
                transcript=_transcript(wav),
            )
        )
    return voices


def voice_named(name: str) -> Voice | None:
    return next((v for v in catalog() if v.name == name), None)


def grouped() -> list[tuple[str, list[Voice]]]:
    """[("Recommended", [Aaron, Ana]), ("Women", ...), ("Men", ...), ("Other", ...)],
    empty groups left out; each gender group ordered young -> adult -> mature, then by name."""
    voices = catalog()
    featured = [v for name in FEATURED for v in voices if v.name == name]
    voices = [v for v in voices if v not in featured]
    known = {gender for gender, _ in GROUPS}
    groups = [(FEATURED_GROUP, featured)]
    groups += [(label, [v for v in voices if v.gender == gender]) for gender, label in GROUPS]
    groups.append((OTHER_GROUP, [v for v in voices if v.gender not in known]))
    return [
        (label, members if label == FEATURED_GROUP else sorted(members, key=_age_then_name))
        for label, members in groups
        if members
    ]


def _age_then_name(voice: Voice) -> tuple[int, str]:
    rank = AGE_ORDER.index(voice.age) if voice.age in AGE_ORDER else len(AGE_ORDER)
    return rank, voice.name.lower()


def _metadata(path: Path) -> dict[str, dict]:
    """voices.json without its comment keys; a missing or broken file is just no metadata."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def _transcript(wav: Path) -> str:
    try:
        return " ".join(wav.with_name(wav.name + ".txt").read_text(encoding="utf-8").split())
    except (OSError, ValueError):  # missing, or not UTF-8
        return ""
