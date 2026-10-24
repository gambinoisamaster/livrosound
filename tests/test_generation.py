"""Audio generation: which blocks get audio, in what order, how each block's audio
is put together (cues, lead-ins, clean joins), cancelling, the Whisper retry loop,
and the 5% rule. The fast tests use FakeEngine; the slow one reads the opening of
Artful Sentences with Pocket-TTS."""

from __future__ import annotations

import time
import wave

import numpy as np
import pytest

from livrosound import dsp, loudness
from livrosound.audio_store import AudioStore
from livrosound.book import Block, Book
from livrosound.generation import STREAM_THRESHOLD, GenerationCancelled, Generator, stream_ready
from livrosound.speech import SpeechOptions, block_chunks
from livrosound.validate import MAX_ATTEMPTS, Verdict

from .fakes import FakeEngine

SR = dsp.SAMPLE_RATE
LONG = (
    "The harbour lay quiet under a pale sky, and the fishermen, who had been up since four, "
    "sat mending their nets along the wall while the gulls wheeled overhead, crying for scraps "
    "that nobody had the patience to throw them that morning."
)


def make_book() -> Book:
    blocks = [
        Block("heading", "Contents", level=1, role="contents"),  # 0: skipped front matter
        Block("heading", "The Harbour", level=1, role="title"),  # 1: the title announcement
        Block("heading", "CHAPTER ONE", level=1),  # 2
        Block("paragraph", "It was a bright cold day. The boats came in early."),  # 3
        Block("image", "", src="map.png"),  # 4: silent
        Block("paragraph", LONG),  # 5: one long sentence, cut at its clauses
        Block("separator", ""),  # 6: a pause
        Block("heading", "A Section", level=2),  # 7
        Block("note", "1. A footnote nobody asked to hear."),  # 8: footnotes are off
        Block("paragraph", "Last words here."),  # 9
    ]
    return Book("gen", "The Harbour", "Ann Author", "", "txt", blocks=blocks)


ORDER = [1, 2, 3, 5, 6, 7, 9]


@pytest.fixture
def store(tmp_path):
    s = AudioStore(tmp_path / "audio.sqlite")
    yield s
    s.close()


def generator(store, engine=None, checker=None, book=None, options=None) -> Generator:
    return Generator(book or make_book(), store, engine or FakeEngine(), "Aaron", options or SpeechOptions(), checker)


def samples(pcm: bytes) -> np.ndarray:
    return dsp.from_pcm16(pcm)


def max_jump(x: np.ndarray, start_ms: float, end_ms: float) -> float:
    """Largest sample-to-sample step in [start_ms, end_ms) (a click is a big step)."""
    a, b = max(0, dsp.samples_of(start_ms)), min(len(x), dsp.samples_of(end_ms))
    return float(np.abs(np.diff(x[a:b])).max()) if b - a > 1 else 0.0


# ---------- order, weights, queue ----------


def test_order_is_the_blocks_read_aloud():
    g = generator(None)
    assert g.order() == ORDER
    with_notes = generator(None, options=SpeechOptions(read_footnotes=True))
    assert with_notes.order() == [1, 2, 3, 5, 6, 7, 8, 9]
    no_headings = generator(None, options=SpeechOptions(read_headings=False))
    assert no_headings.order() == [1, 3, 5, 6, 9], "the title announcement is always read"


def test_weights_count_characters_and_a_separator_counts_one():
    book = make_book()
    weights = generator(None, book=book).weights()
    assert list(weights) == ORDER
    assert weights[6] == 1
    assert weights[5] == len(LONG)
    assert all(weights[i] == len(book.blocks[i].text) for i in ORDER if i != 6)


