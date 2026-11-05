"""The dialogs on the way from a file to listening, and back to a book later.

Upload: ImportProgressDialog while the book is read → LayoutDialog →
ChooseVoiceDialog → CustomizeAudioDialog. The library page: BookDetailsDialog.

They keep Script Radio's Casting Panel look: a big centered title (castingTitle)
over a muted subtitle (castingSubtitle), rows of label + switch, a quiet button
on the left (goBackButton) and the fern one on the right (generateAudioButton).
Colors come from the main window's stylesheet (theme.stylesheet), which a dialog
inherits from its parent, and, in the painted cards, from theme.current() at
paint time.

Every dialog has a classmethod ask(...) that runs it modally and returns the
answer (None or "cancel" when the user backs out). Building one without exec()
gives tests a dialog they can drive.

Non-obvious rules:
- ChooseVoiceDialog keeps one VoicePreviews per parent window, found again by
  object name on the next opening: a clip still being rendered when the dialog
  closes finishes on a worker thread that must outlive the dialog.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QComboBox,
    QCompleter,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import voice_preview, voices
from ..book import Book
from . import theme as themes
from .theme import ACCENT, ON_ACCENT, reading_font, ui_font
from .widgets import GradientProgressBar, ToggleSwitch, VoicePicker, VoicePreviews, book_pixmap

if TYPE_CHECKING:
    from ..engines.base import TTSEngine
    from ..library import BookRecord, Settings

PAUSE_RANGE = (0.2, 2.0)  # seconds between paragraphs, in steps of 0.1


# ---------- shared pieces ----------


def _button(text: str, object_name: str, *, default: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(object_name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if default:
        button.setDefault(True)
    return button


def _hint(text: str = "", size: float = 12) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setFont(ui_font(size))
    label.setWordWrap(True)
    return label


class _Dialog(QDialog):
    """A modal dialog with the Casting Panel's margins, title and subtitle."""

    def __init__(self, parent: QWidget | None, title: str, subtitle: str):
        super().__init__(parent)
        self.setWindowTitle(f"LivroSound | {title}")
        self.setModal(True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(28, 24, 28, 22)
        self.body.setSpacing(12)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("castingTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setFont(ui_font(26, QFont.Weight.DemiBold))
        self.body.addWidget(self.title_label)

        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("castingSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle_label.setFont(ui_font(14))
        self.body.addWidget(self.subtitle_label)
        self.body.addSpacing(8)

    def _add_buttons(self, left: QPushButton, right: QPushButton) -> None:
        """The bottom row: left button, space, the fern button (Script Radio's Go Back / Generate Audio)."""
        row = QHBoxLayout()
        row.addWidget(left)
        row.addStretch(1)
        row.addWidget(right)
        self.body.addSpacing(10)
        self.body.addLayout(row)


class _SwitchRow(QWidget):
    """A label (with an optional hint under it) and a switch at the right."""

    def __init__(self, text: str, checked: bool, *, hint: str = "", tooltip: str = "", enabled: bool = True):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(16)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.label = QLabel(text)
        self.label.setFont(ui_font(14))
        self.label.setWordWrap(True)
        if not enabled:
            self.label.setObjectName("hint")  # the stylesheet doesn't dim a disabled label
        texts.addWidget(self.label)
        self.hint = _hint(hint)
        self.hint.setVisible(bool(hint))
        texts.addWidget(self.hint)
        layout.addLayout(texts, 1)
        self.switch = ToggleSwitch()
        self.switch.setChecked(checked)
        self.switch.setEnabled(enabled)
        layout.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)
        if tooltip:
            self.label.setToolTip(tooltip)
            self.switch.setToolTip(tooltip)


# ---------- Reading a book ----------


class ImportProgressDialog(QDialog):
    """A compact modal shown while a book is read: what's happening, a progress bar
    and Cancel. Cancel (or Esc, or closing it) emits canceled once and waits: the
    main window closes the dialog with finish() when the import thread ends."""

    canceled = Signal()

    def __init__(self, parent: QWidget | None, title: str):
        super().__init__(parent)
        self.setWindowTitle("LivroSound")
        self.setModal(True)
        self.setFixedWidth(460)
        self._finished = False
        self._canceling = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(16)

        top = QHBoxLayout()
        top.setSpacing(16)
        icon = QLabel()
        icon.setPixmap(book_pixmap(56))
        top.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(5)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("alertTitle")
        self.title_label.setFont(ui_font(15, QFont.Weight.DemiBold))
        self.title_label.setWordWrap(True)
        texts.addWidget(self.title_label)
        self.message = QLabel("Getting ready…")
        self.message.setObjectName("alertText")
        self.message.setFont(ui_font(13))
        texts.addWidget(self.message)
        top.addLayout(texts, 1)
        outer.addLayout(top)

        self.bar = GradientProgressBar()
        self.bar.setFixedHeight(20)
        self.bar.setRange(0, 0)  # no count yet
        outer.addWidget(self.bar)

        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_button = _button("Cancel", "secondaryButton")
        self.cancel_button.setMinimumWidth(96)
        self.cancel_button.clicked.connect(self.reject)
        row.addWidget(self.cancel_button)
        outer.addLayout(row)

    def set_progress(self, done: int, total: int, message: str = "") -> None:
        """ingest's progress callback, relayed on the UI thread: (done, total, message)."""
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(max(0, min(done, total)))
        if not self._canceling:
            self.message.setText(message or (f"{done} of {total}" if total > 0 else "Reading…"))

    def is_canceling(self) -> bool:
        return self._canceling

    def finish(self) -> None:
        """Close for good (the import ended: done, failed or canceled)."""
        self._finished = True
        self.accept()

    def reject(self):
        if self._finished:
            super().reject()
            return
        if not self._canceling:
            self._canceling = True
            self.cancel_button.setEnabled(False)
            self.message.setText("Canceling…")
            self.canceled.emit()


# ---------- Choose a Layout ----------


class LayoutCard(QAbstractButton):
    """A big selectable card: a page thumbnail on top, a title and a one-line
    description below. Fern ring and check badge when chosen; double-click = Continue."""

    activated = Signal()

    PAD = 14
    THUMB = QSize(276, 280)

    def __init__(self, title: str, description: str, paint_thumbnail: Callable[[QPainter, QRectF], None]):
        super().__init__()
        self.title = title
        self.description = description
        self._paint_thumbnail = paint_thumbnail
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setAccessibleName(title)
        self.setToolTip(description)
        self.setFixedSize(self.THUMB.width() + 2 * self.PAD, self.preferred_height())

    def preferred_height(self) -> int:
        """Tall enough for the thumbnail, the title and the wrapped description."""
        title_h = QFontMetrics(self._title_font()).height()
        bounds = QRect(0, 0, self.THUMB.width(), 1000)
        flags = Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap
        text_h = QFontMetrics(self._description_font()).boundingRect(bounds, flags, self.description).height()
        return self.PAD + self.THUMB.height() + 14 + title_h + 3 + text_h + 14

    @staticmethod
    def _title_font() -> QFont:
        return ui_font(15, QFont.Weight.DemiBold)

    @staticmethod
    def _description_font() -> QFont:
        return ui_font(12)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(True)
            self.activated.emit()

    def paintEvent(self, event):
        t = themes.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        chosen = self.isChecked()
        if chosen:
            pen = QPen(QColor(ACCENT), 2.5)
        else:
            pen = QPen(QColor(t.muted if self.underMouse() else t.line), 1)
        p.setPen(pen)
        p.setBrush(QColor(t.raised))
        inset = pen.widthF() / 2
        p.drawRoundedRect(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), 16, 16)

        thumb = QRectF(self.PAD, self.PAD, self.THUMB.width(), self.THUMB.height())
        clip = QPainterPath()
        clip.addRoundedRect(thumb, 10, 10)
        p.save()
        p.setClipPath(clip)
        p.fillRect(thumb, QColor(t.window))
        self._paint_thumbnail(p, thumb)
        p.restore()
        if chosen:
            _check_badge(p, QPointF(thumb.right() - 16, thumb.top() + 16))

        p.setPen(QColor(t.text))
        p.setFont(self._title_font())
        title_h = QFontMetrics(self._title_font()).height()
        top = thumb.bottom() + 14
        p.drawText(QRectF(self.PAD, top, thumb.width(), title_h), Qt.AlignmentFlag.AlignCenter, self.title)
        p.setPen(QColor(t.muted))
        p.setFont(self._description_font())
        flags = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap
        below = QRectF(self.PAD, top + title_h + 3, thumb.width(), self.height() - top - title_h)
        p.drawText(below, flags, self.description)


def _check_badge(p: QPainter, center: QPointF) -> None:
    p.setPen(QPen(QColor(ON_ACCENT), 2))
    p.setBrush(QColor(ACCENT))
    p.drawEllipse(center, 12, 12)
    p.setPen(QPen(QColor(ON_ACCENT), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    x, y = center.x(), center.y()
    p.drawPolyline(QPolygonF([QPointF(x - 5, y + 0.5), QPointF(x - 1.5, y + 4), QPointF(x + 5, y - 3.5)]))


def _page_rect(area: QRectF, aspect: float = 0.72) -> QRectF:
    """A portrait page centered in the thumbnail area."""
    height = area.height() - 32
    width = min(area.width() - 32, height * aspect)
    return QRectF(area.center().x() - width / 2, area.top() + 16, width, height)


def _paint_page(p: QPainter, page: QRectF, fill: str, border: QColor) -> None:
    """A sheet of paper with a soft shadow."""
    p.setPen(Qt.PenStyle.NoPen)
    for spread, alpha in ((3, 10), (1.5, 16)):
        p.setBrush(QColor(0, 0, 0, alpha))
        p.drawRoundedRect(page.adjusted(-spread, -spread + 2, spread, spread + 2), 4 + spread, 4 + spread)
    p.setPen(QPen(border, 1))
    p.setBrush(QColor(fill))
    p.drawRoundedRect(page, 4, 4)


def _paint_pixmap(p: QPainter, area: QRectF, pixmap: QPixmap) -> None:
    """A real preview, scaled to fit the thumbnail and kept whole."""
    t = themes.current()
    room = area.adjusted(16, 16, -16, -16).size()
    size = pixmap.deviceIndependentSize().scaled(room, Qt.AspectRatioMode.KeepAspectRatio)
    target = QRectF(0, 0, size.width(), size.height())
    target.moveCenter(area.center())
    _paint_page(p, target, t.page, QColor(t.line))
    clip = QPainterPath()
    clip.addRoundedRect(target, 4, 4)
    p.save()
    p.setClipPath(clip)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
    p.restore()


def _paint_page_sketch(p: QPainter, area: QRectF) -> None:
    """The original layout without a preview: an outlined page with lines of type."""
    t = themes.current()
    page = _page_rect(area)
    _paint_page(p, page, "#FFFFFF" if not t.dark else t.raised, QColor(t.muted))
    ink = QColor(t.muted)
    ink.setAlphaF(0.45)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(ink)
    left, width = page.left() + 18, page.width() - 36
    y = page.top() + 26
    p.drawRoundedRect(QRectF(page.center().x() - width * 0.22, y, width * 0.44, 6), 3, 3)  # a heading
    y += 26
    # Paragraphs of justified lines: indented first lines, short last lines.
    for lines, last in ((5, 0.62), (4, 0.4), (6, 0.78), (3, 0.5)):
        for i in range(lines):
            indent = 12 if i == 0 else 0
            span = width * last if i == lines - 1 else width
            if y + 4 > page.bottom() - 18:
                return
            p.drawRoundedRect(QRectF(left + indent, y, span - indent, 4), 2, 2)
            y += 10
        y += 6


def _paint_reflow_sample(p: QPainter, area: QRectF, book: Book) -> None:
    """The LivroSound layout without a preview: the book's first heading and
    paragraph set in the reading serif on a page in the theme's colors."""
    t = themes.current()
    page = _page_rect(area)
    _paint_page(p, page, t.page, QColor(t.line))
    heading, paragraph = _opening(book)
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    inner = page.adjusted(18, 22, -18, -14)
    doc.setTextWidth(inner.width())
    cursor = QTextCursor(doc)
    color = QColor(t.text)
    if heading:
        block = QTextBlockFormat()
        block.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        block.setBottomMargin(10)
        chars = QTextCharFormat()
        font = reading_font(12, QFont.Weight.DemiBold)
        font.setPixelSize(13)
        chars.setFont(font)
        chars.setForeground(color)
        cursor.setBlockFormat(block)
        cursor.insertText(heading, chars)
        cursor.insertBlock()
    block = QTextBlockFormat()
    block.setTextIndent(14)
    block.setLineHeight(145, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
    chars = QTextCharFormat()
    font = reading_font(9)
    font.setPixelSize(10)
    chars.setFont(font)
    chars.setForeground(color)
    cursor.setBlockFormat(block)
    cursor.insertText(paragraph, chars)
    p.save()
    p.translate(inner.topLeft())
    doc.drawContents(p, QRectF(0, 0, inner.width(), inner.height()))
    p.restore()
    # The text fades out at the bottom of the page instead of stopping mid-line.
    fade = QLinearGradient(0, page.bottom() - 46, 0, page.bottom() - 6)
    clear = QColor(t.page)
    clear.setAlpha(0)
    fade.setColorAt(0, clear)
    fade.setColorAt(1, QColor(t.page))
    p.fillRect(QRectF(page.left() + 1, page.bottom() - 46, page.width() - 2, 45), fade)


def _opening(book: Book) -> tuple[str, str]:
    """The heading just before the book's first real paragraph, and that paragraph."""
    heading = ""
    for block in book.blocks:
        if block.skipped or not block.text.strip():
            continue
        if block.kind == "heading":
            heading = block.text
        elif block.kind == "paragraph" and len(block.text) >= 60:
            text = block.text
            # Enough for a thumbnail; QTextDocument needn't lay out a whole chapter.
            return heading, text[:900]
    return heading or book.title, ""


class LayoutDialog(_Dialog):
    """Keep the book's own layout, or read it in LivroSound's."""

    def __init__(
        self,
        parent: QWidget | None,
        book: Book,
        preview_original: QPixmap | None = None,
        preview_reflow: QPixmap | None = None,
    ):
        super().__init__(parent, "Choose a Layout", "You can change this any time from Aa.")

        def original(p: QPainter, area: QRectF):
            if preview_original is not None and not preview_original.isNull():
                _paint_pixmap(p, area, preview_original)
            else:
                _paint_page_sketch(p, area)

        def reflow(p: QPainter, area: QRectF):
            if preview_reflow is not None and not preview_reflow.isNull():
                _paint_pixmap(p, area, preview_reflow)
            else:
                _paint_reflow_sample(p, area, book)

        self.cards = {
            "original": LayoutCard("Keep Original Layout", "The pages exactly as in your file.", original),
            "livrosound": LayoutCard("LivroSound Layout", "Clean, book-like text in Iowan, like Apple Books.", reflow),
        }
        group = QButtonGroup(self)
        row = QHBoxLayout()
        row.setSpacing(20)
        row.addStretch(1)
        height = max(card.preferred_height() for card in self.cards.values())
        for card in self.cards.values():
            card.setFixedHeight(height)  # side by side, the same size
            group.addButton(card)
            card.activated.connect(self.accept)
            row.addWidget(card)
        row.addStretch(1)
        self.body.addLayout(row)

        self.cancel_button = _button("Cancel", "goBackButton")
        self.cancel_button.clicked.connect(self.reject)
        self.continue_button = _button("Continue", "generateAudioButton", default=True)
        self.continue_button.clicked.connect(self.accept)
        self._add_buttons(self.cancel_button, self.continue_button)
        group.buttonToggled.connect(lambda *_: self.continue_button.setEnabled(self.choice() is not None))

        self.cards["original" if book.source_kind == "pdf" else "livrosound"].setChecked(True)
        self.continue_button.setEnabled(self.choice() is not None)

    @classmethod
    def ask(
        cls,
        parent: QWidget | None,
        book: Book,
        preview_original: QPixmap | None = None,
        preview_reflow: QPixmap | None = None,
    ) -> str | None:
        dialog = cls(parent, book, preview_original, preview_reflow)
        return dialog.choice() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def choice(self) -> str | None:
        """"original" | "livrosound", or None if neither is chosen."""
        return next((mode for mode, card in self.cards.items() if card.isChecked()), None)


# ---------- Choose a Voice ----------


class ChooseVoiceDialog(_Dialog):
    """The one narrator, with ▶ previews read by the app's engine (Pocket-TTS). When the
    engine can't run here (an Intel Mac), it says why and Continue stays off."""

    PREVIEWS_NAME = "chooseVoicePreviews"

    def __init__(
        self,
        parent: QWidget | None,
        engine: TTSEngine,
        current_voice: str,
        previews_render: Callable[[TTSEngine, str], Path] = voice_preview.make_preview,
        *,
        before_play: Callable[[], None] | None = None,
    ):
        super().__init__(parent, "Choose a Voice", "Pick the narrator who will read your book aloud.")
        self.setMinimumWidth(560)
        ok, why = engine.available()
        offered = set(engine.voices()) if ok else set()
        groups = [(group, [v.name for v in members if v.name in offered]) for group, members in voices.grouped()]
        groups = [(group, names) for group, names in groups if names]
        names = [voice for _, members in groups for voice in members]
        if current_voice not in names:
            current_voice = voices.DEFAULT_VOICE if voices.DEFAULT_VOICE in names else (names[0] if names else "")
        self._voice = current_voice

        self.previews = self._shared_previews(parent if parent is not None else self)
        self.previews.before_play = before_play or (lambda: None)
        self.previews.set_render((lambda voice: previews_render(engine, voice)) if ok else None)

        row = QHBoxLayout()
        row.setSpacing(18)
        row.addWidget(self._row_label("Narrator"))
        self.picker = VoicePicker(groups, self._voice, self.previews)
        self.picker.changed.connect(self._on_voice)
        self.picker.setEnabled(ok)
        row.addWidget(self.picker, 1)
        self.body.addLayout(row)
        self.note = _hint(why or engine.install_hint)
        self.note.setVisible(not ok)
        self.body.addWidget(self.note)
        self.body.addStretch(1)

        self.cancel_button = _button("Cancel", "goBackButton")
        self.cancel_button.clicked.connect(self.reject)
        self.continue_button = _button("Continue", "generateAudioButton", default=True)
        self.continue_button.clicked.connect(self.accept)
        self._add_buttons(self.cancel_button, self.continue_button)
        self.continue_button.setEnabled(self.choice() is not None)
        if self.choice():
            self.previews.warm([self._voice])  # loads the engine too, so ▶ and generation start sooner

    @classmethod
    def ask(
        cls,
        parent: QWidget | None,
        engine: TTSEngine,
        current_voice: str,
        previews_render: Callable[[TTSEngine, str], Path] = voice_preview.make_preview,
        *,
        before_play: Callable[[], None] | None = None,
    ) -> str | None:
        """The narrator chosen (None: Cancel)."""
        dialog = cls(parent, engine, current_voice, previews_render, before_play=before_play)
        return dialog.choice() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def choice(self) -> str | None:
        """The voice, or None when the engine can't run here or has no voices."""
        return self._voice if self.picker.isEnabled() and self._voice else None

    def done(self, result: int):
        # The shared previews outlive the dialog: stop the clip, drop queued renders.
        self.previews.stop()
        self.previews.set_render(None)
        self.previews.before_play = lambda: None
        super().done(result)

    def _on_voice(self, voice: str):
        self._voice = voice
        self.previews.warm([voice])

    @staticmethod
    def _row_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(ui_font(14))
        label.setMinimumHeight(40)  # the picker's height, so the name lines up with its middle
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return label

    @classmethod
    def _shared_previews(cls, owner: QWidget) -> VoicePreviews:
        previews = owner.findChild(VoicePreviews, cls.PREVIEWS_NAME, Qt.FindChildOption.FindDirectChildrenOnly)
        if previews is None:
            previews = VoicePreviews(None, owner)
            previews.setObjectName(cls.PREVIEWS_NAME)
        return previews


# ---------- Customize Audio ----------


class CustomizeAudioDialog(_Dialog):
    """The last stop before generating: reading options, saved as they change.
    Generate Audio starts right away (no extra confirmation), as in Script Radio."""

    def __init__(self, parent: QWidget | None, settings: Settings, whisper: tuple[bool, str]):
        super().__init__(parent, "Customize Audio", "Fine-tune the reading, then press Generate Audio to start.")
        self.setMinimumWidth(520)
        self._settings = settings
        self._went_back = False
        whisper_ok, whisper_why = whisper

        self.switches: dict[str, ToggleSwitch] = {}
        rows = (
            ("highlight", "Highlight sentences during playback", settings.highlight, ""),
            ("read_headings", "Read chapter titles", settings.read_headings, "Announces each chapter before its text."),
            ("read_footnotes", "Read footnotes", settings.read_footnotes, "Reads the book's notes where they appear."),
        )
        for field, text, checked, tooltip in rows:
            self._add_switch(field, _SwitchRow(text, checked, tooltip=tooltip))
        whisper_tip = "Transcribes each sentence on this Mac and remakes any that come out wrong."
        self._add_switch(
            "validate_audio",
            _SwitchRow(
                "Check every sentence with Whisper (slower)",
                settings.validate_audio and whisper_ok,
                hint="" if whisper_ok else whisper_why,
                tooltip=whisper_tip if whisper_ok else "",
                enabled=whisper_ok,
            ),
        )

        pause = QWidget()
        pause_layout = QVBoxLayout(pause)
        pause_layout.setContentsMargins(6, 14, 6, 4)
        pause_layout.setSpacing(8)
        self.pause_label = QLabel()
        self.pause_label.setFont(ui_font(14))
        pause_layout.addWidget(self.pause_label)
        self.pause_slider = QSlider(Qt.Orientation.Horizontal)
        low, high = (round(v * 10) for v in PAUSE_RANGE)
        self.pause_slider.setRange(low, high)
        self.pause_slider.setSingleStep(1)
        self.pause_slider.setPageStep(2)
        self.pause_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        # Fern up to the handle, like the switches above it.
        self.pause_slider.setStyleSheet(f"QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}")
        self.pause_slider.setValue(min(high, max(low, round(settings.paragraph_pause * 10))))
        self.pause_slider.valueChanged.connect(self._on_pause)
        pause_layout.addWidget(self.pause_slider)
        ends = QHBoxLayout()
        ends.addWidget(_hint(f"{PAUSE_RANGE[0]:.1f} s", 11))
        ends.addStretch(1)
        ends.addWidget(_hint(f"{PAUSE_RANGE[1]:.1f} s", 11))
        pause_layout.addLayout(ends)
        self.body.addWidget(pause)
        self._show_pause(self.pause_slider.value())
        self.body.addStretch(1)

        self.back_button = _button("Go Back", "goBackButton")
        self.back_button.setToolTip("Return to Choose a Voice")
        self.back_button.clicked.connect(self.go_back)
        self.generate_button = _button("Generate Audio", "generateAudioButton", default=True)
        self.generate_button.clicked.connect(self.accept)
        self._add_buttons(self.back_button, self.generate_button)

    @classmethod
    def ask(cls, parent: QWidget | None, settings: Settings, whisper: tuple[bool, str]) -> str:
        """"generate", "back" (to Choose a Voice) or "cancel"."""
        dialog = cls(parent, settings, whisper)
        return dialog.outcome(dialog.exec())

    def outcome(self, result: int) -> str:
        if result == QDialog.DialogCode.Accepted:
            return "generate"
        return "back" if self._went_back else "cancel"

    def go_back(self) -> None:
        self._went_back = True
        self.reject()

    def _add_switch(self, field: str, row: _SwitchRow) -> None:
        row.switch.toggled.connect(lambda on, f=field: self._set(f, on))
        self.switches[field] = row.switch
        self.body.addWidget(row)

    def _set(self, field: str, value) -> None:
        setattr(self._settings, field, value)
        self._settings.save()

    def _show_pause(self, tenths: int) -> None:
        self.pause_label.setText(f"Pause between paragraphs: {tenths / 10:.1f} s")

    def _on_pause(self, tenths: int):
        self._show_pause(tenths)
        self._set("paragraph_pause", tenths / 10)


# ---------- Book Details ----------


def opened_text(iso: str, today: date) -> str:
    """'today', 'yesterday', 'Oct 22', 'Oct 22, 2025' ('' when unknown)."""
    try:
        day = datetime.fromisoformat(iso).date()
    except ValueError:
        return ""
    if day == today:
        return "today"
    if (today - day).days == 1:
        return "yesterday"
    text = f"{day:%b} {day.day}"
    return text if day.year == today.year else f"{text}, {day.year}"


def detail_rows(record: BookRecord, today: date) -> list[tuple[str, str]]:
    """(label, value) for Book Details, leaving out what isn't known."""
    from .. import shelf

    fraction = shelf.read_fraction(record)
    status = shelf.status(record)
    progress = status if status != "Reading" else f"{max(1, math.floor(fraction * 100))}% read"
    remaining = shelf.seconds_remaining(record)
    if remaining is not None and status != "Finished":
        progress += f" · about {shelf.duration_text(remaining)} left to hear"
    ready = 100 if record.generated_fraction >= 1 else math.floor(record.generated_fraction * 100)
    rows = [
        ("Author", record.author),
        ("Format", record.source_kind.upper()),
        ("Pages", str(record.pages) if record.pages else ""),
        ("Series", record.series + (f", book {record.series_index:g}" if record.series and record.series_index else "")),
        ("Published", record.published),
        ("Subjects", ", ".join(shelf.strings(record.subjects))),
        ("Progress", progress),
        ("Audio", f"{ready}% made with {record.voice}" if ready and record.voice else "Not made yet"),
        ("Added", opened_text(record.date_added or record.last_opened, today)),
        ("Last read", opened_text(record.last_opened, today)),
        ("File", record.source_path),
    ]
    return [(label, value) for label, value in rows if value]


class BookDetailsDialog(_Dialog):
    """A book's facts (format, pages, series, subjects, progress, audio) and its tags and
    collections, which the user edits here (comma-separated, names in use offered)."""

    def __init__(self, parent: QWidget | None, record: BookRecord, tags_in_use: list[str],
                 collections_in_use: list[str]):
        super().__init__(parent, record.title or "Untitled", "")
        self.subtitle_label.hide()
        self.setMinimumWidth(560)
        self.title_label.setFont(reading_font(22, QFont.Weight.DemiBold))
        self.title_label.setWordWrap(True)
        self._record = record
        card = QFrame()
        card.setObjectName("card")
        grid = QGridLayout(card)
        grid.setContentsMargins(18, 14, 18, 14)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        for row, (label, value) in enumerate(detail_rows(record, date.today())):
            name = _hint(label, 12)
            name.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            text = QLabel(value)
            text.setFont(ui_font(13))
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(name, row, 0)
            grid.addWidget(text, row, 1)
        grid.setColumnStretch(1, 1)
        self.body.addWidget(card)
        self.tags = self._names_field("Tags", record.tags, tags_in_use)
        self.collections = self._names_field("Collections", record.collections, collections_in_use)
        self.cancel_button = _button("Cancel", "goBackButton")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = _button("Save", "generateAudioButton", default=True)
        self.save_button.clicked.connect(self.accept)
        self._add_buttons(self.cancel_button, self.save_button)

    def _names_field(self, label: str, values: list, in_use: list[str]) -> QLineEdit:
        from .. import shelf

        row = QHBoxLayout()
        name = QLabel(label)
        name.setFont(ui_font(14))
        name.setFixedWidth(96)
        edit = QLineEdit(", ".join(shelf.strings(values)))
        edit.setPlaceholderText("Separate names with commas")
        edit.setFont(ui_font(13))
        if in_use:
            completer = QCompleter(in_use, edit)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            edit.setCompleter(completer)
        row.addWidget(name)
        row.addWidget(edit, 1)
        self.body.addLayout(row)
        return edit

    def names(self) -> tuple[list[str], list[str]]:
        """(tags, collections) as typed."""
        def split(text: str) -> list[str]:
            out: list[str] = []
            for part in text.split(","):
                part = " ".join(part.split())
                if part and part.casefold() not in (o.casefold() for o in out):
                    out.append(part)
            return out

        return split(self.tags.text()), split(self.collections.text())

    @classmethod
    def ask(cls, parent: QWidget | None, record: BookRecord, tags_in_use: list[str],
            collections_in_use: list[str]) -> tuple[list[str], list[str]] | None:
        """The (tags, collections) to keep, or None (Cancel)."""
        dialog = cls(parent, record, tags_in_use, collections_in_use)
        return dialog.names() if dialog.exec() == QDialog.DialogCode.Accepted else None


# ---------- Settings ----------

WEIGHTS = {300: "Light", 400: "Regular", 500: "Medium", 600: "Semibold", 700: "Bold"}
DEFAULT_FONT_LABEL = "Iowan Old Style (default)"
SHORTCUTS = (
    ("Space", "Play or pause"),
    ("← / →", "Back or forward 10 seconds"),
    ("↑ / ↓, Page Up / Page Down", "Turn the page (single and double page)"),
    ("⌘F", "Find in the book"),
    ("⌘G / ⇧⌘G", "Next or previous match"),
    ("⌘+ / ⌘−", "Larger or smaller text"),
    ("⌘O", "Upload a new book"),
    ("⇧⌘L", "Go to Library"),
    ("⌘,", "Settings"),
)


class SettingsDialog(_Dialog):
    """Everything the reader can tune, in tabs (Readest's Settings): Font, Layout,
    Color, Reading, General and Shortcuts. Every change goes into Settings at once
    and is announced with changed(field) so the window applies it live; the
    window saves."""

    changed = Signal(str)
    TABS = ("Font", "Layout", "Color", "Reading", "General", "Shortcuts")

    def __init__(self, parent: QWidget | None, settings: Settings, *, add_font: Callable[[], list[str]] | None = None,
                 pick_color: Callable[[str, str], str | None] | None = None):
        super().__init__(parent, "Settings", "")
        self.subtitle_label.hide()
        self.setMinimumSize(600, 620)
        self._settings = settings
        self._add_font = add_font
        self._pick_color = pick_color or _pick_color
        self.controls: dict[str, QWidget] = {}
        from .widgets import segmented_control

        tabs, self.tab_buttons = segmented_control([(t, t, "") for t in self.TABS], "Font", self.show_tab, self)
        self.body.addWidget(tabs)
        self.pages = QStackedWidget()
        for build in (self._font_tab, self._layout_tab, self._color_tab, self._reading_tab, self._general_tab,
                      self._shortcuts_tab):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(6, 10, 6, 6)
            layout.setSpacing(6)
            build(layout)
            layout.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            self.pages.addWidget(scroll)
        self.body.addWidget(self.pages, 1)
        done = _button("Done", "generateAudioButton", default=True)
        done.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(done)
        self.body.addLayout(row)
        self.done_button = done

    @classmethod
    def ask(cls, parent: QWidget | None, settings: Settings, on_change: Callable[[str], None], **kwargs) -> None:
        dialog = cls(parent, settings, **kwargs)
        dialog.changed.connect(on_change)
        dialog.exec()

    def show_tab(self, name: str) -> None:
        self.pages.setCurrentIndex(self.TABS.index(name))
        self.tab_buttons[name].setChecked(True)

    def _set(self, field: str, value) -> None:
        if getattr(self._settings, field) != value:
            setattr(self._settings, field, value)
            self.changed.emit(field)

    # ---------- rows ----------

    def _slider(self, layout: QVBoxLayout, field: str, label: str, low: float, high: float, step: float,
                show: Callable[[float], str], *, scale: float = 1.0) -> QSlider:
        """A labeled slider for a numeric setting (value = slider position × step)."""
        title = QLabel()
        title.setFont(ui_font(13))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(round(low / step), round(high / step))
        slider.setCursor(Qt.CursorShape.PointingHandCursor)
        slider.setStyleSheet(f"QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}")
        value = getattr(self._settings, field) / scale
        slider.setValue(round(value / step))

        def moved(position: int):
            number = position * step
            title.setText(f"{label}: {show(number)}")
            kind = type(getattr(self._settings, field))
            self._set(field, kind(round(number * scale, 3)) if kind is float else kind(round(number * scale)))

        slider.valueChanged.connect(moved)
        title.setText(f"{label}: {show(slider.value() * step)}")
        layout.addSpacing(4)
        layout.addWidget(title)
        layout.addWidget(slider)
        self.controls[field] = slider
        return slider

    def _switch(self, layout: QVBoxLayout, field: str, text: str, hint: str = "") -> ToggleSwitch:
        row = _SwitchRow(text, bool(getattr(self._settings, field)), hint=hint)
        row.switch.toggled.connect(lambda on: self._set(field, on))
        layout.addWidget(row)
        self.controls[field] = row.switch
        return row.switch

    def _segments(self, layout: QVBoxLayout, field: str, label: str, choices) -> dict[str, QToolButton]:
        from .widgets import segmented_control

        caption = QLabel(label)
        caption.setFont(ui_font(13))
        layout.addSpacing(4)
        layout.addWidget(caption)
        frame, buttons = segmented_control(choices, getattr(self._settings, field), lambda k: self._set(field, k), self)
        layout.addWidget(frame)
        self.controls[field] = frame
        return buttons

    # ---------- tabs ----------

    def _font_tab(self, layout: QVBoxLayout) -> None:
        from . import fonts

        caption = QLabel("Font")
        caption.setFont(ui_font(13))
        layout.addWidget(caption)
        row = QHBoxLayout()
        self.font_combo = QComboBox()
        self.font_combo.setFont(ui_font(13))
        self._fill_fonts(fonts.families())
        self.font_combo.currentIndexChanged.connect(
            lambda _i: self._set("font_family", self.font_combo.currentData() or "")
        )
        add = _button("Add Font…", "secondaryButton")
        add.setToolTip("Use a font file of your own (.ttf, .otf, .ttc)")
        add.clicked.connect(self._on_add_font)
        add.setEnabled(self._add_font is not None)
        row.addWidget(self.font_combo, 1)
        row.addWidget(add)
        layout.addLayout(row)
        self.add_font_button = add
        self.font_sample = QLabel("The quick brown fox jumps over the lazy dog.")
        self.font_sample.setWordWrap(True)
        layout.addWidget(self.font_sample)
        self._slider(layout, "font_weight", "Weight", 300, 700, 100, lambda v: WEIGHTS.get(round(v), str(round(v))))
        self._slider(layout, "reflow_font_size", "Size", 12, 40, 1, lambda v: f"{v:g} pt")
        self.changed.connect(lambda field: self._show_sample() if field.startswith("font") or field == "reflow_font_size" else None)
        self._show_sample()

    def _fill_fonts(self, names: list[str]) -> None:
        self.font_combo.blockSignals(True)
        self.font_combo.clear()
        self.font_combo.addItem(DEFAULT_FONT_LABEL, "")
        for name in names:
            self.font_combo.addItem(name, name)
        index = self.font_combo.findData(self._settings.font_family)
        self.font_combo.setCurrentIndex(max(0, index))
        self.font_combo.blockSignals(False)

    def _show_sample(self) -> None:
        s = self._settings
        self.font_sample.setFont(reading_font(min(28, s.reflow_font_size), QFont.Weight(s.font_weight), s.font_family))

    def _on_add_font(self) -> None:
        from . import fonts

        added = self._add_font() if self._add_font is not None else []
        if added:
            self._fill_fonts(sorted(set(fonts.families()) | set(added), key=str.casefold))
            self.font_combo.setCurrentIndex(max(0, self.font_combo.findData(added[0])))

    def _layout_tab(self, layout: QVBoxLayout) -> None:
        from .widgets import PAGE_VIEWS

        self._segments(layout, "page_view", "Page view", PAGE_VIEWS)
        self._slider(layout, "line_spacing", "Line spacing", 1.0, 2.6, 0.1, lambda v: f"{v:.1f}×")
        self._slider(layout, "paragraph_spacing", "Paragraph spacing", 0.0, 2.0, 0.1, lambda v: f"{v:.1f} em")
        self._slider(layout, "word_spacing", "Word spacing", 0, 12, 1, lambda v: f"+{v:g} px" if v else "normal")
        self._slider(layout, "letter_spacing", "Letter spacing", 0, 4, 0.5, lambda v: f"+{v:g} px" if v else "normal")
        self._switch(layout, "justify", "Justify paragraphs", "Even right edges, like a printed page.")
        centered = _SwitchRow("Center the text", self._settings.margin_left < 0 and self._settings.margin_right < 0,
                              hint="Turn off to set the left and right margins yourself.")
        layout.addWidget(centered)
        self.controls["centered"] = centered.switch
        sliders = []
        for field, label in (("margin_left", "Left margin"), ("margin_right", "Right margin")):
            slider = self._slider(layout, field, label, 0, 400, 4, lambda v: f"{v:g} px")
            sliders.append((field, slider))

        def center(on: bool):
            for field, slider in sliders:
                slider.setEnabled(not on)
                if on:
                    self._set(field, -1)
                else:
                    self._set(field, max(0, slider.value() * 4) or 64)

        centered.switch.toggled.connect(center)
        for field, slider in sliders:
            slider.setEnabled(not centered.switch.isChecked())
            if getattr(self._settings, field) < 0:
                slider.blockSignals(True)
                slider.setValue(16)
                slider.blockSignals(False)
        self._slider(layout, "max_column", "Widest text column", 400, 1600, 20, lambda v: f"{v:g} px")

    def _color_tab(self, layout: QVBoxLayout) -> None:
        from .widgets import ThemeCard

        grid = QGridLayout()
        grid.setSpacing(10)
        group = QButtonGroup(self)
        self.theme_cards: dict[str, ThemeCard] = {}
        for i, key in enumerate([*themes.THEMES, "custom"]):
            card = ThemeCard(key)
            card.setChecked(key == self._settings.theme)
            card.clicked.connect(lambda _=False, k=key: self._set("theme", k))
            group.addButton(card)
            grid.addWidget(card, i // 3, i % 3)
            self.theme_cards[key] = card
        layout.addLayout(grid)
        row = QHBoxLayout()
        caption = QLabel("Custom theme")
        caption.setFont(ui_font(13))
        row.addWidget(caption)
        row.addStretch(1)
        for field, label in (("custom_page", "Paper…"), ("custom_text", "Text…")):
            button = _button(label, "secondaryButton")
            button.clicked.connect(lambda _=False, f=field, t=label: self._choose_color(f, t))
            row.addWidget(button)
            self.controls[field] = button
        layout.addLayout(row)
        layout.addWidget(_hairline_frame())
        self._switch(layout, "auto_mode", "Auto Mode", "Follow macOS: one theme in Light appearance, another in Dark.")
        for field, label in (("light_theme", "In Light appearance"), ("dark_theme", "In Dark appearance")):
            row = QHBoxLayout()
            name = QLabel(label)
            name.setFont(ui_font(13))
            combo = QComboBox()
            for key in [*themes.THEMES, "custom"]:
                combo.addItem(themes.theme_for(key).name, key)
            combo.setCurrentIndex(max(0, combo.findData(getattr(self._settings, field))))
            combo.currentIndexChanged.connect(lambda _i, f=field, c=combo: self._set(f, c.currentData()))
            row.addWidget(name)
            row.addStretch(1)
            row.addWidget(combo)
            layout.addLayout(row)
            self.controls[field] = combo

    def _choose_color(self, field: str, title: str) -> None:
        color = self._pick_color(getattr(self._settings, field), title.rstrip("…"))
        if color:
            self._set(field, color)
            for card in self.theme_cards.values():
                card.update()

    def _reading_tab(self, layout: QVBoxLayout) -> None:
        from .widgets import HIGHLIGHT_UNITS

        self._switch(layout, "read_aloud", "Read books aloud",
                     "Off: LivroSound is an e-reader only. No audio is made or played.")
        self._switch(layout, "highlight", "Highlight while reading")
        self._segments(layout, "highlight_unit", "Highlighter", HIGHLIGHT_UNITS)
        layout.addWidget(_hint("Word by word follows the narrator's timing within each sentence (an estimate).", 11))
        self._switch(layout, "show_page_numbers", "Show page numbers")
        self._switch(layout, "show_read_percent", "Show how much you've read")
        self._switch(layout, "always_show_status_bar", "Always show the status bar",
                     "Otherwise the page and progress fade while you read.")

    def _general_tab(self, layout: QVBoxLayout) -> None:
        self._switch(layout, "open_last_book", "Open last book on start", "Skip the start page and continue reading.")
        self._switch(layout, "auto_import", "Auto import on file open",
                     "A book opened with LivroSound from Finder, or dropped on its window, joins your library at once.")

    def _shortcuts_tab(self, layout: QVBoxLayout) -> None:
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(10)
        for row, (keys, action) in enumerate(SHORTCUTS):
            key = QLabel(keys)
            key.setFont(ui_font(13, QFont.Weight.DemiBold))
            grid.addWidget(key, row, 0)
            grid.addWidget(QLabel(action), row, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)


def _hairline_frame() -> QFrame:
    line = QFrame()
    line.setObjectName("hairline")
    line.setFixedHeight(1)
    return line


def _pick_color(current: str, title: str) -> str | None:
    from PySide6.QtWidgets import QColorDialog

    color = QColorDialog.getColor(QColor(current), None, title)
    return color.name() if color.isValid() else None
