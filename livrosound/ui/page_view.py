"""The original layout of a PDF book: its own pages, as in Preview.

Why: a typeset book is best read on its own pages. The user reads in Preview
with Continuous Scroll, zoomed in until the text column fills the window, and
wants LivroSound to look the same, with a soft band over the words being read.
So PageView scrolls through every page continuously, crops each page to
Book.text_box plus a margin (the empty paper around the text is dropped) and
scales that region to the window's width times the zoom (set_scale: 1.0 = fit
the width, MIN_ZOOM..MAX_ZOOM). PDFs here are born digital (scanned books are
refused at import), so the word boxes of the text layer sit exactly on the
glyphs.

Rendering never blocks painting. One worker thread renders pages with PyMuPDF
from its own pymupdf.Document (MuPDF documents must not be shared across
threads), most wanted first:
- where the view is heading: a small preview of each page (a few ms), then the
  page sharp. After a long jump (a Contents pick) those pages render while the
  scroll animation runs, so the view never lands on blank pages;
- previews of the pages passed on the way, then PREFETCH neighbours.
Every sharp render also leaves a preview behind, so a page once seen is never
blank again while it re-renders. Sharp pages are rendered at device resolution
and drawn 1:1, kept in a small LRU cache; after a zoom or resize the old images
are stretched until the sharp ones replace them (debounced, so dragging the
window edge doesn't queue a render per pixel).

Themes: in Original the pages look exactly like the file. In the other themes a
page is tinted by its brightness: white paper becomes the theme's page color
and black ink its text color, so Calm reads as sepia and Dark as white on
black. Pages with real color or a large picture (a cover, a plate) keep their
own colors. Annotations saved in the file (say a Preview highlight) are not
drawn: they would look like the reading highlight.

The highlight follows the user's rules for the original layout:
- It is built only from the sentence's own word boxes (Block.words): one band
  per printed line, from the first to the last of that line's words the
  sentence touches. A word partly inside the range is covered whole, so a band
  never cuts a word ("dus[t with those impuritie]s" can't happen), and since
  running heads and page numbers are not in any block, a sentence that runs
  over a page break gets a band on each page and none over the head between.
  A band's padding never reaches past the middle of the space to the next word.
- The band is blended, not painted over: multiplied onto a light page (the ink
  stays black, the paper takes the highlight color), screened onto a dark one,
  so the letters under it stay crisp.
- Adjacent lines' bands are kept apart by a hairline gap, so a long sentence
  reads as a stack of lines, like a highlighter pen, rather than one slab.

Clicks and hover use a per-page index of word boxes built once in set_book.

Page views: "scroll" is the continuous column above. "single" and "double"
show one page (or two side by side) fitted whole into the window, each spread
exactly one viewport high, so a page turn moves the (hidden) scroll bar by
the viewport's height and the rendering, highlighting and hit testing above
work unchanged. The user's margins (ReadingStyle) replace GUTTER beside the
pages.
"""

from __future__ import annotations

import atexit
import bisect
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPainter, QPainterPath
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QFrame

from livrosound.book import Book, PageInfo, Word
from livrosound.ui import theme as themes
from livrosound.ui.reader_view import (
    PAGE_MODES,
    ReaderView,
    ReadingStyle,
    centered_scroll_value,
    outside_band,
    too_tall,
)
from livrosound.ui.theme import Theme

MIN_ZOOM = 0.8
MAX_ZOOM = 2.0
CROP_MARGIN = 0.06  # around Book.text_box, as a share of its width/height
GUTTER = 24  # px beside the pages at zoom 1.0
PAGE_GAP = 16  # px between pages
EDGE = 16  # px above the first page and below the last
PREFETCH = 2  # pages rendered ahead on each side of the visible ones
CACHE_PAGES = 12
CACHE_BYTES = 384 << 20  # a retina page at fit-width is ~30 MB
MAX_PAGE_PIXELS = 24_000_000  # very high zooms render softer rather than huge
RENDER_DELAY_MS = 120  # after a zoom or resize, before sharp pages are rendered
PREVIEW_SCALE = 0.3  # previews (shown until the sharp page arrives) are this share of the page's size
PREVIEW_PAGES = 64  # previews kept (~0.7 MB each)
HIT_TOLERANCE_PX = 10  # a click this close to a word still hits it

Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points
Tone = tuple[tuple[int, int, int], tuple[int, int, int]]  # (paper, ink) colors, RGB


# ---------- geometry (pure; tested directly) ----------


def crop_boxes(book: Book, words_by_page: dict[int, list[Box]] | None = None) -> list[Box]:
    """The region of each page that is shown: Book.text_box grown by CROP_MARGIN,
    and further wherever the book's own words on that page reach past it (a
    footnote, a recto page set further right), clamped to the page. Without a
    text box (or when it misses a page) the whole page is shown. (A page that
    ends up wider than most is scaled down to fit the width: PageView._fits.)"""
    boxes: list[Box] = []
    tb = book.text_box
    for index, info in enumerate(book.pages):
        page = (0.0, 0.0, float(info.width), float(info.height))
        if tb is None:
            boxes.append(page)
            continue
        mx, my = (tb[2] - tb[0]) * CROP_MARGIN, (tb[3] - tb[1]) * CROP_MARGIN
        x0, y0, x1, y1 = tb[0] - mx, tb[1] - my, tb[2] + mx, tb[3] + my
        for wx0, wy0, wx1, wy1 in (words_by_page or {}).get(index, ()):
            x0, y0, x1, y1 = min(x0, wx0 - mx / 3), min(y0, wy0 - my / 3), max(x1, wx1 + mx / 3), max(y1, wy1 + my / 3)
        x0, y0, x1, y1 = max(x0, 0.0), max(y0, 0.0), min(x1, page[2]), min(y1, page[3])
        if x1 - x0 < page[2] * 0.1 or y1 - y0 < page[3] * 0.1:
            boxes.append(page)
        else:
            boxes.append((x0, y0, x1, y1))
    return boxes