def test_queue_wraps_around_and_skips_generated_blocks(store):
    g = generator(store)
    assert g.queue_from(0) == ORDER
    assert g.queue_from(5) == [5, 6, 7, 9, 1, 2, 3]
    assert g.queue_from(4) == [5, 6, 7, 9, 1, 2, 3], "an image starts at the next block read"
    assert g.queue_from(100) == ORDER, "past the end: the whole book from the start"
    store.put(6, dsp.to_pcm16(dsp.silence(100)), [])
    store.put(2, dsp.to_pcm16(dsp.silence(100)), [])
    assert g.queue_from(5) == [5, 7, 9, 1, 3]


def test_fraction_done_counts_characters(store):
    g = generator(store)
    weights = g.weights()
    total = sum(weights.values())
    assert g.fraction_done() == 0.0
    store.put(5, dsp.to_pcm16(dsp.silence(100)), [])
    assert g.fraction_done() == pytest.approx(len(LONG) / total)
    for block in ORDER:
        store.put(block, dsp.to_pcm16(dsp.silence(100)), [])
    assert g.fraction_done() == 1.0


def test_a_book_with_nothing_to_read_is_done(store):
    book = Book("e", "Empty", "", "", "txt", blocks=[Block("image", "", src="x.png")])
    g = generator(store, book=book)
    assert g.order() == [] and g.queue_from(0) == []
    assert g.fraction_done() == 1.0


# ---------- one block's audio ----------


