"""Custom widgets: popovers (themes, contents, voices), the narrator picker with
audition clips, the player capsule, the progress bar, switches and alerts.

Colors come from theme.current() at paint time and from the app stylesheet
(theme.stylesheet), so everything follows the theme. Ported from Script Radio's
widgets, with what a book needs instead of a screenplay:

- ThemesPopover ("Aa") also switches the layout (the book's Original layout or
  LivroSound's) and turns the sentence highlight on or off.
- ContentsPopover replaces the scene list: the book's table of contents, every
  row a link, with a fern dot on chapters whose audio is ready.
- VoicePicker / VoiceMenu pick one narrator from voices grouped under headers
  ("Women", "Men"); VoicePreviews is engine-agnostic: it is handed a render
  callable (voice -> WAV path) and runs it off the UI thread.
- PlayerCapsule owns its five buttons: −10 sec, ⏮ (paragraph back), ▶, ⏭, +10 sec.
- confirm() / inform() are small macOS-style alerts: No on the left, Yes on the right.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QCompleter,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..paths import ICONS_DIR
from . import theme as themes
from .theme import ACCENT, ACCENT_HOVER, ON_ACCENT, PROGRESS_BLUES, READING_FAMILIES, THEMES, reading_font, ui_font

APP_ICON = ICONS_DIR / "livrosound.svg"
BOOK_BOUNDS = QRectF(28, 104, 456, 306)  # the book's outline inside the icon's 512 × 512 view box
SKIP_MS = 10_000  # what −10 sec / +10 sec jump

VoiceGroups = list[tuple[str, list[str]]]  # [("Women", ["Ana", …]), ("Men", ["Aaron", …])]


# ---------- icons (painted, so they're crisp on Retina and follow the theme) ----------


def _canvas(width: int, height: int) -> tuple[QPixmap, QPainter]:
    pixmap = QPixmap(width * 2, height * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pixmap, painter


def book_pixmap(width: int) -> QPixmap:
    """The LivroSound book, cropped to its own outline, width points wide (2× pixels)."""
    height = round(width * BOOK_BOUNDS.height() / BOOK_BOUNDS.width())
    pixmap, p = _canvas(width, height)
    renderer = QSvgRenderer(str(APP_ICON))
    renderer.setViewBox(BOOK_BOUNDS)
    renderer.render(p, QRectF(0, 0, width, height))
    p.end()
    return pixmap


def chevron_icon(color: str, size: int = 16) -> QIcon:
    pixmap, p = _canvas(size, size)
    pen = QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    m = size / 2
    p.drawPolyline(QPolygonF([QPointF(m - 4.5, m - 2), QPointF(m, m + 2.5), QPointF(m + 4.5, m - 2)]))
    p.end()
    return QIcon(pixmap)


def aa_icon(color: str) -> QIcon:
    """A small and a large A — the Apple Books "text & themes" mark."""
    pixmap, p = _canvas(36, 24)
    p.setPen(QColor(color))
    small, large = ui_font(12, QFont.Weight.DemiBold), ui_font(12, QFont.Weight.DemiBold)
    small.setPixelSize(15)
    large.setPixelSize(23)
    baseline = 21
    p.setFont(small)
    p.drawText(QPointF(4, baseline), "A")
    p.setFont(large)
    p.drawText(QPointF(16, baseline), "A")
    p.end()
    return QIcon(pixmap)


def list_icon(color: str) -> QIcon:
    pixmap, p = _canvas(20, 16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    for y in (3, 8, 13):
        p.drawEllipse(QPointF(3, y), 1.5, 1.5)
        p.drawRoundedRect(QRectF(7, y - 1, 12, 2), 1, 1)
    p.end()
    return QIcon(pixmap)


def half_circle_icon(color: str, size: int = 20) -> QIcon:
    """◐ — switch between a light and a dark theme."""
    pixmap, p = _canvas(size, size)
    c = QColor(color)
    r = QRectF(3, 3, size - 6, size - 6)
    p.setPen(QPen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(r)
    path = QPainterPath()
    path.moveTo(r.center())
    path.arcTo(r, 90, 180)
    path.closeSubpath()
    p.fillPath(path, c)
    p.end()
    return QIcon(pixmap)


def _transport_pixmap(kind: str, color: QColor, size: int) -> QPixmap:
    """▶ play, ❚❚ pause, ⏮ previous, ⏭ next — drawn rather than typed, since the
    glyphs differ from font to font and sit off-center in round buttons."""
    pixmap, p = _canvas(size, size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    s = size
    if kind == "play":
        # Nudged right: a triangle looks centered when its center of mass is.
        p.drawPolygon(QPolygonF([QPointF(s * 0.26, s * 0.14), QPointF(s * 0.9, s * 0.5), QPointF(s * 0.26, s * 0.86)]))
    elif kind == "pause":
        for x in (s * 0.22, s * 0.58):
            p.drawRoundedRect(QRectF(x, s * 0.15, s * 0.2, s * 0.7), s * 0.06, s * 0.06)
    else:  # "next": ▶| ; "previous" is its mirror image, |◀
        tri = QPolygonF([QPointF(s * 0.18, s * 0.18), QPointF(s * 0.7, s * 0.5), QPointF(s * 0.18, s * 0.82)])
        bar = QRectF(s * 0.74, s * 0.18, s * 0.14, s * 0.64)
        if kind == "previous":
            tri = QPolygonF([QPointF(s - pt.x(), pt.y()) for pt in tri])
            bar.moveLeft(s - bar.right())
        p.drawPolygon(tri)
        p.drawRoundedRect(bar, s * 0.05, s * 0.05)
    p.end()
    return pixmap


def _transport_icon(kind: str, color: QColor, disabled: QColor, size: int) -> QIcon:
    icon = QIcon()
    icon.addPixmap(_transport_pixmap(kind, color, size), QIcon.Mode.Normal)
    icon.addPixmap(_transport_pixmap(kind, disabled, size), QIcon.Mode.Disabled)
    return icon


def _with_alpha(color: str, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(alpha)
    return c


def _hairline() -> QFrame:
    line = QFrame()
    line.setObjectName("hairline")
    line.setFixedHeight(1)
    return line


def _popover_title(text: str) -> QLabel:
    title = QLabel(text)
    title.setObjectName("popoverTitle")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    title.setFont(ui_font(13, QFont.Weight.DemiBold))
    return title


# ---------- popover ----------


class Popover(QWidget):
    """A floating rounded panel, like the popovers in Apple's apps, optionally with
    an arrow pointing at the button that opened it. Closes on click-outside or Esc."""

    RADIUS = 14
    ARROW_W, ARROW_H = 20, 9

    def __init__(self, parent: QWidget | None, *, arrow: bool = True, width: int | None = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._arrow = arrow
        self._arrow_x = (width or 0) / 2
        if width:
            self.setFixedWidth(width)
        self.body = QVBoxLayout(self)
        top = self.ARROW_H if arrow else 0
        self.body.setContentsMargins(12, 10 + top, 12, 12)
        self.body.setSpacing(10)

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = self.ARROW_H if self._arrow else 0
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, top + 0.5, self.width() - 1, self.height() - top - 1), self.RADIUS, self.RADIUS)
        if self._arrow:
            x = self._arrow_x
            notch = QPainterPath()
            notch.addPolygon(
                QPolygonF([QPointF(x - self.ARROW_W / 2, top + 1), QPointF(x, 0.5), QPointF(x + self.ARROW_W / 2, top + 1)])
            )
            path = path.united(notch)
        p.setPen(QPen(QColor(t.line), 1))
        p.setBrush(QColor(t.panel))
        p.drawPath(path)

    def popup_under(self, anchor: QWidget, *, align_left: bool = False) -> None:
        """Show just below anchor (above it if the screen runs out), the arrow on its center."""
        self.adjustSize()
        screen = (anchor.screen() or self.screen()).availableGeometry()
        bottom_center = anchor.mapToGlobal(QPoint(anchor.width() // 2, anchor.height()))
        left = anchor.mapToGlobal(QPoint(0, 0)).x() if align_left else bottom_center.x() - self.width() // 2
        x = max(screen.left() + 8, min(left, screen.right() - self.width() - 8))
        y = bottom_center.y() + 4
        if y + self.height() > screen.bottom() - 8:
            y = max(screen.top() + 8, anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4)
        margin = self.RADIUS + self.ARROW_W / 2
        self._arrow_x = max(margin, min(bottom_center.x() - x, self.width() - margin))
        self.move(x, y)
        self.show()


def _popover_list(width: int) -> QListWidget:
    """A list for a popover. Its width is fixed before rows are added: row widgets
    are sized from the list's width when they're inserted."""
    lst = QListWidget()
    lst.setObjectName("popoverList")
    lst.setFixedWidth(width)
    lst.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    lst.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    lst.setFrameShape(QFrame.Shape.NoFrame)
    lst.setSpacing(1)
    return lst


# ---------- voice auditions ----------


class _PreviewWorker(QThread):
    """Renders audition clips one at a time, taking jobs from the controller."""

    ready = Signal(str, str, int)  # voice, path, render generation
    failed = Signal(str, str, int)  # voice, error, render generation

    def __init__(self, next_job: Callable[[], tuple[str, Callable[[str], Path], int] | None]):
        super().__init__()
        self._next_job = next_job

    def run(self):
        while (job := self._next_job()) is not None:
            voice, render, generation = job
            try:
                self.ready.emit(voice, str(render(voice)), generation)
            except Exception as e:  # one bad voice shouldn't stop the rest
                self.failed.emit(voice, str(e) or type(e).__name__, generation)


class VoicePreviews(QObject):
    """Plays a voice's audition clip, rendering it first (off the UI thread) when
    it hasn't been made yet.

    It knows nothing about engines: render(voice) returns the path of a WAV
    (e.g. voice_preview.make_preview bound to the chosen engine) and may take
    seconds the first time. set_render() switches to another engine's clips.
    state_changed(voice, state) reports "idle", "loading" or "playing" so every
    play button showing that voice can update itself."""

    state_changed = Signal(str, str)
    failed = Signal(str)

    def __init__(self, render: Callable[[str], Path] | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._render = render
        self._paths: dict[str, Path] = {}  # clips made with the current render
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.player.setAudioOutput(self.output)
        self.player.playbackStateChanged.connect(self._on_playback_state)
        self._current: str | None = None  # the voice playing or being rendered to play
        self._state = "idle"
        self._jobs: deque[str] = deque()
        self._jobs_lock = threading.Lock()
        # Bumped by set_render: clips still coming from the old render are ignored.
        self._generation = 0
        self._worker: _PreviewWorker | None = None
        self.before_play: Callable[[], None] = lambda: None  # e.g. pause the book

    def set_render(self, render: Callable[[str], Path] | None) -> None:
        """Audition clips from now on come from render (another engine)."""
        self.stop()
        with self._jobs_lock:
            self._jobs.clear()
            self._generation += 1
            self._render = render
        self._paths.clear()

    def state(self, voice: str) -> str:
        return self._state if voice == self._current else "idle"

    def is_ready(self, voice: str) -> bool:
        path = self._paths.get(voice)
        return path is not None and path.is_file()

    def toggle(self, voice: str) -> None:
        if voice == self._current and self._state in ("playing", "loading"):
            self.stop()
            return
        self.stop()
        if self._render is None:
            return
        self._current = voice
        if self.is_ready(voice):
            self._play(voice)
        else:
            self._set_state("loading")
            self._enqueue([voice], front=True)

    def warm(self, voices: list[str]) -> None:
        """Make any missing clips in the background, so they play instantly later."""
        if self._render is not None:
            self._enqueue([v for v in voices if not self.is_ready(v)])

    def stop(self) -> None:
        current = self._current
        self._current = None
        self.player.stop()
        if current is not None:
            self._state = "idle"
            self.state_changed.emit(current, "idle")

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop playing and drop queued clips. A clip being rendered still finishes on its
        thread: with wait, this blocks until it has (up to 5 s); else see rendering()/wait()."""
        with self._jobs_lock:
            self._jobs.clear()
        self.stop()
        if wait and self._worker is not None:
            self._worker.wait(5000)

    def rendering(self) -> bool:
        """Is a clip still being rendered on the worker thread? (The app must not quit then.)"""
        return self._worker is not None and self._worker.isRunning()

    def wait(self, ms: int) -> bool:
        """Wait up to ms for the rendering thread to end; True when it has."""
        return self._worker is None or self._worker.wait(ms)

    # -- internals --

    def _set_state(self, state: str) -> None:
        self._state = state
        if self._current is not None:
            self.state_changed.emit(self._current, state)

    def _play(self, voice: str) -> None:
        self.before_play()
        self.player.setSource(QUrl.fromLocalFile(str(self._paths[voice])))
        self.player.play()
        self._set_state("playing")

    def _enqueue(self, voices: list[str], front: bool = False) -> None:
        with self._jobs_lock:
            for voice in voices:
                if voice in self._jobs:
                    self._jobs.remove(voice)
                if front:
                    self._jobs.appendleft(voice)
                else:
                    self._jobs.append(voice)
        if self._worker is None or not self._worker.isRunning():
            self._worker = _PreviewWorker(self._next_job)
            self._worker.ready.connect(self._on_ready)
            self._worker.failed.connect(self._on_failed)
            self._worker.finished.connect(self._on_worker_finished)
            self._worker.start()

    def _next_job(self):
        with self._jobs_lock:
            if self._jobs and self._render is not None:
                return self._jobs.popleft(), self._render, self._generation
        return None

    def _on_worker_finished(self):
        # A worker reads as stopped while it is still emitting finished, so a clip asked
        # for in that moment already started its replacement: ignore the old one's signal,
        # or the running replacement would be dropped (Qt aborts: "QThread: Destroyed
        # while thread is still running").
        if self.sender() is not self._worker:
            return
        with self._jobs_lock:
            pending = bool(self._jobs)
        if pending:  # a job arrived just as the worker was finishing
            self._worker = None
            self._enqueue([])

    def _on_ready(self, voice: str, path: str, generation: int):
        if generation != self._generation:
            return
        self._paths[voice] = Path(path)
        if voice == self._current and self._state == "loading":
            self._play(voice)

    def _on_failed(self, voice: str, message: str, generation: int):
        if generation != self._generation:
            return
        if voice == self._current:
            self._set_state("idle")
            self._current = None
        self.failed.emit(f"Couldn't make a preview of {voice}: {message}")

    def _on_playback_state(self, state):
        if state == QMediaPlayer.PlaybackState.StoppedState and self._state == "playing":
            self.stop()


