"""The two pages without the text: audio-generation only, and audio player only.

GenerationPage — "audio-generation only mode" (user request): the book's cover,
how far its audio has got, how long the rest takes, and what to save when it's
done (nothing, one MP3, one M4B, or a file per chapter). While it's shown the
window streams nothing and draws no text or pages, so the processor works on
the audio alone; the main window does the rest (it owns generation).

PlayerPage — "audio-player only mode" (the user's screenshot of a listening
app): no text, only the cover, the chapter being heard and the time left in
the book, a bar across the chapter (drag it to move within the chapter), a
bar across the whole book divided into its chapters (click one to go there),
and the player's buttons. The main window feeds it update(...) and answers
its signals.

Both paint their background in the theme's panel color, like the landing page.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..library import BookRecord
from . import theme as themes
from .library_page import CoverCache, paint_drawn_cover
from .theme import ACCENT, reading_font, ui_font
from .widgets import GradientProgressBar, PlayerCapsule, segmented_control

AFTER_CHOICES = [
    ("nothing", "Nothing", "Just make the audio"),
    ("mp3", "MP3", "Save the book as one MP3 with chapters"),
    ("m4b", "M4B", "Save the book as an M4B audiobook with chapters"),
    ("chapters", "Chapters", "Save one file per chapter"),
]


def clock(ms: float) -> str:
    """'48:32', '1:02:05'."""
    seconds = max(0, round(ms / 1000))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class _Cover(QWidget):
    """The book's cover (or a drawn one), fitted to the widget."""

    def __init__(self, size: QSize, parent=None):
        super().__init__(parent)
        self.setFixedSize(size)
        self._record: BookRecord | None = None
        self._covers = CoverCache()

    def set_record(self, record: BookRecord | None) -> None:
        self._record = record
        if record is not None:
            self._covers.forget(record.book_id)
        self.update()

    def paintEvent(self, event):
        if self._record is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pixmap = self._covers.pixmap(self._record.book_id, self.size(), self.devicePixelRatioF())
        if pixmap is None:
            height = self.height()
            width = min(self.width(), height / 1.5)
            paint_drawn_cover(p, QRectF((self.width() - width) / 2, 0, width, height), self._record)
            return
        size = pixmap.deviceIndependentSize()
        p.drawPixmap(int((self.width() - size.width()) / 2), int((self.height() - size.height()) / 2), pixmap)


class _Page(QWidget):
    def paintEvent(self, event):
        QPainter(self).fillRect(self.rect(), QColor(themes.current().panel))


def _label(text: str, size: float, weight=QFont.Weight.Normal, name: str = "") -> QLabel:
    label = QLabel(text)
    label.setFont(ui_font(size, weight))
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    return label


