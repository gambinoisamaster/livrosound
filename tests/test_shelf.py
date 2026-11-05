"""The library page's ordering, grouping and search (shelf.py), straight on BookRecords."""

from __future__ import annotations

import pytest

from livrosound import shelf
from livrosound.library import BookRecord


def rec(book_id, title, author="", **kw) -> BookRecord:
    return BookRecord(book_id, title, author, f"/x/{book_id}", kw.pop("kind", "epub"), **kw)


@pytest.fixture
def books():
    return [
        rec("a", "The Trial", "Franz Kafka", kind="docx", pages=200, last_opened="2026-10-01T10:00:00",
            date_added="2026-01-01T10:00:00", published="1925", read_fraction=1.0, spoken_chars=300_000,
            tags=["classic"], collections=["Summer"], subjects=["Fiction", "Law"]),
        rec("b", "Anna Karenina", "Leo Tolstoy", pages=900, last_opened="2026-10-23T10:00:00",
            date_added="2026-05-01T10:00:00", published="1877", read_fraction=0.25, spoken_chars=2_000_000,
            tags=["classic", "russian"], series="Russian Novels", series_index=2, subjects=["Fiction"]),
        rec("c", "A Boy's Own Story", "Edmund White", kind="pdf", last_opened="2026-09-01T10:00:00",
            date_added="2026-09-01T10:00:00", series="Russian Novels", series_index=1),
    ]


def ids(records) -> list[str]:
    return [r.book_id for r in records]


def test_titles_sort_without_articles_and_authors_by_last_name(books):
    assert ids(shelf.sort_records(books, "title")) == ["b", "c", "a"]  # Anna, Boy's, Trial
    assert ids(shelf.sort_records(books, "title", descending=True)) == ["a", "c", "b"]
    assert ids(shelf.sort_records(books, "author")) == ["a", "b", "c"]  # Kafka, Tolstoy, White
    assert shelf.author_key("Leo Tolstoy and Someone Else") == "tolstoy leo"


def test_missing_values_sort_last_both_ways(books):
    assert ids(shelf.sort_records(books, "pages")) == ["a", "b", "c"]
    assert ids(shelf.sort_records(books, "pages", descending=True)) == ["b", "a", "c"]
    assert ids(shelf.sort_records(books, "date_published")) == ["b", "a", "c"]
    assert ids(shelf.sort_records(books, "series")) == ["c", "b", "a"]  # by index inside the series


def test_dates_progress_time_and_then_by(books):
    assert ids(shelf.sort_records(books, "date_read", descending=True)) == ["b", "a", "c"]
    assert ids(shelf.sort_records(books, "date_added")) == ["a", "b", "c"]
    assert ids(shelf.sort_records(books, "progress", descending=True)) == ["a", "b", "c"]
    assert ids(shelf.sort_records(books, "time_remaining")) == ["a", "b", "c"]  # c's length is unknown
    # Then by: ties of the first key are ordered by the second.
    assert ids(shelf.sort_records(books, "format")) == ["a", "b", "c"]  # docx, epub, pdf
    books[0].source_kind = "epub"
    assert ids(shelf.sort_records(books, "format", then="title")) == ["b", "a", "c"]
    assert ids(shelf.sort_records(books, "format", then="title", then_descending=True)) == ["a", "b", "c"]
    assert ids(shelf.sort_records(books, "format", descending=True, then="title")) == ["c", "b", "a"]


def test_groups(books):
    named = lambda groups: [(label, ids(rs)) for label, rs in groups]  # noqa: E731
    assert named(shelf.group_records(books, "none")) == [("", ["a", "b", "c"])]
    assert named(shelf.group_records(books, "tags")) == [("classic", ["a", "b"]), ("russian", ["b"]), ("No Tags", ["c"])]
    assert named(shelf.group_records(books, "authors")) == [
        ("Franz Kafka", ["a"]), ("Leo Tolstoy", ["b"]), ("Edmund White", ["c"]),
    ]
    assert named(shelf.group_records(books, "collections")) == [("Summer", ["a"]), ("Not in a Collection", ["b", "c"])]
    assert named(shelf.group_records(books, "subjects"))[0] == ("Fiction", ["a", "b"])
    assert named(shelf.group_records(books, "series")) == [("Russian Novels", ["b", "c"]), ("Not in a Series", ["a"])]
    assert named(shelf.group_records(books, "status")) == [("Reading", ["b"]), ("Not Started", ["c"]), ("Finished", ["a"])]
    assert shelf.group_records([], "tags") == [] and shelf.group_records([], "none") == []


def test_search_time_left_and_odd_json(books):
    assert ids(r for r in books if shelf.matches(r, "tolstoy")) == ["b"]
    assert ids(r for r in books if shelf.matches(r, "CLASSIC trial")) == ["a"]
    assert ids(r for r in books if shelf.matches(r, "summer")) == ["a"]
    assert all(shelf.matches(r, "  ") for r in books)
    assert shelf.seconds_remaining(books[1]) == pytest.approx(1_500_000 / 15)
    assert shelf.seconds_remaining(books[2]) is None
    assert shelf.duration_text(7 * 3600 + 15 * 60) == "7h 15m" and shelf.duration_text(48 * 60) == "48m"
    assert shelf.duration_text(20) == "1m" and shelf.duration_text(0) == "under a minute"
    assert shelf.strings(["x", 3, " x ", "", None, "y"]) == ["x", "y"] and shelf.strings("nope") == []
    assert shelf.all_values(books, "tags") == ["classic", "russian"]
