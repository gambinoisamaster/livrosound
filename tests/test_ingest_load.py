"""load_book (formats, errors, the cache) and the machinery the ingesters share:
text maps (textmap), running heads and page numbers (heads), front/back matter
(matter).

The running-head cases are the shapes scanned books really have: "10 ARTFUL
SENTENCES" / "SHORT SENTENCES 19" from Artful Sentences, glued inside a
paragraph or alone on a line, next to text that must never be cut ("In 1984
George Orwell", "Henry VIII", "1. TEMPERANCE.", "CHAPTER IV").
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from livrosound import paths
from livrosound.book import PARSER_VERSION, TITLE_ROLE, Block, Book, file_book_id
from livrosound.ingest import (
    SUPPORTED_SUFFIXES,
    BookImportError,
    ImportCancelled,
    book_id_for,
    load_book,
    matter,
    strip_running_heads,
)
from livrosound.ingest.heads import find_running_heads, letters_key, page_break_join
from livrosound.ingest.textmap import TextBuilder, edit_source

STORY = "\n\n".join(
    ["THE LONG NIGHT", "CHAPTER ONE"]
    + [f"Paragraph {n} tells the story of a long night, and of the people who stayed up for it." for n in range(1, 4)]
)


# ---------------------------------------------------------------------------
# load_book


def test_supported_suffixes():
    assert SUPPORTED_SUFFIXES == (".pdf", ".epub", ".docx", ".txt")


def test_unknown_suffix_and_missing_file(tmp_path):
    doc = tmp_path / "book.mobi"
    doc.write_text("x")
    with pytest.raises(BookImportError, match="can't open .mobi"):
        load_book(doc)
    with pytest.raises(BookImportError, match="couldn't be found"):
        load_book(tmp_path / "gone.txt")


def test_empty_book_has_no_readable_text(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("\n\n   \n")
    with pytest.raises(BookImportError, match="No readable text"):
        load_book(empty)


def test_corrupt_files_get_friendly_errors(tmp_path):
    epub = tmp_path / "bad.epub"
    epub.write_bytes(b"not a zip at all")
    with pytest.raises(BookImportError, match="damaged or DRM"):
        load_book(epub)
    docx = tmp_path / "bad.docx"
    docx.write_bytes(b"PK\x03\x04 broken")
    with pytest.raises(BookImportError, match="Word document"):
        load_book(docx)


def test_load_book_saves_and_reuses_the_cache(tmp_path, monkeypatch):
    src = tmp_path / "night.txt"
    src.write_text(STORY)
    book = load_book(src)
    folder = paths.book_dir(book.book_id)
    assert book.book_id == file_book_id(src)
    assert book.source_path == str(src.resolve())
    assert book.source_kind == "txt"
    assert (folder / "book.json").is_file() and (folder / "original.html").is_file()
    assert not (folder / "import.json").exists()  # Book.load's parser version is the whole cache key

    from livrosound.ingest import txt

    monkeypatch.setattr(txt, "parse_txt", lambda *a: pytest.fail("parsed again instead of using the cache"))
    again = load_book(src)
    assert [b.text for b in again.blocks] == [b.text for b in book.blocks]
    assert again.original_html == book.original_html


def test_same_file_from_another_folder_reuses_the_cache_with_its_new_path(tmp_path):
    first = tmp_path / "a" / "night.txt"
    second = tmp_path / "b" / "night copy.txt"
    for f in (first, second):
        f.parent.mkdir()
        f.write_text(STORY)
    book = load_book(first)
    moved = load_book(second)
    assert moved.book_id == book.book_id
    assert moved.source_path == str(second.resolve())
    assert Book.load(paths.book_dir(book.book_id)).source_path == str(second.resolve())


def test_stale_cache_is_parsed_again_and_its_audio_discarded(tmp_path):
    src = tmp_path / "night.txt"
    src.write_text(STORY)
    book = load_book(src)
    folder = paths.book_dir(book.book_id)
    data = json.loads((folder / "book.json").read_text())
    data["parser_version"] = PARSER_VERSION - 1
    data["blocks"][-1]["text"] = "stale"
    (folder / "book.json").write_text(json.dumps(data))
    (folder / "audio.sqlite").write_bytes(b"old audio")
    fresh = load_book(src)
    assert fresh.blocks[-1].text != "stale"
    assert not (folder / "audio.sqlite").exists()


def test_pdfs_go_to_the_pdf_parser_once(tmp_path, monkeypatch):
    calls = []

    def parse_pdf(path, book_id, progress, cancelled):
        calls.append(book_id)
        progress(1, 1, "Reading page 1 of 1")
        return Book(book_id, "Digital", "", str(path), "pdf", blocks=[Block("paragraph", "Words on a page.")])

    _pdf_parser(monkeypatch, parse_pdf)
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    seen = []
    book = load_book(pdf, progress=lambda *a: seen.append(a))
    assert load_book(pdf).blocks[0].text == "Words on a page." and len(calls) == 1 and seen
    assert book.book_id == file_book_id(pdf)


def test_cancel_and_progress_reach_the_epub_parser(tmp_path):
    from tests.test_ingest_epub import make_epub3

    path = make_epub3(tmp_path)
    seen = []
    with pytest.raises(ImportCancelled):
        load_book(path, progress=lambda done, total, msg: seen.append((done, total)), cancelled=lambda: bool(seen))
    assert seen and seen[0][1] > 1
    book = load_book(path, progress=lambda done, total, msg: seen.append((done, total)))
    assert seen[-1][0] == seen[-1][1]
    assert book.source_kind == "epub"


def test_unexpected_parser_failure_becomes_a_friendly_error(tmp_path, monkeypatch):
    from livrosound.ingest import txt

    def boom(*a):
        raise KeyError("oops")

    monkeypatch.setattr(txt, "parse_txt", boom)
    src = tmp_path / "x.txt"
    src.write_text("Hello there.")
    with pytest.raises(BookImportError, match="couldn't be read") as info:
        load_book(src)
    assert isinstance(info.value.__cause__, KeyError)


# ---------------------------------------------------------------------------
# textmap


def test_builder_collapses_whitespace_like_a_browser():
    b = TextBuilder()
    b.add("  Hello \n\t ", 0, 0)
    b.add("big", 1, 0, ("i",))
    b.add(" world­!", 2, 0)
    b.add("  ", 3, 0)
    m = b.build()
    assert m.text == "Hello big world!"
    assert m.styles == [(6, 9, "i")]
    # "big" maps to source 1, the space before "world" to the no-break space in source 2
    assert m.source_ranges(6, 9) == [(1, 0, 3)]
    assert m.source_ranges(9, 10) == [(2, 0, 1)]


def test_builder_line_breaks_and_ligatures():
    b = TextBuilder()
    b.add("The ﬁrst line", 0, 0)
    b.line_break()
    b.add("  second line ", 1, 0)
    b.line_break()
    b.line_break()
    assert b.build(verse=True).text == "The first line\nsecond line"
    b2 = TextBuilder()
    b2.add("one", 0, 0)
    b2.line_break()
    b2.line_break()
    b2.add("two", 1, 0)
    assert b2.build().text == "one two"


def test_mapped_text_delete_slice_join_keep_the_map():
    b = TextBuilder()
    b.add("the walls 24 ARTFUL SENTENCES are white", 7, 100, ("i",))
    m = b.build()
    removed = m.delete([(10, 30)])
    assert m.text == "the walls are white"
    assert removed == [(7, 110, 130)]
    assert m.styles == [(0, 19, "i")]
    assert m.source_ranges(10, 13) == [(7, 130, 133)]
    head, tail = m.slice(0, 9), m.slice(10, 19)
    joined = head.join(tail, " ")
    assert joined.text == "the walls are white"
    assert joined.source_start() == (7, 100)
    assert joined.source_ranges(10, 13) == [(7, 130, 133)]


def test_edit_source_deletes_and_places_anchors():
    pieces = edit_source("abc HEAD def", [(4, 9)], [(0, "ls-b0"), (9, "ls-b1")])
    assert pieces == [("ls-b0", "a"), "bc ", ("ls-b1", "d"), "ef"]


# ---------------------------------------------------------------------------
# Running heads


def _page(n: int, body: str) -> list[Block]:
    return [Block("paragraph", body)]


def _artful_blocks() -> list[Block]:
    """Pages of a scanned book with heads glued into the text and on their own lines."""
    filler = " ".join(["word"] * 120)
    blocks = [Block("heading", "CHAPTER ONE", level=1), Block("heading", "Short Sentences", level=2)]
    for page in range(10, 22):
        head = f"{page} ARTFUL SENTENCES" if page % 2 == 0 else f"SHORT SENTENCES {page}"
        if page % 3 == 0:
            blocks.append(Block("paragraph", head))
            blocks.append(Block("paragraph", f"{filler} and the walls are white."))
        else:
            blocks.append(Block("paragraph", f"{filler} the walls {head} are white.", styles=[(0, 4, "i")]))
    return blocks


@pytest.mark.parametrize("form", ["number first", "number last"])
def test_running_heads_are_cut_in_both_forms(form):
    blocks = _artful_blocks()
    strip_running_heads(blocks, "Artful Sentences: Syntax as Style", ["Chapter One: Short Sentences"])
    text = "\n".join(b.text for b in blocks)
    assert "ARTFUL SENTENCES" not in text
    assert "SHORT SENTENCES 1" not in text
    assert "the walls are white." in text
    assert "walls  are" not in text
    assert blocks[0].text == "CHAPTER ONE" and blocks[1].text == "Short Sentences"  # never running heads
    glued = next(b for b in blocks if "the walls are white" in b.text and b.styles)
    assert glued.styles == [(0, 4, "i")]


def test_heads_need_recurrence_or_a_title_match():
    filler = " ".join(["word"] * 150)
    blocks = [
        Block("paragraph", f"In 1984 George Orwell published it. {filler}"),
        Block("paragraph", f"Henry VIII married. {filler} Henry IV ruled. {filler} Henry V fought."),
        Block("paragraph", "1. TEMPERANCE."),
        Block("paragraph", "2. SILENCE."),
        Block("paragraph", "3. ORDER."),
        Block("heading", "CHAPTER IV"),
        Block("paragraph", f"See Chapter 3 and Chapter 5 and Chapter 7 for more. {filler}"),
        Block("paragraph", "ALL-MALE COLLEGE, 1957"),
    ]
    before = [b.text for b in blocks]
    cuts = strip_running_heads(blocks, "All-Male College, 1957", ["CHAPTER IV"])
    assert cuts == []
    assert [b.text for b in blocks] == before


def test_recurring_capitals_with_rising_page_numbers_are_heads_without_a_title():
    filler = " ".join(["word"] * 250)
    blocks = [Block("paragraph", f"{filler} {n} THE OLD HOUSE {filler}") for n in range(40, 46)]
    cuts = find_running_heads([b.text for b in blocks], ["paragraph"] * 6, "", [])
    assert len(cuts) == 6 and all(c.text.endswith("THE OLD HOUSE") for c in cuts)


def test_lone_page_numbers_versus_numbered_sections():
    filler = " ".join(["word"] * 300)
    pages = []
    for n in range(5, 12):
        pages += [Block("paragraph", filler), Block("paragraph", str(n))]
    cuts = find_running_heads([b.text for b in pages], [b.kind for b in pages], "", [])
    assert {c.text for c in cuts} == {str(n) for n in range(5, 12)}
    long_filler = " ".join(["word"] * 3000)
    sections = []
    for n in range(1, 6):
        sections += [Block("paragraph", str(n)), Block("paragraph", long_filler)]
    assert find_running_heads([b.text for b in sections], [b.kind for b in sections], "", []) == []


def test_page_aware_heads_and_bracketed_page_numbers():
    texts = [
        "THE OVERCOAT mention in what department. There is nothing in the world more readily moved to wrath than a gov[12]",
        "THE OVERCOAT ernment office, and the clerk went home [13]",
        "THE OVIERGOAT hasy. “What an infliction!” said the mother. And he [14]",
        "THE OVERCOAT would take it, gazing only at the paper. [15]",
        "1H EB OVER COAL not, of course, to say much, but since it is now the rule. [16]",
    ]
    cuts = find_running_heads(texts, ["paragraph"] * 5, "", [], page_starts=list(range(5)))
    heads = sorted((c.block, c.text) for c in cuts if c.kind == "head")
    numbers = {c.block: c for c in cuts if c.kind == "page-number"}
    assert heads == [(0, "THE OVERCOAT"), (1, "THE OVERCOAT"), (2, "THE OVIERGOAT"), (3, "THE OVERCOAT"), (4, "1H EB OVER COAL")]
    assert next(c for c in cuts if c.block == 0 and c.kind == "head").heading  # the section's first, clean head
    assert numbers[0].glued and not numbers[1].glued
    assert page_break_join("in some gov", "ernment office", True) == ""
    assert page_break_join("the clerk went home", "hasy. said", False) == " "
    assert page_break_join("He went home.", "The next day", False) is None
    assert page_break_join("UNITED STATES OF AMERICA", "CONTENTS", False) is None


def test_letters_key_ignores_case_spacing_and_punctuation():
    assert letters_key("A R T F U L  Sentences") == letters_key("artful-sentences!") == "ARTFULSENTENCES"


# ---------------------------------------------------------------------------
# Front and back matter


def _book(*items: tuple[str, str]) -> list[Block]:
    return [Block(kind, text, level=1 if kind == "heading" else 0) for kind, text in items]


def test_roles_front_matter_title_and_contents_links():
    blocks = _book(
        ("image", ""),
        ("heading", "THE LONG NIGHT"),
        ("paragraph", "by Ana Lima"),
        ("paragraph", "Copyright © 2020 Ana Lima. All rights reserved."),
        ("paragraph", "ISBN 978-0-00-000000-0"),
        ("paragraph", "Printed in Brazil"),
        ("heading", "Contents"),
        ("paragraph", "Chapter 1 ........ 3"),
        ("paragraph", "Chapter 2 ........ 9"),
        ("heading", "Dedication"),
        ("paragraph", "For my mother."),
        ("heading", "Chapter 1"),
        ("paragraph", "It was night. " * 20),
        ("heading", "Chapter 2"),
        ("paragraph", "It was still night. " * 20),
        ("paragraph", "THE END"),
        ("paragraph", "Thanks for reading! Visit our site for more books."),
    )
    matter.assign_roles(blocks, "The Long Night")
    matter.link_contents(blocks)
    roles = [b.role for b in blocks]
    assert roles[0] == "cover"
    assert roles[1] == TITLE_ROLE
    assert roles[2] == "frontmatter"
    assert roles[3:6] == ["copyright"] * 3
    assert roles[6:9] == ["contents"] * 3
    assert roles[9:16] == [""] * 7  # the dedication and the book itself are read
    assert roles[16] == "blurb"
    assert blocks[7].link == 11 and blocks[8].link == 13
    assert sum(r == TITLE_ROLE for r in roles) == 1


def test_back_cues_only_at_the_back_and_readable_cues_stay():
    blocks = _book(
        ("heading", "Index"),  # at the front: not an index
        ("paragraph", "A word " * 30),
        ("heading", "Preface"),
        ("paragraph", "Why I wrote this. " * 10),
        ("heading", "Chapter 1"),
        *[("paragraph", "Text " * 30)] * 6,
        ("heading", "Acknowledgments"),
        ("paragraph", "Thanks to everyone."),
        ("heading", "Index"),
        ("paragraph", "Apples, 12, 45"),
        ("paragraph", "Pears, 3"),
    )
    matter.assign_roles(blocks, "Fruit")
    roles = [b.role for b in blocks]
    assert roles[:11] == [""] * 11
    assert roles[11:13] == ["acknowledgments"] * 2
    assert roles[13:] == ["index"] * 3


def test_toc_from_headings_joins_chapter_number_and_title_and_levels_rank():
    blocks = [
        Block("heading", "CHAPTER ONE", level=2),
        Block("heading", "Short Sentences", level=3),
        Block("paragraph", "Text."),
        Block("heading", "CHAPTER TWO", level=2),
        Block("paragraph", "More."),
    ]
    matter.rank_levels(blocks)
    assert [b.level for b in blocks] == [1, 2, 0, 1, 0]
    toc = matter.toc_from_headings(blocks)
    assert [(e.title, e.block, e.level) for e in toc] == [("Chapter One: Short Sentences", 0, 1), ("Chapter Two", 3, 1)]


def test_split_byline():
    assert matter.split_byline("OVERCOAT and Other Stories by Nikolay Gogol ¢ ALFRED A. KNOPF MCMXXIII") == (
        "Overcoat and Other Stories",
        "Nikolay Gogol",
    )
    assert matter.split_byline("No byline here") == ("", "")


def test_clean_author_and_title_keys():
    assert matter.clean_author("Cooper, Dennis, 1953-") == "Dennis Cooper"
    assert matter.clean_author("Unknown") == ""
    assert matter.clean_author("Leo Tolstoy") == "Leo Tolstoy"
    keys = matter.title_keys("Blood Meridian (Picador Collection)")
    assert letters_key("BLOOD MERIDIAN") in keys
    assert letters_key("Oblivion") in matter.title_keys("Oblivion: Stories")


def test_notices_from_ebook_makers():
    for text in (
        "* A Distributed Proofreaders Canada eBook *",
        "This eBook is made available at no cost and with very few restrictions.",
        "Thank you for downloading this Touchstone eBook.",
        "The Project Gutenberg eBook of The River",
    ):
        assert matter.is_notice(text), text
    assert matter.is_notice("Date first posted: Nov. 4, 2021", front=True)
    assert not matter.is_notice("Date first posted: Nov. 4, 2021")
    assert not matter.is_notice("It was the best of times.")


def test_a_contents_section_never_swallows_the_book():
    filler = "It was night and the river ran on past the old house by the bridge. " * 4
    blocks = _book(("heading", "Contents"), ("paragraph", "One"), ("paragraph", "Two"), ("paragraph", filler), ("paragraph", filler))
    matter.assign_roles(blocks, "River")
    assert [b.role for b in blocks] == ["contents", "contents", "contents", "", ""]
    # with units (EPUB files), a section ends with its file
    blocks = _book(("heading", "Contents"), ("paragraph", "One"), ("paragraph", "Short one."), ("paragraph", "Short two."))
    matter.assign_roles(blocks, "River", units=[0, 2])
    assert [b.role for b in blocks][:2] == ["contents", "contents"] and blocks[2].role != "contents"


def test_toc_never_lists_skipped_blocks_but_links_still_reach_them(tmp_path, monkeypatch):
    """Whatever the parser (here a PDF-like one), Book.toc skips front and back matter."""
    from livrosound.book import TocEntry

    def parse_pdf(path, book_id, progress, cancelled):
        blocks = [
            Block("heading", "Contents", role="contents"),
            Block("paragraph", "Acknowledgments ........ 3", role="contents", link=4),
            Block("heading", "Chapter One"),
            Block("paragraph", "It was night."),
            Block("heading", "Acknowledgments", role="acknowledgments"),
            Block("heading", "Index", role="index"),
        ]
        toc = [TocEntry("Chapter One", 2), TocEntry("Acknowledgments", 4), TocEntry("Index", 5)]
        return Book(book_id, "Scan", "", str(path), "pdf", blocks=blocks, toc=toc)

    fake = types.ModuleType("livrosound.ingest.pdf")
    fake.parse_pdf = parse_pdf
    monkeypatch.setitem(sys.modules, "livrosound.ingest.pdf", fake)
    import livrosound.ingest as ingest

    monkeypatch.setattr(ingest, "pdf", fake, raising=False)
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    for book in (load_book(pdf), load_book(pdf)):  # parsed, then from the cache
        assert [(e.title, e.block) for e in book.toc] == [("Chapter One", 2)]
        assert book.blocks[1].link == 4


def test_clean_title_drops_series_tags_but_keeps_subtitles():
    assert matter.clean_title("Blood Meridian (Picador Collection)") == "Blood Meridian"
    assert matter.clean_title("Anna Karenina (Penguin Classics)") == "Anna Karenina"
    assert matter.clean_title("The Stranger (Vintage International)") == "The Stranger"
    assert matter.clean_title("Middlemarch (Oxford World's Classics)") == "Middlemarch"
    assert matter.clean_title("The Secret History (z-lib.org)") == "The Secret History"
    assert matter.clean_title("Oblivion: Stories") == "Oblivion: Stories"
    assert matter.clean_title("The Hobbit (or There and Back Again)") == "The Hobbit (or There and Back Again)"


def test_dedications_stay_readable_on_a_title_page():
    blocks = _book(
        ("heading", "Maurice"),
        ("paragraph", "by"),
        ("paragraph", "E. M. FORSTER"),
        ("paragraph", "A NOVEL"),
        ("paragraph", "Dedicated to a Happier Year"),
        ("heading", "PART One"),
        ("paragraph", "Once a term the whole school went for a walk. " * 8),
    )
    matter.assign_roles(blocks, "Maurice", author="E. M. Forster", units=[0, 5])
    assert [b.role for b in blocks] == [TITLE_ROLE, "frontmatter", "frontmatter", "frontmatter", "", "", ""]
    assert matter.is_dedication("For Lucien") and matter.is_dedication("To my wife, Helen")
    assert not matter.is_dedication("To the Lighthouse") and not matter.is_dedication("To be or not to be")


def _items(*items):
    from livrosound.ingest.textmap import TextBuilder

    out = []
    for k, (kind, text) in enumerate(items):
        builder = TextBuilder()
        builder.add(text, k, 0)
        out.append(types.SimpleNamespace(kind=kind, level=2 if kind == "heading" else 0, mapped=builder.build(), anchors=[f"a{k}"]))
    return out


def test_number_headings_merge_part_words_and_find_numbered_chapters():
    text = ("Once a term the whole school went for a walk. " * 20).strip()  # a chapter, not a list item
    items = _items(
        ("heading", "PART "), ("paragraph", "One"), ("paragraph", "1"), ("paragraph", text),
        ("paragraph", "2"), ("paragraph", text), ("heading", "PART"), ("paragraph", "Two"), ("paragraph", "3"), ("paragraph", text),
    )
    deleted = []
    out = matter.number_headings(items, deleted.extend)
    assert [(i.kind, i.mapped.text) for i in out] == [
        ("heading", "PART One"), ("heading", "1"), ("paragraph", text), ("heading", "2"), ("paragraph", text),
        ("heading", "PART Two"), ("heading", "3"), ("paragraph", text),
    ]
    assert out[1].level == out[0].level + 1 and out[0].anchors == ["a0", "a1"]
    assert deleted == []


def test_number_headings_drop_a_numeral_that_repeats_the_chapter_number():
    items = _items(("heading", "Chapter 3"), ("paragraph", "3"), ("paragraph", "It rained."), ("heading", "CHAPTER FOUR"),
                   ("paragraph", "IV"), ("paragraph", "It rained again."))
    deleted = []
    out = matter.number_headings(items, deleted.extend)
    assert [i.mapped.text for i in out] == ["Chapter 3", "It rained.", "CHAPTER FOUR", "It rained again."]
    assert deleted == [(1, 0, 1), (4, 0, 2)]  # their source ranges, for original.html


def test_number_headings_leave_lists_and_scales_alone_and_join_a_number_to_its_title():
    items = _items(
        ("paragraph", "1"), ("heading", "Are You Smart, Scattered, and Lost?"), ("paragraph", "Text of the chapter."),
        ("paragraph", "1."), ("paragraph", "Pick a goal."), ("paragraph", "2."), ("paragraph", "Do it."),
        ("paragraph", "5"), ("paragraph", "4"), ("paragraph", "3"), ("paragraph", "2"), ("paragraph", "1"),
    )
    out = matter.number_headings(items, lambda ranges: None)
    assert (out[0].kind, out[0].mapped.text) == ("heading", "1 Are You Smart, Scattered, and Lost?")
    assert [i.kind for i in out[1:]] == ["paragraph"] * 10
    joined = matter.number_headings(_items(("heading", "2"), ("heading", "Your Profile"), ("paragraph", "Text.")), print)
    assert [(i.kind, i.mapped.text) for i in joined] == [("heading", "2 Your Profile"), ("paragraph", "Text.")]


def test_book_id_for_files_and_unzipped_epub_folders(tmp_path):
    import zipfile

    from tests.test_ingest_epub import make_epub2

    src = tmp_path / "night.txt"
    src.write_text(STORY)
    assert book_id_for(src) == file_book_id(src) == load_book(src).book_id
    zipped = make_epub2(tmp_path)
    folder = tmp_path / "unzipped" / "Tales.epub"
    with zipfile.ZipFile(zipped) as z:
        z.extractall(folder)
    assert book_id_for(folder) == load_book(folder).book_id != book_id_for(zipped)
    with pytest.raises(OSError):
        book_id_for(tmp_path / "missing.txt")


def _pdf_parser(monkeypatch, parse):
    fake = types.ModuleType("livrosound.ingest.pdf")
    fake.parse_pdf = parse
    monkeypatch.setitem(sys.modules, "livrosound.ingest.pdf", fake)
    import livrosound.ingest as ingest

    monkeypatch.setattr(ingest, "pdf", fake, raising=False)


@pytest.mark.parametrize("failure", [ImportCancelled(), BookImportError("This PDF couldn't be read.")])
def test_a_cancelled_or_failed_reimport_keeps_the_audio(tmp_path, monkeypatch, failure):
    """A book whose cache must be read again (a newer parser) keeps its audio when that
    re-import is cancelled or fails; only a finished re-import drops it."""
    version = {"n": 1}

    def parse(path, book_id, progress, cancelled):
        return Book(book_id, "Doc", "", str(path), "pdf", blocks=[Block("paragraph", f"Parse {version['n']}.")])

    _pdf_parser(monkeypatch, parse)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    book = load_book(pdf)
    folder = paths.book_dir(book.book_id)
    audio = folder / "audio.sqlite"
    audio.write_bytes(b"made audio")
    data = json.loads((folder / "book.json").read_text())
    data["parser_version"] = PARSER_VERSION - 1  # stale: load_book must parse again
    (folder / "book.json").write_text(json.dumps(data))

    def fail(*args):
        raise failure

    _pdf_parser(monkeypatch, fail)
    with pytest.raises(type(failure)):
        load_book(pdf)
    assert audio.read_bytes() == b"made audio"
    assert (folder / "book.json").exists()

    version["n"] = 2
    _pdf_parser(monkeypatch, parse)
    assert load_book(pdf).blocks[0].text == "Parse 2."
    assert not audio.exists()


def test_paragraph_spans_cut_long_text_at_sentence_ends():
    from livrosound.ingest.heads import paragraph_spans

    text = " ".join(f"This is sentence number {n}, which goes on for a little while to fill the page." for n in range(80))
    spans = paragraph_spans(text)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(text[a:b].endswith(".") and 600 <= b - a <= 1600 for a, b in spans)
    assert all(text[b:c].strip() == "" for (_, b), (c, _) in zip(spans, spans[1:]))
    assert paragraph_spans("Short. Text.") == [(0, 12)]


def test_page_start_debris_needs_page_starts_and_spares_real_openings():
    pages = [
        "THE OVERCOAT mention in what department. " * 6,
        "THE OVERCOAT us the grade is what must be put first. " * 6,
        "THE OVERCOAT would take it, gazing only at the paper. " * 6,
        "HESSEN EVeSSCY eS P ROS PE Cor head and her curls were wet. " * 6,
        "The overcoat was too thin, and the frost bit through it. " * 6,
        "THE OVERCOAT It was a cold day in the department. " * 6,
    ]
    cuts = find_running_heads(pages, ["paragraph"] * 6, "", [], page_starts=list(range(6)))
    heads = {c.block: c.text for c in cuts if c.kind == "head"}
    assert heads[3] == "HESSEN EVeSSCY eS P ROS PE Cor"
    assert 4 not in heads and heads[5] == "THE OVERCOAT"
    assert not [c for c in find_running_heads(pages, ["paragraph"] * 6, "", []) if c.block == 3]
