"""TXT ingestion: encodings, Project Gutenberg wrappers, hard-wrapped and
one-line-per-paragraph text, headings, the title page, running heads from
scanned books, and original.html; slow tests read all_male_college.txt and a
Harvard Classics daily reading.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path

import pytest

from livrosound.book import TITLE_ROLE
from livrosound.ingest.txt import parse_txt
from tests.test_ingest_epub import EBOOKS, qt_check

WRAPPED = """THE RIVER

by Ana Lima


CHAPTER I

The river ran past the house all winter, and the children who lived there
had learned to read its moods the way other children read the faces of
their parents. In the spring it rose and grew brown and independ-
ent, and in the summer it shrank to a fair-
weather stream that a boy could cross.
    Their mother said nothing about it. She was a quiet woman who had
come from the hills, and the river was not her river.

CHAPTER II
The Flood

It came in April, after three days of rain, and it took the lower field
and the fence and the old boat before anyone was awake to see it go.

* * *

Nobody spoke of it again.
"""


def write(tmp_path: Path, text: str, name: str = "book.txt", encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def texts(book) -> list[str]:
    return [b.text for b in book.blocks]


def test_hard_wrapped_paragraphs_headings_and_title(tmp_path, qapp):
    book = parse_txt(write(tmp_path, WRAPPED), "t1")
    assert (book.title, book.author) == ("The River", "Ana Lima")
    blocks = book.blocks
    assert [b.kind for b in blocks] == [
        "heading", "paragraph", "heading", "paragraph", "paragraph", "heading", "heading", "paragraph", "separator", "paragraph",
    ]
    assert blocks[0].role == TITLE_ROLE and blocks[1].role == "frontmatter"
    first = blocks[3].text
    assert "brown and independent, and in the summer it shrank to a fair-weather stream" in first
    assert "\n" not in first and "  " not in first
    assert blocks[4].text.startswith("Their mother said nothing")  # an indent opens a paragraph
    assert [(e.title, e.level) for e in book.toc] == [("Chapter I", 1), ("Chapter II: The Flood", 1)]
    assert blocks[5].level == 1 and blocks[6].level == 2
    assert qt_check(book) == ([], [])
    assert "<pre>" in book.original_html and "independ-\nent" in book.original_html  # the text as it is


def test_one_paragraph_per_line_crlf_and_list_items(tmp_path):
    text = (
        "FRANKLIN'S ADVICE FOR THE NEW YEAR\r\nFranklin · Autobiography\r\nThe Harvard Classics, Vol. 1, pp. 79–85\r\n\r\n"
        + "thirteen names of virtues all that at that time occurr’d to me as necessary or desirable, and annexed to each a"
        " short precept, which fully express’d the extent I gave to its meaning, a long line that runs on and on.\r\n\r\n"
        + "1. TEMPERANCE.\r\n\r\nEat not to dullness; drink not to elevation.\r\n\r\n2. SILENCE.\r\n\r\n"
        + "Speak not but what may benefit others or yourself; avoid trifling conversation.\r\n"
    )
    book = parse_txt(write(tmp_path, text), "t2")
    assert book.title == "Franklin's Advice for the New Year"
    assert [b.role for b in book.blocks[:4]] == [TITLE_ROLE, "frontmatter", "frontmatter", ""]
    assert book.blocks[4].text == "1. TEMPERANCE." and book.blocks[4].kind == "paragraph"
    assert all("\r" not in t for t in texts(book))


@pytest.mark.parametrize(
    "encoding, sample",
    [("utf-8-sig", "Café “quoted” — dash"), ("cp1252", "Café “quoted” — dash"), ("latin-1", "Café déjà vu")],
)
def test_encodings(tmp_path, encoding, sample):
    text = f"A Title\n\n{sample} and more words in a sentence that goes on for a while.\n"
    book = parse_txt(write(tmp_path, text, encoding=encoding), "t3")
    assert sample in book.blocks[-1].text


def test_gutenberg_wrapper_is_left_out(tmp_path):
    text = (
        "The Project Gutenberg eBook of The River\n\nTitle: The River\n\nAuthor: Ana Lima\n\n"
        "*** START OF THE PROJECT GUTENBERG EBOOK THE RIVER ***\n\n\nCHAPTER I\n\nIt was a river.\n\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK THE RIVER ***\n\nSection 1. General Terms of Use.\n"
    )
    book = parse_txt(write(tmp_path, text), "t4")
    assert (book.title, book.author) == ("The River", "Ana Lima")
    assert texts(book) == ["CHAPTER I", "It was a river."]
    assert "Gutenberg" not in book.original_html


def test_running_heads_from_a_scan_are_cut_and_paragraphs_rejoined(tmp_path, qapp):
    filler = "the sentence goes on and on with many words in it " * 12
    parts = ["ARTFUL SENTENCES", "", "CHAPTER ONE", "", "Short Sentences", ""]
    for page in range(10, 18):
        head = f"{page} ARTFUL SENTENCES" if page % 2 == 0 else f"SHORT SENTENCES {page}"
        parts += [f"{filler.strip()} and then", "", head, "", f"{filler.strip()} it ended here.", ""]
    parts += ["See also 1984 George Orwell for more.", ""]
    book = parse_txt(write(tmp_path, "\n".join(parts)), "t5")
    body = "\n".join(texts(book))
    assert "ARTFUL SENTENCES" not in body.replace("ARTFUL SENTENCES\nCHAPTER ONE", "")
    assert not re.search(r"SHORT SENTENCES \d", body)
    assert body.count("and then the sentence goes on") == 8  # each paragraph split by a head is one again
    assert "CHAPTER ONE" in body and "Short Sentences" in body and "1984 George Orwell" in body
    assert "10 ARTFUL SENTENCES" not in book.original_html
    assert qt_check(book) == ([], [])


def test_midline_syllable_hyphens_are_joined_when_the_text_has_many(tmp_path, qapp):
    words = ["Rus-sians", "giv-ing", "fa-ther", "whis-pered", "obvi-ous", "be-cause"]
    lines = [f"Line {n} says {w} and keeps a well-known compound and a self-conscious one." for n, w in enumerate(words)]
    book = parse_txt(write(tmp_path, "A Title\n\n" + "\n".join(lines)), "t6")
    body = " ".join(texts(book))
    assert all(w.replace("-", "") in body for w in words)
    assert "well-known" in body and "self-conscious" in body
    assert qt_check(book) == ([], [])
    few = parse_txt(write(tmp_path, "A Title\n\nOnly one fa-ther here, and a to-day.\n", "few.txt"), "t7")
    assert "fa-ther" in few.blocks[-1].text  # one artefact is not evidence enough


def test_contents_lines_link_to_their_chapters(tmp_path):
    text = (
        "THE RIVER\n\nContents\n\nChapter 1 ........ 3\nChapter 2 ........ 9\n\n"
        "Chapter 1\n\nIt was a river that ran past the house all winter long and into the spring.\n\n"
        "Chapter 2\n\nIt flooded in April and took the lower field with it, and the fence too.\n"
    )
    book = parse_txt(write(tmp_path, text), "t8")
    lines = [b for b in book.blocks if b.role == "contents" and b.kind != "heading"]
    assert [(b.text, book.blocks[b.link].text) for b in lines] == [
        ("Chapter 1 ........ 3", "Chapter 1"),
        ("Chapter 2 ........ 9", "Chapter 2"),
    ]


def test_not_a_text_file(tmp_path):
    from livrosound.ingest import BookImportError

    path = tmp_path / "binary.txt"
    path.write_bytes(b"\x00\x01\x02" * 100)
    with pytest.raises(BookImportError, match="text file"):
        parse_txt(path, "t9")


# ---------------------------------------------------------------------------
# Real files (slow)


def _real(name: str) -> Path:
    path = EBOOKS / name
    if not path.is_file():
        pytest.skip(f"sample missing: {name}")
    return path


@pytest.mark.slow
def test_real_all_male_college(qapp):
    t = time.perf_counter()
    book = parse_txt(_real("all_male_college.txt"), "amc")
    elapsed = time.perf_counter() - t
    print(f"\nall_male_college.txt: {len(book.blocks)} blocks, {len(book.toc)} TOC entries, {elapsed:.2f} s")
    assert book.title == "All-Male College, 1957"
    assert book.blocks[0].role == TITLE_ROLE and book.blocks[0].text.startswith("ALL-MALE COLLEGE")
    assert [e.title for e in book.toc][:2] == ["Chapter One", "Chapter Two: My First Glimpse of Dusty and the Gang Shower"]
    assert len(book.toc) == 7
    body = " ".join(b.text for b in book.blocks)
    assert "Russians" in body and "giving" in body and "Rus-sians" not in body
    assert all(b.text.strip() for b in book.blocks)
    assert Counter(b.role for b in book.blocks)[""] == len(book.blocks) - 1
    assert qt_check(book) == ([], [])


@pytest.mark.slow
def test_real_harvard_daily(qapp):
    book = parse_txt(_real("Harvard Classics Daily/Day 001 - Franklin's Advice for the New Year.txt"), "hc")
    print(f"\nHarvard Day 001: {len(book.blocks)} blocks, {len(book.toc)} TOC entries")
    assert book.title == "Franklin's Advice for the New Year"
    assert [b.role for b in book.blocks[:4]] == [TITLE_ROLE, "frontmatter", "frontmatter", ""]
    assert book.blocks[3].text.startswith("thirteen names of virtues")
    assert all(b.kind != "heading" for b in book.blocks[1:])  # "1. TEMPERANCE." is a list item
    assert qt_check(book) == ([], [])


def test_a_short_file_of_headings_keeps_wrapped_sentences_whole(tmp_path):
    text = (
        "A SHORT BOOK\n\nONE\n\nthe first part\nof the story was told\nby the river and the\n"
        "wind until morning came.\n\nTWO\n\nIt ended there.\n\nTHREE\n\nNothing else.\n"
    )
    book = parse_txt(write(tmp_path, text), "t10")
    assert "the first part of the story was told by the river and the wind until morning came." in texts(book)


def test_a_numeral_repeating_the_chapter_number_is_dropped(tmp_path, qapp):
    text = (
        "THE RIVER\n\nCHAPTER 3\n\n3\n\nIt rained all day, and the river rose over the lower field.\n\n"
        "CHAPTER 4\n\n4\n\nThe water went down again in the night and nobody spoke of it.\n"
    )
    book = parse_txt(write(tmp_path, text), "t11")
    assert texts(book) == [
        "THE RIVER", "CHAPTER 3", "It rained all day, and the river rose over the lower field.",
        "CHAPTER 4", "The water went down again in the night and nobody spoke of it.",
    ]
    assert qt_check(book) == ([], [])
