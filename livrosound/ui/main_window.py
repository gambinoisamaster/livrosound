"""The main window: every user flow of LivroSound, from a file to listening.

It owns no reading, rendering or audio logic of its own; it wires the finished
modules together (docs/DESIGN.md, "User flows (binding)"), the way Script
Radio's main window does:

    landing  -> Upload New Book -> import thread (PDF with text, EPUB, DOCX, TXT)
             -> LayoutDialog -> ChooseVoiceDialog <-> CustomizeAudioDialog
             -> the reader -> "Generate audio from the beginning?"
    landing  -> Go to Library -> LibraryPage (covers; search, group, sort, tags,
                collections) -> the reader at the saved position ->
                "Continue generating audio?"

The reader is a stage holding one ReaderView (PageView for a PDF in its
original layout, ReflowView otherwise) with the player capsule and the speed
control floating over its bottom edge; the toolbar holds New Book, Contents,
Aa, Re-Generate Audio and the book's title; the status bar holds the blue
generation bar (Cancel Generation floats above it), then Export MP3 or the fern
Resume Generating in its place.

Rules that are easy to break:
- Audio is made by one GenerationWorker at a time, on its own thread. Starting
  from another block while one runs asks it to stop and starts the next run
  when its thread has finished (the UI thread never waits for it). Blocks the
  stopping run still finishes are kept and announced like any other.
- Play is gated by the 5% rule after a (re)start: it enables once the unbroken
  audio from where the reader stands holds 5% of the book (or reaches its end),
  with the ready ding only for audio made in this session at the user's
  request (never when a resumed book carries on generating).
- The player may wait for a block only when the running worker will make it
  within WAIT_AHEAD_CHARS characters; otherwise it stops and the user is asked.
- Everything the user does at a position goes through _go_to(block, char):
  clicks, Contents picks and lines of the book's own contents page (Block.link)
  alike. Questions are asked after the click or signal has been handled
  (QTimer.singleShot(0)), never inside the view's or the player's own handlers.
- A book's AudioStore is closed only after every thread using it has stopped:
  closing a book hands its worker over to _retiring, which closes the store
  once that thread is done.
- Quitting saves the position and cancels the worker; if it doesn't stop at
  once, the window hides and closes for good when it has.
"""

from __future__ import annotations

import bisect
import dataclasses
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtMultimedia import QMediaDevices, QSoundEffect
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from .. import bookinfo, export, paths, shelf
from ..audio_store import AudioStore, Cue, cue_at
from ..book import Book
from ..engines import load_engines
from ..engines.base import TTSEngine
from ..generation import GenerationCancelled, Generator, stream_ready
from ..ingest import SUPPORTED_SUFFIXES, BookImportError, ImportCancelled, book_id_for, load_book
from ..library import BookRecord, Library, Settings, audio_signature
from ..sentences import split_sentences, word_at
from ..timeline import Timeline
from ..validate import Transcriber
from . import dialogs, fonts
from . import theme as themes
from .landing import LivroSoundLanding
from .library_page import LibraryPage
from .page_view import MAX_ZOOM, MIN_ZOOM, PageView
from .player import BookPlayer, QtSinkOutput
from .reader_view import ReadingStyle
from .reflow_view import ReflowView
from .theme import Theme, theme_for, tooltip_stylesheet, ui_font
from .widgets import (
    ContentsPopover,
    GradientProgressBar,
    PlayerCapsule,
    ThemesPopover,
    VoicePreviews,
    aa_icon,
    confirm,
    inform,
    list_icon,
)

PLAYBACK_SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
REFLOW_SIZES = list(range(14, 38, 2))  # LivroSound layout text sizes the Aa buttons step through (pt)
ZOOM_STEP = 0.1  # PDF pages
POSITION_SAVE_INTERVAL_MS = 3000
RECORD_SAVE_INTERVAL_S = 5.0  # the library hears how far generation got this often
# Playback may wait at a block without audio only when the running generation
# will make it within this many characters (about half a minute of Pocket-TTS).
WAIT_AHEAD_CHARS = 2000
PICK_A_START = "Click any part of the text, or choose a chapter from Contents, to start generating audio from there."
IMPORT_PROGRESS_DELAY_MS = 300  # a cached book opens without flashing the progress dialog
READY_SOUND = paths.ASSETS_DIR / "sounds" / "ready.wav"
INFO_FADE_MS = 3000  # the page and progress fade this long after the reader last moved
PAGE_COUNT_DELAY_MS = 400  # counting a reflowed book's pages waits for resizing to settle
STYLE_FIELDS = ("font_family", "font_weight", "line_spacing", "paragraph_spacing", "word_spacing",
                "letter_spacing", "justify", "margin_left", "margin_right", "max_column")
BOOK_FILTER = "Books (*.pdf *.epub *.docx *.txt)"
PREVIEW_SIZE = QSize(488, 496)  # LayoutDialog thumbnails, rendered at twice the card's size

CREDITS_HTML = """
<h3>LivroSound</h3>
<p>Your books, read aloud — entirely on this Mac. For personal, non-commercial use.
Built on the shoulders of:</p>
<ul>
<li><b>Pocket-TTS</b> — Kyutai Labs' pocket-sized CPU text-to-speech. MIT license.
    <a href='https://github.com/kyutai-labs/pocket-tts'>github.com/kyutai-labs/pocket-tts</a></li>
<li><b>PyMuPDF</b> (AGPL-3.0) — PDF text and pages</li>
<li><b>Whisper</b> via mlx-whisper (MIT) — the optional audio check</li>
<li>ffmpeg + LAME — speed changes and MP3 export</li>
<li>PySide6 / Qt (LGPL) — this interface</li>
</ul>
<p>The narrators are voices cloned from the user's own recordings.</p>
"""

ABOUT_HTML = """
<h3>LivroSound</h3>
<p>Turns a PDF, EPUB, Word or text file into narrated audio,
and reads along with you: the sentence being read is highlighted and kept in view.</p>
<p>Everything happens on this Mac: nothing is uploaded anywhere.</p>
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _spoken_before(book: Book) -> list[int]:
    """Spoken characters before each block (and in all, last): the library's "34% read"."""
    before = [0]
    for block in book.blocks:
        before.append(before[-1] + (len(block.text) if block.spoken else 0))
    return before


def _gather_info(book: Book) -> bookinfo.BookInfo:
    """(Off the UI thread) a new book's library info, its cover saved as cover.jpg."""
    words = sum(len(b.text.split()) for b in book.blocks)
    info = bookinfo.read_info(book.source_path, book.source_kind, words=words)
    if info.cover:
        bookinfo.save_cover(book.book_id, info.cover)
    return info


def _apply_info(record: BookRecord, info: bookinfo.BookInfo) -> None:
    record.subjects, record.series, record.series_index = list(info.subjects), info.series, info.series_index
    record.published, record.pages = info.published, info.pages


def _opening_block(book: Book) -> int:
    """Where the book itself starts: the heading just before its first real paragraph
    (or that paragraph), past the front matter."""
    heading = -1
    for i, block in enumerate(book.blocks):
        if block.skipped or not block.text.strip():
            continue
        if block.kind == "heading":
            heading = i
        elif block.kind == "paragraph" and len(block.text) >= 60:
            return heading if 0 <= heading and i - heading <= 4 else i
    return 0


def _system_is_dark() -> bool:
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def _file_name(title: str) -> str:
    """A title as a file name (no slashes or colons)."""
    name = " ".join(title.replace("/", "-").replace(":", " -").split()).strip(" .")
    return name or "Book"


class SpeedCombo(QComboBox):
    """The playback speed beside the player, with a small chevron so it reads as a menu
    (the stylesheet's rounded pill hides the platform's arrow)."""

    def paintEvent(self, event):
        super().paintEvent(event)
        color = QColor(themes.current().on_chrome)
        color.setAlphaF(0.75)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        x, y = self.width() - 17, self.height() / 2
        p.drawPolyline(QPolygonF([QPointF(x - 3.5, y - 1.5), QPointF(x, y + 2), QPointF(x + 3.5, y - 1.5)]))
        p.end()


# ---------- threads ----------


class GenerationWorker(QThread):
    """One run of Generator.run from a start block, on its own thread."""

    block_done = Signal(int, int)  # block, duration_ms
    status = Signal(str)
    finished_ok = Signal(list)  # Whisper issues
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, generator: Generator, start_block: int):
        super().__init__()
        self.generator = generator
        self.start_block = start_block
        self._cancel = threading.Event()
        order = generator.order()
        made = generator.store.generated()
        # What this run makes before wrapping around to the book's beginning: the
        # only blocks playback may wait for.
        self.ahead = [b for b in order[bisect.bisect_left(order, start_block):] if b not in made]
        self._ahead_index = {block: i for i, block in enumerate(self.ahead)}
        self._next = 0  # the first entry of `ahead` that may still be missing

    def cancel(self) -> None:
        self._cancel.set()

    def is_cancelling(self) -> bool:
        return self._cancel.is_set()

    def reaches_soon(self, block: int, weights: dict[int, int], limit: int) -> bool:
        """(UI thread; polled while playback waits) Will this run make block before it
        has made `limit` more characters?"""
        i = self._ahead_index.get(block)
        if i is None or self._cancel.is_set():
            return False
        has = self.generator.store.has
        while self._next < len(self.ahead) and has(self.ahead[self._next]):
            self._next += 1
        todo = 0
        for b in self.ahead[self._next : i]:
            if not has(b):
                todo += weights.get(b, 1)
                if todo > limit:
                    return False
        return True

    def run(self):
        try:
            issues = self.generator.run(
                self.start_block,
                on_block=self.block_done.emit,
                on_status=self.status.emit,
                cancelled=self._cancel.is_set,
            )
        except GenerationCancelled:
            self.cancelled.emit()
        except Exception as e:  # shown to the user, never lost in a thread
            self.failed.emit(str(e) or type(e).__name__)
        else:
            self.finished_ok.emit(issues)


class Task(QThread):
    """A job off the UI thread: job(progress, cancelled) -> result. Used to look a file
    over, import or load a book, read the library's book info, and export the MP3."""

    progressed = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, job: Callable[[Callable[[int, int, str], None], Callable[[], bool]], object]):
        super().__init__()
        self._job = job
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self):
        try:
            result = self._job(self.progressed.emit, self._cancel.is_set)
        except (ImportCancelled, export.ExportCancelled):
            self.cancelled.emit()
        except BookImportError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(str(e) or type(e).__name__)
        else:
            self.done.emit(result)


# ---------- the window ----------