class PreviewButton(QAbstractButton):
    """▶ plays a voice's audition clip, ■ stops it; a spinning ring while it's being made."""

    def __init__(self, previews: VoicePreviews, voice: str, *, filled: bool = True, size: int = 26, parent=None):
        super().__init__(parent)
        self._previews = previews
        self._voice = voice
        self._filled = filled
        self._state = previews.state(voice)
        self._angle = 0
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Hear this voice")
        self._spin = QTimer(self)
        self._spin.setInterval(30)
        self._spin.timeout.connect(self._advance)
        previews.state_changed.connect(self._on_state)
        self.clicked.connect(lambda: self._previews.toggle(self._voice))

    def set_voice(self, voice: str) -> None:
        self._voice = voice
        self._on_state(voice, self._previews.state(voice))

    def _on_state(self, voice: str, state: str):
        if voice != self._voice:
            return
        self._state = state
        self.setToolTip("Stop" if state == "playing" else "Preparing preview…" if state == "loading" else "Hear this voice")
        if state == "loading":
            self._spin.start()
        else:
            self._spin.stop()
        self.update()

    def _advance(self):
        self._angle = (self._angle + 12) % 360
        self.update()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        hover = self.underMouse() and self.isEnabled()
        if not self.isEnabled():
            p.setOpacity(0.35)  # e.g. no engine to preview with
        if self._state == "loading":
            p.setPen(QPen(QColor(t.line), 2))
            p.drawEllipse(r.adjusted(2, 2, -2, -2))
            p.setPen(QPen(QColor(ACCENT), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(r.adjusted(2, 2, -2, -2), -self._angle * 16, 100 * 16)
            return
        solid = self._filled or hover or self._state == "playing"
        if solid:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT_HOVER if hover else ACCENT))
            glyph = QColor(ON_ACCENT)
        else:
            p.setPen(QPen(QColor(t.line), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            glyph = QColor(t.text)
        p.drawEllipse(r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glyph)
        c = r.center()
        s = r.width() * 0.17
        if self._state == "playing":
            p.drawRoundedRect(QRectF(c.x() - s, c.y() - s, 2 * s, 2 * s), 1.5, 1.5)
        else:
            p.drawPolygon(QPolygonF([
                QPointF(c.x() - s * 0.8, c.y() - s * 1.25),
                QPointF(c.x() + s * 1.35, c.y()),
                QPointF(c.x() - s * 0.8, c.y() + s * 1.25),
            ]))


class _VoiceRow(QWidget):
    def __init__(self, voice: str, previews: VoicePreviews):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 6, 0)
        name = QLabel(voice)
        name.setFont(ui_font(13))
        layout.addWidget(name, 1)
        layout.addWidget(PreviewButton(previews, voice, size=24))  # fern, like the picker's ▶


class VoiceMenu(Popover):
    """Every voice under a picker, grouped under headers ("Women", "Men"):
    click a name to choose it, ▶ to hear it first."""

    chosen = Signal(str)

    ROW_HEIGHT, HEADER_HEIGHT = 36, 30

    def __init__(self, groups: VoiceGroups, current: str, previews: VoicePreviews, parent: QWidget | None, width: int):
        super().__init__(parent, arrow=False, width=width)
        self.body.setContentsMargins(6, 6, 6, 6)
        self.list = _popover_list(width - 12)
        total = 0
        for group, voices in groups:
            if not voices:
                continue
            header = QListWidgetItem()
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setSizeHint(QSize(0, self.HEADER_HEIGHT))
            self.list.addItem(header)
            label = QLabel(group)
            label.setObjectName("groupHeader")
            label.setFont(ui_font(11, QFont.Weight.DemiBold))
            label.setContentsMargins(10, 8, 0, 0)
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.list.setItemWidget(header, label)
            total += self.HEADER_HEIGHT + 2
            for voice in voices:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, voice)
                item.setSizeHint(QSize(0, self.ROW_HEIGHT))
                self.list.addItem(item)
                self.list.setItemWidget(item, _VoiceRow(voice, previews))
                total += self.ROW_HEIGHT + 2
                if voice == current:
                    self.list.setCurrentItem(item)
        self.list.setFixedHeight(min(520, total + 4))
        self.list.itemClicked.connect(self._choose)
        self.list.itemActivated.connect(self._choose)
        self.body.addWidget(self.list)
        if self.list.currentItem() is not None:
            self.list.scrollToItem(self.list.currentItem(), QAbstractItemView.ScrollHint.PositionAtCenter)

    def _choose(self, item: QListWidgetItem):
        voice = item.data(Qt.ItemDataRole.UserRole)
        if voice:  # not a group header
            self.chosen.emit(voice)
            self.close()


