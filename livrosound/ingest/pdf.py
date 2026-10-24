"""PDF → Book: a printed page's lines, put back together as the book's blocks.

A PDF knows where every glyph sits, not what a paragraph is. Printed books encode
structure in geometry, and that is what this module reads, page by page, from the
PDF's own text (PyMuPDF "rawdict": a box per word, its font size, italic, bold,
superscript):

    paragraph   a first line indented from the text's left edge, or vertical space
    heading     larger type, a short centred line, or "CHAPTER ONE" (textutil.HEADING_RE)
    quote       lines set in from the margin (and usually smaller): epigraphs, block
                quotes; an attribution set in further ("Anthony Burgess, Enderby, 406")
                is its own right-aligned quote block
    note        smaller, tighter lines at the foot of the page (footnotes)
    furniture   running heads and page numbers: dropped

Everything is measured relative to the page's own type (em = the body's font size,
pitch = the distance between baselines), never in absolute points.

The rules that aren't obvious:
- Only PDFs with digital text are read. A scan (most pages one full-page image, with
  no visible text or only an old OCR layer's invisible text) is refused with
  BookImportError: OCR is too imperfect to narrate (user decision).
- Word geometry is kept exactly: every Block carries the box of each word with the
  span of Block.text it covers, so the page view can highlight a sentence on the
  page and map a click back. Every text repair (ligatures, letter-spaced capitals,
  drop capitals, line-end hyphens) happens on word tokens before a block's text is
  assembled, so spans stay right: a drop cap keeps its own box ("A" + "NTHONY"),
  spaces belong to no word.
- Running heads and page numbers are never part of the book (user requirement):
  the first/last line of a page is furniture when it carries a page number at
  either end ("10 ARTFUL SENTENCES", "SHORT SENTENCES 19"), is a lone page number,
  or its words recur at the top/bottom of several pages, compared ignoring case,
  spacing and letter-spacing ("A R T F U L"). A chapter's opening line is not
  (user correction): "CHAPTER ONE" occurring once is a heading, whatever ornament
  sits beside it.
- A line that stops mid-sentence and one that goes on with it stay in one block, even
  when one is centred or set in (a dedication, an epigraph): the narrator never
  pauses mid-sentence.
- Only the book itself is read (user requirement): the cover, blurbs, title copies,
  the copyright page, acknowledgements, the printed contents and the index get
  Block.role (book.SKIPPED_ROLES); the cleanest title-page copy of the title gets
  book.TITLE_ROLE. Each line of the printed contents is its own block, linked
  (Block.link) to the chapter it names. TOC: the PDF outline (with the book's
  "Chapter N" headings under a coarse one), else the printed contents, else headings.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf

from .. import textutil
from ..book import SKIPPED_ROLES, TITLE_ROLE, Block, Book, PageInfo, TocEntry
from .heads import letters_key

# A scan: this share of its pages are one image covering the page, without visible text.
SCANNED_SHARE = 0.6
IMAGE_PAGE_COVER = 0.85
SCAN_SAMPLE_PAGES = 24

_FLAGS = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES  # word boxes, no image data
_ITALIC_FONT = re.compile(r"ital|obl|[-,_ ]it(?![a-z])", re.I)
_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demi", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f\ufffd]")  # a font without a Unicode map reads as \x01
_ORNAMENT = re.compile(r"^(?=.*[*⁂•∗✱✻❦❧☙◆◇#§])[*⁂•·∗✱✻❦❧☙◆◇#§~.\s]+$")
# A footnote's opening mark: "1 Loving.", "2Isabel." (fused), "* Magazine of…".
_NOTE_MARKER = re.compile(r"^[\[(]?([0-9]{1,2}|[*†‡§¹²³⁴⁵⁶⁷⁸⁹])[\])]?\.?(?:\s+(?=\S)|(?=[A-Z“‘\"'(\[]))")
_CHAPTER_HEADING = re.compile(r"^(?:chapter|chap\.|part|book|canto|stave|letter|act|volume|vol\.)\b", re.I)
# Headings that open the book itself, where the front matter ends.
_BODY_START = re.compile(r"^(?:chapter|chap\.|part|book|canto|stave|preface|foreword|introduction|prologue)\b", re.I)
_CONTENTS_HEADING = re.compile(r"^(?:table\s+of\s+)?contents\b", re.I)
_ACK_HEADING = re.compile(r"^acknowledge?ments?\b", re.I)
_INDEX_HEADING = re.compile(
    r"^(?:index\b|general\s+index|bibliography|select(?:ed)?\s+bibliography|works\s+cited|references$)", re.I
)
_COPYRIGHT = re.compile(
    r"copyright|©|\(c\)\s*(?:19|20)\d\d|all rights reserved|\bisbn\b|library of congress|published by"
    r"|printed (?:and bound )?(?:in|by)|first (?:published|edition)|cataloging[- ]in[- ]publication"
    r"|no part of this (?:book|publication)|composed in|typeset (?:in|by)",
    re.I,
)
_ISBN = re.compile(r"\bisbn\b", re.I)
_OUTLINE_PAGE = re.compile(r"^document\s+outline$", re.I)  # calibre prints the outline at the end
_ABOUT_AUTHOR = re.compile(r"^about\s+the\s+(?:author|authors|translator)\b", re.I)
_PRAISE = re.compile(r"^praise\s+for\b", re.I)
_ALSO_BY = re.compile(r"^(?:also|other books|books)\s+by\b", re.I)
# A printed contents line: "1 Short Sentences ........ 9", "Chapter IV. The Storm 54".
_ENTRY_NUMBER = re.compile(r"^(?:(?:chapter|part|book)\s+)?(\d{1,3}|[IVXLC]{1,6})([.):]\s*|\s+)(?=\S)", re.I)
_ENTRY_PAGE = re.compile(r"(?:\s*[.·…_]{2,}\s*|\s+)(\d{1,4}|[ivxlc]{1,7})\.?$")
# Small words a title line never ends on: the heading goes on in the next line.
_OPEN_END = re.compile(r"(?:\b(?:of|and|the|a|an|to|in|for|with|on|or|&)|[-—–,&])$", re.I)


def _no_progress(done: int, total: int, message: str) -> None:
    pass


def _never() -> bool:
    return False


# ---------------------------------------------------------------------------
# Data


@dataclass(eq=False)
class _Tok:
    """A word on the page with its box (PDF points from the page's top-left)."""

    text: str
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    size: float  # font size
    italic: bool = False
    bold: bool = False
    sup: bool = False
    glue: bool = False  # no space before the next token: a superscript, a drop cap, a split word
    soft: bool = False  # ends in a soft hyphen: a sure syllable break at a line end
    dropcap: bool = False  # a drop capital: its box spans several lines, so it doesn't measure its row
    font: str = ""  # the font's family ("DanteMT" for "ABCDEF+DanteMT-Italic")

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass(eq=False)
class _Row:
    """Tokens that share a baseline, left to right."""

    toks: list[_Tok]
    cap: bool = False  # opens a paragraph with a drop capital
    beside_cap: bool = False  # set in to clear a drop capital: not a paragraph indent
    label: str = ""  # head | orn | quote | attrib | verse | para | cont
    level: int = 0
    qnew: bool = False  # quote: starts a new quote block
    role: str = ""

    @property
    def _line(self) -> list[_Tok]:
        return [t for t in self.toks if not t.dropcap] or self.toks

    @property
    def x0(self) -> float:
        return min(t.x0 for t in self.toks)

    @property
    def x1(self) -> float:
        return max(t.x1 for t in self.toks)

    @property
    def y0(self) -> float:
        return min(t.y0 for t in self._line)

    @property
    def y1(self) -> float:
        return max(t.y1 for t in self._line)

    @property
    def height(self) -> float:
        return statistics.median(t.height for t in self._line)

    @property
    def size(self) -> float:
        return statistics.median(t.size for t in self._line)

    @property
    def text(self) -> str:
        return " ".join(t.text for t in self.toks)

    @property
    def font(self) -> str:
        return Counter(t.font for t in self._line).most_common(1)[0][0]

    @property
    def page(self) -> int:
        return self.toks[0].page


@dataclass
class _Metrics:
    em: float  # body font size: the unit of indents
    h: float  # body row height
    pitch: float  # baseline to baseline
    left: float  # body text's left edge
    right: float  # body text's right edge (justified prose)

    @property
    def center(self) -> float:
        return (self.left + self.right) / 2


@dataclass
class _Page:
    index: int
    width: float
    height: float
    rows: list[_Row]
    picture: bool  # carries a large illustration (a cover)
    folio: int | None = None  # its printed page number, from the running head
    frames: list[tuple[list[_Row], _Metrics]] = field(default_factory=list)  # columns in reading order
    notes: list[_Row] = field(default_factory=list)

    def words(self) -> int:
        return sum(len(r.toks) for r in self.rows)


# ---------------------------------------------------------------------------
# Entry point


def parse_pdf(
    path: Path,
    book_id: str,
    progress: Callable[[int, int, str], None] = _no_progress,
    cancelled: Callable[[], bool] = _never,
    *,
    pages: range | None = None,
) -> Book:
    """The Book in a PDF with digital text; BookImportError for a scan. pages limits
    the import to some pages (tests, previews)."""
    from . import BookImportError, ImportCancelled

    path = Path(path)
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # pymupdf raises its own types for damaged files
        raise BookImportError("This PDF couldn't be opened. It may be damaged.") from e
    with doc:
        if doc.needs_pass:
            raise BookImportError("This PDF is password-protected. Remove the password and try again.")
        title, author = _metadata(doc, path)
        if is_scanned(doc):
            raise BookImportError(
                f"“{title or path.stem}” is a scanned book. LivroSound reads PDFs with digital text; "
                "scanned books aren't supported yet. Try an EPUB, DOCX or TXT, or a digital PDF of it."
            )
        indices = [i for i in (pages if pages is not None else range(doc.page_count)) if 0 <= i < doc.page_count]
        read: list[_Page] = []
        total = len(indices)
        for k, i in enumerate(indices):
            if cancelled():
                raise ImportCancelled()
            progress(k, total, f"Reading page {k + 1} of {total}")
            page = doc[i]
            cover = _image_cover(page)
            read.append(_Page(i, page.rect.width, page.rect.height, _rows(_text_layer(page, i)), 0.3 <= cover))
        if cancelled():
            raise ImportCancelled()
        progress(total, total, "Laying out the pages")
        outline = doc.get_toc(simple=True)
        info = [PageInfo(round(p.rect.width, 2), round(p.rect.height, 2)) for p in doc]
    blocks, toc, byline = _layout(read, title, author, outline)
    title = _shorter_title(title, blocks) if title else _title_from_blocks(blocks) or _stem_title(path)
    author = author or byline
    return Book(
        book_id=book_id,
        title=title,
        author=author,
        source_path=str(path),
        source_kind="pdf",
        blocks=blocks,
        toc=toc,
        pages=info,
        text_box=_text_box(read),
    )


def is_scanned(doc: pymupdf.Document) -> bool:
    """Is this PDF a scan? Most of its pages (sampled) are one image covering the page,
    with no visible text: an image-only PDF, or a scan under an old OCR layer's
    invisible text (the Internet Archive's)."""
    n = doc.page_count
    if not n:
        return False
    sample = sorted({int((k + 0.5) * n / min(n, SCAN_SAMPLE_PAGES)) for k in range(min(n, SCAN_SAMPLE_PAGES))})
    scans = sum(1 for i in sample if _image_cover(doc[i]) >= IMAGE_PAGE_COVER and _visible_chars(doc[i]) < 30)
    return scans >= SCANNED_SHARE * len(sample)


def _visible_chars(page: pymupdf.Page) -> int:
    """Characters drawn visibly (an OCR layer's text is invisible: render mode 3 or
    zero opacity)."""
    return sum(
        len(span["chars"])
        for span in page.get_texttrace()
        if span["type"] != 3 and span.get("opacity", 1) > 0
    )


def _layout(pages: list[_Page], title: str, author: str, outline: list) -> tuple[list[Block], list[TocEntry], str]:
    """Pages of rows → blocks, the table of contents, and the author's name when the
    title page gives it and the metadata doesn't."""
    for p in pages:
        for r in p.rows:
            _join_letter_spacing(r)
        p.rows = [r for r in p.rows if any(c.isalnum() for c in r.text) or _ORNAMENT.match(r.text)]
        for r in p.rows:
            _strip_ornament(r)
    body_em = _median([r.size for p in pages for r in p.rows if len(r.toks) >= 5]) or 10.0
    _strip_running_heads(pages, title, author, body_em)
    vocab = textutil.Vocab.from_texts("\n".join(r.text for r in p.rows) for p in pages)
    fonts = Counter(t.font for p in pages for r in p.rows for t in r.toks)
    body_font = fonts.most_common(1)[0][0] if fonts else ""
    _measure_pages(pages)
    indent = _paragraph_indent(pages)
    for p in pages:
        for rows, m in p.frames:
            _drop_marginalia(rows, m)
        if p.frames:
            rows, m = p.frames[-1]
            body, p.notes = _split_notes(rows, m)
            p.frames[-1] = (body, m)
        for rows, m in p.frames:
            _label(rows, m, indent, body_font)
    pages_in = [p for p in pages if p.frames or p.notes]
    byline = _assign_roles(pages_in, title, author, body_em, outline)
    drafts = _build(pages_in)
    blocks = [_block(d, vocab) for d in drafts]
    blocks = [b for b in blocks if b.text.strip() or b.kind == "separator"]
    printed = _contents_links(blocks, pages_in)  # links the contents page's lines either way
    detected = _heading_toc(blocks)
    toc = _with_chapters(_outline_toc(outline, blocks), detected, blocks) or printed or detected
    return blocks, toc, byline


# ---------------------------------------------------------------------------
# Reading lines with word boxes


def _text_layer(page: pymupdf.Page, index: int) -> list[list[_Tok]]:
    """The text layer's lines as tokens: one per word, split at spaces (or at a gap,
    for PDFs that position words without space characters) and at superscripts."""
    lines: list[list[_Tok]] = []
    for block in page.get_text("rawdict", flags=_FLAGS)["blocks"]:
        for line in block.get("lines", ()):
            if line["dir"][0] < 0.98:
                continue  # rotated text: a spine, a stamp, a sideways margin note
            toks: list[_Tok] = []
            cur: _Tok | None = None
            for span in line["spans"]:
                size, font, flags = span["size"], span["font"], span["flags"]
                italic = bool(flags & 2 or _ITALIC_FONT.search(font))
                bold = bool(flags & 16 or _BOLD_FONT.search(font))
                sup = bool(flags & 1)
                top, bottom = span["bbox"][1], span["bbox"][3]
                for ch in span["chars"]:
                    c = ch["c"]
                    x0, x1 = ch["bbox"][0], ch["bbox"][2]
                    if not c or c.isspace():
                        cur = None
                        continue
                    if cur is not None:
                        if x0 - cur.x1 > 0.2 * size:
                            cur = None
                        elif sup != cur.sup:
                            cur.glue = True
                            cur = None
                    if cur is None:
                        cur = _Tok(c, index, x0, top, x1, bottom, size, italic, bold, sup, font=_family(font))
                        toks.append(cur)
                    else:
                        cur.text += c
                        cur.x1 = max(cur.x1, x1)
                        cur.y0, cur.y1 = min(cur.y0, top), max(cur.y1, bottom)
            if toks:
                lines.append(toks)
    return [cleaned for toks in lines if (cleaned := _clean_tokens(toks))]


def _family(font: str) -> str:
    """'ABCDEF+DanteMT-Italic' -> 'DanteMT': a font's family without subset tag or style."""
    return re.split(r"[-,]", font.split("+")[-1])[0]


def _image_cover(page: pymupdf.Page) -> float:
    """Share of the page its largest image covers (a scan or a cover: all of it)."""
    area = abs(page.rect)
    if not area:
        return 0.0
    return max((abs(pymupdf.Rect(i["bbox"]) & page.rect) / area for i in page.get_image_info()), default=0.0)


def _clean_tokens(toks: list[_Tok]) -> list[_Tok]:
    """Unicode clean-up of each token (ligatures, odd spaces, glyphs without Unicode)."""
    out = []
    for t in toks:
        text = _CONTROL.sub("", t.text)
        if text.endswith("\u00ad"):
            t.soft = True
            text = text[:-1] + "-"
        text = textutil.normalize_chars(text).replace("\n", "")
        if not text.strip():
            continue
        out += _split_box(t, text.split()) if " " in text else [_retext(t, text)]
    return out


def _retext(t: _Tok, text: str) -> _Tok:
    t.text = text
    return t


def _split_box(t: _Tok, parts: list[str]) -> list[_Tok]:
    """A token split into words, each with a share of its box in proportion to its length."""
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return [_retext(t, parts[0])] if parts else []
    total = sum(len(p) for p in parts) + len(parts) - 1
    width = t.x1 - t.x0
    out, pos = [], 0
    for k, p in enumerate(parts):
        x0 = t.x0 + width * pos / total
        pos += len(p)
        x1 = t.x0 + width * pos / total
        pos += 1
        last = k == len(parts) - 1
        out.append(replace(t, text=p, x0=x0, x1=x1, glue=t.glue if last else False, soft=t.soft if last else False))
    return out


def _rows(lines: list[list[_Tok]]) -> list[_Row]:
    """Lines that share a baseline become one row; drop capitals are set aside and
    joined to the line they open."""
    toks = [t for line in lines for t in line]
    if not toks:
        return []
    typical = statistics.median(t.height for t in toks)
    caps = [t for t in toks if t.height >= 1.8 * typical and len(t.text.strip("\"'“‘’”")) == 1 and t.text.strip("\"'“‘’”").isalpha()]
    if caps:
        lines = [[t for t in line if t not in caps] for line in lines]
        lines = [line for line in lines if line]
    rows: list[_Row] = []
    for line in sorted(lines, key=lambda ts: sum(t.y0 + t.y1 for t in ts) / len(ts)):
        y0, y1 = min(t.y0 for t in line), max(t.y1 for t in line)
        h, cy = y1 - y0, (y0 + y1) / 2
        for row in reversed(rows[-4:]):
            if abs(cy - (row.y0 + row.y1) / 2) <= 0.3 * min(h, row.y1 - row.y0) and not _collides(row.toks, line):
                row.toks = sorted(row.toks + line, key=lambda t: t.x0)
                break
        else:
            rows.append(_Row(sorted(line, key=lambda t: t.x0)))
    for cap in caps:
        _attach_cap(cap, rows)
    rows.sort(key=lambda r: (r.y0 + r.y1) / 2)
    return rows


def _collides(a: list[_Tok], b: list[_Tok]) -> bool:
    """Do two sets of tokens overlap side by side (so they can't share a line)?"""
    for s in a:
        for t in b:
            overlap = min(s.x1, t.x1) - max(s.x0, t.x0)
            if overlap > 0.3 * min(s.x1 - s.x0, t.x1 - t.x0, 1e9):
                return True
    return False


def _attach_cap(cap: _Tok, rows: list[_Row]) -> None:
    """A drop capital opens the first line set beside it ('A' + 'NTHONY' -> 'ANTHONY');
    the lines beside it are indented to clear it, which is not a paragraph indent."""
    span = cap.y1 - cap.y0
    beside = [
        r for r in rows
        if r.x0 > cap.x0 and r.x0 - cap.x1 < 3 * r.height
        and r.y0 < cap.y1 - 0.2 * span and r.y1 > cap.y0 and r.y0 > cap.y0 - 0.5 * r.height
    ]
    if not beside:
        rows.append(_Row([cap]))
        return
    first = min(beside, key=lambda r: r.y0)
    for r in beside:
        r.beside_cap = True
    cap.glue = _cap_joins(cap.text.strip("\"'“‘’”"), first.toks[0].text)
    cap.dropcap = True
    cap.size = first.size
    first.toks.insert(0, cap)
    first.cap = True


def _cap_joins(letter: str, nxt: str) -> bool:
    """Is a drop capital the first letter of the next word ('A' + 'NTHONY'), or a word
    of its own ('I' + 'STOOD', 'A' + 'man')?"""
    m = re.match(rf"[{textutil.LETTER}]+", nxt)
    if not m:
        return False
    word = m.group(0)
    if letter.upper() in "AIO" and textutil.is_word(word) and not textutil.is_word(letter + word):
        return False
    return True


# ---------------------------------------------------------------------------
# Furniture: ornaments, running heads, page numbers


def _strip_ornament(row: _Row) -> None:
    """An ornament beside a heading (a dingbat or a symbol-font flourish: '❧ CHAPTER
    ONE', 'WA CONTENTS') is not part of it."""
    if not 2 <= len(row.toks) <= 8 or _chapter_like(row.text):
        return
    for side in (0, -1):
        t = row.toks[side]
        rest = row.toks[1:] if side == 0 else row.toks[:-1]
        if not _chapter_like(" ".join(x.text for x in rest)):
            continue
        near = rest[0] if side == 0 else rest[-1]
        gap = near.x0 - t.x1 if side == 0 else t.x0 - near.x1
        core = textutil.WORD_RE.findall(t.text)
        wordy = bool(core) and all(textutil.is_word(w) for w in core)
        if len(t.text) <= 4 and not _folio(t.text) and not t.text.strip(".").isdigit() and (gap >= 0.9 * t.size or not wordy):
            row.toks.remove(t)
            return


def _folio(word: str) -> int | None:
    """A page number token: '14', '[14]', or a lowercase roman 'xii' (as a negative)."""
    w = word.strip(".,:;“”\"'‘’")
    if not w or not textutil.PAGE_NUMBER_RE.match(w):
        return None
    digits = w.strip("[]()").lower().removeprefix("p.").removeprefix("page").strip()
    if any(c.isdigit() for c in digits):
        digits = digits.replace("l", "1").replace("i", "1").replace("o", "0")
        return int(digits) if digits.isdigit() and 0 < int(digits) < 5000 else None
    value = textutil.roman_to_int(digits)
    return -value if value else None


def _head_parts(row: _Row) -> tuple[list[str], int | None, str]:
    """(words, page number at either end, key of the other words)."""
    text = re.sub(r"(?<=\d) (?=\d)", "", textutil.fix_spaced_letters(row.text))  # "1 0" -> "10"
    words = text.split()
    number = None
    rest = list(words)
    if rest and (n := _folio(rest[-1])) is not None:
        number = n
        rest.pop()
    if rest and (n := _folio(rest[0])) is not None:
        number = n if number is None else number
        rest.pop(0)
    return words, number, letters_key(" ".join(rest))


@dataclass
class _Heads:
    """What the running heads of a book look like."""

    counts: Counter  # key (words without the page number) -> pages it opens or closes
    known: set[str]  # keys of the book's title and author
    body_em: float
    offset: int | None  # PDF page index minus printed page number
    opened: dict[str, int] = field(default_factory=dict)  # key -> page whose first line it was, kept


def _strip_running_heads(pages: list[_Page], title: str, author: str, body_em: float) -> None:
    """Drop each page's running head and page number (top and bottom lines), and
    remember the printed page number."""
    counts: Counter[str] = Counter()
    offsets: Counter[int] = Counter()
    for p in pages:
        keys = set()
        for r in {id(r): r for r in (p.rows[:2] + p.rows[-1:])}.values():
            words, number, key = _head_parts(r)
            if len(key) >= 3:
                keys.add(key)
            if number and number > 0 and len(words) <= 8 and not _chapter_like(" ".join(words)):
                offsets[p.index - number] += 1
        counts.update(keys)
    common = offsets.most_common(1)
    heads = _Heads(
        counts,
        {k for t in (title, title.split(":")[0], author) if len(k := letters_key(t)) >= 4},
        body_em,
        common[0][0] if common and common[0][1] >= 3 else None,
    )
    for p in pages:
        pitch = _median([b.y0 - a.y0 for a, b in zip(p.rows, p.rows[1:])]) or 1.0
        for top in (True, False):
            while p.rows:
                row = p.rows[0] if top else p.rows[-1]
                inside = row.y0 < 0.16 * p.height if top else row.y1 > 0.84 * p.height
                neighbour = (p.rows[1] if top else p.rows[-2]) if len(p.rows) > 1 else None
                apart = neighbour is None or abs(neighbour.y0 - row.y0) >= 1.35 * pitch
                if not inside or not _is_head(row, p.index, top, apart, heads):
                    if top:
                        heads.opened.setdefault(_head_parts(row)[2], p.index)
                    break
                number = _head_parts(row)[1]
                if number and number > 0 and p.folio is None:
                    p.folio = number
                p.rows.remove(row)


def _is_head(row: _Row, index: int, top: bool, apart: bool, heads: _Heads) -> bool:
    words, number, key = _head_parts(row)
    if not words:
        return True
    if len(words) == 1 and number is not None:
        return True  # a lone page number
    text = " ".join(words)
    on_folio = number is not None and number > 0 and heads.offset is not None and abs(index - heads.offset - number) <= 1
    if _chapter_like(text) and not on_folio:
        # "CHAPTER ONE", "Chapter 12", "INDEX" open a chapter; but "BIBLIOGRAPHY-INDEX 281"
        # on page after page is a running head (its number the page's, or recurring).
        if number is None or _CHAPTER_HEADING.match(text) or heads.counts[key] < 3:
            return False
    n = len(words) - (number is not None)
    letters = re.sub(r"[^A-Za-z]", "", text)
    caps = len(letters) >= 3 and letters.isupper()
    small = row.size <= 0.92 * heads.body_em
    repeated = heads.counts[key] >= 3
    titled = any(len(key) >= 4 and _similar(key, k) >= 0.85 for k in heads.known)
    # The opening line of a section ("TRANSLATOR'S NOTE") repeated atop its next pages
    # without a page number (the number sits at the foot).
    first = heads.opened.get(key)
    if top and first is not None and 0 < index - first <= 60 and len(key) >= 6 and n <= 8 and (caps or small) and apart:
        return True
    if number is not None and n <= 10:
        return repeated or titled or (top and (apart or caps or small))
    if repeated and n <= 10:
        return apart or caps or small
    return top and titled and n <= 8 and (caps or small) and apart


def _chapter_like(text: str) -> bool:
    return bool(textutil.HEADING_RE.match(text))


def _similar(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


# ---------------------------------------------------------------------------
# Letter-spacing


def _join_letter_spacing(row: _Row) -> None:
    """Letter-spaced capitals ("C H A P T E R  O N E": tracked small caps, which MuPDF
    reads with a space between every letter) rejoined, each word boxing its letters."""
    toks = row.toks
    out: list[_Tok] = []
    i = 0
    while i < len(toks):
        j = i
        while j < len(toks) and len(toks[j].text) == 1 and toks[j].text.isalpha():
            j += 1
        if j - i >= 2:
            out += _join_spaced(toks[i:j])
            i = j
            continue
        out.append(toks[i])
        i += 1
    row.toks = out


def _join_spaced(letters: list[_Tok]) -> list[_Tok]:
    """'A R T F U L  S E N T E N C E S' (one token per letter) -> 'ARTFUL', 'SENTENCES'."""
    gaps = [b.x0 - a.x1 for a, b in zip(letters, letters[1:])]
    typical = statistics.median(gaps)
    spaced = letters[0].text
    for gap, t in zip(gaps, letters[1:]):
        spaced += ("  " if gap > 1.8 * max(typical, 0.05 * t.size) else " ") + t.text
    fixed = textutil.fix_spaced_letters(spaced)
    if fixed == spaced:
        return letters
    out: list[_Tok] = []
    k = 0
    for word in fixed.split():
        group = letters[k : k + len(word)]
        k += len(word)
        first = group[0]
        out.append(replace(first, text=word, x1=group[-1].x1, y0=min(t.y0 for t in group), y1=max(t.y1 for t in group)))
    return out


# ---------------------------------------------------------------------------
# Margins


def _segments(toks: list[_Tok], gap: float) -> list[list[_Tok]]:
    """Tokens split where they lie further apart than gap (a page number stays with its line)."""
    out = [[toks[0]]]
    for t in toks[1:]:
        if t.x0 - out[-1][-1].x1 > gap and _folio(t.text) is None:
            out.append([t])
        else:
            out[-1].append(t)
    return out


def _drop_marginalia(rows: list[_Row], m: _Metrics) -> None:
    """Short marks outside the text column: a poem's line numbers ("10", "20")."""
    for r in rows:
        while len(r.toks) > 1 and r.toks[0].x1 < m.left - 0.5 * m.em and len(r.toks[0].text) <= 3:
            r.toks.pop(0)
        while len(r.toks) > 1 and r.toks[-1].x0 > m.right + 1.0 * m.em and len(r.toks[-1].text) <= 3:
            r.toks.pop()


# ---------------------------------------------------------------------------
# Measuring pages


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _measure(rows: list[_Row]) -> _Metrics | None:
    body = [r for r in rows if len(r.toks) >= 4 and not r.cap]
    if len(body) < 5:
        return None
    em = statistics.median(r.size for r in body)
    h = statistics.median(r.height for r in body)
    steps = [b.y0 - a.y0 for a, b in zip(rows, rows[1:]) if 0.7 * h < b.y0 - a.y0 < 1.7 * h]
    pitch = statistics.median(steps) if steps else 1.2 * h
    xs = sorted(r.x0 for r in body if not r.beside_cap)
    clusters: list[list[float]] = []
    for x in xs:
        if clusters and x - clusters[-1][-1] <= 0.4 * em:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    big = [c for c in clusters if len(c) >= max(2, 0.08 * len(body))]  # quotes may outnumber body lines
    left = statistics.median((big or clusters)[0])
    # The right edge full lines reach (justified prose): the rightmost end shared by two
    # lines; one line overshooting it (hanging punctuation, a scrawl) is ignored.
    ends = sorted((r.x1 for r in body), reverse=True)
    right = next((x for x in ends if sum(abs(y - x) <= 0.6 * em for y in ends) >= 2), ends[0])
    return _Metrics(em, h, pitch, left, right)


def _columns(rows: list[_Row]) -> list[list[_Row]]:
    """A two-column page (an index) as its two columns, left first; else [rows].
    A gutter is a vertical band few line pieces cross; a heading above the columns
    goes first, a line straddling the gutter between them is cut in two."""
    if len(rows) < 12:
        return [rows]
    em = _median([r.size for r in rows])
    pieces = [(r, s) for r in rows for s in _segments(r.toks, 2.0 * em)]
    lo, hi = min(r.x0 for r in rows), max(r.x1 for r in rows)
    best = None
    for k in range(30, 71, 2):
        g = lo + (hi - lo) * k / 100
        crossing = [r for r, s in pieces if s[0].x0 < g < s[-1].x1]
        left = sum(1 for _, s in pieces if s[-1].x1 <= g)
        right = sum(1 for _, s in pieces if s[0].x0 >= g)
        if left >= 5 and right >= 5 and (best is None or len(crossing) < len(best[1])):
            best = (g, crossing)
    if best is None or len(best[1]) > max(2, 0.1 * len(pieces)):
        return [rows]
    g, crossing = best
    sides = [[r for r, s in pieces if s[-1].x1 <= g], [r for r, s in pieces if s[0].x0 >= g]]
    # A heading set right of the gutter but above every left-column line goes first.
    crossing += [r for r in sides[1] if r.y1 <= min(x.y0 for x in sides[0]) + 1 and r not in sides[0]]
    sides[1] = [r for r in sides[1] if r not in crossing]
    top = min(r.y0 for c in sides for r in c)
    bottom = max(r.y1 for c in sides for r in c)
    above = [r for r in crossing if r.y1 <= top + 1]
    below = [r for r in crossing if r.y0 >= bottom - 1]
    inside = [r for r in crossing if r not in above and r not in below]
    for r in inside:
        left_end = max((t.x1 for t in r.toks if (t.x0 + t.x1) / 2 < g), default=None)
        right_start = min((t.x0 for t in r.toks if (t.x0 + t.x1) / 2 >= g), default=None)
        if left_end is not None and right_start is not None and right_start - left_end < 0.8 * em:
            return [rows]  # a line running across the gutter: not a two-column page
    out: list[list[_Row]] = [[], []]
    for r in rows:
        if r in above or r in below:
            continue
        for side in (0, 1):
            toks = [t for t in r.toks if ((t.x0 + t.x1) / 2 < g) == (side == 0)]
            if toks:
                out[side].append(_Row(toks, r.cap, r.beside_cap))
    return [above + out[0], out[1] + below]


def _measure_pages(pages: list[_Page]) -> None:
    """Each page's columns and metrics; pages with too few lines borrow the book's
    typical metrics for their side (left and right pages have different margins)."""
    measured: dict[int, list[_Metrics]] = {0: [], 1: []}
    for p in pages:
        columns = _columns(p.rows)
        p.frames = [(rows, _measure(rows)) for rows in columns]
        if len(columns) == 1 and p.frames[0][1]:
            measured[p.index % 2].append(p.frames[0][1])
    typical = {}
    for parity in (0, 1):
        ms = measured[parity] or measured[1 - parity]
        if ms:
            typical[parity] = _Metrics(*(statistics.median(getattr(m, f) for m in ms) for f in ("em", "h", "pitch", "left", "right")))
    width = _median([m.right - m.left for ms in measured.values() for m in ms])
    for p in pages:
        fallback = typical.get(p.index % 2)
        frames = []
        for rows, m in p.frames:
            if not rows:
                continue
            if m is not None and len(p.frames) == 1 and width and m.right - m.left < 0.9 * width:
                m.right = m.left + width  # a page of short lines (verse, examples): the column is as wide as elsewhere
            if m is None:
                m = fallback or _Metrics(rows[0].size, rows[0].height, 1.2 * rows[0].height, min(r.x0 for r in rows), max(r.x1 for r in rows))
            frames.append((rows, m))
        p.frames = frames


def _paragraph_indent(pages: list[_Page]) -> float:
    """The book's first-line indent in ems (0 when paragraphs aren't indented)."""
    insets = []
    for p in pages:
        for rows, m in p.frames:
            for a, b in zip(rows, rows[1:]):
                ia, ib = (a.x0 - m.left) / m.em, (b.x0 - m.left) / m.em
                if 0.5 <= ia <= 5 and abs(ib) < 0.4 and a.x1 > m.right - 1.0 * m.em and len(a.toks) >= 4 and not a.beside_cap:
                    insets.append(ia)
    return statistics.median(insets) if len(insets) >= 3 else 0.0


# ---------------------------------------------------------------------------
# Footnotes and line labels


def _split_notes(rows: list[_Row], m: _Metrics) -> tuple[list[_Row], list[_Row]]:
    """The footnotes at the foot of a page: below the body after a gap, flush with the
    margin, set smaller than the body, opening with a note mark ("1 Syntactic
    Structures…") or set tighter."""
    n = len(rows)
    if n < 3:
        return rows, []
    k = n
    while k > 1 and rows[k - 1].size < 0.92 * m.em and (rows[k - 1].x0 - m.left) / m.em < 2.0:
        k -= 1
    zone = rows[k:]
    if zone and len(zone) <= 0.5 * n:
        gap = (zone[0].y0 - rows[k - 1].y0) / m.pitch
        marker = _NOTE_MARKER.match(zone[0].text) is not None
        if gap > 1.4 and (marker or _tight(zone, m) or zone[0].size < 0.88 * m.em):
            return rows[:k], zone
    return rows, []


def _tight(zone: list[_Row], m: _Metrics) -> bool:
    """Set with tighter leading than the body (notes are)."""
    return len(zone) > 1 and _median([b.y0 - a.y0 for a, b in zip(zone, zone[1:])]) < 0.95 * m.pitch


def _centered(r: _Row, m: _Metrics) -> bool:
    mid = (r.x0 + r.x1) / 2
    return abs(mid - m.center) < 1.0 * m.em and r.x0 - m.left > 2 * m.em and m.right - r.x1 > 2 * m.em


def _heading_level(r: _Row, prev: _Row | None, nxt: _Row | None, m: _Metrics, body_font: str = "") -> int:
    """0, or the heading level of a row: 1 = chapter-like, 2 = a section."""
    text = r.text.strip()
    words = text.split()
    if not words or len(text) > 120:
        return 0
    rel = r.size / m.em
    if text[:1].islower():
        return 0  # a heading never starts in lowercase: "book." ends "…going back to his"
    alone = prev is None or r.y0 - prev.y0 >= 1.4 * m.pitch
    spaced_below = nxt is None or nxt.y0 - r.y0 >= 1.4 * m.pitch
    if textutil.HEADING_RE.match(text) and len(words) <= 10:
        bare = len(words) == 1 and _CHAPTER_HEADING.match(text)  # "Book", "Part" without a number
        return 0 if bare and not (alone and spaced_below) else 1
    short = r.x1 < m.right - 3 * m.em
    opens = bool(re.match(r"[\"'“‘(\[]?[A-Z0-9]", text))
    if (
        rel >= 1.25 and len(words) <= 15 and opens and (alone or spaced_below) and not text.endswith((",", ";", "."))
        and (r.x1 < m.right - 1.5 * m.em or len(words) <= 6)
    ):
        return 1 if rel >= 1.45 else 2
    sentence_break = re.search(r"[.!?]\s+[A-Z]", text)
    if (
        _centered(r, m) and len(words) <= 10 and opens and alone and spaced_below and not sentence_break
        and not text.endswith((",", ";")) and not re.search(r",\s*\d+$", text)
    ):
        return 2
    flush = abs(r.x0 - m.left) < 0.6 * m.em
    if rel >= 1.12 and alone and short and flush and opens and len(words) <= 12 and not text.endswith((".", ",", ";")):
        return 2
    # A section title set in another typeface (a sans among serif text) at the body's size.
    if (
        body_font and r.font and r.font != body_font and rel >= 0.95 and alone and short and opens
        and len(words) <= 12 and not text.endswith((",", ";", "."))
    ):
        return 2
    # A section title alone on its line, set in the body's type, between a finished
    # passage and flush full-width text: "Type four: transitives" / "Maximum activity…".
    after_end = prev is None or not _unfinished(prev.text)
    full_next = (
        nxt is not None and abs(nxt.x0 - m.left) < 0.5 * m.em and nxt.x1 > m.right - 1.5 * m.em
        and nxt.y0 - r.y0 < 1.5 * m.pitch
    )
    if (
        after_end and full_next and flush and opens and r.x1 < m.right - 4 * m.em and len(words) <= 8
        and _unfinished(text) and not text.endswith((",", ";", "-", "—")) and not sentence_break
    ):
        return 2
    letters = re.sub(r"[^A-Za-z]", "", text)
    if len(letters) >= 4 and letters.isupper() and len(words) <= 8 and alone and short and not text.endswith(","):
        return 2
    return 0


def _label(rows: list[_Row], m: _Metrics, indent: float, body_font: str = "") -> None:
    """Label each row of a column: head | orn | quote | attrib | verse | para | cont."""
    n = len(rows)
    for i, r in enumerate(rows):
        prev = rows[i - 1] if i else None
        nxt = rows[i + 1] if i + 1 < n else None
        if _ORNAMENT.match(r.text):
            r.label = "orn"
        elif level := _heading_level(r, prev, nxt, m, body_font):
            r.label, r.level = "head", level
    _missing_drop_cap(rows, m)
    ins = [0.0 if r.beside_cap or r.cap else (r.x0 - m.left) / m.em for r in rows]
    rins = [(m.right - r.x1) / m.em for r in rows]
    qmin = max(1.2, 1.3 * indent) if indent else 1.2
    cand = []
    for i, r in enumerate(rows):
        small = r.size < 0.9 * m.em
        nxt = rows[i + 1] if i + 1 < n else None
        # The line above an attribution: set in, with a shorter line set in deeper below it.
        above_attribution = (
            nxt is not None and not nxt.label and ins[i] >= max(0.8, indent + 0.3)
            and ins[i + 1] >= ins[i] + 0.8 and rins[i + 1] >= 1.5 and nxt.y0 - r.y0 < 1.3 * m.pitch
        )
        c = not r.label and (
            (ins[i] >= qmin and (rins[i] >= 1.0 or ins[i] >= 1.6 * max(indent, 1.0)))
            or (small and ins[i] >= 0.8 and rins[i] >= 0.8 and len(r.toks) >= 2)
            or above_attribution
        )
        cand.append(c)
    deep = max(2.5, 1.8 * indent)
    for i in range(n):
        if cand[i] and not ((i and cand[i - 1]) or (i + 1 < n and cand[i + 1]) or ins[i] >= deep):
            cand[i] = False
    i = 0
    while i < n:
        if not cand[i]:
            i += 1
            continue
        j = i
        while j < n and cand[j]:
            j += 1
        _label_quotes(rows[i:j], ins[i:j], rins[i:j], m)
        i = j
    _label_rest(rows, m, ins, indent)
    _join_unfinished(rows, m)


def _label_rest(rows: list[_Row], m: _Metrics, ins: list[float], indent: float) -> None:
    """Paragraph starts and continuation lines among the rows not labelled yet."""
    for i, r in enumerate(rows):
        if r.label:
            continue
        prev = rows[i - 1] if i else None
        if r.cap:
            r.label = "para"
        elif r.beside_cap:
            r.label = "cont"
        elif ins[i] >= 0.5:
            r.label = "para"
        elif prev is not None and (
            prev.label in ("head", "orn", "quote", "attrib", "verse")
            or r.y0 - prev.y0 > 1.6 * m.pitch
            or (not indent and m.right - prev.x1 > 2 * m.em and prev.text.endswith(textutil.TERMINAL))
        ):
            r.label = "para"
        else:
            r.label = "cont"


# The last word of a line that can't end a sentence ("…and for" / "Paul Edward…").
_OPEN_WORD = re.compile(
    r"\b(?:and|or|nor|but|for|of|the|a|an|to|in|on|at|by|from|with|as|than|that|whose|which|who|my|his|her|"
    r"its|our|their|your|this|these|those|is|was|were|are|be|ever)$",
    re.I,
)


def _join_unfinished(rows: list[_Row], m: _Metrics) -> None:
    """A line that doesn't finish its sentence, followed at a normal line gap by one
    that goes on with it, stays in the same block: a centred dedication or epigraph
    ("…shall be the" / "education of our heroes.") must not be cut into a paragraph
    and a quote, or the narrator pauses mid-sentence."""
    for i in range(1, len(rows)):
        a, b = rows[i - 1], rows[i]
        if a.label in ("head", "orn") or b.label in ("head", "orn") or b.cap:
            continue
        if a.label == "attrib":  # an attribution runs over: "Must Converge," / "218"
            if a.text.rstrip().endswith(",") and abs(b.x0 - a.x0) < 0.6 * m.em and b.y0 - a.y0 <= 1.4 * m.pitch:
                b.label, b.qnew = "attrib", False
            continue
        if not _unfinished(a.text) or b.y0 - a.y0 > 1.35 * m.pitch:
            continue
        lower = b.text[:1].islower()
        centred = all(abs((r.x0 + r.x1) / 2 - m.center) < 1.5 * m.em for r in (a, b))
        if not (lower or (centred and _OPEN_WORD.search(a.text))):
            continue
        if a.label in ("para", "cont"):
            b.label, b.qnew = "cont", False  # a set-in last line of the paragraph's sentence
        elif b.label in ("quote", "verse", "attrib"):
            b.label, b.qnew = a.label, False  # the quotation goes on


def _unfinished(text: str) -> bool:
    """Does a line stop mid-sentence? (Not after an attribution's page number:
    "Pincher Martin, 182".)"""
    text = text.rstrip()
    return not text.endswith(textutil.TERMINAL) and not re.search(r",\s*\d+$", text)


def _missing_drop_cap(rows: list[_Row], m: _Metrics) -> None:
    """Two or three full-width lines set in alike right under a heading, then flush text:
    the lines beside a drop capital drawn as a picture ("N the department…"), not a quote."""
    n = len(rows)
    for i, r in enumerate(rows):
        if r.label != "head":
            continue
        group = []
        j = i + 1
        while j < n and len(group) < 4 and not rows[j].label and not rows[j].cap:
            inset = (rows[j].x0 - m.left) / m.em
            if not 1.0 <= inset <= 6 or m.right - rows[j].x1 > 1.0 * m.em:
                break
            if group and abs(rows[j].x0 - rows[group[0]].x0) > 1.0 * m.em:
                break
            group.append(j)
            j += 1
        if 2 <= len(group) <= 3 and j < n and abs(rows[j].x0 - m.left) < 0.4 * m.em:
            rows[group[0]].cap = True
            for g in group:
                rows[g].beside_cap = True


def _label_quotes(run: list[_Row], ins: list[float], rins: list[float], m: _Metrics) -> None:
    """A run of set-in rows: quotes, the attributions set in deeper below them, and
    where one quoted passage ends and the next begins."""
    n = len(run)
    gap = [k > 0 and run[k].y0 - run[k - 1].y0 > 1.3 * m.pitch for k in range(n)]
    right = max(r.x1 for r in run)
    labels = ["quote"] * n
    i = 1
    while i < n:
        q = ins[i - 1]  # the quoted line above
        dashed = run[i].text.startswith(("—", "–", "―"))
        if labels[i - 1] == "quote" and not gap[i] and (ins[i] >= q + 0.8 or dashed):
            j = i + 1
            while j < n and not gap[j] and ins[j] >= q + 0.8:
                j += 1
            # Attribution: closes the passage, or is short and follows a finished sentence;
            # otherwise it is the indented first line of the quote's next paragraph.
            closes = j == n or gap[j]
            short = all(right - run[k].x1 > 2 * m.em for k in range(i, j)) and run[i - 1].text.endswith(textutil.TERMINAL)
            if dashed or closes or short:
                for k in range(i, j):
                    labels[k] = "attrib"
                i = j + 1
                continue
        i += 1
    for k, r in enumerate(run):
        r.label = labels[k]
        r.qnew = k > 0 and (labels[k] != labels[k - 1] or gap[k] or (labels[k] == "quote" and ins[k] >= ins[k - 1] + 0.8))
    # A quoted poem: a passage of short lines, each starting with a capital.
    k = 0
    while k < n:
        j = k + 1
        while j < n and labels[j] == "quote" and not run[j].qnew:
            j += 1
        block = list(range(k, j)) if labels[k] == "quote" else []
        if len(block) >= 2:
            lines = [run[q] for q in block]
            short = sum(rins[q] >= 2.0 for q in block) >= 0.7 * len(block)
            upper = sum(r.text[:1].isupper() for r in lines) >= 0.8 * len(lines)
            if short and upper and not any(r.text.endswith("-") for r in lines):
                for r in lines:
                    r.label = "verse"
        k = j


# ---------------------------------------------------------------------------
# Front and back matter


def _assign_roles(pages: list[_Page], title: str, author: str, body_em: float, outline: list) -> str:
    """Block roles from page-level signals: front matter before the book's first
    chapter-like heading, back matter from the index on (see book.SKIPPED_ROLES).
    Returns the author's name from the title page when only it gives one."""
    n = len(pages)
    if not n:
        return ""
    begin = _body_start(pages, outline)
    start = begin[0] if begin else None
    front = pages[:start] if start is not None else pages[: min(15, n // 4)]
    # The lines above the opening heading on its own page: the end of the contents,
    # or a title, author and dedication set on the same page as Chapter 1.
    lead: list[_Row] = []
    if begin and begin[1] is not None:
        rows = _all_rows(pages[start])
        lead = rows[: rows.index(begin[1])]
    title_words = {w.lower() for w in textutil.WORD_RE.findall(f"{title} {author}") if len(w) >= 2}
    roles: dict[int, str] = {}
    for k, p in enumerate(front):
        roles[k] = _front_role(p, title_words, body_em)
    for k in range(1, len(front)):  # a contents or acknowledgements page that runs on
        prev, p = roles.get(k - 1, ""), roles.get(k, "")
        if not p and prev == "contents" and _numbered_lines(_all_rows(front[k]), 0.5):
            roles[k] = "contents"
        elif not p and prev == "acknowledgments" and front[k].frames and not any(r.label == "head" for r in _all_rows(front[k])):
            first = _all_rows(front[k])[0]
            if first.label == "cont":
                roles[k] = "acknowledgments"
    titles = [k for k, r in roles.items() if r == "title"]
    content = [k for k in range(len(front)) if _all_rows(front[k])]
    cover = None
    if content:
        first = content[0]
        if (titles and titles[0] == first and len(titles) >= 2) or (front[first].picture and front[first].words() <= 40):
            cover = first
            roles[first] = "cover"
            titles = [k for k in titles if k != first]
    title_page = titles[0] if titles else cover
    if title_page is not None:
        for k in content:
            if k < title_page and k != cover and not roles.get(k):
                roles[k] = "blurb"
    for k, p in enumerate(front):
        role = roles.get(k, "")
        rows = _all_rows(p)
        if role in ("title", "cover"):
            for r in rows:
                r.role = "frontmatter" if role == "title" else "cover"
        elif role:
            for r in rows + p.notes:
                r.role = role
    if lead and front and roles.get(len(front) - 1) == "contents":
        for r in lead:
            r.role = "contents"
    if title_page is not None:
        pages_with_title = [k for k in [title_page, *titles, cover] if k is not None]
        _announce([_all_rows(front[k]) for k in dict.fromkeys(pages_with_title)], title)
    byline = _title_lines([*(_all_rows(p) for p in front), lead], title, author)
    _section_roles(pages, len(front) if start is not None else 0)
    return byline


_PUBLISHER = re.compile(r"\b(?:press|books|publish\w*|editions?|knopf|penguin|vintage|random house|harper\w*|scribner|ltd|inc|llc)\b", re.I)


def _title_lines(groups: list[list[_Row]], title: str, author: str) -> str:
    """Title, author and publisher lines on a front page that holds more (a converted
    ebook's first page: "Donna Tartt", "The Secret History", then the dedication).
    The title (with its subtitle) is announced once (TITLE_ROLE, unless a title page
    already is); every other copy, the author line and the publisher are
    "frontmatter". Returns the author's line when the metadata names no author."""
    announced = any(r.role == TITLE_ROLE for rows in groups for r in rows)
    if not title and not announced:
        # No title in the metadata: the first heading before the book begins is it.
        first = next((r for rows in groups for r in rows if not r.role and r.label == "head" and not _chapter_like(r.text)), None)
        title = first.text if first is not None else ""
    keys = _title_keys(title)
    author_key = letters_key(author)
    if not keys:
        return ""
    byline = ""
    for rows in groups:
        marked = []
        for k, r in enumerate(rows):
            if r.role or r.label != "head":
                continue
            key = letters_key(r.text)
            if any(_similar(key, t) >= 0.85 for t in keys):
                r.role = "frontmatter" if announced else TITLE_ROLE
                if not announced:
                    marked += _mark_subtitle(rows, k, title)
                announced = True
                marked.append(k)
            elif len(author_key) >= 4 and _similar(key, author_key) >= 0.85:
                r.role = "frontmatter"
                marked.append(k)
        for k in sorted(set(marked)):  # the byline and the publisher's line right beside them
            for j in (k - 1, k + 1):
                if not 0 <= j < len(rows) or rows[j].role:
                    continue
                if len(rows[j].toks) <= 6 and _PUBLISHER.search(rows[j].text):
                    rows[j].role = "frontmatter"
                elif not author and not byline and j == k + 1 and _looks_like_name(rows[j].text):
                    rows[j].role = "frontmatter"
                    byline = " ".join(rows[j].text.split())
    return byline


def _title_keys(title: str) -> list[str]:
    return [k for k in (letters_key(title), letters_key(title.split(":")[0])) if len(k) >= 4]


def _mark_subtitle(rows: list[_Row], k: int, title: str) -> list[int]:
    """The lines right after the title line that carry the rest of the title
    ("ARTFUL SENTENCES:" / "Syntax as Style") join its TITLE_ROLE block."""
    full = letters_key(title)
    marked = []
    for j in range(k + 1, min(len(rows), k + 3)):
        key = letters_key(rows[j].text)
        if len(key) < 3 or rows[j].page != rows[k].page or key not in full or rows[j].role not in ("", "frontmatter", "cover"):
            break
        rows[j].role = TITLE_ROLE
        marked.append(j)
    return marked


_NAME = re.compile(
    r"^(?:[A-Z][\w’'.-]*)(?:,?\s+(?:[A-Z][\w’'.-]*|de|van|von|da|du|di|la|le|Jr\.?|Sr\.?)){1,4}$"
)


def _looks_like_name(text: str) -> bool:
    """'Shirley Jackson', 'William Strunk, Jr.': an author's byline under a title."""
    text = " ".join(text.split())
    return (
        len(text) <= 40 and not any(c.isdigit() for c in text) and bool(_NAME.match(text))
        and not textutil.HEADING_RE.match(text) and text.split()[0] not in ("For", "To", "In", "With", "Dedicated")
    )


def _announce(pages: list[list[_Row]], title: str) -> None:
    """The book announces itself once: on the title-page (or cover) heading closest to
    the known title, the first title page winning a near tie; with no known title, on
    the first title page's first heading."""
    keys = _title_keys(title)
    best, score = None, -1.0
    for rows in pages:  # the first title page first
        for r in [r for r in rows if r.label == "head"] or rows[:1]:
            s = max((_similar(letters_key(r.text), k) for k in keys), default=0.0)
            if best is None or s > score + 0.05:
                best, score = r, s
            if not keys:
                break
        if not keys and best is not None:
            break
    if best is not None:
        best.role = TITLE_ROLE
        rows = next(rows for rows in pages if best in rows)
        _mark_subtitle(rows, rows.index(best), title)


def _all_rows(p: _Page) -> list[_Row]:
    return [r for rows, _ in p.frames for r in rows]


def _body_start(pages: list[_Page], outline: list) -> tuple[int, _Row | None] | None:
    """Where the book itself begins: (position in pages, the heading row that opens it):
    its first chapter-like heading, or the first outline entry that isn't front matter."""
    found = None
    for k, p in enumerate(pages):
        row = next((r for r in _all_rows(p) if r.label == "head" and _BODY_START.match(r.text)), None)
        if row is not None:
            found = (k, row)
            break
    skip = re.compile(r"cover|title|copyright|contents|dedication|acknowledg|also by|praise|about|epigraph|half", re.I)
    for entry in outline:
        if len(entry) >= 3 and entry[2] > 0 and not skip.search(entry[1]):
            target = entry[2] - 1
            k = next((k for k, p in enumerate(pages) if p.index >= target), None)
            if k is not None and (found is None or k < found[0]):
                key = letters_key(entry[1])
                heads = [r for r in _all_rows(pages[k]) if r.label == "head"]
                row = max(heads, key=lambda r: _similar(letters_key(r.text), key), default=None)
                if row is not None and _similar(letters_key(row.text), key) < 0.6:
                    row = None
                found = (k, row)
            break
    return found


def _front_role(p: _Page, title_words: set[str], body_em: float) -> str:
    rows = _all_rows(p)
    if not rows:
        return ""
    text = " ".join(r.text for r in rows)
    nwords = len(text.split())
    heads = [r.text for r in rows if r.label == "head"]
    first = heads[0] if heads else rows[0].text
    if any(textutil.looks_like_boilerplate(r.text) for r in rows) and nwords <= 100:
        return "boilerplate"
    if _CONTENTS_HEADING.match(first) or _numbered_lines(rows, 0.6):
        return "contents"
    if _ACK_HEADING.match(first):
        return "acknowledgments"
    if _PRAISE.match(rows[0].text):
        return "blurb"
    if _ALSO_BY.match(rows[0].text):
        return "frontmatter"
    if _title_like(rows, nwords, title_words, body_em):
        return "title"
    hits = len({m.group().lower() for m in _COPYRIGHT.finditer(text)})
    if hits >= 2 or (hits and nwords <= 150):
        return "copyright"
    return ""


def _title_like(rows: list[_Row], nwords: int, title_words: set[str], body_em: float) -> bool:
    """A page made of the title, subtitle and author (a title page or a cover)."""
    if nwords > 40:
        return False
    words = [w for r in rows for w in textutil.WORD_RE.findall(r.text)]
    letters = sum(len(w) for w in words)
    if not letters:
        return False
    if title_words:
        titled = sum(len(w) for w in words if w.lower() in title_words)
        return titled >= 0.5 * letters
    big = sum(len(w) for r in rows if r.size >= 1.5 * body_em for w in textutil.WORD_RE.findall(r.text))
    return big >= 0.6 * letters and nwords <= 25


def _numbered_lines(rows: list[_Row], share: float) -> bool:
    """Do most lines end in a page number (a contents page)?"""
    lines = [r for r in rows if r.label != "head"]
    if len(lines) < 4:
        return False
    ending = sum(1 for r in lines if _ENTRY_PAGE.search(r.text) and len(r.toks) >= 2)
    return ending >= share * len(lines)


def _section_roles(pages: list[_Page], front_end: int) -> None:
    """Sections that are skipped wherever they are: acknowledgements (until the next
    heading at its level), and an index or bibliography in the back half (to the end,
    except a colophon or back-cover copy after it)."""
    n = len(pages)
    section, level = "", 0
    index_from = None
    for k, p in enumerate(pages):
        rows = _all_rows(p)
        page_role = next((r.role for r in rows if r.role), "")
        if k < front_end and page_role:
            section = ""
            continue
        if index_from is not None:
            back = _back_role(p, k, n)
            if back:
                for r in rows + p.notes:
                    r.role = back
                continue
        for r in rows:
            if r.label == "head":
                if _ACK_HEADING.match(r.text):
                    section, level = "acknowledgments", r.level
                elif k >= n // 2 and _INDEX_HEADING.match(r.text):
                    section, level = "index", r.level
                    index_from = k
                elif k >= n // 2 and (_CONTENTS_HEADING.match(r.text) or _OUTLINE_PAGE.match(r.text)):
                    section, level = "contents", r.level  # a contents list printed at the back
                    index_from = k
                elif k >= n // 2 and _ABOUT_AUTHOR.match(r.text):
                    section, level = "blurb", r.level  # the author's bio, like back-cover copy
                elif section and section not in ("index", "contents") and r.level <= level:
                    section = ""
            if section and not r.role:
                r.role = section
        for r in p.notes:
            if section and not r.role:
                r.role = section
    if index_from is None:  # no index: only the last pages' colophon or back cover
        for k in range(max(front_end, n - 3), n):
            back = _back_role(pages[k], k, n, strict=True)
            for r in _all_rows(pages[k]) + pages[k].notes:
                if back and not r.role:
                    r.role = back


def _back_role(p: _Page, k: int, n: int, strict: bool = False) -> str:
    """After the index: a colophon ("Composed in…, Printed by…") or back-cover copy."""
    rows = _all_rows(p)
    if not rows:
        return ""
    text = " ".join(r.text for r in rows)
    words = len(text.split())
    if words <= 150 and re.search(r"printed (?:and bound )?(?:in|by)|composed in|typeset (?:in|by)|copyright|©", text, re.I):
        return "copyright"
    if _ISBN.search(text) and k >= n - 2:
        return "blurb"
    if strict:
        return ""
    ending = sum(1 for r in rows if re.search(r"\d[\d,–\-\s]*$", r.text))
    if words >= 40 and ending < 0.25 * len(rows):
        return "blurb"
    return ""


# ---------------------------------------------------------------------------
# Building blocks


@dataclass
class _Draft:
    kind: str
    rows: list[_Row]
    level: int = 0
    align: str = ""
    role: str = ""
    open: bool = True  # may still continue on the next line, column or page


def _build(pages: list[_Page]) -> list[_Draft]:
    """Rows in reading order → draft blocks: paragraphs continued across pages (notes
    wait until the paragraph they interrupt is finished), quotes, headings."""
    drafts: list[_Draft] = []
    pending: list[_Draft] = []
    current: _Draft | None = None

    def start(kind: str, row: _Row, **kw) -> _Draft:
        nonlocal current
        drafts.extend(pending)
        pending.clear()
        current = _Draft(kind, [row], role=row.role, **kw)
        drafts.append(current)
        return current

    for p in pages:
        for rows, m in p.frames:
            for row in rows:
                label = row.label
                if row.role in ("contents", "index", TITLE_ROLE, "frontmatter", "cover"):
                    # Lists and title lines: one block per line (each contents line is a link).
                    joined = current is not None and current.role == row.role and (
                        (current.kind == "heading" and label == "head" and _wraps(current, row))
                        or (row.role == TITLE_ROLE and current.rows[-1].page == row.page)  # title + subtitle
                    )
                    title_re = _CONTENTS_HEADING if row.role == "contents" else _INDEX_HEADING
                    if row.role in ("contents", "index") and label == "head" and not title_re.match(row.text):
                        label = "para"  # an entry ("Acknowledgments 5"), not the page's heading
                    if joined:
                        current.rows.append(row)
                    else:
                        start("heading" if label == "head" else "paragraph", row, level=row.level or 0).open = False
                    continue
                if label == "head":
                    if current is not None and current.kind == "heading" and current.role == row.role and _wraps(current, row):
                        current.rows.append(row)
                        continue
                    level = row.level
                    if current is not None and current.kind == "heading" and current.rows[0].page == row.page and _designator(current):
                        level = min(level, current.level)  # "CHAPTER ONE" / "Short Sentences": one chapter title
                    start("heading", row, level=level, align="center" if _centered(row, m) else "").open = False
                elif label == "orn":
                    start("separator", row).open = False
                elif label in ("quote", "attrib", "verse"):
                    kind = "verse" if label == "verse" else "quote"
                    align = "right" if label == "attrib" else ""
                    same = (
                        current is not None and current.kind == kind and current.align == align
                        and current.role == row.role and current.open and not row.qnew
                        and row.page - current.rows[-1].page <= 1
                    )
                    if same:
                        current.rows.append(row)
                    else:
                        start(kind, row, align=align)
                elif (label == "para" and not _goes_on(current, row)) or not (
                    current is not None and current.kind == "paragraph" and current.open and current.role == row.role
                    and row.page - current.rows[-1].page <= 1  # never across a blank page
                ):
                    start("paragraph", row)
                else:
                    current.rows.append(row)
                if current.kind == "paragraph":
                    current.open = not (m.right - row.x1 > 2 * m.em and not _unfinished(row.text))
        note: _Draft | None = None
        for row in p.notes:
            if note is None or _NOTE_MARKER.match(row.text):
                note = _Draft("note", [row], role=row.role, open=False)
                pending.append(note)
            else:
                note.rows.append(row)
    drafts.extend(pending)
    return drafts


def _goes_on(current: _Draft | None, row: _Row) -> bool:
    """Does an indented line starting in lowercase continue an unfinished paragraph?"""
    return (
        current is not None and current.kind == "paragraph" and row.text[:1].islower()
        and _unfinished(current.rows[-1].text)
    )


def _designator(d: _Draft) -> bool:
    return _numbers_a_chapter(" ".join(r.text for r in d.rows))


def _numbers_a_chapter(text: str) -> bool:
    """A heading that only numbers a chapter ('CHAPTER ONE', 'Part 2', 'IV'): its title follows."""
    if not textutil.HEADING_RE.match(text):
        return False
    return (bool(_CHAPTER_HEADING.match(text)) and len(text.split()) <= 3) or textutil.roman_to_int(text) is not None


def _wraps(d: _Draft, row: _Row) -> bool:
    """Is row the second line of the heading d ('Bibliography—Index of' / 'Authors &
    Editions Quoted'), rather than a heading of its own ('CHAPTER ONE' / 'Short Sentences')?"""
    last = d.rows[-1]
    if row.page != last.page or not 0.85 < row.size / last.size < 1.18:
        return False
    if row.y0 - last.y0 > 1.8 * max(last.height, row.height):
        return False
    a, b = last.text, row.text
    if _numbers_a_chapter(a) or textutil.HEADING_RE.match(b):
        return False
    caps = re.sub(r"[^A-Za-z]", "", a + b).isupper()
    return b[:1].islower() or bool(_OPEN_END.search(a)) or (caps and not a.endswith((":", ".")))


def _block(d: _Draft, vocab: textutil.Vocab) -> Block:
    """Assemble a draft's tokens into a Block: text, word boxes and style runs line up."""
    toks: list[_Tok] = []
    line_starts: set[int] = set()
    for r in d.rows:
        if toks:
            line_starts.add(len(toks))
        toks += r.toks
    parts: list[str] = []
    words = []
    styles: list[tuple[int, int, str]] = []
    pos = 0
    for i, t in enumerate(toks):
        text, sep = t.text, ""
        if i + 1 < len(toks):
            if i + 1 in line_starts:
                if d.kind == "verse":
                    sep = "\n"
                else:
                    mode = "drop-hyphen" if t.soft else textutil.join_mode(text, toks[i + 1].text, vocab)
                    if mode == "drop-hyphen" and text.endswith(("-", "­")) and len(text) > 1:
                        text = text[:-1]
                    sep = " " if mode == "space" else ""
            else:
                sep = "" if t.glue else " "
        start = pos
        parts.append(text)
        pos += len(text)
        words.append((t.page, round(t.x0, 1), round(t.y0, 1), round(t.x1, 1), round(t.y1, 1), start, pos))
        for flag, style in ((t.italic, "i"), (t.bold, "b"), (t.sup, "sup")):
            if not flag:
                continue
            if styles and styles[-1][2] == style and styles[-1][1] >= start - 1:
                styles[-1] = (styles[-1][0], pos, style)
            else:
                styles.append((start, pos, style))
        parts.append(sep)
        pos += len(sep)
    text = "".join(parts)
    return Block(
        kind=d.kind,
        text=text,
        level=d.level if d.kind == "heading" else 0,
        align=d.align,
        styles=styles,
        page=d.rows[0].page,
        words=words,
        role=d.role,
    )


# ---------------------------------------------------------------------------
# Table of contents


def _key(text: str) -> str:
    return letters_key(text)


def _groups(blocks: list[Block]) -> list[tuple[int, list[int]]]:
    """Runs of consecutive heading blocks on one page ('CHAPTER ONE' + 'Short Sentences'):
    (first block, all of them)."""
    out: list[tuple[int, list[int]]] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind != "heading":
            i += 1
            continue
        group = [i]
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if (
            nxt is not None and nxt.kind == "heading" and nxt.page == b.page and nxt.role == b.role
            and _numbers_a_chapter(b.text) and not _chapter_like(nxt.text)
        ):
            group.append(i + 1)
        out.append((i, group))
        i = group[-1] + 1
    return out


def _group_title(blocks: list[Block], group: list[int]) -> str:
    """'CHAPTER ONE' + 'Short Sentences' -> 'Chapter One: Short Sentences'."""
    parts = []
    for i in group:
        t = " ".join(blocks[i].text.split()).strip()
        letters = re.sub(r"[^A-Za-z]", "", t)
        if letters.isupper() or letters.islower():
            t = textutil.title_case(t.lower() if letters.isupper() else t)
        parts.append(t.rstrip(":.").strip())
    return ": ".join(p for p in parts if p)


_TOC_ROLES = ("", "acknowledgments", "contents", "index")


def _heading_toc(blocks: list[Block]) -> list[TocEntry]:
    """The TOC from detected headings: one entry per run of headings."""
    toc: list[TocEntry] = []
    for first, group in _groups(blocks):
        if blocks[first].role not in _TOC_ROLES:
            continue
        level = min(blocks[i].level or 1 for i in group)
        if level > 2:
            continue
        entry = TocEntry(_group_title(blocks, group), first, level)
        if toc and toc[-1].title == entry.title and blocks[first].page - blocks[toc[-1].block].page <= 3:
            toc[-1] = entry  # a part-title page, then the story's own first page: go to the story
        else:
            toc.append(entry)
    # Section numbers inside chapters ("2", "3") don't belong beside "Chapter 1", "Chapter 2".
    numerals = [e for e in toc if re.fullmatch(r"\d{1,3}|[IVXLC]{1,7}", e.title)]
    if numerals and len(numerals) < len(toc):
        toc = [e for e in toc if e not in numerals]
    return toc


def _outline_toc(outline: list, blocks: list[Block]) -> list[TocEntry]:
    """The PDF's own outline, each entry mapped to the heading it names on its page
    (else the first block starting on that page)."""
    if not outline or not blocks:
        return []
    toc = []
    for entry in outline:
        level, title, page = entry[0], " ".join(str(entry[1]).split()), entry[2] - 1
        if page < 0 or not title:
            continue
        target = _block_on_page(blocks, page, _key(title))
        if target is not None:
            toc.append(TocEntry(title, target, max(1, level)))
    toc.sort(key=lambda e: e.block)
    # Entries for front matter this PDF lacks all land on its first block: keep the
    # last of them, the one that names it.
    first = toc[0].block if toc else 0
    return [e for k, e in enumerate(toc) if e.block != first or k + 1 == len(toc) or toc[k + 1].block != first]


def _with_chapters(outline: list[TocEntry], detected: list[TocEntry], blocks: list[Block]) -> list[TocEntry]:
    """A coarse outline (Prologue, Book 1, Book 2, Epilogue) gains the book's own
    "Chapter N" headings, one level below the outline entry they fall under."""
    targets = {e.block for e in outline}
    chapters = [
        e for e in detected
        if e.block not in targets and _chapter_like(text := blocks[e.block].text)
        and (len(text.split()) >= 2 or not _CHAPTER_HEADING.match(text))
    ]
    if not outline or len(chapters) <= len(outline):
        return outline
    merged = list(outline)
    for c in chapters:
        parent = next((e for e in reversed(outline) if e.block <= c.block), None)
        merged.append(TocEntry(c.title, c.block, (parent.level if parent else 1) + 1))
    merged.sort(key=lambda e: (e.block, e.level))
    return merged


def _block_on_page(blocks: list[Block], page: int, key: str) -> int | None:
    on_page = [i for i, b in enumerate(blocks) if b.page == page]
    best, score = None, 0.0
    for i in on_page:
        if blocks[i].kind == "heading":
            s = _similar(key, _key(blocks[i].text))
            if i + 1 < len(blocks) and blocks[i + 1].kind == "heading" and blocks[i + 1].page == page:
                # "Introduction" / "Haneke's Anachronism" for "Introduction: Haneke's Anachronism"
                s = max(s, _similar(key, _key(blocks[i].text + blocks[i + 1].text)))
            if s > score:
                best, score = i, s
    if best is not None and score >= 0.6:
        return _group_start(blocks, best)
    later = [i for i, b in enumerate(blocks) if b.page >= page]
    return later[0] if later else None


def _group_start(blocks: list[Block], index: int) -> int:
    for first, group in _groups(blocks):
        if index in group:
            return first
    return index


def _contents_links(blocks: list[Block], pages: list[_Page]) -> list[TocEntry]:
    """Link each line of the book's printed contents to the chapter it names
    (Block.link), and return the TOC it describes."""
    entries = [i for i, b in enumerate(blocks) if b.role == "contents" and b.kind != "heading"]
    if not entries:
        return []
    offset = _folio_offset(pages)
    groups = _groups(blocks)
    candidates = [(first, group) for first, group in groups if blocks[first].role not in ("contents", TITLE_ROLE, "frontmatter", "cover", "blurb")]
    linked: list[tuple[int, int]] = []
    for i in entries:
        title, printed = _entry_parts(blocks[i].text)
        key = _key(title)
        if len(key) < 3:
            continue
        pool = candidates
        if printed is not None and offset is not None:
            near = [c for c in candidates if abs(blocks[c[0]].page - (printed + offset)) <= 1]
            pool = near or pool
        best, score = None, 0.0
        for first, group in pool:
            for g in group:
                hk = _key(blocks[g].text)
                s = _similar(key, hk)
                if (len(key) >= 6 and key in hk) or (len(hk) >= max(5, 0.4 * len(key)) and hk in key):
                    s = max(s, 0.9 if key in hk else 0.8)
                if s > score:
                    best, score = first, s
        if (best is None or score < 0.75) and printed is not None and offset is not None:
            best = next((k for k, b in enumerate(blocks) if b.page >= printed + offset and b.role not in SKIPPED_ROLES), None)
            score = 1.0 if best is not None else 0.0
        if best is not None and score >= 0.75:
            blocks[i].link = best
            linked.append((i, best))
    if len(linked) < 2:
        return []
    toc = []
    seen = set()
    for i, target in linked:
        if target in seen:
            continue
        seen.add(target)
        group = next((g for f, g in groups if f == target), [target])
        title = _entry_parts(blocks[i].text)[0]
        named = blocks[target].kind == "heading" and any(_similar(_key(title), _key(blocks[g].text)) >= 0.75 or _key(title) in _key(blocks[g].text) for g in group)
        if named:
            title = _group_title(blocks, group)
        elif re.sub(r"[^A-Za-z]", "", title).isupper():
            title = textutil.title_case(title.lower())
        toc.append(TocEntry(title, target, 1))
    toc.sort(key=lambda e: e.block)
    return toc


def _entry_parts(text: str) -> tuple[str, int | None]:
    """'1 Short Sentences ........ 9' -> ('Short Sentences', 9)."""
    text = " ".join(text.split())
    printed = None
    if m := _ENTRY_PAGE.search(text):
        value = _folio(m.group(1))
        if value is not None and m.start() > 0:
            printed = value if value > 0 else None
            text = text[: m.start()]
    # A leading chapter number: "1 ", "IV. ", "Chapter 3: " (not the pronoun in "I Remember").
    if (m := _ENTRY_NUMBER.match(text)) and len(text.split()) > 1:
        number, sep = m.group(1), m.group(2)
        if number.isdigit() or (number.isupper() and textutil.roman_to_int(number) and (len(number) > 1 or sep.strip())):
            text = text[m.end() :]
    return text.strip(" .·…_"), printed


def _folio_offset(pages: list[_Page]) -> int | None:
    """PDF page index minus printed page number, from the running heads (the most common)."""
    diffs = Counter(p.index - p.folio for p in pages if p.folio)
    return diffs.most_common(1)[0][0] if diffs else None


# ---------------------------------------------------------------------------
# Title, author, text box


def _metadata(doc: pymupdf.Document, path: Path) -> tuple[str, str]:
    meta = doc.metadata or {}
    return _clean_title(meta.get("title") or ""), _clean_author(meta.get("author") or "")


def _clean_title(title: str) -> str:
    """'Artful sentences : syntax as style' -> 'Artful Sentences: Syntax as Style'; file
    names and placeholders ('Microsoft Word - draft.docx', 'untitled') are no title."""
    title = " ".join(title.split())
    if not title or re.search(r"\.(?:docx?|pdf|indd|qxd|rtf|tex|txt)$|^microsoft word\b|^untitled\b|^\W+$", title, re.I):
        return ""
    title = re.sub(r"\s+([:;,])", r"\1", title)
    long_words = [w for w in re.findall(r"[A-Za-z]+", title) if len(w) > 3]
    capitalised = sum(w[0].isupper() for w in long_words)
    if title.isupper() or (len(long_words) >= 3 and capitalised <= 1):
        title = textutil.title_case(title.lower() if title.isupper() else title)
    return title


def _clean_author(author: str) -> str:
    """'Tufte, Virginia' -> 'Virginia Tufte'; 'Gogol, Nikolai, 1809-1852' -> 'Nikolai Gogol'."""
    names: list[str] = []
    for part in re.split(r"\s*;\s*|\s+&\s+|\s+and\s+", " ".join(author.split())):
        part = re.sub(r",?\s*(?:\d{3,4}\s*-\s*(?:\d{3,4})?|\d{3,4})\.?\s*$", "", part).strip(" ,")
        if part.count(",") == 1:
            last, first = (s.strip() for s in part.split(","))
            part = f"{first} {last}".strip()
        if not part or len(part.split()) > 4 or _PUBLISHER.search(part) or re.search(r"\bby\b", part, re.I):
            continue  # a shop's keyword stuffing: "The Elements of Style by William Strunk"
        if any(part in n or n in part for n in names):
            continue  # "William Strunk" and "William Strunk Junior"
        names.append(part)
    return " and ".join(names[:3])


def _shorter_title(title: str, blocks: list[Block]) -> str:
    """A shop's keyword-stuffed metadata title ("THE ELEMENTS OF STYLE (UPDATED 2011
    EDITION) The All-Time Bestselling…") gives way to the title page's own words."""
    if len(title.split()) <= 12:
        return title
    announced = next((b for b in blocks if b.role == TITLE_ROLE), None)
    if announced is None or letters_key(announced.text) not in letters_key(title):
        return title
    text = " ".join(announced.text.split()).rstrip(":")
    letters = re.sub(r"[^A-Za-z]", "", text)
    return textutil.title_case(text.lower()) if letters.isupper() else text


def _stem_title(path: Path) -> str:
    return " ".join(re.sub(r"[_]+", " ", path.stem).split())


def _title_from_blocks(blocks: list[Block]) -> str:
    """The first heading of the title page, or failing that the book's first heading."""
    for b in blocks:
        if b.role == TITLE_ROLE or (b.kind == "heading" and b.role not in ("contents", "boilerplate", "copyright")):
            text = " ".join(b.text.split()).rstrip(":")
            letters = re.sub(r"[^A-Za-z]", "", text)
            return textutil.title_case(text.lower()) if letters.isupper() else text
    return ""


def _text_box(pages: list[_Page]) -> tuple[float, float, float, float] | None:
    """The box that holds the text of a typical page: each page's extent (headings and
    notes included, front and back matter not), then the 5th percentile of the left and
    top edges and the 95th of the right and bottom ones across pages, so one odd page
    (a wide table, a picture) doesn't stretch it."""
    extents = []
    for p in pages:
        rows = [r for rows, _ in p.frames for r in rows if r.role in ("", TITLE_ROLE)] + [r for r in p.notes if not r.role]
        if rows:
            extents.append((min(r.x0 for r in rows), min(r.y0 for r in rows), max(r.x1 for r in rows), max(r.y1 for r in rows)))
    if not extents:
        return None

    def pct(values: list[float], q: float) -> float:
        values = sorted(values)
        return values[min(len(values) - 1, max(0, round(q * (len(values) - 1))))]

    x0s, y0s, x1s, y1s = zip(*extents)
    return (round(pct(x0s, 0.05), 1), round(pct(y0s, 0.05), 1), round(pct(x1s, 0.95), 1), round(pct(y1s, 0.95), 1))
