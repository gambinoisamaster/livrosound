"""The reflowable reader: LivroSound's own layout of any book, and the original
layout of EPUB, DOCX and TXT books.

ReflowView is a read-only QTextEdit that holds the whole book as one
QTextDocument. Qt lays a big document out lazily, so Anna Karenina (2 M chars)
opens in about 0.3 s in the LivroSound layout and about 2 s in the original one
(Qt's HTML import is most of that), and scrolls freely. Two layouts:

- "livrosound" — built block by block with a QTextCursor in one edit block, set
  like Apple Books: Iowan Old Style, about 150% line height, centered chapter
  headings, a first-line indent only on a paragraph that follows a paragraph
  (never after a heading, scene break, image, epigraph or quote), quotes
  indented on both sides, verse with its own line breaks, notes small and muted,
  scene breaks as a centered "⁂", images scaled to the column, inline italic,
  bold, Iowan's own small caps and superscript. Block.text goes in char for char
  (verse "\\n" becomes a line separator, also one char), so a block's char map is
  its start plus each char's offset.
- "original" — Book.original_html rendered by Qt. Each block's start comes from
  its <a name="ls-b{i}"></a> anchor and its chars are aligned inside
  [start_i, start_next) by matching the non-space chars in order (case, quotes,
  dashes and ligatures folded) with a bounded look-ahead, so a char the page
  doesn't show never sends the rest of the block running off to the end.

Rules that are easy to break:

- Qt drops an empty <a name> that sits between two block elements, so every
  ls-b anchor is first moved inside the element that follows it. Qt also
  ignores margins and indents given in pt or em, so those become px.
- Qt counts positions in UTF-16 units: a char outside the BMP takes two.
- The highlight is built only from the sentence's own chars (user requirement:
  it never passes over a running head or anything else that isn't the block):
  one ExtraSelection per run of chars that sit side by side in the document,
  split wherever the gap holds anything but the block's own spaces, and at line
  breaks, so the color hugs the words.
- Front and back matter (Block.skipped) is shown muted in the LivroSound
  layout and is never highlighted. Lines of the book's own contents page
  (Block.link >= 0) get a pointing hand and an underline on hover; clicking
  them emits clicked() like any other block (the main window follows the link).
- The text column is centered and at most MAX_COLUMN px wide. It's made with
  viewport margins and a fixed wrap width, not document margins, so resizing a
  wide window never re-lays out the book; only a new column width does.
- Qt's lazy layout is fragile: never change a format of a shown document
  except colors (image sizes are set before the document is shown, or right
  before a new column width re-lays it all), widen the scroll range by hand
  when jumping far ahead, and check Qt's hit test against block geometry.
- The original HTML's text and background colors are dropped: the theme
  colors the page.
- The user's ReadingStyle (font family and weight, line, paragraph, word and
  letter spacing, justification) sets the LivroSound layout; the margins and
  the widest column apply to both layouts.
- Paged (single or double page), the document is laid out with Qt's own
  pagination (QTextDocument.setPageSize: no line ever straddles a page edge),
  each page exactly one viewport high, and the scroll bar moves a page (or a
  spread of two) at a time. QTextEdit drops the page height whenever it
  re-wraps, so _paginate() puts it back after every rebuild and resize. Double
  page lays the text out at one page's width and paints page 2k on the left
  and 2k+1 on the right itself; mouse events on the right page are moved to
  where that page sits in the document, so selecting and clicking work there
  as everywhere else.
"""

from __future__ import annotations

import bisect
import math
import re
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, QSizeF, Qt, QUrl
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QGuiApplication,
    QImageReader,
    QMouseEvent,
    QPainter,
    QPalette,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextImageFormat,
)
from PySide6.QtWidgets import QFrame, QTextEdit

from livrosound import paths
from livrosound.book import ANCHOR_PREFIX, Block, Book
from livrosound.ui import theme as themes
from livrosound.ui.reader_view import (
    MODES,
    PAGE_MODES,
    ReaderView,
    ReadingStyle,
    centered_scroll_value,
    ensure_reading_fonts,
    outside_band,
    too_tall,
)
from livrosound.ui.theme import Theme

DEFAULT_SIZE = 20.0  # pt
MIN_SIZE, MAX_SIZE = 12.0, 40.0
MAX_COLUMN = 700  # px: the widest the text column gets
MIN_MARGIN = 24  # px beside the column on a narrow window
COLUMN_PADDING = 22  # px inside the column, so a highlight never touches its edge
TOP_PADDING = 56
BOTTOM_PADDING = 160
PAGED_TOP = 40  # px above the text on each page
PAGED_BOTTOM = 104  # px under it: clear of the player floating over the page's foot
PAGE_GUTTER = 64  # px between the two pages of a spread
HEADING_SCALE = {1: 1.6, 2: 1.25}  # deeper levels: 1.1
MAX_IMAGE_HEIGHT = 600  # px: a cover fits a typical window
SEPARATOR = "\u2042"  # ⁂
LOOKAHEAD = 200  # original layout: how far (in non-space chars) a block's next char may be found
_PROBE = 6  # chars that must follow a re-found char, so a common letter isn't matched far away
_TOP_PROBE = 4  # px below the top of the view where current_block looks
_LAYOUT_REST = 100_000  # chars: finishing the lazy layout of this much text is quick
_HIT_SLOP = 80  # px: how far outside its block Qt's hit test may land (block margins)

# Invisible chars that count as space when aligning and highlighting: soft
# hyphens, zero-width spaces and joiners, the BOM.
_INVISIBLE = "\u00ad\u200b\u200c\u200d\u2060\ufeff"
_LINE_BREAKS = "\n\r\u2028\u2029\ufdd0\ufdd1"  # Qt's paragraph/line separators and frame marks


