"""Export a book's audio as one MP3 with chapter markers.

A book is hours of audio (a 10-hour book is ~1.7 GB of PCM), so it is never
held in memory: blocks are read from the AudioStore one at a time, in reading
order, and piped straight into ffmpeg's LAME encoder. The book's title and
author become ID3 tags and its table of contents becomes ID3 chapters, so
Apple Books, podcast apps and car stereos can jump between chapters.

Rules:
- Every ordered block must have audio (ValueError otherwise): export is
  offered once generation reaches 100%.
- A chapter starts at the first ordered block at or after its TOC entry's
  block (a heading that isn't read aloud still marks where its chapter
  starts) and ends where the next one starts, or at the end. An entry with no
  audio at or after it (an index at the back) gets no chapter.
- ffmpeg writes "<path>.partial", renamed over path only when it's done: a
  cancelled or failed export never leaves a half-written MP3 behind, nor
  replaces an older one.
- cancelled() is asked before every block; cancelling kills ffmpeg, removes
  the partial file and raises ExportCancelled.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import dsp
from .audio_store import AudioStore
from .book import Book
from .timeline import Timeline


class ExportCancelled(Exception):
    """The export was stopped; nothing was written."""


def _never() -> bool:
    return False


def export_mp3(book: Book, store: AudioStore, order: list[int], path: Path, *, bitrate: str = "96k",
               progress: Callable[[float], None] = lambda fraction: None,
               cancelled: Callable[[], bool] = _never) -> None:
    """Encode the audio of the ordered blocks to an MP3 at path, with tags and chapters.
    progress(fraction) follows the encoding (0..1)."""
    path = Path(path)
    if not order:
        raise ValueError("This book has no audio to export")
    durations = store.durations()
    missing = [b for b in order if b not in durations]
    if missing:
        raise ValueError(f"{len(missing)} of {len(order)} blocks have no audio yet (the first is block {missing[0]})")
    total_ms = sum(durations[b] for b in order)
    chapters = _chapters(book, Timeline(order, {b: durations[b] for b in order}), total_ms)
    partial = path.with_name(path.name + ".partial")
    with tempfile.TemporaryDirectory(prefix="livrosound-export-") as tmp:
        metadata = Path(tmp) / "metadata.txt"
        metadata.write_text(_ffmetadata(book, chapters), encoding="utf-8")
        log_path = Path(tmp) / "ffmpeg.log"
        pcm_format = ["-f", "s16le", "-ar", str(dsp.SAMPLE_RATE), "-ac", "1"]
        command = [
            dsp.ffmpeg_binary(), "-v", "error", "-y",
            *pcm_format, "-i", "pipe:0",
            "-f", "ffmetadata", "-i", str(metadata),
            "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1",
            "-c:a", "libmp3lame", "-b:a", bitrate, "-ac", "1", "-id3v2_version", "3",
            "-f", "mp3", str(partial),
        ]
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
        try:
            try:
                done_ms = 0
                for block in order:
                    if cancelled():
                        raise ExportCancelled()
                    pcm = store.pcm(block)
                    if pcm is None:  # the store was reset meanwhile
                        raise ValueError(f"block {block} has no audio")
                    proc.stdin.write(pcm)
                    done_ms += durations[block]
                    progress(done_ms / total_ms if total_ms else 1.0)
                proc.stdin.close()
            except BrokenPipeError:
                pass  # ffmpeg quit early: its exit code and log say why
            if proc.wait():
                message = log_path.read_text(encoding="utf-8", errors="replace").strip()[-500:]
                raise RuntimeError(f"ffmpeg couldn't write the MP3: {message}")
        except BaseException:
            proc.kill()
            with contextlib.suppress(OSError):
                proc.stdin.close()  # unflushed audio can't reach a killed ffmpeg
            proc.wait()
            partial.unlink(missing_ok=True)
            raise
    os.replace(partial, path)


def _chapters(book: Book, timeline: Timeline, total_ms: int) -> list[tuple[int, int, str]]:
    """(start_ms, end_ms, title) per TOC entry that has audio at or after it, in time order.
    An entry for front/back matter that is never read (Acknowledgments, an index) gets no
    chapter: it would only label the audio of whatever follows it."""
    starts = []
    for entry in book.toc:
        if 0 <= entry.block < len(book.blocks) and book.blocks[entry.block].skipped:
            continue
        start = timeline.start_ms_of(entry.block)
        if start is not None and start < total_ms:
            starts.append((start, entry.title))
    starts.sort(key=lambda s: s[0])
    if starts and starts[0][0] > 0:
        # The opening (the title announcement, an epigraph) belongs to the first chapter
        # rather than to none: players show "no chapter" for audio before the first mark.
        starts[0] = (0, starts[0][1])
    ends = [start for start, _ in starts[1:]] + [total_ms]
    return [(start, end, title) for (start, title), end in zip(starts, ends)]


def _ffmetadata(book: Book, chapters: list[tuple[int, int, str]]) -> str:
    """ffmpeg's metadata file: the book's tags, then one [CHAPTER] section per chapter."""
    lines = [";FFMETADATA1", f"title={_escape(book.title)}", f"album={_escape(book.title)}"]
    if book.author.strip():
        lines.append(f"artist={_escape(book.author)}")
    for start, end, title in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={_escape(title)}"]
    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    """One line, with ffmetadata's special characters (= ; # \\) backslash-escaped."""
    return re.sub(r"([=;#\\])", r"\\\1", " ".join(value.split()))