class MainWindow(QMainWindow):
    def __init__(self, engine: TTSEngine | None = None, output_factory=None):
        super().__init__()
        self.setWindowTitle("LivroSound")
        self.resize(1180, 820)
        self.setMinimumSize(760, 560)

        self.engine = engine or load_engines()[0]  # Pocket-TTS (tests pass a fake)
        self.settings = Settings.load()
        fonts.load_user_fonts()  # the fonts the user added, so their choice is there to read in
        self.library = Library.load()
        self.transcriber = Transcriber()
        self.theme: Theme = theme_for(self.settings.theme)

        # The open book and its audio.
        self.book: Book | None = None
        self.record: BookRecord | None = None
        self.store: AudioStore | None = None
        self.generator: Generator | None = None
        self.timeline: Timeline | None = None
        self.order: list[int] = []
        self.weights: dict[int, int] = {}
        self._before: list[int] = [0]  # spoken characters before each block
        self._cues: dict[int, list[Cue]] = {}  # block -> its cues, read once from the store

        # Generation.
        self._worker: GenerationWorker | None = None
        self._pending_start: int | None = None  # the block to start from once the running worker stops
        self._retiring: list[GenerationWorker] = []  # workers of a closed book, still stopping
        self._released: list[tuple[str, Callable[[], None]]] = []  # (book id, then) once its workers stop
        self._pending_open: str | None = None  # a book waiting for its old worker to stop before it opens
        self._gate: tuple[int, bool] | None = None  # (block, ding): Play waits for 5% of the book from block
        self._pending_char: tuple[int, int] | None = None  # (block, char) to seek to once that block is made
        self._pick_mode = False  # "No" to "Generate audio from the beginning?": the next click picks the start
        self._record_saved_at = 0.0

        # Playback and following.
        self._auto_follow = True
        self._follow_block = -1  # the block auto-follow last scrolled to
        self._highlight: tuple[int, int, int] | None = None  # (block, start, end) shown now
        self._position_block = -1
        self._info_block = -1  # the block the page/progress line was last worked out for
        self._saved_position: tuple[int, int] | None = None
        self._at_end = False

        # Other threads and popups.
        # Every thread started, kept referenced until its finished signal is handled (a
        # QThread destroyed while it runs aborts the app); Quit waits for all of them.
        self._threads: set[QThread] = set()
        self._task: Task | None = None  # looking a file over, importing or loading a book
        self._export: Task | None = None
        self._export_store: AudioStore | None = None  # the store the running export reads
        self._export_book = ""  # and its book
        self._closing = False
        self._went_away = False  # Quit hid the window while threads finished
        self._close_timer = QTimer(self)  # while hidden: close for good once every thread has stopped
        self._close_timer.setInterval(100)
        self._close_timer.timeout.connect(self.close)
        self._contents_popover: ContentsPopover | None = None
        self._themes_popover: ThemesPopover | None = None
        self._ready_sound: QSoundEffect | None = None
        self._views: dict[QWidget, tuple[Book, str]] = {}  # view -> (book, mode) it shows
        self._told_missing = ""  # the book whose PDF couldn't be found, already said
        self.reflow_view: ReflowView | None = None
        self.page_view: PageView | None = None
        self.view: ReflowView | PageView | None = None

        self.player = BookPlayer(self, output_factory=output_factory or QtSinkOutput)
        self.player.set_waiting_allowed(self._waiting_allowed)
        self.player.position_changed.connect(self._on_position)
        self.player.playing_changed.connect(self._on_playing_changed)
        self.player.needs_audio.connect(self._on_needs_audio)
        self.player.finished.connect(self._on_finished)
        self.player.waiting_changed.connect(self._on_waiting)
        self.player.error.connect(self._on_player_error)
        self._position_timer = QTimer(self)
        self._position_timer.setInterval(POSITION_SAVE_INTERVAL_MS)
        self._position_timer.timeout.connect(self._save_position)

        self._build_menus()
        self._build_toolbar()
        self._build_reader()
        self._build_status_bar()
        self.landing = LivroSoundLanding()
        self.landing.upload_clicked.connect(self.open_book_dialog)
        self.landing.library_clicked.connect(self.show_library)
        self._info_task: Task | None = None  # reading the library's covers and book info
        self._build_library_page()
        self.pages = QStackedWidget()
        self.pages.addWidget(self.landing)
        self.pages.addWidget(self.library_page)
        self.pages.addWidget(self.reader_page)
        self.setCentralWidget(self.pages)
        self.setAcceptDrops(True)  # a book dropped on the window opens (Auto Import on File Open)
        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda _scheme: self._follow_appearance())
        themes.set_custom_colors(self.settings.custom_page, self.settings.custom_text)
        self._restyle(self._effective_theme())
        self._show_landing()
        if output_factory is None:
            # The first sound output and sound effect take a moment to open: do it now.
            QTimer.singleShot(0, self, QMediaDevices.defaultAudioOutput)
            QTimer.singleShot(0, self, self._ready_sound_effect)
        if self.settings.open_last_book and self.library.recent():
            QTimer.singleShot(0, self, self._open_last_book)

    # ---------- building ----------

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("File")
        self.upload_action = QAction("Upload New Book…", self)
        self.upload_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Open))
        self.upload_action.triggered.connect(self.open_book_dialog)
        self.library_action = QAction("Go to Library", self)
        self.library_action.setShortcut(QKeySequence("Ctrl+Shift+L"))
        self.library_action.triggered.connect(self.show_library)
        file_menu.addAction(self.upload_action)
        file_menu.addAction(self.library_action)
        self.settings_action = QAction("Settings…", self)
        self.settings_action.setShortcut(QKeySequence("Ctrl+,"))
        self.settings_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        self.settings_action.triggered.connect(self.open_settings)
        file_menu.addAction(self.settings_action)
        view_menu = self.menuBar().addMenu("View")
        for text, keys, step in (("Larger Text", "Ctrl++", 1), ("Smaller Text", "Ctrl+-", -1)):
            action = QAction(text, self)
            action.setShortcuts([QKeySequence(keys)] + ([QKeySequence("Ctrl+=")] if step > 0 else []))
            action.triggered.connect(lambda _=False, s=step: self._step_text_size(s) if self._reading() else None)
            view_menu.addAction(action)
        self.view_menu = view_menu
        # About in the app menu (macOS moves it there), and Help ▸ Credits.
        help_menu = self.menuBar().addMenu("Help")
        about = QAction("About LivroSound", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(lambda: QMessageBox.about(self, "About LivroSound", ABOUT_HTML))
        credits = QAction("Credits", self)
        credits.setMenuRole(QAction.MenuRole.NoRole)
        credits.triggered.connect(lambda: QMessageBox.about(self, "Credits", CREDITS_HTML))
        help_menu.addAction(about)
        help_menu.addAction(credits)

    def _build_toolbar(self):
        tb = self.addToolBar("Main")
        self.toolbar = tb
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        def action(text, slot):
            a = QAction(text, self)
            a.triggered.connect(slot)
            tb.addAction(a)
            button = tb.widgetForAction(a)
            button.installEventFilter(self)  # its tooltip would only repeat the label
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            return a

        def pill(text, tooltip, slot, icon_size):
            button = QToolButton()
            button.setObjectName("pillButton")
            button.setText(text)
            button.setToolTip(tooltip)
            button.setIconSize(icon_size)
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon if text else Qt.ToolButtonStyle.ToolButtonIconOnly
            )
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            tb.addWidget(button)
            return button

        self.library_toolbar_action = action("Library", self.show_library)
        self.new_action = action("New Book", self.open_book_dialog)
        # Like Apple Books: a list button for the chapters, "Aa" for themes and text.
        self.contents_button = pill("Contents", "The book's chapters", self._open_contents, QSize(20, 16))
        self.themes_button = pill("", "Themes, text size, layout and highlight", self._open_themes, QSize(36, 24))
        self.regenerate_action = action("Re-Generate Audio", self.regenerate)

        # The title, centered on the window rather than in the room the buttons leave (_place_title).
        self.title_label = QLabel(tb)
        self.title_label.setObjectName("bookTitle")
        self.title_label.setFont(themes.reading_font(17, QFont.Weight.DemiBold))
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._title_text = ""
        tb.installEventFilter(self)

    def _build_reader(self):
        # The page fills the window between the toolbar and the status bar; the
        # player floats over its bottom edge.
        self.reader_page = QWidget()
        self.reader_page.setObjectName("transportWidget")
        outer = QVBoxLayout(self.reader_page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.stage = QWidget()
        self.stage.setObjectName("readerStage")
        self.stage_layout = QVBoxLayout(self.stage)
        self.stage_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.stage, 1)

        self.capsule = PlayerCapsule(self.stage)
        self.capsule.play_clicked.connect(self.toggle_play)
        self.capsule.skip_clicked.connect(self.skip)
        self.capsule.paragraph_clicked.connect(self.step_paragraph)
        for button in self.capsule.buttons():
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Space and the arrows belong to the player

        self.speed_combo = SpeedCombo(self.stage)
        self.speed_combo.setObjectName("speedCombo")
        for rate in PLAYBACK_SPEEDS:
            self.speed_combo.addItem(f"{rate:g}×")
        rate = min(PLAYBACK_SPEEDS, key=lambda r: abs(r - self.settings.playback_rate))
        self.speed_combo.setCurrentIndex(PLAYBACK_SPEEDS.index(rate))
        self.player.set_rate(rate)
        self.speed_combo.setFixedWidth(74)
        self.speed_combo.setToolTip("Playback speed")
        self.speed_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.speed_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speed_combo.currentIndexChanged.connect(self._on_speed_changed)

        # Cancel floats just above the progress bar while audio is being made.
        self.cancel_button = QPushButton("Cancel Generation", self.stage)
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setFixedHeight(30)  # the stylesheet rounds it with half of this
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cancel_button.clicked.connect(self.cancel_generation)
        self.cancel_button.hide()
        self.stage.installEventFilter(self)

        # Bottom left of the page: "Page 12 of 340 · 34%", fading while reading
        # unless Always Show Status Bar is on.
        self.reading_info = QLabel(self.stage)
        self.reading_info.setObjectName("readingInfo")
        self.reading_info.setFont(ui_font(11, QFont.Weight.Medium))
        self.reading_info.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.reading_info.hide()
        self._info_timer = QTimer(self)
        self._info_timer.setSingleShot(True)
        self._info_timer.setInterval(INFO_FADE_MS)
        self._info_timer.timeout.connect(self._fade_reading_info)
        self._page_count_timer = QTimer(self)
        self._page_count_timer.setSingleShot(True)
        self._page_count_timer.setInterval(PAGE_COUNT_DELAY_MS)
        self._page_count_timer.timeout.connect(self._count_pages)

    def _build_status_bar(self):
        # Bottom right: the generation bar; Export MP3 in its place at 100%, or the
        # fern Resume Generating once generation was stopped.
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.progress = GradientProgressBar()
        self.progress.setFixedSize(240, 22)
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.export_button = QPushButton("Export MP3")
        self.export_button.setObjectName("exportButton")
        self.export_button.setToolTip("Save the whole book as one MP3, with chapters")
        self.resume_button = QPushButton("Resume Generating")
        self.resume_button.setObjectName("resumeButton")
        self.resume_button.setToolTip("Make the rest of the audio, from where you are")
        for button, slot in ((self.export_button, self.export), (self.resume_button, self.resume_generation)):
            button.setFixedHeight(22)  # the stylesheet rounds them with half of this
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(slot)
            button.hide()
            sb.addPermanentWidget(button)
        sb.addPermanentWidget(self.progress)

    # ---------- layout of the floating pieces ----------

    def _place_overlays(self):
        """The player centered over the bottom of the page with the speed control beside
        it, and Cancel Generation just above the progress bar."""
        stage, capsule = self.stage, self.capsule
        capsule.adjustSize()
        x = (stage.width() - capsule.width()) // 2
        y = stage.height() - capsule.height() - 20
        capsule.move(x, y)
        capsule.raise_()
        speed = self.speed_combo
        speed.move(x + capsule.width() + 14, y + (capsule.height() - speed.height()) // 2)
        speed.raise_()
        if not self.cancel_button.isHidden():
            self.cancel_button.adjustSize()
            if self.progress.isVisible():
                bar = stage.mapFromGlobal(self.progress.mapToGlobal(QPoint(0, 0)))
                center = bar.x() + self.progress.width() // 2
            else:
                center = stage.width() - 140
            cx = min(stage.width() - self.cancel_button.width() - 12, center - self.cancel_button.width() // 2)
            self.cancel_button.move(cx, stage.height() - self.cancel_button.height() - 8)
            self.cancel_button.raise_()
        info = self.reading_info
        info.adjustSize()
        info.move(16, stage.height() - info.height() - 14)
        info.raise_()

    def _place_title(self):
        """Center the title on the toolbar; a title too long for that slides right
        (as far as the window's edge) before it's shortened with "…", and never runs
        into the buttons on the left."""
        tb, label = self.toolbar, self.title_label
        last = tb.widgetForAction(self.regenerate_action)
        left_edge = (last.geometry().right() if last is not None else 0) + 24
        right_edge = tb.width() - 20
        room = max(0, right_edge - left_edge)
        label.setText(QFontMetrics(label.font()).elidedText(self._title_text, Qt.TextElideMode.ElideRight, room))
        label.adjustSize()
        x = min(max(left_edge, int(tb.width() / 2 - label.width() / 2)), right_edge - label.width())
        label.move(max(left_edge, x), int((tb.height() - label.height()) / 2))
        label.raise_()

    def _set_title(self, title: str):
        self._title_text = title
        self.setWindowTitle(f"{title} — LivroSound" if title else "LivroSound")
        self._place_title()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.ToolTip and isinstance(watched, QToolButton):
            if watched.toolTip().replace("&", "").strip() == watched.text().replace("&", "").strip():
                return True  # a tip that only repeats the button's label is noise
        if event.type() == QEvent.Type.Resize:
            if watched is self.stage:
                self._place_overlays()
            elif watched is self.toolbar:
                QTimer.singleShot(0, self, self._place_title)
        return super().eventFilter(watched, event)

    # ---------- pages ----------

    def _show_landing(self):
        self.pages.setCurrentWidget(self.landing)
        self.toolbar.hide()
        self.statusBar().hide()
        self._set_title("")

    def _show_reader(self):
        self.pages.setCurrentWidget(self.reader_page)
        self.toolbar.show()
        self.statusBar().show()
        QTimer.singleShot(0, self, self._place_title)
        QTimer.singleShot(0, self, self._place_overlays)

    def _reflow(self) -> ReflowView:
        if self.reflow_view is None:
            self.reflow_view = self._connect_view(ReflowView())
        return self.reflow_view

    def _pages(self) -> PageView:
        if self.page_view is None:
            self.page_view = self._connect_view(PageView())
        return self.page_view

    def _connect_view(self, view):
        view.clicked.connect(self._on_view_clicked)
        view.user_scrolled.connect(self._suspend_auto_follow)
        view.verticalScrollBar().valueChanged.connect(lambda _v, v=view: self._on_view_moved(v))
        view.hide()
        return view

    def _layout_mode(self) -> str:
        layout = self.record.layout if self.record is not None else ""
        return layout if layout in ("original", "livrosound") else "livrosound"

    def _setup_view(self):
        """Show the book in its layout: a PDF's own pages in PageView, everything else in ReflowView.
        A PDF whose file is gone can't show its pages: it opens in the LivroSound layout, and
        the user is told why (once per book)."""
        book, mode = self.book, self._layout_mode()
        if book.source_kind == "pdf" and mode == "original" and not Path(book.source_path).is_file():
            mode = "livrosound"
            if self._told_missing != book.book_id:
                self._told_missing = book.book_id
                QTimer.singleShot(0, self, lambda: inform(
                    self,
                    "The PDF isn't where it was.",
                    f"LivroSound can't find {book.source_path}, so its pages can't be shown: the book "
                    "opens in the LivroSound layout. Upload the PDF from its new place to see its pages again.",
                ))
        if book.source_kind == "pdf" and mode == "original":
            view = self._pages()
            view.set_scale(self.settings.page_zoom)
        else:
            view = self._reflow()
            view.set_scale(self.settings.reflow_font_size)
        view.set_theme(self.theme)
        view.set_style(self._reading_style())
        view.set_page_mode(self.settings.page_view)
        shown = self._views.get(view)
        if shown is None or shown[0] is not book or shown[1] != mode:
            view.set_book(book, mode)
            self._views[view] = (book, mode)
        if view is not self.view:
            old, self.view = self.view, view
            if old is not None:
                self.stage_layout.removeWidget(old)
                old.hide()
                old.clear_highlight()
                if old is self.page_view:
                    self.page_view.shutdown()  # its render thread; set_book starts a new one
                    self._views.pop(old, None)
            self.stage_layout.addWidget(view)
            view.show()
            self._place_overlays()
        self._highlight = None
        self._follow_block = -1
        view.setFocus()
        self._update_reading_info()
        self._page_count_timer.start()

    # ---------- theme ----------

    def _apply_theme(self, key: str):
        """The user chose a reading theme. With Auto Mode it becomes the theme for the
        appearance macOS has now (light or dark)."""
        t = theme_for(key)
        self.settings.theme = t.key
        if self.settings.auto_mode:
            if _system_is_dark():
                self.settings.dark_theme = t.key
            else:
                self.settings.light_theme = t.key
        self.settings.save()
        self._restyle(t.key)

    def _effective_theme(self) -> str:
        """The theme to show: with Auto Mode, the light or dark one for macOS's appearance."""
        s = self.settings
        if s.auto_mode:
            return s.dark_theme if _system_is_dark() else s.light_theme
        return s.theme

    def _follow_appearance(self):
        """macOS switched Light/Dark (or Auto Mode was turned on or off)."""
        key = self._effective_theme()
        if key != self.theme.key:
            self._restyle(key)

    def _restyle(self, key: str):
        """Restyle everything for a reading theme (see theme.py)."""
        t = theme_for(key)
        self.theme = t
        themes.set_current(t)  # before the stylesheet: painted widgets read it
        self.setStyleSheet(themes.stylesheet(t))
        palette = QPalette()
        for role, color in (
            (QPalette.ColorRole.Window, t.window), (QPalette.ColorRole.WindowText, t.text),
            (QPalette.ColorRole.Base, t.raised), (QPalette.ColorRole.AlternateBase, t.raised_hover),
            (QPalette.ColorRole.Text, t.text), (QPalette.ColorRole.Button, t.raised),
            (QPalette.ColorRole.ButtonText, t.text), (QPalette.ColorRole.Highlight, t.highlight),
            (QPalette.ColorRole.HighlightedText, t.text), (QPalette.ColorRole.ToolTipBase, t.panel),
            (QPalette.ColorRole.ToolTipText, t.text), (QPalette.ColorRole.PlaceholderText, t.muted),
        ):
            palette.setColor(role, QColor(color))
        QApplication.setPalette(palette)
        # Tooltips are windows of their own, so their style goes on the application.
        QApplication.instance().setStyleSheet(tooltip_stylesheet(t))
        for view in (self.reflow_view, self.page_view):
            if view is not None:
                view.set_theme(t)
        self.contents_button.setIcon(list_icon(t.on_chrome))
        self.themes_button.setIcon(aa_icon(t.on_chrome))
        self.capsule.refresh_icons()
        self.capsule.update()
        self.landing.update()
        self.library_page.update()
        self.library_page.shelf.viewport().update()
        themes_popover = self._popover("_themes_popover")
        if themes_popover is not None:
            themes_popover.set_theme(t.key)
        if self.view is not None and self._highlight is not None:
            self.view.show_highlight(*self._highlight)

    # ---------- opening a book ----------

    def _busy(self) -> bool:
        """Is a book being opened (or waiting to)? Then nothing else opens. (An export
        carries on through a book switch: its book's store stays open until it ends.)"""
        if self._task is not None or self._pending_open is not None:
            self.statusBar().showMessage("Still opening a book…", 3000)
            return True
        return False

    def open_book_dialog(self):
        """Upload New Book: choose a file, then import it (flow 2)."""
        if self._busy():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Upload a Book", "", BOOK_FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str):
        """Import a book file (or an unzipped EPUB folder). Its fingerprint comes first,
        off the UI thread: a book opened before can pick up where it was left."""
        if self._busy():
            return
        path = str(Path(path).expanduser())
        suffix = Path(path).suffix.lower()

        def look(progress, cancelled):
            if suffix not in SUPPORTED_SUFFIXES:
                raise BookImportError(
                    f"LivroSound can't open {suffix or 'these'} files. Choose a PDF, EPUB, Word (.docx) or text file."
                )
            try:
                book_id = book_id_for(path)  # a file, or an unzipped EPUB folder
            except OSError as e:
                raise BookImportError(
                    "The file couldn't be read. Check that it's downloaded and that you can open it."
                ) from e
            return book_id

        self.statusBar().showMessage(f"Opening “{Path(path).name}”…")
        self._start_task(Task(look), lambda book_id: self._on_looked_over(path, book_id), self._import_failed)

    def _on_looked_over(self, path: str, book_id: str):
        self.statusBar().clearMessage()
        record = self.library.get(book_id)
        if record is not None and confirm(self, "You've opened this book before.", "Pick up where you left off?"):
            if record.source_path != path:
                record.source_path = path  # the same file, opened from another folder
            self._resume(record)
            return
        if self._closing or self._busy():  # something else started opening meanwhile
            return
        reopen = None
        if self.book is not None and self.book.book_id == book_id:
            # Importing again may replace this book's text and audio: close it first,
            # and reopen it if the import doesn't go through.
            reopen = self.record
            self._close_book()
        self._when_released(book_id, lambda: self._import(path, reopen))

    def _import(self, path: str, reopen: BookRecord | None):
        def job(report, cancelled):
            book = load_book(path, progress=report, cancelled=cancelled)
            return book, _gather_info(book)

        task = Task(job)
        close_progress = self._progress_for(task, f"Reading “{Path(path).stem}”")

        def done(result: tuple[Book, bookinfo.BookInfo]):
            close_progress()
            self._when_no_dialog(lambda: self._on_imported(*result))

        def failed(message: str):
            close_progress()
            self._import_failed(message)
            if reopen is not None:
                self._resume(reopen, ask=False)

        def cancelled():
            close_progress()
            self.statusBar().showMessage("Import cancelled.", 4000)
            if reopen is not None:
                self._resume(reopen, ask=False)

        self._start_task(task, done, failed, cancelled)

    def _progress_for(self, task: Task, title: str) -> Callable[[], None]:
        """ImportProgressDialog for a task, shown only if it takes a moment (a cached book
        opens at once). Returns what closes it; the task's end closes it too."""
        progress = dialogs.ImportProgressDialog(self, title)
        task.progressed.connect(progress.set_progress)
        progress.canceled.connect(task.cancel)
        timer = QTimer(progress)
        timer.setSingleShot(True)
        timer.timeout.connect(progress.open)
        timer.start(IMPORT_PROGRESS_DELAY_MS)
        closed = []

        def close():
            if not closed:
                closed.append(True)
                timer.stop()
                progress.finish()
                progress.deleteLater()

        task.finished.connect(close)
        return close

    def _import_failed(self, message: str):
        self.statusBar().clearMessage()
        # A PDF of page images (a scan) has no text to read: ingest says so in plain words.
        scanned = "scan" in message.lower()
        inform(self, "Scanned books aren't supported yet." if scanned else "This book couldn't be opened.", message)

    def _start_task(self, task: Task, done, failed=None, cancelled=None):
        """Run a Task; its outcome is ignored once Quit has begun (no dialog may pop up then)."""
        self._task = task
        self._threads.add(task)

        def outcome(handler, *args):
            if self._task is task:
                self._task = None  # done: what follows may open another book
            if handler is not None and not self._closing:
                handler(*args)

        task.done.connect(lambda result: outcome(done, result))
        task.failed.connect(lambda message: outcome(failed, message))
        task.cancelled.connect(lambda: outcome(cancelled))
        task.finished.connect(lambda: self._on_task_finished(task))
        task.start()

    def _when_no_dialog(self, then: Callable[[], None]):
        """Run then() once no modal dialog is up. A dialog's event loop runs queued work (a
        finished import, a stopped worker's next step); switching the open book there would
        pull it from under the code that opened the dialog."""
        if self._closing:
            return
        if QApplication.activeModalWidget() is not None:
            QTimer.singleShot(100, self, lambda: self._when_no_dialog(then))
        else:
            then()

    def _on_task_finished(self, task: Task):
        self._threads.discard(task)
        task.deleteLater()
        if task is self._task:
            self._task = None

    def _on_imported(self, book: Book, info: bookinfo.BookInfo | None = None):
        """Flows 3–5 for a freshly imported book."""
        self._close_book()
        record = self.library.get(book.book_id) or BookRecord(
            book.book_id, book.title, book.author, book.source_path, book.source_kind,
            layout="original" if book.source_kind == "pdf" else "livrosound", date_added=_now(),
        )
        if info is not None:
            _apply_info(record, info)
            record.info_version = bookinfo.INFO_VERSION
            self.library_page.cover_changed(book.book_id)
        record.title, record.author = book.title, book.author
        record.source_path, record.source_kind = book.source_path, book.source_kind
        layout = dialogs.LayoutDialog.ask(self, book, *self._layout_previews(book))
        if layout is not None:
            record.layout = layout
        generate = layout is not None and self._choose_voice()
        if self._closing:
            return
        self._close_book()  # (nothing is open, unless something slipped in under the dialogs)
        self._open_book(book, record)
        if not generate:
            self.statusBar().showMessage("Click any part of the text to choose a voice and start making audio.")
            return
        if self._use_chosen_voice(replace=False):
            self._ask_where_to_start()

    # ---------- the library ----------

    def _build_library_page(self):
        page = LibraryPage(self.library, self.settings)
        page.open_requested.connect(self._open_from_library)
        page.upload_requested.connect(self.open_book_dialog)
        page.home_requested.connect(self._show_landing)
        page.back_to_book_requested.connect(self._show_reader)
        page.remove_requested.connect(self._ask_to_remove)
        page.details_requested.connect(self._show_details)
        page.reveal_requested.connect(self._reveal)
        page.about_requested.connect(lambda: QMessageBox.about(self, "About LivroSound", ABOUT_HTML))
        page.option_toggled.connect(self._on_app_option)
        page.changed.connect(self._save_library_and_settings)
        self.library_page = page

    def show_library(self):
        """Go to Library (flow 9): every book as a cover, the open book still open (unless
        it was removed from the library meanwhile: then it closes and stays gone)."""
        if self.book is not None and self.library.get(self.book.book_id) is None:
            self._close_book(save=False)
        self.library_page.set_book_open(self.book.title if self.book is not None else "")
        self.library_page.refresh()
        self.pages.setCurrentWidget(self.library_page)
        self.toolbar.hide()
        self.statusBar().hide()
        self._set_title("")
        self._refresh_library_info()

    def _save_library_and_settings(self):
        self.settings.save()
        self.library.save()

    def _open_from_library(self, book_id: str):
        record = self.library.get(book_id)
        if record is not None and not self._busy():
            self._resume(record)

    def _open_last_book(self):
        """Open Last Book on Start."""
        recent = self.library.recent()
        if recent and self.book is None and not self._busy():
            self._resume(recent[0])

    def _ask_to_remove(self, book_ids: list[str]):
        """Remove from Library: asks, then closes the book if it's open and deletes its
        text and audio (never the user's file)."""
        records = [r for r in (self.library.get(b) for b in book_ids) if r is not None]
        if not records:
            return
        if len(records) == 1:
            title = f"Remove “{records[0].title}” from your library?"
            text = "Its text and audio are deleted from this Mac. Your file stays where it is."
        else:
            title = f"Remove {len(records)} books from your library?"
            text = "Their text and audio are deleted from this Mac. Your files stay where they are."
        if not confirm(self, title, text, yes="Remove", no="Cancel", destructive=True):
            return
        for record in records:
            self._remove_book(record.book_id)
        self.library_page.shelf.set_selecting(False)
        self.library_page.set_book_open(self.book.title if self.book is not None else "")
        self.library_page.refresh()

    def _show_details(self, book_id: str):
        record = self.library.get(book_id)
        if record is None:
            return
        records = self.library.recent()
        answer = dialogs.BookDetailsDialog.ask(
            self, record, shelf.all_values(records, "tags"), shelf.all_values(records, "collections")
        )
        if answer is not None and self.library.get(book_id) is record:
            record.tags, record.collections = answer
            self.library.save()
            self.library_page.refresh()

    def _reveal(self, book_id: str):
        """Show in Finder: the book's file, selected in its folder."""
        record = self.library.get(book_id)
        path = Path(record.source_path) if record is not None and record.source_path else None
        if path is None or not path.exists():
            inform(self, "The file isn't where it was.", f"LivroSound can't find {path or 'it'}.")
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _on_app_option(self, field: str, on: bool):
        """The library's ☰ menu: Auto Mode restyles at once; the others apply where they're used."""
        if field == "auto_mode":
            self._follow_appearance()

    def _refresh_library_info(self):
        """Read covers and book info (subjects, series, pages, length) for books that lack
        them, off the UI thread; each book's tile updates as it's read."""
        if self._info_task is not None or self._closing:
            return
        todo = [
            (r.book_id, r.source_path, r.source_kind)
            for r in self.library.recent()
            if r.info_version < bookinfo.INFO_VERSION
        ]
        if not todo:
            return
        results: dict[str, tuple] = {}

        def job(report, cancelled):
            for i, (book_id, source, kind) in enumerate(todo):
                if cancelled():
                    break
                book = Book.load(paths.book_dir(book_id))
                words = sum(len(b.text.split()) for b in book.blocks) if book is not None else 0
                info = bookinfo.read_info(source, kind, words=words) if source and Path(source).exists() else None
                if info is not None and info.cover:
                    bookinfo.save_cover(book_id, info.cover)
                results[book_id] = (info, _spoken_before(book) if book is not None else None)
                report(i + 1, len(todo), book_id)

        task = Task(job)
        self._info_task = task
        self._threads.add(task)
        task.progressed.connect(lambda _done, _total, book_id: self._apply_book_info(book_id, *results.pop(book_id)))
        task.finished.connect(lambda: self._on_info_finished(task))
        task.start()

    def _apply_book_info(self, book_id: str, info: bookinfo.BookInfo | None, before: list[int] | None):
        record = self.library.get(book_id)
        if record is None or self._closing:
            return
        if info is not None:
            _apply_info(record, info)
        if before is not None:
            record.spoken_chars = before[-1]
            if record.read_fraction < 0 and before[-1]:
                record.read_fraction = before[min(max(record.position_block, 0), len(before) - 1)] / before[-1]
        record.info_version = bookinfo.INFO_VERSION
        self.library_page.cover_changed(book_id)

    def _on_info_finished(self, task: Task):
        self._threads.discard(task)
        task.deleteLater()
        if task is self._info_task:
            self._info_task = None
        if not self._closing:
            self.library.save()
            self.library_page.refresh()

    # ---------- books from Finder ----------

    def open_external(self, path: str):
        """A book opened with LivroSound from Finder (or dropped on the window): imported
        at once with Auto Import on File Open, else after asking."""
        if self._closing:
            return
        name = Path(path).name
        if not self.settings.auto_import and not confirm(
            self, f"Add “{name}” to your library?", "LivroSound reads it and keeps it with your other books.",
            yes="Add", no="Cancel",
        ):
            return
        self._when_no_dialog(lambda: self.open_path(path))

    def dragEnterEvent(self, event):
        if self._dropped_book(event.mimeData()) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._dropped_book(event.mimeData())
        if path is not None:
            event.acceptProposedAction()
            QTimer.singleShot(0, self, lambda: self.open_external(path))

    @staticmethod
    def _dropped_book(mime) -> str | None:
        for url in mime.urls() if mime.hasUrls() else []:
            path = Path(url.toLocalFile())
            if path.suffix.lower() in SUPPORTED_SUFFIXES:
                return str(path)
        return None

    # ---------- resuming a book ----------

    def _remove_book(self, book_id: str):
        """Remove from Library (the user was asked first): an open book is closed first,
        and its files are deleted only once no worker writes to them any more."""
        if self.book is not None and self.book.book_id == book_id:
            self._close_book(save=False)
            if self.pages.currentWidget() is self.reader_page:
                self._show_landing()
        self.library.remove(book_id, delete_files=False)
        self.library.save()
        self._when_released(book_id, lambda: self.library.remove(book_id), opening=False)

    def _resume(self, record: BookRecord, *, ask: bool = True):
        """Open a book from the library at its saved position; offer to carry on generating."""
        if self.book is not None and self.book.book_id == record.book_id:
            self._show_reader()
            return
        folder = paths.book_dir(record.book_id)

        def job(report, cancelled):
            book = Book.load(folder)
            if book is not None:
                if record.source_path and record.source_path != book.source_path and Path(record.source_path).exists():
                    book.source_path = record.source_path  # moved: PDF pages are drawn from the file
                    book.save(folder)
                return book
            if record.source_path and Path(record.source_path).exists():
                # The cache is gone: read the book again.
                return load_book(record.source_path, progress=report, cancelled=cancelled)
            raise BookImportError(
                f"Its file is no longer at {record.source_path or 'its old place'}, and LivroSound's copy of "
                "its text was removed. Upload the book again to keep listening."
            )

        task = Task(job)
        close_progress = self._progress_for(task, f"Opening “{record.title}”")

        def done(book: Book):
            close_progress()
            self._when_no_dialog(lambda: opened(book))

        def opened(book: Book):
            self._close_book()
            rec = record
            if book.book_id != record.book_id:  # the file changed since: its old text and audio are stale
                self.library.remove(record.book_id)
                rec = dataclasses.replace(
                    record, book_id=book.book_id, audio_signature="", generated_fraction=0.0, generation_start=-1
                )
            self._open_book(book, rec)
            if ask:
                self._offer_to_continue()

        def failed(message: str):
            close_progress()
            inform(self, f"“{record.title}” couldn't be opened.", message)

        def cancelled():
            close_progress()
            self.statusBar().showMessage("Opening cancelled.", 4000)

        self._when_released(record.book_id, lambda: self._start_task(task, done, failed, cancelled))

    def _offer_to_continue(self):
        """A resumed book whose generation was left unfinished: "Continue generating audio?" (no ding)."""
        rec = self.record
        if not self.order or rec is None or rec.generation_start < 0 or not self._engine_ready():
            return
        fraction = self.generator.fraction_done()
        if fraction >= 1:
            return
        percent = int(fraction * 100)
        book = self.book
        go_on = confirm(self, "Continue generating audio?", f"{percent}% of this book's audio is ready.")
        if not self._still(book):
            return
        if go_on:
            self._start_generation(self._position_block_or_first(), fresh=False, move=False)
        else:
            self.statusBar().showMessage("Press Resume Generating to make the rest of the audio.", 8000)

    # ---------- the open book ----------

    def _open_book(self, book: Book, record: BookRecord):
        """Show a book at the record's position, with whatever audio its store holds."""
        self.book, self.record = book, record
        record.last_opened = _now()
        self.store = AudioStore(paths.book_dir(book.book_id) / "audio.sqlite")
        if self.store.signature != record.audio_signature and not self.store.generated():
            # The audio was dropped (the book was read again): nothing is made yet.
            record.audio_signature = self.store.signature
            record.generated_fraction, record.generation_start = 0.0, -1
        self._before = _spoken_before(book)
        record.spoken_chars = self._before[-1]
        self._cues.clear()
        self._gate = self._pending_char = None
        self._pick_mode = self._at_end = False
        self._auto_follow, self._follow_block, self._highlight = True, -1, None
        self._position_block = -1
        self._set_title(book.title)
        self._show_reader()
        self._setup_view()
        self._build_audio((record.position_block, record.position_ms))
        self.library.upsert(record)
        self.library.save()
        if (record.position_block, record.position_ms) != (0, 0):
            self._center_on_position(animate=False)
        self._refresh_transport()
        self._show_generation_state()

    def _build_audio(self, position: tuple[int, int]):
        """A generator, timeline and player for the record's engine, voice and speech
        options (the settings' when none are chosen yet), standing at position."""
        rec = self.record
        options = rec.audio_options() or self.settings.speech_options()
        checker = self.transcriber if self.settings.validate_audio and self.transcriber.available()[0] else None
        voice = rec.voice or self.settings.voice
        self.generator = Generator(self.book, self.store, self.engine, voice, options, checker)
        self.order = self.generator.order()
        self.weights = self.generator.weights()
        self.timeline = Timeline(self.order, self.store.durations())
        self.player.set_rate(self.settings.playback_rate)
        self.player.set_book(self.store, self.timeline)
        self.player.seek(*position)
        self._position_block = -1
        self._on_position(*self.player.position())

    def _close_book(self, *, save: bool = True):
        """Put the open book away: position saved, playback stopped, generation
        cancelled (its store closes once the worker has stopped)."""
        if self.book is None:
            return
        self.player.pause()
        if save:
            self._save_position(force=True)
        for name in ("_contents_popover", "_themes_popover"):
            popover = self._popover(name)
            if popover is not None:
                popover.close()
        worker, self._worker, self._pending_start = self._worker, None, None
        if worker is not None:
            worker.cancel()
            self._retiring.append(worker)
        if self.view is not None:
            self.view.clear_highlight()
        store = self.store
        self.book = self.record = self.store = self.generator = self.timeline = None
        self._release_store(store)
        self.order, self.weights, self._cues = [], {}, {}
        self._gate = self._pending_char = self._highlight = None
        self._position_timer.stop()
        self._show_generation_state()

    def _when_released(self, book_id: str, then: Callable[[], None], *, opening: bool = True):
        """Run then() once nothing works on that book's audio any more: no stopping worker
        writes to it, no export reads it. While an open waits (opening), the window is
        busy: a second request isn't queued."""
        if self._in_use(book_id):
            if opening:
                self._pending_open = book_id
                self.statusBar().showMessage("Finishing with that book's audio…")
            self._released.append((book_id, then))
        else:
            then()

    def _in_use(self, book_id: str) -> bool:
        exporting = self._export is not None and self._export_store is not None and self._export_book == book_id
        return exporting or any(w.generator.book.book_id == book_id for w in self._retiring)

    def _released_now(self, book_id: str):
        """A worker or export of this book just ended: run what waited for it, if nothing else still runs."""
        if self._closing or self._in_use(book_id):
            return
        ready = [then for bid, then in self._released if bid == book_id]
        self._released = [(bid, then) for bid, then in self._released if bid != book_id]
        if ready:
            self._when_no_dialog(lambda: self._run_released(book_id, ready))

    def _release_store(self, store: AudioStore | None):
        """Close a book's store once nothing uses it: not the open book, no stopping worker,
        no export still reading it."""
        if store is None or store is self.store or store is self._export_store:
            return
        if any(w.generator.store is store for w in self._retiring):
            return
        store.close()

    def _save_record(self):
        """Save the open book's record, unless it was removed from the library meanwhile
        (Remove from Library while it was open: it must not come back)."""
        if self.record is not None and self.library.get(self.record.book_id) is self.record:
            self.library.save()

    def _save_position(self, force: bool = False):
        if self.record is None or self.store is None:
            return
        block, ms = self.player.position()
        if not force and (block, ms) == self._saved_position:
            return
        self._saved_position = (block, ms)
        rec = self.record
        rec.position_block, rec.position_ms = block, ms
        if self._before[-1]:
            rec.read_fraction = self._before[min(max(block, 0), len(self._before) - 1)] / self._before[-1]
        if self.generator is not None:
            rec.generated_fraction = self.generator.fraction_done()
        self._save_record()

    # ---------- voice and options ----------

    def _still(self, book: Book | None) -> bool:
        """After a dialog: is the same book still open (and the app not quitting)? A dialog's
        event loop runs queued work that may have switched books."""
        return book is not None and self.book is book and not self._closing

    def _engine_ready(self) -> bool:
        """Has this book a voice to read with: chosen, made with this engine (a book whose
        audio another engine made gets its voice chosen again), and usable here?"""
        rec, engine = self.record, self.engine
        if rec is None or rec.engine != engine.name or not engine.available()[0]:
            return False
        return rec.voice in engine.voices()

    def _pause_for_preview(self):
        self.player.pause()

    def _choose_voice(self) -> bool:
        """Choose a Voice → Customize Audio (Go Back returns to the voice). True = Generate Audio."""
        while True:
            voice = dialogs.ChooseVoiceDialog.ask(
                self, self.engine, self.settings.voice, before_play=self._pause_for_preview
            )
            if voice is None:
                return False
            self.settings.voice = voice
            self.settings.save()
            answer = dialogs.CustomizeAudioDialog.ask(self, self.settings, self.transcriber.available())
            self._apply_highlight_setting()
            if answer == "generate":
                return True
            if answer == "cancel":
                return False

    def _use_chosen_voice(self, *, replace: bool) -> bool:
        """The voice and options just chosen become the book's. Audio made another way
        (or any audio, when replace) is dropped, asking first. False = the user kept it."""
        book = self.book
        engine, voice = self.engine.name, self.settings.voice
        options = self.settings.speech_options()
        signature = audio_signature(engine, voice, options)
        if replace or self.store.signature != signature:
            if self.store.generated() and not confirm(
                self,
                "Replace the audio made so far?",
                "It's deleted and made again with the voice and settings you chose.",
                yes="Replace",
                no="Keep It",
                destructive=True,
            ):
                return False
            if not self._still(book):
                return False
            self.player.pause()
            self.store.reset(signature)
            self._cues.clear()
            self.record.generated_fraction, self.record.generation_start = 0.0, -1
        rec = self.record
        rec.engine, rec.voice, rec.audio_signature = engine, voice, signature
        rec.speech_options = dataclasses.asdict(options)
        self._build_audio(self.player.position())
        self._save_record()
        self._refresh_transport()
        self._show_generation_state()
        return True

    def _ask_where_to_start(self):
        """Flow 5: "Generate audio from the beginning?" — No: the next click picks the start."""
        if not self.order:
            self.statusBar().showMessage("This book has nothing to read aloud.")
            return
        if self.generator.fraction_done() >= 1:
            self.statusBar().showMessage("All of this book's audio is ready.", 6000)
            return
        book = self.book
        start = confirm(self, "Generate audio from the beginning?", "")
        if not self._still(book):
            return
        if start:
            self._start_generation(self.order[0])
        else:
            self._pick_mode = True
            inform(self, "Choose where to start", PICK_A_START)  # its own window, as asked
            self.statusBar().showMessage(PICK_A_START)  # and a reminder below the book

    def regenerate(self):
        """Re-Generate Audio (flow 10): Choose a Voice → Customize Audio → replace → flow 5."""
        if self.book is None or self._worker is not None:
            return  # disabled while generating, and until a cancelled run has stopped writing
        self.player.pause()
        book = self.book
        if not self._choose_voice() or not self._still(book):
            return
        if self._use_chosen_voice(replace=True):
            self._ask_where_to_start()

    # ---------- generation ----------

    def _generating(self) -> bool:
        """Is audio being made (or about to be, by a restart)?"""
        return self._pending_start is not None or (self._worker is not None and not self._worker.is_cancelling())

    def _start_generation(self, block: int, *, fresh: bool = True, move: bool = True, char: int = 0):
        """Make audio from block on (then wrapping around). With move, playback stops and
        the position moves there; Play waits for 5% of the book from the position."""
        self._pick_mode = False
        target = self.timeline.first_at_or_after(block)
        if target is None:
            target = self.order[0]
        if move:
            self.player.pause()
            self.player.seek(target, 0)
            self._auto_follow, self._follow_block = True, target
            if not self.store.has(target):
                self._preview_highlight(target, char)
            self.view.scroll_to(target, char)
            self._pending_char = (target, char) if char else None
        # The 5% wait is for starting where there's no audio yet. Where there is, Play stays
        # on: the player waits at a gap generation is about to fill, else it asks.
        here = self.timeline.first_at_or_after(self.player.position()[0])
        self._gate = None
        if (
            here is not None
            and not self.store.has(here)
            and not self.player.is_playing()
            and not stream_ready(self.order, self.store.generated(), here, self.weights)
        ):
            self._gate = (here, fresh)
        self.record.generation_start = target
        self.record.speech_options = dataclasses.asdict(self.generator.options)
        self._save_record()
        if self._worker is not None:
            self._pending_start = target
            self._worker.cancel()
        else:
            self._launch(target)
        self.statusBar().showMessage("Making audio…" if self._gate else "Making the rest of the audio…")
        self._refresh_transport()
        self._show_generation_state()

    def _launch(self, start: int):
        if self._closing:
            return  # Quit: nothing new starts
        worker = GenerationWorker(self.generator, start)
        # Each slot is told which worker spoke: a stopping one may still finish a block.
        worker.block_done.connect(lambda block, ms: self._on_block_done(worker, block, ms))
        worker.status.connect(lambda message: self._on_generation_status(worker, message))
        worker.finished_ok.connect(lambda issues: self._on_generation_done(worker, issues))
        worker.failed.connect(lambda message: self._on_generation_failed(worker, message))
        worker.finished.connect(lambda: self._on_worker_finished(worker))
        self._worker = worker
        self._threads.add(worker)
        self._record_saved_at = time.monotonic()
        worker.start()

    def _of_this_book(self, worker: GenerationWorker) -> bool:
        return self.store is not None and worker.generator.store is self.store

    def _on_block_done(self, worker: GenerationWorker, block: int, duration_ms: int):
        if not self._of_this_book(worker):
            return  # a closed book's worker, finishing its last block
        self.timeline.set_duration(block, duration_ms)
        self.player.block_generated(block)
        self._cues.pop(block, None)
        fraction = self.generator.fraction_done()
        self._set_progress(fraction)
        if self._pending_char is not None and self._pending_char[0] == block:
            _, char = self._pending_char
            self._pending_char = None
            if not self.player.is_playing() and self.player.position()[0] == block:
                self.player.seek(block, self._cue_ms(block, char))
        self._check_gate()
        if block == self._position_block:
            self._on_position(*self.player.position())  # the sentence there has cues now
        self._refresh_transport()
        contents = self._popover("_contents_popover")
        if contents is not None:
            contents.set_ready(self._toc_ready())
        if time.monotonic() - self._record_saved_at >= RECORD_SAVE_INTERVAL_S:
            self._record_saved_at = time.monotonic()
            self.record.generated_fraction = fraction
            self._save_record()

    def _on_generation_status(self, worker: GenerationWorker, message: str):
        if worker is not self._worker:
            return
        self.progress.setToolTip(message)
        if self._gate is not None:
            self.statusBar().showMessage(f"Making audio… {message}")

    def _check_gate(self):
        """Play enables once 5% of the book from the position is ready (flow 6)."""
        if self._gate is None:
            return
        block, ding = self._gate
        if not stream_ready(self.order, self.store.generated(), block, self.weights):
            return
        self._gate = None
        if ding:
            self._ding()
        if self._generating():
            self.statusBar().showMessage("Ready to play — generation continues in the background")
        else:
            self.statusBar().showMessage("Ready to play", 6000)
        self._refresh_transport()

    def _on_generation_done(self, worker: GenerationWorker, issues: list):
        if not self._of_this_book(worker):
            return
        self.record.generated_fraction = self.generator.fraction_done()
        if self.record.generated_fraction >= 1:
            self.record.generation_start = -1
            self.statusBar().showMessage("All the audio is ready. Export MP3 saves the book as one file.", 10000)
        self._save_record()
        if issues:
            self._report_issues(issues)

    def _report_issues(self, issues):
        worst = sorted(issues, key=lambda i: -i.error_rate)[:5]
        lines = "\n".join(
            f"• {int(i.error_rate * 100)}% off: “{i.text}” — heard “{i.transcript or '…'}”" for i in worst
        )
        more = f"\n…and {len(issues) - len(worst)} more." if len(issues) > len(worst) else ""
        inform(
            self,
            f"{len(issues)} sentence{'' if len(issues) == 1 else 's'} didn't pass the Whisper check.",
            f"The best of three takes was kept for each. Click one to listen.\n\n{lines}{more}",
        )

    def _on_generation_failed(self, worker: GenerationWorker, message: str):
        if not self._of_this_book(worker):
            return
        self._save_position(force=True)
        if paths.book_dir(self.book.book_id).is_dir():
            inform(self, "Audio generation stopped.", f"{message}\n\nWhat was made so far is kept.")
        else:  # LivroSound's copy of the book was deleted (cache folder removed) while it ran
            inform(
                self,
                "Audio generation stopped.",
                f"{message}\n\nLivroSound's copy of this book was deleted while its audio was being made, "
                "so that audio is gone. Upload the book again to make it.",
            )

    def _on_worker_finished(self, worker: GenerationWorker):
        self._threads.discard(worker)
        if worker in self._retiring:
            self._retiring.remove(worker)
            self._release_store(worker.generator.store)
            self._released_now(worker.generator.book.book_id)
        elif worker is self._worker:
            self._worker = None
            if self._pending_start is not None and not self._closing:
                start, self._pending_start = self._pending_start, None
                self._launch(start)
            else:
                if self._gate is not None:
                    self._gate = None  # nothing more is coming: Play follows the audio there is
                self._refresh_transport()
                self._show_generation_state()
        worker.deleteLater()

    def _run_released(self, book_id: str, steps: list[Callable[[], None]]):
        if self._pending_open == book_id:
            self._pending_open = None
        for step in steps:
            step()

    def cancel_generation(self):
        """Cancel Generation: asks first, keeps what's made; Resume Generating takes the bar's place."""
        worker = self._worker
        if worker is None or not self._generating():
            return
        if not confirm(
            self, "Stop generating audio?", "What's been made so far is kept.",
            yes="Stop", no="Keep Going", destructive=True,
        ):
            return
        if self._worker is not worker or self._closing:
            return
        if self.player.is_playing() and not self.store.has(self.player.position()[0]):
            self.player.pause()  # it was waiting for audio that won't come now
        self._pending_start = None
        worker.cancel()
        self._save_position(force=True)
        self.statusBar().showMessage("Audio generation stopped. What's been made so far is kept.", 8000)
        self._show_generation_state()

    def resume_generation(self):
        """Resume Generating: from the first missing block at or after the position."""
        if self.book is None or self._generating():
            return
        book = self.book
        if not self._engine_ready():
            if not self._choose_voice() or not self._still(book) or not self._use_chosen_voice(replace=False):
                return
        if self._still(book) and not self._generating():
            self._start_generation(self._position_block_or_first(), move=False)

    def _show_generation_state(self):
        """The bottom right and the toolbar for what generation is doing (flow 6)."""
        has_book = self.book is not None and self.generator is not None
        running = has_book and self._generating()
        exporting = self._export is not None
        done = has_book and bool(self.order) and self.generator.fraction_done() >= 1
        started = has_book and bool(self.record.engine) and (
            self.record.generation_start >= 0 or bool(self.store.generated())
        )
        self.progress.setVisible(running or exporting)
        self.cancel_button.setVisible(running and not exporting)
        self.cancel_button.setEnabled(True)
        self.export_button.setVisible(done and not running and not exporting)
        self.resume_button.setVisible(has_book and not running and not done and started and bool(self.order))
        self.regenerate_action.setEnabled(has_book and self._worker is None and not exporting)
        self.contents_button.setEnabled(has_book)
        self.themes_button.setEnabled(has_book)
        if running:
            self._set_progress(self.generator.fraction_done())
            QTimer.singleShot(0, self, self._place_overlays)  # once the status bar has laid the bar out

    def _set_progress(self, fraction: float):
        if self._export is not None:
            return
        self.progress.setValue(round(fraction * 1000))
        self.progress.setFormat(f"Generating {int(fraction * 100)}%")

    def _waiting_allowed(self, block: int) -> bool:
        """May playback wait at this block? Only when the running worker makes it soon."""
        worker = self._worker
        return (
            worker is not None
            and self._pending_start is None
            and worker.generator is self.generator
            and worker.reaches_soon(block, self.weights, WAIT_AHEAD_CHARS)
        )

    def _ready_sound_effect(self) -> QSoundEffect:
        if self._ready_sound is None:
            self._ready_sound = QSoundEffect(self)
            self._ready_sound.setSource(QUrl.fromLocalFile(str(READY_SOUND)))
            self._ready_sound.setVolume(0.3)
        return self._ready_sound

    def _ding(self):
        """The ready sound: freshly made audio can be played."""
        self._ready_sound_effect().play()

    # ---------- going somewhere in the book ----------

    def _on_view_clicked(self, block: int, char: int):
        if self.book is None or not 0 <= block < len(self.book.blocks):
            return
        link = self.book.blocks[block].link
        if link >= 0:  # a line of the book's own contents page: like picking that chapter
            self.view.scroll_to(link, 0)
            QTimer.singleShot(0, self, lambda: self._go_to(link))
        else:
            QTimer.singleShot(0, self, lambda: self._go_to(block, char))

    def _on_contents_picked(self, index: int):
        if self.book is None or not 0 <= index < len(self.book.toc):
            return
        entry = self.book.toc[index]
        self.view.scroll_to(entry.block, 0)
        QTimer.singleShot(0, self, lambda: self._go_to(entry.block))

    def _go_to(self, block: int, char: int = 0):
        """Flow 8: play from there if its audio is ready, else offer to make it."""
        if self.book is None or self.timeline is None:
            return
        target = self.timeline.first_at_or_after(block)
        if target is None:
            self.statusBar().showMessage("This part of the book isn't read aloud.", 5000)
            return
        if target != block:
            char = 0  # a block that isn't read (an image, the front matter): the next one that is
        if self.store.has(target):
            self._auto_follow, self._follow_block = True, -1
            self._gate = None
            self._at_end = False
            self.player.seek(target, self._cue_ms(target, char))
            self._refresh_transport()
            self.player.play()
            return
        book = self.book
        if not self._engine_ready():
            # No voice yet: choose one, then start making audio from there.
            if not self._choose_voice() or not self._still(book) or not self._use_chosen_voice(replace=False):
                return
            self._start_generation(target, char=char)
            return
        if self._pick_mode:
            ok = confirm(self, "Start generating audio from this point?", "")
        else:
            ok = confirm(self, "Audio not ready yet.", "Start generating audio from this point?")
        if ok and self._still(book):
            self._start_generation(target, char=char)

    def _cue_ms(self, block: int, char: int) -> int:
        """When the sentence holding char starts in the block's audio (0: the block's start)."""
        if char <= 0:
            return 0
        best = 0
        for cue in self._cues_for(block):
            if cue.start_char <= char:
                best = cue.start_ms
            if cue.start_char <= char < cue.end_char:
                return cue.start_ms
        return best

    def _cues_for(self, block: int) -> list[Cue]:
        cues = self._cues.get(block)
        if cues is None:
            info = self.store.info(block) if self.store is not None and self.store.has(block) else None
            if info is None:
                return []
            cues = self._cues[block] = info.cues
        return cues

    def _position_block_or_first(self) -> int:
        block = self.timeline.first_at_or_after(self.player.position()[0])
        return block if block is not None else self.order[0]

    # ---------- playback ----------

    def toggle_play(self):
        if self.book is None:
            return
        if self.player.is_playing():
            self.player.pause()
            return
        if not self.capsule.play_button.isEnabled():
            return
        if self._at_end:
            self._at_end = False
            self.player.seek(self.order[0])
        self._auto_follow, self._follow_block = True, -1
        self.player.play()

    def skip(self, delta_ms: int):
        """−10 sec / +10 sec: audio, highlight and scroll follow."""
        if self.book is None or not self.capsule.back_button.isEnabled():
            return
        self._auto_follow = True
        self._at_end = False
        self.player.skip(delta_ms)

    def step_paragraph(self, step: int):
        """⏮ / ⏭: one paragraph back or forward."""
        if self.book is None or not self.capsule.prev_button.isEnabled():
            return
        self._auto_follow = True
        self._at_end = False
        if step > 0:
            self.player.next_block()
        else:
            self.player.prev_block()

    def _on_speed_changed(self, index: int):
        rate = PLAYBACK_SPEEDS[index]
        self.settings.playback_rate = rate
        self.settings.save()
        if self.book is not None:  # a closed book's store mustn't be read; _build_audio applies it
            self.player.set_rate(rate)

    def _on_playing_changed(self, playing: bool):
        self.capsule.set_playing(playing)
        if playing:
            self._position_timer.start()
        else:
            self._position_timer.stop()
            self._save_position()
        self._refresh_transport()

    def _on_waiting(self, waiting: bool):
        if waiting:
            self.statusBar().showMessage("Waiting for audio…")
        elif self.statusBar().currentMessage() == "Waiting for audio…":
            self.statusBar().clearMessage()

    def _on_needs_audio(self, block: int):
        # Asked once the player has finished handling whatever ran into the gap.
        QTimer.singleShot(0, self, lambda: self._ask_for_audio(block))

    def _ask_for_audio(self, block: int):
        """Playback reached a block without audio and none is coming soon (flow 8)."""
        if self.book is None or self._closing or self.store.has(block):
            return
        self._save_position(force=True)
        book = self.book
        if not self._engine_ready():
            if self._choose_voice() and self._still(book) and self._use_chosen_voice(replace=False):
                self._start_generation(block)
            return
        if confirm(self, "Audio not ready yet.", "Start generating audio from this point?") and self._still(book):
            self._start_generation(block)

    def _on_finished(self):
        self._at_end = True
        self.statusBar().showMessage("The end.", 6000)
        self._save_position(force=True)

    def _on_player_error(self, message: str):
        self.statusBar().showMessage(message, 8000)
        inform(self, "The audio can't be played.", message)

    def _refresh_transport(self):
        """Play enables where there's audio to play and no 5% wait; the rest once any audio exists."""
        has_book = self.book is not None and self.store is not None and bool(self.order)
        playing = self.player.is_playing()
        here = self.timeline.first_at_or_after(self.player.position()[0]) if has_book else None
        ready_here = here is not None and self.store.has(here)
        self.capsule.set_play_enabled(has_book and (playing or (self._gate is None and ready_here)))
        self.capsule.set_navigation_enabled(has_book and self._gate is None and bool(self.store.durations()))

    def keyPressEvent(self, event):
        """Space = play/pause, ← / → = −10 / +10 sec (the reader views pass these keys on)."""
        plain = not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier  # macOS marks arrows "keypad"
        if self.pages.currentWidget() is self.reader_page and plain:
            key = event.key()
            if key == Qt.Key.Key_Space:
                self.toggle_play()
                return
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                self.skip(-10_000 if key == Qt.Key.Key_Left else 10_000)
                return
        super().keyPressEvent(event)

    # ---------- highlight and auto-follow ----------

    def _on_position(self, block: int, ms: int):
        """Highlight the sentence being heard and keep it in view (flow 7)."""
        if self.book is None:
            return
        if block != self._position_block:
            self._position_block = block
            self._refresh_transport()
        if not self.settings.highlight or self.view is None:
            return
        cues = self._cues_for(block)
        # During a block's lead-in silence (a heading's breath), playback keeps the last
        # sentence lit until the block is heard; standing still there, the block's first is.
        cue = cue_at(cues, ms) or (cues[0] if cues and not self.player.is_playing() else None)
        if cue is None:
            return  # no audio here yet, or not yet heard: keep what's shown
        span = self._highlight_span(block, ms, cue)
        if block != self._info_block:
            self._info_block = block
            self._update_reading_info()
        if span == self._highlight:
            return
        self._highlight = span
        self.view.show_highlight(*span)
        if self._auto_follow:
            if block != self._follow_block:
                self._follow_block = block
                self.view.scroll_to(block, cue.start_char)
            else:
                self.view.keep_in_view(*span)

    def _highlight_span(self, block: int, ms: int, cue: Cue) -> tuple[int, int, int]:
        """What the highlighter covers (Settings ▸ Reading ▸ Highlighter): the word being
        said, the sentence (a cue), or the whole paragraph."""
        unit = self.settings.highlight_unit
        if unit == "paragraph":
            return block, 0, len(self.book.blocks[block].text)
        if unit == "word" and cue.end_ms > cue.start_ms:
            fraction = (ms - cue.start_ms) / (cue.end_ms - cue.start_ms)
            return (block, *word_at(self.book.blocks[block].text, cue.start_char, cue.end_char, fraction))
        return block, cue.start_char, cue.end_char

    def _center_on_position(self, *, animate: bool):
        """The sentence at the position, highlighted and centered (a resumed book, a new layout)."""
        if self.view is None or self.book is None:
            return
        block, _ = self.player.position()
        char = 0
        if self._highlight is not None and self._highlight[0] == block:
            char = self._highlight[1]
            if self.settings.highlight:
                self.view.show_highlight(*self._highlight)
        self._follow_block = block
        self.view.scroll_to(block, char, animate=animate)

    def _preview_highlight(self, block: int, char: int):
        """Before a block has audio: highlight the sentence that will be read first."""
        if not self.settings.highlight or self.view is None:
            return
        text = self.book.blocks[block].text
        spans = split_sentences(text) or [(0, len(text))]
        start, end = next(((a, b) for a, b in spans if a <= char < b), spans[0])
        self._highlight = (block, start, end)
        self.view.show_highlight(block, start, end)

    def _suspend_auto_follow(self):
        self._auto_follow = False

    def _apply_highlight_setting(self):
        if self.view is None:
            return
        if not self.settings.highlight:
            self.view.clear_highlight()
            self._highlight = None
        else:
            self._highlight = None
            self._on_position(*self.player.position())

    # ---------- toolbar popovers ----------

    def _popover(self, name: str):
        """The Contents or Aa popover while it's open (they delete themselves on closing)."""
        popover = getattr(self, name)
        if popover is not None and not isValid(popover):
            popover = None
            setattr(self, name, None)
        return popover

    def _open_contents(self):
        if self.book is None:
            return
        entries = [(e.title, e.level, ready) for e, ready in zip(self.book.toc, self._toc_ready(), strict=True)]
        chapter = self.book.chapter_of(self.player.position()[0])
        current = self.book.toc.index(chapter) if chapter is not None else None
        popover = ContentsPopover(self, entries, current)
        popover.picked.connect(self._on_contents_picked)
        self._contents_popover = popover
        popover.popup_under(self.contents_button)

    def _toc_ready(self) -> list[bool]:
        """Per Contents entry: does every block it reads (up to the next entry of its level or
        higher) have audio?"""
        toc, order = self.book.toc, self.order
        made = self.store.generated()
        ready = []
        for i, entry in enumerate(toc):
            end = next((e.block for e in toc[i + 1 :] if e.level <= entry.level), len(self.book.blocks))
            a, b = bisect.bisect_left(order, entry.block), bisect.bisect_left(order, max(end, entry.block + 1))
            ready.append(a < b and all(order[k] in made for k in range(a, b)))
        return ready

    def _open_themes(self):
        """"Aa": themes, text size, layout and the highlight, like Apple Books."""
        if self.book is None:
            return
        popover = ThemesPopover(
            self, self.theme.key, layout=self._layout_mode(), highlight=self.settings.highlight,
            page_view=self.settings.page_view, highlight_unit=self.settings.highlight_unit,
        )
        popover.theme_chosen.connect(self._apply_theme)
        popover.text_size_step.connect(self._step_text_size)
        popover.layout_chosen.connect(self._set_layout)
        popover.highlight_toggled.connect(self._on_highlight_toggled)
        popover.page_view_chosen.connect(lambda mode: self._change_setting("page_view", mode))
        popover.highlight_unit_chosen.connect(lambda unit: self._change_setting("highlight_unit", unit))
        popover.settings_requested.connect(lambda: QTimer.singleShot(0, self, self.open_settings))
        self._themes_popover = popover
        popover.popup_under(self.themes_button)

    def _step_text_size(self, step: int):
        if self.view is None:
            return
        self._page_count_timer.start()
        if self.view is self.page_view:
            zoom = round(min(MAX_ZOOM, max(MIN_ZOOM, self.settings.page_zoom + step * ZOOM_STEP)), 2)
            if zoom == self.settings.page_zoom:
                return
            self.settings.page_zoom = zoom
            self.view.set_scale(zoom)
            self.statusBar().showMessage(f"Zoom: {round(zoom * 100)}%", 3000)
        else:
            current = min(REFLOW_SIZES, key=lambda s: abs(s - self.settings.reflow_font_size))
            size = REFLOW_SIZES[max(0, min(len(REFLOW_SIZES) - 1, REFLOW_SIZES.index(current) + step))]
            if size == self.settings.reflow_font_size:
                return
            self.settings.reflow_font_size = size
            self.view.set_scale(size)
            self.statusBar().showMessage(f"Text size: {size} pt", 3000)
        self.settings.save()
        if self._highlight is not None and self._auto_follow:
            block, start, end = self._highlight
            QTimer.singleShot(0, self, lambda: self.view.scroll_to(block, start, animate=False) if self.view else None)

    def _set_layout(self, mode: str):
        """Aa ▸ Layout: swap the view, keeping the position and the highlight."""
        if self.book is None or mode == self._layout_mode():
            return
        self.record.layout = mode
        self._save_record()
        self._told_missing = ""  # asked for its pages: say again if they can't be shown
        highlight = self._highlight
        self._setup_view()
        self._highlight = highlight
        self._center_on_position(animate=False)

    # ---------- settings ----------

    def open_settings(self):
        """Settings… (⌘,): every change applies at once."""
        dialogs.SettingsDialog.ask(self, self.settings, self._on_setting_changed, add_font=self._add_font)
        self.settings.save()

    def _add_font(self) -> list[str]:
        path, _ = QFileDialog.getOpenFileName(self, "Add a Font", "", "Fonts (*.ttf *.otf *.ttc)")
        if not path:
            return []
        try:
            return fonts.add_font(path)
        except (OSError, ValueError) as e:
            inform(self, "This font couldn't be added.", str(e))
            return []

    def _change_setting(self, field: str, value):
        setattr(self.settings, field, value)
        self._on_setting_changed(field)

    def _on_setting_changed(self, field: str):
        """A setting changed (the Settings window, Aa, the library's ☰): apply it now."""
        s = self.settings
        if field in STYLE_FIELDS:
            for view in (self.reflow_view, self.page_view):
                if view is not None:
                    view.set_style(self._reading_style())
            self._after_relayout()
        elif field == "page_view":
            for view in (self.reflow_view, self.page_view):
                if view is not None:
                    view.set_page_mode(s.page_view)
            self._after_relayout()
        elif field == "reflow_font_size":
            if self.reflow_view is not None:
                self.reflow_view.set_scale(s.reflow_font_size)
            self._after_relayout()
        elif field == "theme":
            self._apply_theme(s.theme)
        elif field in ("custom_page", "custom_text"):
            themes.set_custom_colors(s.custom_page, s.custom_text)
            if self.theme.key == "custom":
                self._restyle("custom")
        elif field in ("auto_mode", "light_theme", "dark_theme"):
            self._follow_appearance()
        elif field in ("highlight", "highlight_unit"):
            self._apply_highlight_setting()
        elif field in ("show_page_numbers", "show_read_percent", "always_show_status_bar"):
            self._page_count_timer.start()
            self._update_reading_info()
        s.save()

    def _reading_style(self) -> ReadingStyle:
        s = self.settings
        return ReadingStyle(
            family=s.font_family, weight=s.font_weight, line_spacing=s.line_spacing,
            paragraph_spacing=s.paragraph_spacing, word_spacing=s.word_spacing, letter_spacing=s.letter_spacing,
            justify=s.justify, margin_left=s.margin_left, margin_right=s.margin_right, max_column=s.max_column,
        )

    def _after_relayout(self):
        """The text moved: the sentence being read back in view, the pages counted again."""
        if self.view is not None and self._highlight is not None and self._auto_follow:
            block, start, _ = self._highlight
            QTimer.singleShot(0, self, lambda: self.view.scroll_to(block, start, animate=False) if self.view else None)
        self._page_count_timer.start()
        self._update_reading_info()

    def _reading(self) -> bool:
        return self.book is not None and self.pages.currentWidget() is self.reader_page

    # ---------- page and progress ----------

    def _on_view_moved(self, view):
        if view is self.view:
            self._update_reading_info()
            self._poke_reading_info()

    def _count_pages(self):
        if self.view is self.reflow_view and self.view is not None and self.settings.show_page_numbers:
            self.view.count_pages()
        self._update_reading_info()

    def reading_fraction(self) -> float:
        """How much of the book lies before where the reader is: the audio's position while
        following it, else the page on screen."""
        if self.book is None or not self._before[-1]:
            return 0.0
        block = self.player.position()[0]
        if self.view is not None and (not self._auto_follow or not (self.store and self.store.durations())):
            block = self.view.current_block()
        return self._before[min(max(block, 0), len(self._before) - 1)] / self._before[-1]

    def reading_info_text(self) -> str:
        """"Page 12 of 340 · 34%" ("Pages 3–4 of 120" on a double page)."""
        if self.book is None or self.view is None:
            return ""
        parts = []
        if self.settings.show_page_numbers:
            info = self.view.page_info()
            if info is not None:
                first, last, total = info
                parts.append(f"Pages {first}–{last} of {total}" if last > first else f"Page {first} of {total}")
        if self.settings.show_read_percent:
            parts.append(f"{int(self.reading_fraction() * 100)}%")
        return "  ·  ".join(parts)

    def _update_reading_info(self):
        text = self.reading_info_text()
        self.reading_info.setText(text)
        if not text:
            self.reading_info.hide()
        elif self.settings.always_show_status_bar:
            self.reading_info.show()
        self._place_overlays()

    def _poke_reading_info(self):
        """The reader moved: show the page and progress for a moment."""
        if self.reading_info.text():
            self.reading_info.show()
            self._place_overlays()
            if not self.settings.always_show_status_bar:
                self._info_timer.start()

    def _fade_reading_info(self):
        if not self.settings.always_show_status_bar:
            self.reading_info.hide()

    def _on_highlight_toggled(self, on: bool):
        self.settings.highlight = on
        self.settings.save()
        self._apply_highlight_setting()

    # ---------- Choose a Layout previews ----------

    def _layout_previews(self, book: Book) -> tuple[QPixmap | None, QPixmap | None]:
        """Real thumbnails for LayoutDialog's two cards where they're cheap to make: the
        book's opening page, as printed and in LivroSound's layout."""
        start = _opening_block(book)
        original = reflow = None
        try:
            if book.source_kind == "pdf":
                original = self._pdf_preview(book, start)
            elif len(book.original_html) <= 400_000:
                original = self._reflow_preview(book, start, "original")
        except Exception:  # a preview is a nicety: the card draws a sketch instead
            original = None
        try:
            reflow = self._reflow_preview(book, start, "livrosound")
        except Exception:
            reflow = None
        return original, reflow

    def _reflow_preview(self, book: Book, start: int, mode: str) -> QPixmap:
        view = ReflowView()
        try:
            view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            # Shown (off screen), so it gets its size before laying out and scrolling.
            view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            view.resize(PREVIEW_SIZE)
            view.show()
            view.set_theme(self.theme)
            if mode == "livrosound":  # the opening, at the top of the page
                view.set_book(dataclasses.replace(book, blocks=book.blocks[start : start + 30], toc=[]), mode)
            else:
                view.set_book(book, mode)
                view.scroll_to(start, animate=False)
                bar = view.verticalScrollBar()
                bar.setValue(bar.value() + PREVIEW_SIZE.height() // 2 - 80)  # centered → near the top
            return view.grab()
        finally:
            view.deleteLater()

    def _pdf_preview(self, book: Book, start: int) -> QPixmap | None:
        import pymupdf

        block = book.blocks[start]
        page_index = block.page if block.page >= 0 else (block.words[0][0] if block.words else 0)
        with pymupdf.open(book.source_path) as doc:
            page = doc[page_index]
            clip = page.rect
            if book.text_box is not None:
                x0, y0, x1, y1 = book.text_box
                mx, my = (x1 - x0) * 0.06, (y1 - y0) * 0.06
                clip = pymupdf.Rect(x0 - mx, y0 - my, x1 + mx, y1 + my) & page.rect
            zoom = 2 * PREVIEW_SIZE.width() / max(1.0, clip.width)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, annots=False, alpha=False)
            image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(2.0)
        return pixmap

    # ---------- export ----------

    def export(self):
        """Export MP3 (at 100%): one file with chapters, made off the UI thread."""
        if self.book is None or self._export is not None or self._generating():
            return
        default = Path.home() / "Music" / f"{_file_name(self.book.title)}.mp3"
        path, _ = QFileDialog.getSaveFileName(self, "Export MP3", str(default), "MP3 audio (*.mp3)")
        if not path:
            return
        if not path.lower().endswith(".mp3"):
            path += ".mp3"
        book, store, order = self.book, self.store, list(self.order)

        def job(report, cancelled):
            export.export_mp3(
                book, store, order, Path(path),
                progress=lambda f: report(round(f * 1000), 1000, ""), cancelled=cancelled,
            )
            return path

        task = Task(job)
        # The store stays open until the export ends, whatever book is open meanwhile.
        self._export, self._export_store, self._export_book = task, store, book.book_id
        self._threads.add(task)
        task.progressed.connect(lambda done, total, _m: self._show_export_progress(done / max(1, total)))
        task.done.connect(self._on_exported)
        task.failed.connect(lambda message: inform(self, "The MP3 couldn't be saved.", message))
        task.cancelled.connect(lambda: self.statusBar().showMessage("Export cancelled.", 4000))
        task.finished.connect(lambda: self._on_export_finished(task))
        self._show_export_progress(0.0)
        self._show_generation_state()
        self.statusBar().showMessage(f"Exporting “{Path(path).name}”…")
        task.start()

    def _show_export_progress(self, fraction: float):
        self.progress.setValue(round(fraction * 1000))
        self.progress.setFormat(f"Exporting {int(fraction * 100)}%")

    def _on_exported(self, path: str):
        target = Path(path)
        self.statusBar().showMessage(f"Saved “{target.name}” in {target.parent.name or target.parent}.", 12000)

    def _on_export_finished(self, task: Task):
        self._threads.discard(task)
        task.deleteLater()
        if task is self._export:
            store, self._export, self._export_store = self._export_store, None, None
            self._release_store(store)  # the book may have been closed meanwhile
            self._released_now(self._export_book)
        self._show_generation_state()

    # ---------- quitting ----------

    def closeEvent(self, event):
        """Save where the reader is, stop playback and every thread, then close (flow 12).
        A thread that can't stop at once (a chunk of speech, a big book being read, a voice clip
        being made) is waited for with the window hidden; then the app quits."""
        if not self._closing:
            self._closing = True
            self.player.pause()
            self._save_position(force=True)
            self.settings.save()
            if self.page_view is not None:
                self.page_view.shutdown()
        # Every pass: nothing new may start, and whatever runs is asked to stop.
        self._pending_start = self._pending_open = None
        self._released.clear()
        for thread in self._threads:
            thread.cancel()
        previews = self.findChild(VoicePreviews, dialogs.ChooseVoiceDialog.PREVIEWS_NAME)
        if previews is not None:
            previews.shutdown(wait=False)
        threads = list(self._threads)
        deadline = time.monotonic() + 2.0
        for thread in threads:
            thread.wait(max(0, int((deadline - time.monotonic()) * 1000)))
        if previews is not None:
            previews.wait(max(0, int((deadline - time.monotonic()) * 1000)))
        if any(t.isRunning() for t in threads) or (previews is not None and previews.rendering()):
            event.ignore()
            if not self._went_away:
                self._went_away = True
                # While hidden, a dialog closing (an import's progress) would count as the
                # last window closing and quit under the running threads: we quit ourselves.
                QApplication.instance().setQuitOnLastWindowClosed(False)
                self.hide()
                self._close_timer.start()
            return
        self._close_timer.stop()
        # Closed for good: signals still queued from the threads find no book to act on.
        stores = {w.generator.store for w in self._retiring} | {self.store, self._export_store}
        self._retiring.clear()
        self.book = self.record = self.store = self.generator = self.timeline = None
        self._worker = self._export_store = None
        for store in stores - {None}:
            store.close()
        super().closeEvent(event)
        if self._went_away:
            # Hidden, this window no longer counts as the last one to close: quit ourselves.
            QApplication.instance().setQuitOnLastWindowClosed(True)
            QTimer.singleShot(0, QApplication.instance().quit)