class GenerationPage(_Page):
    back_requested = Signal()  # Back to Reading
    stop_requested = Signal()
    after_chosen = Signal(str)  # see AFTER_CHOICES

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("generationPage")
        self.cover = _Cover(QSize(170, 255))
        self.title = QLabel()
        self.title.setFont(reading_font(24, QFont.Weight.DemiBold))
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        self.author = _label("", 14, name="caption")
        self.headline = _label("Making the audio", 15, QFont.Weight.DemiBold)
        self.progress = GradientProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFixedSize(420, 24)
        self.progress.setTextVisible(True)
        self.eta = _label("", 13)
        self.note = _label(
            "Audio-generation only: no streaming, no text on screen, so the Mac's processor works on the "
            "audio alone. The Mac stays awake until it's done.", 12, name="caption",
        )
        for label in (self.eta, self.note):
            label.setFixedWidth(560)
            label.setMinimumHeight(label.heightForWidth(560))
        after = _label("When it's done, save:", 13)
        frame, self.after_buttons = segmented_control(AFTER_CHOICES, "nothing", self.after_chosen.emit, self)
        frame.setFixedWidth(420)
        self.saved = _label("", 12, name="caption")
        self.back_button = QPushButton("Back to Reading")
        self.back_button.setObjectName("secondaryButton")
        self.back_button.setToolTip("Show the book again; the audio keeps being made")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.stop_button = QPushButton("Stop Generating")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        for button in (self.back_button, self.stop_button):
            button.setFixedHeight(32)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.back_button)
        buttons.addSpacing(10)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(8)
        layout.addStretch(2)
        for widget in (self.cover, self.title, self.author):
            layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(18)
        for widget in (self.headline, self.progress, self.eta, self.note):
            layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(18)
        layout.addWidget(after, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(frame, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.saved, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(16)
        layout.addLayout(buttons)
        layout.addStretch(3)

    def set_book(self, record: BookRecord, after: str) -> None:
        self.cover.set_record(record)
        self.title.setText(record.title)
        self.author.setText(record.author)
        self.author.setVisible(bool(record.author))
        self.after_buttons[after if after in self.after_buttons else "nothing"].setChecked(True)
        self.saved.setText("")

    def update_progress(self, fraction: float, eta: str, running: bool) -> None:
        self.progress.setValue(round(fraction * 1000))
        self.progress.setFormat(f"{int(fraction * 100)}%")
        self.eta.setText(eta)
        self.headline.setText("Making the audio" if running else ("All the audio is ready" if fraction >= 1
                                                                    else "Stopped"))
        self.stop_button.setVisible(running)


class ChapterBar(QWidget):
    """The whole book as one bar, a segment per chapter as long as its audio, filled to
    where the listening is; the current chapter stands out. Clicking a segment picks it."""

    chapter_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(14)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lengths: list[float] = []
        self._position = 0.0  # share of the book heard
        self._current = -1

    def set_state(self, lengths: list[float], position: float, current: int) -> None:
        self._lengths, self._position, self._current = lengths, position, current
        self.update()

    def segment_at(self, x: float) -> int:
        total = sum(self._lengths)
        if total <= 0:
            return -1
        at = x / max(1, self.width()) * total
        for i, length in enumerate(self._lengths):
            at -= length
            if at < 0:
                return i
        return len(self._lengths) - 1

    def mousePressEvent(self, event):
        index = self.segment_at(event.position().x())
        if index >= 0:
            self.chapter_clicked.emit(index)

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        total = sum(self._lengths)
        if total <= 0:
            return
        gap = 2.0
        x = 0.0
        width = self.width() - gap * max(0, len(self._lengths) - 1)
        heard = self._position * width
        for i, length in enumerate(self._lengths):
            w = max(1.0, length / total * width)
            rect = QRectF(x, 3, w, self.height() - 6)
            base = QColor(t.text)
            base.setAlphaF(0.14 if i != self._current else 0.26)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(base)
            p.drawRoundedRect(rect, 3, 3)
            filled = min(w, max(0.0, heard - (x - gap * i)))
            if filled > 0:
                p.setBrush(QColor(ACCENT))
                p.drawRoundedRect(QRectF(x, 3, filled, self.height() - 6), 3, 3)
            x += w + gap
        p.setPen(QPen(QColor(t.text), 2))
        marker = min(self.width() - 1, heard + gap * max(0, self._current))
        p.drawLine(int(marker), 0, int(marker), self.height())


class PlayerPage(_Page):
    show_text_requested = Signal()
    chapters_requested = Signal()
    seek_requested = Signal(float)  # share of the current chapter
    chapter_requested = Signal(int)  # a chapter of the chapter bar
    speed_chosen = Signal(int)  # index into the speeds

    def __init__(self, speeds: list[str], parent=None):
        super().__init__(parent)
        self.setObjectName("playerPage")
        top = QHBoxLayout()
        top.setContentsMargins(24, 14, 24, 0)
        self.show_text_button = self._tool("Show Text", "Back to the book's text", self.show_text_requested.emit)
        self.chapters_button = self._tool("Chapters", "The book's chapters", self.chapters_requested.emit)
        self.book_title = QLabel()
        self.book_title.setFont(ui_font(14, QFont.Weight.DemiBold))
        self.book_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.speed = QComboBox()
        self.speed.addItems(speeds)
        self.speed.setFixedWidth(84)
        self.speed.setToolTip("Playback speed")
        self.speed.currentIndexChanged.connect(self.speed_chosen.emit)
        top.addWidget(self.chapters_button)
        top.addWidget(self.book_title, 1)
        top.addWidget(self.speed)
        top.addWidget(self.show_text_button)

        self.cover = _Cover(QSize(260, 300))
        self.chapter_title = _label("", 15, QFont.Weight.DemiBold)
        self.left_in_book = _label("", 13, name="caption")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider.setStyleSheet(f"QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}")
        self.slider.sliderReleased.connect(lambda: self.seek_requested.emit(self.slider.value() / 1000))
        self.elapsed = QLabel("0:00")
        self.remaining = QLabel("-0:00")
        for label in (self.elapsed, self.remaining):
            label.setFont(ui_font(11))
            label.setObjectName("caption")
        times = QHBoxLayout()
        times.addWidget(self.elapsed)
        times.addStretch(1)
        times.addWidget(self.remaining)
        self.chapter_bar = ChapterBar()
        self.chapter_bar.setToolTip("The whole book, chapter by chapter: click one to go there")
        self.chapter_bar.chapter_clicked.connect(self.chapter_requested.emit)
        self.capsule = PlayerCapsule(self)

        middle = QVBoxLayout()
        middle.setContentsMargins(60, 0, 60, 0)
        middle.setSpacing(8)
        middle.addWidget(self.slider)
        middle.addLayout(times)
        middle.addSpacing(10)
        caption = _label("Chapters", 11, name="caption")
        caption.setAlignment(Qt.AlignmentFlag.AlignLeft)
        middle.addWidget(caption)
        middle.addWidget(self.chapter_bar)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 30)
        layout.setSpacing(6)
        layout.addLayout(top)
        layout.addStretch(2)
        layout.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(10)
        layout.addWidget(self.chapter_title)
        layout.addWidget(self.left_in_book)
        layout.addStretch(1)
        layout.addLayout(middle)
        layout.addSpacing(18)
        layout.addWidget(self.capsule, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

    def _tool(self, text: str, tip: str, slot) -> QToolButton:
        button = QToolButton()
        button.setObjectName("libraryTool")
        button.setText(text)
        button.setToolTip(tip)
        button.setFixedHeight(32)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def set_book(self, record: BookRecord, speed_index: int) -> None:
        self.cover.set_record(record)
        self.book_title.setText(record.title)
        self.speed.blockSignals(True)
        self.speed.setCurrentIndex(speed_index)
        self.speed.blockSignals(False)

    def update_position(self, chapter: str, elapsed_ms: float, chapter_ms: float, left_in_book_ms: float,
                        lengths: list[float], book_fraction: float, current: int) -> None:
        self.chapter_title.setText(chapter)
        self.left_in_book.setText(f"{_long_time(left_in_book_ms)} left in book")
        if not self.slider.isSliderDown():
            self.slider.setValue(round(1000 * elapsed_ms / chapter_ms) if chapter_ms > 0 else 0)
        self.elapsed.setText(clock(elapsed_ms))
        self.remaining.setText("-" + clock(max(0.0, chapter_ms - elapsed_ms)))
        self.chapter_bar.set_state(lengths, book_fraction, current)


def _long_time(ms: float) -> str:
    """'7h 15m', '48m'."""
    minutes = round(ms / 60000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"
