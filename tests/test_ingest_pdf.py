"""PDF ingestion of books with digital text: synthetic books built with PyMuPDF (fast),
real digital PDFs from the sample library and the refusal of scans (slow)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pytest

from livrosound.book import SKIPPED_ROLES, TITLE_ROLE, Book
from livrosound.ingest import BookImportError, ImportCancelled
from livrosound.ingest import pdf as pdf_module
from livrosound.ingest.pdf import parse_pdf

EBOOKS = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Ebooks"
ARTFUL = EBOOKS / "Artful_Sentences.pdf"
SECRET_HISTORY = EBOOKS / "The Secret History by Donna Tartt (z-lib.org).epub.pdf"
HILL_HOUSE = EBOOKS / "The Haunting of Hill House by Shirley Jackson.pdf"
ELEMENTS = EBOOKS / "THE ELEMENTS OF STYLE UPDATED 2011 EDITION William.pdf"

W, H = 432.0, 648.0  # a 6 x 9 in page
LEFT, RIGHT = 54.0, 378.0
TOP, BOTTOM = 72.0, 590.0
SIZE, LEADING = 11.0, 14.0
EM = SIZE

FILLER = (
    "The quiet street held its breath while the evening settled over the houses and gardens, "
    "and every window showed a lamp that someone had lit against the coming dark. People walked "
    "home slowly, talking about the weather and the price of bread, and nobody noticed the stranger "
    "who stood by the fountain reading a letter he had carried for many years without opening it. "
)


@dataclass
class Drawn:
    page: int
    text: str
    rect: pymupdf.Rect


class Printer:
    """Sets a small book in type: justified paragraphs placed word by word, running heads,
    and a record of every word drawn (to check the word boxes against)."""

    def __init__(self, title_head: str = "THE TEST BOOK", chapter_head: str = "A CHAPTER TITLE"):
        self.doc = pymupdf.open()
        self.drawn: list[Drawn] = []
        self.page: pymupdf.Page | None = None
        self.y = TOP
        self.title_head = title_head
        self.chapter_head = chapter_head
        self.heads = True
        self.folio_offset = 0  # printed page number = page index + 1 - offset

    # --- pages ---

    def new_page(self, heads: bool | None = None) -> pymupdf.Page:
        self.page = self.doc.new_page(width=W, height=H)
        self.y = TOP
        if heads if heads is not None else self.heads:
            self.running_head()
        return self.page

    @property
    def index(self) -> int:
        return self.doc.page_count - 1

    def running_head(self) -> None:
        number = self.index + 1 - self.folio_offset
        if number % 2 == 0:
            self.words_at(f"{number} {self.title_head}".split(), LEFT, 40, 8, gap=8, record=False)
        else:
            words = f"{self.chapter_head} {number}".split()
            width = sum(pymupdf.get_text_length(w, "tiro", 8) for w in words) + 8 * (len(words) - 1)
            self.words_at(words, RIGHT - width, 40, 8, gap=8, record=False)

    def ensure(self, height: float) -> None:
        if self.page is None or self.y + height > BOTTOM:
            self.new_page()

    # --- drawing ---

    def words_at(self, words, x, baseline, size, *, font="tiro", gap=None, record=True):
        """Draw words from x at a baseline, separated by gap (default: a space)."""
        space = pymupdf.get_text_length(" ", font, size) if gap is None else gap
        for word in words:
            self.page.insert_text((x, baseline), word, fontname=font, fontsize=size)
            width = pymupdf.get_text_length(word, font, size)
            if record:
                self.drawn.append(Drawn(self.index, word, pymupdf.Rect(x, baseline - size, x + width, baseline + 0.3 * size)))
            x += width + space

    def line(self, text, *, x=LEFT, size=SIZE, font="tiro", right=None, advance=None):
        """One line of text (justified to `right` when given)."""
        self.ensure(size * 1.3)
        words = text.split()
        baseline = self.y + size
        if right is not None and len(words) > 1:
            widths = sum(pymupdf.get_text_length(w, font, size) for w in words)
            self.words_at(words, x, baseline, size, font=font, gap=(right - x - widths) / (len(words) - 1))
        else:
            self.words_at(words, x, baseline, size, font=font)
        self.y += advance if advance is not None else size * LEADING / SIZE

    def paragraph(self, text, *, indent=1.5 * EM, left=LEFT, right=RIGHT, size=SIZE, font="tiro", gap_after=0.0):
        """A justified paragraph, its first line indented, continuing on new pages."""
        words = text.split()
        space = pymupdf.get_text_length(" ", font, size)
        lines, current, width, first = [], [], 0.0, True
        for word in words:
            w = pymupdf.get_text_length(word, font, size)
            avail = right - left - (indent if first else 0)
            if current and width + space + w > avail:
                lines.append((current, first))
                current, width, first = [], 0.0, False
            width = width + space + w if current else w
            current.append(word)
        lines.append((current, first))
        for k, (ws, is_first) in enumerate(lines):
            x = left + (indent if is_first else 0)
            last = k == len(lines) - 1
            self.line(" ".join(ws), x=x, size=size, font=font, right=None if last else right, advance=size * LEADING / SIZE)
        self.y += gap_after

    def space(self, points: float) -> None:
        self.y += points

    def save(self, folder: Path, name: str = "book.pdf") -> Path:
        path = folder / name
        self.doc.save(path)
        return path


def parse(path: Path) -> Book:
    return parse_pdf(path, "test-book", lambda *a: None, lambda: False)


def readable(book: Book):
    return [b for b in book.blocks if not b.skipped]


def block_with(book: Book, needle: str):
    found = [b for b in book.blocks if needle in b.text]
    assert found, f"no block contains {needle!r}"
    return found[0]


def body_page(p: Printer, paragraphs: int = 3) -> None:
    for _ in range(paragraphs):
        p.paragraph(FILLER, gap_after=0)


# ---------------------------------------------------------------------------
# Running heads, page numbers, paragraphs across pages


def test_running_heads_and_page_numbers_never_reach_the_text(tmp_path):
    p = Printer()
    p.new_page()
    body_page(p, 2)
    p.new_page()
    body_page(p, 2)
    p.new_page(heads=False)
    body_page(p, 2)
    p.words_at(["3"], W / 2 - 3, 620, 8, record=False)  # a lone folio at the foot
    p.new_page()
    body_page(p, 2)
    book = parse(p.save(tmp_path))
    text = "\n".join(b.text for b in book.blocks)
    assert "TEST BOOK" not in text and "CHAPTER TITLE" not in text
    assert not re.search(r"(?<!\S)[1-4](?!\S)", text), "a page number was read as text"
    for b in book.blocks:
        assert all(w[2] > 50 for w in b.words), "a running head's box is among the words"


def test_paragraph_across_a_page_break_skips_the_running_head(tmp_path):
    p = Printer()
    p.new_page()
    body_page(p, 3)
    long_text = (FILLER * 3) + "Independently of everything else, the end came quietly."
    p.y = BOTTOM - 6 * LEADING
    p.paragraph(long_text)  # runs over onto page 2, under its running head "2 THE TEST BOOK"
    assert p.index == 1
    p.paragraph("A new paragraph begins after the long one, as paragraphs do.")
    book = parse(p.save(tmp_path))
    merged = block_with(book, "the end came quietly")
    assert {w[0] for w in merged.words} == {0, 1}
    assert merged.page == 0
    assert "TEST BOOK" not in merged.text and " 2 " not in merged.text
    head_band = [w for w in merged.words if w[0] == 1 and w[2] < 50]
    assert not head_band
    assert merged.text.startswith("The quiet street")
    assert block_with(book, "A new paragraph begins").text.startswith("A new paragraph")


def test_chapter_opening_is_a_heading_and_later_heads_are_furniture(tmp_path):
    p = Printer(chapter_head="SHORT SENTENCES")
    p.new_page(heads=False)
    # An ornament the old OCR layer reads as "Yee)", set apart from the small-caps line.
    p.words_at(["Yee)"], LEFT, 80, 9, record=False)
    p.words_at(["CHAPTER", "ONE"], LEFT + 40, 80, 9)
    p.y = 130
    p.words_at(["Short", "Sentences"], 200, 150, 20)
    p.y = 200
    body_page(p, 2)
    p.new_page()  # "2 THE TEST BOOK"
    body_page(p, 3)
    p.new_page()  # "SHORT SENTENCES 3"
    body_page(p, 3)
    book = parse(p.save(tmp_path))
    first, second = book.blocks[0], book.blocks[1]
    assert (first.kind, first.text, first.level) == ("heading", "CHAPTER ONE", 1)
    assert (second.kind, second.text, second.level) == ("heading", "Short Sentences", 1)
    assert not any("Yee" in b.text for b in book.blocks)
    assert book.toc[0].title == "Chapter One: Short Sentences" and book.toc[0].block == 0
    text = "\n".join(b.text for b in book.blocks[2:])
    assert "SHORT SENTENCES" not in text and "TEST BOOK" not in text


def test_hyphenated_line_ends_join(tmp_path):
    p = Printer()
    p.new_page()
    body_page(p, 1)
    p.line("A simple sentence consists of an independent clause, independ-", x=LEFT + 1.5 * EM, right=RIGHT)
    p.line("ent in that it makes sense without being attached to anything else", right=RIGHT)
    p.line("at all. Time flies.")
    body_page(p, 1)
    book = parse(p.save(tmp_path))
    block = block_with(book, "independent in that")
    assert "independ-" not in block.text
    k = block.text.index("independent in")
    word = next(w for w in block.words if w[5] <= k < w[6])
    assert block.text[word[5] : word[6]] == "independ"  # the first half's box covers its letters


def test_paragraph_indents_split_paragraphs(tmp_path):
    p = Printer()
    p.new_page()
    p.paragraph("First paragraph. " + FILLER)
    p.paragraph("Second paragraph. " + FILLER)
    p.paragraph("Third paragraph. " + FILLER)
    book = parse(p.save(tmp_path))
    starts = [b.text[:17] for b in book.blocks if b.kind == "paragraph"]
    assert starts == ["First paragraph. ", "Second paragraph.", "Third paragraph. "]


# ---------------------------------------------------------------------------
# Quotes, drop caps, notes


def test_epigraph_is_a_quote_with_a_right_aligned_attribution(tmp_path):
    p = Printer()
    p.new_page(heads=False)
    p.line("CHAPTER ONE", x=LEFT, size=9)
    p.space(30)
    for text in (
        "And the words slide into the slots ordained by syntax,",
        "and glitter as with atmospheric dust with those",
        "impurities which we call meaning.",
    ):
        p.line(text, x=LEFT + 4 * EM, size=10)
    p.line("Anthony Burgess, Enderby, 406", x=LEFT + 7 * EM, size=10)
    p.space(20)
    body_page(p, 3)
    book = parse(p.save(tmp_path))
    quote = block_with(book, "slots ordained")
    assert quote.kind == "quote"
    assert quote.text == (
        "And the words slide into the slots ordained by syntax, and glitter as with atmospheric "
        "dust with those impurities which we call meaning."
    )
    attribution = book.blocks[book.blocks.index(quote) + 1]
    assert (attribution.kind, attribution.align, attribution.text) == ("quote", "right", "Anthony Burgess, Enderby, 406")
    assert book.blocks[book.blocks.index(quote) + 2].text.startswith("The quiet street")


def test_drop_cap_joins_its_word_and_keeps_its_box(tmp_path):
    p = Printer()
    p.new_page(heads=False)
    body_page(p, 1)
    p.space(10)
    top = p.y
    p.page.insert_text((LEFT, top + 2 * LEADING + 2), "A", fontname="tiro", fontsize=3 * SIZE)  # a drop capital
    cap_x1 = LEFT + pymupdf.get_text_length("A", "tiro", 3 * SIZE) + 4
    p.line("NTHONY BURGESS is right: it is the words that shine and sparkle", x=cap_x1, right=RIGHT)
    p.line("and glitter, sometimes radiant with an author's inspired choice.", x=cap_x1, right=RIGHT)
    p.line("But it is syntax that gives words the power to relate to each other in a", right=RIGHT)
    p.line("sequence, to create rhythms and emphasis, to carry meaning of whatever kind.")
    body_page(p, 1)
    book = parse(p.save(tmp_path))
    block = block_with(book, "NTHONY")
    assert block.text.startswith("ANTHONY BURGESS is right: it is the words")
    assert "whatever kind." in block.text  # the lines beside the capital are not new paragraphs
    cap = block.words[0]
    assert (cap[5], cap[6]) == (0, 1) and cap[4] - cap[2] > 2 * SIZE  # the capital's own, tall box
    assert block.words[1][5] == 1  # "NTHONY" follows with no space


def test_footnote_waits_for_the_paragraph_it_interrupts(tmp_path):
    p = Printer()
    p.new_page()
    body_page(p, 1)
    p.y = BOTTOM - 8 * LEADING
    p.paragraph(FILLER * 3 + "The letter, at last, was opened.")  # runs over onto page 2
    p.page = p.doc[0]
    p.words_at("1 Syntactic Structures (The Hague: Mouton, 1957), 80.".split(), LEFT, 600, 8.5)
    p.page = p.doc[1]
    book = parse(p.save(tmp_path))
    note = block_with(book, "Syntactic Structures")
    assert note.kind == "note" and note.text.startswith("1 Syntactic")
    merged = block_with(book, "was opened")
    assert book.blocks.index(note) == book.blocks.index(merged) + 1
    assert "Syntactic" not in merged.text


# ---------------------------------------------------------------------------
# Word boxes


def test_word_boxes_cover_exactly_the_words_drawn(tmp_path):
    p = Printer()
    p.new_page()
    body_page(p, 2)
    p.line("A simple sentence consists of an independent clause, independ-", x=LEFT + 1.5 * EM, right=RIGHT)
    p.line("ent in that it makes sense without being attached to anything.")
    p.space(20)
    p.line("And the words slide into the slots ordained by syntax.", x=LEFT + 4 * EM, size=10)
    p.line("Anthony Burgess, Enderby, 406", x=LEFT + 7 * EM, size=10)
    p.new_page()
    body_page(p, 3)
    book = parse(p.save(tmp_path))
    drawn = list(p.drawn)
    for b in book.blocks:
        last = 0
        for page, x0, y0, x1, y1, start, end in b.words:
            assert last <= start < end <= len(b.text)
            last = end
            piece = b.text[start:end]
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            source = next(d for d in drawn if d.page == page and d.rect.contains(pymupdf.Point(cx, cy)))
            assert piece in (source.text, source.text.rstrip("-")), (piece, source.text)
        covered = {k for w in b.words for k in range(w[5], w[6])}
        assert all(k in covered for k, c in enumerate(b.text) if not c.isspace())
    assert sum(len(b.words) for b in book.blocks) == len(drawn)


def test_outline_maps_to_the_headings_it_names(tmp_path):
    p = Printer()
    p.heads = False
    for title in ("The First Chapter", "The Second Chapter"):
        p.new_page()
        p.line(title, x=150, size=18, advance=40)
        body_page(p, 3)
        p.new_page()
        body_page(p, 3)
    p.doc.set_toc([[1, "The First Chapter", 1], [1, "The Second Chapter", 3]])
    book = parse(p.save(tmp_path))
    assert [(e.title, book.blocks[e.block].text) for e in book.toc] == [
        ("The First Chapter", "The First Chapter"),
        ("The Second Chapter", "The Second Chapter"),
    ]


def test_headings_make_the_toc_without_an_outline(tmp_path):
    p = Printer()
    p.heads = False
    for number, title in (("ONE", "The Beginning"), ("TWO", "The Middle")):
        p.new_page()
        p.line(f"CHAPTER {number}", x=LEFT, size=9, advance=40)
        p.line(title, x=200, size=20, advance=50)
        body_page(p, 3)
    book = parse(p.save(tmp_path))
    assert [e.title for e in book.toc] == ["Chapter One: The Beginning", "Chapter Two: The Middle"]
    assert [book.blocks[e.block].text for e in book.toc] == ["CHAPTER ONE", "CHAPTER TWO"]


# ---------------------------------------------------------------------------
# Front and back matter


def _book_with_matter(tmp_path) -> tuple[Path, Printer]:
    p = Printer(title_head="ARTFUL SENTENCES", chapter_head="THE BEGINNING")
    p.heads = False
    p.folio_offset = 6  # page index 6 is printed page 1
    # 0 cover: title and author over artwork
    p.new_page()
    p.line("Virginia Tufte", x=LEFT, size=24, advance=80)
    p.line("ARTFUL SENTENCES:", x=LEFT, size=26, advance=40)
    p.line("Syntax as Style", x=200, size=22)
    # 1 blurb
    p.new_page()
    p.paragraph(
        "In Artful Sentences: Syntax as Style, Virginia Tufte presents and comments on more than a "
        "thousand excellent sentences chosen from the works of authors in the twentieth and twenty-first "
        "centuries. The sentences come from an extensive search to identify some of the ways professional "
        "writers use the generous resources of the English language.",
        indent=0,
    )
    # 2 title page
    p.new_page()
    p.y = 200
    p.line("ARTFUL SENTENCES:", x=LEFT, size=24, advance=36)
    p.line("Syntax as Style", x=200, size=22, advance=60)
    p.line("Virginia Tufte", x=170, size=18)
    # 3 a library's stamp page
    p.new_page()
    p.y = 300
    p.line("This content downloaded from 128.59.222.107 on Mon, 01 Jan 2024", x=60)
    p.line("All use subject to https://about.jstor.org/terms", x=90)
    # 4 copyright page
    p.new_page()
    p.y = 380
    p.line("Copyright © 2006 by Virginia Tufte", x=120, size=9)
    p.line("Published by Graphics Press LLC", x=125, size=9)
    p.line("All rights reserved. Printed in the United States of America.", x=80, size=9)
    # 5 contents
    p.new_page()
    p.line("CONTENTS", x=LEFT, size=12, advance=40)
    for line in ("1  The Beginning ........ 1", "2  Middle Matters ........ 3", "Afterword ........ 5", "Index ........ 6"):
        p.line(line, x=LEFT + 20)
    # 6.. chapter one (printed pages 1, 2)
    p.heads = True
    p.new_page(heads=False)
    p.line("CHAPTER ONE", x=LEFT, size=9, advance=40)
    p.line("The Beginning", x=200, size=20, advance=50)
    body_page(p, 3)
    p.new_page()
    body_page(p, 3)
    # 8.. chapter two (printed 3, 4), its title worded differently from the contents line
    p.chapter_head = "MIDDLE MATTERS"
    p.new_page(heads=False)
    p.line("CHAPTER TWO", x=LEFT, size=9, advance=40)
    p.line("Matters in the Middle", x=180, size=20, advance=50)
    body_page(p, 3)
    p.new_page()
    body_page(p, 3)
    # 10 printed 5: an afterword whose heading the contents names differently
    p.chapter_head = "LAST WORDS"
    p.new_page(heads=False)
    p.line("Last Words", x=190, size=20, advance=50)
    body_page(p, 3)
    # 11 printed 6: the index
    p.chapter_head = "INDEX"
    p.new_page(heads=False)
    p.line("INDEX", x=190, size=16, advance=40)
    for entry in ("Appositive, 12-14", "Basic sentences, 9-34", "Clauses, 21", "Dashes, 45-46", "Economy, 230", "Fragments, 19"):
        p.line(entry)
    # 12 back cover
    p.new_page(heads=False)
    p.paragraph(
        "Virginia Tufte is distinguished emerita professor of English at the University of Southern "
        "California. Her special fields are English Renaissance poetry, Milton, and the history and grammar "
        "of English. She has lived most of her life in southern California.",
        indent=0,
    )
    p.line("ISBN 0-9613921-8-5", x=250, size=9)
    p.doc.set_metadata({"title": "Artful sentences : syntax as style", "author": "Tufte, Virginia"})
    return p.save(tmp_path), p


def test_front_and_back_matter_roles(tmp_path):
    path, _ = _book_with_matter(tmp_path)
    book = parse(path)
    assert (book.title, book.author) == ("Artful Sentences: Syntax as Style", "Virginia Tufte")
    roles = {}
    for b in book.blocks:
        roles.setdefault(b.page, set()).add(b.role)
    assert roles[0] == {"cover"}
    assert roles[1] == {"blurb"}
    assert roles[2] == {TITLE_ROLE, "frontmatter"}
    assert roles[3] == {"boilerplate"}
    assert roles[4] == {"copyright"}
    assert roles[5] == {"contents"}
    assert roles[6] == roles[7] == roles[8] == roles[10] == {""}
    assert roles[11] == {"index"}
    assert roles[12] == {"blurb"}
    # Title and subtitle are one block, announced once, with the boxes of both lines.
    titles = [b for b in book.blocks if b.role == TITLE_ROLE]
    assert [b.text for b in titles] == ["ARTFUL SENTENCES: Syntax as Style"] and titles[0].page == 2
    assert len({round(w[2]) for w in titles[0].words}) == 2
    assert block_with(book, "Virginia Tufte").role in ("cover", "frontmatter")
    assert all(b.role in SKIPPED_ROLES for b in book.blocks if b.page < 6 and b.role != TITLE_ROLE)


def test_contents_lines_link_to_their_chapters(tmp_path):
    path, _ = _book_with_matter(tmp_path)
    book = parse(path)
    lines = [b for b in book.blocks if b.role == "contents" and b.kind != "heading"]
    assert [b.text for b in lines] == [
        "1 The Beginning ........ 1",
        "2 Middle Matters ........ 3",
        "Afterword ........ 5",
        "Index ........ 6",
    ]
    targets = [book.blocks[b.link].text for b in lines]
    # by title; by printed page number (via the running heads' page numbers); by title again
    assert targets == ["CHAPTER ONE", "CHAPTER TWO", "Last Words", "INDEX"]
    assert [e.title for e in book.toc] == [
        "Chapter One: The Beginning",
        "Middle Matters",
        "Afterword",
        "Index",
    ]
    assert [book.blocks[e.block].text for e in book.toc] == targets


def _centred(p: Printer, text: str, size: float = SIZE, font: str = "tiro") -> None:
    width = pymupdf.get_text_length(text, font, size)
    p.line(text, x=(LEFT + RIGHT - width) / 2, size=size, font=font)


def _converted_ebook(tmp_path, chapters_per_book: int = 3) -> Path:
    """A typeset novel as calibre makes them: title, author, dedication and epigraph on
    one page; a coarse outline (Prologue, Book 1, Book 2, About the Author) over many
    "Chapter N" headings."""
    p = Printer()
    p.heads = False
    p.new_page()
    p.y = 90
    _centred(p, "Donna Tartt", 20)
    p.space(6)
    _centred(p, "The Secret History", 20)
    p.space(10)
    _centred(p, "For Bret Easton Ellis, whose generosity will never cease to warm my heart; and for")
    _centred(p, "Paul Edward Mc Gloin, muse and Maecenas, who is the dearest friend I will ever")
    _centred(p, "have in this world.")
    p.space(14)
    _centred(p, "Come then, and let us pass a leisure hour in storytelling, and our story shall be the")
    _centred(p, "education of our heroes.")
    _centred(p, "– PLATO, Republic, book ii")
    outline = []
    p.new_page()
    outline.append([1, "Prologue", p.index + 1])
    _centred(p, "Prologue", 20)
    p.space(10)
    body_page(p, 3)
    number = 0
    for book in (1, 2):
        p.new_page()
        outline.append([1, f"Book {book}", p.index + 1])
        _centred(p, f"Book {book}", 20)
        for _ in range(chapters_per_book):
            number += 1
            p.new_page()
            _centred(p, f"Chapter {number}", 17)
            p.space(10)
            p.paragraph(FILLER)
            p.paragraph("'I think that's a good idea,' said Henry, going back to his")
            p.line("book.")
            body_page(p, 2)
    p.new_page()
    outline.append([1, "About the Author", p.index + 1])
    _centred(p, "About the Author", 17)
    p.space(10)
    p.paragraph("Donna Tartt was educated at the University of Mississippi and Bennington College, and is the "
                "author of a second novel. She lives in Virginia.")
    p.doc.set_toc(outline)
    p.doc.set_metadata({"title": "The Secret History", "author": "Donna Tartt"})
    return p.save(tmp_path)


def test_title_lines_on_a_converted_ebooks_first_page(tmp_path):
    book = parse(_converted_ebook(tmp_path))
    author, title = book.blocks[0], book.blocks[1]
    assert (author.text, author.role) == ("Donna Tartt", "frontmatter")
    assert (title.text, title.role) == ("The Secret History", TITLE_ROLE)
    assert [b for b in book.blocks if b.role == TITLE_ROLE] == [title]


def test_unfinished_lines_stay_in_one_block(tmp_path):
    book = parse(_converted_ebook(tmp_path))
    dedication = block_with(book, "Bret Easton Ellis")
    assert dedication.text.endswith("the dearest friend I will ever have in this world.")
    assert not dedication.skipped
    epigraph = block_with(book, "leisure hour")
    assert epigraph.text.endswith("our story shall be the education of our heroes.")
    plato = block_with(book, "PLATO")
    assert plato is not epigraph and plato.text.endswith("PLATO, Republic, book ii")
    # A paragraph's last word alone on a line ("book.") is not a heading.
    assert not any(b.kind == "heading" and b.text == "book." for b in book.blocks)
    assert block_with(book, "going back to his").text.endswith("going back to his book.")


def test_a_coarse_outline_gains_the_chapter_headings(tmp_path):
    book = parse(_converted_ebook(tmp_path))
    assert [(e.level, e.title) for e in book.toc] == [
        (1, "Prologue"),
        (1, "Book 1"),
        (2, "Chapter 1"),
        (2, "Chapter 2"),
        (2, "Chapter 3"),
        (1, "Book 2"),
        (2, "Chapter 4"),
        (2, "Chapter 5"),
        (2, "Chapter 6"),
        (1, "About the Author"),
    ]
    assert all(book.blocks[e.block].kind == "heading" for e in book.toc)
    assert [book.blocks[e.block].text for e in book.toc][2] == "Chapter 1"


def test_about_the_author_at_the_back_is_not_read(tmp_path):
    book = parse(_converted_ebook(tmp_path))
    bio = block_with(book, "educated at the University of Mississippi")
    assert bio.role == "blurb" and block_with(book, "About the Author").role == "blurb"


def test_a_section_title_after_an_attribution_starts_a_new_block(tmp_path):
    p = Printer()
    p.new_page(heads=False)
    body_page(p, 2)
    p.y = BOTTOM - 3 * LEADING
    p.line("The clouds were sitting on the land.", x=LEFT + 3 * EM, size=10)
    p.line("William Golding, Lord of the Flies, 70", x=LEFT + 5 * EM, size=10)
    p.new_page(heads=False)
    p.line("Type four: transitives")
    p.paragraph(FILLER, indent=0)
    body_page(p, 2)
    book = parse(p.save(tmp_path))
    attribution = block_with(book, "Lord of the Flies")
    assert attribution.text == "William Golding, Lord of the Flies, 70"
    heading = book.blocks[book.blocks.index(attribution) + 1]
    assert (heading.kind, heading.text) == ("heading", "Type four: transitives")
    assert book.blocks[book.blocks.index(heading) + 1].text.startswith("The quiet street")


# ---------------------------------------------------------------------------
# Unreadable glyphs, progress and cancel


def test_control_characters_are_no_text():
    tok = pdf_module._Tok("\x01\x01\x01", 0, 0, 0, 10, 10, 10)
    assert pdf_module._clean_tokens([tok]) == []
    tok = pdf_module._Tok("wor�d", 0, 0, 0, 10, 10, 10)
    assert [t.text for t in pdf_module._clean_tokens([tok])] == ["word"]


def test_progress_and_cancel(tmp_path):
    p = Printer()
    for _ in range(3):
        p.new_page()
        body_page(p, 1)
    path = p.save(tmp_path)
    seen = []
    parse_pdf(path, "b", lambda done, total, message: seen.append((done, total, message)), lambda: False)
    assert seen[:3] == [(0, 3, "Reading page 1 of 3"), (1, 3, "Reading page 2 of 3"), (2, 3, "Reading page 3 of 3")]
    assert seen[-1][0] == 3
    calls = []
    with pytest.raises(ImportCancelled):
        parse_pdf(path, "b", lambda *a: calls.append(a), lambda: len(calls) >= 2)


def test_metadata_title_and_author_are_cleaned():
    assert pdf_module._clean_title("Artful sentences : syntax as style") == "Artful Sentences: Syntax as Style"
    assert pdf_module._clean_title("Microsoft Word - draft3.docx") == ""
    assert pdf_module._clean_title("The Overcoat and Other Stories") == "The Overcoat and Other Stories"
    assert pdf_module._clean_author("Tufte, Virginia") == "Virginia Tufte"
    assert pdf_module._clean_author("Gogol, Nikolai, 1809-1852") == "Nikolai Gogol"
    assert pdf_module._clean_author("Nikolay Gogol") == "Nikolay Gogol"


def test_italic_and_bold_runs(tmp_path):
    p = Printer()
    p.new_page()
    body_page(p, 1)
    y = p.y + SIZE
    p.words_at(["She", "read"], LEFT, y, SIZE)
    p.words_at(["Grammar", "as", "Style"], LEFT + 50, y, SIZE, font="tiit")
    p.words_at(["twice,", "with", "care."], LEFT + 160, y, SIZE, font="tibo")
    p.y += LEADING
    body_page(p, 1)
    book = parse(p.save(tmp_path))
    block = block_with(book, "Grammar as Style")
    runs = {block.text[s:e]: style for s, e, style in block.styles}
    assert runs.get("Grammar as Style") == "i"
    assert runs.get("twice, with care.") == "b"


def test_two_column_index_reads_down_each_column(tmp_path):
    p = Printer()
    p.heads = False
    p.new_page()
    body_page(p, 3)
    p.new_page()
    p.line("INDEX", x=190, size=16, advance=40)
    left = [f"Alpha {k}, {k * 3}" for k in range(12)]
    right = [f"Omega {k}, {k * 5}" for k in range(12)]
    for a, b in zip(left, right):
        p.words_at(a.split(), LEFT, p.y + SIZE, SIZE)
        p.words_at(b.split(), 230, p.y + SIZE, SIZE)
        p.y += LEADING
    book = parse(p.save(tmp_path))
    entries = [b.text for b in book.blocks if b.page == 1 and b.kind != "heading"]
    assert entries == left + right


# ---------------------------------------------------------------------------
# Scans are refused; real digital books (slow)


def _image_page(doc: pymupdf.Document, text: str = "", render_mode: int = 0) -> None:
    page = doc.new_page(width=W, height=H)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 450), 0)
    pix.clear_with(235)
    page.insert_image(page.rect, pixmap=pix)
    if text:
        y = TOP
        for _ in range(20):
            page.insert_text((LEFT, y), text, fontname="tiro", fontsize=SIZE, render_mode=render_mode)
            y += LEADING


def test_an_image_only_pdf_is_refused(tmp_path):
    doc = pymupdf.open()
    for _ in range(4):
        _image_page(doc)
    doc.set_metadata({"title": "Old Book"})
    doc.save(tmp_path / "scan.pdf")
    with pytest.raises(BookImportError, match="“Old Book” is a scanned book"):
        parse(tmp_path / "scan.pdf")


def test_a_scan_under_an_invisible_ocr_layer_is_refused(tmp_path):
    doc = pymupdf.open()
    for _ in range(4):
        _image_page(doc, "The quiet street held its breath while the evening settled.", render_mode=3)
    doc.save(tmp_path / "ocr-layer.pdf")
    with pytest.raises(BookImportError, match="“ocr-layer” is a scanned book"):
        parse(tmp_path / "ocr-layer.pdf")


def test_digital_text_over_a_page_picture_is_read(tmp_path):
    doc = pymupdf.open()
    for _ in range(4):
        _image_page(doc, "The quiet street held its breath while the evening settled.")
    doc.save(tmp_path / "background.pdf")
    assert block_with(parse(tmp_path / "background.pdf"), "The quiet street")


def test_letter_spaced_capitals_are_rejoined(tmp_path):
    p = Printer()
    p.heads = False

    def tracked(text, x, y, size):
        for word in text.split():
            for c in word:
                p.page.insert_text((x, y), c, fontname="tiro", fontsize=size)
                x += pymupdf.get_text_length(c, "tiro", size) + 0.3 * size
            x += 0.9 * size

    for k in range(3):
        p.new_page()
        tracked(f"ARTFUL SENTENCES {k + 2}", LEFT, 40, 8)  # a tracked running head
        p.y = TOP
        if k == 0:
            tracked("CHAPTER ONE", LEFT, 90, 9)
            p.y = 110
            p.line("Short Sentences", x=200, size=20, advance=50)
        body_page(p, 3)
    book = parse(p.save(tmp_path))
    assert [b.text for b in book.blocks[:2]] == ["CHAPTER ONE", "Short Sentences"]
    assert not any("ARTFUL" in b.text or "A R T" in b.text for b in book.blocks)


def test_a_title_page_without_metadata_gives_title_and_author(tmp_path):
    p = Printer()
    p.heads = False
    p.new_page()  # title, author, dedication and chapter 1 on one page, no metadata
    _centred(p, "The Haunting of Hill House", 20)
    p.space(6)
    _centred(p, "Shirley Jackson", 14)
    p.space(10)
    _centred(p, "For Leonard Brown")
    p.space(20)
    _centred(p, "Chapter 1", 17)
    p.space(10)
    body_page(p, 3)
    book = parse(p.save(tmp_path))
    assert (book.title, book.author) == ("The Haunting of Hill House", "Shirley Jackson")
    assert [(b.text, b.role) for b in book.blocks[:4]] == [
        ("The Haunting of Hill House", TITLE_ROLE),
        ("Shirley Jackson", "frontmatter"),
        ("For Leonard Brown", ""),
        ("Chapter 1", ""),
    ]


def test_a_section_title_in_another_typeface_is_a_heading(tmp_path):
    p = Printer()
    p.new_page(heads=False)
    body_page(p, 2)
    p.space(16)
    p.line("Television, Auteurism, and National Cinema", font="helv", size=SIZE + 0.5)
    p.space(4)
    body_page(p, 2)
    book = parse(p.save(tmp_path))
    heading = block_with(book, "National Cinema")
    assert (heading.kind, heading.level) == ("heading", 2)


def test_printed_contents_running_onto_the_first_chapter_page(tmp_path):
    p = Printer()
    p.heads = False
    p.new_page()
    p.line("TABLE OF CONTENTS", x=150, size=14, advance=40)
    for k in range(1, 30):
        p.line(f"{k}. Rule number {k} of good writing.", x=LEFT + 10)
    p.new_page()
    for k in range(30, 34):
        p.line(f"{k}. Rule number {k} of good writing.", x=LEFT + 10)
    p.space(20)
    p.line("I. INTRODUCTORY", x=170, size=16, advance=40)
    body_page(p, 3)
    p.doc.set_toc([[1, "I. INTRODUCTORY", 2]])
    book = parse(p.save(tmp_path))
    assert block_with(book, "Rule number 33").role == "contents"
    intro = block_with(book, "INTRODUCTORY")
    assert intro.kind == "heading" and not intro.role
    assert book.toc[0].block == book.blocks.index(intro)


def test_outline_entries_for_cut_front_matter_collapse(tmp_path):
    p = Printer()
    p.heads = False
    p.new_page()
    p.line("Introduction", x=LEFT, size=22, advance=30)
    p.line("Haneke's Anachronism", x=LEFT, size=16, advance=40)
    body_page(p, 3)
    p.new_page()
    p.line("Chapter 1", x=LEFT, size=22, advance=40)
    body_page(p, 3)
    p.doc.set_toc([
        [1, "A Companion to Michael Haneke", 1], [2, "Contents", 1], [2, "Acknowledgments", 1],
        [2, "Introduction: Haneke's Anachronism", 1], [2, "Chapter 1", 2],
    ])
    book = parse(p.save(tmp_path))
    assert [(e.title, book.blocks[e.block].text) for e in book.toc] == [
        ("Introduction: Haneke's Anachronism", "Introduction"),
        ("Chapter 1", "Chapter 1"),
    ]


needs_artful = pytest.mark.skipif(not ARTFUL.is_file(), reason="Artful_Sentences.pdf not available")


@pytest.mark.slow
@needs_artful
def test_a_scanned_book_is_refused():
    with pytest.raises(BookImportError, match="“Artful Sentences: Syntax as Style” is a scanned book"):
        parse_pdf(ARTFUL, "artful")


@pytest.mark.slow
@pytest.mark.skipif(not SECRET_HISTORY.is_file(), reason="sample not available")
def test_the_secret_history():
    book = parse_pdf(SECRET_HISTORY, "secret")
    assert (book.title, book.author) == ("The Secret History", "Donna Tartt")
    assert [(b.text, b.role) for b in book.blocks[:2]] == [("Donna Tartt", "frontmatter"), ("The Secret History", TITLE_ROLE)]
    assert book.blocks[2].text.endswith("the dearest friend I will ever have in this world.")
    toc = [(e.level, e.title) for e in book.toc]
    assert toc[:4] == [(1, "Prologue"), (1, "Book 1"), (2, "Chapter 1"), (2, "Chapter 2")]
    assert (1, "Book 2") in toc and (2, "Chapter 8") in toc
    assert book.blocks[book.toc[0].block].text == "Prologue"
    assert not any(b.kind == "heading" and b.text[:1].islower() for b in book.blocks)


@pytest.mark.slow
@pytest.mark.skipif(not HILL_HOUSE.is_file(), reason="sample not available")
def test_the_haunting_of_hill_house():
    book = parse_pdf(HILL_HOUSE, "hill")
    assert (book.title, book.author) == ("The Haunting of Hill House", "Shirley Jackson")
    assert [b.role for b in book.blocks[:3]] == [TITLE_ROLE, "frontmatter", ""]
    assert [e.title for e in book.toc][:3] == ["Chapter 1", "Chapter 2", "Chapter 3"]


@pytest.mark.slow
@pytest.mark.skipif(not ELEMENTS.is_file(), reason="sample not available")
def test_the_elements_of_style():
    book = parse_pdf(ELEMENTS, "elements")
    assert book.title == "The Elements of Style"
    start = next(i for i, b in enumerate(book.blocks) if not b.role)
    assert book.blocks[start].text == "I. INTRODUCTORY"
    assert all(b.role in SKIPPED_ROLES or b.role == TITLE_ROLE for b in book.blocks[:start])
    assert book.blocks[-1].role == "contents"  # the outline printed at the end