class VoicePicker(QFrame):
    """[⌄ | voice name ........ ▶] — the chevron opens the grouped voice list, the
    name can be typed into to search, and ▶ plays the chosen voice's audition clip."""

    changed = Signal(str)

    def __init__(self, groups: VoiceGroups, current: str, previews: VoicePreviews, parent=None):
        super().__init__(parent)
        self.setObjectName("voicePicker")
        self._groups = [(group, list(voices)) for group, voices in groups]
        self._voices = [voice for _, voices in self._groups for voice in voices]
        self._current = current if current in self._voices else (self._voices[0] if self._voices else "")
        self._previews = previews
        self.setMinimumHeight(40)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 4, 6, 4)
        layout.setSpacing(6)

        self.arrow = QToolButton()
        self.arrow.setObjectName("pickerArrow")
        self.arrow.setFixedSize(28, 28)
        self.arrow.setIconSize(QSize(16, 16))
        self.arrow.setCursor(Qt.CursorShape.PointingHandCursor)
        self.arrow.setToolTip("Show all voices")
        self.arrow.clicked.connect(self.open_menu)
        self.refresh_icons()

        divider = QFrame()
        divider.setObjectName("pickerDivider")
        divider.setFixedSize(1, 20)

        self.edit = QLineEdit(self._current)
        self.edit.setFont(ui_font(14))
        self.edit.setPlaceholderText("Type to search voices…")
        completer = QCompleter(self._voices, self.edit)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.activated.connect(self.set_current)
        self.edit.setCompleter(completer)
        self.edit.editingFinished.connect(self._snap_to_valid)

        self.play = PreviewButton(previews, self._current)

        layout.addWidget(self.arrow)
        layout.addWidget(divider)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.play)

    def currentText(self) -> str:  # noqa: N802 — same name as QComboBox's
        return self._current

    def set_current(self, voice: str) -> None:
        if voice not in self._voices or voice == self._current:
            return
        self._current = voice
        self.edit.setText(voice)
        self.play.set_voice(voice)
        self.changed.emit(voice)

    def refresh_icons(self) -> None:
        self.arrow.setIcon(chevron_icon(themes.current().text))

    def open_menu(self) -> VoiceMenu:
        menu = VoiceMenu(self._groups, self._current, self._previews, self, width=self.width())
        menu.chosen.connect(self.set_current)
        menu.popup_under(self, align_left=True)
        return menu

    def mousePressEvent(self, event):
        # A click on the bubble's padding opens the list, like a dropdown.
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_menu()
        super().mousePressEvent(event)

    def _snap_to_valid(self):
        text = self.edit.text().strip()
        match = next((voice for voice in self._voices if voice.lower() == text.lower()), None)
        if match is not None and match != self._current:
            self.set_current(match)
        else:
            self.edit.setText(self._current)


