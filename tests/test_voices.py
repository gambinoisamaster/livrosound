"""The narrator catalog: the user's cloned voices, by proper name, grouped for Choose a Voice."""

import json
import re

import pytest

from livrosound import paths, voices
from livrosound.voices import DEFAULT_VOICE, catalog, grouped, voice_named

CLONED = [
    "Aaron", "Ana", "Anne", "Connor", "Danny", "Emily", "Ethan", "Gertrude", "Gigi",
    "Johnny", "Kyle", "Liz", "Matthew", "Ronald", "Ruth", "Susan", "Therese",
]


def test_the_catalog_is_the_seventeen_cloned_voices():
    names = [v.name for v in catalog()]
    assert names == CLONED
    assert all(re.fullmatch(r"[A-Z][a-z]+", name) for name in names), "proper names only"
    for voice in catalog():
        assert voice.wav == paths.VOICES_DIR / f"{voice.name}.wav" and voice.wav.is_file()
        assert voice.transcript and "\n" not in voice.transcript
        assert voice.gender in ("female", "male") and voice.age in ("young", "adult", "mature")


def test_default_narrators():
    assert DEFAULT_VOICE == "Aaron"
    aaron, ana = voice_named("Aaron"), voice_named("Ana")
    assert (aaron.gender, aaron.age) == ("male", "adult")
    assert (ana.gender, ana.age) == ("female", "adult")
    assert voice_named("Default_Narrator_Male") is None, "Script Radio's file names are gone"
    assert voice_named("alba") is None, "Pocket-TTS's own voices are never offered"


def test_grouped_aaron_and_ana_first_then_women_then_men_young_to_mature():
    groups = dict(grouped())
    assert [label for label, _ in grouped()] == ["Recommended", "Women", "Men"]
    assert [v.name for v in groups["Recommended"]] == ["Aaron", "Ana"]  # the user's rule: at the top
    assert [v.name for v in groups["Women"]] == [
        "Emily", "Gigi", "Liz", "Ruth", "Anne", "Susan", "Therese", "Gertrude",
    ]
    assert [v.name for v in groups["Men"]] == [
        "Danny", "Kyle", "Connor", "Ethan", "Johnny", "Matthew", "Ronald",
    ]


@pytest.fixture
def folder(tmp_path, monkeypatch):
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    monkeypatch.setattr(voices, "VOICES_DIR", voices_dir)
    return voices_dir


def _add(folder, name, transcript="Hello there."):
    (folder / f"{name}.wav").write_bytes(b"RIFF")
    if transcript is not None:
        (folder / f"{name}.wav.txt").write_text(transcript, encoding="utf-8")


def test_metadata_is_optional(folder):
    _add(folder, "Zed", transcript="  Line one\nline two.  ")
    _add(folder, "mia", transcript=None)
    _add(folder, "Bea")
    (folder / ".Hidden.wav").write_bytes(b"RIFF")
    (folder / "notes.txt").write_text("not a voice")
    (folder / "voices.json").write_text(json.dumps({
        "_comment": {"gender": "male"},
        "Bea": {"gender": "Female", "age": "Mature"},
        "Ghost": {"gender": "male", "age": "adult"},  # no sample: not a voice
    }))
    assert [v.name for v in catalog()] == ["Bea", "mia", "Zed"]
    zed = voice_named("Zed")
    assert zed.transcript == "Line one line two."  # one line, like Script Radio's cleaned transcripts
    assert (zed.gender, zed.age) == ("", "")
    assert voice_named("mia").transcript == ""
    assert voice_named("Bea").gender == "female" and voice_named("Bea").age == "mature"
    assert voice_named("Ghost") is None
    assert [(label, [v.name for v in vs]) for label, vs in grouped()] == [
        ("Women", ["Bea"]),
        ("Other", ["mia", "Zed"]),
    ]


def test_unknown_ages_sort_last(folder):
    for name in ("Old", "Kid", "Odd", "Mid"):
        _add(folder, name)
    (folder / "voices.json").write_text(json.dumps({
        "Old": {"gender": "male", "age": "mature"},
        "Kid": {"gender": "male", "age": "young"},
        "Odd": {"gender": "male", "age": "ageless"},
        "Mid": {"gender": "male", "age": "adult"},
    }))
    assert [v.name for v in dict(grouped())["Men"]] == ["Kid", "Mid", "Old", "Odd"]


def test_broken_metadata_or_missing_folder(folder, monkeypatch):
    _add(folder, "Solo")
    (folder / "voices.json").write_text("{not json")
    assert [(label, [v.name for v in vs]) for label, vs in grouped()] == [("Other", ["Solo"])]
    (folder / "voices.json").write_text("[1, 2]")
    assert voice_named("Solo").gender == ""
    monkeypatch.setattr(voices, "VOICES_DIR", folder / "missing")
    assert catalog() == [] and grouped() == [] and voice_named("Solo") is None
