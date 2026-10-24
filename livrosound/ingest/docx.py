"""DOCX → Book with python-docx: the body's paragraphs (and table cells) in order.

Styles say what a paragraph is: Title → the top heading, Heading 1…9 (or a
style based on one, or an outline level) → headings, Quote / Intense Quote →
quote, everything else (list items included) → paragraph. A short Normal
paragraph that reads "Chapter 1" (textutil.HEADING_RE) is a heading too: many
manuscripts set chapter numbers by hand in bold. Runs give italic and bold
(their own setting, else their character or paragraph style's); line breaks
inside a paragraph make it verse when there are two or more.

Text is taken from every w:r of a paragraph, hyperlinks and tracked insertions
included, deletions left out; page headers and footers are separate parts and
never part of the book. Title and author come from the core properties (the
first author of "Franz Kafka; Breon Mitchell"), else the first Title/heading
paragraph, else the file name.

original.html renders each paragraph as <p> or <h1>–<h6> with the document's
own look as inline CSS (alignment, first-line and left indents, each run's font
family, size, weight and slant), each preceded, inside the element, by its
block's <a name="ls-b{i}"></a> anchor.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from livrosound.book import ANCHOR_PREFIX, Block, Book
from livrosound.textutil import HEADING_RE

from . import BookImportError, matter
from .heads import find_running_heads, widen_cuts
from .textmap import MappedText, TextBuilder, edit_source

_HEADING_STYLE = re.compile(r"^heading\s*(\d)$", re.I)
_QUOTE_STYLE = re.compile(r"quot|block\s*text|epigraph", re.I)
_ALIGN = {
    WD_ALIGN_PARAGRAPH.CENTER: "center",
    WD_ALIGN_PARAGRAPH.RIGHT: "right",
    WD_ALIGN_PARAGRAPH.JUSTIFY: "justify",
    WD_ALIGN_PARAGRAPH.LEFT: "left",
}


@dataclass
class _Run:
    text: str
    css: str  # inline CSS of the run in original.html


@dataclass
class _Para:
    kind: str
    level: int
    align: str
    css: str  # the paragraph's inline CSS
    runs: list[int] = field(default_factory=list)  # indices into the run list
    mapped: MappedText = field(default_factory=MappedText)
    tag: str = "p"


def parse_docx(path: Path, book_id: str) -> Book:
    """Parse a Word .docx file."""
    try:
        document = docx.Document(str(path))
    except Exception as e:  # not a zip, not a Word document, a .doc renamed .docx
        raise BookImportError("This Word document is damaged or isn't a .docx file.") from e
    runs: list[_Run] = []
    paras: list[_Para] = []
    char_styles = _CharStyles(document)
    for paragraph in _paragraphs(document):
        para = _paragraph(paragraph, runs, char_styles)
        if para is not None:
            paras.append(para)

    props = document.core_properties
    title = matter.clean_title((props.title or "").strip())
    if re.match(r"^(?:microsoft word - .*|untitled.*|document\d*|title)$", title, re.I):
        title = ""  # what Word fills in by itself
    author = matter.clean_author(re.split(r"\s*;\s*", (props.author or "").strip())[0])
    if not title:
        top = next((p for p in paras if p.kind == "heading" and p.level <= 1), None)
        title = matter.display_title(top.mapped.text) if top is not None else path.stem.replace("_", " ").strip()

    deletions: dict[int, list[tuple[int, int]]] = {}
    listed = matter.contents_lines([p.kind for p in paras], [p.mapped.text for p in paras])
    cuts = find_running_heads(
        [p.mapped.text if i not in listed else "" for i, p in enumerate(paras)], [p.kind for p in paras], title,
        [p.mapped.text for p in paras if p.kind == "heading"],
    )
    for cut_block in sorted({c.block for c in cuts}):
        p = paras[cut_block]
        spans = widen_cuts(p.mapped.text, [(c.start, c.end) for c in cuts if c.block == cut_block])
        for src, a, b in p.mapped.delete(spans):
            deletions.setdefault(src, []).append((a, b))
        p.mapped.strip()
    paras = [p for p in paras if p.mapped.text.strip()]
    paras = matter.number_headings(paras, lambda ranges: [deletions.setdefault(src, []).append((a, b)) for src, a, b in ranges])

    blocks = [Block(kind=p.kind, text=p.mapped.text, level=p.level, align=p.align, styles=list(p.mapped.styles)) for p in paras]
    if not blocks:
        raise BookImportError("No readable text was found in this book.")
    matter.assign_roles(blocks, title, author=author)
    matter.rank_levels(blocks)
    matter.link_contents(blocks)
    return Book(
        book_id=book_id, title=title, author=author, source_path=str(path), source_kind="docx",
        blocks=blocks, toc=matter.toc_from_headings(blocks),
        original_html=_original_html(document, paras, runs, deletions),
    )


def _paragraphs(document):
    """The body's paragraphs in order, table cells' paragraphs included (a merged cell once)."""
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document._body)
        elif child.tag == qn("w:tbl"):
            seen = set()
            for tc in child.iter(qn("w:tc")):
                if id(tc) in seen:
                    continue
                seen.add(id(tc))
                for p in tc.iterchildren(qn("w:p")):
                    yield Paragraph(p, document._body)


class _CharStyles:
    """Character styles by id, looked up once (python-docx's Run.style scans every style each time)."""

    def __init__(self, document) -> None:
        self._document = document
        self._cache: dict[str, tuple[bool | None, bool | None, str | None, object]] = {}

    def of(self, run: Run) -> tuple[bool | None, bool | None, str | None, object]:
        """(italic, bold, font name, font size) the run's character style sets."""
        rpr = run._r.rPr
        style_id = rpr.rStyle.val if rpr is not None and rpr.rStyle is not None else None
        if style_id is None:
            return None, None, None, None
        if style_id not in self._cache:
            element = self._document.styles.element.get_by_id(style_id)
            if element is None:
                self._cache[style_id] = (None, None, None, None)
            else:
                font = self._document.styles[element.name_val].font
                self._cache[style_id] = (font.italic, font.bold, font.name, font.size)
        return self._cache[style_id]


def _paragraph(paragraph: Paragraph, runs: list[_Run], char_styles: _CharStyles) -> _Para | None:
    style = paragraph.style
    kind, level = "paragraph", 0
    names = []
    s = style
    while s is not None and len(names) < 6:
        names.append(s.name or "")
        s = s.base_style
    for name in names:
        m = _HEADING_STYLE.match(name)
        if name.lower() == "title":
            kind, level = "heading", 1
            break
        if m:
            kind, level = "heading", int(m.group(1)) + 1
            break
        if _QUOTE_STYLE.search(name):
            kind = "quote"
            break
    outline = paragraph._p.find(f"{qn('w:pPr')}/{qn('w:outlineLvl')}")
    if kind == "paragraph" and outline is not None and (outline.get(qn("w:val")) or "9").isdigit() and int(outline.get(qn("w:val"))) < 9:
        kind, level = "heading", int(outline.get(qn("w:val"))) + 2

    builder = TextBuilder()
    first_run = len(runs)
    para_italic = bool(style.font.italic) if style is not None else False
    para_bold = bool(style.font.bold) if style is not None else False
    for r in _runs(paragraph):
        text = r.text
        if not text:
            continue
        style_italic, style_bold, style_font, style_size = char_styles.of(r)
        italic = r.italic if r.italic is not None else bool(style_italic) or para_italic
        bold = r.bold if r.bold is not None else bool(style_bold) or para_bold
        styles = tuple(st for st, on in (("i", italic), ("b", bold)) if on)
        if r.font.superscript:
            styles += ("sup",)
        src = len(runs)
        runs.append(_Run(text, _run_css(r, italic, bold, style_font, style_size)))
        pos = 0
        for k, piece in enumerate(text.split("\n")):
            if k:
                builder.line_break()
            builder.add(piece, src, pos, styles)
            pos += len(piece) + 1
    verse = kind == "paragraph" and builder.breaks >= 2
    mapped = builder.build(verse=verse)
    if not mapped.text.strip():
        return None
    if verse:
        kind = "verse"
    text = mapped.text.strip()
    if kind == "paragraph" and len(text.split()) <= 8 and HEADING_RE.match(text) and not matter.is_contents_line(text):
        kind, level = "heading", 2  # "Chapter 1" set by hand
    align = _ALIGN.get(paragraph.alignment if paragraph.alignment is not None else style.paragraph_format.alignment, "")
    tag = f"h{min(6, max(1, level))}" if kind == "heading" else ("blockquote" if kind == "quote" else "p")
    return _Para(kind, level, "" if align == "left" else align, _paragraph_css(paragraph, align),
                 list(range(first_run, len(runs))), mapped, tag)


def _runs(paragraph: Paragraph):
    """Every run of the paragraph in order, inside hyperlinks and insertions too, not deletions."""
    for r in paragraph._p.iter(qn("w:r")):
        parent = r.getparent()
        skip = False
        while parent is not None and parent is not paragraph._p:
            if parent.tag in (qn("w:del"), qn("w:moveFrom")):
                skip = True
                break
            parent = parent.getparent()
        if not skip:
            yield Run(r, paragraph)


def _paragraph_css(paragraph: Paragraph, align: str) -> str:
    fmt = paragraph.paragraph_format
    style_fmt = paragraph.style.paragraph_format if paragraph.style is not None else None
    css = []
    if align:
        css.append(f"text-align:{align}")
    first = fmt.first_line_indent if fmt.first_line_indent is not None else (style_fmt.first_line_indent if style_fmt else None)
    if first:
        css.append(f"text-indent:{first.pt:.1f}pt")
    left = fmt.left_indent if fmt.left_indent is not None else (style_fmt.left_indent if style_fmt else None)
    if left:
        css.append(f"margin-left:{left.pt:.1f}pt")
    return ";".join(css)


def _run_css(run: Run, italic: bool, bold: bool, style_font: str | None, style_size) -> str:
    css = []
    name = run.font.name or style_font
    if name:
        css.append(f"font-family:'{name}'")
    size = run.font.size or style_size
    if size:
        css.append(f"font-size:{size.pt:.1f}pt")
    if italic:
        css.append("font-style:italic")
    if bold:
        css.append("font-weight:bold")
    if run.font.superscript:
        css.append("vertical-align:super")
    return ";".join(css)


def _original_html(document, paras: list[_Para], runs: list[_Run], deletions: dict[int, list[tuple[int, int]]]) -> str:
    try:
        normal = document.styles["Normal"].font
    except KeyError:
        normal = None
    body_css = []
    if normal is not None and normal.name:
        body_css.append(f"font-family:'{normal.name}'")
    if normal is not None and normal.size:
        body_css.append(f"font-size:{normal.size.pt:.1f}pt")
    anchors: dict[int, list[tuple[int, str]]] = {}
    for i, p in enumerate(paras):
        first = p.mapped.source_start()
        if first is not None:
            anchors.setdefault(first[0], []).append((first[1], f"{ANCHOR_PREFIX}{i}"))
    out = []
    for p in paras:
        out.append(f'<{p.tag} style="{_attr(p.css)}">' if p.css else f"<{p.tag}>")
        for k in p.runs:
            run = runs[k]
            pieces = edit_source(run.text, deletions.get(k, []), anchors.get(k, []))
            for piece in pieces:
                name = None
                if isinstance(piece, tuple):
                    name, piece = piece  # the anchor holds the block's first character
                text = html.escape(piece, quote=False).replace("\n", "<br/>")
                if text and run.css:
                    text = f'<span style="{_attr(run.css)}">{text}</span>'
                out.append(f'<a name="{name}">{text}</a>' if name else text)
        out.append(f"</{p.tag}>\n")
    style = _attr(";".join(body_css))
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"/></head>'
        f'<body style="{style}">\n' + "".join(out) + "</body></html>\n"
    )


def _attr(value: str) -> str:
    """A double-quoted attribute value (single quotes, as in font names, stay readable)."""
    return html.escape(value, quote=False).replace('"', "&quot;")