def _same_line(a: Word, b: Word) -> bool:
    """Do two consecutive words sit on one printed line? (Compared word to word, so
    a tall drop cap doesn't swallow the lines beside it.)"""
    if a[0] != b[0]:
        return False
    overlap = min(a[4], b[4]) - max(a[2], b[2])
    return overlap >= 0.5 * min(a[4] - a[2], b[4] - b[2])


@dataclass
class Band:
    """One printed line of a highlight."""

    page: int
    box: Box  # padded, in PDF points


def line_bands(words: list[Word], start: int, end: int) -> list[Band]:
    """The highlight of chars [start, end) of a block: one band per printed line,
    spanning the first to the last word the range touches on it (whole words),
    padded (never past the middle of the space to a neighbouring word), with a gap
    kept between adjacent lines. A drop cap much taller than its line gets a band
    of its own."""
    hits = [i for i, w in enumerate(words) if w[5] < end and w[6] > start and w[3] > w[1] and w[4] > w[2]]
    lines: list[list[int]] = []
    for i in hits:
        if lines and _same_line(words[lines[-1][-1]], words[i]):
            lines[-1].append(i)
        else:
            lines.append([i])
    bands: list[Band] = []
    extras: list[Band] = []
    for line in lines:
        line_words = [words[i] for i in line]
        heights = sorted(w[4] - w[2] for w in line_words)
        median = heights[len(heights) // 2]
        body = [w for w in line_words if w[4] - w[2] <= 1.8 * median]
        y0, y1 = min(w[2] for w in body), max(w[4] for w in body)
        pad, pad_y = 0.14 * (y1 - y0), 0.04 * (y1 - y0)
        first, last = line_words[0], line_words[-1]
        pad_left, pad_right = pad, pad
        if line[0] > 0 and _same_line(words[line[0] - 1], first):
            pad_left = min(pad, max(0.0, first[1] - words[line[0] - 1][3]) / 2)
        if line[-1] + 1 < len(words) and _same_line(last, words[line[-1] + 1]):
            pad_right = min(pad, max(0.0, words[line[-1] + 1][1] - last[3]) / 2)
        x0, x1 = min(w[1] for w in line_words), max(w[3] for w in line_words)
        bands.append(Band(first[0], (x0 - pad_left, y0 - pad_y, x1 + pad_right, y1 + pad_y)))
        for w in line_words:
            if w[4] - w[2] > 1.8 * median:
                extras.append(Band(w[0], (w[1] - pad, w[2] - pad_y, w[3] + pad, w[4] + pad_y)))
    for a, b in zip(bands, bands[1:]):
        apart = b.box[1] >= a.box[3] or b.box[3] <= a.box[1] or a.box[0] >= b.box[2] or b.box[0] >= a.box[2]
        if a.page != b.page or apart:
            continue
        gap = 0.08 * min(a.box[3] - a.box[1], b.box[3] - b.box[1])
        middle = (a.box[3] + b.box[1]) / 2 if b.box[1] > a.box[1] else (a.box[1] + a.box[3]) / 2
        a.box = (*a.box[:3], min(a.box[3], middle - gap / 2))
        b.box = (b.box[0], max(b.box[1], middle + gap / 2), *b.box[2:])
    return bands + extras


def _rgb(color: str) -> tuple[int, int, int]:
    c = QColor(color)
    return c.red(), c.green(), c.blue()


def page_tone(theme: Theme) -> Tone | None:
    """The (paper, ink) colors pages are tinted to in a theme; None = as printed."""
    paper, ink = _rgb(theme.page), _rgb(theme.text)
    if paper == (255, 255, 255) and ink == (0, 0, 0):
        return None
    return paper, ink


def band_blend(theme: Theme, *, as_printed: bool) -> tuple[QColor, QPainter.CompositionMode]:
    """How a band is laid on a page so that the paper under it turns the theme's
    highlight color while the letters keep their own."""
    if as_printed or not theme.dark:
        # A light page: multiply, so the paper takes the highlight color and the ink stays as dark.
        # (A page kept as printed is light paper even in a dark theme: it gets the light highlighter.)
        paper = (255, 255, 255) if as_printed else _rgb(theme.page)
        highlight = _rgb(themes.THEMES["original"].highlight if theme.dark else theme.highlight)
        channels = (min(255, round(255 * h / max(p, 1))) for h, p in zip(highlight, paper))
        return QColor(*channels), QPainter.CompositionMode.CompositionMode_Multiply
    # A dark page: screen, so the paper lightens to the highlight color and light ink stays light.
    paper, highlight = _rgb(theme.page), _rgb(theme.highlight)
    channels = (max(0, min(255, round(255 - 255 * (255 - h) / max(255 - p, 1)))) for h, p in zip(highlight, paper))
    return QColor(*channels), QPainter.CompositionMode.CompositionMode_Screen


# ---------- rendering (worker thread) ----------


@dataclass(frozen=True)
class _Key:
    """What a page image was rendered for. A sharp image fits one book, zoom,
    window width, screen and theme; a preview only the book and theme."""

    serial: int  # which book
    scale: float  # logical px per PDF point on a typical page
    dpr: float
    tone: Tone | None
    preview: bool = False

    def for_preview(self) -> _Key:
        return _Key(self.serial, 0.0, 1.0, self.tone, True)


@dataclass(frozen=True)
class _Job:
    page: int
    key: _Key
    clip: Box
    width: int  # device pixels
    height: int
    preview_size: tuple[int, int] | None = None  # a sharp render also hands back a preview this size


@dataclass
class _Entry:
    key: _Key
    image: QImage
    toned: bool  # tinted to the theme (else as printed)

    @property
    def nbytes(self) -> int:
        return self.image.sizeInBytes()


def _tinted(page: pymupdf.Page, clip: Box, rgb: np.ndarray, tone: Tone) -> np.ndarray | None:
    """A page tinted to the theme by its brightness, as 0xAARRGGBB; None for a page
    with real color or a large picture, which keeps its own colors."""
    sample = rgb[::6, ::6].astype(np.int16)
    chroma = sample.max(axis=2) - sample.min(axis=2)
    if (chroma > 64).mean() > 0.04:
        return None
    shown = pymupdf.Rect(clip)
    pictures = sum(abs(pymupdf.Rect(i["bbox"]) & shown) for i in page.get_image_info())
    if pictures > 0.3 * abs(shown):
        return None
    r, g, b = (rgb[..., i].astype(np.uint16) for i in range(3))
    luma = ((r * 77 + g * 150 + b * 29) >> 8).astype(np.uint8)
    t = (np.arange(256, dtype=np.float32) / 255)[:, None]
    paper, ink = np.array(tone[0], np.float32), np.array(tone[1], np.float32)
    rgb_lut = np.rint(ink + (paper - ink) * t).astype(np.uint32)
    lut = 0xFF000000 | (rgb_lut[:, 0] << 16) | (rgb_lut[:, 1] << 8) | rgb_lut[:, 2]
    return lut[luma]


def cut_words(page: pymupdf.Page, clip: Box) -> list[Box]:
    """Words of the page's text that the crop's top or bottom edge slices through (a
    running head or a converter's stamp just at the edge of the text box)."""
    x0, y0, x1, y1 = clip
    return [
        (a, b, c, d) for a, b, c, d, *_ in page.get_text("words") if c > x0 and a < x1 and (b < y0 < d or b < y1 < d)
    ]


def render_page(doc: pymupdf.Document, job: _Job, cut: list[Box] = ()) -> tuple[QImage, bool]:
    """Render a job's region of a page: (Format_RGB32 image, tinted to the theme).
    The `cut` words are painted over with the paper, rather than shown in half."""
    x0, y0, x1, y1 = job.clip
    page = doc[job.page]
    matrix = pymupdf.Matrix(job.width / (x1 - x0), job.height / (y1 - y0))
    pix = page.get_pixmap(
        matrix=matrix, clip=pymupdf.Rect(job.clip), alpha=False, annots=False, colorspace=pymupdf.csRGB
    )
    image, toned = None, False
    if job.key.tone is not None:
        rgb = np.frombuffer(pix.samples_mv, np.uint8).reshape(pix.height, pix.stride)[:, : pix.width * 3]
        tinted = _tinted(page, job.clip, rgb.reshape(pix.height, pix.width, 3), job.key.tone)
        if tinted is not None:
            tinted = np.ascontiguousarray(tinted)
            image = QImage(tinted.data, pix.width, pix.height, pix.width * 4, QImage.Format.Format_RGB32).copy()
            toned = True
    if image is None:
        image = QImage(pix.samples_mv, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
        image = image.convertToFormat(QImage.Format.Format_RGB32)
    if cut:
        kx, ky = image.width() / (x1 - x0), image.height() / (y1 - y0)
        paper = image.pixelColor(0, image.height() // 2)  # the crop's left margin
        painter = QPainter(image)
        for a, b, c, d in cut:
            painter.fillRect(QRectF((a - x0) * kx - 1, (b - y0) * ky - 1, (c - a) * kx + 2, (d - b) * ky + 2), paper)
        painter.end()
    return image, toned


class _RenderWorker(QThread):
    """Renders pages one at a time from its own Document, most wanted first.

    The view replaces the whole wish list as the user scrolls (set_jobs); a page
    being rendered, or rendered but not yet received by the view, is never queued
    twice (done() acknowledges it). Every sharp page also comes back shrunk to a
    preview."""

    rendered = Signal(int, object, QImage, bool)  # page, _Key, image, tinted
    failed = Signal(int, object)  # page, _Key

    def __init__(self, path: str):
        super().__init__()
        self._path = path
        self._cond = threading.Condition()
        self._jobs: list[_Job] = []
        self._busy: set[tuple[int, _Key]] = set()
        self._cut: dict[int, list[Box]] = {}  # page -> its cut_words (the crop never changes)
        self._stopping = False

    def set_jobs(self, jobs: list[_Job]) -> None:
        with self._cond:
            self._jobs = [j for j in jobs if (j.page, j.key) not in self._busy]
            self._cond.notify()

    def done(self, page: int, key: _Key) -> None:
        with self._cond:
            self._busy.discard((page, key))

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._jobs = []
            self._cond.notify()
        self.wait()
        _live_workers.discard(self)

    def run(self) -> None:
        try:
            doc = pymupdf.open(self._path)
        except Exception:  # missing or unreadable: every page stays a placeholder
            doc = None
        while True:
            with self._cond:
                while not self._jobs and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    break
                job = self._jobs.pop(0)
                self._busy.add((job.page, job.key))
            try:
                if doc is None:
                    raise RuntimeError("no document")
                if job.page not in self._cut:
                    self._cut[job.page] = cut_words(doc[job.page], job.clip)
                image, toned = render_page(doc, job, self._cut[job.page])
            except Exception:
                self.failed.emit(job.page, job.key)
                continue
            self.rendered.emit(job.page, job.key, image, toned)
            if job.preview_size is not None:
                small = image.scaled(
                    *job.preview_size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
                self.rendered.emit(job.page, job.key.for_preview(), small, toned)
        if doc is not None:
            doc.close()


# Every running worker, so Python never frees a QThread whose thread still runs
# (its view may be gone); all are stopped when the app quits, or at exit for a
# script that never ran the event loop.
_live_workers: set[_RenderWorker] = set()
_quit_hooked = False


def _stop_all_workers() -> None:
    for worker in list(_live_workers):
        worker.stop()


atexit.register(_stop_all_workers)


class _WorkerHandle:
    """The view's current worker, reachable from the view's destroyed() signal
    (a slot there must not hold the view itself)."""

    def __init__(self):
        self.worker: _RenderWorker | None = None

    def start(self, path: str) -> _RenderWorker:
        self.stop()
        self.worker = _RenderWorker(path)
        _live_workers.add(self.worker)
        self.worker.start()
        return self.worker

    def stop(self, *_) -> None:
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.stop()


# ---------- the view ----------


class PageView(ReaderView, QAbstractScrollArea):
    """A PDF book's own pages in continuous scroll, with the sentence highlighted."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("pageView")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)  # no relayout loop as it comes and goes
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._theme = themes.current()
        self._zoom = 1.0
        self._crops: list[Box] = []
        self._fits: list[float] = []  # < 1 for a page wider than most, so it still fits the width
        self._scale = 1.0  # logical px per PDF point on a typical page
        self._rects: list[QRect] = []  # each page in content coordinates (logical px)
        self._tops: list[int] = []
        self._content = (0, 0)
        self._words: dict[int, list[tuple[Box, int, int]]] = {}  # page -> (box, block, start char)
        self._block_pages: list[int] = []  # each block's page (-1 = none), for current_block
        self._bands: list[Band] = []
        self._highlighted = -1  # the block the bands belong to
        self._cache: OrderedDict[int, _Entry] = OrderedDict()  # sharp pages
        self._previews: OrderedDict[int, _Entry] = OrderedDict()  # small ones, stretched until a sharp one arrives
        self._failed: set[tuple[int, _Key]] = set()
        self._render = _WorkerHandle()
        self._serial = 0  # which book: part of every render key, so a late page of the last book is ignored
        self._render_key: _Key | None = None  # the key pages are rendered at (lags a zoom/resize)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(RENDER_DELAY_MS)
        self._render_timer.timeout.connect(self._sharpen)
        self._margins = (GUTTER, GUTTER)  # px beside the pages (the user's, or GUTTER)
        global _quit_hooked
        app = QApplication.instance()
        if app is not None and not _quit_hooked:
            app.aboutToQuit.connect(_stop_all_workers)
            _quit_hooked = True
        self.destroyed.connect(self._render.stop)
        self._init_reader_view()

    # ---------- ReaderView ----------

    def set_book(self, book: Book, mode: str = "original") -> None:
        """Show a PDF book's pages (always its original layout; `mode` is ignored)."""
        self._render.stop()
        self._serial += 1
        self._book = book
        self._bands, self._highlighted = [], -1
        self._cache.clear()
        self._previews.clear()
        self._failed.clear()
        if not book.pages and book.source_kind == "pdf":
            book.pages = _read_page_sizes(book.source_path)
        self._words = {}
        boxes: dict[int, list[Box]] = {}
        for index, block in enumerate(book.blocks):
            for word in block.words:
                box = (word[1], word[2], word[3], word[4])
                self._words.setdefault(word[0], []).append((box, index, word[5]))
                boxes.setdefault(word[0], []).append(box)
        self._block_pages = [b.page if b.page >= 0 else (b.words[0][0] if b.words else -1) for b in book.blocks]
        self._crops = crop_boxes(book, boxes)
        widths = sorted(c[2] - c[0] for c in self._crops)
        typical = widths[len(widths) // 2] if widths else 1.0
        self._fits = [min(1.0, typical / (c[2] - c[0])) for c in self._crops]
        if book.source_path and Path(book.source_path).is_file():
            worker = self._render.start(book.source_path)
            worker.rendered.connect(self._on_rendered)
            worker.failed.connect(self._on_failed)
        self._render_key = None
        self._relayout(keep_anchor=False)
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().maximum() // 2)
        self._sharpen()

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        if self._render_key is not None and self._key() != self._render_key:
            self._sharpen()
        self.viewport().update()

    def set_scale(self, value: float) -> None:
        """The zoom: 1.0 fits the text column to the width; MIN_ZOOM..MAX_ZOOM."""
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(value)))
        if abs(zoom - self._zoom) < 1e-6:
            return
        self._zoom = zoom
        if self._book is not None:
            self._relayout(keep_anchor=True)

    def zoom(self) -> float:
        return self._zoom

    def set_page_mode(self, mode: str) -> None:
        if mode not in PAGE_MODES:
            raise ValueError(f"unknown page mode {mode!r}")
        if mode == self._page_mode:
            return
        anchor = self._anchor()
        self._page_mode = mode
        paged = mode != "scroll"
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if paged else Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        )
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if paged else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        if self._book is not None:
            self._relayout(keep_anchor=False)
            if anchor is not None:
                self._restore_anchor(anchor)

    def set_style(self, style: ReadingStyle) -> None:
        """Only the margins apply to a PDF's own pages."""
        margins = (
            style.margin_left if style.margin_left >= 0 else GUTTER,
            style.margin_right if style.margin_right >= 0 else GUTTER,
        )
        if margins != self._margins:
            self._margins = margins
            if self._book is not None:
                self._relayout(keep_anchor=True)

    def spread_step(self) -> int:
        return max(1, self.viewport().height())

    def page_info(self) -> tuple[int, int, int] | None:
        """The printed pages on screen (in the middle of the view, scrolling)."""
        if not self._rects:
            return None
        pages = list(self._visible_pages())
        if not self.paged():
            middle = self.verticalScrollBar().value() + self.viewport().height() / 2
            page = min(pages, key=lambda i: abs(self._rects[i].center().y() - middle))
            pages = [page]
        return pages[0] + 1, pages[-1] + 1, len(self._rects)

    def _per_spread(self) -> int:
        return 2 if self._page_mode == "double" else 1

    def show_highlight(self, block: int, start: int, end: int) -> None:
        had = bool(self._bands)
        self._bands, self._highlighted = [], block
        if self._book is not None and 0 <= block < len(self._book.blocks):
            b = self._book.blocks[block]
            if not b.skipped:  # front/back matter is never highlighted
                self._bands = [band for band in line_bands(b.words, start, end) if 0 <= band.page < len(self._crops)]
        if had or self._bands:
            self.viewport().update()

    def clear_highlight(self) -> None:
        self._highlighted = -1
        if self._bands:
            self._bands = []
            self.viewport().update()

    def highlight_rects(self) -> list[tuple[int, QRectF]]:
        """The bands as drawn: (page, rect in PDF points)."""
        return [(b.page, QRectF(QPointF(b.box[0], b.box[1]), QPointF(b.box[2], b.box[3]))) for b in self._bands]

    def scroll_to(self, block: int, char: int = 0, *, animate: bool = True) -> None:
        """Center the block; one too tall for that centers the highlighted sentence
        in it, else the line holding the char (ReaderView's auto-follow rule)."""
        target = self._locate(block, char)
        if target is None:
            return
        page, box = target
        if self.paged():
            self.turn_to(page // self._per_spread() * self.spread_step())
            return
        if box is None:  # a block without words (an image): its page's top
            self.scroll_to_value(self._rects[page].top() - EDGE, animate=animate)
            return
        height = self.viewport().height()
        span = self._span([(w[0], w[1:5]) for w in self._book.blocks[block].words])
        if span is None or too_tall(int(span.top()), int(span.bottom()), height):
            highlight = self._span([(b.page, b.box) for b in self._bands]) if self._highlighted == block else None
            span = highlight or self.map_box(page, box)
        self._follow_horizontally(self.map_box(page, box))
        bar = self.verticalScrollBar()
        self.scroll_to_value(
            centered_scroll_value(bar.value(), int(span.top()), int(span.bottom()), height, bar.maximum()),
            animate=animate,
        )

    def keep_in_view(self, block: int, start: int, end: int) -> None:
        if self._book is None or not (0 <= block < len(self._book.blocks)):
            return
        bands = [b for b in line_bands(self._book.blocks[block].words, start, end) if 0 <= b.page < len(self._rects)]
        span = self._span([(b.page, b.box) for b in bands])
        if span is None:
            return
        if self.paged():
            if bands[0].page not in self._visible_pages():
                self.turn_to(bands[0].page // self._per_spread() * self.spread_step())
            return
        bar = self.verticalScrollBar()
        shift = bar.value() - self.scroll_target()  # judge where the view is heading, not where it is mid-animation
        top, bottom = int(span.top()) + shift, int(span.bottom()) + shift
        height = self.viewport().height()
        if not outside_band(top, bottom, height):
            return
        first = self.map_box(bands[0].page, bands[0].box)
        if too_tall(top, bottom, height):
            bottom = top + int(first.height())  # center its first line
        self._follow_horizontally(first)
        self.scroll_to_value(centered_scroll_value(self.scroll_target(), top, bottom, height, bar.maximum()))

    def current_block(self) -> int:
        if self._book is None or not self._rects:
            return 0
        for page in self._visible_pages():
            best = None
            for box, block, _ in self._words.get(page, ()):
                y = self.map_box(page, box).center().y()
                if y >= 0 and (best is None or (y, box[0]) < best[0]):
                    best = ((y, box[0]), block)
            if best is not None:
                return best[1]
        page = max(0, bisect.bisect_right(self._tops, self.verticalScrollBar().value()) - 1)
        before = [i for i, p in enumerate(self._block_pages) if 0 <= p <= page]
        return before[-1] if before else 0

    def hit_test(self, pos: QPoint) -> tuple[int, int] | None:
        if not self._rects:
            return None
        cy = pos.y() + self.verticalScrollBar().value()
        page = max(0, bisect.bisect_right(self._tops, cy) - 1)
        best = None
        for candidate in (page, page - 1, page + 1):
            if not 0 <= candidate < len(self._rects):
                continue
            px, py = self.map_to_page(candidate, QPointF(pos))
            tolerance = HIT_TOLERANCE_PX / self._page_scale(candidate)
            for box, block, char in self._words.get(candidate, ()):
                dx = max(box[0] - px, 0.0, px - box[2])
                dy = max(box[1] - py, 0.0, py - box[3])
                if dx > tolerance or dy > tolerance:
                    continue
                distance = (dy, dx)  # the line under the pointer first, then the nearest word on it
                if best is None or distance < best[0]:
                    best = (distance, block, char)
        return None if best is None else (best[1], best[2])

    def scroll_to_value(self, target: int, *, animate: bool = True) -> None:
        super().scroll_to_value(target, animate=animate)
        self._schedule()  # the destination's pages first, while the view travels there

    def shutdown(self) -> None:
        """Stop the render thread (the main window calls this before closing)."""
        self._render.stop()

    # ---------- coordinates ----------

    def map_box(self, page: int, box: Box) -> QRectF:
        """A box in a page's PDF points -> viewport coordinates."""
        rect, crop, s = self._rects[page], self._crops[page], self._page_scale(page)
        h, v = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
        return QRectF(
            rect.left() - h + (box[0] - crop[0]) * s,
            rect.top() - v + (box[1] - crop[1]) * s,
            (box[2] - box[0]) * s,
            (box[3] - box[1]) * s,
        )

    def map_to_page(self, page: int, point: QPointF) -> tuple[float, float]:
        """A viewport point -> PDF points on a page."""
        rect, crop, s = self._rects[page], self._crops[page], self._page_scale(page)
        x = point.x() + self.horizontalScrollBar().value() - rect.left()
        y = point.y() + self.verticalScrollBar().value() - rect.top()
        return crop[0] + x / s, crop[1] + y / s

    def page_rect(self, page: int) -> QRectF:
        """Where a page's image is, in viewport coordinates."""
        h, v = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
        return QRectF(self._rects[page]).translated(-h, -v)

    def crop_box(self, page: int) -> Box:
        """The part of a page that is shown, in PDF points."""
        return self._crops[page]

    def cached_image(self, page: int) -> QImage | None:
        """The page's sharp image if one is cached at the current zoom, size and theme."""
        entry = self._cache.get(page)
        return entry.image if entry is not None and entry.key == self._key() else None

    def preview_image(self, page: int) -> QImage | None:
        """The page's preview in the current theme, if there is one."""
        entry = self._previews.get(page)
        return entry.image if entry is not None and entry.key == self._key().for_preview() else None

    def _page_scale(self, page: int) -> float:
        return self._scale * self._fits[page]

    def _span(self, boxes: list[tuple[int, Box]]) -> QRectF | None:
        """The viewport rect around some (page, box) boxes."""
        rects = [self.map_box(page, box) for page, box in boxes if 0 <= page < len(self._rects)]
        if not rects:
            return None
        rect = rects[0]
        for other in rects[1:]:
            rect = rect.united(other)
        return rect

    def _locate(self, block: int, char: int) -> tuple[int, Box | None] | None:
        """The page and box of the word holding a char (or the block's page)."""
        if self._book is None or not self._rects or not self._book.blocks:
            return None
        block = max(0, min(block, len(self._book.blocks) - 1))
        words = self._book.blocks[block].words
        if words:
            word = next((w for w in words if w[6] > char), words[-1])
            if 0 <= word[0] < len(self._rects):
                return word[0], word[1:5]
        pages = [p for p in self._block_pages[: block + 1] if p >= 0]
        page = pages[-1] if pages else 0
        return (page, None) if page < len(self._rects) else None

    def _follow_horizontally(self, span: QRectF) -> None:
        """Zoomed past the width: bring a line's start into view if it's off to a side."""
        bar = self.horizontalScrollBar()
        width = self.viewport().width()
        if bar.maximum() > 0 and (span.left() < 0 or span.left() > width * 0.8):
            bar.setValue(int(bar.value() + span.left() - width * 0.08))

    # ---------- layout ----------

    def _key(self) -> _Key:
        return _Key(self._serial, round(self._scale, 6), self.viewport().devicePixelRatioF(), page_tone(self._theme))

    def _relayout(self, *, keep_anchor: bool) -> None:
        anchor = self._anchor() if keep_anchor else None
        self.stop_scrolling()
        width = max(1, self.viewport().width())
        left, right = self._margins
        typical = max((c[2] - c[0]) * f for c, f in zip(self._crops, self._fits)) if self._crops else 1.0
        room = max(40, width - left - right)
        if self.paged():
            self._layout_spreads(typical, width, room)
        else:
            self._scale = max(0.02, room / typical) * self._zoom
            sizes = [self._job_size(i, self._scale) for i in range(len(self._crops))]
            content_w = max([width] + [w + left + right for w, _ in sizes])
            rects, tops, y = [], [], EDGE
            for w, h in sizes:
                rects.append(QRect(left + (content_w - left - right - w) // 2, y, w, h))
                tops.append(y)
                y += h + PAGE_GAP
            self._rects, self._tops = rects, tops
            self._content = (content_w, y - PAGE_GAP + EDGE if rects else 0)
        self._update_scrollbars()
        if anchor is not None:
            self._restore_anchor(anchor)
        if self._render_key is not None and self._key() != self._render_key:
            self._render_timer.start()  # stretch the old images until the sharp ones are worth rendering
        self._schedule()
        self.viewport().update()

    def _layout_spreads(self, typical_width: float, width: int, room: int) -> None:
        """Paged: each spread of one or two pages fitted whole into the view, one viewport high."""
        per = self._per_spread()
        height = max(1, self.viewport().height())
        heights = sorted((c[3] - c[1]) * f for c, f in zip(self._crops, self._fits)) if self._crops else [1.0]
        typical_height = heights[len(heights) // 2]
        across = (room - PAGE_GAP * (per - 1)) / per / typical_width
        down = (height - 2 * EDGE) / typical_height
        self._scale = max(0.02, min(across * self._zoom, down))
        sizes = [self._job_size(i, self._scale) for i in range(len(self._crops))]
        rects, tops = [], []
        left = self._margins[0]
        for first in range(0, len(sizes), per):
            group = sizes[first : first + per]
            top = first // per * height
            used = sum(w for w, _ in group) + PAGE_GAP * (len(group) - 1)
            x = left + (room - used) // 2
            for w, h in group:
                rects.append(QRect(x, top + max(EDGE, (height - h) // 2), w, h))
                tops.append(top)
                x += w + PAGE_GAP
        self._rects, self._tops = rects, tops
        spreads = -(-len(sizes) // per)
        self._content = (width, spreads * height)

    def _update_scrollbars(self) -> None:
        vw, vh = self.viewport().width(), self.viewport().height()
        v, h = self.verticalScrollBar(), self.horizontalScrollBar()
        v.setRange(0, max(0, self._content[1] - vh))
        v.setPageStep(max(1, vh if self.paged() else vh - 40))
        v.setSingleStep(40)
        h.setRange(0, max(0, self._content[0] - vw))
        h.setPageStep(max(1, vw))
        h.setSingleStep(40)

    def _anchor(self) -> tuple[int, float, float] | None:
        """The page at the middle of the view and where in it (fractions), to keep
        the reader's place across a zoom or resize."""
        if not self._rects:
            return None
        cx = self.horizontalScrollBar().value() + self.viewport().width() / 2
        cy = self.verticalScrollBar().value() + self.viewport().height() / 2
        page = max(0, bisect.bisect_right(self._tops, cy) - 1)
        rect = self._rects[page]
        return page, (cx - rect.left()) / rect.width(), (cy - rect.top()) / rect.height()

    def _restore_anchor(self, anchor: tuple[int, float, float]) -> None:
        page, fx, fy = anchor
        page = min(page, len(self._rects) - 1)
        if self.paged():
            self.verticalScrollBar().setValue(page // self._per_spread() * self.spread_step())
            return
        rect = self._rects[page]
        self.horizontalScrollBar().setValue(int(rect.left() + fx * rect.width() - self.viewport().width() / 2))
        self.verticalScrollBar().setValue(int(rect.top() + fy * rect.height() - self.viewport().height() / 2))

    def _visible_pages(self, value: int | None = None) -> range:
        """The pages on screen at a scroll value (now, by default)."""
        if not self._rects:
            return range(0)
        v = self.verticalScrollBar().value() if value is None else value
        if self.paged():
            per = self._per_spread()
            first = min(len(self._rects) - 1, v // self.spread_step() * per)
            return range(first, min(len(self._rects), first + per))
        first = max(0, bisect.bisect_right(self._tops, v) - 1)
        last = max(first, bisect.bisect_right(self._tops, v + self.viewport().height()) - 1)
        if self._rects[first].bottom() < v and first + 1 < len(self._rects):  # in the gap above a page
            first += 1
        return range(first, min(last, len(self._rects) - 1) + 1)

    # ---------- rendering ----------

    def _sharpen(self) -> None:
        """Render at the current zoom, size and theme from now on."""
        self._render_key = self._key()
        self._schedule()

    def _schedule(self) -> None:
        """Tell the worker which pages to render, most wanted first: previews of the
        pages where the view is heading (the end of a scroll animation after a jump),
        then those sharp, then previews of pages passed on the way, then neighbours."""
        worker = self._render.worker
        if worker is None or self._render_key is None or not self._rects:
            return
        target = self.scroll_target()
        middle = target + self.viewport().height() / 2
        heading = sorted(self._visible_pages(target), key=lambda p: abs(self._rects[p].center().y() - middle))
        passing = [p for p in self._visible_pages() if p not in heading]
        last, first = max(heading), min(heading)
        around = [p for d in range(1, PREFETCH + 1) for p in (last + d, first - d) if 0 <= p < len(self._rects)]
        jobs = (
            [self._preview_job(p) for p in heading]
            + [self._sharp_job(p) for p in heading]
            + [self._preview_job(p) for p in passing]
            + [self._sharp_job(p) for p in around]
        )
        worker.set_jobs([job for job in jobs if job is not None])

    def _job_size(self, page: int, scale: float) -> tuple[int, int]:
        """A page's size in logical px at a scale."""
        crop, fit = self._crops[page], self._fits[page]
        return max(1, round((crop[2] - crop[0]) * scale * fit)), max(1, round((crop[3] - crop[1]) * scale * fit))

    def _sharp_job(self, page: int) -> _Job | None:
        key = self._render_key
        entry = self._cache.get(page)
        if (entry is not None and entry.key == key) or (page, key) in self._failed:
            return None
        w, h = self._job_size(page, key.scale)
        device = min(key.dpr, (MAX_PAGE_PIXELS / (w * h)) ** 0.5)
        preview = self._job_size(page, key.scale * PREVIEW_SCALE)
        return _Job(page, key, self._crops[page], max(1, round(w * device)), max(1, round(h * device)), preview)

    def _preview_job(self, page: int) -> _Job | None:
        key = self._render_key.for_preview()
        entry, preview = self._cache.get(page), self._previews.get(page)
        if (entry is not None and entry.key == self._render_key) or (preview is not None and preview.key == key):
            return None
        if (page, key) in self._failed:
            return None
        return _Job(page, key, self._crops[page], *self._job_size(page, self._render_key.scale * PREVIEW_SCALE))

    def _on_rendered(self, page: int, key: _Key, image: QImage, toned: bool) -> None:
        worker = self._render.worker
        if key.serial != self._serial or worker is None:
            return  # a page of the previous book
        worker.done(page, key)
        if key.preview:
            self._previews[page] = _Entry(key, image, toned)
            self._previews.move_to_end(page)
            while len(self._previews) > PREVIEW_PAGES:
                self._previews.popitem(last=False)
        else:
            if key != self._render_key and page in self._cache:
                return  # outdated, and the page already has something to show
            self._cache[page] = _Entry(key, image, toned)
            self._cache.move_to_end(page)
            self._evict()
        self.viewport().update()

    def _on_failed(self, page: int, key: _Key) -> None:
        worker = self._render.worker
        if key.serial == self._serial and worker is not None:
            worker.done(page, key)
            self._failed.add((page, key))

    def _evict(self) -> None:
        keep = set(self._visible_pages())
        total = sum(e.nbytes for e in self._cache.values())
        for page in list(self._cache):
            if len(self._cache) <= CACHE_PAGES and total <= CACHE_BYTES:
                break
            if page not in keep:
                total -= self._cache.pop(page).nbytes

    # ---------- Qt events ----------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._book is not None:
            self._relayout(keep_anchor=True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule()

    def hideEvent(self, event) -> None:
        """Swapped out for the other layout: give the page images' memory back
        (not when the window is just minimized)."""
        super().hideEvent(event)
        if not event.spontaneous():
            self._cache.clear()
            self._previews.clear()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self._schedule()
        self.viewport().update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        bar = self.verticalScrollBar()
        if event.key() in (Qt.Key.Key_Home, Qt.Key.Key_End) and not event.modifiers() and not self.paged():
            self._scrolled_by_hand()
            bar.setValue(bar.minimum() if event.key() == Qt.Key.Key_Home else bar.maximum())
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.fillRect(event.rect(), QColor(self._theme.window))
        h, v = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
        key, dpr = self._key(), self.viewport().devicePixelRatioF()
        highlighted = {band.page for band in self._bands}
        for page in self._visible_pages():
            rect = QRectF(self._rects[page]).translated(-h, -v)
            self._paint_frame(painter, rect)
            shown = self._shown(page, key)
            if shown is None:
                painter.fillRect(rect, QColor(self._theme.page))
            elif shown.key == key and shown.image.width() >= rect.width() * dpr - 1:
                # Rendered at device resolution, so drawn 1:1 (MuPDF may round a size up a pixel).
                image = shown.image
                source = QRectF(0, 0, min(image.width(), rect.width() * dpr), min(image.height(), rect.height() * dpr))
                painter.drawImage(QRectF(rect.topLeft(), source.size() / dpr), image, source)
                self._cache.move_to_end(page)
            else:  # a preview, an older zoom or theme (or a capped huge zoom), stretched
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                painter.drawImage(rect, shown.image, QRectF(shown.image.rect()))
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            if page in highlighted:
                self._paint_bands(painter, page, rect, shown)
        painter.end()

    def _shown(self, page: int, key: _Key) -> _Entry | None:
        """The best image of a page there is: sharp and current, else whichever of
        the sharp one (an older zoom) and the preview is in the current theme."""
        sharp, preview = self._cache.get(page), self._previews.get(page)
        if sharp is not None and (sharp.key.tone == key.tone or preview is None or preview.key.tone != key.tone):
            return sharp
        return preview

    def _paint_frame(self, painter: QPainter, rect: QRectF) -> None:
        """A soft shadow under the page (a hairline on dark themes)."""
        painter.setPen(Qt.PenStyle.NoPen)
        if self._theme.dark:
            painter.setBrush(QColor(self._theme.line))
            painter.drawRect(rect.adjusted(-1, -1, 1, 1))
        else:
            for spread, alpha in ((3, 10), (2, 14), (1, 26)):
                painter.setBrush(QColor(0, 0, 0, alpha))
                painter.drawRoundedRect(rect.adjusted(-spread, -spread + 1, spread, spread + 1), spread, spread)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_bands(self, painter: QPainter, page: int, rect: QRectF, shown: _Entry | None) -> None:
        path = QPainterPath()
        for band in self._bands:
            if band.page == page:
                box = self.map_box(page, band.box)
                radius = min(box.height() * 0.22, box.height() / 2, box.width() / 2)
                path.addRoundedRect(box, radius, radius)
        path = path.simplified()  # overlapping bands (a drop cap) blend once
        color, mode = band_blend(self._theme, as_printed=shown is not None and not shown.toned)
        painter.save()
        painter.setClipRect(rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(mode)
        painter.fillPath(path, color)
        painter.restore()


def _read_page_sizes(path: str) -> list[PageInfo]:
    """Page sizes straight from the PDF, for a book saved without them."""
    try:
        with pymupdf.open(path) as doc:
            return [PageInfo(page.rect.width, page.rect.height) for page in doc]
    except Exception:
        return []
