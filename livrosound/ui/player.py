"""Plays a book's audio from the AudioStore, block after block, with no gaps.

There is never one big audio file: generation stores each block's audio on its
own (and may still be making the next one), so all playback is a live stream,
as in Script Radio's _pump_stream: a timer pushes 16-bit PCM into an output
every 20 ms and runs from the end of one block straight into the next ordered
block. Rules:

- A position is (block, ms into that block's audio) in *source* time, whatever
  the speed. Every write to the output is logged as a segment (output bytes ->
  block, offset into its audio, stretch ratio), so the output's processed time
  maps back to the source position of exactly what is being heard.
- Speed: a block is time-stretched whole (dsp.time_stretch keeps the pitch) on
  a worker thread, the next blocks a step ahead; within a block, source time =
  output time × raw length / stretched length (≈ the rate). A changed speed
  restarts the output at the current source position. A stretch that isn't
  ready yet is waited for in silence, never replaced by unstretched audio.
- Pause, seek and speed changes stop the output; play starts a new one at the
  exact source position (to the sample: pause → play resumes where it stopped,
  with an 8 ms fade-in so restarting mid-word doesn't click). A new output also
  opens the current default device, so headphones plugged in while paused work.
- Reaching a block without audio: if generation will reach it soon
  (set_waiting_allowed), playback waits there, still "playing", and resumes by
  itself the moment the block lands (block_generated, or the next timer tick);
  it gives up as soon as the callback says no. Otherwise it stops at (block, 0)
  and emits needs_audio(block). Past the last ordered block: finished().
- The output is injectable (AudioOutput: the slice of QAudioSink used here), so
  tests drive a fake clock instead of a sound card.

Everything runs on the UI thread except the stretching, which is handed the raw
PCM as bytes: SQLite is only ever read on the UI thread's own connection.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from PySide6.QtCore import QIODevice, QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices, QtAudio

from livrosound import dsp
from livrosound.audio_store import AudioStore
from livrosound.timeline import Timeline

SAMPLE_RATE = dsp.SAMPLE_RATE
BUFFER_MS = 1000  # the output's buffer rides out a busy UI thread; pause and seek discard it anyway
PUMP_INTERVAL_MS = 20
POSITION_INTERVAL_MS = 33  # position_changed ~30 times a second while playing
MIN_RATE, MAX_RATE = 0.5, 2.0
_PREFETCH_BLOCKS = 2  # stretched ahead of the block being fed
_CACHE_SIZE = 8  # (block, rate) audio kept in memory


class Writable(Protocol):
    def write(self, data: bytes) -> int: ...


class AudioOutput(Protocol):
    """What BookPlayer needs from an output: QAudioSink's push mode, under Qt's names."""

    def start(self) -> Writable | None: ...  # where to write PCM; None if the output can't play

    def bytesFree(self) -> int: ...

    def bufferSize(self) -> int: ...

    def processedUSecs(self) -> int: ...  # audio consumed since start(), in µs

    def stop(self) -> None: ...  # discards whatever is still buffered


class QtSinkOutput:
    """A QAudioSink on the current default output device: 16-bit mono at SAMPLE_RATE."""

    def __init__(self, parent: QObject | None = None):
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = QMediaDevices.defaultAudioOutput()
        self._sink: QAudioSink | None = None
        if not device.isNull() and device.isFormatSupported(fmt):
            self._sink = QAudioSink(device, fmt, parent)
            self._sink.setBufferSize(2 * dsp.samples_of(BUFFER_MS))

    def start(self) -> QIODevice | None:
        if self._sink is None:
            return None
        io = self._sink.start()
        return io if self._sink.error() == QtAudio.Error.NoError else None

    def bytesFree(self) -> int:
        return self._sink.bytesFree() if self._sink is not None else 0

    def bufferSize(self) -> int:
        return self._sink.bufferSize() if self._sink is not None else 0

    def processedUSecs(self) -> int:
        return self._sink.processedUSecs() if self._sink is not None else 0

    def stop(self) -> None:
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
            self._sink = None


@dataclass
class _Segment:
    """A stretch of the output that plays one block's audio, contiguously."""

    out_start: int  # byte offsets in the output since it started
    out_end: int
    block: int
    audio_start: int  # byte offset into the block's audio at the speed it was fed
    ratio: float  # source samples per output sample (1 at normal speed)


