"""The pages without text: audio-generation only and audio player only."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from livrosound.library import BookRecord
from livrosound.ui.audio_pages import ChapterBar, GenerationPage, PlayerPage, clock


def test_clock():
    assert clock(0) == "0:00" and clock(54_000) == "0:54" and clock(2_912_000) == "48:32"
    assert clock(3_725_000) == "1:02:05" and clock(-5) == "0:00"


def test_chapter_bar_segments(qapp):
    bar = ChapterBar()
    bar.resize(300, 14)
    bar.set_state([100, 200, 100], 0.3, 1)
    assert bar.segment_at(10) == 0 and bar.segment_at(150) == 1 and bar.segment_at(299) == 2
    picked = []
    bar.chapter_clicked.connect(picked.append)
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=QPoint(250, 7))
    assert picked == [2]
    bar.grab()  # paints


def test_pages_show_the_book(qapp):
    record = BookRecord("pg", "Bad Behavior", "Mary Gaitskill", "/x", "epub")
    generation = GenerationPage()
    generation.set_book(record, "m4b")
    generation.update_progress(0.37, "About 2 h left", True)
    assert generation.progress.format() == "37%" and generation.eta.text() == "About 2 h left"
    assert generation.after_buttons["m4b"].isChecked()
    generation.update_progress(1.0, "", False)
    assert generation.headline.text() == "All the audio is ready" and generation.stop_button.isHidden()
    player = PlayerPage(["1×", "2×"])
    player.set_book(record, 1)
    player.update_position("Chapter 1: Daisy's Valentine", 54_000, 2_966_000, 26_100_000, [10, 20], 0.1, 0)
    assert player.speed.currentIndex() == 1 and player.book_title.text() == "Bad Behavior"
    assert player.elapsed.text() == "0:54" and player.remaining.text() == "-48:32"
    assert player.left_in_book.text() == "7h 15m left in book" and player.slider.value() == 18
    seeks = []
    player.seek_requested.connect(seeks.append)
    player.slider.setValue(500)
    player.slider.sliderReleased.emit()
    assert seeks == [0.5]
    for page in (generation, player):
        page.resize(900, 700)
        page.grab()
    QApplication.processEvents()
