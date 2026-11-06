"""MainWindow end to end, offscreen: every user flow of DESIGN.md driven the way a
user would, with scripted answers to the dialogs and questions, a fake engine
that can be held mid-sentence (so "not generated yet" is deterministic), and the
player's fake sound output (its clock advances only when a test says so).

Waiting is done with a QEventLoop + QTimer (never QTest.qWait, which holds the
GIL and starves the generation thread)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QMimeData, Qt, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from livrosound import bookinfo, dsp, paths
from livrosound import export as export_mod
from livrosound.audio_store import cue_at
from livrosound.book import Book
from livrosound.library import Library, audio_signature
from livrosound.ui import dialogs
from livrosound.ui import main_window as mw
from livrosound.ui.main_window import MainWindow
from livrosound.ui.page_view import PageView
from livrosound.ui.reflow_view import ReflowView

from .fake_output import FakeOutputs
from .fakes import FakeEngine
from .test_ingest_epub import make_epub3
from .test_ingest_pdf import _converted_ebook

SENTENCES = [
    "The rain had not stopped for three days, and the river ran high under the old stone bridge.",
    "Clara watched it from the window while her tea went cold beside the unread letters.",
    "Nobody in the village could remember a spring quite like this one.",
    "In the evening the bells rang twice, which was a sign that something had changed.",
]
CHAPTERS = ("ONE", "TWO", "THREE")
PARAGRAPHS = 12  # per chapter


def write_txt(folder: Path, name: str = "rain.txt") -> Path:
    """A small novel: a title, an author line, three chapters of twelve two-sentence paragraphs."""
    lines = ["THE LONG RAIN", "", "by Ana Lima", ""]
    for chapter in CHAPTERS:
        lines += ["", f"CHAPTER {chapter}", ""]
        for k in range(PARAGRAPHS):
            lines += [f"{SENTENCES[k % 4]} {SENTENCES[(k + 1) % 4]}", ""]
    path = folder / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class HeldEngine(FakeEngine):
    """FakeEngine that takes a little time per chunk and can be held before its next
    chunk, so a test decides how far generation has got. park() holds it and waits
    until the worker is actually stopped inside synthesize (before that, a cancel
    could still reach it between chunks)."""

    def __init__(self, delay: float = 0.004):
        super().__init__()
        self.delay = delay
        self._go = threading.Event()
        self._go.set()
        self._parked = threading.Event()

    def hold(self):
        self._go.clear()

    def park(self):
        self.hold()
        wait_until(self._parked.is_set, what="the worker to park")

    def release(self):
        self._go.set()

    def synthesize(self, voice, text):
        if not self._go.is_set():
            self._parked.set()
            self._go.wait(30)
            self._parked.clear()
        time.sleep(self.delay)
        return super().synthesize(voice, text)


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(condition, timeout: float = 20.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or condition}")
        wait(10)


class Script:
    """Scripted answers for every question the window asks, and its windows."""

    def __init__(self, monkeypatch, qapp):
        self.monkeypatch = monkeypatch
        self.answers: dict[str, bool] = {}
        self.asked: list[str] = []
        self.informed: list[tuple[str, str]] = []
        self.layout: str | None = "livrosound"
        self.voice: str | None = "Aaron"
        self.customize = "generate"
        self.file = ""
        self.save_as = ""
        self.windows: list[tuple[MainWindow, HeldEngine]] = []
        monkeypatch.setattr(mw, "confirm", self._confirm)
        monkeypatch.setattr(mw, "inform", lambda parent, title, text: self.informed.append((title, text)))
        monkeypatch.setattr(dialogs.LayoutDialog, "ask", lambda *a, **k: self.layout)
        monkeypatch.setattr(dialogs.ChooseVoiceDialog, "ask", lambda *a, **k: self.voice)
        monkeypatch.setattr(dialogs.CustomizeAudioDialog, "ask", lambda *a, **k: self.customize)
        monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (self.file, ""))
        monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", lambda *a, **k: (self.save_as, ""))

    def _confirm(self, parent, title, text, yes="Yes", no="No", *, destructive=False):
        self.asked.append(title)
        if title not in self.answers:
            raise AssertionError(f"unexpected question: {title!r}")
        return self.answers[title]

    def window(self, engine: HeldEngine | None = None) -> tuple[MainWindow, FakeOutputs, HeldEngine]:
        engine = engine or HeldEngine()
        outputs = FakeOutputs()
        window = MainWindow(engine, output_factory=outputs)
        window.dings = []
        window._ding = lambda: window.dings.append(time.monotonic())
        window.resize(1100, 800)
        window.show()
        self.windows.append((window, engine))
        return window, outputs, engine

    def close_all(self):
        for window, engine in self.windows:
            engine.release()
            window.player.pause()
            window.close()
        for window, _ in self.windows:
            wait_until(lambda w=window: not _threads_running(w), what="threads to stop")
            window.deleteLater()
        self.windows.clear()


def _threads_running(window: MainWindow) -> bool:
    return any(t.isRunning() for t in window._threads)


@pytest.fixture
def script(qapp, monkeypatch):
    s = Script(monkeypatch, qapp)
    yield s
    s.close_all()


def play(window: MainWindow, outputs: FakeOutputs, ms: int) -> None:
    """Let ms of audio play through the fake output, as its 20 ms timer would."""
    for _ in range(ms // 20):
        if outputs.current is not None:
            outputs.current.advance(20)
        window.player._pump()
        QCoreApplication.processEvents()


def expected_highlight(window: MainWindow) -> tuple[int, int, int]:
    block, ms = window.player.position()
    cues = window.store.info(block).cues
    cue = cue_at(cues, ms) or cues[0]
    return block, cue.start_char, cue.end_char


def pick_from_library(window: MainWindow, book_id: str) -> None:
    """Go to Library, then click the book's cover."""
    window.landing.library_button.click()
    assert window.pages.currentWidget() is window.library_page
    window.library_page.open_requested.emit(book_id)


def open_book(script: Script, window: MainWindow, path: Path) -> None:
    script.file = str(path)
    window.open_book_dialog()
    wait_until(lambda: window.book is not None and window._task is None, what="the book to open")


