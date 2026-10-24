from __future__ import annotations

import sqlite3
import threading

import pytest

from livrosound.audio_store import AudioStore, BlockAudio, Cue, cue_at


def pcm_ms(ms: int, fill: int = 1) -> bytes:
    """16-bit mono PCM at 24 kHz lasting ms (every sample = fill, so blocks are distinguishable)."""
    return fill.to_bytes(2, "little", signed=True) * (24 * ms)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "book" / "audio.sqlite"  # the folder doesn't exist yet


@pytest.fixture
def store(path):
    s = AudioStore(path)
    yield s
    s.close()


def test_new_store_is_empty(store, path):
    assert path.is_file()
    assert store.signature == ""
    assert store.generated() == set()
    assert store.durations() == {}
    assert not store.has(0)
    assert store.pcm(0) is None
    assert store.info(0) is None


def test_put_and_read_back(store):
    cues = [Cue(0, 400, 0, 12), Cue(750, 1500, 13, 40)]
    store.put(3, pcm_ms(1500, fill=7), cues)
    assert store.has(3)
    assert not store.has(4)
    assert store.generated() == {3}
    assert store.durations() == {3: 1500}
    assert store.pcm(3) == pcm_ms(1500, fill=7)
    assert store.info(3) == BlockAudio(3, 1500, cues)


def test_duration_comes_from_the_pcm_length(store):
    store.put(0, b"\x00\x00" * 24000, [])  # 1 s
    store.put(1, b"\x00\x00" * 24011, [])  # 1000.46 ms
    store.put(2, b"\x00\x00" * 24013, [])  # 1000.54 ms
    store.put(3, b"", [])
    assert store.durations() == {0: 1000, 1: 1000, 2: 1001, 3: 0}


def test_odd_byte_count_is_rejected(store):
    with pytest.raises(ValueError):
        store.put(0, b"\x00\x00\x00", [])
    assert not store.has(0)


def test_put_replaces(store):
    store.put(5, pcm_ms(1000, fill=1), [Cue(0, 1000, 0, 5)])
    store.put(5, pcm_ms(250, fill=2), [])
    assert store.pcm(5) == pcm_ms(250, fill=2)
    assert store.info(5) == BlockAudio(5, 250, [])
    assert store.durations() == {5: 250}


def test_reads_return_copies(store):
    store.put(1, pcm_ms(10), [])
    store.generated().add(99)
    store.durations()[99] = 1
    assert store.generated() == {1}
    assert store.durations() == {1: 10}


def test_reset_drops_audio_and_sets_signature(store):
    store.put(1, pcm_ms(100), [])
    store.put(2, pcm_ms(100), [])
    store.reset("abc")
    assert store.signature == "abc"
    assert store.generated() == set()
    assert store.pcm(1) is None
    store.put(1, pcm_ms(50), [])
    assert store.durations() == {1: 50}


def test_reset_gives_disk_space_back(path):
    def size_on_disk() -> int:  # the database plus its write-ahead log
        return sum(p.stat().st_size for p in path.parent.glob(path.name + "*"))

    store = AudioStore(path)
    for block in range(20):
        store.put(block, pcm_ms(10_000), [])  # 480 KB each
    big = size_on_disk()
    store.reset("new")
    store.close()
    assert big > 9_000_000
    assert size_on_disk() < 200_000


def test_reopen_keeps_everything(path):
    store = AudioStore(path)
    store.reset("sig-1")
    store.put(4, pcm_ms(300, fill=4), [Cue(0, 300, 0, 9)])
    store.put(8, pcm_ms(700, fill=8), [])
    store.close()

    again = AudioStore(path)
    try:
        assert again.signature == "sig-1"
        assert again.generated() == {4, 8}
        assert again.durations() == {4: 300, 8: 700}
        assert again.pcm(8) == pcm_ms(700, fill=8)
        assert again.info(4) == BlockAudio(4, 300, [Cue(0, 300, 0, 9)])
    finally:
        again.close()


