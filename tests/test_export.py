"""MP3 export: the book's audio in reading order, its tags and one chapter per TOC entry,
written atomically (a cancelled or failed export leaves nothing behind)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from livrosound import dsp
from livrosound.audio_store import AudioStore
from livrosound.book import Block, Book, TocEntry
from livrosound.export import ExportCancelled, export_mp3


def _ffprobe() -> str | None:
    beside = Path(dsp.ffmpeg_binary()).with_name("ffprobe")
    return shutil.which("ffprobe") or (str(beside) if beside.is_file() else None)


pytestmark = pytest.mark.skipif(_ffprobe() is None, reason="ffmpeg/ffprobe isn't installed")

# Block index -> its audio's length in ms (blocks 0 and 3 aren't read: no audio).
DURATIONS = {1: 1500, 2: 4200, 4: 2600, 5: 1200, 6: 3100}
ORDER = sorted(DURATIONS)


def make_book(title="The Harbour", author="Ann Author", toc=None) -> Book:
    blocks = [
        Block("heading", "Contents", level=1, role="contents"),
        Block("heading", "The Harbour", level=1, role="title"),
        Block("paragraph", "Opening words."),
        Block("heading", "Part One", level=1),  # not read (read_headings off): its chapter starts at 4
        Block("paragraph", "The first part."),
        Block("separator", ""),
        Block("paragraph", "More of the first part."),
        Block("heading", "Index", level=1, role="index"),
    ]
    if toc is None:
        toc = [TocEntry("The Harbour", 1), TocEntry("Part One", 3), TocEntry("A Pause", 5), TocEntry("Index", 7)]
    return Book("exp", title, author, "", "txt", blocks=blocks, toc=toc)


def tone(ms: int, freq: float) -> bytes:
    t = np.arange(dsp.samples_of(ms)) / dsp.SAMPLE_RATE
    return dsp.to_pcm16(0.3 * np.sin(2 * np.pi * freq * t))


@pytest.fixture
def store(tmp_path):
    s = AudioStore(tmp_path / "audio.sqlite")
    for k, (block, ms) in enumerate(DURATIONS.items()):
        s.put(block, tone(ms, 220 + 110 * k), [])
    yield s
    s.close()


def probe(path: Path) -> dict:
    result = subprocess.run(
        [_ffprobe(), "-v", "error", "-show_format", "-show_chapters", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def test_export_writes_tags_chapters_and_the_whole_book(store, tmp_path):
    path = tmp_path / "out" / "The Harbour.mp3"
    path.parent.mkdir()
    fractions = []
    export_mp3(make_book(), store, ORDER, path, progress=fractions.append)
    assert path.is_file() and not path.with_name(path.name + ".partial").exists()
    info = probe(path)
    tags = {k.lower(): v for k, v in info["format"]["tags"].items()}
    assert (tags["title"], tags["artist"], tags["album"]) == ("The Harbour", "Ann Author", "The Harbour")
    assert float(info["format"]["duration"]) * 1000 == pytest.approx(sum(DURATIONS.values()), abs=100)

    chapters = [(round(float(c["start_time"]) * 1000), round(float(c["end_time"]) * 1000), c["tags"]["title"])
                for c in info["chapters"]]
    # "The Harbour" -> block 1 (0 ms); "Part One" (block 3, unread) -> block 4 (5700 ms);
    # "A Pause" -> block 5 (8300 ms); "Index" (after the last block with audio) -> no chapter.
    assert chapters == [(0, 5700, "The Harbour"), (5700, 8300, "Part One"), (8300, 12600, "A Pause")]

    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert len(fractions) == len(ORDER)


def test_audio_is_in_reading_order(store, tmp_path):
    """Each block is a tone of its own pitch: decode the MP3 and find them in order."""
    path = tmp_path / "book.mp3"
    export_mp3(make_book(), store, ORDER, path)
    pcm = subprocess.run(
        [dsp.ffmpeg_binary(), "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "24000", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    x = dsp.from_pcm16(pcm)
    start = 0
    for k, (block, ms) in enumerate(DURATIONS.items()):
        middle = x[dsp.samples_of(start + ms / 2 - 100) : dsp.samples_of(start + ms / 2 + 100)]
        spectrum = np.abs(np.fft.rfft(middle))
        peak = np.fft.rfftfreq(len(middle), 1 / dsp.SAMPLE_RATE)[spectrum.argmax()]
        assert peak == pytest.approx(220 + 110 * k, abs=10), block
        start += ms


def test_special_characters_in_tags_survive(store, tmp_path):
    path = tmp_path / "odd.mp3"
    title = "Love; War = Peace #1 \\ Part"
    book = make_book(title=title, author="", toc=[TocEntry("Chapter 1: A=B; C#", 1)])
    export_mp3(book, store, ORDER, path)
    info = probe(path)
    tags = {k.lower(): v for k, v in info["format"]["tags"].items()}
    assert tags["title"] == title and "artist" not in tags
    assert [c["tags"]["title"] for c in info["chapters"]] == ["Chapter 1: A=B; C#"]
    assert round(float(info["chapters"][0]["end_time"]) * 1000) == sum(DURATIONS.values())


def test_a_book_without_contents_has_no_chapters(store, tmp_path):
    path = tmp_path / "plain.mp3"
    export_mp3(make_book(toc=[]), store, ORDER, path)
    assert probe(path)["chapters"] == []


def test_contents_entries_for_unread_front_matter_get_no_chapter(store, tmp_path):
    """Artful Sentences' contents list "Acknowledgments", which is never read: as a chapter
    it would only label the epigraphs that follow it."""
    path = tmp_path / "front.mp3"
    toc = [TocEntry("Contents", 0), TocEntry("Part One", 3)]
    export_mp3(make_book(toc=toc), store, ORDER, path)
    chapters = probe(path)["chapters"]
    assert [c["tags"]["title"] for c in chapters] == ["Part One"]
    assert float(chapters[0]["start_time"]) == 0, "the opening before the first entry joins the first chapter"


def test_cancel_removes_the_partial_file_and_keeps_an_older_export(store, tmp_path):
    path = tmp_path / "book.mp3"
    path.write_bytes(b"an older export")
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) >= 3

    with pytest.raises(ExportCancelled):
        export_mp3(make_book(), store, ORDER, path, cancelled=cancelled)
    assert path.read_bytes() == b"an older export"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audio.sqlite", "audio.sqlite-shm", "audio.sqlite-wal",
                                                         "book.mp3"]


def test_a_block_without_audio_is_refused(store, tmp_path):
    path = tmp_path / "book.mp3"
    with pytest.raises(ValueError, match="block 3"):
        export_mp3(make_book(), store, [1, 2, 3, 4], path)
    with pytest.raises(ValueError):
        export_mp3(make_book(), store, [], path)
    assert list(tmp_path.glob("book.mp3*")) == []


def test_an_ffmpeg_failure_is_reported_and_leaves_nothing(store, tmp_path):
    path = tmp_path / "book.mp3"
    with pytest.raises(RuntimeError, match="ffmpeg"):
        export_mp3(make_book(), store, ORDER, path, bitrate="not-a-bitrate")
    assert list(tmp_path.glob("book.mp3*")) == []