# ---------- switch ----------


class ToggleSwitch(QCheckBox):
    """A checkbox drawn as a macOS-style sliding switch (fern when on)."""

    def __init__(self, parent=None, on_color: str = ACCENT):
        super().__init__(parent)
        self._on_color = on_color
        self._slide = QVariantAnimation(self)  # the knob's position, 0 = off .. 1 = on, while it moves
        self._slide.setDuration(140)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide.valueChanged.connect(self.update)
        self.toggled.connect(self._on_toggled)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 24)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def _knob(self) -> float:
        if self._slide.state() == QAbstractAnimation.State.Running:
            return float(self._slide.currentValue())
        return 1.0 if self.isChecked() else 0.0

    def _on_toggled(self, on: bool):
        start = self._knob() if self._slide.state() == QAbstractAnimation.State.Running else (0.0 if on else 1.0)
        self._slide.stop()
        if self.isVisible():
            self._slide.setStartValue(start)
            self._slide.setEndValue(1.0 if on else 0.0)
            self._slide.start()

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        off, on = QColor("#5A5A5E" if t.dark else "#C7C7CC"), QColor(self._on_color)
        k = self._knob()
        track = QColor.fromRgbF(
            off.redF() + (on.redF() - off.redF()) * k,
            off.greenF() + (on.greenF() - off.greenF()) * k,
            off.blueF() + (on.blueF() - off.blueF()) * k,
        )
        h = self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, self.width(), h), h / 2, h / 2)
        diameter = h - 4
        x = 2 + (self.width() - diameter - 4) * k
        p.setBrush(QColor(0, 0, 0, 40))  # a hairline shadow under the knob
        p.drawEllipse(QRectF(x, 2.6, diameter, diameter))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(x, 2, diameter, diameter))


# ---------- Themes & Settings ----------


class ThemeCard(QAbstractButton):
    """One theme as a little page: 'Aa' in its colors and the reading serif, its name below."""

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setCheckable(True)
        self.setFixedSize(118, 92)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{themes.theme_for(key).name} theme")

    def paintEvent(self, event):
        shown, card = themes.current(), themes.theme_for(self.key)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(2, 2, self.width() - 4, self.height() - 4)
        if self.isChecked():
            p.setPen(QPen(QColor(shown.text), 2.5))
        else:
            p.setPen(QPen(QColor(shown.line), 1))
        p.setBrush(QColor(card.page))
        p.drawRoundedRect(r, 14, 14)
        p.setPen(QColor(card.text))
        serif = QFont()
        serif.setFamilies(READING_FAMILIES)
        serif.setPixelSize(30)
        p.setFont(serif)
        p.drawText(QRectF(r.x(), r.y() + 8, r.width(), 40), Qt.AlignmentFlag.AlignCenter, "Aa")
        p.setFont(ui_font(12, QFont.Weight.Medium))
        p.drawText(QRectF(r.x(), r.y() + 52, r.width(), 24), Qt.AlignmentFlag.AlignCenter, card.name)


def segmented_control(choices: list[tuple[str, str, str]], current: str, on_choose: Callable[[str], None],
              parent: QWidget) -> tuple[QFrame, dict[str, QToolButton]]:
    """A macOS segmented control: (key, label, tooltip) per segment, one checked."""
    frame = QFrame()
    frame.setObjectName("segmented")
    frame.setFixedHeight(40)
    row = QHBoxLayout(frame)
    row.setContentsMargins(4, 4, 4, 4)
    row.setSpacing(2)
    buttons: dict[str, QToolButton] = {}
    group = QButtonGroup(parent)
    for key, text, tip in choices:
        button = QToolButton()
        button.setText(text)
        button.setCheckable(True)
        button.setChecked(key == current)
        button.setFont(ui_font(13, QFont.Weight.Medium))
        button.setFixedHeight(32)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tip)
        button.clicked.connect(lambda _=False, k=key: on_choose(k))
        group.addButton(button)
        row.addWidget(button, 1)
        buttons[key] = button
    return frame, buttons


