"""DOCX ingestion: a Word file built here with python-docx covers styles,
runs, tables, hyperlinks, manual chapter headings, front/back matter and
original.html; a slow test reads The Trial.
"""

from __future__ import annotations

import time
from pathlib import Path

import docx
import pytest
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from livrosound.book import TITLE_ROLE
from livrosound.ingest import BookImportError
from livrosound.ingest.docx import parse_docx
from tests.test_ingest_epub import EBOOKS, qt_check


def _hyperlink(paragraph, text: str) -> None:
    link = OxmlElement("w:hyperlink")
    link.set(qn("w:anchor"), "somewhere")
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = text
    run.append(t)
    link.append(run)
    paragraph._p.append(link)


def _tracked(paragraph, kind: str, text: str) -> None:
    wrapper = OxmlElement(f"w:{kind}")
    wrapper.set(qn("w:id"), "1")
    wrapper.set(qn("w:author"), "x")
    run = OxmlElement("w:r")
    t = OxmlElement("w:t" if kind == "ins" else "w:delText")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    wrapper.append(run)
    paragraph._p.append(wrapper)


def make_docx(tmp_path: Path, *, title: str | None = "The Long Night") -> Path:
    d = docx.Document()
    if title:
        d.core_properties.title = title
    d.core_properties.author = "Ana Lima; Rui Costa"
    d.add_paragraph("The Long Night", style="Title")
    by = d.add_paragraph("by Ana Lima")
    by.alignment = WD_ALIGN_PARAGRAPH.CENTER
    d.add_paragraph("Copyright © 2024 Ana Lima. All rights reserved.")
    d.add_heading("Contents", level=1)
    d.add_paragraph("Chapter 1 ........ 3")
    d.add_paragraph("")
    manual = d.add_paragraph()
    r = manual.add_run("Chapter 1")
    r.bold, r.font.size = True, Pt(18)
    d.add_heading("The Storm", level=2)
    p = d.add_paragraph()
    p.paragraph_format.first_line_indent = Pt(18)
    p.add_run("It was a ")
    dark = p.add_run("dark")
    dark.italic = True
    p.add_run(" and ")
    stormy = p.add_run("stormy")
    stormy.bold = True
    styled = p.add_run(" night,")
    styled.font.name, styled.font.size = "Georgia", Pt(13)
    _hyperlink(p, " said the link")
    _tracked(p, "ins", " and the insertion")
    _tracked(p, "del", " NOT THIS")
    p.add_run(".")
    d.add_paragraph("A quoted passage.", style="Quote")
    d.add_paragraph("First item of a list.", style="List Bullet")
    verse = d.add_paragraph()
    verse.add_run("Line one")
    verse.add_run().add_break()
    verse.add_run("Line two")
    verse.add_run().add_break()
    verse.add_run("Line three")
    centered = d.add_paragraph("A centered line.")
    centered.alignment = WD_ALIGN_PARAGRAPH.CENTER
    table = d.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Cell one."
    table.cell(0, 1).text = "Cell two."
    d.add_paragraph("The story went on for a long while, and it was a good story with many words in it. " * 3)
    d.add_heading("Index", level=1)
    d.add_paragraph("storm, 3")
    path = tmp_path / "night.docx"
    d.save(path)
    return path


def by_text(book, text: str):
    return next(b for b in book.blocks if b.text == text)


def test_docx_text_kinds_and_styles(tmp_path):
    book = parse_docx(make_docx(tmp_path), "d1")
    assert (book.title, book.author) == ("The Long Night", "Ana Lima")
    story = "It was a dark and stormy night, said the link and the insertion."
    first = by_text(book, story)
    assert first.kind == "paragraph"
    assert (9, 13, "i") in first.styles and (18, 24, "b") in first.styles
    assert by_text(book, "A quoted passage.").kind == "quote"
    assert by_text(book, "First item of a list.").kind == "paragraph"
    assert by_text(book, "Line one\nLine two\nLine three").kind == "verse"
    assert by_text(book, "A centered line.").align == "center"
    assert by_text(book, "Cell one.").kind == by_text(book, "Cell two.").kind == "paragraph"
    chapter, storm = by_text(book, "Chapter 1"), by_text(book, "The Storm")
    assert chapter.kind == storm.kind == "heading" and chapter.level < storm.level
    assert all(b.text for b in book.blocks)  # the empty paragraph is gone


def test_docx_roles_toc_and_contents(tmp_path):
    book = parse_docx(make_docx(tmp_path), "d1")
    roles = {b.text: b.role for b in book.blocks}
    assert roles["The Long Night"] == TITLE_ROLE
    assert roles["by Ana Lima"] == "frontmatter"
    assert roles["Copyright © 2024 Ana Lima. All rights reserved."] == "copyright"
    assert roles["Contents"] == roles["Chapter 1 ........ 3"] == "contents"
    assert roles["Index"] == roles["storm, 3"] == "index"
    assert roles["Chapter 1"] == ""
    line = by_text(book, "Chapter 1 ........ 3")
    assert book.blocks[line.link].text == "Chapter 1"
    assert [(e.title, e.level) for e in book.toc] == [("Chapter 1: The Storm", 1)]


def test_docx_original_html(tmp_path, qapp):
    book = parse_docx(make_docx(tmp_path), "d1")
    assert qt_check(book) == ([], [])
    html = book.original_html
    assert "font-family:'Georgia'" in html and "font-size:13.0pt" in html
    assert "text-indent:18.0pt" in html and "text-align:center" in html
    assert "font-style:italic" in html and "NOT THIS" not in html


def test_docx_title_falls_back_to_the_title_paragraph(tmp_path):
    book = parse_docx(make_docx(tmp_path, title=None), "d2")
    assert book.title == "The Long Night"


def test_not_a_docx(tmp_path):
    path = tmp_path / "old.docx"
    path.write_bytes(b"\xd0\xcf\x11\xe0 an old .doc")
    with pytest.raises(BookImportError, match="Word document"):
        parse_docx(path, "d3")


@pytest.mark.slow
def test_real_the_trial(qapp):
    path = EBOOKS / "The Trial by Franz Kafka.docx"
    if not path.is_file():
        pytest.skip("sample missing")
    t = time.perf_counter()
    book = parse_docx(path, "trial")
    elapsed = time.perf_counter() - t
    print(f"\nThe Trial: {len(book.blocks)} blocks, {len(book.toc)} TOC entries, {elapsed:.2f} s")
    assert elapsed < 5
    assert (book.title, book.author) == ("The Trial", "Franz Kafka")
    assert [b.role for b in book.blocks[:4]] == [TITLE_ROLE, "frontmatter", "frontmatter", ""]
    assert book.toc[0].title == "Chapter 1: Arrest"
    assert book.blocks[book.toc[0].block + 2].text.startswith("Someone must have slandered Josef K.")
    bibliography = next(i for i, b in enumerate(book.blocks) if b.text == "Bibliography")
    assert all(b.role == "index" for b in book.blocks[bibliography:])
    assert by_text(book, "The Life of Franz Kafka").role == ""
    assert all(b.text.strip() for b in book.blocks)
    assert qt_check(book) == ([], [])


def test_docx_title_loses_a_series_tag(tmp_path):
    book = parse_docx(make_docx(tmp_path, title="The Long Night (Penguin Classics)"), "d4")
    assert book.title == "The Long Night"