def ffprobe(path: Path) -> dict:
    beside = Path(dsp.ffmpeg_binary()).with_name("ffprobe")
    exe = shutil.which("ffprobe") or (str(beside) if beside.is_file() else None)
    if exe is None:
        pytest.skip("ffprobe isn't installed")
    out = subprocess.run(
        [exe, "-v", "error", "-show_format", "-show_chapters", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


# ---------- flow 1: upload, listen, navigate, restart, cancel, resume, export ----------


def test_upload_listen_navigate_restart_cancel_resume_and_export(script, tmp_path):
    window, outputs, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    engine.hold()
    open_book(script, window, write_txt(tmp_path))

    # LivroSound layout, the reader open, generation running from the first block with Play gated.
    assert isinstance(window.view, ReflowView) and window.view._mode == "livrosound"
    assert window.pages.currentWidget() is window.reader_page and window.toolbar.isVisible()
    assert window._title_text == "The Long Rain"
    assert script.asked == ["Generate audio from the beginning?"]
    assert window._worker is not None and window._worker.start_block == window.order[0]
    assert not window.capsule.play_button.isEnabled()
    assert window.progress.isVisible() and window.cancel_button.isVisible()
    assert not window.regenerate_action.isEnabled()
    assert window.progress.text() == "Generating 0%"

    # 5% of the book from the start → Play enables with one ding.
    engine.release()
    wait_until(window.capsule.play_button.isEnabled, what="Play to enable")
    engine.park()
    assert len(window.dings) == 1
    assert window.statusBar().currentMessage() == "Ready to play — generation continues in the background"
    assert window.progress.text().startswith("Generating ") and window.progress.value() > 0

    # Play: the position advances and the highlight follows the cue being heard.
    window.toggle_play()
    assert window.player.is_playing() and window.capsule._playing
    play(window, outputs, 2000)  # past the title's lead-in silence
    block, ms = window.player.position()
    assert (block, ms) > (window.order[0], 0)
    assert window._highlight == expected_highlight(window)
    assert window.view._highlight == window._highlight
    window.toggle_play()
    assert not window.player.is_playing()

    # −10 / +10 sec and ⏮ / ⏭ move the audio, the highlight and the view.
    before = window.player.position()
    window.skip(10_000)
    after = window.player.position()
    assert after == window.timeline.skip(*before, 10_000) and after != before
    assert window.view._highlight == expected_highlight(window)
    window.skip(-10_000)
    assert window.player.position() == window.timeline.skip(*after, -10_000)
    here = window.player.position()[0]
    window.step_paragraph(1)
    assert window.player.position() == (window.timeline.next_block(here), 0)
    assert window.view._highlight[0] == window.timeline.next_block(here)
    window.step_paragraph(-1)
    assert window.player.position() == (here, 0)
    assert window.view._highlight == expected_highlight(window)

    # A click on text that has no audio yet asks, then restarts generation there; Play re-gates.
    script.answers["Audio not ready yet."] = True
    chapter_three = window.book.toc[2].block
    far = chapter_three + 4
    assert not window.store.has(far)
    window.view.clicked.emit(far, 10)
    wait_until(lambda: "Audio not ready yet." in script.asked, what="the question")
    assert window.player.position() == (far, 0)
    assert window._highlight[0] == far  # the sentence that will be read first
    assert not window.capsule.play_button.isEnabled()
    assert window._pending_start == far  # the held run stops first
    engine.release()
    wait_until(window.capsule.play_button.isEnabled, what="Play to enable again")
    engine.park()
    assert len(window.dings) == 2
    chapter_two = window.book.toc[1].block
    assert window.store.has(far) and not window.store.has(chapter_two + 4)  # made from the click on
    assert window.player.position()[0] == far

    # Cancel Generation asks, keeps the audio, and Resume Generating takes the bar's place.
    script.answers["Stop generating audio?"] = True
    window.cancel_button.click()
    assert script.asked[-1] == "Stop generating audio?"
    assert window.resume_button.isVisible() and not window.progress.isVisible()
    assert not window.cancel_button.isVisible()
    engine.release()
    wait_until(lambda: window._worker is None, what="the worker to stop")
    made = window.store.generated()
    assert made and window.generator.fraction_done() < 1
    assert window.resume_button.isVisible() and window.regenerate_action.isEnabled()

    window.resume_button.click()
    assert window._worker is not None and not window.resume_button.isVisible()
    wait_until(lambda: window._worker is None, what="generation to finish")
    assert window.generator.fraction_done() == 1
    assert window.export_button.isVisible()
    assert not window.progress.isVisible() and not window.cancel_button.isVisible()
    assert len(window.dings) == 2  # Play was enabled already: no ding
    record = Library.load().get(window.book.book_id)
    assert (record.generated_fraction, record.generation_start) == (1.0, -1)

    # Export MP3: one playable file with the book's chapters.
    script.save_as = str(tmp_path / "The Long Rain.mp3")
    window.export_button.click()
    assert window.progress.isVisible() and window.progress.text().startswith("Exporting")
    wait_until(lambda: window._export is None, what="the export")
    info = ffprobe(Path(script.save_as))
    assert float(info["format"]["duration"]) > 60
    assert [c["tags"]["title"] for c in info["chapters"]] == ["Chapter One", "Chapter Two", "Chapter Three"]
    assert info["format"]["tags"]["title"] == "The Long Rain"
    assert window.statusBar().currentMessage().startswith("Saved “The Long Rain.mp3”")
    assert window.export_button.isVisible()
    assert not script.informed


# ---------- flow 5 "No": pick the start point from Contents ----------


def test_no_to_the_beginning_then_a_contents_pick_starts_there(script, tmp_path):
    window, outputs, engine = script.window()
    script.answers["Generate audio from the beginning?"] = False
    open_book(script, window, write_txt(tmp_path))
    assert window._worker is None and window._pick_mode
    assert window.statusBar().currentMessage().startswith("Click any part of the text, or choose a chapter")
    assert not window.capsule.play_button.isEnabled()
    assert not window.resume_button.isVisible() and not window.export_button.isVisible()

    engine.hold()
    script.answers["Start generating audio from this point?"] = True
    window._open_contents()
    popover = window._contents_popover
    assert [popover.list.item(i).text() for i in range(popover.list.count())] == [
        "Chapter One", "Chapter Two", "Chapter Three"
    ]
    popover.list.itemClicked.emit(popover.list.item(1))  # Chapter Two
    wait_until(lambda: window._worker is not None, what="generation to start")
    chapter_two = window.book.toc[1].block
    assert script.asked[-1] == "Start generating audio from this point?"
    assert window._worker.start_block == chapter_two
    assert window.player.position() == (chapter_two, 0)
    assert not window._pick_mode
    engine.release()
    wait_until(window.capsule.play_button.isEnabled, what="Play to enable")
    assert len(window.dings) == 1
    wait_until(lambda: window._worker is None, what="generation to finish")
    assert window._toc_ready() == [True, True, True]


def test_contents_dots_follow_the_audio(script, tmp_path):
    window, outputs, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    engine.hold()
    open_book(script, window, write_txt(tmp_path))
    window._open_contents()
    popover = window._contents_popover
    dots = lambda: [bool(popover.list.item(i).data(Qt.ItemDataRole.UserRole + 2)) for i in range(3)]  # noqa: E731
    assert dots() == [False, False, False]
    engine.release()
    wait_until(lambda: dots()[0], what="chapter one's dot")  # the open popover updates as audio arrives
    wait_until(lambda: window._worker is None)
    assert dots() == [True, True, True]
    popover.close()


# ---------- flow 9: quit, then resume the book where it was ----------


def test_quit_and_resume_where_you_left_off(script, tmp_path):
    window, outputs, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    engine.hold()
    path = write_txt(tmp_path)
    open_book(script, window, path)
    engine.release()
    wait_until(window.capsule.play_button.isEnabled)
    engine.park()
    window.step_paragraph(1)
    window.step_paragraph(1)
    window.toggle_play()
    play(window, outputs, 2500)
    window.toggle_play()
    saved = window.player.position()
    book_id = window.book.book_id
    assert window._worker is not None  # still generating (held)

    # Quitting while a chunk is still being made: the window goes at once and closes
    # for good when the worker has stopped; finished blocks are kept.
    store = window.store
    window.close()
    assert not window.isVisible() and window._closing and window._went_away
    engine.release()
    wait_until(lambda: window.book is None, what="the window to close for good")
    assert not _threads_running(window) and store._closed
    record = Library.load().get(book_id)
    assert (record.position_block, record.position_ms) == saved
    assert 0 < record.generated_fraction < 1 and record.generation_start >= 0
    assert record.engine == "Fake" and record.voice == "Aaron" and record.speech_options
    assert 0 < record.read_fraction < 1

    # A new session: Go to Library → the book → the same position, highlighted; no ding.
    second, outputs2, engine2 = script.window()
    script.answers["Continue generating audio?"] = True
    engine2.hold()
    pick_from_library(second, book_id)
    wait_until(lambda: second.book is not None and second._task is None, what="the book to reopen")
    assert second.player.position() == saved
    assert second._highlight == expected_highlight(second) and second.view._highlight == second._highlight
    assert second.capsule.play_button.isEnabled()
    assert script.asked[-1] == "Continue generating audio?"
    assert second._worker is not None and second._worker.start_block == saved[0]
    engine2.release()
    wait_until(lambda: second._worker is None, what="generation to finish")
    assert second.generator.fraction_done() == 1 and second.dings == []

    # Uploading the same file again offers to pick up where it was left.
    script.answers["You've opened this book before."] = True
    open_book(script, second, path)
    assert script.asked[-1] == "You've opened this book before."
    assert second.book.book_id == book_id


def test_resume_without_continuing_offers_resume_generating(script, tmp_path):
    window, outputs, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    engine.hold()
    open_book(script, window, write_txt(tmp_path))
    engine.release()
    wait_until(window.capsule.play_button.isEnabled)
    engine.hold()
    book_id = window.book.book_id
    window.close()
    engine.release()
    wait_until(lambda: not _threads_running(window))

    second, _, engine2 = script.window()
    script.answers["Continue generating audio?"] = False
    pick_from_library(second, book_id)
    wait_until(lambda: second.book is not None and second._task is None)
    assert second._worker is None
    assert second.resume_button.isVisible() and not second.export_button.isVisible()
    assert second.capsule.play_button.isEnabled()  # the start has audio


# ---------- flows 3 and 7: layouts ----------


def test_pdf_original_layout_uses_the_page_view_and_aa_swaps_layouts(script, tmp_path):
    window, outputs, engine = script.window()
    script.layout = "original"
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, _converted_ebook(tmp_path))
    assert isinstance(window.view, PageView)
    wait_until(lambda: window._worker is None, what="generation to finish")
    block = window.order[len(window.order) // 2]
    window.player.seek(block, 1500)
    expected = expected_highlight(window)
    assert window._highlight == expected
    assert window.view.highlight_rects()  # a band on the page

    window._open_themes()
    popover = window._themes_popover
    assert popover.layout_buttons["original"].isChecked()
    popover.layout_buttons["livrosound"].click()
    assert isinstance(window.view, ReflowView) and window.view._mode == "livrosound"
    assert window.player.position() == (block, 1500)
    assert window.view._highlight == expected
    assert window.record.layout == "livrosound"
    assert window.page_view.isHidden()

    window._set_layout("original")
    assert window.view is window.page_view and not window.page_view.isHidden()
    assert window.view.highlight_rects() and window._highlight == expected
    popover.close()


def test_epub_in_its_original_layout(script, tmp_path):
    window, outputs, engine = script.window()
    script.layout = "original"
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, make_epub3(tmp_path))
    assert isinstance(window.view, ReflowView) and window.view._mode == "original"
    assert window.book.blocks[window.order[0]].role == "title"
    wait_until(lambda: window._worker is None, what="generation to finish")

    # A line of the book's own contents page is a link: it plays from its chapter.
    line = next(i for i, b in enumerate(window.book.blocks) if b.link >= 0)
    target = window.book.blocks[line].link
    window.view.clicked.emit(line, 0)
    wait_until(window.player.is_playing, what="playback from the linked chapter")
    assert window.player.position()[0] == window.timeline.first_at_or_after(target)
    assert window.view._highlight == expected_highlight(window)


# ---------- flow 8: playback runs into a gap ----------


def _partly_generated(script, tmp_path):
    """A book with audio from the start up to a gap, and no generation running."""
    window, outputs, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    script.answers["Stop generating audio?"] = True
    engine.hold()
    open_book(script, window, write_txt(tmp_path))
    engine.release()
    wait_until(window.capsule.play_button.isEnabled)
    engine.hold()
    window.cancel_generation()
    engine.release()
    wait_until(lambda: window._worker is None)
    order, made = window.order, window.store.generated()
    gap = next(b for b in order if b not in made)
    last = window.timeline.prev_block(gap)
    return window, outputs, engine, gap, last


def test_playback_into_a_gap_asks_for_audio(script, tmp_path):
    window, outputs, engine, gap, last = _partly_generated(script, tmp_path)
    script.answers["Audio not ready yet."] = False
    window.player.seek(last, window.store.durations()[last] - 300)
    window.toggle_play()
    play(window, outputs, 1500)
    wait_until(lambda: "Audio not ready yet." in script.asked, what="the question")
    assert not window.player.is_playing() and window.player.position() == (gap, 0)
    assert window._worker is None

    script.answers["Audio not ready yet."] = True
    engine.hold()
    window.toggle_play()  # Play is enabled? Not at the gap: it asks again via ⏭/clicks instead
    window.view.clicked.emit(gap, 0)
    wait_until(lambda: window._worker is not None, what="generation from the gap")
    assert window._worker.start_block == gap
    engine.release()


def test_playback_waits_for_a_block_generation_is_about_to_make(script, tmp_path):
    window, outputs, engine, gap, last = _partly_generated(script, tmp_path)
    engine.hold()
    window.resume_generation()  # from the position, which is before the gap: the worker makes the gap next
    assert window._worker is not None
    window.player.seek(last, window.store.durations()[last] - 300)
    asked = len(script.asked)
    window.toggle_play()
    play(window, outputs, 1500)
    assert window.player.is_playing() and window.player._waiting == gap
    assert window.statusBar().currentMessage() == "Waiting for audio…"
    assert len(script.asked) == asked  # no question: the audio is coming
    engine.release()
    wait_until(lambda: window.store.has(gap), what="the gap's audio")
    play(window, outputs, 600)
    assert window.player.position()[0] == gap and window.player.position()[1] > 0
    assert window.view._highlight[0] == gap


# ---------- flow 10: Re-Generate Audio ----------


def test_regenerate_asks_before_replacing_the_audio(script, tmp_path):
    window, outputs, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path))
    wait_until(lambda: window._worker is None)
    old_signature = window.store.signature
    made = window.store.generated()

    script.voice = "Ana"
    script.answers["Replace the audio made so far?"] = False
    window.regenerate_action.trigger()
    assert script.asked[-1] == "Replace the audio made so far?"
    assert window.store.generated() == made and window.store.signature == old_signature
    assert window.record.voice == "Aaron"

    script.answers["Replace the audio made so far?"] = True
    script.answers["Generate audio from the beginning?"] = False
    window.regenerate_action.trigger()
    assert script.asked[-2:] == ["Replace the audio made so far?", "Generate audio from the beginning?"]
    assert window.store.generated() == set()
    assert window.record.voice == "Ana"
    assert window.store.signature == audio_signature("Fake", "Ana", window.settings.speech_options())
    assert window.record.audio_signature == window.store.signature
    assert window._pick_mode and not window.export_button.isVisible()
    assert not window.capsule.play_button.isEnabled()