def _fold_table() -> dict[int, int]:
    """1:1 folding for matching a block's text against Qt's rendering of the HTML:
    lower case, straight quotes, one dash, a ligature as its first letter."""
    table = {}
    for cp in range(0x3000):
        low = chr(cp).lower()
        if low != chr(cp) and len(low) == 1:
            table[cp] = ord(low)
    for chars, to in (
        ("\u2018\u2019\u201a\u201b\u2032`\u00b4", "'"),
        ("\u201c\u201d\u201e\u201f\u2033", '"'),
        ("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
        ("\ufb00\ufb01\ufb02\ufb03\ufb04", "f"),  # ff fi fl ffi ffl
        ("\ufb05\ufb06", "s"),  # long st, st
    ):
        for ch in chars:
            table[ord(ch)] = ord(to)
    return table


_FOLD = _fold_table()


def _ignorable_table() -> np.ndarray:
    table = np.zeros(0x10000, dtype=bool)
    for cp in range(0x3001):
        if chr(cp).isspace():
            table[cp] = True
    for ch in _INVISIBLE + "\ufffc\ufdd0\ufdd1":  # + images and frame marks: never part of a block's words
        table[ord(ch)] = True
    return table


_IGNORABLE = _ignorable_table()


def _codepoints(text: str) -> np.ndarray:
    return np.frombuffer(text.encode("utf-32-le"), dtype=np.uint32)


def _ignorable(cp: np.ndarray) -> np.ndarray:
    return _IGNORABLE[np.minimum(cp, 0xFFFF)] & (cp <= 0xFFFF)


def _is_space(ch: str) -> bool:
    return ch.isspace() or ch in _INVISIBLE


def _qt_positions(text: str) -> np.ndarray | None:
    """Qt's position of each index of text, plus one past the end, when text has
    chars outside the BMP (two UTF-16 units each); None when they're the same."""
    if not text or max(text) <= "\uffff":
        return None
    wide = _codepoints(text) > 0xFFFF
    return np.concatenate(([0], np.cumsum(1 + wide.astype(np.int64))))


# ---------- the original layout: preparing the HTML, aligning the blocks ----------

_ANY_ANCHOR = re.compile(
    rf"""<a\s[^>]*?\b(?:name|id)\s*=\s*["']?({ANCHOR_PREFIX}\d+)["']?[^>]*?(?:/>|>\s*</a>)""", re.I
)
# A run of block anchors right before an opening tag that can hold text.
_ANCHORS_BEFORE_TAG = re.compile(
    rf"""((?:<a name="{ANCHOR_PREFIX}\d+"></a>\s*)*<a name="{ANCHOR_PREFIX}\d+"></a>)\s*"""
    r"""(<(?!(?:a|img|br|hr|wbr|svg|image|input)\b)[a-zA-Z][\w:-]*(?:\s[^>]*)?(?<!/)>)""",
    re.I,
)
_STYLE_ATTR = re.compile(r"""(\bstyle\s*=\s*)(["'])(.*?)\2""", re.I | re.S)
_STYLE_BLOCK = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.I | re.S)
_COLOR_DECL = re.compile(r"(?<![\w-])(?:color|background(?:-color|-image)?)\s*:[^;}]*;?", re.I)
_COLOR_ATTR = re.compile(r"""\s(?:color|bgcolor|text|link|vlink|alink)\s*=\s*(["']?)[^"'\s>]*\1""", re.I)
_TAG_WITH_COLOR = re.compile(r"<(?:font|body|table|tr|td|th)\b[^>]*>", re.I)
# Qt's rich text takes margins, paddings and indents in px only (6pt or 1.5em are ignored).
_BOX_DECL = re.compile(
    r"((?<![\w-])(?:margin|padding)(?:-top|-bottom|-left|-right)?|text-indent)(\s*:\s*)([^;}\"']+)", re.I
)
_LENGTH = re.compile(r"(-?\d*\.?\d+)(pt|em|rem)\b", re.I)


# Under the book's own CSS: Qt's HTML defaults set text tight (line height 100%).
_ORIGINAL_DEFAULTS = "p, li, blockquote, dd, pre { line-height: 135%; }"


def prepare_original_html(html: str, em_px: float = 16.0) -> str:
    """original_html made ready for Qt: block anchors moved inside the element that
    follows them (Qt drops an empty anchor between two blocks), colors removed (the
    theme colors the page), and margins/indents in pt or em turned into px (Qt
    ignores them otherwise and every paragraph runs into the next)."""
    html = _ANY_ANCHOR.sub(r'<a name="\1"></a>', html)
    for _ in range(8):  # one tag per pass: <div><blockquote><p> takes three
        html, moved = _ANCHORS_BEFORE_TAG.subn(r"\2\1", html)
        if not moved:
            break

    def px(m: re.Match) -> str:
        value = float(m.group(1)) * (4 / 3 if m.group(2).lower() == "pt" else em_px)
        return f"{round(value)}px"

    def css(text: str) -> str:
        text = _COLOR_DECL.sub("", text)
        return _BOX_DECL.sub(lambda m: m.group(1) + m.group(2) + _LENGTH.sub(px, m.group(3)), text)

    html = _STYLE_BLOCK.sub(lambda m: m.group(1) + css(m.group(2)) + m.group(3), html)
    html = _STYLE_ATTR.sub(lambda m: m.group(1) + m.group(2) + css(m.group(3)) + m.group(2), html)
    return _TAG_WITH_COLOR.sub(lambda m: _COLOR_ATTR.sub("", m.group(0)), html)


def _align_run(text: str, doc: str, lookahead: int) -> list[int]:
    """Index into doc of each char of text (both without spaces), -1 where the page
    doesn't show it. A char that isn't next in doc is looked for up to `lookahead`
    chars ahead, together with the chars that follow it, so the two re-sync after
    a running head, a stray hyphen or a ligature without wandering off."""
    out = [-1] * len(text)
    j, n = 0, len(doc)
    for k, ch in enumerate(text):
        if j < n and doc[j] == ch:
            out[k] = j
            j += 1
            continue
        probe = text[k : k + _PROBE]
        found = doc.find(probe, j, min(n, j + lookahead + len(probe)))
        if found >= 0:
            out[k] = found
            j = found + 1
    return out


