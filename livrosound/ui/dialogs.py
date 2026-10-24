"""The dialogs on the way from a file to listening, and back to a book later.

Upload: ImportProgressDialog while the book is read → LayoutDialog →
ChooseVoiceDialog → CustomizeAudioDialog. Resume: LibraryDialog.

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
- LibraryDialog removes a book through on_remove(book_id) when given (the main
  window closes the book if it's open and deletes its files once nothing uses
  them); otherwise it removes it from the library itself.
- ChooseVoiceDialog keeps one VoicePreviews per parent window, found again by
  object name on the next opening: a clip still being rendered when the dialog
  closes finishes on a worker thread that must outlive the dialog.
- LibraryDialog's "34% read" is the share of the book's spoken characters before
  the saved position; that needs the book's blocks (book.json), so the running
  sums are cached per book file.
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
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .. import paths, voice_preview, voices
from ..book import Book
from . import theme as themes
from .theme import ACCENT, ACCENT_HOVER, ON_ACCENT, reading_font, ui_font
from .widgets import GradientProgressBar, ToggleSwitch, VoicePicker, VoicePreviews, book_pixmap, confirm

if TYPE_CHECKING:
    from ..engines.base import TTSEngine
    from ..library import BookRecord, Library, Settings

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


# ---------- Your Books ----------

# Generic covers, one per book (by id), all dark enough for white type.
COVER_COLORS = ("#4F7942", "#1A4FA8", "#6B4E8A", "#9A4E2E", "#2F5357", "#7A5C2E", "#8A3B52")

# (book id, book.json mtime) -> spoken characters before each block (running sums)
_progress_cache: dict[tuple[str, int], list[int]] = {}


def read_fraction(record: BookRecord) -> float | None:
    """How much of the book lies before the saved position (by spoken characters),
    or None when the book's text isn't cached. The main window keeps it on the
    record as it saves the position; older records are worked out from book.json."""
    if record.read_fraction >= 0:
        return min(1.0, record.read_fraction)
    path = paths.book_dir(record.book_id) / "book.json"
    try:
        key = (record.book_id, path.stat().st_mtime_ns)
    except OSError:
        return None
    before = _progress_cache.get(key)
    if before is None:
        book = Book.load(path.parent)
        if book is None:
            return None
        before = [0]
        for block in book.blocks:
            before.append(before[-1] + (len(block.text) if block.spoken else 0))
        _progress_cache[key] = before
    if before[-1] == 0:
        return None
    return before[min(max(record.position_block, 0), len(before) - 1)] / before[-1]


def opened_text(iso: str, today: date) -> str:
    """'opened today', 'opened yesterday', 'opened Oct 22', 'opened Oct 22, 2025'."""
    try:
        day = datetime.fromisoformat(iso).date()
    except ValueError:
        return ""
    if day == today:
        return "opened today"
    if (today - day).days == 1:
        return "opened yesterday"
    text = f"{day:%b} {day.day}"
    return f"opened {text}" if day.year == today.year else f"opened {text}, {day.year}"


def book_status(record: BookRecord, today: date) -> str:
    """'34% read · audio 100% ready · opened Oct 22'."""
    parts = []
    fraction = read_fraction(record)
    if fraction is not None:
        percent = math.floor(fraction * 100)
        if percent > 0:
            parts.append(f"{percent}% read")
        else:  # under 1%: somewhere in the first pages, or not started at all
            parts.append("Just started" if fraction > 0 or record.position_ms > 0 else "Not started")
    ready = 100 if record.generated_fraction >= 1 else math.floor(record.generated_fraction * 100)
    parts.append(f"audio {ready}% ready" if ready > 0 else "no audio yet")
    opened = opened_text(record.last_opened, today)
    if opened:
        parts.append(opened)
    return " · ".join(parts)


def _cover_pixmap(record: BookRecord, width: int = 36, height: int = 48) -> QPixmap:
    """A plain cover: the title's initial in the reading serif, the format below."""
    pixmap = QPixmap(width * 2, height * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(COVER_COLORS[sum(map(ord, record.book_id)) % len(COVER_COLORS)])
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(0, 0, width, height), 4, 4)
    p.setBrush(color.darker(125))
    p.drawRoundedRect(QRectF(0, 0, 4, height), 2, 2)  # the spine
    p.setPen(QColor("#FFFFFF"))
    serif = reading_font(12, QFont.Weight.DemiBold)
    serif.setPixelSize(20)
    p.setFont(serif)
    initial = next((c for c in record.title if c.isalnum()), "")
    p.drawText(QRectF(3, 4, width - 3, 26), Qt.AlignmentFlag.AlignCenter, initial.upper())
    label = ui_font(8, QFont.Weight.DemiBold)
    label.setPixelSize(8)
    p.setFont(label)
    p.setPen(QColor(255, 255, 255, 210))
    p.drawText(QRectF(3, height - 15, width - 3, 11), Qt.AlignmentFlag.AlignCenter, record.source_kind.upper())
    p.end()
    return pixmap