def test_cancelling_the_voice_shows_the_book_without_audio(script, tmp_path):
    window, outputs, engine = script.window()
    script.voice = None
    open_book(script, window, write_txt(tmp_path))
    assert window._worker is None and script.asked == []
    assert window.record.engine == "" and not window.capsule.play_button.isEnabled()

    # The first click on the text chooses the voice, then makes audio from there.
    script.voice = "Aaron"
    engine.hold()
    block = window.book.toc[1].block + 2
    window.view.clicked.emit(block, 0)
    wait_until(lambda: window._worker is not None, what="generation to start")
    assert window._worker.start_block == block and window.record.engine == "Fake"
    assert script.asked == []  # choosing the voice was the answer
    engine.release()


# ---------- keyboard, speed, themes, highlight ----------


def test_keyboard_speed_theme_and_highlight(script, tmp_path):
    window, outputs, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path))
    wait_until(lambda: window._worker is None)

    QTest.keyClick(window.view, Qt.Key.Key_Space)
    assert window.player.is_playing()
    QTest.keyClick(window.view, Qt.Key.Key_Space)
    assert not window.player.is_playing()
    before = window.player.position()
    QTest.keyClick(window.view, Qt.Key.Key_Right)
    assert window.player.position() == window.timeline.skip(*before, 10_000)
    QTest.keyClick(window.view, Qt.Key.Key_Left)
    assert window.player.position() == before

    window.speed_combo.setCurrentIndex(mw.PLAYBACK_SPEEDS.index(1.5))
    assert window.player._rate == 1.5 and window.settings.playback_rate == 1.5

    window._apply_theme("dark")
    assert window.settings.theme == "dark" and window.view._theme.key == "dark"
    size = window.settings.reflow_font_size
    window._step_text_size(1)
    assert window.settings.reflow_font_size == size + 2 and window.view._size == size + 2

    window._on_highlight_toggled(False)
    assert window.view._highlight is None and not window.settings.highlight
    window.skip(10_000)
    assert window.view._highlight is None
    window._on_highlight_toggled(True)
    assert window.view._highlight == expected_highlight(window)


