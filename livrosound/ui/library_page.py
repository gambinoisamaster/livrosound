"""The library: every book the user has opened, as covers in a grid or rows in a list.

Why: "Go to Library" (the landing page) replaces the old Resume dialog. It is
modelled on the user's Readest screenshots: a search field, + to upload, a ⋯
menu (List / Grid, Columns, Group by …, Sort by …, Ascending / Descending,
Then by …) and a ☰ menu with the app's own options (Open Last Book on Start,
Auto Import on File Open, Always Show Status Bar, Auto Mode, Settings…, About).
Select turns clicks into choosing books, with a floating bar of actions (Open,
Collection, Tag, Details, Remove), like Readest's.

The page owns no files: it reads and edits the BookRecords of the window's one
Library (tags and collections) and the Settings (view, grouping, sorting), then
emits `changed` so the window saves them. Opening and removing books are the
window's (open_requested, remove_requested): it closes the open book first and
asks before removing.

BookShelf paints the books itself (a QAbstractScrollArea, like PageView):
sections with a header when grouped, then tiles (grid) or rows (list). Covers
come from bookinfo.cover_path (cover.jpg, loaded once per file version, scaled
for the tile); a book without one gets a drawn cover in the reading serif.
Ordering and grouping are shelf.py's.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QActionGroup,
    QColor,
    QFont,
    QFontMetrics,
    QImageReader,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .. import bookinfo, shelf
from ..library import BookRecord, Library, Settings
from . import theme as themes
from .theme import ACCENT, ON_ACCENT, reading_font, ui_font

COVER_COLORS = ("#4F7942", "#1A4FA8", "#6B4E8A", "#9A4E2E", "#2F5357", "#7A5C2E", "#8A3B52")
MIN_TILE = 132  # px: the narrowest a cover gets when the columns are automatic
MAX_TILE = 210
COVER_ASPECT = 1.5  # height / width of a tile's cover box
TILE_TEXT = 46  # px under the cover: the title and the progress line
GRID_SPACING = (24, 28)  # px between tiles (across, down)
SIDE = 32  # px left and right of the shelf
HEADER_HEIGHT = 44
ROW_HEIGHT = 78
MAX_COLUMNS = 12

APP_OPTIONS = (
    ("open_last_book", "Open Last Book on Start"),
    ("auto_import", "Auto Import on File Open"),
    ("always_show_status_bar", "Always Show Status Bar"),
    ("auto_mode", "Auto Mode"),
)


def _menu(parent: QWidget) -> QMenu:
    menu = QMenu(parent)
    menu.setFont(ui_font(13))
    menu.setToolTipsVisible(True)
    return menu


def cover_color(book_id: str) -> QColor:
    return QColor(COVER_COLORS[sum(map(ord, book_id)) % len(COVER_COLORS)])


def paint_drawn_cover(p: QPainter, rect: QRectF, record: BookRecord) -> None:
    """A cover for a book without one: its color, the title in the reading serif, the author."""
    color = cover_color(record.book_id)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0, color.lighter(112))
    gradient.setColorAt(1, color.darker(118))
    path = QPainterPath()
    path.addRoundedRect(rect, 4, 4)
    p.fillPath(path, gradient)
    p.fillRect(QRectF(rect.left(), rect.top(), max(3.0, rect.width() * 0.04), rect.height()), color.darker(135))
    pad = rect.width() * 0.11
    inner = rect.adjusted(pad, rect.height() * 0.12, -pad * 0.8, -rect.height() * 0.1)
    title_font = reading_font(10, QFont.Weight.DemiBold)
    title_font.setPixelSize(max(8, round(rect.width() * 0.13)))
    p.setFont(title_font)
    p.setPen(QColor("#FFFFFF"))
    flags = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap
    title_box = QRectF(inner.left(), inner.top(), inner.width(), inner.height() * 0.62)
    p.drawText(title_box, flags, _clip_lines(record.title or "Untitled", title_font, title_box))
    if record.author:
        author_font = ui_font(9)
        author_font.setPixelSize(max(7, round(rect.width() * 0.075)))
        p.setFont(author_font)
        p.setPen(QColor(255, 255, 255, 215))
        author_box = QRectF(inner.left(), inner.top() + inner.height() * 0.66, inner.width(), inner.height() * 0.24)
        p.drawText(author_box, flags, _clip_lines(record.author, author_font, author_box))
    label_font = ui_font(8, QFont.Weight.DemiBold)
    label_font.setPixelSize(max(6, round(rect.width() * 0.06)))
    p.setFont(label_font)
    p.setPen(QColor(255, 255, 255, 170))
    p.drawText(QRectF(rect.left(), rect.bottom() - rect.height() * 0.1, rect.width(), rect.height() * 0.08),
               Qt.AlignmentFlag.AlignCenter, record.source_kind.upper())


def _clip_lines(text: str, font: QFont, box: QRectF) -> str:
    """As much of text as fits the box's lines, the last one ending in "…"."""
    metrics = QFontMetrics(font)
    lines = max(1, int(box.height() // metrics.lineSpacing()))
    words, out, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if metrics.horizontalAdvance(trial) <= box.width() or not line:
            line = trial
        else:
            out.append(line)
            line = word
    out.append(line)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = metrics.elidedText(out[-1] + " …", Qt.TextElideMode.ElideRight, int(box.width()))
    return "\n".join(out)


class CoverCache:
    """Cover images by book, loaded from cover.jpg once per version of the file and
    scaled once per size."""

    def __init__(self):
        self._images: dict[str, tuple[int, QPixmap | None]] = {}
        self._scaled: dict[tuple[str, int, int, float], QPixmap] = {}

    def pixmap(self, book_id: str, size: QSize, dpr: float) -> QPixmap | None:
        """The cover scaled to fit size (logical px), or None when the book has none."""
        path = bookinfo.cover_path(book_id)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = -1
        cached = self._images.get(book_id)
        if cached is None or cached[0] != stamp:
            original = None
            if stamp >= 0:
                reader = QImageReader(str(path))
                image = reader.read()
                original = QPixmap.fromImage(image) if not image.isNull() else None
            self._images[book_id] = cached = (stamp, original)
            for key in [k for k in self._scaled if k[0] == book_id]:
                del self._scaled[key]
        original = cached[1]
        if original is None:
            return None
        key = (book_id, size.width(), size.height(), dpr)
        scaled = self._scaled.get(key)
        if scaled is None:
            device = QSize(round(size.width() * dpr), round(size.height() * dpr))
            scaled = original.scaled(device, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            self._scaled[key] = scaled
        return scaled

    def forget(self, book_id: str) -> None:
        self._images.pop(book_id, None)


@dataclass
class _Item:
    kind: str  # "header" | "book"
    rect: QRect  # content coordinates
    text: str = ""  # header label
    record: BookRecord | None = None


class BookShelf(QAbstractScrollArea):
    """The books, painted: section headers, then a grid of covers or a list of rows."""

    open_requested = Signal(str)
    selection_changed = Signal()
    menu_requested = Signal(str, QPoint)  # book id, global position

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("bookShelf")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Always there: a bar that comes and goes changes the shelf's width, which changes how
        # many covers fit a row, which changes whether the bar is needed — the covers would
        # jump back and forth for ever.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.viewport().setMouseTracking(True)
        self.covers = CoverCache()
        self._sections: list[tuple[str, list[BookRecord]]] = []
        self._view = "grid"
        self._columns = 0
        self._items: list[_Item] = []
        self._height = 0
        self._selecting = False
        self._selected: set[str] = set()
        self._hover = ""
        self._current = ""  # the book Return opens

    # ---------- content ----------

    def set_sections(self, sections: list[tuple[str, list[BookRecord]]], view: str, columns: int) -> None:
        self._sections, self._view, self._columns = sections, view, columns
        shown = {r.book_id for _, records in sections for r in records}
        if self._selected - shown:
            self._selected &= shown
            self.selection_changed.emit()
        self._relayout()

    def books(self) -> list[BookRecord]:
        """The books shown, in order (a book in two groups once)."""
        seen, out = set(), []
        for _, records in self._sections:
            for r in records:
                if r.book_id not in seen:
                    seen.add(r.book_id)
                    out.append(r)
        return out

    def columns(self) -> int:
        """How many tiles a grid row holds now."""
        return self._grid_metrics()[0]

    def item_rect(self, book_id: str) -> QRect | None:
        """Where a book's (first) tile or row is, in viewport coordinates."""
        for item in self._items:
            if item.kind == "book" and item.record.book_id == book_id:
                return item.rect.translated(0, -self.verticalScrollBar().value())
        return None

    def headers(self) -> list[str]:
        return [item.text for item in self._items if item.kind == "header"]

    # ---------- selection ----------

    def set_selecting(self, on: bool) -> None:
        self._selecting = on
        if not on:
            self._selected.clear()
        self.selection_changed.emit()
        self.viewport().update()

    def selecting(self) -> bool:
        return self._selecting

    def selected(self) -> list[str]:
        """Selected book ids, in shelf order."""
        return [r.book_id for r in self.books() if r.book_id in self._selected]

    def select_all(self) -> None:
        self._selected = {r.book_id for r in self.books()}
        self.selection_changed.emit()
        self.viewport().update()

    def toggle(self, book_id: str) -> None:
        self._selected ^= {book_id}
        self.selection_changed.emit()
        self.viewport().update()

    # ---------- layout ----------

    def _grid_metrics(self) -> tuple[int, float]:
        """(columns, tile width) for the viewport's width."""
        available = max(1, self.viewport().width() - 2 * SIDE)
        gap = GRID_SPACING[0]
        if self._columns > 0:
            columns = self._columns
        else:
            columns = max(1, int((available + gap) // (MIN_TILE + gap)))
            columns = min(columns, max(1, math.ceil((available + gap) / (MAX_TILE + gap))) + 1)
        width = (available - (columns - 1) * gap) / columns
        return columns, max(60.0, width)

    def _relayout(self) -> None:
        items: list[_Item] = []
        y = 14
        width = self.viewport().width()
        columns, tile = self._grid_metrics()
        tile_height = round(tile * COVER_ASPECT) + TILE_TEXT
        for label, records in self._sections:
            if label:
                items.append(_Item("header", QRect(SIDE, y, width - 2 * SIDE, HEADER_HEIGHT), f"{label}  ·  {len(records)}"))
                y += HEADER_HEIGHT
            if self._view == "list":
                for record in records:
                    items.append(_Item("book", QRect(SIDE - 10, y, width - 2 * SIDE + 20, ROW_HEIGHT), record=record))
                    y += ROW_HEIGHT
                y += 10
            else:
                for i, record in enumerate(records):
                    row, col = divmod(i, columns)
                    x = SIDE + col * (tile + GRID_SPACING[0])
                    top = y + row * (tile_height + GRID_SPACING[1])
                    items.append(_Item("book", QRect(round(x), top, round(tile), tile_height), record=record))
                rows = math.ceil(len(records) / columns)
                y += rows * (tile_height + GRID_SPACING[1]) + 6
        self._items, self._height = items, y + 24
        bar = self.verticalScrollBar()
        bar.setRange(0, max(0, self._height - self.viewport().height()))
        bar.setPageStep(self.viewport().height())
        bar.setSingleStep(40)
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.viewport().update()

    # ---------- events ----------

    def book_at(self, pos: QPoint) -> str:
        y = pos.y() + self.verticalScrollBar().value()
        for item in self._items:
            if item.kind == "book" and item.rect.contains(pos.x(), y):
                return item.record.book_id
        return ""

    def mousePressEvent(self, event) -> None:
        book_id = self.book_at(event.position().toPoint())
        if not book_id:
            return
        self._current = book_id
        if event.button() == Qt.MouseButton.RightButton:
            self.menu_requested.emit(book_id, event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._selecting or event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if not self._selecting:
                self._selecting = True
            self.toggle(book_id)
        else:
            self.open_requested.emit(book_id)

    def mouseMoveEvent(self, event) -> None:
        book_id = self.book_at(event.position().toPoint())
        if book_id != self._hover:
            self._hover = book_id
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if book_id else Qt.CursorShape.ArrowCursor)
            self.viewport().update()

    def leaveEvent(self, event) -> None:
        self._hover = ""
        self.viewport().update()
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._current:
            self.open_requested.emit(self._current)
            return
        super().keyPressEvent(event)

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        t = themes.current()
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(event.rect(), QColor(t.panel))
        v = self.verticalScrollBar().value()
        visible = QRect(0, v, self.viewport().width(), self.viewport().height())
        dpr = self.viewport().devicePixelRatioF()
        for item in self._items:
            if not item.rect.intersects(visible):
                continue
            rect = item.rect.translated(0, -v)
            if item.kind == "header":
                self._paint_header(p, rect, item.text, t)
            elif self._view == "list":
                self._paint_row(p, rect, item.record, t, dpr)
            else:
                self._paint_tile(p, rect, item.record, t, dpr)
        if not self._items:
            p.setPen(QColor(t.muted))
            p.setFont(ui_font(15))
            p.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, "No books here.")
        p.end()

    def _paint_header(self, p: QPainter, rect: QRect, text: str, t) -> None:
        p.setPen(QColor(t.text))
        p.setFont(ui_font(15, QFont.Weight.DemiBold))
        p.drawText(rect.adjusted(0, 10, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        p.fillRect(QRect(rect.left(), rect.bottom(), rect.width(), 1), QColor(t.hairline))

    def _paint_cover(self, p: QPainter, box: QRectF, record: BookRecord, t, dpr: float, *, bottom: bool) -> QRectF:
        """The cover, fitted in box (aligned to its bottom on the grid); returns where it went."""
        pixmap = self.covers.pixmap(record.book_id, box.size().toSize(), dpr)
        if pixmap is not None:
            size = pixmap.deviceIndependentSize()
            x = box.left() + (box.width() - size.width()) / 2
            y = box.bottom() - size.height() if bottom else box.top() + (box.height() - size.height()) / 2
            rect = QRectF(x, y, size.width(), size.height())
        else:
            rect = box
        self._paint_shadow(p, rect, t)
        if pixmap is not None:
            path = QPainterPath()
            path.addRoundedRect(rect, 3, 3)
            p.save()
            p.setClipPath(path)
            p.drawPixmap(rect.topLeft(), pixmap)
            p.restore()
        else:
            paint_drawn_cover(p, rect, record)
        return rect

    @staticmethod
    def _paint_shadow(p: QPainter, rect: QRectF, t) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        if t.dark:
            p.setBrush(QColor(255, 255, 255, 30))
            p.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 4, 4)
        else:
            for spread, alpha in ((4, 8), (2, 16), (1, 30)):
                p.setBrush(QColor(0, 0, 0, alpha))
                p.drawRoundedRect(rect.adjusted(-spread, -spread + 2, spread, spread + 2), 4 + spread, 4 + spread)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _progress_text(self, record: BookRecord) -> str:
        fraction = shelf.read_fraction(record)
        if shelf.status(record) == "Not Started":
            return "New"
        return f"{max(1, math.floor(fraction * 100)) if fraction > 0 else 0}%"

    @staticmethod
    def _audio_text(record: BookRecord) -> str:
        if record.generated_fraction >= 1:
            return "♪ ready"
        percent = math.floor(record.generated_fraction * 100)
        return f"♪ {percent}%" if percent > 0 else ""

    def _paint_tile(self, p: QPainter, rect: QRect, record: BookRecord, t, dpr: float) -> None:
        cover_box = QRectF(rect.left(), rect.top(), rect.width(), rect.height() - TILE_TEXT)
        if record.book_id == self._hover and not self._selecting:
            cover_box.translate(0, -3)
        cover = self._paint_cover(p, cover_box, record, t, dpr, bottom=True)
        title_font = ui_font(12, QFont.Weight.Medium)
        p.setFont(title_font)
        p.setPen(QColor(t.text))
        text_top = rect.bottom() - TILE_TEXT + 8
        title = QFontMetrics(title_font).elidedText(record.title or "Untitled", Qt.TextElideMode.ElideRight, rect.width())
        p.drawText(QRect(rect.left(), text_top, rect.width(), 18), Qt.AlignmentFlag.AlignLeft, title)
        p.setFont(ui_font(10.5))
        p.setPen(QColor(t.muted))
        line = QRect(rect.left(), text_top + 19, rect.width(), 16)
        p.drawText(line, Qt.AlignmentFlag.AlignLeft, self._progress_text(record))
        p.drawText(line, Qt.AlignmentFlag.AlignRight, self._audio_text(record))
        if self._selecting:
            self._paint_check(p, QRectF(cover.right() - 26, cover.bottom() - 26, 20, 20), record.book_id in self._selected)
            if record.book_id in self._selected:
                p.setPen(QPen(QColor(ACCENT), 3))
                p.drawRoundedRect(cover.adjusted(-3, -3, 3, 3), 5, 5)

    def _paint_row(self, p: QPainter, rect: QRect, record: BookRecord, t, dpr: float) -> None:
        if record.book_id == self._hover or record.book_id in self._selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.highlight if record.book_id in self._selected else t.raised_hover))
            p.drawRoundedRect(QRectF(rect).adjusted(0, 3, 0, -3), 10, 10)
        left = rect.left() + 10
        if self._selecting:
            self._paint_check(p, QRectF(left, rect.center().y() - 10, 20, 20), record.book_id in self._selected)
            left += 32
        box = QRectF(left, rect.top() + 9, (rect.height() - 18) / COVER_ASPECT, rect.height() - 18)
        self._paint_cover(p, box, record, t, dpr, bottom=False)
        x = int(box.right()) + 16
        right_width = 230
        text_width = max(60, rect.right() - x - right_width - 14)
        title_font = ui_font(14, QFont.Weight.DemiBold)
        p.setFont(title_font)
        p.setPen(QColor(t.text))
        title = QFontMetrics(title_font).elidedText(record.title or "Untitled", Qt.TextElideMode.ElideRight, text_width)
        p.drawText(QRect(x, rect.top() + 14, text_width, 20), Qt.AlignmentFlag.AlignLeft, title)
        p.setFont(ui_font(12))
        p.setPen(QColor(t.muted))
        author = QFontMetrics(ui_font(12)).elidedText(record.author, Qt.TextElideMode.ElideRight, text_width)
        p.drawText(QRect(x, rect.top() + 35, text_width, 18), Qt.AlignmentFlag.AlignLeft, author)
        details = [record.source_kind.upper()]
        if record.pages:
            details.append(f"{record.pages} pages")
        if record.series:
            details.append(record.series + (f" #{record.series_index:g}" if record.series_index else ""))
        p.setFont(ui_font(11))
        p.drawText(QRect(x, rect.top() + 54, text_width, 16), Qt.AlignmentFlag.AlignLeft, " · ".join(details))
        right = QRect(rect.right() - right_width - 10, rect.top(), right_width, rect.height())
        p.setFont(ui_font(12, QFont.Weight.Medium))
        p.setPen(QColor(t.text))
        progress = self._progress_text(record)
        p.drawText(right.adjusted(0, 16, 0, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                   progress if progress == "New" else f"{progress} read")
        p.setFont(ui_font(11))
        p.setPen(QColor(t.muted))
        remaining = shelf.seconds_remaining(record)
        info = [self._audio_text(record)]
        if remaining is not None and shelf.status(record) != "Finished":
            info.append(f"{shelf.duration_text(remaining)} left")
        p.drawText(right.adjusted(0, 38, 0, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                   "  ·  ".join(i for i in info if i))

    @staticmethod
    def _paint_check(p: QPainter, rect: QRectF, on: bool) -> None:
        p.setPen(QPen(QColor("#FFFFFF"), 1.6))
        p.setBrush(QColor(ACCENT) if on else QColor(0, 0, 0, 90))
        p.drawEllipse(rect)
        if on:
            p.setPen(QPen(QColor(ON_ACCENT), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            c = rect.center()
            p.drawPolyline(QPolygonF([c + QPointF(-5, 0), c + QPointF(-1, 4), c + QPointF(5, -4)]))
        p.setBrush(Qt.BrushStyle.NoBrush)


class LibraryPage(QWidget):
    """The library page; the main window answers its signals and saves after `changed`."""

    open_requested = Signal(str)
    upload_requested = Signal()
    home_requested = Signal()
    back_to_book_requested = Signal()
    remove_requested = Signal(list)  # book ids
    details_requested = Signal(str)
    reveal_requested = Signal(str)  # Show in Finder
    settings_requested = Signal()
    about_requested = Signal()
    option_toggled = Signal(str, bool)  # one of APP_OPTIONS' settings
    changed = Signal()  # records or settings edited: save them

    def __init__(self, library: Library, settings: Settings, parent=None):
        super().__init__(parent)
        self.setObjectName("libraryPage")
        self.library, self.settings = library, settings
        # A one-line question (title, label, names in use) -> answer; tests answer instead.
        self.ask_text: Callable[[str, str, list[str]], str | None] = (
            lambda title, label, names: _ask_text(self.window(), title, label, names)
        )

        bar = QHBoxLayout()
        bar.setContentsMargins(SIDE - 6, 16, SIDE - 6, 6)
        bar.setSpacing(8)
        self.home_button = self._tool("‹  Home", "Back to the start page", self.home_requested.emit)
        self.book_button = self._tool("Back to Book", "Return to the book you're reading", self.back_to_book_requested.emit)
        self.book_button.hide()
        self.search = QLineEdit()
        self.search.setObjectName("librarySearch")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(32)
        self.search.setFont(ui_font(13))
        self.search.textChanged.connect(lambda _text: self.refresh())
        self.add_button = self._tool("+", "Upload a new book (⌘O)", self.upload_requested.emit)
        self.select_button = self._tool("Select", "Choose books to open, sort into collections or remove", self._toggle_selecting)
        self.select_all_button = self._tool("Select All", "", self._select_all)
        self.select_all_button.hide()
        self.view_button = self._tool("⋯", "View, group and sort", None)
        self.view_button.clicked.connect(lambda: self._popup(self.view_menu(), self.view_button))
        self.app_button = self._tool("☰", "Settings and options", None)
        self.app_button.clicked.connect(lambda: self._popup(self.app_menu(), self.app_button))
        bar.addWidget(self.home_button)
        bar.addWidget(self.book_button)
        bar.addWidget(self.search, 1)
        for button in (self.add_button, self.select_button, self.select_all_button, self.view_button, self.app_button):
            bar.addWidget(button)

        heading = QHBoxLayout()
        heading.setContentsMargins(SIDE, 8, SIDE, 2)
        self.title = QLabel("Library")
        self.title.setObjectName("libraryTitle")
        self.title.setFont(ui_font(26, QFont.Weight.Bold))
        self.count = QLabel()
        self.count.setObjectName("caption")
        self.count.setFont(ui_font(13))
        heading.addWidget(self.title)
        heading.addSpacing(10)
        heading.addWidget(self.count, 0, Qt.AlignmentFlag.AlignBottom)
        heading.addStretch(1)

        self.shelf = BookShelf()
        self.shelf.open_requested.connect(self.open_requested.emit)
        self.shelf.selection_changed.connect(self._on_selection)
        self.shelf.menu_requested.connect(lambda book_id, pos: self.context_menu(book_id).exec(pos))

        self.empty = QWidget()
        empty_layout = QVBoxLayout(self.empty)
        empty_text = QLabel("Your library is empty.\nUpload a book to start reading and listening.")
        empty_text.setObjectName("caption")
        empty_text.setFont(ui_font(15))
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_upload = QPushButton("Upload New Book")
        self.empty_upload.setObjectName("primaryButton")
        self.empty_upload.setFixedHeight(32)
        self.empty_upload.clicked.connect(self.upload_requested.emit)
        empty_layout.addStretch(2)
        empty_layout.addWidget(empty_text)
        empty_layout.addSpacing(14)
        empty_layout.addWidget(self.empty_upload, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch(3)

        self.actions_bar = QFrame(self)
        self.actions_bar.setObjectName("selectionBar")
        actions = QHBoxLayout(self.actions_bar)
        actions.setContentsMargins(12, 6, 12, 6)
        actions.setSpacing(4)
        self.action_buttons: dict[str, QToolButton] = {}
        for key, label, slot in (
            ("open", "Open", self._open_selected),
            ("collection", "Collection", self._collection_selected),
            ("tag", "Tag", self._tag_selected),
            ("details", "Details", self._details_selected),
            ("remove", "Remove", lambda: self.remove_requested.emit(self.shelf.selected())),
            ("cancel", "Cancel", lambda: self.shelf.set_selecting(False)),
        ):
            button = QToolButton()
            button.setObjectName("selectionAction")
            button.setText(label)
            button.setFont(ui_font(12, QFont.Weight.Medium))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            actions.addWidget(button)
            self.action_buttons[key] = button
        self.actions_bar.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(bar)
        layout.addLayout(heading)
        layout.addWidget(self.shelf, 1)
        layout.addWidget(self.empty, 1)

        QShortcut(QKeySequence.StandardKey.Find, self, self.search.setFocus)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, lambda: self.shelf.set_selecting(False))
        self.refresh()

    # ---------- what's shown ----------

    def set_book_open(self, title: str) -> None:
        """A book is open in the reader: offer the way back to it ("" = none)."""
        self.book_button.setVisible(bool(title))
        self.book_button.setToolTip(f"Return to “{title}”" if title else "")

    def refresh(self) -> None:
        """Rebuild the shelf from the library and the settings."""
        records = self.library.recent()
        s = self.settings
        query = self.search.text()
        shown = [r for r in records if shelf.matches(r, query)]
        ordered = shelf.sort_records(shown, s.library_sort, s.library_descending, s.library_then, s.library_then_descending)
        sections = shelf.group_records(ordered, s.library_group)
        self.shelf.set_sections(sections, s.library_view, s.library_columns)
        total = len(records)
        self.search.setPlaceholderText(f"Search in {total} book{'' if total == 1 else 's'}…")
        self.count.setText(f"{len(shown)} of {total}" if query else f"{total} book{'' if total == 1 else 's'}")
        empty = total == 0
        self.shelf.setVisible(not empty)
        self.empty.setVisible(empty)
        self.select_button.setEnabled(not empty)

    def cover_changed(self, book_id: str) -> None:
        self.shelf.covers.forget(book_id)
        self.shelf.viewport().update()

    # ---------- menus ----------

    def view_menu(self) -> QMenu:
        """⋯: List / Grid, Columns, Group by, Sort by, Ascending / Descending, Then by."""
        s = self.settings
        menu = _menu(self)
        views = QActionGroup(menu)
        for key, label in (("list", "List"), ("grid", "Grid")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(s.library_view == key)
            action.setActionGroup(views)
            action.triggered.connect(lambda _=False, k=key: self._set("library_view", k))
        menu.addSeparator()
        menu.addSection("Columns")
        menu.addAction(self._columns_row(menu))
        group_menu = menu.addMenu("Group by…")
        self._choices(group_menu, shelf.GROUPS, s.library_group, "library_group")
        sort_menu = menu.addMenu("Sort by…")
        self._choices(sort_menu, shelf.SORTS, s.library_sort, "library_sort")
        sort_menu.addSeparator()
        self._direction(sort_menu, "library_descending")
        then_menu = menu.addMenu("Then by…")
        self._choices(then_menu, {"": "None", **shelf.SORTS}, s.library_then, "library_then")
        then_menu.addSeparator()
        self._direction(then_menu, "library_then_descending")
        return menu

    def _choices(self, menu: QMenu, choices: dict[str, str], current: str, field: str) -> None:
        group = QActionGroup(menu)
        for key, label in choices.items():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(current == key)
            action.setActionGroup(group)
            action.setData(key)
            action.triggered.connect(lambda _=False, k=key: self._set(field, k))

    def _direction(self, menu: QMenu, field: str) -> None:
        group = QActionGroup(menu)
        for label, descending in (("Ascending", False), ("Descending", True)):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(getattr(self.settings, field) == descending)
            action.setActionGroup(group)
            action.triggered.connect(lambda _=False, d=descending: self._set(field, d))

    def _columns_row(self, menu: QMenu) -> QWidgetAction:
        """Columns: [✓ Auto]  N  [−] [+] in one menu row."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(14, 2, 10, 2)
        auto = QToolButton()
        auto.setObjectName("menuToggle")
        auto.setText(("✓ " if self.settings.library_columns == 0 else "") + "Auto")
        auto.clicked.connect(lambda: (self._set("library_columns", 0), menu.close()))
        count = QLabel(str(self.settings.library_columns or self.shelf.columns()))
        count.setObjectName("caption")
        minus, plus = QToolButton(), QToolButton()
        for button, text, step in ((minus, "−", -1), (plus, "+", 1)):
            button.setObjectName("roundButton")
            button.setText(text)
            button.setFixedSize(28, 28)
            button.clicked.connect(lambda _=False, d=step, label=count: self._step_columns(d, label))
        layout.addWidget(auto)
        layout.addStretch(1)
        layout.addWidget(count)
        layout.addWidget(minus)
        layout.addWidget(plus)
        action = QWidgetAction(menu)
        action.setDefaultWidget(row)
        return action

    def _step_columns(self, step: int, label: QLabel | None = None) -> None:
        current = self.settings.library_columns or self.shelf.columns()
        self._set("library_columns", max(1, min(MAX_COLUMNS, current + step)))
        if label is not None:
            label.setText(str(self.settings.library_columns))

    def app_menu(self) -> QMenu:
        """☰: the app's options (Readest's menu), Settings… and About."""
        menu = _menu(self)
        for field, label in APP_OPTIONS:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(bool(getattr(self.settings, field)))
            action.setData(field)
            action.toggled.connect(lambda on, f=field: self._toggle_option(f, on))
            if field == "always_show_status_bar":
                action.setToolTip("Keep the page number and reading progress showing under the book")
            elif field == "auto_mode":
                action.setToolTip("Switch between your light theme and Dark with macOS's appearance")
        menu.addSeparator()
        settings = menu.addAction("Settings…")
        settings.triggered.connect(self.settings_requested.emit)
        menu.addSeparator()
        about = menu.addAction("About LivroSound")
        about.triggered.connect(self.about_requested.emit)
        return menu

    def context_menu(self, book_id: str) -> QMenu:
        """A book's right-click menu."""
        menu = _menu(self)
        menu.addAction("Open").triggered.connect(lambda: self.open_requested.emit(book_id))
        menu.addAction("Details…").triggered.connect(lambda: self.details_requested.emit(book_id))
        menu.addSeparator()
        self._fill_collection_menu(menu.addMenu("Add to Collection"), [book_id])
        menu.addAction("Add Tag…").triggered.connect(lambda: self._tag([book_id]))
        menu.addAction("Show in Finder").triggered.connect(lambda: self.reveal_requested.emit(book_id))
        menu.addSeparator()
        menu.addAction("Remove from Library…").triggered.connect(lambda: self.remove_requested.emit([book_id]))
        return menu

    def _fill_collection_menu(self, menu: QMenu, book_ids: list[str]) -> None:
        records = [r for r in (self.library.get(b) for b in book_ids) if r is not None]
        for name in shelf.all_values(self.library.recent(), "collections"):
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(bool(records) and all(name in shelf.strings(r.collections) for r in records))
            action.toggled.connect(lambda on, n=name: self.set_collection(book_ids, n, on))
        if not menu.isEmpty():
            menu.addSeparator()
        menu.addAction("New Collection…").triggered.connect(lambda: self._new_collection(book_ids))

    # ---------- editing ----------

    def set_collection(self, book_ids: list[str], name: str, on: bool) -> None:
        """Put books in a collection, or take them out."""
        self._edit_list(book_ids, "collections", name, on)

    def add_tag(self, book_ids: list[str], tag: str) -> None:
        self._edit_list(book_ids, "tags", tag, True)

    def _edit_list(self, book_ids: list[str], field: str, value: str, on: bool) -> None:
        value = " ".join(value.split())
        if not value:
            return
        for book_id in book_ids:
            record = self.library.get(book_id)
            if record is None:
                continue
            values = [v for v in shelf.strings(getattr(record, field)) if shelf.fold(v) != shelf.fold(value)]
            if on:
                values.append(value)
            setattr(record, field, values)
        self.changed.emit()
        self.refresh()

    def _new_collection(self, book_ids: list[str]) -> None:
        name = self.ask_text("New Collection", "Name the collection:", shelf.all_values(self.library.recent(), "collections"))
        if name:
            self.set_collection(book_ids, name, True)

    def _tag(self, book_ids: list[str]) -> None:
        tag = self.ask_text("Add a Tag", "Tag:", shelf.all_values(self.library.recent(), "tags"))
        if tag:
            self.add_tag(book_ids, tag)

    def _set(self, field: str, value) -> None:
        setattr(self.settings, field, value)
        self.changed.emit()
        self.refresh()

    def _toggle_option(self, field: str, on: bool) -> None:
        setattr(self.settings, field, on)
        self.changed.emit()
        self.option_toggled.emit(field, on)

    # ---------- selecting ----------

    def _toggle_selecting(self) -> None:
        self.shelf.set_selecting(not self.shelf.selecting())

    def _select_all(self) -> None:
        self.shelf.select_all()

    def _on_selection(self) -> None:
        selecting = self.shelf.selecting()
        chosen = self.shelf.selected()
        self.select_button.setText("Done" if selecting else "Select")
        self.select_all_button.setVisible(selecting)
        self.actions_bar.setVisible(selecting)
        for key, button in self.action_buttons.items():
            button.setEnabled(key == "cancel" or bool(chosen) and (key not in ("open", "details") or len(chosen) == 1))
        self._place_actions()

    def _open_selected(self) -> None:
        chosen = self.shelf.selected()
        if len(chosen) == 1:
            self.shelf.set_selecting(False)
            self.open_requested.emit(chosen[0])

    def _details_selected(self) -> None:
        chosen = self.shelf.selected()
        if len(chosen) == 1:
            self.details_requested.emit(chosen[0])

    def _collection_selected(self) -> None:
        menu = _menu(self)
        self._fill_collection_menu(menu, self.shelf.selected())
        self._popup(menu, self.action_buttons["collection"], above=True)

    def _tag_selected(self) -> None:
        chosen = self.shelf.selected()
        if chosen:
            self._tag(chosen)

    def _place_actions(self) -> None:
        bar = self.actions_bar
        bar.adjustSize()
        bar.move((self.width() - bar.width()) // 2, self.height() - bar.height() - 22)
        bar.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_actions()

    # ---------- helpers ----------

    def _tool(self, text: str, tooltip: str, slot) -> QToolButton:
        button = QToolButton()
        button.setObjectName("libraryTool")
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFont(ui_font(14 if len(text) <= 2 else 13, QFont.Weight.Medium))
        button.setFixedHeight(32)
        button.setMinimumWidth(36)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if slot is not None:
            button.clicked.connect(slot)
        return button

    @staticmethod
    def _popup(menu: QMenu, anchor: QWidget, *, above: bool = False) -> None:
        if above:
            pos = anchor.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 6))
        else:
            pos = anchor.mapToGlobal(QPoint(anchor.width() - menu.sizeHint().width(), anchor.height() + 6))
        QTimer.singleShot(0, anchor, lambda: menu.exec(pos))

    def paintEvent(self, event) -> None:
        QPainter(self).fillRect(self.rect(), QColor(themes.current().panel))


def _ask_text(parent: QWidget, title: str, label: str, suggestions: list[str]) -> str | None:
    """A one-line question with the names already in use offered as completions."""
    from PySide6.QtWidgets import QCompleter, QInputDialog

    dialog = QInputDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setLabelText(label)
    dialog.setInputMode(QInputDialog.InputMode.TextInput)
    edit = dialog.findChild(QLineEdit)
    if edit is not None and suggestions:
        completer = QCompleter(suggestions, dialog)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        edit.setCompleter(completer)
    if dialog.exec():
        return dialog.textValue().strip() or None
    return None
