"""LivroSound: your books, read aloud. Run with `uv run main.py`."""

from __future__ import annotations

import logging
import os
import sys
import warnings


class _NoLibraryChatter(logging.Filter):
    """Pocket-TTS's codec logs its INFO lines on the root logger itself."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or "site-packages" not in record.pathname


def quiet_libraries() -> None:
    """Keep the terminal for real problems: Pocket-TTS chats at INFO level (every
    sentence's timing, partly on the root logger) and torch warns about things no
    reader can act on. Errors, and the app's own warnings, still get through."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")  # Whisper's first download
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.ffmpeg.info=false")  # its version banner
    for name in ("pocket_tts", "huggingface_hub"):
        logging.getLogger(name).setLevel(logging.ERROR)
    logging.getLogger().addFilter(_NoLibraryChatter())
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", message=".*weight_norm.*")  # torch, inside pocket_tts


def main() -> None:
    quiet_libraries()
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from livrosound.paths import ICONS_DIR
    from livrosound.ui.main_window import MainWindow
    from livrosound.ui.reader_view import ensure_reading_fonts
    from livrosound.ui.theme import ui_font

    app = QApplication(sys.argv)
    ensure_reading_fonts()  # Iowan Old Style, which macOS hides from apps (see reader_view.py)
    app.setApplicationName("LivroSound")
    app.setStyle("Fusion")
    app.setFont(ui_font(13))  # SF Pro, the macOS system font (see ui/theme.py)
    app.setWindowIcon(QIcon(str(ICONS_DIR / "livrosound-1024.png")))  # the window and the Dock

    class FileOpener(QObject):
        """Books opened with LivroSound from Finder (or dropped on its Dock icon) arrive
        as FileOpen events, the first ones before the window exists."""

        def __init__(self):
            super().__init__()
            self.window = None
            self.waiting = [a for a in sys.argv[1:] if not a.startswith("-")]  # `uv run main.py book.epub`

        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.FileOpen and event.file():
                self.waiting.append(event.file())
                self.deliver()
                return True
            return False

        def deliver(self):
            while self.window is not None and self.waiting:
                self.window.open_external(self.waiting.pop(0))

    opener = FileOpener()
    app.installEventFilter(opener)
    window = MainWindow()
    window.show()
    opener.window = window
    opener.deliver()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