def test_a_file_that_cannot_be_read(script, tmp_path):
    window, _, _ = script.window()
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n")
    script.file = str(empty)
    window.open_book_dialog()
    wait_until(lambda: script.informed and window._task is None, what="the error")
    assert script.informed[0][0] == "This book couldn't be opened."
    assert window.book is None and window.pages.currentWidget() is window.landing


def test_removing_the_open_book_from_the_library_closes_it(script, tmp_path, monkeypatch):
    window, _, _ = script.window()
    script.voice = None
    open_book(script, window, write_txt(tmp_path))
    book_id = window.book.book_id
    title = window.book.title

    window.library_action.trigger()
    assert window.pages.currentWidget() is window.library_page and window.library_page.book_button.isVisible()
    script.answers[f"Remove “{title}” from your library?"] = True
    window.library_page.remove_requested.emit([book_id])
    assert window.book is None and window.pages.currentWidget() is window.library_page
    assert Library.load().get(book_id) is None and not paths.book_dir(book_id).exists()
    assert window.library_page.empty.isVisible() and not window.library_page.book_button.isVisible()


# ---------- Choose a Layout gets real previews ----------


@pytest.mark.parametrize("kind", ["txt", "epub", "pdf"])
def test_layout_dialog_gets_previews_of_the_books_opening(script, tmp_path, monkeypatch, kind):
    window, _, _ = script.window()
    seen = []
    monkeypatch.setattr(dialogs.LayoutDialog, "ask", lambda parent, book, *previews: seen.append(previews))
    script.voice = None
    path = {"txt": write_txt, "epub": make_epub3, "pdf": _converted_ebook}[kind](tmp_path)
    open_book(script, window, path)
    (original, reflow), = seen
    for pixmap in (original, reflow):
        assert pixmap is not None and not pixmap.isNull()
        size = pixmap.deviceIndependentSize()
        assert 200 < size.width() and 200 < size.height()


# ---------- imports that take a while, re-imports, failures ----------


def test_cancelling_a_slow_import_goes_back(script, tmp_path, monkeypatch):
    window, _, _ = script.window()
    started = threading.Event()

    def slow_load(path, *, progress, cancelled):
        started.set()
        for page in range(500):
            if cancelled():
                raise mw.ImportCancelled()
            progress(page, 500, f"Reading page {page + 1} of 500")
            time.sleep(0.01)
        raise AssertionError("never cancelled")

    monkeypatch.setattr(mw, "load_book", slow_load)
    script.file = str(write_txt(tmp_path))
    window.open_book_dialog()
    wait_until(lambda: window.findChild(dialogs.ImportProgressDialog) is not None
               and window.findChild(dialogs.ImportProgressDialog).isVisible(), what="the progress dialog")
    progress = window.findChild(dialogs.ImportProgressDialog)
    wait_until(lambda: progress.message.text().startswith("Reading page"))
    progress.cancel_button.click()
    wait_until(lambda: window._task is None, what="the import to stop")
    wait(20)
    assert window.statusBar().currentMessage() == "Import cancelled."
    assert window.book is None and window.pages.currentWidget() is window.landing
    assert window.findChild(dialogs.ImportProgressDialog) is None or not progress.isVisible()


def test_uploading_the_open_book_again_and_starting_over(script, tmp_path):
    window, _, _ = script.window()
    script.answers["Generate audio from the beginning?"] = True
    path = write_txt(tmp_path)
    open_book(script, window, path)
    wait_until(lambda: window._worker is None)
    first_store = window.store
    made = window.store.generated()

    script.answers["You've opened this book before."] = False  # import it again
    open_book(script, window, path)
    assert first_store._closed and window.store is not first_store
    assert window.store.generated() == made  # same voice and settings: the audio is kept
    assert window.export_button.isVisible()
    assert script.asked.count("Generate audio from the beginning?") == 1  # all of it is ready