PAGE_VIEWS = [
    ("scroll", "Scroll", "Continuous scroll"),
    ("single", "Single", "One page at a time"),
    ("double", "Double", "Two pages side by side"),
]
HIGHLIGHT_UNITS = [
    ("word", "Word", "Light up each word as it's read"),
    ("sentence", "Sentence", "Light up the sentence being read"),
    ("paragraph", "Paragraph", "Light up the paragraph being read"),
]


class ThemesPopover(Popover):
    """Themes & Settings ("Aa"): text size, light/dark, the reading themes, Override
    book layout (this book set with your font and margins instead of its own), the
    page view, the highlighter, and More Settings… (the Settings window)."""

    theme_chosen = Signal(str)
    text_size_step = Signal(int)  # -1 smaller, +1 larger
    override_toggled = Signal(bool)
    highlight_toggled = Signal(bool)
    page_view_chosen = Signal(str)  # see PAGE_VIEWS
    highlight_unit_chosen = Signal(str)  # see HIGHLIGHT_UNITS
    settings_requested = Signal()

    def __init__(self, parent: QWidget | None, current_key: str, *, override: bool = False, highlight: bool = True,
                 page_view: str = "scroll", highlight_unit: str = "sentence"):
        super().__init__(parent, width=300)
        self.body.addWidget(_popover_title("Themes & Settings"))
        self.body.addWidget(_hairline())

        row = QHBoxLayout()
        row.setSpacing(10)
        segmented = QFrame()
        segmented.setObjectName("segmented")
        segmented.setFixedHeight(40)
        seg = QHBoxLayout(segmented)
        seg.setContentsMargins(4, 4, 4, 4)
        seg.setSpacing(0)
        self.smaller, self.larger = QToolButton(), QToolButton()
        for button, size, step, tip in ((self.smaller, 12, -1, "Smaller text"), (self.larger, 17, 1, "Larger text")):
            button.setText("A")
            button.setFont(ui_font(size, QFont.Weight.Medium))
            button.setFixedHeight(32)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, s=step: self.text_size_step.emit(s))
        divider = QFrame()
        divider.setObjectName("segDivider")
        divider.setFixedSize(1, 20)
        seg.addWidget(self.smaller, 1)
        seg.addWidget(divider, 0, Qt.AlignmentFlag.AlignVCenter)
        seg.addWidget(self.larger, 1)
        row.addWidget(segmented, 2)

        self.appearance = QToolButton()
        self.appearance.setObjectName("roundButton")
        self.appearance.setFixedHeight(40)
        self.appearance.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.appearance.setIconSize(QSize(20, 20))
        self.appearance.setCursor(Qt.CursorShape.PointingHandCursor)
        self.appearance.setToolTip("Switch between a light and a dark theme")
        self.appearance.clicked.connect(lambda: self.theme_chosen.emit(THEMES[self._key].partner))
        row.addWidget(self.appearance, 1)
        self.body.addLayout(row)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.cards: dict[str, ThemeCard] = {}
        cards = QButtonGroup(self)
        for i, key in enumerate([*THEMES, "custom"]):
            card = ThemeCard(key)
            card.clicked.connect(lambda _=False, k=key: self.theme_chosen.emit(k))
            cards.addButton(card)
            grid.addWidget(card, i // 2, i % 2)
            self.cards[key] = card
        self.body.addLayout(grid)
        self.body.addWidget(_hairline())

        override_row = QHBoxLayout()
        override_row.setContentsMargins(2, 0, 2, 0)
        override_label = QLabel("Override book layout")
        override_label.setFont(ui_font(13))
        override_label.setToolTip("Set this book with your font, spacing and margins instead of its own")
        self.override_switch = ToggleSwitch()
        self.override_switch.setChecked(override)
        self.override_switch.toggled.connect(self.override_toggled)
        override_row.addWidget(override_label)
        override_row.addStretch(1)
        override_row.addWidget(self.override_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body.addLayout(override_row)

        caption = QLabel("Pages")
        caption.setObjectName("caption")
        caption.setFont(ui_font(11, QFont.Weight.DemiBold))
        self.body.addWidget(caption)
        frame, self.page_view_buttons = segmented_control(PAGE_VIEWS, page_view, self.page_view_chosen.emit, self)
        self.body.addWidget(frame)

        switch_row = QHBoxLayout()
        switch_row.setContentsMargins(2, 2, 2, 0)
        label = QLabel("Highlight while reading")
        label.setFont(ui_font(13))
        self.highlight_switch = ToggleSwitch()
        self.highlight_switch.toggled.connect(self.highlight_toggled)
        switch_row.addWidget(label)
        switch_row.addStretch(1)
        switch_row.addWidget(self.highlight_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body.addLayout(switch_row)
        frame, self.unit_buttons = segmented_control(HIGHLIGHT_UNITS, highlight_unit, self.highlight_unit_chosen.emit, self)
        self.body.addWidget(frame)
        self.body.addWidget(_hairline())
        more = QPushButton("More Settings…")
        more.setObjectName("secondaryButton")
        more.setFixedHeight(32)
        more.setCursor(Qt.CursorShape.PointingHandCursor)
        more.setToolTip("Fonts, margins, spacing, colors and more (⌘,)")
        more.clicked.connect(lambda: (self.close(), self.settings_requested.emit()))
        self.body.addWidget(more)
        self.more_button = more

        self._key = current_key
        self.set_theme(current_key)
        self.set_highlight(highlight)

    def set_theme(self, key: str) -> None:
        self._key = key
        for k, card in self.cards.items():
            card.setChecked(k == key)
            card.update()
        self.appearance.setIcon(half_circle_icon(themes.current().text))
        self.update()

    def set_highlight(self, on: bool) -> None:
        """Show the switch on or off (no signal)."""
        self.highlight_switch.blockSignals(True)
        self.highlight_switch.setChecked(on)
        self.highlight_switch.blockSignals(False)


# ---------- contents ----------

_LEVEL_ROLE = Qt.ItemDataRole.UserRole + 1
_READY_ROLE = Qt.ItemDataRole.UserRole + 2


class _ContentsDelegate(QStyledItemDelegate):
    """Paints a contents row: the title (wrapped, indented by level, underlined on
    hover like a link) and a fern dot when the chapter's audio is ready. Painted
    rather than one widget per row, so a book with a thousand chapters opens instantly."""

    PAD_X, PAD_Y, INDENT, DOT_AREA, DOT_RADIUS = 10, 7, 18, 22, 3.5

    @staticmethod
    def font_for(level: int) -> QFont:
        return ui_font(13, QFont.Weight.Medium if level <= 1 else QFont.Weight.Normal)

    @classmethod
    def text_rect(cls, rect: QRect, level: int) -> QRect:
        left = cls.PAD_X + cls.INDENT * min(3, max(0, level - 1))
        return rect.adjusted(left, cls.PAD_Y, -(cls.PAD_X + cls.DOT_AREA), -cls.PAD_Y)

    @classmethod
    def row_height(cls, title: str, level: int, width: int) -> int:
        rect = cls.text_rect(QRect(0, 0, width, 100_000), level)
        text = QFontMetrics(cls.font_for(level)).boundingRect(rect, Qt.TextFlag.TextWordWrap, title)
        return text.height() + 2 * cls.PAD_Y

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        t = themes.current()
        title = index.data(Qt.ItemDataRole.DisplayRole) or ""
        level = index.data(_LEVEL_ROLE) or 1
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if selected or hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.highlight if selected else t.raised_hover))
            painter.drawRoundedRect(QRectF(rect).adjusted(1, 0, -1, 0), 8, 8)
        font = self.font_for(level)
        font.setUnderline(hover)
        painter.setFont(font)
        painter.setPen(QColor(t.text))
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap
        painter.drawText(self.text_rect(rect, level), flags, title)
        if index.data(_READY_ROLE):
            y = rect.top() + self.PAD_Y + QFontMetrics(font).height() / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(ACCENT))
            painter.drawEllipse(QPointF(rect.right() - self.PAD_X - 6, y), self.DOT_RADIUS, self.DOT_RADIUS)
        painter.restore()


