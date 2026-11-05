"""The landing page: what LivroSound shows before a book is open.

Minimal on purpose, like the welcome windows of Apple's apps: the open-book icon,
the wordmark, one quiet line, and the only two things to do here — upload a new
book (fern, the app's "go" color) or go to the library (outlined), where every
book opened before waits at its cover. A footnote says what files work and that nothing
leaves the Mac.

The page paints its own background in the theme's panel color (white,
sand, slate or near-black: flat, like Apple's welcome windows); the buttons
and labels are styled by theme.stylesheet() through their object names.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from . import theme as themes
from .theme import ui_font
from .widgets import APP_ICON, BOOK_BOUNDS


class _Book(QWidget):
    """The app icon's book, cropped to its outline and drawn as vectors at the
    widget's size, so it stays crisp at any scale."""

    def __init__(self, width: int, parent=None):
        super().__init__(parent)
        self._renderer = QSvgRenderer(str(APP_ICON), self)
        self._renderer.setViewBox(BOOK_BOUNDS)
        self.setFixedSize(width, round(width * BOOK_BOUNDS.height() / BOOK_BOUNDS.width()))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._renderer.render(p, QRectF(self.rect()))


class LivroSoundLanding(QWidget):
    """The start page; the main window answers its two buttons."""

    upload_clicked = Signal()
    library_clicked = Signal()

    BOOK_WIDTH = 236
    BUTTON_HEIGHT = 44  # the stylesheet rounds the buttons with half of this
    BUTTON_PADDING = 30  # each side, as in the stylesheet

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("landingPage")

        self.icon = _Book(self.BOOK_WIDTH)

        wordmark = QLabel("LivroSound")
        wordmark.setObjectName("wordmark")
        font = ui_font(40, QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, -1.0)  # tight, like Apple's display type
        wordmark.setFont(font)

        tagline = QLabel("Your books, read aloud.")
        tagline.setObjectName("tagline")
        tagline.setFont(ui_font(17))

        self.upload_button = self._button("Upload New Book", "landingPrimary")
        self.upload_button.setToolTip("PDF, EPUB, Word or text file")
        self.upload_button.clicked.connect(lambda: self.upload_clicked.emit())
        self.library_button = self._button("Go to Library", "landingSecondary")
        self.library_button.setToolTip("Your books, and where you left off in each")
        self.library_button.clicked.connect(lambda: self.library_clicked.emit())
        # Both the width of the longer label, measured here rather than from sizeHint(),
        # which doesn't know the stylesheet's padding until the page is in the window.
        width = max(
            QFontMetrics(b.font()).horizontalAdvance(b.text()) + 2 * self.BUTTON_PADDING + 4
            for b in (self.upload_button, self.library_button)
        )
        for button in (self.upload_button, self.library_button):
            button.setFixedWidth(width)
        buttons = QHBoxLayout()
        buttons.setSpacing(14)
        buttons.addStretch(1)
        buttons.addWidget(self.upload_button)
        buttons.addWidget(self.library_button)
        buttons.addStretch(1)

        footnote = QLabel("PDF · EPUB · DOCX · TXT — everything stays on your Mac")
        footnote.setObjectName("landingFootnote")
        footnote.setFont(ui_font(12))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 26)
        layout.setSpacing(0)
        layout.addStretch(5)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(30)
        layout.addWidget(wordmark, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(6)
        layout.addWidget(tagline, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(40)
        layout.addLayout(buttons)
        layout.addStretch(7)
        layout.addWidget(footnote, 0, Qt.AlignmentFlag.AlignHCenter)


    def _button(self, text: str, object_name: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setFont(ui_font(15, QFont.Weight.DemiBold if object_name == "landingPrimary" else QFont.Weight.Medium))
        button.setFixedHeight(self.BUTTON_HEIGHT)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
        return button

    def paintEvent(self, event):
        QPainter(self).fillRect(self.rect(), QColor(themes.current().panel))
