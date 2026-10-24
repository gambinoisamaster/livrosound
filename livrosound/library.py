"""User settings and the library of books, persisted as JSON under the cache folder.

Two files, two lifetimes (like Script Radio's settings.py):
- settings.json — preferences that outlive any one book (theme, sizes, voice,
                  speech options).
- library.json  — every book opened: where it came from, its layout and voice,
                  where the reader left off, how far its audio got. Lets "Resume
                  a Previous Book" reopen it exactly where it was closed.

Rules:
- Paths are resolved on every call (tests point LIVROSOUND_CACHE elsewhere).
- Writes are atomic (temp file + rename): a crash mid-write never corrupts a file.
- Reads are tolerant: unknown keys (from newer versions) are ignored, missing
  ones and values of the wrong type take the default, an unreadable file is an
  empty one. A hand-edited or half-old file never stops the app from starting.
- Library changes stay in memory until save().
- remove() deletes a book's cache folder only when it is a real folder (not a
  symlink) directly inside books_dir(), so an odd book id or a hand-made link
  can never delete anything else.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path
from typing import TypeVar

from livrosound import paths
from livrosound.speech import SPEECH_VERSION, SpeechOptions

_T = TypeVar("_T")


@dataclass
class Settings:
    # Reading theme: "original", "quiet", "calm" or "dark" (see ui/theme.py).
    theme: str = "original"
    reflow_font_size: int = 20  # LivroSound layout, in points
    page_zoom: float = 1.0  # original-layout PDF pages (1.0 = the text box fills the width)
    highlight: bool = True
    playback_rate: float = 1.0
    paragraph_pause: float = 0.6  # seconds between paragraphs
    read_headings: bool = True
    read_footnotes: bool = False
    # Transcribe every generated chunk with Whisper and regenerate mismatches.
    validate_audio: bool = False
    voice: str = "Aaron"

    @classmethod
    def load(cls) -> "Settings":
        return _from_dict(cls, _read_json(paths.settings_path()))

    def save(self) -> None:
        _write_json(paths.settings_path(), asdict(self))

    def speech_options(self) -> SpeechOptions:
        return SpeechOptions(
            paragraph_pause_ms=round(self.paragraph_pause * 1000),
            read_headings=self.read_headings,
            read_footnotes=self.read_footnotes,
        )


@dataclass
class BookRecord:
    book_id: str
    title: str
    author: str
    source_path: str
    source_kind: str  # "pdf" | "epub" | "docx" | "txt"
    layout: str = "livrosound"  # "original" | "livrosound"
    engine: str = ""
    voice: str = ""
    audio_signature: str = ""  # audio_signature() of the audio in the book's store
    position_block: int = 0
    position_ms: int = 0
    last_opened: str = ""  # ISO timestamp
    generated_fraction: float = 0.0
    generation_start: int = -1  # block an unfinished generation run started from; -1 = none
    # The SpeechOptions (asdict) the book's audio is made with, so continuing its
    # generation later matches it even after the settings changed; {} = none yet.
    speech_options: dict = field(default_factory=dict)
    # Share of the book's spoken characters before the saved position (the library's
    # "34% read"), kept up to date as the position is saved; -1 = not known yet.
    read_fraction: float = -1.0

    def audio_options(self) -> SpeechOptions | None:
        """speech_options as SpeechOptions (None when unset or unreadable)."""
        if not self.speech_options:
            return None
        return _from_dict(SpeechOptions, self.speech_options)


class Library:
    """Every book opened, by id."""

    def __init__(self, records: Iterable[BookRecord] = ()):
        self._records = {r.book_id: r for r in records}

    @classmethod
    def load(cls) -> "Library":
        data = _read_json(paths.library_path())
        books = data.get("books") if isinstance(data, dict) else None
        if not isinstance(books, list):
            return cls()
        records = (_from_dict(BookRecord, b) for b in books if isinstance(b, dict))
        return cls(r for r in records if r.book_id)

    def save(self) -> None:
        _write_json(paths.library_path(), {"books": [asdict(r) for r in self._records.values()]})

    def get(self, book_id: str) -> BookRecord | None:
        return self._records.get(book_id)

    def upsert(self, record: BookRecord) -> None:
        """Add a book, or replace the record with the same id."""
        self._records[record.book_id] = record

    def remove(self, book_id: str, *, delete_files: bool = True) -> None:
        """Forget a book and (by default) delete its cache folder: its text and audio."""
        self._records.pop(book_id, None)
        if not delete_files:
            return
        folder = paths.book_dir(book_id)
        if folder.is_dir() and not folder.is_symlink() and folder.resolve().parent == paths.books_dir().resolve():
            shutil.rmtree(folder)

    def recent(self) -> list[BookRecord]:
        """Newest last_opened first; never-opened records last."""
        return sorted(self._records.values(), key=lambda r: r.last_opened, reverse=True)


def audio_signature(engine: str, voice: str, options: SpeechOptions) -> str:
    """A fingerprint of everything that shapes a book's audio: stored audio made with a
    different engine, voice, speech version or option is stale."""
    spec = {"engine": engine, "voice": voice, "speech_version": SPEECH_VERSION, "options": asdict(options)}
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()


def _from_dict(cls: type[_T], data: object) -> _T:
    """An instance of the dataclass cls from parsed JSON, tolerating whatever is in it.
    Fields without a default (BookRecord's identity) fall back to ""."""
    values = {}
    for f in fields(cls):
        if f.default is not MISSING:
            default = f.default
        elif f.default_factory is not MISSING:
            default = f.default_factory()
        else:
            default = ""
        values[f.name] = _coerce(data.get(f.name), default) if isinstance(data, dict) else default
    return cls(**values)


def _coerce(value: object, default):
    """value if it has default's type (ints accepted for floats, whole floats for ints), else default."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(value, bool):
        return default
    if isinstance(default, int):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value if isinstance(value, int) else default
    if isinstance(default, float):
        return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else default
    return value if isinstance(value, type(default)) else default


def _read_json(path: Path) -> object:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp file beside the target, so two writers never share one.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
