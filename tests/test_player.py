"""BookPlayer against a fake output and a real AudioStore + Timeline.

Every block's synthetic audio encodes where it comes from: sample i of block b
holds BASE[b] + i // 24 (its ms), so the next sample the fake output would play
tells which (block, ms) is being heard, even after the fake speed change. The
tests pump the player by hand (as its 20 ms timer would) while advancing the
fake output's clock, so they are deterministic and need no sound card.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from livrosound.audio_store import AudioStore
from livrosound.timeline import Timeline
from livrosound.ui.player import BookPlayer
from tests.fake_output import FakeOutputs, InlineExecutor, ManualExecutor, fake_stretch

# Blocks 0, 2 and 5 aren't read (an image, front matter, a separator with pauses off).
ORDER = [1, 3, 4, 6]
DURATIONS = {1: 1000, 3: 2000, 4: 500, 6: 1500}
BASE = {1: 0, 3: 4000, 4: 8000, 6: 12000}
FADE_BYTES = 8 * 48  # the 8 ms fade-in after starting mid-block


def block_pcm(block: int, ms: int | None = None) -> bytes:
    ms = DURATIONS[block] if ms is None else ms
    return (BASE[block] + np.arange(24 * ms) // 24).astype("<i2").tobytes()


def decode(sample: bytes) -> tuple[int, int]:
    value = int.from_bytes(sample, "little", signed=True)
    block = next(b for b, base in BASE.items() if base <= value < base + 4000)
    return block, value - BASE[block]


class Rig:
    """A player on a store holding `blocks`, with every signal recorded."""

    def __init__(self, store: AudioStore, blocks=DURATIONS, *, executor=None, stretch=fake_stretch, **output):
        for block in blocks:
            store.put(block, block_pcm(block), [])
        self.store = store
        self.timeline = Timeline(ORDER, store.durations())
        self.outputs = FakeOutputs(**output)
        self.player = BookPlayer(output_factory=self.outputs, stretch=stretch, executor=executor or InlineExecutor())
        self.player.set_book(store, self.timeline)
        self.events: list[tuple] = []
        p = self.player
        p.position_changed.connect(lambda b, ms: self.events.append(("position", b, ms)))
        p.playing_changed.connect(lambda on: self.events.append(("playing", on)))
        p.needs_audio.connect(lambda b: self.events.append(("needs", b)))
        p.finished.connect(lambda: self.events.append(("finished",)))
        p.waiting_changed.connect(lambda on: self.events.append(("waiting", on)))
        p.error.connect(lambda msg: self.events.append(("error", msg)))

    @property
    def out(self):
        return self.outputs.current

    def run(self, ms: float, step: float = 20) -> None:
        """Let ms of output time pass, pumping as the timer would."""
        while ms > 0:
            if self.out is not None:
                self.out.advance(min(step, ms))
            self.player._pump()
            ms -= step

    def of(self, kind: str) -> list:
        return [e[1] if len(e) == 2 else e[1:] for e in self.events if e[0] == kind]

    def next_heard(self) -> tuple[int, int]:
        """The (block, ms) of the next sample the output will play."""
        return decode(self.out.written[self.out.played : self.out.played + 2])


@pytest.fixture
def store(tmp_path):
    s = AudioStore(tmp_path / "audio.sqlite")
    yield s
    s.close()


@pytest.fixture
def rig(store, qapp):
    rigs = []

    def make(blocks=DURATIONS, **kwargs) -> Rig:
        r = Rig(store, blocks, **kwargs)
        rigs.append(r)
        return r

    yield make
    for r in rigs:
        r.player.pause()  # no timer outlives the test


# ---------- playing through ----------


def test_plays_every_block_back_to_back(rig):
    r = rig()
    r.player.play()
    assert r.player.is_playing()
    r.run(5200)
    assert r.of("finished") == [()]
    assert r.of("playing") == [True, False]
    assert not r.player.is_playing()
    assert len(r.outputs.made) == 1  # one stream, no restarts at the joins
    assert bytes(r.out.written) == b"".join(block_pcm(b) for b in ORDER)
    assert r.player.position() == (6, 1500)
    assert r.out.stopped


def test_position_follows_what_is_heard(rig):
    r = rig()
    r.player.play()
    assert r.of("position") == [(1, 0)]
    elapsed = 0
    for at, expected in [(500, (1, 500)), (1000, (3, 0)), (1740, (3, 740)), (3000, (4, 0)),
                         (3260, (4, 260)), (4700, (6, 1200))]:
        r.run(at - elapsed)
        elapsed = at
        assert r.player.position() == expected
        assert r.next_heard() == expected


def test_position_is_reported_about_30_times_a_second(rig):
    r = rig()
    r.player.play()
    r.events.clear()
    r.run(1000)
    positions = r.of("position")
    assert 20 <= len(positions) <= 40
    assert positions == sorted(positions)  # (block, ms) only moves forward
    assert positions[-1] == (3, 0)


def test_play_from_a_saved_position(rig):
    r = rig()
    r.player.seek(3, 700)
    assert r.outputs.made == []  # paused: nothing opened
    r.player.play()
    assert r.out.written[:2] == b"\x00\x00"  # fades in from silence mid-block
    assert bytes(r.out.written[FADE_BYTES:4000]) == block_pcm(3)[700 * 48 + FADE_BYTES : 700 * 48 + 4000]
    r.run(300)
    assert r.player.position() == (3, 1000)


# ---------- pause, seek, skip, paragraphs ----------


def test_pause_and_resume_are_exact_to_the_sample(rig):
    r = rig()
    r.player.play()
    r.run(737.5)  # 17,700 samples: not on a whole ms
    first = r.out
    r.player.pause()
    assert first.stopped
    assert not r.player.is_playing()
    assert r.player.position() == (1, 738)
    r.run(500)  # time passes while paused: nothing moves
    assert r.player.position() == (1, 738)
    r.player.play()
    second = r.out
    assert second is not first
    resumed = block_pcm(1)[17700 * 2 :]
    assert first.heard + resumed == block_pcm(1)
    assert bytes(second.written[FADE_BYTES:]) == (resumed + block_pcm(3))[FADE_BYTES : len(second.written)]
    assert r.of("playing") == [True, False, True]


def test_seek_while_playing_restarts_there(rig):
    r = rig()
    r.player.play()
    r.run(200)
    r.player.seek(6, 300)
    assert r.player.is_playing()
    assert len(r.outputs.made) == 2 and r.outputs.made[0].stopped
    assert r.of("position")[-1] == (6, 300)  # reported at once
    r.run(200)
    assert r.player.position() == (6, 500)
    assert r.next_heard() == (6, 500)
    r.player.seek(1, 900)  # back across blocks
    r.run(300)
    assert r.player.position() == (3, 200)


def test_seek_clamps_and_maps_unread_blocks(rig):
    r = rig()
    p = r.player
    p.seek(3, 99999)
    assert p.position() == (3, 1999)
    p.seek(3, -50)
    assert p.position() == (3, 0)
    p.seek(2, 500)  # not read: the next block that is, from its start
    assert p.position() == (3, 0)
    p.seek(0)
    assert p.position() == (1, 0)
    p.seek(4, 250)
    p.seek(7)  # past the last read block: ignored
    assert p.position() == (4, 250)


def test_skip_crosses_blocks_and_clamps(rig):
    r = rig()
    p = r.player
    p.seek(1, 800)
    p.skip(500)
    assert p.position() == (3, 300)
    p.skip(-500)
    assert p.position() == (1, 800)
    p.skip(-10000)
    assert p.position() == (1, 0)
    p.seek(6, 1000)
    p.skip(10000)
    assert p.position() == (6, 1499)


def test_skip_stops_at_the_edge_of_the_ready_audio(rig):
    r = rig({1: 1000, 3: 2000, 6: 1500})  # block 4 not generated
    p = r.player
    p.seek(3, 1800)
    p.skip(10000)
    assert p.position() == (3, 1999)
    p.seek(6, 200)
    p.skip(-10000)
    assert p.position() == (6, 0)


def test_skip_while_playing(rig):
    r = rig()
    r.player.play()
    r.run(200)
    r.player.skip(1000)
    assert r.player.position() == (3, 200)
    r.run(100)
    assert r.player.position() == (3, 300)
    assert r.next_heard() == (3, 300)


def test_next_and_previous_paragraph(rig):
    r = rig()
    p = r.player
    p.seek(3, 1200)
    p.next_block()
    assert p.position() == (4, 0)
    p.seek(3, 1200)
    p.prev_block()  # from the middle: one paragraph back, not this one's start
    assert p.position() == (1, 0)
    p.prev_block()  # the first block: its start
    assert p.position() == (1, 0)
    p.seek(6, 100)
    p.next_block()  # the last block: nowhere to go
    assert p.position() == (6, 100)
    assert r.of("needs") == []


def test_paragraph_jumps_while_playing(rig):
    r = rig()
    r.player.play()
    r.run(500)
    r.player.next_block()
    r.run(100)
    assert r.player.position() == (3, 100)
    r.run(400)
    r.player.prev_block()
    r.run(100)
    assert r.player.position() == (1, 100)
    assert r.next_heard() == (1, 100)


def test_next_paragraph_without_audio_asks_for_it(rig):
    r = rig({1: 1000, 3: 2000, 6: 1500})
    p = r.player
    p.seek(3, 500)
    p.next_block()
    assert r.of("needs") == [4]
    assert p.position() == (4, 0)
    p.seek(3, 500)
    p.play()
    p.next_block()
    assert r.of("needs") == [4, 4]
    assert not p.is_playing()
    assert p.position() == (4, 0)


def test_previous_paragraph_without_audio_goes_to_this_ones_start(rig):
    r = rig({3: 2000, 4: 500, 6: 1500})  # block 1 not generated
    r.player.seek(3, 500)
    r.player.prev_block()
    assert r.player.position() == (3, 0)


# ---------- speed ----------


def test_speed_maps_output_time_back_to_source_time(rig):
    r = rig()
    p = r.player
    p.set_rate(2.0)
    p.play()
    r.run(250)
    assert p.position() == (1, 500)
    r.run(250)
    assert p.position() == (3, 0)
    r.run(300)
    assert p.position() == (3, 600)
    assert r.next_heard() == (3, 600)
    p.set_rate(1.0)  # mid-block: restarts where it is
    assert p.position() == (3, 600)
    r.run(100)
    assert p.position() == (3, 700)
    p.set_rate(0.5)
    r.run(100)
    assert p.position() == (3, 750)
    assert r.next_heard() == (3, 750)
    assert len(r.outputs.made) == 3


def test_speed_plays_gaplessly_across_blocks(rig):
    r = rig()
    r.player.set_rate(1.5)
    r.player.play()
    r.run(4000)
    assert r.of("finished") == [()]
    assert bytes(r.out.written) == b"".join(fake_stretch(block_pcm(b), 1.5) for b in ORDER)


def test_speed_clamps(rig):
    r = rig()
    p = r.player
    p.set_rate(5)
    p.play()
    r.run(100)
    assert p.position() == (1, 200)
    p.set_rate(0.1)
    r.run(100)
    assert p.position() == (1, 250)


def test_speed_waits_for_the_stretch_never_plays_unstretched_audio(rig):
    executor = ManualExecutor()
    r = rig(executor=executor)
    p = r.player
    p.set_rate(1.5)  # paused: starts stretching the first blocks
    assert len(executor.jobs) == 3
    p.play()
    r.run(100)
    assert r.out.written == b""
    assert p.position() == (1, 0)
    assert p.is_playing()
    executor.run_all()
    r.run(20)  # the tick that picks the stretch up writes it
    r.run(100)
    assert p.position() == (1, 150)
    assert bytes(r.out.written[:100]) == fake_stretch(block_pcm(1), 1.5)[:100]


def test_real_time_stretch(store, qapp):
    """dsp.time_stretch (ffmpeg atempo) on worker threads: lengths and position mapping hold."""
    tone = (8000 * np.sin(2 * np.pi * 220 * np.arange(24 * 2000) / 24000)).astype("<i2").tobytes()
    store.put(1, tone, [])
    store.put(3, tone, [])
    outputs = FakeOutputs()
    player = BookPlayer(output_factory=outputs)
    player.set_book(store, Timeline([1, 3], store.durations()))
    finished = []
    player.finished.connect(lambda: finished.append(True))
    player.set_rate(1.5)
    player.play()
    deadline = time.monotonic() + 10
    while not outputs.current.written and time.monotonic() < deadline:
        time.sleep(0.01)
        player._pump()
    assert outputs.current.written, "the stretch never arrived"
    outputs.current.advance(400)
    block, ms = player.position()
    assert block == 1 and abs(ms - 600) <= 10
    while not finished and time.monotonic() < deadline:
        outputs.current.advance(20)
        player._pump()
        time.sleep(0.001)
    assert finished
    seconds = len(outputs.current.written) / 48000
    assert abs(seconds - 4.0 / 1.5) < 0.05
    player.pause()


# ---------- missing audio ----------


def test_waits_for_generation_then_resumes(rig):
    r = rig({1: 1000, 3: 2000, 6: 1500})
    p = r.player
    p.set_waiting_allowed(lambda block: block == 4)
    p.seek(3, 1500)
    p.play()
    r.run(600)
    assert r.of("waiting") == [True]
    assert p.is_playing()
    assert p.position() == (4, 0)
    assert r.out.stopped
    r.run(200)  # still waiting: no new output
    assert len(r.outputs.made) == 1
    r.store.put(4, block_pcm(4), [])
    p.block_generated(4)
    assert r.of("waiting") == [True, False]
    assert len(r.outputs.made) == 2
    assert r.timeline.ready(4)  # the player keeps the timeline in step
    r.run(100)
    assert p.position() == (4, 100)
    assert r.next_heard() == (4, 100)
    r.run(2000)
    assert r.of("finished") == [()]
    assert r.of("needs") == []


def test_block_arriving_before_the_buffer_drains_plays_on_gaplessly(rig):
    r = rig({1: 1000, 3: 2000, 6: 1500})
    p = r.player
    p.set_waiting_allowed(lambda block: True)
    p.seek(3, 1500)
    p.play()
    r.run(100)  # all of block 3 is written; 400 ms of it still buffered
    r.store.put(4, block_pcm(4), [])
    p.block_generated(4)
    r.run(2500)
    assert r.of("finished") == [()]
    assert r.of("waiting") == []
    assert len(r.outputs.made) == 1
    expected = block_pcm(3)[1500 * 48 :] + block_pcm(4) + block_pcm(6)
    assert bytes(r.out.written[FADE_BYTES:]) == expected[FADE_BYTES:]


def test_waiting_gives_up_when_generation_wont_come(rig):
    r = rig({1: 1000, 3: 2000, 6: 1500})
    p = r.player
    coming = True
    p.set_waiting_allowed(lambda block: coming)
    p.seek(3, 1800)
    p.play()
    r.run(400)
    assert r.of("waiting") == [True]
    coming = False  # e.g. generation was cancelled
    r.run(20)
    assert r.of("needs") == [4]
    assert r.of("waiting") == [True, False]
    assert not p.is_playing()
    assert p.position() == (4, 0)


def test_needs_audio_when_none_is_coming(rig):
    r = rig({1: 1000, 3: 2000, 6: 1500})
    p = r.player
    p.seek(3, 1800)
    p.play()
    r.run(400)
    assert r.of("needs") == [4]
    assert r.of("playing") == [True, False]
    assert r.of("waiting") == []
    assert p.position() == (4, 0)
    assert r.out.stopped
    p.play()  # still nothing there
    assert r.of("needs") == [4, 4]
    assert not p.is_playing()


def test_pause_while_waiting(rig):
    r = rig({1: 1000, 3: 2000, 6: 1500})
    p = r.player
    p.set_waiting_allowed(lambda block: True)
    p.seek(3, 1900)
    p.play()
    r.run(200)
    p.pause()
    assert r.of("waiting") == [True, False]
    assert not p.is_playing()
    r.store.put(4, block_pcm(4), [])
    p.block_generated(4)
    assert len(r.outputs.made) == 1  # paused: stays paused
    p.play()
    r.run(100)
    assert p.position() == (4, 100)


def test_regenerated_block_replaces_cached_audio(rig):
    r = rig()
    p = r.player
    p.seek(4)
    p.play()
    r.run(100)
    p.pause()
    r.store.put(4, (BASE[6] + np.zeros(24 * 500)).astype("<i2").tobytes(), [])
    p.block_generated(4)
    p.seek(4)
    p.play()
    assert r.next_heard() == (6, 0)  # the new audio, not a cached copy of the old


# ---------- misc ----------


def test_no_sound_output(rig):
    r = rig(can_start=False)
    r.player.play()
    assert not r.player.is_playing()
    assert r.of("playing") == [True, False]
    assert len(r.of("error")) == 1


def test_set_book_stops_and_starts_over(rig, tmp_path):
    r = rig()
    r.player.play()
    r.run(1500)
    other = AudioStore(tmp_path / "other.sqlite")
    other.put(2, block_pcm(1), [])
    r.player.set_book(other, Timeline([2, 5], other.durations()))
    assert not r.player.is_playing()
    assert r.out.stopped
    assert r.player.position() == (2, 0)
    other.close()


# ---------- real sound output ----------


def _wait(ms: int) -> None:
    """Run the event loop for ms. (QTest.qWait holds the GIL, which starves the stretch workers.)"""
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@pytest.mark.slow
def test_real_sound_output_advances(store, qapp):
    """(Silent) audio through the default output device: the position follows the clock, at 1x and 2x."""
    from PySide6.QtMultimedia import QMediaDevices

    if QMediaDevices.defaultAudioOutput().isNull():
        pytest.skip("no audio output device")
    store.put(1, bytes(2 * 24000 * 4), [])
    player = BookPlayer()
    player.set_book(store, Timeline([1], store.durations()))
    positions = []
    player.position_changed.connect(lambda b, ms: positions.append(ms))
    player.play()
    assert player.is_playing()
    started = time.monotonic()
    _wait(1000)
    elapsed_ms = (time.monotonic() - started) * 1000
    block, ms = player.position()
    assert block == 1
    assert elapsed_ms - 300 <= ms <= elapsed_ms + 50
    assert len(positions) >= 20  # reported continuously while playing
    player.set_rate(2.0)  # ffmpeg on a worker, then a new output
    _wait(800)
    advanced = player.position()[1] - ms
    player.pause()
    assert 900 <= advanced <= 1650


def test_stopping_the_sound_card_output_silences_its_buffer_at_once():
    """A pause or a jump must go quiet immediately: the queued audio is muted and dropped
    before the sink stops (on macOS stop() alone plays it out, ~0.5 s of lag or overlap)."""
    from livrosound.ui.player import BUFFER_MS, QtSinkOutput

    calls = []

    class Sink:
        def setVolume(self, v):
            calls.append(("volume", v))

        def reset(self):
            calls.append("reset")

        def stop(self):
            calls.append("stop")

        def deleteLater(self):
            calls.append("deleted")

    output = QtSinkOutput.__new__(QtSinkOutput)
    output._sink = Sink()
    output.stop()
    assert calls == [("volume", 0.0), "reset", "stop", "deleted"]
    assert output._sink is None
    output.stop()  # a second stop is harmless
    assert BUFFER_MS <= 300, "a pause can never lag more than the buffer"