def test_cues_are_ordered_inside_the_audio_and_voice_the_chunks(store):
    g = generator(store)
    book = make_book()
    for index in ORDER:
        pcm, cues, issues = g.render_block(index)
        assert issues == []
        duration = dsp.ms_of(len(pcm) // 2)
        chunks = block_chunks(book, index, SpeechOptions())
        assert [(c.start_char, c.end_char) for c in cues] == [(c.start, c.end) for c in chunks]
        end = 0
        for cue in cues:
            assert end <= cue.start_ms < cue.end_ms <= duration
            end = cue.end_ms
    long_cues = g.render_block(5)[1]
    assert len(long_cues) >= 2, "the long sentence is voiced in pieces"


def test_cue_lengths_follow_the_engine_clips(store):
    """FakeEngine: 160 ms a word, trimmed to 30 ms after the last burst (its 40 ms tail goes)."""
    g = generator(store)
    _, cues, _ = g.render_block(3)
    words = [len(text.split()) for _, text in g.engine.calls]
    assert [c.end_ms - c.start_ms for c in cues] == [160 * n - 10 for n in words]


class TickEngine(FakeEngine):
    """FakeEngine with Pocket-TTS's end-of-clip tick: a 25 ms sound, cut off by the clip's
    end, 40 ms after the last word."""

    def synthesize(self, voice, text):
        x, rate = super().synthesize(voice, text)
        t = np.arange(int(0.025 * rate)) / rate
        return np.concatenate([x, (0.2 * np.sin(2 * np.pi * 1000 * t + 1)).astype(np.float32)]), rate


def test_the_engine_tick_is_gone_and_chunk_joins_are_smooth(store):
    g = generator(store, engine=TickEngine())
    pcm, cues, _ = g.render_block(5)
    x = samples(pcm)
    assert len(cues) >= 2
    words = [len(text.split()) for _, text in g.engine.calls]
    assert [c.end_ms - c.start_ms for c in cues] == [160 * n - 10 for n in words]
    for cue in cues:
        tail = x[dsp.samples_of(cue.end_ms - 25) : dsp.samples_of(cue.end_ms)]
        assert np.abs(tail).max() == 0.0, "the trim's quiet pad, no tick"
        assert max_jump(x, cue.end_ms - 20, cue.end_ms + 20) < 0.01
        assert max_jump(x, cue.start_ms - 20, cue.start_ms + 20) < 0.03  # a sine starting from 0
    assert x[-1] == 0.0 and x[0] == 0.0


class OffZeroEngine(FakeEngine):
    """A hum that starts and ends at its peak (0.25 s a word): every clip edge needs declicking."""

    def __init__(self, rate=24000):
        super().__init__()
        self.rate = rate

    def synthesize(self, voice, text):
        self.calls.append((voice, text))
        t = np.arange(int(0.25 * self.rate * len(text.split()))) / self.rate
        return (0.3 * np.cos(2 * np.pi * 150 * t)).astype(np.float32), self.rate


def test_clips_that_end_off_zero_are_declicked(store):
    g = generator(store, engine=OffZeroEngine())
    pcm, cues, _ = g.render_block(5)
    x = samples(pcm)
    inner = max_jump(x, cues[0].start_ms + 20, cues[0].end_ms - 20)
    for cue in cues:
        for edge in (cue.start_ms, cue.end_ms):
            assert max_jump(x, edge - 15, edge + 15) <= inner + 0.002
    assert inner < 0.02


@pytest.mark.parametrize("rate", [22050, 44100, 48000])
def test_any_engine_rate_is_resampled_to_24k(store, rate):
    g = generator(store, engine=OffZeroEngine(rate))
    pcm, cues, _ = g.render_block(9)  # "Last words here." -> one 750 ms clip
    assert cues[0].end_ms - cues[0].start_ms == pytest.approx(750, abs=2)
    assert dsp.ms_of(len(pcm) // 2) == pytest.approx(750 + 600, abs=2)  # + the paragraph pause


def test_lead_in_before_headings_and_trailing_pauses(store):
    g = generator(store)
    pcm, cues, _ = g.render_block(2)  # level-1 heading
    assert cues[0].start_ms == 1200
    assert dsp.ms_of(len(pcm) // 2) == cues[-1].end_ms + 900, "a heading is followed by 900 ms"
    assert g.render_block(7)[1][0].start_ms == 800  # level-2 heading
    pcm, cues, _ = g.render_block(3)
    assert cues[0].start_ms == 0
    assert dsp.ms_of(len(pcm) // 2) == cues[-1].end_ms + 600, "paragraph pause"
    assert np.abs(samples(pcm)[: dsp.samples_of(cues[0].end_ms)]).max() > 0


def test_paragraph_pause_follows_the_options(store):
    g = generator(store, options=SpeechOptions(paragraph_pause_ms=1500))
    pcm, cues, _ = g.render_block(3)
    assert dsp.ms_of(len(pcm) // 2) == cues[-1].end_ms + 1500


def test_a_separator_is_silence_without_cues(store):
    g = generator(store)
    pcm, cues, issues = g.render_block(6)
    assert cues == [] and issues == []
    assert dsp.ms_of(len(pcm) // 2) == 1200
    assert not np.any(samples(pcm))
    assert g.engine.calls == []


def test_a_read_block_with_nothing_to_say_is_a_pause(store):
    book = Book("t", "", "", "", "txt", blocks=[Block("heading", "   ", level=1, role="title")])
    g = generator(store, book=book)
    assert g.order() == [0]
    pcm, cues, _ = g.render_block(0)
    assert cues == [] and dsp.ms_of(len(pcm) // 2) == 1200 + 600


def test_a_block_is_leveled_as_a_whole(store):
    quiet = FakeEngine()
    quiet_synth = quiet.synthesize
    quiet.synthesize = lambda voice, text: (quiet_synth(voice, text)[0] * 0.02, 24000)
    for engine in (FakeEngine(), quiet):
        x = samples(generator(store, engine=engine).render_block(5)[0])
        assert loudness.loudness(x, SR) == pytest.approx(loudness.TARGET_LUFS, abs=0.5)
        assert np.abs(x).max() <= 10 ** (loudness.PEAK_CEILING_DBFS / 20) + 1e-4


def test_unread_blocks_are_refused(store):
    g = generator(store)
    for index in (0, 4, 8):
        with pytest.raises(ValueError):
            g.render_block(index)


# ---------- runs ----------


def test_run_makes_every_block_and_reports_progress(store):
    g = generator(store)
    seen, statuses = [], []
    issues = g.run(5, on_block=lambda b, ms: seen.append((b, ms)), on_status=statuses.append)
    assert issues == []
    assert [b for b, _ in seen] == [5, 6, 7, 9, 1, 2, 3]
    assert store.generated() == set(ORDER)
    assert all(ms == store.info(b).duration_ms for b, ms in seen)
    assert statuses[0] == "Reading paragraph 4 of 7"
    assert statuses[-1] == "Reading paragraph 3 of 7"
    assert g.fraction_done() == 1.0
    stored = store.info(5)
    pcm, cues, _ = g.render_block(5)
    assert store.pcm(5) == pcm and stored.cues == cues, "deterministic engine: same audio as render_block"


def test_run_skips_what_exists(store):
    g = generator(store)
    g.run(0, on_block=lambda b, ms: None)
    calls = len(g.engine.calls)
    assert g.run(3) == []
    assert len(g.engine.calls) == calls


def test_run_skips_a_block_finished_meanwhile(store):
    """A run replaced by one from another point can still store the block it was making."""
    g = generator(store)
    made = []

    def on_block(block, ms):
        made.append(block)
        if block == 5:
            store.put(7, dsp.to_pcm16(dsp.silence(100)), [])

    g.run(5, on_block=on_block)
    assert made == [5, 6, 9, 1, 2, 3]
    assert store.info(7).duration_ms == 100
    assert ("Aaron", "A Section.") not in g.engine.calls


class CountingCancel:
    """cancelled() turning True on its nth call."""

    def __init__(self, n: int):
        self.n, self.calls = n, 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls >= self.n


def test_cancel_mid_block_keeps_only_whole_blocks(store):
    g = generator(store)
    reference = {b: g.render_block(b) for b in ORDER}
    # Calls: before block 1, before its one chunk; the same for block 2; before block 3, before
    # its first chunk, then between its two chunks -> cancelled halfway through block 3.
    cancel = CountingCancel(7)
    done = []
    with pytest.raises(GenerationCancelled):
        g.run(0, on_block=lambda b, ms: done.append(b), cancelled=cancel)
    assert done == [1, 2]
    assert store.generated() == {1, 2}
    for b in done:
        assert store.pcm(b) == reference[b][0]
    g.run(0)
    assert store.generated() == set(ORDER)
    assert store.pcm(3) == reference[3][0]


def test_cancel_before_any_block(store):
    g = generator(store)
    with pytest.raises(GenerationCancelled):
        g.run(0, cancelled=lambda: True)
    assert store.generated() == set() and g.engine.calls == []


# ---------- the Whisper check ----------


class ScriptedChecker:
    """A fake Transcriber: the nth check returns the nth verdict (error rates), then passes."""

    def __init__(self, rates):
        self.rates = list(rates)
        self.checked: list[tuple[int, str]] = []

    def check(self, x, sr, text):
        assert sr == SR
        self.checked.append((len(x), text))
        rate = self.rates.pop(0) if self.rates else 0.0
        return Verdict(ok=rate <= 0.15, error_rate=rate, transcript=f"heard {rate}")


class TakeEngine(FakeEngine):
    """Every take of a text is one word longer than the last, so the kept take shows in the cue."""

    def synthesize(self, voice, text):
        takes = sum(1 for _, t in self.calls if t == text)
        x, rate = super().synthesize(voice, text + " uh" * takes)
        self.calls[-1] = (voice, text)
        return x, rate


def test_a_failed_chunk_is_retried_until_it_passes(store):
    checker = ScriptedChecker([0.5, 0.0])
    g = generator(store, engine=TakeEngine(), checker=checker)
    pcm, cues, issues = g.render_block(9)
    assert issues == []
    assert len(g.engine.calls) == 2
    assert cues[0].end_ms - cues[0].start_ms == 160 * 4 - 10, "the passing (second) take is kept"


def test_a_chunk_that_never_passes_keeps_its_best_take_and_is_reported(store):
    checker = ScriptedChecker([0.6, 0.3, 0.9])
    g = generator(store, engine=TakeEngine(), checker=checker)
    pcm, cues, issues = g.render_block(9)
    assert len(g.engine.calls) == MAX_ATTEMPTS == 3
    assert cues[0].end_ms - cues[0].start_ms == 160 * 4 - 10, "take two had the lowest error rate"
    assert len(issues) == 1
    issue = issues[0]
    assert (issue.block, issue.text, issue.transcript, issue.error_rate) == (9, "Last words here.", "heard 0.3", 0.3)


def test_run_collects_the_issues(store):
    g = generator(store, checker=ScriptedChecker([0.9] * 3))
    issues = g.run(9)
    assert [i.block for i in issues] == [9]
    assert store.generated() == set(ORDER), "a chunk that never passed is still stored (best take)"


def test_cancel_is_checked_between_takes(store):
    g = generator(store, checker=ScriptedChecker([0.9] * 9))
    cancel = CountingCancel(4)  # before block 1, before its chunk, then between its first two takes
    with pytest.raises(GenerationCancelled):
        g.run(1, cancelled=cancel)
    assert len(g.engine.calls) == 2
    assert store.generated() == set()


# ---------- engine errors ----------


class FlakyEngine(FakeEngine):
    def __init__(self, failures: int):
        super().__init__()
        self.failures = failures

    def synthesize(self, voice, text):
        if self.failures:
            self.failures -= 1
            raise OSError("model hiccup")
        return super().synthesize(voice, text)


def test_an_engine_error_is_retried_once(store):
    g = generator(store, engine=FlakyEngine(1))
    pcm, cues, _ = g.render_block(9)
    assert len(cues) == 1


def test_a_second_engine_error_names_the_block_and_its_text(store):
    g = generator(store, engine=FlakyEngine(2))
    with pytest.raises(RuntimeError, match=r'block 9 \("Last words here\."\).*model hiccup'):
        g.run(9)
    assert not store.has(9)


# ---------- the 5% rule ----------


def test_stream_ready_from_the_start():
    order = list(range(100))
    weights = {b: 10 for b in order}
    assert not stream_ready(order, set(), 0, weights)
    assert not stream_ready(order, set(range(4)), 0, weights)
    assert stream_ready(order, set(range(5)), 0, weights), "exactly 5%"
    assert STREAM_THRESHOLD == 0.05


def test_stream_ready_threshold_exactly_with_uneven_weights():
    order = [0, 2, 4, 6]
    weights = {0: 3, 2: 17, 4: 20, 6: 20}  # 60 in all
    assert stream_ready(order, {0}, 0, weights, threshold=0.05)  # 3/60 = 5%
    assert not stream_ready(order, {0}, 0, weights, threshold=0.051)
    assert stream_ready(order, {2}, 1, weights, threshold=17 / 60), "from block 1: block 2 is first"
    assert not stream_ready(order, {2}, 1, weights, threshold=18 / 60)


def test_stream_ready_counts_only_the_unbroken_run_from_the_start_point():
    order = list(range(100))
    weights = {b: 1 for b in order}
    generated = {50, 51, 53, 54, 55, 56, 57, 58}  # a gap at 52
    assert not stream_ready(order, generated, 50, weights)
    assert stream_ready(order, generated | {52}, 50, weights)
    assert not stream_ready(order, set(range(10)), 50, weights), "audio elsewhere doesn't count"


def test_stream_ready_near_the_end_when_the_rest_is_done():
    order = list(range(100))
    weights = {b: 1 for b in order}
    assert not stream_ready(order, {97, 98}, 97, weights)
    assert stream_ready(order, {97, 98, 99}, 97, weights), "the run reaches the end of the book"
    assert stream_ready(order, {99}, 99, weights)


def test_stream_ready_between_blocks_and_past_the_end():
    order = [2, 5, 9]
    weights = {2: 10, 5: 10, 9: 10}
    assert stream_ready(order, {9}, 6, weights), "an unread block starts at the next ordered one"
    assert not stream_ready(order, {2}, 3, weights)
    assert stream_ready(order, set(), 10, weights), "nothing after the start: at the end already"
    assert stream_ready([], set(), 0, {})


def test_stream_ready_with_generator_weights(store):
    g = generator(store)
    order, weights = g.order(), g.weights()
    total = sum(weights.values())
    store.put(1, dsp.to_pcm16(dsp.silence(100)), [])
    store.put(2, dsp.to_pcm16(dsp.silence(100)), [])  # "The Harbour", "CHAPTER ONE": 22 characters
    assert stream_ready(order, store.generated(), 0, weights, threshold=22 / total)
    assert not stream_ready(order, store.generated(), 0, weights, threshold=23 / total)
    assert stream_ready(order, store.generated(), 0, weights) == (22 / total >= STREAM_THRESHOLD)


# ---------- real Pocket-TTS ----------

# The opening of Chapter One of Virginia Tufte's "Artful Sentences": an epigraph and three
# paragraphs of prose with quotes, a colon, dashes and italics-free short sentences.
ARTFUL_OPENING = [
    ("heading", "CHAPTER ONE"),
    ("heading", "Short Sentences"),
    (
        "quote",
        "And the words slide into the slots ordained by syntax, and glitter as with atmospheric dust "
        "with those impurities which we call meaning.",
    ),
    (
        "paragraph",
        "ANTHONY BURGESS is right: it is the words that shine and sparkle and glitter, sometimes radiant "
        "with an author\u2019s inspired choice. But it is syntax that gives words the power to relate to "
        "each other in a sequence, to create rhythms and emphasis, to carry meaning\u2014of whatever "
        "kind\u2014as well as glow individually in just the right place.",
    ),
    (
        "paragraph",
        "The basic unit of English syntax is the clause. Its \u201cslots ordained by syntax\u201d are a "
        "subject and a predicate. What traditional grammarians call a \u201csimple\u201d sentence consists "
        "of an independent clause, independent in that it makes sense without being attached to anything: "
        "Time flies. Without losing its nature as a basic sentence, however, a \u201csimple\u201d sentence "
        "may include optional added slots such as spaces for modifiers, complements, objects.",
    ),
    (
        "paragraph",
        "Professional writers frequently use for particular effects these short sentences, their very "
        "length making effective juxtaposition with longer more complicated sentences. Often also, in "
        "creating a long sentence, good writers construct a short independent clause and attach free "
        "modifiers loosely around it. It is as if a short sentence serves as nucleus inside a long sentence.",
    ),
]


def _artful_opening() -> Book:
    blocks = [Block(kind, text, level=1 if kind == "heading" else 0) for kind, text in ARTFUL_OPENING]
    return Book("artful", "Artful Sentences", "Virginia Tufte", "", "txt", blocks=blocks)


def _tail_bursts(x: np.ndarray, end_ms: int) -> list[tuple[int, int]]:
    """Sounds in the last 150 ms before end_ms that are short (<= 120 ms) and stand alone
    after >= 30 ms of quiet: (start, end) in ms before end_ms. 10 ms windows, loud = within
    40 dB of the block's peak."""
    win = dsp.samples_of(10)
    a = max(0, dsp.samples_of(end_ms - 150))
    seg = np.abs(x[a : dsp.samples_of(end_ms)])
    n = len(seg) // win
    peaks = seg[: n * win].reshape(n, win).max(axis=1)
    loud = peaks > np.abs(x).max() * 10 ** (-40 / 20)
    runs = np.flatnonzero(np.diff(np.concatenate(([0], loud.astype(np.int8), [0])))).reshape(-1, 2)
    return [
        (int(n - s) * 10, int(n - e) * 10)
        for (_, prev_end), (s, e) in zip(runs, runs[1:])
        if (e - s) * 10 <= 120 and (s - prev_end) * 10 >= 30
    ]


@pytest.mark.slow
def test_pocket_tts_reads_the_opening_of_artful_sentences(tmp_path):
    from livrosound.engines import engine_named
    from livrosound.validate import Transcriber, word_error_rate

    engine = engine_named("Pocket-TTS")
    ok, why = engine.available()
    if not ok:
        pytest.skip(why)
    book = _artful_opening()
    assert len(book.blocks) == 6 and all(len(b.text) > 150 for b in book.blocks[3:])
    store = AudioStore(tmp_path / "audio.sqlite")
    g = Generator(book, store, engine, "Aaron", SpeechOptions())
    engine.synthesize("Aaron", "Warming up.")  # the model loads once (~7 s): not part of the speed

    started = time.perf_counter()
    issues = g.run(0)
    elapsed = time.perf_counter() - started
    audio_s = sum(store.durations().values()) / 1000
    speed = audio_s / elapsed
    print(f"\n{audio_s:.1f} s of audio in {elapsed:.1f} s: {speed:.2f}x realtime")
    assert issues == [] and store.generated() == set(range(6))
    assert speed > 1.5

    pcm = b"".join(store.pcm(b) for b in g.order())
    with wave.open(str(tmp_path / "artful_opening_aaron.wav"), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(pcm)
    print(f"saved {tmp_path / 'artful_opening_aaron.wav'}")

    joins, speech_jumps, bursts = [], [], []
    for b in g.order():
        x = samples(store.pcm(b))
        cues = store.info(b).cues
        for cue in cues:
            joins.append(max(max_jump(x, cue.start_ms - 10, cue.start_ms + 3), max_jump(x, cue.end_ms - 3, cue.end_ms + 10)))
            speech_jumps.append(max_jump(x, cue.start_ms, cue.end_ms))
            bursts += [(b, cue.end_ms, burst) for burst in _tail_bursts(x, cue.end_ms)]
            # The clip's last 20 ms is the trim's quiet pad, ramped to zero: nothing cut off.
            tail = np.abs(x[dsp.samples_of(cue.end_ms - 5) : dsp.samples_of(cue.end_ms)])
            assert tail.max() < 0.01, (b, cue)
    print(f"{len(joins)} joins: worst step {max(joins):.4f}; inside speech the median worst step is "
          f"{np.median(speech_jumps):.3f}; short bursts at chunk tails: {bursts}")
    assert max(joins) < 0.02
    assert not [burst for burst in bursts if burst[2][1] < 25], "a short sound running into a chunk's end"

    checker = Transcriber()
    if checker.available()[0]:
        options = SpeechOptions()
        for b in g.order():
            text = " ".join(c.text for c in block_chunks(book, b, options))
            transcript = checker.transcribe(samples(store.pcm(b)), SR)
            rate = word_error_rate(text, transcript)
            print(f"block {b}: error rate {rate:.3f}\n  said:  {text}\n  heard: {transcript}")
            assert rate < 0.15, (text, transcript)
    store.close()


@pytest.mark.slow
def test_whisper_check_with_real_models(tmp_path):
    """The Whisper check wired to real models: a clean heading and paragraph pass first time."""
    from livrosound.engines import engine_named
    from livrosound.validate import Transcriber

    engine, checker = engine_named("Pocket-TTS"), Transcriber()
    if not (engine.available()[0] and checker.available()[0]):
        pytest.skip("Pocket-TTS or Whisper isn't installed")
    store = AudioStore(tmp_path / "audio.sqlite")
    g = generator(store, engine=engine, checker=checker)
    issues = []
    for block in (2, 3):
        _, cues, block_issues = g.render_block(block)
        issues += block_issues
        assert cues
    assert issues == [], issues
    store.close()