class BrokenEngine(HeldEngine):
    def synthesize(self, voice, text):
        if "Clara" in text:
            raise RuntimeError("the model ran out of memory")
        return super().synthesize(voice, text)


def test_an_engine_failure_is_reported_and_the_audio_kept(script, tmp_path):
    window, _, _ = script.window(BrokenEngine())
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path))
    wait_until(lambda: window._worker is None, what="generation to stop")
    title, text = script.informed[-1]
    assert title == "Audio generation stopped."
    assert "the model ran out of memory" in text and "kept" in text
    assert window.store.generated() and window.generator.fraction_done() < 1
    assert window.resume_button.isVisible()


def test_no_sound_output(script, tmp_path):
    engine = HeldEngine()
    window = MainWindow(engine, output_factory=FakeOutputs(can_start=False))
    window._ding = lambda: None
    window.show()
    script.windows.append((window, engine))
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path))
    wait_until(window.capsule.play_button.isEnabled)
    window.toggle_play()
    assert not window.player.is_playing()
    assert script.informed[-1][0] == "The audio can't be played."


# ---------- regressions from the audits ----------


def _closed_for_good(window: MainWindow) -> bool:
    return window._closing and window.book is None and not window._close_timer.isActive()


def _slot_errors(monkeypatch) -> list[str]:
    """Exceptions raised inside Qt slots (they never fail a test on their own)."""
    errors: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, v, tb: errors.append(f"{t.__name__}: {v}"))
    return errors


QUIT_SCRIPT = """
import sys, time
sys.path.insert(0, {root!r})
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from livrosound.ui import dialogs
from livrosound.ui import main_window as mw
from tests.fake_output import FakeOutputs
from tests.fakes import FakeEngine

class SlowEngine(FakeEngine):
    def synthesize(self, voice, text):
        time.sleep(2.6)  # longer than Quit's 2 s wait: a long chunk with Whisper retakes
        return super().synthesize(voice, text)

app = QApplication(sys.argv)
mw.confirm = lambda *a, **k: True
dialogs.LayoutDialog.ask = staticmethod(lambda *a, **k: "livrosound")
dialogs.ChooseVoiceDialog.ask = staticmethod(lambda *a, **k: "Aaron")
dialogs.CustomizeAudioDialog.ask = staticmethod(lambda *a, **k: "generate")
window = mw.MainWindow(SlowEngine(), output_factory=FakeOutputs())
window.show()
QTimer.singleShot(0, lambda: window.open_path(sys.argv[1]))

def quit_once_generating():
    if window._worker is None or not window._worker.isRunning():
        QTimer.singleShot(50, quit_once_generating)
        return
    window.close()
    print("hidden" if not window.isVisible() else "visible", flush=True)

QTimer.singleShot(100, quit_once_generating)
QTimer.singleShot(20000, lambda: (print("STILL RUNNING", flush=True), app.exit(99)))
sys.exit(app.exec())
"""


def test_quit_while_a_chunk_is_made_ends_the_app(tmp_path):
    """Bug 1: Quit hides the window while the chunk finishes; the app must then exit
    (a hidden window's later close doesn't count as the last window closing)."""
    script = tmp_path / "quit.py"
    script.write_text(QUIT_SCRIPT.format(root=str(Path(__file__).resolve().parents[1])))
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "LIVROSOUND_CACHE": str(tmp_path / "cache")}
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(script), str(write_txt(tmp_path))], capture_output=True, text=True, env=env, timeout=60,
    )
    assert "hidden" in result.stdout and "STILL RUNNING" not in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 15


def test_quit_schedules_the_app_to_quit_once_hidden_threads_end(script, tmp_path, monkeypatch, qapp):
    quits = []
    monkeypatch.setattr(qapp, "quit", lambda: quits.append(True))
    window, _, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    engine.hold()
    open_book(script, window, write_txt(tmp_path))
    engine.park()
    window.close()
    assert window._went_away and not window.isVisible() and not quits
    # Hidden, no dialog closing (an import's progress) may count as the last window closing.
    assert not qapp.quitOnLastWindowClosed()
    engine.release()
    wait_until(lambda: _closed_for_good(window) and quits, what="the app to quit")
    assert quits == [True] and qapp.quitOnLastWindowClosed()


def test_removing_the_open_book_closes_it_and_never_brings_it_back(script, tmp_path, monkeypatch):
    """Bug 3: Remove from Library on the open book (playing): it closes, its record stays
    gone, and its files are deleted."""
    window, outputs, _ = script.window()
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path))
    wait_until(lambda: window._worker is None)
    book_id = window.book.book_id
    window.toggle_play()
    play(window, outputs, 1500)

    window.library_action.trigger()
    script.answers[f"Remove “{window.book.title}” from your library?"] = True
    window.library_page.remove_requested.emit([book_id])
    wait(3500)  # the user keeps looking at the library: the 3 s position save would fire
    assert window.book is None and window.pages.currentWidget() is window.library_page
    assert window.library.get(book_id) is None and Library.load().get(book_id) is None
    assert not paths.book_dir(book_id).exists()


def test_removing_the_open_book_while_it_generates(script, tmp_path, monkeypatch):
    """Bug 3: the files go only once the worker writing to them has stopped."""
    window, _, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    engine.hold()
    open_book(script, window, write_txt(tmp_path))
    engine.park()
    book_id = window.book.book_id
    script.answers[f"Remove “{window.book.title}” from your library?"] = True
    window.library_page.remove_requested.emit([book_id])
    assert window.book is None and window._retiring
    assert paths.book_dir(book_id).exists()  # its worker still holds the audio
    engine.release()
    wait_until(lambda: not window._retiring, what="the worker to stop")
    wait(50)
    assert not paths.book_dir(book_id).exists() and Library.load().get(book_id) is None


def test_removing_the_open_book_outside_the_window_is_not_undone(script, tmp_path, monkeypatch):
    """Bug 3: a record removed behind the window's back (the library itself) isn't
    re-saved by the position timer or by arriving audio."""
    window, _, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    engine.delay = 0.05
    open_book(script, window, write_txt(tmp_path))
    book_id = window.book.book_id

    window.library.remove(book_id)
    window.library.save()
    wait(1500)  # blocks keep arriving meanwhile
    window.library_action.trigger()  # the library sees the book is gone and closes it
    assert window.book is None and Library.load().get(book_id) is None