@dataclass
class _Feed:
    """The block being written into the output."""

    block: int
    start_ms: float  # where to start in it (source time), applied once its audio is ready
    job: Future  # -> (audio at the current speed, ratio)
    audio: bytes | None = None
    ratio: float = 1.0
    offset: int = 0  # next byte of audio to write


def _faded_in(pcm: bytes) -> bytes:
    """pcm whose first DECLICK_MS rise from silence (starting mid-word doesn't click)."""
    n = min(len(pcm) // 2, dsp.samples_of(dsp.DECLICK_MS))
    head = np.frombuffer(pcm[: 2 * n], "<i2") * np.linspace(0, 1, n, endpoint=False)
    return head.astype("<i2").tobytes() + pcm[2 * n :]


class BookPlayer(QObject):
    position_changed = Signal(int, int)  # block, ms into its audio (source time, not stretched)
    playing_changed = Signal(bool)
    needs_audio = Signal(int)  # stopped at a block that has no audio and none is coming soon
    finished = Signal()  # played past the last ordered block
    waiting_changed = Signal(bool)  # holding at a block generation is about to make (still "playing")
    error = Signal(str)  # no sound output, or a speed change failed; playback stopped

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        output_factory: Callable[[QObject], AudioOutput] = QtSinkOutput,
        stretch: Callable[[bytes, float], bytes] = dsp.time_stretch,
        executor: Executor | None = None,
    ):
        super().__init__(parent)
        self._make_output = output_factory
        self._stretch = stretch
        self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="livrosound-stretch")
        self._store: AudioStore | None = None
        self._timeline: Timeline | None = None
        self._waiting_allowed: Callable[[int], bool] = lambda block: False
        self._rate = 1.0
        self._playing = False
        self._waiting: int | None = None
        # The position while no output runs, and where the running one started.
        self._block = 0
        self._ms = 0.0
        self._cache: OrderedDict[tuple[int, float], Future] = OrderedDict()
        # The running output and what has gone into it.
        self._output: AudioOutput | None = None
        self._device: Writable | None = None
        self._written = 0
        self._segments: list[_Segment] = []
        self._feed: _Feed | None = None
        self._starved: int | None = None  # everything is written and this next block has no audio yet
        self._fed_all = False  # everything is written up to the end of the book
        self._fade_in = False
        self._emitted_us = 0
        self._timer = QTimer(self)
        self._timer.setInterval(PUMP_INTERVAL_MS)
        self._timer.timeout.connect(self._pump)

    # ---------- public ----------

    def set_book(self, store: AudioStore, timeline: Timeline) -> None:
        """Stop, forget the old book's audio and stand at the start of this one."""
        self._halt()
        for job in self._cache.values():
            job.cancel()
        self._cache.clear()
        self._store, self._timeline = store, timeline
        first = timeline.first_at_or_after(0)
        self._block, self._ms = (0 if first is None else first), 0.0

    def set_waiting_allowed(self, allowed: Callable[[int], bool]) -> None:
        """allowed(block): will generation make this block soon? Asked every tick while
        waiting (keep it cheap); True holds playback there instead of emitting needs_audio."""
        self._waiting_allowed = allowed

    def is_playing(self) -> bool:
        """True from play() until pause/stop, including while waiting for a block's audio."""
        return self._playing

    def play(self) -> None:
        if self._store is None or self._playing:
            return
        self._set_playing(True)
        self._start(self._block, self._ms)

    def pause(self) -> None:
        if not self._playing:
            return
        self._block, self._ms = self._exact_position()
        self._halt()
        self._emit_position()

    def position(self) -> tuple[int, int]:
        block, ms = self._exact_position()
        return block, round(ms)

    def seek(self, block: int, ms: int = 0) -> None:
        """Go to (block, ms), playing on from there if playing. A block that isn't read
        (an image, front matter) means the start of the next one that is."""
        if self._store is None:
            return
        target = self._timeline.first_at_or_after(block)
        if target is None:
            return
        duration = self._store.durations().get(target)
        ms = 0 if target != block or duration is None else min(max(0, ms), max(0, duration - 1))
        if self._playing:
            self._start(target, ms)
        else:
            self._block, self._ms = target, float(ms)
            self._emit_position()
            self._prefetch(target)

    def skip(self, delta_ms: int) -> None:
        """±10 s through the ready audio, across blocks, clamped at the edge of the run."""
        if self._store is None:
            return
        block, ms = self.position()
        self.seek(*self._timeline.skip(block, ms, delta_ms))

    def next_block(self) -> None:
        """The start of the next paragraph; needs_audio if it has none (and none is coming)."""
        if self._store is None:
            return
        nxt = self._timeline.next_block(self.position()[0])
        if nxt is None:
            return
        was_playing = self._playing
        self.seek(nxt)  # while playing, a block without audio waits or asks by itself
        if not was_playing and not self._store.has(nxt) and not self._waiting_allowed(nxt):
            self.needs_audio.emit(nxt)

    def prev_block(self) -> None:
        """The start of the previous paragraph, even from the middle of this one
        (one paragraph back); this one's start if the previous has no audio."""
        if self._store is None:
            return
        block = self.position()[0]
        prev = self._timeline.prev_block(block)
        self.seek(prev if prev is not None and self._store.has(prev) else block)

    def set_rate(self, rate: float) -> None:
        """Playback speed, 0.5..2.0, pitch unchanged; playing output restarts where it is."""
        rate = min(MAX_RATE, max(MIN_RATE, float(rate)))
        if rate == self._rate:
            return
        if self._output is not None:
            block, ms = self._exact_position()
            self._rate = rate
            self._start(block, ms)
        else:
            self._rate = rate
            if self._store is not None:
                self._prefetch(self._block)

    def block_generated(self, block: int) -> None:
        """A block just got (new) audio: keep the timeline in step, drop stale copies,
        and carry on if playback was waiting for it."""
        if self._store is None:
            return
        duration = self._store.durations().get(block)
        if duration is None:
            return
        self._timeline.set_duration(block, duration)
        for key in [k for k in self._cache if k[0] == block]:
            del self._cache[key]
        if block in (self._waiting, self._starved):
            self._pump()
        elif self._feed is not None:
            self._prefetch(self._feed.block)

    # ---------- output ----------

    def _start(self, block: int, ms: float) -> None:
        """Play from (block, ms) on a fresh output (only while playing)."""
        self._stop_output()
        self._block, self._ms = block, float(ms)
        self._emit_position()
        if not self._store.has(block):
            self._missing(block)
            return
        self._set_waiting(None)
        output = self._make_output(self)
        device = output.start()
        if device is None:
            output.stop()
            self._halt()
            self.error.emit("Couldn't play the audio: no sound output is available.")
            return
        self._output, self._device = output, device
        self._begin_feed(block, ms)
        self._fade_in = ms > 0
        self._timer.start()
        self._pump()

    def _missing(self, block: int) -> None:
        """Playback is at a block without audio: hold there if it's coming soon, else stop and ask."""
        if self._waiting_allowed(block):
            self._set_waiting(block)
            self._timer.start()
        else:
            self._halt()
            self.needs_audio.emit(block)

    def _stop_output(self) -> None:
        self._timer.stop()
        if self._output is not None:
            self._output.stop()
        self._output = self._device = self._feed = self._starved = None
        self._written = self._emitted_us = 0
        self._segments = []
        self._fed_all = False

    def _halt(self) -> None:
        self._stop_output()
        self._set_waiting(None)
        self._set_playing(False)

    def _fail(self, message: str) -> None:
        self._block, self._ms = self._exact_position()
        self._halt()
        self.error.emit(message)

    def _finish(self) -> None:
        self._block, self._ms = self._exact_position()
        self._halt()
        self._emit_position()
        self.finished.emit()

    def _pump(self) -> None:
        """Every tick: feed the output, report the position, and handle running dry."""
        if self._waiting is not None:
            if self._store.has(self._waiting):
                self._start(self._waiting, 0)
            elif not self._waiting_allowed(self._waiting):
                self._missing(self._waiting)
            return
        if self._output is None:
            return
        self._feed_output()
        output = self._output
        if output is None:  # the feed failed
            return
        played_us = output.processedUSecs()
        if played_us - self._emitted_us >= POSITION_INTERVAL_MS * 1000:
            self._emitted_us = played_us
            self._emit_position()
        played = self._played_bytes()
        while len(self._segments) > 1 and self._segments[1].out_start <= played:
            del self._segments[0]
        if (self._fed_all or self._starved is not None) and output.bytesFree() >= output.bufferSize():
            if self._fed_all:
                self._finish()
            else:
                self._start(self._starved, 0)  # waits there or asks for it

    def _feed_output(self) -> None:
        """Write as much audio as the output takes, running on into the next ordered blocks."""
        while self._output is not None:
            feed = self._feed
            if feed.audio is None and not self._resolve(feed):
                return
            free = self._output.bytesFree()
            free -= free % 2
            if free <= 0:
                return
            if feed.offset < len(feed.audio):
                chunk = feed.audio[feed.offset : feed.offset + free]
                if self._fade_in:
                    chunk = _faded_in(chunk)
                    self._fade_in = False
                written = self._device.write(chunk)
                if written < 0:
                    self._fail("The audio output stopped unexpectedly.")
                    return
                self._written += written
                self._segments[-1].out_end += written
                feed.offset += written
                if written < len(chunk):
                    return
                continue
            nxt = self._timeline.next_block(feed.block)
            if nxt is None:
                self._fed_all = True
                return
            if not self._store.has(nxt):
                self._starved = nxt
                return
            self._starved = None
            self._begin_feed(nxt, 0.0)

    def _begin_feed(self, block: int, start_ms: float) -> None:
        self._feed = _Feed(block, start_ms, self._audio(block, self._rate))
        self._prefetch(block)

    def _resolve(self, feed: _Feed) -> bool:
        """Take the fed block's audio once it's ready (its stretch may still be running)."""
        if not feed.job.done():
            return False
        try:
            feed.audio, feed.ratio = feed.job.result()
        except Exception as exc:
            self._cache.pop((feed.block, self._rate), None)
            self._fail(f"Couldn't change the playback speed: {exc}")
            return False
        start = 2 * round(feed.start_ms * SAMPLE_RATE / 1000 / feed.ratio)
        feed.offset = min(len(feed.audio), start)
        self._segments.append(_Segment(self._written, self._written, feed.block, feed.offset, feed.ratio))
        return True

    # ---------- audio at a speed ----------

    def _audio(self, block: int, rate: float) -> Future:
        """A block's audio at a speed, as a Future of (pcm, ratio); stretching runs on a worker."""
        key = (block, rate)
        job = self._cache.get(key)
        if job is not None:
            self._cache.move_to_end(key)
            return job
        raw = self._store.pcm(block) or b""
        if rate == 1.0:
            job = Future()
            job.set_result((raw, 1.0))
        else:
            job = self._executor.submit(self._stretched, raw, rate)
        self._cache[key] = job
        if len(self._cache) > _CACHE_SIZE:
            self._cache.popitem(last=False)
        return job

    def _stretched(self, raw: bytes, rate: float) -> tuple[bytes, float]:
        """(Worker thread.) raw played rate times as fast, and source samples per output sample."""
        audio = self._stretch(raw, rate)
        audio = audio[: len(audio) - len(audio) % 2]
        return audio, (len(raw) / len(audio) if audio else 1.0)

    def _prefetch(self, block: int | None) -> None:
        """Start stretching this block and the next ones, so speed never waits at a join."""
        if self._rate == 1.0:
            return
        for _ in range(1 + _PREFETCH_BLOCKS):
            if block is None or not self._store.has(block):
                return
            self._audio(block, self._rate)
            block = self._timeline.next_block(block)

    # ---------- position ----------

    def _played_bytes(self) -> int:
        samples = round(self._output.processedUSecs() * SAMPLE_RATE / 1_000_000)
        return min(self._written, 2 * samples)

    def _exact_position(self) -> tuple[int, float]:
        """(block, ms) being heard, in source time, to the sample."""
        if self._output is None or not self._segments:
            return self._block, self._ms
        played = self._played_bytes()
        i = max(0, bisect_right(self._segments, played, key=lambda s: s.out_start) - 1)
        seg = self._segments[i]
        offset = seg.audio_start + min(played, seg.out_end) - seg.out_start
        return seg.block, offset / 2 * seg.ratio * 1000 / SAMPLE_RATE

    def _emit_position(self) -> None:
        block, ms = self._exact_position()
        self.position_changed.emit(block, round(ms))

    def _set_playing(self, playing: bool) -> None:
        if playing != self._playing:
            self._playing = playing
            self.playing_changed.emit(playing)

    def _set_waiting(self, block: int | None) -> None:
        was_waiting = self._waiting is not None
        self._waiting = block
        if was_waiting != (block is not None):
            self.waiting_changed.emit(block is not None)
