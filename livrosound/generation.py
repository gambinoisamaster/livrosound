"""Turn a book into audio, one block at a time, in the background.

A book is hours of audio, far too much to make before anyone listens. So each
block (a paragraph, a heading, a scene break) is rendered on its own and
stored whole in the book's AudioStore: playback can start as soon as enough
audio from the reader's starting point exists (the 5% rule, stream_ready)
while generation carries on, and a run that is cancelled or crashes keeps
every block it finished.

Rules:
- Only blocks speech.is_read() accepts get audio (order()), in reading order.
  A run can start anywhere (a click, a Contents pick): it goes to the end of
  the book, then wraps around to the beginning, skipping blocks that already
  have audio, so one full run always leaves the whole book ready.
- Progress and the 5% rule count characters of text (weights()), not blocks:
  a heading is a few characters, a paragraph hundreds. A separator counts 1.
- A block's audio is its lead-in silence, then for every chunk the engine's
  clip resampled to 24 kHz, trimmed to its speech (which drops Pocket-TTS's
  end-of-clip tick) and declicked, followed by the chunk's pause. The whole
  block is leveled at once: loudness.level only multiplies, so the declicked
  zero edges stay zero and no join between chunks can click.
- Cues are relative to the block's own audio, so any block can be made (or
  remade) without moving another.
- A separator is its 1200 ms lead-in of silence with no cues. A block that is
  read but turns out to have nothing to say is a paragraph pause, so no block
  on the timeline is ever empty.
- cancelled() is asked before every block and between chunks; a block is
  stored only once it's complete.
- With a checker (Whisper), a chunk whose transcript doesn't match its text is
  synthesized again, up to validate.MAX_ATTEMPTS takes; the best take is kept
  and the chunks that never passed come back as Issues.
- An engine error is retried once (a passing hiccup), then raised with the
  block index and the text it was reading.
"""

from __future__ import annotations

import functools
from bisect import bisect_left
from collections.abc import Callable

import numpy as np

from . import dsp, loudness, speech
from .audio_store import AudioStore, Cue
from .book import Book
from .engines.base import TTSEngine
from .speech import SpeechOptions
from .validate import MAX_ATTEMPTS, Issue, Transcriber

STREAM_THRESHOLD = 0.05  # Play enables once 5% of the book is ready from the start point


class GenerationCancelled(Exception):
    """The run was stopped; every block finished before it is kept."""


def _never() -> bool:
    return False