def test_file_is_in_wal_mode(store, path):
    store.put(0, pcm_ms(10), [])
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_closed_store_refuses_access_and_can_be_reopened(path):
    store = AudioStore(path)
    store.put(1, pcm_ms(10), [])
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.pcm(1)
    with pytest.raises(sqlite3.ProgrammingError):
        store.put(2, pcm_ms(10), [])
    store.close()  # twice is fine
    again = AudioStore(path)
    assert again.pcm(1) == pcm_ms(10)
    again.close()


def run_in_thread(fn):
    """Run fn in a new thread; return its result or re-raise its exception here."""
    out: dict = {}

    def target():
        try:
            out["value"] = fn()
        except BaseException as e:
            out["error"] = e

    t = threading.Thread(target=target)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()
    if "error" in out:
        raise out["error"]
    return out.get("value")


def test_usable_from_other_threads(store):
    # The store is created on this thread; another thread gets its own connection.
    run_in_thread(lambda: store.put(2, pcm_ms(200, fill=2), [Cue(0, 200, 0, 3)]))
    assert store.has(2)
    assert store.pcm(2) == pcm_ms(200, fill=2)
    assert run_in_thread(lambda: store.info(2)) == BlockAudio(2, 200, [Cue(0, 200, 0, 3)])


def test_other_threads_close_their_connection_after_close(path):
    store = AudioStore(path)
    opened = threading.Event()
    closed = threading.Event()
    result: dict = {}

    def worker():
        store.put(1, pcm_ms(10), [])  # opens this thread's connection
        opened.set()
        closed.wait(10)
        try:
            store.pcm(1)
        except sqlite3.ProgrammingError as e:
            result["error"] = e
        result["conn"] = getattr(store._local, "conn", None)

    t = threading.Thread(target=worker)
    t.start()
    assert opened.wait(10)
    store.close()
    closed.set()
    t.join(10)
    assert isinstance(result.get("error"), sqlite3.ProgrammingError)
    assert result["conn"] is None


def test_concurrent_writer_and_readers(path):
    store = AudioStore(path)
    store.reset("sig")
    blocks = 120
    stop = threading.Event()
    errors: list[BaseException] = []
    seen_max = [0, 0, 0]

    def writer():
        try:
            for b in range(blocks):
                store.put(b, pcm_ms(20 + b, fill=b), [Cue(0, 20 + b, 0, b)])
        except BaseException as e:
            errors.append(e)
        finally:
            stop.set()

    def reader(slot: int):
        try:
            while True:
                done = stop.is_set()
                generated = store.generated()
                durations = store.durations()
                for b in generated:
                    # Anything reported as generated is fully readable, from any thread.
                    assert store.has(b)
                    assert durations.get(b, 20 + b) == 20 + b
                    assert store.pcm(b) == pcm_ms(20 + b, fill=b)
                    assert store.info(b) == BlockAudio(b, 20 + b, [Cue(0, 20 + b, 0, b)])
                seen_max[slot] = max(seen_max[slot], len(generated))
                if done:
                    return
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert store.generated() == set(range(blocks))
    assert seen_max == [blocks] * 3  # every reader saw the finished set
    store.close()

    again = AudioStore(path)
    assert again.durations() == {b: 20 + b for b in range(blocks)}
    again.close()


# ---------- cue_at ----------

CUES = [Cue(0, 900, 0, 10), Cue(1000, 2000, 11, 30), Cue(2400, 3000, 31, 50)]


def test_cue_at_empty():
    assert cue_at([], 0) is None
    assert cue_at([], 5000) is None


def test_cue_at_before_the_first_cue():
    assert cue_at([Cue(500, 900, 0, 4)], 0) is None
    assert cue_at([Cue(500, 900, 0, 4)], 499) is None
    assert cue_at(CUES, -1) is None


def test_cue_at_finds_the_last_cue_started():
    assert cue_at(CUES, 0) == CUES[0]
    assert cue_at(CUES, 950) == CUES[0]  # in the pause after it
    assert cue_at(CUES, 999) == CUES[0]
    assert cue_at(CUES, 1000) == CUES[1]
    assert cue_at(CUES, 2399) == CUES[1]
    assert cue_at(CUES, 2400) == CUES[2]
    assert cue_at(CUES, 10**9) == CUES[2]
