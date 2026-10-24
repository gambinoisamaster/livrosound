"""The helper scripts in scripts/ run end to end: smoke_test.py imports a tiny TXT,
makes its first blocks' audio with the fake engine and exports an MP3 with
chapters, all in its own temporary cache; make_voice_previews.py renders one
clip per voice and skips the ones already made."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from livrosound import dsp, voice_preview
from tests.fakes import FakeEngine

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

BOOK = """THE LIGHTHOUSE

by Ana Lima


CHAPTER I

The keeper climbed the stairs at dusk and lit the lamp. Below him the sea moved like a slow animal.

He did not look at it. In 1914 his father had looked at it too long.

CHAPTER II

The storm came in April and took the boat before anyone was awake.
"""


def _has_ffmpeg() -> bool:
    try:
        ffmpeg = dsp.ffmpeg_binary()
    except FileNotFoundError:
        return False
    return bool(shutil.which("ffprobe") or Path(ffmpeg).with_name("ffprobe").is_file())


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _smoke(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "smoke_test.py"), *args],
        capture_output=True, text=True, timeout=180, cwd=ROOT,
    )


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe isn't installed")
def test_smoke_test_reads_a_small_book_with_the_fake_engine(tmp_path, isolated_cache):
    book = tmp_path / "lighthouse.txt"
    book.write_text(BOOK, encoding="utf-8")
    mp3 = tmp_path / "out" / "lighthouse.mp3"

    result = _smoke(str(book), "--engine", "fake", "--blocks", "4", "--out", str(mp3))

    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert "title: The Lighthouse" in out and "author: Ana Lima" in out
    assert "Chapter I" in out  # the table of contents
    assert "The Lighthouse, by Ana Lima." in out  # the title is announced once, first
    assert "In nineteen fourteen" in out  # what the narrator says, not what the page says
    assert out.count("  block ") == 4
    assert "MP3 chapters: 1 (Chapter I)" in out
    assert out.rstrip().endswith("OK")
    assert mp3.stat().st_size > 0
    assert not (isolated_cache / "books").exists()  # its own temporary cache, never the library's


def test_smoke_test_reports_a_file_it_cant_open(tmp_path):
    notes = tmp_path / "notes.rtf"
    notes.write_text("{\\rtf1 hello}", encoding="utf-8")

    result = _smoke(str(notes), "--out", str(tmp_path / "x.mp3"))

    assert result.returncode != 0
    assert "Import failed: LivroSound can't open .rtf files" in result.stderr
    assert not (tmp_path / "x.mp3").exists()


def test_make_voice_previews_renders_each_clip_once(monkeypatch, capsys):
    script = _script("make_voice_previews")
    engine = FakeEngine(voices=("Aaron", "Ana"))
    monkeypatch.setattr(script, "load_engines", lambda: [engine])

    assert script.main([]) == 0
    assert "Done: 2 new clip(s)." in capsys.readouterr().out
    assert all(voice_preview.preview_path("Fake", v).is_file() for v in ("Aaron", "Ana"))
    assert len(engine.calls) == 2

    assert script.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("already made") == 2 and "Done: 0 new clip(s)." in out
    assert len(engine.calls) == 2  # nothing synthesized again


def test_make_voice_previews_can_pick_one_engine(monkeypatch, capsys):
    script = _script("make_voice_previews")
    fake, other = FakeEngine(voices=("Aaron",)), FakeEngine(voices=("Ana",))
    other.name = "Other"
    monkeypatch.setattr(script, "load_engines", lambda: [fake, other])

    assert script.main(["--engine", "other"]) == 0
    assert fake.calls == [] and len(other.calls) == 1
    with pytest.raises(SystemExit, match="Missing isn't available"):
        script.main(["--engine", "Missing"])