class Generator:
    def __init__(self, book: Book, store: AudioStore, engine: TTSEngine, voice: str,
                 options: SpeechOptions, checker: Transcriber | None = None):
        self.book = book
        self.store = store
        self.engine = engine
        self.voice = voice
        self.options = options
        self.checker = checker

    # is_read costs ~35 µs a block: worked out once per generator, not on every progress update.
    @functools.cached_property
    def _order(self) -> list[int]:
        return [i for i, block in enumerate(self.book.blocks) if speech.is_read(block, self.options)]

    @functools.cached_property
    def _weights(self) -> dict[int, int]:
        blocks = self.book.blocks
        return {i: 1 if blocks[i].kind == "separator" else max(1, len(blocks[i].text)) for i in self._order}

    @functools.cached_property
    def _total_weight(self) -> int:
        return sum(self._weights.values())

    def order(self) -> list[int]:
        """The blocks that get audio, in reading order."""
        return list(self._order)

    def weights(self) -> dict[int, int]:
        """Characters per ordered block (a separator counts 1)."""
        return dict(self._weights)

    def queue_from(self, start_block: int) -> list[int]:
        """What a run from start_block makes, in order: the ordered blocks from the first
        one at or after start_block to the end, then from the beginning; generated ones skipped."""
        k = bisect_left(self._order, start_block)
        generated = self.store.generated()
        return [b for b in self._order[k:] + self._order[:k] if b not in generated]

    def fraction_done(self) -> float:
        """Share of the book's characters that have audio (1.0 for a book with nothing to read)."""
        if not self._total_weight:
            return 1.0
        return sum(self._weights.get(b, 0) for b in self.store.generated()) / self._total_weight

    def render_block(self, index: int, *, cancelled: Callable[[], bool] = _never
                     ) -> tuple[bytes, list[Cue], list[Issue]]:
        """A block's audio (16-bit mono PCM at 24 kHz), its cues, and the chunks that failed
        the Whisper check. Raises GenerationCancelled between chunks when cancelled()."""
        if index not in self._weights:
            raise ValueError(f"block {index} isn't read aloud")
        block = self.book.blocks[index]
        chunks = speech.block_chunks(self.book, index, self.options)
        parts = [dsp.silence(speech.lead_in_ms(block, self.options))]  # a separator is only this
        length = len(parts[0])
        cues: list[Cue] = []
        issues: list[Issue] = []
        for chunk in chunks:
            if cancelled():
                raise GenerationCancelled()
            x, issue = self._voice(index, chunk.text, cancelled)
            if issue:
                issues.append(issue)
            cues.append(Cue(dsp.ms_of(length), dsp.ms_of(length + len(x)), chunk.start, chunk.end))
            pause = dsp.silence(chunk.pause_after_ms)
            parts += [x, pause]
            length += len(x) + len(pause)
        if not chunks and block.kind != "separator":
            parts.append(dsp.silence(self.options.paragraph_pause_ms))
        audio = loudness.level(np.concatenate(parts), dsp.SAMPLE_RATE)
        return dsp.to_pcm16(audio), cues, issues

    def run(self, start_block: int, *,
            on_block: Callable[[int, int], None] = lambda block, duration_ms: None,
            on_status: Callable[[str], None] = lambda message: None,
            cancelled: Callable[[], bool] = _never) -> list[Issue]:
        """Make every missing block, starting at start_block and wrapping around (queue_from).
        on_block(block, duration_ms) follows each store.put. Returns the Whisper issues."""
        return self.run_blocks(self.queue_from(start_block), on_block=on_block, on_status=on_status,
                               cancelled=cancelled)

    def run_blocks(self, blocks: list[int], *,
                   on_block: Callable[[int, int], None] = lambda block, duration_ms: None,
                   on_status: Callable[[str], None] = lambda message: None,
                   cancelled: Callable[[], bool] = _never) -> list[Issue]:
        """Make just these blocks (those still missing), in the order given: the chapters
        the user picked to generate and export."""
        position = {block: k for k, block in enumerate(self._order, 1)}
        issues: list[Issue] = []
        for block in [b for b in blocks if b in self._weights]:
            if cancelled():
                raise GenerationCancelled()
            if self.store.has(block):
                continue  # finished meanwhile by a run this one replaced
            on_status(f"Reading paragraph {position[block]} of {len(self._order)}")
            pcm, cues, block_issues = self.render_block(block, cancelled=cancelled)
            self.store.put(block, pcm, cues)
            issues += block_issues
            on_block(block, dsp.ms_of(len(pcm) // 2))
        return issues

    def _voice(self, index: int, text: str, cancelled: Callable[[], bool]) -> tuple[np.ndarray, Issue | None]:
        """One chunk's clip, ready to join; with a checker, the best of up to MAX_ATTEMPTS takes."""
        best: np.ndarray | None = None
        best_rate, best_transcript = float("inf"), ""
        for attempt in range(MAX_ATTEMPTS if self.checker else 1):
            if attempt and cancelled():
                raise GenerationCancelled()
            x = self._synthesize(index, text)
            if self.checker is None:
                return x, None
            verdict = self.checker.check(x, dsp.SAMPLE_RATE, text)
            if verdict.ok:
                return x, None
            if verdict.error_rate < best_rate:
                best, best_rate, best_transcript = x, verdict.error_rate, verdict.transcript
        return best, Issue(index, text, best_transcript, best_rate)

    def _synthesize(self, index: int, text: str) -> np.ndarray:
        """The engine's clip at 24 kHz, trimmed to its speech and declicked."""
        try:
            x, rate = self.engine.synthesize(self.voice, text)
        except Exception:
            try:
                x, rate = self.engine.synthesize(self.voice, text)
            except Exception as err:
                raise RuntimeError(f'{self.engine.name} failed on block {index} ("{text}"): {err}') from err
        x = dsp.resample(x, rate, dsp.SAMPLE_RATE)
        return dsp.declick(dsp.trim_speech(x, dsp.SAMPLE_RATE), dsp.SAMPLE_RATE)


def stream_ready(order: list[int], generated: set[int], start_block: int,
                 weights: dict[int, int], threshold: float = STREAM_THRESHOLD) -> bool:
    """Can playback from start_block begin? True once the unbroken run of generated blocks
    from the first ordered block at or after start_block holds threshold of the whole
    book's weight, or reaches the end of the book (a start past the last block with
    audio is at the end already)."""
    total = sum(weights.get(b, 1) for b in order)
    covered = 0
    for block in order[bisect_left(order, start_block):]:
        if block not in generated:
            return total > 0 and covered / total >= threshold
        covered += weights.get(block, 1)
    return True
