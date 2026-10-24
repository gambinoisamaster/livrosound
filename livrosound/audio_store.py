"""A book's generated audio: one SQLite file per book, one row per block.

Why SQLite and not a WAV per block: a long book has thousands of blocks; one
file keeps a book's audio together, every block lands atomically (a crash mid-
write never leaves half a block), and in WAL mode the generator thread can
write while the player and the UI read. Audio is raw 16-bit mono PCM at 24 kHz,
so a block's duration follows from its length.

Rules:
- One sqlite3 connection per thread (threading.local), each used and closed
  only by the thread that opened it. close() closes the caller's connection
  and marks the store closed; any other thread closes its own connection the
  next time it touches the store (that access raises), or when it exits.
- generated(), has() and durations() are answered from memory, under a lock:
  the UI asks constantly (progress, Contents dots, the 5% rule) while the
  generator thread writes. A block enters that memory only after its row is
  committed, so whoever sees it can read its audio.
- The signature records how the audio was made (engine, voice, speech options:
  library.audio_signature). reset() drops all audio made another way.
- The pcm column comes last in the row, so reading cues and durations never
  touches the audio's overflow pages; incremental auto-vacuum gives the space
  back after a reset (a 10-hour book is ~1.7 GB of PCM).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from bisect import bisect_right
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 24000  # = dsp.SAMPLE_RATE; stored audio is 16-bit mono at this rate
_BYTES_PER_SAMPLE = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blocks (
    block INTEGER PRIMARY KEY,
    duration_ms INTEGER NOT NULL,
    cues TEXT NOT NULL,
    pcm BLOB NOT NULL
);
"""


@dataclass
class Cue:
    """A chunk inside a block's audio: when it's heard and which characters of Block.text it voices."""

    start_ms: int
    end_ms: int
    start_char: int
    end_char: int


@dataclass
class BlockAudio:
    block: int
    duration_ms: int
    cues: list[Cue]


class AudioStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._closed = False
        self._write_lock = threading.Lock()  # a DB write and its memory update happen together
        self._mem_lock = threading.Lock()
        conn = self._conn()
        conn.executescript(_SCHEMA)
        rows = conn.execute("SELECT value FROM meta WHERE key = 'signature'").fetchall()
        self._signature: str = rows[0][0] if rows else ""
        self._durations: dict[int, int] = dict(conn.execute("SELECT block, duration_ms FROM blocks").fetchall())

    # ---------- connections ----------

    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            self._close_own()
            raise sqlite3.ProgrammingError(f"audio store is closed: {self.path}")
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            # Autocommit: single statements are atomic; multi-statement writes use _write().
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            # Takes effect only on a new, empty file (before WAL and the tables); a no-op after.
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")  # durable enough with WAL, much faster
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _close_own(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            conn.close()

    def close(self) -> None:
        """Close this thread's connection; the store can't be used afterwards (reopen it instead)."""
        self._closed = True
        self._close_own()

    # ---------- writes ----------

    @property
    def signature(self) -> str:
        """How the stored audio was made; "" for a new store."""
        return self._signature

    def reset(self, signature: str) -> None:
        """Drop all audio and record the signature the new audio will be made with."""
        with self._write_lock:
            with self._write() as conn:
                conn.execute("DELETE FROM blocks")
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('signature', ?)", (signature,))
            with self._mem_lock:
                self._durations.clear()
                self._signature = signature
            # Both pragmas work a step at a time: fetch to run them to completion.
            conn.execute("PRAGMA incremental_vacuum").fetchall()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def put(self, block: int, pcm: bytes, cues: list[Cue]) -> None:
        """Store (or replace) a block's audio: 16-bit mono PCM at 24 kHz and its cues."""
        if len(pcm) % _BYTES_PER_SAMPLE:
            raise ValueError(f"block {block}: 16-bit PCM must have an even number of bytes, got {len(pcm)}")
        duration = round(len(pcm) / _BYTES_PER_SAMPLE * 1000 / SAMPLE_RATE)
        cues_json = json.dumps([[c.start_ms, c.end_ms, c.start_char, c.end_char] for c in cues])
        with self._write_lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO blocks (block, duration_ms, cues, pcm) VALUES (?, ?, ?, ?)",
                (block, duration, cues_json, pcm),
            )
            with self._mem_lock:
                self._durations[block] = duration

    # ---------- reads ----------

    def has(self, block: int) -> bool:
        with self._mem_lock:
            return block in self._durations

    def generated(self) -> set[int]:
        """The blocks that have audio (a copy)."""
        with self._mem_lock:
            return set(self._durations)

    def durations(self) -> dict[int, int]:
        """Block -> duration in ms, for every block that has audio (a copy)."""
        with self._mem_lock:
            return dict(self._durations)

    def pcm(self, block: int) -> bytes | None:
        rows = self._conn().execute("SELECT pcm FROM blocks WHERE block = ?", (block,)).fetchall()
        return rows[0][0] if rows else None

    def info(self, block: int) -> BlockAudio | None:
        rows = self._conn().execute("SELECT duration_ms, cues FROM blocks WHERE block = ?", (block,)).fetchall()
        if not rows:
            return None
        duration, cues_json = rows[0]
        return BlockAudio(block, duration, [Cue(*c) for c in json.loads(cues_json)])


def cue_at(cues: list[Cue], ms: int) -> Cue | None:
    """The last cue starting at or before ms (None before the first). Cues are in time order."""
    i = bisect_right(cues, ms, key=lambda c: c.start_ms)
    return cues[i - 1] if i else None
