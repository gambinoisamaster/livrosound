"""How the library page orders and groups the books: pure functions over BookRecords.

The library page (ui/library_page.py) shows every book as a cover in a grid or
a row in a list. The user picks one grouping and two sort keys (Sort by …, Then
by …), each ascending or descending, like Readest's ⋯ menu. Kept free of Qt
so every rule is tested directly.

Rules:
- Sorting is stable and never fails on missing data: a book without the value
  (no series, no publication date, pages unknown) sorts after the books that
  have it, whichever the direction.
- Text compares case- and accent-insensitively, ignoring a leading "The", "A"
  or "An" for titles (as Apple Books does). Authors sort by last name.
- A book can sit in several groups (two tags, two subjects, two collections);
  books with none go to a last group ("No Tags", "Unsorted", …). Groups are in
  name order; "Status" groups go Reading, Not Started, Finished.
- Time remaining is estimated from the spoken characters after the reader's
  position at narration speed (CHARS_PER_SECOND at 1×).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable

from .library import BookRecord

CHARS_PER_SECOND = 15.0  # Pocket-TTS narration at 1×, with its pauses

SORTS: dict[str, str] = {
    "title": "Title",
    "author": "Author",
    "format": "Format",
    "pages": "Pages",
    "series": "Series",
    "date_read": "Date Read",
    "date_added": "Date Added",
    "date_published": "Date Published",
    "progress": "Progress Read",
    "time_remaining": "Time Remaining",
}

GROUPS: dict[str, str] = {
    "none": "None",
    "authors": "Authors",
    "tags": "Tags",
    "subjects": "Subjects",
    "collections": "Collections",
    "series": "Series",
    "status": "Status",
}

_NONE_LABEL = {
    "authors": "Unknown Author",
    "tags": "No Tags",
    "subjects": "No Subjects",
    "collections": "Not in a Collection",
    "series": "Not in a Series",
}
STATUS_ORDER = ("Reading", "Not Started", "Finished")


def fold(text: str) -> str:
    """Lower case without accents, for comparing and searching."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).casefold().strip()


def title_key(title: str) -> str:
    return re.sub(r"^(?:the|a|an)\s+", "", fold(title))


def author_key(author: str) -> str:
    """'Leo Tolstoy' -> 'tolstoy leo'; the first of several authors counts."""
    first = re.split(r"\s+and\s+|,\s*|;\s*|&", author.strip())[0]
    words = fold(first).split()
    return " ".join(words[-1:] + words[:-1]) if words else ""


def strings(values: object) -> list[str]:
    """A record's list field as clean strings (library.json may hold anything)."""
    if not isinstance(values, list):
        return []
    out = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in out:
            out.append(value.strip())
    return out


def read_fraction(record: BookRecord) -> float:
    return min(1.0, max(0.0, record.read_fraction)) if record.read_fraction >= 0 else 0.0


def status(record: BookRecord) -> str:
    fraction = read_fraction(record)
    if fraction >= 0.99:
        return "Finished"
    if fraction > 0 or record.position_block > 0 or record.position_ms > 0:
        return "Reading"
    return "Not Started"


def seconds_remaining(record: BookRecord, rate: float = 1.0) -> float | None:
    """About how long the rest of the book takes to hear, or None if its length is unknown."""
    if record.spoken_chars <= 0:
        return None
    left = record.spoken_chars * (1 - read_fraction(record))
    return left / (CHARS_PER_SECOND * max(0.25, rate))


def duration_text(seconds: float) -> str:
    """'7h 15m', '48m', 'under a minute'."""
    minutes = math.ceil(seconds / 60)
    if minutes < 1:
        return "under a minute"
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h" if hours else f"{minutes}m"


def date_added(record: BookRecord) -> str:
    return record.date_added or record.last_opened


def _value(record: BookRecord, key: str):
    """The sort value of a record, or None when it has none (sorted last)."""
    if key == "title":
        return title_key(record.title) or None
    if key == "author":
        return author_key(record.author) or None
    if key == "format":
        return record.source_kind or None
    if key == "pages":
        return record.pages or None
    if key == "series":
        return (fold(record.series), record.series_index) if record.series else None
    if key == "date_read":
        return record.last_opened or None
    if key == "date_added":
        return date_added(record) or None
    if key == "date_published":
        return record.published or None
    if key == "progress":
        return read_fraction(record)
    if key == "time_remaining":
        return seconds_remaining(record)
    raise ValueError(f"unknown sort {key!r}")


def sort_records(records: Iterable[BookRecord], key: str, descending: bool = False,
                 then: str = "", then_descending: bool = False) -> list[BookRecord]:
    """Records ordered by key, ties by then (and finally by title); missing values last."""
    out = sorted(records, key=lambda r: title_key(r.title))
    for k, desc in ((then, then_descending), (key, descending)):  # least significant first (stable)
        if not k:
            continue
        have = [r for r in out if _value(r, k) is not None]
        lack = [r for r in out if _value(r, k) is None]
        have.sort(key=lambda r: _value(r, k), reverse=desc)
        out = have + lack
    return out


def _labels(record: BookRecord, group: str) -> list[str]:
    if group == "authors":
        names = [a.strip() for a in re.split(r"\s+and\s+|;\s*|&", record.author) if a.strip()]
        return names
    if group == "tags":
        return strings(record.tags)
    if group == "subjects":
        return strings(record.subjects)
    if group == "collections":
        return strings(record.collections)
    if group == "series":
        return [record.series] if record.series else []
    if group == "status":
        return [status(record)]
    raise ValueError(f"unknown grouping {group!r}")


def group_records(records: list[BookRecord], group: str) -> list[tuple[str, list[BookRecord]]]:
    """[(label, records in the given order)], one per group; "none" gives one unnamed group."""
    if group == "none":
        return [("", list(records))] if records else []
    groups: dict[str, list[BookRecord]] = {}
    names: dict[str, str] = {}  # folded -> the first spelling met
    loose: list[BookRecord] = []
    for record in records:
        labels = _labels(record, group)
        if not labels:
            loose.append(record)
        for label in labels:
            k = fold(label)
            names.setdefault(k, label)
            groups.setdefault(k, []).append(record)
    if group == "status":
        ordered = [k for k in map(fold, STATUS_ORDER) if k in groups]
    elif group == "authors":
        ordered = sorted(groups, key=author_key)
    else:
        ordered = sorted(groups)
    out = [(names[k], groups[k]) for k in ordered]
    if loose:
        out.append((_NONE_LABEL.get(group, "Other"), loose))
    return out


def matches(record: BookRecord, query: str) -> bool:
    """The library's search: every word of the query in the title, author, series,
    subjects, tags or collections."""
    words = fold(query).split()
    if not words:
        return True
    haystack = fold(" ".join([
        record.title, record.author, record.series, record.source_kind,
        *strings(record.subjects), *strings(record.tags), *strings(record.collections),
    ]))
    return all(w in haystack for w in words)


def all_values(records: Iterable[BookRecord], field: str) -> list[str]:
    """Every tag (or collection) in use, in name order, one spelling each."""
    seen: dict[str, str] = {}
    for record in records:
        for value in strings(getattr(record, field)):
            seen.setdefault(fold(value), value)
    return [seen[k] for k in sorted(seen)]