def test_quit_during_an_import_shows_nothing_and_starts_nothing(script, tmp_path, monkeypatch):
    """Bug 4: an import that ends after Quit is dropped: no dialogs, no generation."""
    window, _, _ = script.window()
    real = mw.load_book
    importing = threading.Event()

    def slow_load(path, *, progress, cancelled):
        importing.set()
        book = real(path, progress=progress, cancelled=cancelled)
        time.sleep(2.5)  # writing the cache: doesn't look at cancelled
        return book

    monkeypatch.setattr(mw, "load_book", slow_load)
    layouts = []
    monkeypatch.setattr(dialogs.LayoutDialog, "ask", lambda *a, **k: layouts.append(1) or "livrosound")
    script.file = str(write_txt(tmp_path))
    window.open_book_dialog()
    wait_until(importing.is_set, what="the import to start")
    window.close()
    assert window._went_away
    wait_until(lambda: _closed_for_good(window), what="the window to close for good")
    wait(200)
    assert layouts == [] and script.asked == [] and window._worker is None


def test_quit_while_a_voice_preview_is_made(script, qapp):
    """Bug 5: a preview clip still rendering keeps the window hidden until it's done
    (destroying its running thread at exit aborts the app)."""
    window, _, _ = script.window()
    previews = dialogs.ChooseVoiceDialog._shared_previews(window)

    def slow_render(voice):
        time.sleep(2.5)  # Pocket-TTS's first load
        return Path(__file__)  # never played: Quit stops it

    previews.set_render(slow_render)
    previews.warm(["Aaron"])
    wait_until(previews.rendering)
    window.close()
    assert window._went_away and not window.isVisible() and previews.rendering()
    wait_until(lambda: _closed_for_good(window), what="the window to close for good")
    assert not previews.rendering()


def test_book_switching_waits_for_open_dialogs(script, tmp_path):
    """Bug 6: queued work that switches books waits while a modal dialog is up."""
    window, _, _ = script.window()
    ran = []
    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.show()
    assert QApplication.activeModalWidget() is dialog
    window._when_no_dialog(lambda: ran.append(True))
    wait(300)
    assert ran == []
    dialog.close()
    wait_until(lambda: ran == [True], what="the deferred step")


def test_a_book_switched_under_choose_a_voice_keeps_its_own_voice(script, tmp_path, monkeypatch):
    """Bug 6: B's voice choice (made while A reopened underneath) isn't applied to A."""
    window, _, engine = script.window()
    script.voice = None
    b_path = tmp_path / "b.txt"
    b_path.write_text("SECOND BOOK\n\nby Someone\n\n" + "\n\n".join(
        f"Paragraph {i} of the second book, with a few words to read aloud here." for i in range(40)))
    open_book(script, window, b_path)
    b_id = window.book.book_id
    script.voice = "Aaron"
    script.answers["Generate audio from the beginning?"] = True
    engine.hold()
    open_book(script, window, write_txt(tmp_path))
    a_id = window.book.book_id
    engine.release()
    wait_until(lambda: len(window.store.generated()) > 5)
    engine.park()
    a_made = window.store.generated()
    window.show_library()
    window.library_page.open_requested.emit(b_id)
    wait_until(lambda: window.book is not None and window.book.book_id == b_id and window._task is None)
    script.answers["Continue generating audio?"] = False
    window.show_library()
    window.library_page.open_requested.emit(a_id)  # waits for A's worker to stop

    def choose_while_a_reopens(*args, **kwargs):
        engine.release()
        wait_until(lambda: window.book is not None and window.book.book_id == a_id, what="A to reopen")
        return "Ana"

    monkeypatch.setattr(dialogs.ChooseVoiceDialog, "ask", choose_while_a_reopens)
    engine.hold()
    window.view.clicked.emit(30, 0)  # B's text: B has no voice yet
    wait_until(lambda: window.book.book_id == a_id and window._task is None)
    wait(200)
    assert window.record.voice == "Aaron" and window.store.generated() >= a_made
    assert "Replace the audio made so far?" not in script.asked
    engine.release()


def _book_c(tmp_path: Path) -> Path:
    c = tmp_path / "c.txt"
    paragraphs = (f"Paragraph {i} of the third book, read aloud." for i in range(30))
    c.write_text("THIRD BOOK\n\nby Someone\n\n" + "\n\n".join(paragraphs))
    return c


def test_export_survives_another_book_opening(script, tmp_path, monkeypatch):
    """Bug 7: A's export keeps A's store open while C opens (its open was waiting)."""
    window, _, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path, "a.txt"))
    wait_until(lambda: window._worker is None)
    a_id = window.book.book_id
    engine.hold()
    open_book(script, window, _book_c(tmp_path))
    c_id = window.book.book_id
    engine.park()
    window.show_library()
    window.library_page.open_requested.emit(a_id)
    wait_until(lambda: window.book.book_id == a_id and window._task is None)
    script.answers["Continue generating audio?"] = False
    window.show_library()
    window.library_page.open_requested.emit(c_id)  # waits for C's worker
    window.library_page.back_to_book_requested.emit()  # and meanwhile A is exported
    assert window._pending_open == c_id and window.export_button.isVisible()
    real = export_mod.export_mp3

    def slow_export(book, store, order, path, *, progress, cancelled, **kw):
        return real(book, store, order, path, progress=lambda f: (time.sleep(0.05), progress(f)),
                    cancelled=cancelled, **kw)

    monkeypatch.setattr(export_mod, "export_mp3", slow_export)
    script.save_as = str(tmp_path / "A.mp3")
    window.export_button.click()
    engine.release()
    wait_until(lambda: window._export is None and window._task is None and window.book.book_id == c_id, timeout=30)
    assert Path(script.save_as).exists() and not script.informed
    assert window.store is not None and not window.store._closed


def test_export_survives_an_import_finishing(script, tmp_path, monkeypatch):
    """Bug 7: an import that started before Export MP3 lands while the export runs."""
    window, _, _ = script.window()
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path, "a.txt"))
    wait_until(lambda: window._worker is None)
    real_load, real_export = mw.load_book, export_mod.export_mp3

    def slow_load(path, *, progress, cancelled):
        for _ in range(100):
            if cancelled():
                raise mw.ImportCancelled()
            time.sleep(0.01)
        return real_load(path, progress=progress, cancelled=cancelled)

    def slow_export(book, store, order, path, *, progress, cancelled, **kw):
        return real_export(book, store, order, path, progress=lambda f: (time.sleep(0.08), progress(f)),
                           cancelled=cancelled, **kw)

    monkeypatch.setattr(mw, "load_book", slow_load)
    monkeypatch.setattr(export_mod, "export_mp3", slow_export)
    script.voice = None
    script.file = str(_book_c(tmp_path))
    window.open_book_dialog()
    script.save_as = str(tmp_path / "A.mp3")
    window.export()
    wait_until(lambda: window._export is None and window._task is None, timeout=30)
    assert window.book.title == "Third Book"
    assert Path(script.save_as).exists() and not script.informed