class _BookRow(QWidget):
    TEXT_WIDTH = 430  # titles longer than this are elided

    def __init__(self, record: BookRecord, today: date):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(14)
        cover = QLabel()
        cover.setPixmap(_cover_pixmap(record))
        layout.addWidget(cover, 0, Qt.AlignmentFlag.AlignVCenter)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        title_font = ui_font(14, QFont.Weight.DemiBold)
        self.title = QLabel(_elide(record.title or Path(record.source_path).stem, title_font, self.TEXT_WIDTH))
        self.title.setFont(title_font)
        texts.addWidget(self.title)
        author_font = ui_font(12)
        self.author = QLabel(_elide(record.author, author_font, self.TEXT_WIDTH))
        self.author.setFont(author_font)
        self.author.setVisible(bool(record.author))
        texts.addWidget(self.author)
        self.status = QLabel(book_status(record, today))
        self.status.setObjectName("hint")
        self.status.setFont(ui_font(11))
        texts.addWidget(self.status)
        layout.addLayout(texts, 1)


def _elide(text: str, font: QFont, width: int) -> str:
    return QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, width)


class LibraryDialog(_Dialog):
    """"Your Books": every book opened, newest first. Double-click or Open to
    reopen one; right-click to remove it (its text and audio) from the library."""

    ROW_HEIGHT = 70

    def __init__(self, parent: QWidget | None, library: Library, on_remove: Callable[[str], None] | None = None):
        super().__init__(parent, "Your Books", "Pick up where you left off.")
        self.setMinimumSize(620, 540)
        self._library = library
        self._on_remove = on_remove

        self.frame = QFrame()
        self.frame.setObjectName("card")
        frame_layout = QVBoxLayout(self.frame)
        frame_layout.setContentsMargins(6, 6, 6, 6)
        self.list = QListWidget()
        self.list.setObjectName("popoverList")  # the stylesheet's rounded hover and selection rows
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setSpacing(1)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_menu)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        self.list.itemActivated.connect(lambda _item: self.accept())
        self.list.currentItemChanged.connect(lambda *_: self._update_open())
        frame_layout.addWidget(self.list)
        self.empty = _hint("No books yet.\nUpload a book to start listening.", 14)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        frame_layout.addWidget(self.empty, 1)
        self.body.addWidget(self.frame, 1)
        hint = _hint("Right-click a book to remove it.", 11)
        hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.body.addWidget(hint)
        self._remove_hint = hint

        self.cancel_button = _button("Cancel", "goBackButton")
        self.cancel_button.clicked.connect(self.reject)
        self.open_button = _button("Open", "generateAudioButton", default=True)
        self.open_button.setMinimumWidth(120)
        self.open_button.clicked.connect(self.accept)
        self._add_buttons(self.cancel_button, self.open_button)
        self.refresh()

    @classmethod
    def ask(
        cls, parent: QWidget | None, library: Library, on_remove: Callable[[str], None] | None = None
    ) -> str | None:
        """The book to open (None: Cancel). on_remove(book_id) does a removal instead of the dialog."""
        dialog = cls(parent, library, on_remove)
        return dialog.selected() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def refresh(self) -> None:
        """Rebuild the rows from the library, keeping the selection when it's still there."""
        keep = self.selected()
        self.list.clear()
        today = date.today()
        for record in self._library.recent():
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, record.book_id)
            item.setSizeHint(QSize(0, self.ROW_HEIGHT))
            self.list.addItem(item)
            self.list.setItemWidget(item, _BookRow(record, today))
            if record.book_id == keep:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        empty = self.list.count() == 0
        self.list.setVisible(not empty)
        self.empty.setVisible(empty)
        self._remove_hint.setVisible(not empty)
        self._update_open()

    def selected(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def remove(self, book_id: str) -> bool:
        """Ask, then forget the book and delete its text and audio (never the file itself)."""
        record = self._library.get(book_id)
        if record is None:
            return False
        if not confirm(
            self,
            f"Remove “{record.title}” from your library?",
            "Its text and audio are deleted from this Mac. Your file stays where it is.",
            yes="Remove",
            no="Cancel",
            destructive=True,
        ):
            return False
        if self._on_remove is not None:
            self._on_remove(book_id)
        else:
            self._library.remove(book_id)
            self._library.save()
        self.refresh()
        return True

    def context_menu(self, book_id: str) -> QMenu:
        """The right-click menu of a row."""
        t = themes.current()
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {t.panel}; color: {t.text}; border: 1px solid {t.line};"
            f" border-radius: 8px; padding: 4px; }}"
            f" QMenu::item {{ padding: 5px 16px; border-radius: 5px; }}"
            f" QMenu::item:selected {{ background: {ACCENT_HOVER}; color: {ON_ACCENT}; }}"
        )
        menu.setFont(ui_font(13))
        action = menu.addAction("Remove from Library…")
        action.triggered.connect(lambda: self.remove(book_id))
        return menu

    def _show_menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        self.list.setCurrentItem(item)
        self.context_menu(item.data(Qt.ItemDataRole.UserRole)).exec(self.list.viewport().mapToGlobal(pos))

    def _update_open(self):
        self.open_button.setEnabled(self.selected() is not None)
