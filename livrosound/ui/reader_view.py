"""The interface both reader views share, and the scrolling and clicking they share.

LivroSound shows a book in one of two widgets:

    ReflowView  (a QTextEdit)            the LivroSound layout of any book, and the
                                         original layout of EPUB/DOCX/TXT books
    PageView    (a QAbstractScrollArea)  the original pages of a PDF

The main window drives whichever is on screen through ReaderView alone, so
playback, clicks, auto-follow and Contents never care which one it is. Every
position is (block index, char offset into Block.text), as everywhere in the app.

ReaderView is a mixin: list it before the Qt base class
(`class ReflowView(ReaderView, QTextEdit)`), call `_init_reader_view()` at the end
of `__init__`, keep the shown book in `self._book`, and implement the abstract
methods plus `hit_test()`. In return the mixin provides, for both views:

- clicked(block, char) when the left button is pressed and released in place.
  A drag (selecting text) or the second click of a double click is not a click.
- user_scrolled on anything that scrolls by hand: wheel or trackpad, pressing or
  dragging the scroll bar, scroll keys. It also stops a running scroll
  animation, so the reader never fights the auto-follow (Script Radio's rule:
  the main window suspends auto-follow until the next play or seek).
- The player's keys (Space, ← and →) are ignored here so they reach the main
  window, which plays/pauses and skips ±10 s with them.
- A pointing-hand cursor over a line of the book's own contents page
  (Block.link >= 0); `_hover_link_changed()` lets a view decorate it.
- scroll_to_value(): the eased 450 ms auto-scroll of Script Radio's
  _scroll_to_range, and the pure helpers below for where to scroll.

Auto-follow, shared by both views:
- scroll_to brings a block to the middle of the view; when the block is taller
  than FOLLOW_BAND of the view, the highlighted sentence in it (else the line
  holding the char) is centered instead, since centering a long paragraph would
  push the sentence being read off screen.
- keep_in_view re-centers when the sentence leaves the middle FOLLOW_BAND of the view.

Page views (set_page_mode): "scroll" (continuous), "single" or "double" (two
pages side by side). Paged, each view lays its pages out exactly one viewport
high, so a page turn is a jump of spread_step() px of the scroll bar (hidden):
the wheel, ↑/↓, Page Up/Down and Home/End turn pages (one per wheel notch,
with a short pause so a trackpad flick turns one page, not ten), and
auto-follow turns to the page holding the sentence instead of scrolling.

ReadingStyle carries the user's text settings (Settings ▸ Font / Layout):
family, weight, spacing, justification, the margins beside the text and the
widest the column gets. ReflowView applies all of it; PageView, which shows a
PDF's own pages, only the margins.

Marks and selections: set_marks() lays colored spans under the reading
highlight (the reader's own highlights, the current Find match). Dragging
selects text (ReflowView: Qt's own selection; PageView: whole words, across
lines and pages); on release with something selected, selection_made(global
point) asks the window to offer Highlight / Note / Copy / Look Up.
A right click emits context_requested(block, char, global point).

ensure_reading_fonts() makes the book's serif (Iowan Old Style) visible to Qt;
macOS ships it but hides it from apps. ReflowView calls it; the main window
should call it at startup too, so every theme.reading_font() (the toolbar's
book title, the layout previews) gets Iowan rather than the Charter fallback.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QFontDatabase, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication

from livrosound.book import Book
from livrosound.ui.theme import READING_FAMILIES, Theme

MODES = ("original", "livrosound")  # set_book(book, mode)
PAGE_MODES = ("scroll", "single", "double")  # set_page_mode(mode)
WHEEL_NOTCH = 120  # angleDelta of one wheel click: one page turn
TURN_PAUSE_S = 0.35  # after a page turn, the rest of a trackpad flick is ignored
_SYSTEM_READING_FONTS = ("/System/Library/Fonts/Supplemental/Iowan Old Style.ttc",)
FOLLOW_BAND = 0.7  # the middle share of the view the sentence being read stays in
SCROLL_MS = 450
PLAYER_KEYS = (Qt.Key.Key_Space, Qt.Key.Key_Left, Qt.Key.Key_Right)
SCROLL_KEYS = (
    Qt.Key.Key_Up,
    Qt.Key.Key_Down,
    Qt.Key.Key_PageUp,
    Qt.Key.Key_PageDown,
    Qt.Key.Key_Home,
    Qt.Key.Key_End,
)


def ensure_reading_fonts() -> None:
    """Make Iowan Old Style (the book's serif) available to Qt. macOS ships it but
    doesn't list it among the fonts apps can see, so without this
    theme.reading_font() silently falls back to Charter. Call once a QApplication
    exists; later calls do nothing."""
    if READING_FAMILIES[0] in QFontDatabase.families():
        return
    for path in _SYSTEM_READING_FONTS:
        if Path(path).is_file():
            QFontDatabase.addApplicationFont(path)


@dataclass(frozen=True)
class ReadingStyle:
    """How the user wants the text set (Settings ▸ Font and Layout)."""

    family: str = ""  # "" = the reading serif (theme.READING_FAMILIES)
    weight: int = 400  # CSS weight
    line_spacing: float = 1.5  # × the font size
    paragraph_spacing: float = 0.3  # em under a paragraph
    word_spacing: float = 0.0  # px added between words
    letter_spacing: float = 0.0  # px added between letters
    justify: bool = False
    margin_left: int = -1  # px beside the text; -1 = centered
    margin_right: int = -1
    width: int = 85  # % of the window a centered column fills


def centered_scroll_value(current: int, top: int, bottom: int, viewport_height: int, maximum: int) -> int:
    """The scroll value that puts the span [top, bottom] (viewport coordinates at
    scroll value `current`) in the middle of the view, clamped to the scroll range."""
    target = current + (top + bottom) // 2 - viewport_height // 2
    return max(0, min(maximum, target))


def outside_band(top: int, bottom: int, viewport_height: int, band: float = FOLLOW_BAND) -> bool:
    """Has the span [top, bottom] (viewport coordinates) left the middle `band` of the view?"""
    margin = viewport_height * (1 - band) / 2
    return top < margin or bottom > viewport_height - margin


def too_tall(top: int, bottom: int, viewport_height: int, band: float = FOLLOW_BAND) -> bool:
    """Is a block too tall to center as a whole? Then the sentence is centered instead."""
    return bottom - top > viewport_height * band


class _UserScrollWatcher(QObject):
    """Reports pressing, dragging or wheeling on the scroll bar itself."""

    def __init__(self, on_scroll, parent: QObject):
        super().__init__(parent)
        self._on_scroll = on_scroll
        self.forwarding = False  # the view is passing its own wheel event on to the bar

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel, QEvent.Type.KeyPress):
            if not self.forwarding:
                self._on_scroll()
        return False


class ReaderView:
    """What both reader views provide (mixin before a QAbstractScrollArea subclass)."""

    clicked = Signal(int, int)  # (block, char) the user clicked
    user_scrolled = Signal()  # scrolled by hand: the main window suspends auto-follow
    selection_made = Signal(QPoint)  # a drag (or double click) selected text; global point
    context_requested = Signal(int, int, QPoint)  # right click: (block, char), global point

    # ---------- the interface ----------

    def set_book(self, book: Book, mode: str) -> None:
        """Show a book in a layout (one of MODES)."""
        raise NotImplementedError

    def set_theme(self, theme: Theme) -> None:
        raise NotImplementedError

    def set_scale(self, value: float) -> None:
        """ReflowView: the text size in points; PageView: the zoom factor."""
        raise NotImplementedError

    def show_highlight(self, block: int, start: int, end: int) -> None:
        """Highlight the chars [start, end) of a block (the sentence being read)."""
        raise NotImplementedError

    def clear_highlight(self) -> None:
        raise NotImplementedError

    def scroll_to(self, block: int, char: int = 0, *, animate: bool = True) -> None:
        """Center the line holding (block, char) — see the auto-follow rules above."""
        raise NotImplementedError

    def keep_in_view(self, block: int, start: int, end: int) -> None:
        """Re-center when the chars [start, end) drift out of the middle of the view."""
        raise NotImplementedError

    def current_block(self) -> int:
        """The first block visible near the top of the view."""
        raise NotImplementedError

    def set_page_mode(self, mode: str) -> None:
        """Continuous scroll, single page or double page (one of PAGE_MODES)."""
        raise NotImplementedError

    def set_marks(self, marks: list[tuple[int, int, int, str]]) -> None:
        """Colored spans (block, start, end, "#RRGGBB") drawn under the reading highlight."""
        raise NotImplementedError

    def selected_range(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """The selected text: ((block, char), (block, char after its last char)), or None."""
        raise NotImplementedError

    def clear_selection(self) -> None:
        raise NotImplementedError

    def set_style(self, style: ReadingStyle) -> None:
        raise NotImplementedError

    def page_info(self) -> tuple[int, int, int] | None:
        """(first page shown, last page shown, pages in all), 1-based; None when not known yet."""
        raise NotImplementedError

    def spread_step(self) -> int:
        """Paged: how far (scroll bar px) one page turn goes."""
        raise NotImplementedError

    def hit_test(self, pos: QPoint) -> tuple[int, int] | None:
        """(block, char) under a point of the viewport, or None."""
        raise NotImplementedError

    def _hover_link_changed(self, block: int) -> None:
        """The pointer moved onto a contents-page line (its block) or off it (-1)."""

    # ---------- shared behavior ----------

    _book: Book | None = None

    _page_mode = "scroll"

    def page_mode(self) -> str:
        return self._page_mode

    def paged(self) -> bool:
        return self._page_mode != "scroll"

    def page_step(self, step: int) -> None:
        """One page (or one screen, scrolling) forward or back, by the user."""
        self._scrolled_by_hand()
        bar = self.verticalScrollBar()
        if self.paged():
            self.turn_to(bar.value() + step * self.spread_step())
        else:
            self.scroll_to_value(bar.value() + step * max(1, bar.pageStep() - 40))

    def turn_to(self, value: int) -> None:
        """Paged: show the spread holding scroll value `value` (snapped to a page turn)."""
        step = max(1, self.spread_step())
        value = max(0, int(value) // step * step)  # the spread holding it (the left page's turn)
        self._allow_value(value)
        value = min(value, self.verticalScrollBar().maximum() // step * step)
        self.stop_scrolling()
        self.verticalScrollBar().setValue(value)

    def _allow_value(self, value: int) -> None:
        """Paged: let the scroll bar reach `value` (the last spread may start past Qt's maximum)."""

    def _init_reader_view(self) -> None:
        self._wheel = 0
        self._turned_at = 0.0
        self._scroll_animation: QPropertyAnimation | None = None
        self._press_pos: QPoint | None = None
        self._hover_link = -1
        self._idle_cursor = self.viewport().cursor().shape()
        self.viewport().setMouseTracking(True)
        self._scroll_watcher = _UserScrollWatcher(self._scrolled_by_hand, self)
        self.verticalScrollBar().installEventFilter(self._scroll_watcher)

    def scroll_to_value(self, target: int, *, animate: bool = True) -> None:
        """Scroll to a value of the vertical scroll bar, eased (InOutCubic) or at once.
        Paged, it turns to the spread holding target, at once."""
        if self.paged():
            self.turn_to(target)
            return
        scrollbar = self.verticalScrollBar()
        self.stop_scrolling()
        target = max(scrollbar.minimum(), min(scrollbar.maximum(), target))
        if not animate or target == scrollbar.value():
            scrollbar.setValue(target)
            return
        animation = QPropertyAnimation(scrollbar, b"value", self)
        animation.setDuration(SCROLL_MS)
        animation.setStartValue(scrollbar.value())
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._scroll_animation = animation
        animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def stop_scrolling(self) -> None:
        """Stop a running scroll animation where it is."""
        animation, self._scroll_animation = self._scroll_animation, None
        if animation is not None:
            try:
                animation.stop()
            except RuntimeError:  # it finished and Qt already deleted it
                pass

    def scroll_target(self) -> int:
        """Where the view is heading: the animation's end, else the current value."""
        animation = self._scroll_animation
        if animation is not None:
            try:
                if animation.state() == QPropertyAnimation.State.Running:
                    return int(animation.endValue())
            except RuntimeError:
                pass
        return self.verticalScrollBar().value()

    def _scrolled_by_hand(self) -> None:
        self.stop_scrolling()
        self.user_scrolled.emit()

    # ---------- events ----------

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.paged():
            event.accept()
            self._scrolled_by_hand()
            delta = event.angleDelta().y() or event.pixelDelta().y() * 2
            if time.monotonic() - self._turned_at < TURN_PAUSE_S:
                return
            self._wheel += delta
            if abs(self._wheel) >= WHEEL_NOTCH:
                self.page_step(-1 if self._wheel > 0 else 1)
                self._wheel = 0
                self._turned_at = time.monotonic()
            return
        self._scrolled_by_hand()
        self._scroll_watcher.forwarding = True
        try:
            super().wheelEvent(event)
        finally:
            self._scroll_watcher.forwarding = False

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in PLAYER_KEYS and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            event.ignore()  # the main window's play/pause and ±10 s
            return
        if event.key() in SCROLL_KEYS and self.paged():
            key = event.key()
            if key in (Qt.Key.Key_Home, Qt.Key.Key_End):
                self._scrolled_by_hand()
                self.turn_to(0 if key == Qt.Key.Key_Home else 1 << 30)
            else:
                self.page_step(-1 if key in (Qt.Key.Key_Up, Qt.Key.Key_PageUp) else 1)
            return
        if event.key() in SCROLL_KEYS:
            self._scrolled_by_hand()
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        left = event.button() == Qt.MouseButton.LeftButton
        self._press_pos = event.position().toPoint() if left else None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        super().mouseDoubleClickEvent(event)
        self._press_pos = None  # the first click already counted

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        press, self._press_pos = self._press_pos, None
        if press is None or event.button() != Qt.MouseButton.LeftButton:
            return
        if (event.position().toPoint() - press).manhattanLength() >= QApplication.startDragDistance():
            return  # a drag: selecting text, not a click
        hit = self.hit_test(press)
        if hit is not None:
            self.clicked.emit(*hit)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        super().mouseMoveEvent(event)
        if event.buttons() != Qt.MouseButton.NoButton:
            return
        self._set_hover_link(self._link_block_at(event.position().toPoint()))

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self._set_hover_link(-1)

    def _link_block_at(self, pos: QPoint) -> int:
        hit = self.hit_test(pos)
        if hit is None or self._book is None:
            return -1
        return hit[0] if self._book.blocks[hit[0]].link >= 0 else -1

    def _set_hover_link(self, block: int) -> None:
        if block == self._hover_link:
            return
        self._hover_link = block
        if block >= 0:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.viewport().setCursor(self._idle_cursor)
        self._hover_link_changed(block)