def align_blocks(
    doc_text: str, texts: list[str], anchors: dict[int, int], lookahead: int = LOOKAHEAD
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Where each char of each block is in a document's text (indices of doc_text).

    anchors: block index -> index in doc_text where the block's element starts
    (blocks without one continue from the block before). Returns (positions,
    matched, offsets): block i's map is positions[offsets[i]:offsets[i + 1]], one
    index per char of texts[i] plus one past its end, never decreasing; matched
    says which chars were found on the page. A char that isn't (spaces, a letter
    the page lacks) maps just after the last char that was, so nothing runs ahead.
    """
    n = len(texts)
    lengths = np.fromiter((len(t) for t in texts), dtype=np.int64, count=n)
    char_off = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(lengths, out=char_off[1:])

    dcp = _codepoints(doc_text.translate(_FOLD))
    d_keep = np.flatnonzero(~_ignorable(dcp))
    d_str = dcp[d_keep].tobytes().decode("utf-32-le")
    tcp = _codepoints("".join(texts).translate(_FOLD))
    t_mask = ~_ignorable(tcp)
    t_keep = np.flatnonzero(t_mask)
    t_str = tcp[t_keep].tobytes().decode("utf-32-le")
    keep_off = np.searchsorted(t_keep, char_off)

    # Regions between anchors, in document order (an anchor out of order is ignored).
    marks: list[tuple[int, int]] = []
    for block in sorted(anchors):
        pos = anchors[block]
        if 0 <= block < n and (not marks or pos >= marks[-1][1]):
            marks.append((block, pos))
    if not marks or marks[0][0] != 0:
        marks.insert(0, (0, 0))

    found = np.full(len(t_keep), -1, dtype=np.int64)
    seeds = np.zeros(len(tcp), dtype=np.int64)  # leading spaces of an anchored block sit at its anchor
    for g, (b0, p0) in enumerate(marks):
        b1, p1 = marks[g + 1] if g + 1 < len(marks) else (n, len(doc_text))
        ka, kb = keep_off[b0], keep_off[b1]
        da, db = np.searchsorted(d_keep, (p0, p1))
        if lengths[b0]:
            seeds[char_off[b0]] = p0
        if kb - ka == db - da and t_str[ka:kb] == d_str[da:db]:
            found[ka:kb] = d_keep[da:db]
        elif kb > ka:
            idx = np.asarray(_align_run(t_str[ka:kb], d_str[da:db], lookahead), dtype=np.int64)
            hit = idx >= 0
            found[ka:kb][hit] = d_keep[da + idx[hit]]

    # Every char: its doc index if found, else just after the last found char
    # (the running maximum, seeded with each anchor).
    full = np.full(len(tcp), -1, dtype=np.int64)
    full[t_keep] = found
    matched = full >= 0
    after = np.maximum.accumulate(np.where(matched, full + 1, seeds)) if len(tcp) else seeds
    full = np.where(matched, full, after)

    # Each block's chars, then one past its end (an empty block: at its anchor).
    ends = np.concatenate(([0], after))[char_off[1:]]
    for block, pos in marks:
        if not lengths[block]:
            ends[block] = max(ends[block], pos)
    offsets = char_off + np.arange(n + 1)
    out = np.arange(len(tcp)) + np.repeat(np.arange(n), lengths)
    positions = np.empty(len(tcp) + n, dtype=np.int64)
    flags = np.zeros(len(tcp) + n, dtype=bool)
    positions[out] = full
    flags[out] = matched
    positions[offsets[1:] - 1] = ends
    return positions, flags, offsets


# ---------- the view ----------


class ReflowView(ReaderView, QTextEdit):
    """The book as reflowable text (see the module docstring)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        ensure_reading_fonts()
        self.setObjectName("readerView")
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAutoFillBackground(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Always on, so the column's centering never shifts when the bar appears.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setLineWrapMode(QTextEdit.LineWrapMode.FixedPixelWidth)
        self._theme = themes.current()
        self._size = DEFAULT_SIZE
        self._mode = "livrosound"
        self._book: Book | None = None
        self._positions = np.zeros(1, dtype=np.int64)  # see align_blocks: every block's char map
        self._matched = np.zeros(1, dtype=bool)
        self._offsets = np.zeros(1, dtype=np.int64)
        self._starts: list[int] = []  # document position where each block starts
        self._images: list[tuple[int, QSize]] = []  # (document position, natural size)
        self._muted: list[tuple[int, int]] = []  # LivroSound layout: document ranges in the muted color
        self._column = 0  # the viewport's width: the text column plus its padding
        self._highlight: tuple[int, int, int] | None = None  # (block, start, end)
        self._style = ReadingStyle()
        self._marks: list[tuple[int, int, int, str]] = []
        self._page_height = 0  # paged: the height of a page (the viewport's)
        self._total_pages: int | None = None  # counted on demand (it lays the whole book out)
        self._init_reader_view()
        self.set_theme(self._theme)
        self.selectionChanged.connect(self._repaint_if_double)
        self.verticalScrollBar().rangeChanged.connect(lambda *_: self._allow_value(0))

    # ---------- ReaderView ----------

    def set_book(self, book: Book, mode: str) -> None:
        """Show a book. "original" needs Book.original_html (EPUB/DOCX/TXT); a book
        without it (a PDF) is shown in the LivroSound layout."""
        if mode not in MODES:
            raise ValueError(f"unknown layout {mode!r}")
        self.stop_scrolling()
        self._highlight = None
        self._set_hover_link(-1)  # while the old book is still the one shown
        self._book = book
        self._mode = mode if mode == "livrosound" or book.original_html else "livrosound"
        self._rebuild()
        self.verticalScrollBar().setValue(0)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        palette = self.palette()
        page, text = QColor(theme.page), QColor(theme.text)
        for role, color in (
            (QPalette.ColorRole.Base, page),
            (QPalette.ColorRole.Window, page),
            (QPalette.ColorRole.Text, text),
            (QPalette.ColorRole.WindowText, text),
            (QPalette.ColorRole.Highlight, QColor(themes.selection_color(theme))),
            (QPalette.ColorRole.HighlightedText, text),
        ):
            palette.setColor(role, color)
        self.setPalette(palette)
        self.viewport().setPalette(palette)
        if self._muted:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(theme.muted))
            cursor = QTextCursor(self.document())
            cursor.beginEditBlock()
            for start, end in self._muted:
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(fmt)
            cursor.endEditBlock()
        self._refresh_selections()

    def set_scale(self, value: float) -> None:
        """The text size in points (LivroSound layout; the original layout scales
        whatever its HTML doesn't size itself)."""
        size = max(MIN_SIZE, min(MAX_SIZE, float(value)))
        if size == self._size:
            return
        self._size = size
        if self._book is None:
            return
        top = self._top()
        if self._mode == "original":
            self.document().setDefaultFont(self._body_font())
            self._paginate()
        else:
            self._rebuild()
        self._restore_top(top)

    def set_style(self, style: ReadingStyle) -> None:
        """The user's text settings. Margins and the column width re-wrap the text; the
        rest re-sets the LivroSound layout (the original layout keeps its own type)."""
        if style == self._style:
            return
        old, self._style = self._style, style
        if self._book is None:
            return
        top = self._top()
        typeset = (old.family, old.weight, old.line_spacing, old.paragraph_spacing, old.word_spacing,
                   old.letter_spacing, old.justify)
        if typeset != (style.family, style.weight, style.line_spacing, style.paragraph_spacing,
                       style.word_spacing, style.letter_spacing, style.justify):
            if self._mode == "original":
                self.document().setDefaultFont(self._body_font())
            else:
                self._rebuild()
        self._update_geometry()
        self._paginate()
        self._restore_top(top)

    def set_page_mode(self, mode: str) -> None:
        if mode not in PAGE_MODES:
            raise ValueError(f"unknown page mode {mode!r}")
        if mode == self._page_mode:
            return
        top = self._top() if self._book is not None else None
        self._page_mode = mode
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn if mode == "scroll" else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._pad(self.document())
        self._total_pages = None
        self._update_geometry()
        self._paginate()
        self._restore_top(top)
        self.viewport().update()

    def spread_step(self) -> int:
        return max(1, self._page_height) * (2 if self._page_mode == "double" else 1)

    def page_info(self) -> tuple[int, int, int] | None:
        """Pages are screens of text while scrolling. The total comes from count_pages()."""
        if self._book is None or self._total_pages is None:
            return None
        height = self._page_height if self.paged() else max(1, self.viewport().height())
        first = self.verticalScrollBar().value() // max(1, height) + 1
        last = first + 1 if self._page_mode == "double" else first
        total = self._total_pages
        return min(first, total), min(last, total), total

    def count_pages(self) -> int:
        """Lay the whole book out (≈0.7 s for Anna Karenina) and count its pages."""
        if self._total_pages is None and self._book is not None:
            doc = self.document()
            if self.paged():
                self._total_pages = max(1, doc.pageCount())
            else:
                layout = doc.documentLayout()
                layout.blockBoundingRect(doc.lastBlock())  # finishes the lazy layout
                height = layout.documentSize().height()
                self._total_pages = max(1, math.ceil(height / max(1, self.viewport().height())))
        return self._total_pages or 0

    def _top(self) -> tuple[int, int] | None:
        """(block, char) at the top of the page (to keep the reader's place)."""
        return self._locate(self._position_at(QPoint(self._column // 2, PAGED_TOP + 4 if self.paged() else 0)))

    def _restore_top(self, top: tuple[int, int] | None) -> None:
        if top is None:
            return
        position = self._doc_pos(*top)
        if self.paged():
            self._show_page_of(position)
            return
        target = self.verticalScrollBar().value() + self.cursorRect(self._cursor(position)).top()
        self._reach(target, position)
        self.scroll_to_value(target, animate=False)

    def show_highlight(self, block: int, start: int, end: int) -> None:
        if self._book is None or not 0 <= block < len(self._book.blocks) or self._book.blocks[block].skipped:
            self.clear_highlight()
            return
        self._highlight = (block, start, end)
        self._refresh_selections()

    def clear_highlight(self) -> None:
        self._highlight = None
        self._refresh_selections()

    def set_marks(self, marks: list[tuple[int, int, int, str]]) -> None:
        self._marks = [m for m in marks if self._book is not None and 0 <= m[0] < len(self._book.blocks)]
        self._refresh_selections()

    def selected_range(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        cursor = self.textCursor()
        if self._book is None or not cursor.hasSelection():
            return None
        start = self._locate(cursor.selectionStart())
        last = self._locate(max(cursor.selectionStart(), cursor.selectionEnd() - 1))
        if start is None or last is None:
            return None
        return start, (last[0], last[1] + 1)

    def clear_selection(self) -> None:
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        self._repaint_if_double()

    def scroll_to(self, block: int, char: int = 0, *, animate: bool = True) -> None:
        """Center the block, or — when it's taller than most of the view — the
        highlighted sentence in it (else the line holding `char`)."""
        if self._book is None or not self._book.blocks:
            return
        block = max(0, min(len(self._book.blocks) - 1, block))
        positions = self._block_positions(block)
        start, end = int(positions[0]), max(int(positions[-1]), int(positions[0]) + 1)
        if too_tall(*self._span(start, end), self.viewport().height()):
            runs = self._runs(*self._highlight) if self._highlight and self._highlight[0] == block else []
            if runs:
                start, end = runs[0][0], runs[-1][1]
            else:
                start = self._doc_pos(block, char)
                end = start + 1
        if self.paged():
            runs = self._runs(*self._highlight) if self._highlight and self._highlight[0] == block else []
            self._show_page_of(runs[0][0] if runs else self._doc_pos(block, char))
            return
        self._center(start, end, animate)

    def keep_in_view(self, block: int, start: int, end: int) -> None:
        """Re-center when the chars [start, end) of a block leave the middle of the view
        (measured where a running scroll animation is heading)."""
        if self._book is None or not 0 <= block < len(self._book.blocks):
            return
        runs = self._runs(block, start, end)
        if runs:
            first, last = runs[0][0], runs[-1][1]
        else:
            first = self._doc_pos(block, start)
            last = first + 1
        if self.paged():
            if self._page_of(first) not in self._pages_shown():
                self._show_page_of(first)
            return
        top, bottom = self._span(first, last)
        shift = self.verticalScrollBar().value() - self.scroll_target()
        if outside_band(top + shift, bottom + shift, self.viewport().height()):
            self._center(first, last, True)

    def current_block(self) -> int:
        y = PAGED_TOP + _TOP_PROBE if self.paged() else _TOP_PROBE
        hit = self._locate(self._position_at(QPoint(self._column // 2, y)))
        return hit[0] if hit else 0

    def hit_test(self, pos: QPoint) -> tuple[int, int] | None:
        if self._book is None:
            return None
        position = self._position_at(pos)
        cursor = self._cursor(position)
        # The cursor sits between chars: when the point is left of it, it's on the char before.
        if position > cursor.block().position() and self.cursorRect(cursor).left() > pos.x():
            position -= 1
        return self._locate(position)

    def _hover_link_changed(self, block: int) -> None:
        self._refresh_selections()

    # ---------- building the document ----------

    def _rebuild(self) -> None:
        if self._book is None:
            return
        if self._mode == "original":
            doc = self._original_document(self._book)
        else:
            doc = self._reflow_document(self._book)
        old = self.document()
        ours = old.parent() is self  # Qt deletes its own first document itself
        self.setDocument(doc)
        if ours:
            old.deleteLater()
        self._total_pages = None
        self._paginate()
        self._refresh_selections()  # the old ones point into the old document

    def _new_document(self) -> QTextDocument:
        doc = QTextDocument(self)
        doc.setUndoRedoEnabled(False)
        doc.setDocumentMargin(0)
        doc.setDefaultFont(self._body_font())
        return doc

    def _body_font(self) -> QFont:
        return themes.reading_font(self._size, QFont.Weight(self._style.weight), self._style.family)

    def _pad(self, doc: QTextDocument) -> None:
        """The padding around the text: per page when paged (Qt repeats the root
        frame's top and bottom margins on every page)."""
        frame = doc.rootFrame()
        fmt = frame.frameFormat()
        fmt.setLeftMargin(COLUMN_PADDING)
        fmt.setRightMargin(COLUMN_PADDING)
        fmt.setTopMargin(PAGED_TOP if self.paged() else TOP_PADDING)
        fmt.setBottomMargin(PAGED_BOTTOM if self.paged() else BOTTOM_PADDING)
        frame.setFrameFormat(fmt)

    def _reflow_document(self, book: Book) -> QTextDocument:
        doc = self._new_document()
        self._pad(doc)
        styles = _ReflowStyles(self._size, self._theme.muted, self._style)
        res = paths.book_dir(book.book_id) / "res"
        n = len(book.blocks)
        lengths = [len(b.text) for b in book.blocks]
        offsets = np.zeros(n + 1, dtype=np.int64)
        np.cumsum(np.asarray(lengths, dtype=np.int64) + 1, out=offsets[1:])
        positions = np.zeros(int(offsets[-1]), dtype=np.int64)
        matched = np.zeros(int(offsets[-1]), dtype=bool)
        self._starts, self._images, self._muted = [], [], []
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        previous: Block | None = None
        for i, block in enumerate(book.blocks):
            block_format, char_format = styles.formats(block, previous)
            if i:
                cursor.insertBlock(block_format, char_format)
            else:
                cursor.setBlockFormat(block_format)
                cursor.setCharFormat(char_format)
            start = cursor.position()
            self._starts.append(start)
            o, length = int(offsets[i]), lengths[i]
            if block.kind in ("image", "separator"):
                shown = self._insert_image(cursor, res / block.src) if block.kind == "image" else False
                if block.kind == "separator":
                    cursor.insertText(SEPARATOR, char_format)
                    shown = True
                positions[o : o + length] = start
                positions[o + length] = start + int(shown)
            else:
                text = block.text.translate(_AS_LINE_SEPARATORS)
                for a, b, fmt in styles.runs(block, char_format):
                    cursor.insertText(text[a:b], fmt)
                qt = _qt_positions(block.text)
                positions[o : o + length + 1] = start + (qt if qt is not None else np.arange(length + 1))
                matched[o : o + length] = True
            if block.skipped or block.kind in ("note", "separator"):
                self._muted.append((start, cursor.position()))
            previous = block
        cursor.endEditBlock()
        self._positions, self._matched, self._offsets = positions, matched, offsets
        return doc

    def _insert_image(self, cursor: QTextCursor, path: Path) -> bool:
        size = QImageReader(str(path)).size() if path.is_file() else QSize()
        if not size.isValid() or size.isEmpty():
            return False
        fmt = QTextImageFormat()
        fmt.setName(QUrl.fromLocalFile(str(path)).toString())
        shown = self._image_size(size)
        fmt.setWidth(shown.width())
        fmt.setHeight(shown.height())
        self._images.append((cursor.position(), size))
        cursor.insertImage(fmt)
        return True

    def _original_document(self, book: Book) -> QTextDocument:
        folder = paths.book_dir(book.book_id)
        for name in book.fonts:
            font_path = str(folder / "res" / name)
            if font_path not in _FONTS_LOADED:
                _FONTS_LOADED.add(font_path)
                QFontDatabase.addApplicationFont(font_path)
        doc = self._new_document()
        doc.setBaseUrl(QUrl.fromLocalFile(str(folder) + "/"))
        doc.setDefaultStyleSheet(_ORIGINAL_DEFAULTS)
        doc.setHtml(prepare_original_html(book.original_html, _ReflowStyles(self._size, "", self._style).em))
        self._pad(doc)
        anchors, self._images = self._scan(doc, folder)
        self._fit_images(doc)
        text = doc.toPlainText()
        qt = _qt_positions(text)
        if qt is not None:  # anchors are Qt positions; align_blocks works in str indices
            anchors = {b: int(np.searchsorted(qt, p)) for b, p in anchors.items()}
        positions, matched, offsets = align_blocks(text, [b.text for b in book.blocks], anchors)
        if qt is not None:
            positions = qt[np.minimum(positions, len(text))]
        self._positions, self._matched, self._offsets = positions, matched, offsets
        self._starts = [int(positions[o]) for o in offsets[:-1]]
        for block, pos in anchors.items():  # a block's region starts at its anchor
            if 0 <= block < len(self._starts):
                self._starts[block] = int(qt[pos]) if qt is not None else pos
        for i in range(1, len(self._starts)):
            self._starts[i] = max(self._starts[i], self._starts[i - 1])
        self._muted = []
        return doc

    @staticmethod
    def _scan(doc: QTextDocument, folder: Path) -> tuple[dict[int, int], list[tuple[int, QSize]]]:
        """The block anchors and the images of a document made from HTML. Links lose
        Qt's baked-in palette color, so they follow the theme."""
        anchors: dict[int, int] = {}
        images: list[tuple[int, QSize]] = []
        links: list[tuple[int, int, QTextCharFormat]] = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                fmt = fragment.charFormat()
                for name in fmt.anchorNames():
                    if name.startswith(ANCHOR_PREFIX) and name[len(ANCHOR_PREFIX) :].isdigit():
                        anchors.setdefault(int(name[len(ANCHOR_PREFIX) :]), fragment.position())
                if fmt.anchorHref() and fmt.hasProperty(QTextFormat.Property.ForegroundBrush):
                    links.append((fragment.position(), fragment.length(), fmt))
                if fmt.isImageFormat():
                    image = fmt.toImageFormat()
                    url = doc.baseUrl().resolved(QUrl(image.name()))
                    size = QImageReader(url.toLocalFile()).size() if url.isLocalFile() else QSize()
                    if image.width() > 0 and image.height() > 0:
                        size = QSize(round(image.width()), round(image.height()))
                    if size.isValid() and not size.isEmpty():
                        images.extend((fragment.position() + k, size) for k in range(fragment.length()))
                it += 1
            block = block.next()
        if links:
            cursor = QTextCursor(doc)
            cursor.beginEditBlock()
            for start, length, fmt in links:
                fmt.clearForeground()
                cursor.setPosition(start)
                cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
                cursor.setCharFormat(fmt)
            cursor.endEditBlock()
        return anchors, images

    def _image_size(self, natural: QSize) -> QSize:
        """An image's size on the page: its own, scaled down to fit the column and
        MAX_IMAGE_HEIGHT (a cover then fits a typical window)."""
        box = QSize(max(80, (self._column or MAX_COLUMN) - 2 * COLUMN_PADDING), MAX_IMAGE_HEIGHT)
        if natural.width() <= box.width() and natural.height() <= box.height():
            return natural
        return natural.scaled(box, Qt.AspectRatioMode.KeepAspectRatio)

    def _fit_images(self, doc: QTextDocument) -> None:
        """Size every image for the column. Only ever before Qt lays the document
        out (a new document, or right before a new column width re-lays it all):
        changing a format in the middle of Qt's lazy layout leaves blocks
        unpositioned, and the view then paints nothing there."""
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        for pos, natural in self._images:
            cursor.setPosition(pos)
            cursor.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
            fmt = cursor.charFormat()
            if not fmt.isImageFormat():
                continue
            image = fmt.toImageFormat()
            size = self._image_size(natural)
            if round(image.width()) != size.width() or round(image.height()) != size.height():
                image.setWidth(size.width())
                image.setHeight(size.height())
                cursor.setCharFormat(image)
        cursor.endEditBlock()

    # ---------- positions ----------

    def _block_positions(self, block: int) -> np.ndarray:
        return self._positions[self._offsets[block] : self._offsets[block + 1]]

    def _doc_pos(self, block: int, char: int) -> int:
        positions = self._block_positions(block)
        return int(positions[max(0, min(len(positions) - 1, char))])

    def _locate(self, position: int) -> tuple[int, int] | None:
        """(block, char) at a document position."""
        if not self._starts:
            return None
        block = bisect.bisect_right(self._starts, position) - 1
        if block < 0:
            return None
        length = len(self._book.blocks[block].text)
        if length == 0:
            return block, 0
        char = int(np.searchsorted(self._block_positions(block)[:length], position, side="right")) - 1
        return block, max(0, min(length - 1, char))

    def _position_at(self, point: QPoint) -> int:
        """The cursor position nearest a viewport point. Right after a long jump into
        a lazily laid-out book Qt's own hit test can answer "the end of the book",
        so its answer is checked against the block's geometry."""
        cursor = self.cursorForPosition(point)
        y = point.y() + self.verticalScrollBar().value()
        layout = self.document().documentLayout()
        rect = layout.blockBoundingRect(cursor.block())
        if rect.top() - _HIT_SLOP <= y <= rect.bottom() + _HIT_SLOP:
            return cursor.position()
        doc = self.document()
        lo, hi = 0, doc.blockCount() - 1  # the first block that reaches below y
        while lo < hi:
            mid = (lo + hi) // 2
            if layout.blockBoundingRect(doc.findBlockByNumber(mid)).bottom() < y:
                lo = mid + 1
            else:
                hi = mid
        block = doc.findBlockByNumber(lo)
        rect = layout.blockBoundingRect(block)
        lines = block.layout()
        for i in range(lines.lineCount()):
            line = lines.lineAt(i)
            if rect.top() + line.y() + line.height() >= y or i == lines.lineCount() - 1:
                return block.position() + line.xToCursor(point.x() + self.horizontalScrollBar().value() - rect.left())
        return block.position()

    def _cursor(self, position: int) -> QTextCursor:
        cursor = QTextCursor(self.document())
        cursor.setPosition(max(0, min(position, self.document().characterCount() - 1)))
        return cursor

    def _span(self, start: int, end: int) -> tuple[int, int]:
        """Top and bottom (viewport coordinates) of the lines holding [start, end)."""
        first = self.cursorRect(self._cursor(start))
        last = self.cursorRect(self._cursor(max(start, end - 1)))
        return min(first.top(), last.top()), max(first.bottom(), last.bottom())

    def _center(self, start: int, end: int, animate: bool) -> None:
        """Scroll the lines holding the document range [start, end) to the middle of the view."""
        top, bottom = self._span(start, end)
        scrollbar = self.verticalScrollBar()
        height = self.viewport().height()
        self._reach(scrollbar.value() + (top + bottom) // 2 - height // 2, end)
        self.scroll_to_value(
            centered_scroll_value(scrollbar.value(), top, bottom, height, scrollbar.maximum()), animate=animate
        )

    def _reach(self, target: int, position: int) -> None:
        """Let the scroll bar reach `target`. Qt lays a big book out lazily (up to
        document `position` it already is) and widens the scroll range later,
        queued: widen it now. Near the end of the book the rest is laid out first,
        so the range is exact there and Qt never pulls the view back."""
        scrollbar = self.verticalScrollBar()
        if target <= scrollbar.maximum():
            return
        if self.document().characterCount() - position < _LAYOUT_REST:
            height = self.document().documentLayout().documentSize().height()
            target = min(target, int(height) - self.viewport().height())
        scrollbar.setMaximum(max(scrollbar.maximum(), target))

    # ---------- highlight ----------

    def _runs(self, block: int, start: int, end: int) -> list[tuple[int, int]]:
        """Document ranges that show the chars [start, end) of a block: one per run
        of chars side by side, split where the gap between two of them holds
        anything but spaces, or a line break."""
        text = self._book.blocks[block].text
        start, end = max(0, start), min(len(text), end)
        if start >= end:
            return []
        o = int(self._offsets[block])
        positions = self._positions[o + start : o + end].tolist()
        matched = self._matched[o + start : o + end].tolist()
        doc = self.document()
        runs: list[tuple[int, int]] = []
        run_start = run_end = -1
        for ch, pos, ok in zip(text[start:end], positions, matched):
            if not ok or _is_space(ch) or pos < run_end:
                continue
            width = 2 if ord(ch) > 0xFFFF else 1
            if run_end >= 0 and pos > run_end:
                gap = [doc.characterAt(p) for p in range(run_end, pos)]
                if any(not _is_space(c) or c in _LINE_BREAKS for c in gap):
                    runs.append((run_start, run_end))
                    run_start = pos
            elif run_end < 0:
                run_start = pos
            run_end = pos + width
        if run_end >= 0:
            runs.append((run_start, run_end))
        return runs

    def _selection(self, start: int, end: int, fmt: QTextCharFormat) -> QTextEdit.ExtraSelection:
        selection = QTextEdit.ExtraSelection()
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        selection.cursor = cursor
        selection.format = fmt
        return selection

    def _refresh_selections(self) -> None:
        """The highlight, plus the underline of a contents line under the pointer."""
        selections = []
        if self._book is not None:
            for block, start, end, color in self._marks:
                fmt = QTextCharFormat()
                mark = QColor(color)
                mark.setAlphaF(0.42 if not self._theme.dark else 0.55)
                fmt.setBackground(mark)
                selections += [self._selection(a, b, fmt) for a, b in self._runs(block, start, end)]
        if self._book is not None and self._highlight is not None:
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(self._theme.highlight))
            selections += [self._selection(a, b, fmt) for a, b in self._runs(*self._highlight)]
        if self._book is not None and self._hover_link >= 0:
            fmt = QTextCharFormat()
            fmt.setFontUnderline(True)
            text = self._book.blocks[self._hover_link].text
            selections += [self._selection(a, b, fmt) for a, b in self._runs(self._hover_link, 0, len(text))]
        self.setExtraSelections(selections)
        self._repaint_if_double()

    # ---------- events ----------

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            event.accept()  # QTextEdit would zoom its default font; the text size is set_scale's
            return
        super().wheelEvent(event)

    def resizeEvent(self, event) -> None:
        self._update_geometry()
        super().resizeEvent(event)
        if event.size().height() != event.oldSize().height():
            self._total_pages = None  # a screen (or a page) holds more or less now
        self._paginate()

    def _update_geometry(self) -> None:
        """The text column (or the two pages of a spread) and the margins beside it:
        the user's margins where set, centered otherwise, never wider than max_column."""
        style = self._style
        double = self._page_mode == "double"
        bar = 0 if self.paged() else self.verticalScrollBar().sizeHint().width()
        available = self.width() - bar
        left_margin = style.margin_left if style.margin_left >= 0 else MIN_MARGIN
        right_margin = style.margin_right if style.margin_right >= 0 else MIN_MARGIN
        gutter = PAGE_GUTTER if double else 0
        room = available - left_margin - right_margin - gutter
        column = max(160, min(style.max_column, room // (2 if double else 1)))
        used = column * (2 if double else 1) + gutter
        if style.margin_left < 0 and style.margin_right < 0:
            left = max(0, (self.width() - used) // 2)
        else:
            left = left_margin + max(0, available - left_margin - right_margin - used) // 2
        self.setViewportMargins(left, 0, max(0, available - used - left), 0)
        if column != self._column:
            # The document is laid out at the column's width, so only a new column
            # width re-lays out the book (QTextEdit would on every change of the window's width).
            self._column = column
            self._fit_images(self.document())
            self.setLineWrapColumnOrWidth(column)
            self._total_pages = None

    # ---------- pages ----------

    def _paginate(self) -> None:
        """Paged: lay the document out in pages one viewport high (QTextEdit forgets the
        page height whenever it re-wraps). Scrolling: no pages."""
        doc = self.document()
        height = self.viewport().height() if self.paged() else -1
        if self.paged() and height <= 0:
            return
        size = QSizeF(self._column or doc.pageSize().width(), height)
        if doc.pageSize() != size:
            doc.setPageSize(size)
            self._total_pages = None
        self._page_height = max(0, height)
        if self.paged():
            self.turn_to(self.verticalScrollBar().value())

    def _allow_value(self, value: int) -> None:
        """Paged: the last spread starts past Qt's scroll maximum when the last page is
        short; widen the range to it (and to `value`, a page that may not be laid out yet)."""
        if not self.paged() or self._page_height <= 0:
            return
        bar = self.verticalScrollBar()
        height = self.document().documentLayout().documentSize().height()
        last_page = max(0, math.ceil(height / self._page_height) - 1)
        step = self.spread_step()
        last = last_page * self._page_height // step * step
        target = max(last, min(value, (last_page + 1) * self._page_height))
        if bar.maximum() < target:
            bar.setMaximum(target)

    def _page_of(self, position: int) -> int:
        """The page (0-based) a document position is laid out on."""
        cursor = self._cursor(position)
        block = cursor.block()
        rect = self.document().documentLayout().blockBoundingRect(block)
        line = block.layout().lineForTextPosition(cursor.positionInBlock())
        y = rect.top() + (line.y() + line.height() / 2 if line.isValid() else 0)
        return int(y // max(1, self._page_height))

    def _pages_shown(self) -> range:
        first = self.verticalScrollBar().value() // max(1, self._page_height)
        return range(first, first + (2 if self._page_mode == "double" else 1))

    def _show_page_of(self, position: int) -> None:
        """Turn to the spread holding a document position."""
        page = self._page_of(position)
        self.turn_to(page * self._page_height)

    def _repaint_if_double(self) -> None:
        if self._page_mode == "double":
            self.viewport().update()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        if self._page_mode == "double":
            self.viewport().update()  # both pages change: nothing to move by blitting
            return
        super().scrollContentsBy(dx, dy)

    def paintEvent(self, event) -> None:
        if self._page_mode != "double":
            super().paintEvent(event)
            return
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor(self._theme.page))
        height = self._page_height
        top = self.verticalScrollBar().value()
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette = self.palette()
        selections = []
        for extra in self.extraSelections():
            selection = QAbstractTextDocumentLayout.Selection()
            selection.cursor, selection.format = extra.cursor, extra.format
            selections.append(selection)
        cursor = self.textCursor()
        if cursor.hasSelection():
            selection = QAbstractTextDocumentLayout.Selection()
            selection.cursor = cursor
            fmt = QTextCharFormat()
            fmt.setBackground(self.palette().color(QPalette.ColorRole.Highlight))
            fmt.setForeground(self.palette().color(QPalette.ColorRole.HighlightedText))
            selection.format = fmt
            selections.append(selection)
        context.selections = selections
        layout = self.document().documentLayout()
        for i in range(2):
            y = top + i * height
            painter.save()
            painter.translate(i * (self._column + PAGE_GUTTER), -y)
            clip = QRectF(0, y, self._column, height)
            painter.setClipRect(clip)
            context.clip = clip
            layout.draw(painter, context)
            painter.restore()
        spine = QColor(self._theme.text)
        spine.setAlphaF(0.08)
        x = self._column + PAGE_GUTTER / 2
        painter.fillRect(QRectF(x - 0.5, PAGED_TOP, 1, max(0, height - PAGED_TOP - PAGED_BOTTOM)), spine)
        painter.end()

    def _in_flow(self, event: QMouseEvent) -> QMouseEvent:
        """Double page: an event on the right page, moved to where that page sits in the
        document's single column (one page height further down)."""
        if self._page_mode != "double" or event.position().x() < self._column + PAGE_GUTTER / 2:
            return event
        shift = QPointF(-(self._column + PAGE_GUTTER), self._page_height)
        return QMouseEvent(
            event.type(), event.position() + shift, event.scenePosition(), event.globalPosition(),
            event.button(), event.buttons(), event.modifiers(),
        )

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(self._in_flow(event))
        self._repaint_if_double()

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(self._in_flow(event))
        self._repaint_if_double()

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(self._in_flow(event))
        if self.paged():
            self.turn_to(self.verticalScrollBar().value())  # a drag may have scrolled by lines
        if event.button() == Qt.MouseButton.LeftButton and self.textCursor().hasSelection():
            self.selection_made.emit(event.globalPosition().toPoint())

    def contextMenuEvent(self, event) -> None:
        pos = event.pos()
        if self._page_mode == "double" and pos.x() >= self._column + PAGE_GUTTER / 2:
            pos = QPoint(pos.x() - self._column - PAGE_GUTTER, pos.y() + self._page_height)
        hit = self.hit_test(pos)
        if hit is not None:
            self.context_requested.emit(*hit, event.globalPos())

    def mouseDoubleClickEvent(self, event) -> None:
        super().mouseDoubleClickEvent(self._in_flow(event))
        self._repaint_if_double()


_FONTS_LOADED: set[str] = set()  # a book's embedded fonts, registered once per run
_AS_LINE_SEPARATORS = str.maketrans({"\n": "\u2028", "\r": "\u2028", "\u2029": "\u2028"})


class _ReflowStyles:
    """Block and char formats of the LivroSound layout at one text size."""

    def __init__(self, size: float, muted: str, style: ReadingStyle | None = None):
        self.size = size
        self.muted = muted
        self.style = style or ReadingStyle()
        body = themes.reading_font(size, QFont.Weight(self.style.weight), self.style.family)
        dpi = QGuiApplication.primaryScreen().logicalDotsPerInchY() if QGuiApplication.primaryScreen() else 72.0
        self.em = size * dpi / 72  # px
        # Qt's proportional line height is a share of the font's natural line height.
        self.line_percent = round(100 * self.style.line_spacing * self.em / QFontMetricsF(body).height())
        self._char_cache: dict[tuple, QTextCharFormat] = {}

    def formats(self, block: Block, previous: Block | None) -> tuple[QTextBlockFormat, QTextCharFormat]:
        em = self.em
        fmt = QTextBlockFormat()
        fmt.setLineHeight(self.line_percent, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        scale, weight, muted = 1.0, QFont.Weight.Normal, block.skipped
        kind = block.kind
        first = previous is None
        if kind == "heading":
            scale = HEADING_SCALE.get(block.level, 1.1)
            weight = QFont.Weight.DemiBold
            fmt.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            fmt.setLineHeight(118, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            above = {1: 2.6, 2: 1.8}.get(block.level, 1.3)
            fmt.setTopMargin(0 if first else em * above)
            fmt.setBottomMargin(em * (1.0 if block.level <= 1 else 0.6))
        elif kind == "separator":
            muted = True
            fmt.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            fmt.setTopMargin(em * 0.9)
            fmt.setBottomMargin(em * 0.9)
            scale = 1.15
        elif kind == "image":
            fmt.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            fmt.setLineHeight(100, QTextBlockFormat.LineHeightTypes.SingleHeight.value)
            fmt.setTopMargin(em * 0.8)
            fmt.setBottomMargin(em * 0.8)
        elif kind == "quote":
            scale = 0.94
            fmt.setLeftMargin(em * 2)
            fmt.setRightMargin(em * 2)
            fmt.setTopMargin(em * 0.6)
            fmt.setBottomMargin(em * 0.6)
        elif kind == "verse":
            fmt.setLeftMargin(em * 2.2)
            fmt.setTopMargin(em * 0.6)
            fmt.setBottomMargin(em * 0.6)
        elif kind == "note":
            scale = 0.82
            muted = True
            fmt.setTopMargin(em * 0.3)
        else:  # paragraph
            fmt.setBottomMargin(em * self.style.paragraph_spacing)
            # Indented only when it follows a paragraph: never at the top of a chapter,
            # after a scene break or an image, or after an epigraph or quote. Lines of
            # a contents page are never indented.
            follows = previous is not None and previous.kind == "paragraph"
            if follows and block.link < 0 and block.align in ("", "left", "justify"):
                fmt.setTextIndent(em * 1.4)
        if kind in ("paragraph", "quote", "verse", "note") and block.align:
            fmt.setAlignment(_ALIGN.get(block.align, Qt.AlignmentFlag.AlignLeft))
        if self.style.justify and kind in ("paragraph", "quote", "note") and block.align in ("", "left", "justify"):
            fmt.setAlignment(Qt.AlignmentFlag.AlignJustify)
        return fmt, self._char_format(scale, weight, muted)

    def runs(self, block: Block, base: QTextCharFormat) -> list[tuple[int, int, QTextCharFormat]]:
        """[start, end, format) runs of a block's text, one per change of inline style."""
        text = block.text
        if not block.styles:
            return [(0, len(text), base)]
        cuts = {0, len(text)}
        for start, end, _ in block.styles:
            cuts.update((max(0, min(len(text), start)), max(0, min(len(text), end))))
        bounds = sorted(cuts)
        out = []
        for a, b in zip(bounds, bounds[1:]):
            if a == b:
                continue
            active = tuple(sorted({s for start, end, s in block.styles if start <= a and b <= end}))
            out.append((a, b, self._styled(base, active)))
        return out

    def _styled(self, base: QTextCharFormat, active: tuple[str, ...]) -> QTextCharFormat:
        if not active:
            return base
        fmt = QTextCharFormat(base)
        if "i" in active:
            fmt.setFontItalic(True)
        if "b" in active:
            fmt.setFontWeight(QFont.Weight(min(900, max(700, self.style.weight + 300))))
        if "sc" in active:
            # Iowan's own small caps (OpenType smcp): Qt's synthesized ones drop to a
            # fallback sans in every document after the first.
            fmt.setFontFeatures({QFont.Tag("smcp"): 1})
        if "sup" in active:
            fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignSuperScript)
        return fmt

    def _char_format(self, scale: float, weight: QFont.Weight, muted: bool) -> QTextCharFormat:
        key = (scale, weight, muted)
        fmt = self._char_cache.get(key)
        if fmt is None:
            fmt = QTextCharFormat()
            if scale != 1.0:
                fmt.setFontPointSize(self.size * scale)
            if weight != QFont.Weight.Normal:
                fmt.setFontWeight(QFont.Weight(max(int(weight.value), self.style.weight + 200)))
            if self.style.word_spacing:
                fmt.setFontWordSpacing(self.style.word_spacing)
            if self.style.letter_spacing:
                fmt.setFontLetterSpacingType(QFont.SpacingType.AbsoluteSpacing)
                fmt.setFontLetterSpacing(self.style.letter_spacing)
            if muted:
                fmt.setForeground(QColor(self.muted))
            self._char_cache[key] = fmt
        return fmt


_ALIGN = {
    "left": Qt.AlignmentFlag.AlignLeft,
    "center": Qt.AlignmentFlag.AlignHCenter,
    "right": Qt.AlignmentFlag.AlignRight,
    "justify": Qt.AlignmentFlag.AlignJustify,
}

