"""End-to-end smoke test: import a book, read the start of it aloud, export an MP3.

Usage: uv run python scripts/smoke_test.py <book> [--engine fake|pocket] [--blocks N]
                                              [--voice NAME] [--out FILE]

Runs the app's pipeline without the UI: ingest.load_book (prints the title,
author, table of contents, the front/back matter that is skipped, and what the
narrator says first), Generator.run from the beginning of the book for the
first N blocks that get audio, a check of their cues, then export_mp3 of those
blocks with ID3 chapters. Prints timings for every step.

Everything is made in a temporary cache (LIVROSOUND_CACHE), so the library,
settings and audio in cache/ are never touched; only the MP3 is kept
(cache/smoke_test.mp3 unless --out says otherwise).

--engine fake (the default) "speaks" every word as a short tone: no model, done
in a second or two. --engine pocket reads with Pocket-TTS and a cloned voice
(Aaron unless --voice): ~7 s to load, then about 4x faster than real time.

For the unit tests, run: uv run pytest
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np

from livrosound import dsp, paths, speech, voices
from livrosound.audio_store import AudioStore
from livrosound.book import TITLE_ROLE, Book
from livrosound.engines import engine_named
from livrosound.engines.base import TTSEngine
from livrosound.export import export_mp3
from livrosound.generation import GenerationCancelled, Generator, stream_ready
from livrosound.ingest import BookImportError, load_book

TOC_SHOWN = 15
PREVIEW_CHARS = 90


class ToneEngine(TTSEngine):
    """Every word is a 120 ms tone (a pitch per voice) and a 40 ms gap: audio shaped
    like speech, made instantly, so the pipeline can be checked without a model."""

    name = "Tone"
    RATE = 22050  # not 24 kHz, so the resampling step runs as it does for a real engine

    def available(self):
        return True, ""

    def voices(self):
        return [v.name for v in voices.catalog()] or [voices.DEFAULT_VOICE]

    def synthesize(self, voice, text):
        freq = 180 + sum(map(ord, voice)) % 200  # stable across runs, unlike hash()
        t = np.arange(int(0.12 * self.RATE)) / self.RATE
        word = np.concatenate([0.3 * np.sin(2 * np.pi * freq * t), np.zeros(int(0.04 * self.RATE))])
        return np.tile(word, max(1, len(text.split()))).astype(np.float32), self.RATE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a book, make its first blocks' audio, export an MP3.")
    parser.add_argument("book", type=Path, help="a PDF, EPUB, DOCX or TXT file")
    parser.add_argument("--engine", choices=("fake", "pocket"), default="fake")
    parser.add_argument("--blocks", type=int, default=5, metavar="N", help="blocks to generate (default 5)")
    parser.add_argument("--voice", default=voices.DEFAULT_VOICE)
    parser.add_argument("--out", type=Path, default=ROOT / "cache" / "smoke_test.mp3", help="where the MP3 goes")
    args = parser.parse_args(argv)

    engine = _engine(args.engine, args.voice)
    out = args.out.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="livrosound-smoke-") as cache:
        os.environ["LIVROSOUND_CACHE"] = cache  # paths.cache_dir() reads it on every call
        book = _import(args.book)
        store = AudioStore(paths.book_dir(book.book_id) / "audio.sqlite")
        try:
            made = _generate(book, store, engine, args.voice, max(1, args.blocks))
            _export(book, store, made, out)
        finally:
            store.close()
    print("OK")
    return 0


def _engine(choice: str, voice: str) -> TTSEngine:
    engine = ToneEngine() if choice == "fake" else engine_named("Pocket-TTS")
    ok, why = engine.available()
    if not ok:
        sys.exit(why)
    if voice not in engine.voices():
        sys.exit(f"No voice named {voice!r}. Voices: {', '.join(engine.voices())}")
    return engine


def _import(path: Path) -> Book:
    last = [0.0]

    def progress(done: int, total: int, message: str) -> None:
        now = time.perf_counter()
        if done in (0, total) or now - last[0] >= 2:  # a long PDF reports every page
            last[0] = now
            print(f"  {message} ({done}/{total})", flush=True)

    start = time.perf_counter()
    try:
        book = load_book(path, progress=progress)
    except BookImportError as e:
        sys.exit(f"Import failed: {e}")
    print(f"imported in {time.perf_counter() - start:.1f}s")
    print(f"title: {book.title}")
    print(f"author: {book.author or '(none)'}")
    pages = f", {len(book.pages)} pages" if book.pages else ""
    print(f"format: {book.source_kind}{pages}, {len(book.blocks)} blocks")

    print(f"contents: {len(book.toc)} entries")
    for entry in book.toc[:TOC_SHOWN]:
        print(f"  {'  ' * (entry.level - 1)}{entry.title}  -> block {entry.block}")
    if len(book.toc) > TOC_SHOWN:
        print(f"  … and {len(book.toc) - TOC_SHOWN} more")

    roles = Counter(b.role for b in book.blocks if b.skipped)
    if roles:
        listed = ", ".join(f"{role} {n}" for role, n in roles.most_common())
        print(f"never read (front/back matter): {_count(sum(roles.values()), 'block')} ({listed})")
    title_blocks = [i for i, b in enumerate(book.blocks) if b.role == TITLE_ROLE]
    assert len(title_blocks) <= 1, f"more than one title announcement: blocks {title_blocks}"
    linked = sum(1 for b in book.blocks if b.link >= 0)
    if linked:
        print(f"contents-page lines linked to their chapters: {linked}")
    return book


def _generate(book: Book, store: AudioStore, engine: TTSEngine, voice: str, n: int) -> list[int]:
    """Generate audio from the beginning until n blocks are made; returns them in order."""
    options = speech.SpeechOptions()
    gen = Generator(book, store, engine, voice, options)
    order = gen.order()
    assert order, "nothing in this book is read aloud"
    assert not any(book.blocks[b].skipped for b in order), "front/back matter would be read"
    n = min(n, len(order))

    print(f"first blocks read aloud ({len(order)} in all):")
    for b in order[:n]:
        block = book.blocks[b]
        said = " ".join(c.text for c in speech.block_chunks(book, b, options)) or "(a pause)"
        role = f" {block.role}" if block.role else ""
        print(f"  [{b}] {block.kind}{role}: {_short(said)}")

    print(f"generating {_count(n, 'block')} with {engine.name} · {voice} (the first loads the model)…", flush=True)
    made: list[int] = []
    start = last = time.perf_counter()

    def on_block(block: int, duration_ms: int) -> None:
        nonlocal last
        now = time.perf_counter()
        print(f"  block {block}: {duration_ms / 1000:.1f}s of audio in {now - last:.1f}s", flush=True)
        made.append(block)
        last = now

    try:
        gen.run(order[0], on_block=on_block, cancelled=lambda: len(made) >= n)
    except GenerationCancelled:
        pass  # stopped on purpose after n blocks
    wall = time.perf_counter() - start
    audio_s = sum(store.durations()[b] for b in made) / 1000
    speed = f" ({audio_s / wall:.1f}x real time)" if wall > 0 else ""
    print(f"generated {audio_s:.1f}s of audio in {wall:.1f}s{speed}")
    ready = stream_ready(order, store.generated(), order[0], gen.weights())
    print(f"book done: {gen.fraction_done():.1%} · ready to stream (5% rule): {'yes' if ready else 'not yet'}")

    assert made == order[:n], f"blocks made out of order: {made}"
    for b in made:
        _check_cues(book, store, b)
    return made


def _check_cues(book: Book, store: AudioStore, block: int) -> None:
    """Cues are in time order, inside the block's audio, and highlight the block's own text."""
    info = store.info(block)
    assert info is not None and info.duration_ms > 0, f"block {block} has no audio"
    text_len = len(book.blocks[block].text)
    end_ms = end_char = 0
    for cue in info.cues:
        assert end_ms <= cue.start_ms < cue.end_ms <= info.duration_ms, f"block {block}: bad cue time {cue}"
        assert end_char <= cue.start_char < cue.end_char <= text_len, f"block {block}: bad cue span {cue}"
        end_ms, end_char = cue.end_ms, cue.end_char


def _export(book: Book, store: AudioStore, made: list[int], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    export_mp3(book, store, made, out)
    size_kb = out.stat().st_size / 1024
    print(f"exported {size_kb:.0f} KB in {time.perf_counter() - start:.1f}s -> {out}")
    chapters = _chapters(out)
    if chapters is not None:
        titles = ", ".join(chapters[:5]) + (", …" if len(chapters) > 5 else "")
        print(f"MP3 chapters: {len(chapters)}" + (f" ({titles})" if chapters else ""))


def _chapters(mp3: Path) -> list[str] | None:
    """The chapter titles ffprobe finds in the MP3 (None without ffprobe)."""
    beside = Path(dsp.ffmpeg_binary()).with_name("ffprobe")
    ffprobe = shutil.which("ffprobe") or (str(beside) if beside.is_file() else None)
    if ffprobe is None:
        return None
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_chapters", "-of", "json", str(mp3)], capture_output=True, text=True
    )
    if result.returncode:
        return None
    return [c.get("tags", {}).get("title", "") for c in json.loads(result.stdout).get("chapters", [])]


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


if __name__ == "__main__":
    sys.exit(main())