class ContentsPopover(Popover):
    """The book's table of contents. Every entry is a link: click one to go there
    (and to hear it, or start making its audio). A fern dot marks chapters whose
    audio is ready."""

    picked = Signal(int)  # index into entries

    WIDTH = 380

    def __init__(self, parent: QWidget | None, entries: list[tuple[str, int, bool]], current: int | None = None):
        super().__init__(parent, width=self.WIDTH)
        self.body.addWidget(_popover_title("Contents"))
        self.body.addWidget(_hairline())

        list_width = self.WIDTH - 24  # popover padding
        self.list = _popover_list(list_width)
        self.list.setItemDelegate(_ContentsDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.list.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        row_width = list_width - 14 - 2  # scroll bar, item spacing
        total = 0
        for i, (title, level, ready) in enumerate(entries):
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setData(_LEVEL_ROLE, level)
            item.setData(_READY_ROLE, ready)
            item.setToolTip("Audio ready" if ready else "")
            height = max(32, _ContentsDelegate.row_height(title, level, row_width))
            item.setSizeHint(QSize(0, height))
            total += height + 2
            self.list.addItem(item)
            if i == current:
                self.list.setCurrentItem(item)
        if entries:
            self.list.setFixedHeight(min(520, total + 4))
            self.list.itemClicked.connect(self._pick)
            self.list.itemActivated.connect(self._pick)
            self.body.addWidget(self.list)
        else:
            self.list.hide()
            empty = QLabel("This book has no table of contents.\nClick anywhere in the text instead.")
            empty.setObjectName("hint")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setFont(ui_font(13))
            self.body.addWidget(empty)
        if any(ready for _, _, ready in entries):
            legend = QLabel(f'<span style="color:{ACCENT}">●</span>&nbsp; Audio ready')
            legend.setObjectName("caption")
            legend.setFont(ui_font(11))
            legend.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.body.addWidget(legend)
        if self.list.currentItem() is not None:
            QTimer.singleShot(0, self, self._center_current)  # once laid out; never after closing

    def set_ready(self, ready: list[bool]) -> None:
        """Update the fern dots while the list is open (audio keeps arriving)."""
        for i in range(min(self.list.count(), len(ready))):
            item = self.list.item(i)
            if bool(item.data(_READY_ROLE)) != ready[i]:
                item.setData(_READY_ROLE, ready[i])
                item.setToolTip("Audio ready" if ready[i] else "")

    def _center_current(self):
        if self.list.currentItem() is not None:
            self.list.scrollToItem(self.list.currentItem(), QAbstractItemView.ScrollHint.PositionAtCenter)

    def _pick(self, item: QListWidgetItem):
        self.picked.emit(item.data(Qt.ItemDataRole.UserRole))
        self.close()


# ---------- player, progress, alerts ----------


class PlayerCapsule(QWidget):
    """The transport floating over the bottom of the page, in the frame's color,
    symmetrical around Play: [−10 sec] [⏮] [▶] [⏭] [+10 sec].

    ⏮ / ⏭ jump one paragraph; the pill-shaped text buttons jump 10 seconds.
    The capsule only reports clicks; the main window moves the player."""

    play_clicked = Signal()
    skip_clicked = Signal(int)  # -SKIP_MS or +SKIP_MS
    paragraph_clicked = Signal(int)  # -1 previous paragraph, +1 next

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._playing = False

        self.back_button = self._skip_button("−10 sec", "Back 10 seconds (←)", -SKIP_MS)
        self.prev_button = self._round_button("Previous paragraph", -1)
        self.play_button = QPushButton()
        self.play_button.setObjectName("transportButtonPrimary")
        self.play_button.setFixedSize(56, 56)
        self.play_button.setIconSize(QSize(22, 22))
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.clicked.connect(lambda: self.play_clicked.emit())
        self.next_button = self._round_button("Next paragraph", 1)
        self.forward_button = self._skip_button("+10 sec", "Forward 10 seconds (→)", SKIP_MS)
        width = max(self.back_button.sizeHint().width(), self.forward_button.sizeHint().width())
        for button in (self.back_button, self.forward_button):
            button.setFixedWidth(width)  # the same width on both sides keeps Play centered

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(12)
        for button in self.buttons():
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 70))
        self.setGraphicsEffect(shadow)
        self.set_playing(False)

    def buttons(self) -> list[QPushButton]:
        """Left to right."""
        return [self.back_button, self.prev_button, self.play_button, self.next_button, self.forward_button]

    def set_playing(self, playing: bool) -> None:
        """▶ while paused, ❚❚ while playing."""
        self._playing = playing
        self.play_button.setToolTip("Pause (Space)" if playing else "Play (Space)")
        self.play_button.setAccessibleName("Pause" if playing else "Play")
        self.refresh_icons()

    def set_play_enabled(self, enabled: bool) -> None:
        self.play_button.setEnabled(enabled)

    def set_navigation_enabled(self, enabled: bool) -> None:
        for button in (self.back_button, self.prev_button, self.next_button, self.forward_button):
            button.setEnabled(enabled)

    def refresh_icons(self) -> None:
        t = themes.current()
        glyph, dim = QColor(t.on_chrome), _with_alpha(t.on_chrome, 0.4)
        self.prev_button.setIcon(_transport_icon("previous", glyph, dim, 18))
        self.next_button.setIcon(_transport_icon("next", glyph, dim, 18))
        self.play_button.setIcon(_transport_icon("pause" if self._playing else "play", QColor(ON_ACCENT), dim, 22))

    def changeEvent(self, event):
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self.refresh_icons()  # a new theme's stylesheet
        super().changeEvent(event)

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(t.chrome_line), 1))
        p.setBrush(QColor(t.chrome))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)

    def _round_button(self, tooltip: str, step: int) -> QPushButton:
        button = QPushButton()
        button.setObjectName("transportButton")
        button.setFixedSize(44, 44)
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: self.paragraph_clicked.emit(step))
        return button

    def _skip_button(self, text: str, tooltip: str, delta_ms: int) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("skipButton")
        button.setFont(ui_font(12, QFont.Weight.DemiBold))
        button.setFixedHeight(32)  # the stylesheet rounds it with half of this
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: self.skip_clicked.emit(delta_ms))
        return button