def test_a_moved_pdf_is_read_from_its_new_place(script, tmp_path):
    """Bug 8: reopening a PDF from its new folder shows its pages (not blank ones)."""
    window, _, _ = script.window()
    script.layout = "original"
    script.voice = None
    old = _converted_ebook(tmp_path)
    open_book(script, window, old)
    window.close()
    wait_until(lambda: _closed_for_good(window))
    (tmp_path / "moved").mkdir()
    new = tmp_path / "moved" / old.name
    shutil.move(old, new)

    second, _, _ = script.window()
    script.answers["You've opened this book before."] = True
    open_book(script, second, new)
    assert second.book.source_path == str(new) and isinstance(second.view, PageView)
    wait_until(lambda: second.page_view._cache, what="pages rendered")
    assert Book.load(paths.book_dir(second.book.book_id)).source_path == str(new)


def test_a_missing_pdf_opens_in_the_livrosound_layout_and_says_why(script, tmp_path):
    """Bug 8: a PDF that isn't there any more can't show its pages: say so."""
    window, _, _ = script.window()
    script.layout = "original"
    script.voice = None
    pdf = _converted_ebook(tmp_path)
    open_book(script, window, pdf)
    book_id = window.book.book_id
    window.close()
    wait_until(lambda: _closed_for_good(window))
    pdf.unlink()
    second, _, _ = script.window()
    second.show_library()
    second.library_page.open_requested.emit(book_id)
    wait_until(lambda: second.book is not None and second._task is None)
    wait_until(lambda: script.informed, what="the explanation")
    assert script.informed[-1][0] == "The PDF isn't where it was."
    assert isinstance(second.view, ReflowView) and second.record.layout == "original"


def test_an_unzipped_epub_folder_can_be_uploaded(script, tmp_path):
    """Bug 9: an .epub folder (Finder shows it as one book) opens like the file."""
    epub = make_epub3(tmp_path)
    folder = tmp_path / "Unzipped Book.epub"
    with zipfile.ZipFile(epub) as z:
        z.extractall(folder)
    window, _, _ = script.window()
    script.voice = None
    script.file = str(folder)
    window.open_book_dialog()
    wait_until(lambda: window._task is None and (window.book is not None or script.informed))
    assert window.book is not None and window.book.title == "The Long Night" and not script.informed


def test_asking_twice_for_a_book_that_waits_opens_it_once(script, tmp_path, monkeypatch):
    """Bug 10: while an open waits for a stopping worker, the window is busy."""
    errors = _slot_errors(monkeypatch)
    window, _, engine = script.window()
    script.voice = None
    open_book(script, window, _book_c(tmp_path))
    b_id = window.book.book_id
    script.voice = "Aaron"
    script.answers["Generate audio from the beginning?"] = True
    engine.hold()
    a = write_txt(tmp_path)
    open_book(script, window, a)
    a_id = window.book.book_id
    engine.release()
    wait_until(lambda: len(window.store.generated()) > 3)
    engine.park()
    window.show_library()
    window.library_page.open_requested.emit(b_id)
    wait_until(lambda: window.book.book_id == b_id and window._task is None)
    script.answers["Continue generating audio?"] = False
    window.show_library()
    window.library_page.open_requested.emit(a_id)
    window.show_library()
    window.library_page.open_requested.emit(a_id)  # refused: still opening
    script.file = str(a)
    window.open_book_dialog()  # refused too
    assert len(window._released) == 1 and window.statusBar().currentMessage() == "Still opening a book…"
    engine.release()
    wait_until(lambda: not window._released and window._task is None and window.book.book_id == a_id)
    wait(300)
    assert script.asked.count("Continue generating audio?") == 1 and not errors


def test_audio_arriving_after_quit_touches_nothing(script, tmp_path, monkeypatch):
    """Bug 11: a block finished during Quit's wait finds no book (its store is closed)."""
    errors = _slot_errors(monkeypatch)
    window, _, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    engine.hold()
    open_book(script, window, write_txt(tmp_path))
    engine.park()
    threading.Timer(0.3, engine.release).start()  # the chunk ends while Quit waits
    store = window.store
    window.close()
    wait(200)
    assert _closed_for_good(window) and store._closed and not errors


def test_changing_speed_with_no_book_open(script, tmp_path, monkeypatch):
    """Bug 11 (latent): the player keeps the closed book's store; a speed change mustn't read it."""
    errors = _slot_errors(monkeypatch)
    window, _, _ = script.window()
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path))
    wait_until(lambda: window._worker is None)
    window._close_book()
    window.speed_combo.setCurrentIndex(mw.PLAYBACK_SPEEDS.index(1.5))
    assert not errors and window.settings.playback_rate == 1.5
    open_book(script, window, _book_c(tmp_path))
    assert window.player._rate == 1.5


def test_a_scanned_pdf_is_turned_away_kindly(script, tmp_path, monkeypatch):
    """Scanned books aren't supported: ingest's plain message, under a clear title."""
    def scanned(path, *, progress, cancelled):
        raise mw.BookImportError("This PDF is a scan: its pages are pictures with no text to read.")

    monkeypatch.setattr(mw, "load_book", scanned)
    window, _, _ = script.window()
    script.file = str(_converted_ebook(tmp_path))
    window.open_book_dialog()
    wait_until(lambda: script.informed and window._task is None)
    assert script.informed[-1] == (
        "Scanned books aren't supported yet.", "This PDF is a scan: its pages are pictures with no text to read."
    )
    assert window.book is None


def test_generation_failing_because_the_cache_was_deleted_says_so(script, tmp_path):
    """E: when the book's folder vanished mid-run, its audio is gone: don't say it's kept."""
    class Vanishing(HeldEngine):
        def synthesize(self, voice, text):
            if "Clara" in text:
                shutil.rmtree(paths.book_dir(window.book.book_id), ignore_errors=True)
                raise RuntimeError("disk I/O error")
            return super().synthesize(voice, text)

    window, _, _ = script.window(Vanishing())
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path))
    wait_until(lambda: window._worker is None)
    title, text = script.informed[-1]
    assert title == "Audio generation stopped." and "deleted" in text and "kept" not in text


def test_continuing_where_audio_is_ready_keeps_play_on(script, tmp_path):
    """Audit (e2e): Resume Generating at a sentence with audio doesn't gate Play on 5%."""
    window, outputs, engine = script.window()
    script.answers["Generate audio from the beginning?"] = True
    script.answers["Stop generating audio?"] = True
    engine.hold()
    open_book(script, window, write_txt(tmp_path))
    engine.release()
    wait_until(window.capsule.play_button.isEnabled)
    engine.park()
    window.cancel_generation()
    engine.release()
    wait_until(lambda: window._worker is None)
    ready = max(window.store.generated())
    window.player.seek(ready, 500)
    dings = len(window.dings)
    engine.hold()
    window.resume_button.click()
    assert window._worker is not None and window._gate is None
    assert window.capsule.play_button.isEnabled() and window.capsule.next_button.isEnabled()
    window.toggle_play()
    play(window, outputs, 600)
    assert window.player.is_playing() and window.player.position() > (ready, 500)
    window.toggle_play()
    engine.release()
    wait_until(lambda: window._worker is None)
    assert len(window.dings) == dings


