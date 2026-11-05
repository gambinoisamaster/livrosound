"""The reader's own highlights and notes in a book, kept in book_dir/annotations.json.

A highlight spans from (start_block, start_char) to (end_block, end_char), end
exclusive, so one can run across paragraphs; it remembers the text it covers
(for the Notes list, and so it still reads sensibly if a re-parse moves the
blocks), a color from COLORS and an optional note. Reads are tolerant like
library.py's (a broken file is an empty one); writes are atomic.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime

from . import paths
from .book import Book

COLORS: dict[str, str] = {  # name -> highlighter color (drawn see-through over the text)
    "yellow": "#FFD84D",
    "green": "#7ED37A",
    "blue": "#6FB7FF",
    "pink": "#FF8FC2",
    "purple": "#B794F6",
}
DEFAULT_COLOR = "yellow"


@dataclass
class Annotation:
    start_block: int
    start_char: int
    end_block: int
    end_char: int
    text: str = ""
    color: str = DEFAULT_COLOR
    note: str = ""
    created: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def spans(self, book: Book) -> list[tuple[int, int, int]]:
        """(block, start, end) per block the highlight covers."""
        out = []
        for block in range(max(0, self.start_block), min(len(book.blocks) - 1, self.end_block) + 1):
            length = len(book.blocks[block].text)
            start = self.start_char if block == self.start_block else 0
            end = self.end_char if block == self.end_block else length
            if end > start:
                out.append((block, max(0, start), min(length, end)))
        return out

    def covers(self, block: int, char: int) -> bool:
        return (self.start_block, self.start_char) <= (block, char) < (self.end_block, self.end_char)


def text_between(book: Book, start: tuple[int, int], end: tuple[int, int]) -> str:
    """The book's text from start to end (blocks joined by a blank line)."""
    parts = []
    for block in range(start[0], end[0] + 1):
        if not 0 <= block < len(book.blocks):
            continue
        text = book.blocks[block].text
        a = start[1] if block == start[0] else 0
        b = end[1] if block == end[0] else len(text)
        parts.append(text[a:b])
    return "\n\n".join(p for p in parts if p.strip())


class Annotations:
    """One book's highlights, in reading order."""

    def __init__(self, book_id: str):
        self.book_id = book_id
        self._items: list[Annotation] = []
        self.load()

    def _path(self):
        return paths.book_dir(self.book_id) / "annotations.json"

    def load(self) -> None:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        names = {f.name for f in fields(Annotation)}
        items = []
        for raw in data if isinstance(data, list) else []:
            if not isinstance(raw, dict):
                continue
            try:
                item = Annotation(**{k: v for k, v in raw.items() if k in names})
                if not all(isinstance(getattr(item, k), int) for k in ("start_block", "start_char", "end_block", "end_char")):
                    continue
            except TypeError:
                continue
            if item.color not in COLORS:
                item.color = DEFAULT_COLOR
            items.append(item)
        self._items = sorted(items, key=_order)

    def save(self) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="annotations.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([asdict(a) for a in self._items], f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)

    def all(self) -> list[Annotation]:
        return list(self._items)

    def add(self, book: Book, start: tuple[int, int], end: tuple[int, int], color: str = DEFAULT_COLOR,
            note: str = "") -> Annotation:
        if end < start:
            start, end = end, start
        item = Annotation(*start, *end, text=text_between(book, start, end), color=color, note=note,
                          created=datetime.now().isoformat(timespec="seconds"))
        self._items.append(item)
        self._items.sort(key=_order)
        self.save()
        return item

    def get(self, item_id: str) -> Annotation | None:
        return next((a for a in self._items if a.id == item_id), None)

    def update(self, item_id: str, *, color: str | None = None, note: str | None = None) -> None:
        item = self.get(item_id)
        if item is None:
            return
        if color is not None and color in COLORS:
            item.color = color
        if note is not None:
            item.note = note.strip()
        self.save()

    def remove(self, item_id: str) -> None:
        self._items = [a for a in self._items if a.id != item_id]
        self.save()

    def at(self, block: int, char: int) -> Annotation | None:
        """The highlight under a char (the latest made, where they overlap)."""
        hits = [a for a in self._items if a.covers(block, char)]
        return max(hits, key=lambda a: a.created) if hits else None


def _order(a: Annotation) -> tuple[int, int, int, int]:
    return a.start_block, a.start_char, a.end_block, a.end_char