class GradientProgressBar(QProgressBar):
    """Generation progress in blues: the gradient spans the whole bar, so the fill
    starts very light and reaches the darkest blue only at 100%."""

    # Where the gradient gets dark enough for white text (blue at ~55% of the bar).
    WHITE_TEXT_FROM = 0.55

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = r.height() / 2
        bar = QPainterPath()
        bar.addRoundedRect(r, radius, radius)
        p.setPen(QPen(QColor(t.chrome_line), 1))
        p.setBrush(QColor("#FFFFFF"))
        p.drawPath(bar)

        span = self.maximum() - self.minimum()
        done = (self.value() - self.minimum()) / span if span > 0 else 0.0
        split = r.left() + r.width() * max(0.0, min(1.0, done))
        if done > 0:
            gradient = QLinearGradient(r.left(), 0, r.right(), 0)
            for stop, color in zip((0.0, 0.35, 0.7, 1.0), PROGRESS_BLUES):
                gradient.setColorAt(stop, QColor(color))
            filled = QPainterPath()
            filled.addRect(QRectF(r.left(), r.top(), split - r.left(), r.height()))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(gradient)
            p.drawPath(bar.intersected(filled))

        # Dark text over the track and the light blues, white over the dark blues.
        p.setFont(ui_font(11, QFont.Weight.Medium))
        text = self.text()
        white_from = r.left() + r.width() * self.WHITE_TEXT_FROM
        white = QRectF(white_from, 0, max(0.0, split - white_from), self.height())
        p.save()
        dark_area = QPainterPath()
        dark_area.addRect(QRectF(self.rect()))
        cut = QPainterPath()
        cut.addRect(white)
        p.setClipPath(dark_area.subtracted(cut))
        p.setPen(QColor("#1C1C1E"))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()
        if white.width() > 0:
            p.save()
            p.setClipRect(white)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            p.restore()


class Alert(QDialog):
    """A small macOS-style alert: the app icon, a bold title, a line of text and
    a row of buttons (added by confirm() and inform())."""

    def __init__(self, parent: QWidget | None, title: str, text: str):
        super().__init__(parent)
        self.setWindowTitle("LivroSound")
        self.setModal(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(20)

        top = QHBoxLayout()
        top.setSpacing(16)
        icon = QLabel()
        icon.setPixmap(book_pixmap(60))
        icon.setContentsMargins(0, 3, 0, 0)
        top.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(6)
        texts.setContentsMargins(0, 4, 0, 0)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("alertTitle")
        self.title_label.setFont(ui_font(15, QFont.Weight.DemiBold))
        self.title_label.setWordWrap(True)
        self.title_label.setMinimumWidth(300)
        texts.addWidget(self.title_label)
        self.text_label = QLabel(text)
        self.text_label.setObjectName("alertText")
        self.text_label.setFont(ui_font(13))
        self.text_label.setWordWrap(True)
        self.text_label.setVisible(bool(text))
        texts.addWidget(self.text_label)
        texts.addStretch(1)
        top.addLayout(texts, 1)
        outer.addLayout(top)

        self.button_row = QHBoxLayout()
        self.button_row.setSpacing(10)
        outer.addLayout(self.button_row)

    def add_button(self, text: str, object_name: str, *, accept: bool) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setMinimumWidth(96)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(self.accept if accept else self.reject)
        self.button_row.addWidget(button)
        return button


def confirm(
    parent: QWidget | None, title: str, text: str, yes: str = "Yes", no: str = "No", *, destructive: bool = False
) -> bool:
    """Ask a yes/no question: No on the left, Yes (fern, or red when destructive)
    on the right. Return goes to Yes, unless Yes would destroy something."""
    alert = Alert(parent, title, text)
    no_button = alert.add_button(no, "secondaryButton", accept=False)
    alert.button_row.addStretch(1)
    yes_button = alert.add_button(yes, "dangerButton" if destructive else "primaryButton", accept=True)
    default = no_button if destructive else yes_button
    default.setDefault(True)
    default.setFocus()
    return alert.exec() == QDialog.DialogCode.Accepted


def inform(parent: QWidget | None, title: str, text: str) -> None:
    """Tell the user something, with a single OK."""
    alert = Alert(parent, title, text)
    alert.button_row.addStretch(1)
    ok = alert.add_button("OK", "primaryButton", accept=True)
    ok.setDefault(True)
    alert.exec()


# ---------- find, notes and look up ----------


def color_icon(color: str, size: int = 14) -> QIcon:
    """A round swatch of a highlighter color, for menus."""
    pixmap, p = _canvas(size, size)
    p.setPen(QPen(QColor(0, 0, 0, 60), 1))
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    p.end()
    return QIcon(pixmap)


class FindBar(QFrame):
    """Find in the book (⌘F): a search field, "3 of 27", ‹ › and Done, floating at the
    top right of the page. Typing searches after a short pause."""

    query_changed = Signal(str)
    step = Signal(int)  # -1 previous, +1 next match
    closed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("findBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 8, 6)
        row.setSpacing(6)
        self.field = QLineEdit()
        self.field.setObjectName("librarySearch")
        self.field.setPlaceholderText("Find in book")
        self.field.setClearButtonEnabled(True)
        self.field.setFixedWidth(240)
        self.field.setFixedHeight(30)
        self.field.returnPressed.connect(lambda: self.step.emit(1))
        self.count = QLabel("")
        self.count.setObjectName("caption")
        self.count.setFont(ui_font(12))
        self.count.setMinimumWidth(64)
        row.addWidget(self.field)
        row.addWidget(self.count)
        self.buttons = []
        for text, tip, value in (("‹", "Previous match (⇧⌘G)", -1), ("›", "Next match (⌘G)", 1)):
            button = QToolButton()
            button.setObjectName("libraryTool")
            button.setText(text)
            button.setToolTip(tip)
            button.setFont(ui_font(16, QFont.Weight.Medium))
            button.setFixedSize(30, 30)
            button.clicked.connect(lambda _=False, v=value: self.step.emit(v))
            row.addWidget(button)
            self.buttons.append(button)
        done = QToolButton()
        done.setObjectName("libraryTool")
        done.setText("Done")
        done.clicked.connect(self.close_bar)
        row.addWidget(done)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(220)
        self._timer.timeout.connect(lambda: self.query_changed.emit(self.field.text()))
        self.field.textChanged.connect(lambda _t: self._timer.start())
        self.hide()

    def open(self) -> None:
        self.show()
        self.raise_()
        self.field.setFocus()
        self.field.selectAll()

    def close_bar(self) -> None:
        self.hide()
        self.closed.emit()

    def set_count(self, index: int, total: int, capped: bool = False) -> None:
        if not self.field.text().strip():
            self.count.setText("")
        elif not total:
            self.count.setText("Not found")
        else:
            self.count.setText(f"{index + 1} of {total}{'+' if capped else ''}")
        for button in self.buttons:
            button.setEnabled(total > 1)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(t.line), 1))
        p.setBrush(QColor(t.panel))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)