def test_a_heading_lights_up_when_it_is_heard(script, tmp_path):
    """Audit (e2e): during a heading's lead-in silence the last sentence stays lit."""
    window, outputs, _ = script.window()
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path))
    wait_until(lambda: window._worker is None)
    heading = window.book.toc[1].block  # CHAPTER TWO, after chapter one's last paragraph
    last = window.timeline.prev_block(heading)
    window.player.seek(last, window.store.durations()[last] - 400)
    window.toggle_play()
    play(window, outputs, 400)
    lead_in = window.store.info(heading).cues[0].start_ms
    assert lead_in >= 1000
    play(window, outputs, 500)  # inside the heading's silence
    assert window.player.position()[0] == heading and window.player.position()[1] < lead_in
    assert window._highlight[0] == last
    play(window, outputs, lead_in)
    assert window._highlight[0] == heading
    window.toggle_play()


# ---------- the library page ----------


def test_the_library_keeps_the_open_book_and_reads_missing_book_info(script, tmp_path):
    window, _, _ = script.window()
    script.voice = None
    open_book(script, window, write_txt(tmp_path))
    book_id, title = window.book.book_id, window.book.title
    record = window.library.get(book_id)
    assert record.date_added and record.spoken_chars > 0 and record.info_version == bookinfo.INFO_VERSION
    assert record.pages >= 1  # words / 275
    # An older record without the info: Go to Library reads it in the background.
    record.info_version, record.pages, record.spoken_chars = 0, 0, 0
    window.library_toolbar_action.trigger()
    assert window.pages.currentWidget() is window.library_page and window.book is not None
    assert window.library_page.book_button.toolTip() == f"Return to “{title}”"
    wait_until(lambda: window._info_task is None, what="the book info")
    assert record.info_version == bookinfo.INFO_VERSION and record.pages >= 1 and record.spoken_chars > 0
    window.library_page.back_to_book_requested.emit()
    assert window.pages.currentWidget() is window.reader_page
    window.show_library()
    window.library_page.open_requested.emit(book_id)  # the open book: straight back to it
    assert window.pages.currentWidget() is window.reader_page and window.book.book_id == book_id


def test_book_details_save_tags_and_collections(script, tmp_path, monkeypatch):
    window, _, _ = script.window()
    script.voice = None
    open_book(script, window, write_txt(tmp_path))
    book_id = window.book.book_id
    monkeypatch.setattr(dialogs.BookDetailsDialog, "ask", lambda *a, **k: (["classic"], ["Summer"]))
    window.show_library()
    window.library_page.details_requested.emit(book_id)
    record = Library.load().get(book_id)
    assert (record.tags, record.collections) == (["classic"], ["Summer"])


def test_open_last_book_on_start(script, tmp_path):
    window, _, _ = script.window()
    script.voice = None
    open_book(script, window, write_txt(tmp_path))
    book_id = window.book.book_id
    window.settings.open_last_book = True
    window.settings.save()
    window.close()
    wait_until(lambda: not _threads_running(window))
    second, _, _ = script.window()
    script.answers["Continue generating audio?"] = False
    wait_until(lambda: second.book is not None and second._task is None, what="the last book to open")
    assert second.book.book_id == book_id and second.pages.currentWidget() is second.reader_page


def test_books_from_finder_or_dropped_are_imported(script, tmp_path):
    window, _, _ = script.window()
    script.voice = None
    path = write_txt(tmp_path)
    window.settings.auto_import = False
    script.answers["Add “rain.txt” to your library?"] = False
    window.open_external(str(path))
    assert window.book is None and window._task is None
    window.settings.auto_import = True
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path / "notes.xyz")), QUrl.fromLocalFile(str(path))])
    assert window._dropped_book(mime) == str(path)
    window.open_external(str(path))
    wait_until(lambda: window.book is not None and window._task is None, what="the dropped book")
    assert window.book.source_path == str(path)


def test_auto_mode_follows_the_appearance(script, monkeypatch):
    window, _, _ = script.window()
    dark = {"on": False}
    monkeypatch.setattr(mw, "_system_is_dark", lambda: dark["on"])
    window.settings.auto_mode = True
    window.settings.light_theme, window.settings.dark_theme = "calm", "quiet"
    window._follow_appearance()
    assert window.theme.key == "calm"
    dark["on"] = True
    window._follow_appearance()
    assert window.theme.key == "quiet"
    window._apply_theme("dark")  # chosen while macOS is dark: the dark theme from now on
    assert window.settings.dark_theme == "dark" and window.settings.light_theme == "calm"
    window.settings.auto_mode = False
    window._follow_appearance()
    assert window.theme.key == "dark"


# ---------- reading settings ----------


def test_settings_apply_live_and_the_page_line_shows_progress(script, tmp_path):
    window, _, _ = script.window()
    script.voice = None
    open_book(script, window, write_txt(tmp_path))
    view = window.view
    window.settings.always_show_status_bar = True
    window._change_setting("page_view", "single")
    assert view.page_mode() == "single"
    window._count_pages()
    first, last, total = view.page_info()
    assert first == 1 and total >= 1
    assert window.reading_info_text().startswith(f"Page 1 of {total}") and window.reading_info.isVisible()
    window._change_setting("justify", True)
    window._change_setting("font_weight", 600)
    assert view._style.justify and view._style.weight == 600
    window._change_setting("show_page_numbers", False)
    assert window.reading_info_text() == "0%"
    window._change_setting("show_read_percent", False)
    assert window.reading_info_text() == "" and not window.reading_info.isVisible()
    window._change_setting("highlight_unit", "paragraph")
    window._change_setting("theme", "calm")
    assert window.theme.key == "calm"
    window._change_setting("custom_page", "#203040")
    window._change_setting("theme", "custom")
    assert window.theme.key == "custom" and window.theme.page == "#203040"
    assert Library.load() is not None and mw.Settings.load().page_view == "single"


def test_word_and_paragraph_highlighting_follow_the_audio(script, tmp_path):
    window, outputs, _ = script.window()
    script.answers["Generate audio from the beginning?"] = True
    open_book(script, window, write_txt(tmp_path))
    wait_until(lambda: window._worker is None)
    window.settings.highlight_unit = "paragraph"
    window.toggle_play()
    play(window, outputs, 200)
    while cue_at(window.store.info(window.player.position()[0]).cues, window.player.position()[1]) is None:
        play(window, outputs, 100)  # past the title's breath, into words
    play(window, outputs, 200)
    block = window.player.position()[0]
    assert window._highlight == (block, 0, len(window.book.blocks[block].text))
    window._change_setting("highlight_unit", "word")
    play(window, outputs, 100)
    block, start, end = window._highlight
    sentence = expected_highlight(window)
    assert sentence[1] <= start < end <= sentence[2] and " " not in window.book.blocks[block].text[start:end]