class NotesPopover(Popover):
    """The reader's highlights and notes in this book, in reading order; clicking one
    goes there. Each entry: (id, color, excerpt, note, chapter)."""

    picked = Signal(str)
    WIDTH = 380

    def __init__(self, parent: QWidget | None, entries: list[tuple[str, str, str, str, str]]):
        super().__init__(parent, width=self.WIDTH + 24)
        self.body.addWidget(_popover_title("Highlights & Notes"))
        self.body.addWidget(_hairline())
        if not entries:
            empty = QLabel("Select text in the book to highlight it or add a note.")
            empty.setObjectName("caption")
            empty.setWordWrap(True)
            empty.setFont(ui_font(13))
            self.body.addWidget(empty)
            self.list = None
            return
        self.list = _popover_list(self.WIDTH)
        for item_id, color, excerpt, note, chapter in entries:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(8, 6, 8, 6)
            bar = QFrame()
            bar.setFixedWidth(4)
            bar.setStyleSheet(f"background: {color}; border-radius: 2px;")
            layout.addWidget(bar)
            texts = QVBoxLayout()
            texts.setSpacing(2)
            quote = QLabel(QFontMetrics(ui_font(13)).elidedText(" ".join(excerpt.split()), Qt.TextElideMode.ElideRight,
                                                                 2 * (self.WIDTH - 60)))
            quote.setWordWrap(True)
            quote.setFont(reading_font(13))
            texts.addWidget(quote)
            if note:
                label = QLabel(note)
                label.setWordWrap(True)
                label.setFont(ui_font(12, QFont.Weight.Medium))
                texts.addWidget(label)
            if chapter:
                where = QLabel(chapter)
                where.setObjectName("caption")
                where.setFont(ui_font(11))
                texts.addWidget(where)
            layout.addLayout(texts, 1)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, item_id)
            row.setFixedWidth(self.WIDTH - 8)
            row.adjustSize()
            item.setSizeHint(QSize(self.WIDTH - 8, row.sizeHint().height()))
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        self.list.setFixedHeight(min(460, sum(self.list.sizeHintForRow(i) + 2 for i in range(self.list.count())) + 6))
        self.list.itemClicked.connect(lambda item: (self.picked.emit(item.data(Qt.ItemDataRole.UserRole)), self.close()))
        self.body.addWidget(self.list)


class DefinitionPopover(Popover):
    """Look Up: the word, its definition from the Mac's own dictionary (New Oxford
    American), and links to the Dictionary app, Merriam-Webster and Oxford online."""

    open_url = Signal(str)

    def __init__(self, parent: QWidget | None, word: str, definition: str | None,
                 links: list[tuple[str, str, str]]):
        super().__init__(parent, arrow=False, width=420)
        title = QLabel(word)
        title.setFont(reading_font(20, QFont.Weight.DemiBold))
        self.body.addWidget(title)
        text = QLabel(definition or "This Mac's dictionary has no entry for this word. Try a dictionary below.")
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.setFont(ui_font(13))
        text.setObjectName("" if definition else "caption")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(text)
        scroll.setFixedHeight(min(320, max(60, text.sizeHint().height() + 12 if not definition else 300)))
        self.definition = text
        self.body.addWidget(scroll)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.link_buttons = []
        for label, url, tip in links:
            button = QPushButton(label)
            button.setObjectName("secondaryButton")
            button.setFixedHeight(30)
            button.setToolTip(tip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, u=url: self.open_url.emit(u))
            row.addWidget(button)
            self.link_buttons.append(button)
        self.body.addLayout(row)

    def popup_at(self, point: QPoint) -> None:
        self.adjustSize()
        screen = (self.screen()).availableGeometry()
        x = max(screen.left() + 8, min(point.x() - self.width() // 2, screen.right() - self.width() - 8))
        y = point.y() + 14
        if y + self.height() > screen.bottom() - 8:
            y = max(screen.top() + 8, point.y() - self.height() - 14)
        self.move(x, y)
        self.show()
