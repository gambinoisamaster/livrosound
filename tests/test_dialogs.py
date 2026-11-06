"""The dialogs, built offscreen: every answer each one can give, the settings they
change, and how they look in the Original and Dark themes."""

from __future__ import annotations

import threading
import time
import wave
from datetime import date, datetime, timedelta

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from livrosound import paths
from livrosound.book import Block, Book
from livrosound.library import BookRecord, Library, Settings
from livrosound.ui import dialogs as d
from livrosound.ui import theme as themes
from livrosound.ui import widgets as w
from livrosound.ui.theme import THEMES, stylesheet

from .fakes import FakeEngine


@pytest.fixture
def host(qapp):
    """A parent window carrying the app stylesheet, as the main window would."""
    themes.set_current(THEMES["original"])
    widget = QWidget()
    widget.setStyleSheet(stylesheet(THEMES["original"]))
    widget.resize(900, 700)
    yield widget
    widget.close()
    widget.deleteLater()


def _answer(monkeypatch, cls, action):
    """Make cls.exec() run action(dialog) instead of waiting for the user."""

    def fake_exec(self):
        action(self)
        return self.result()

    monkeypatch.setattr(cls, "exec", fake_exec)


def _wait_until(condition, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


# ---------- import progress ----------


def test_import_progress_and_cancel(host):
    dialog = d.ImportProgressDialog(host, "Reading “Artful Sentences”")
    canceled = []
    dialog.canceled.connect(lambda: canceled.append(True))
    assert dialog.bar.maximum() == 0  # busy until there's a count
    dialog.set_progress(12, 316, "Reading page 12 of 316…")
    assert (dialog.bar.value(), dialog.bar.maximum()) == (12, 316)
    assert dialog.message.text() == "Reading page 12 of 316…"
    dialog.set_progress(13, 316, "")
    assert dialog.message.text() == "13 of 316"

    dialog.show()
    dialog.cancel_button.click()
    dialog.reject()  # Esc as well: still one request
    assert canceled == [True]
    assert dialog.is_canceling() and not dialog.cancel_button.isEnabled()
    assert dialog.isVisible()  # waits for the import to stop
    dialog.set_progress(14, 316, "Reading page 14 of 316…")
    assert dialog.message.text() == "Canceling…"
    dialog.finish()
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Accepted


# ---------- a book for the dialogs ----------

PARAGRAPH = (
    "1801.—I have just returned from a visit to my landlord—the solitary neighbour that I shall be "
    "troubled with. This is certainly a beautiful country!"
)


def _book(kind: str) -> Book:
    return Book(
        book_id="wh",
        title="Wuthering Heights",
        author="Emily Brontë",
        source_path=f"/x/wh.{kind}",
        source_kind=kind,
        blocks=[
            Block("heading", "Wuthering Heights", level=1, role="title"),
            Block("heading", "Contents", level=1, role="contents"),
            Block("paragraph", "A copyright line, long enough to be a paragraph but skipped.", role="copyright"),
            Block("heading", "Chapter I", level=1),
            Block("paragraph", "Short."),
            Block("paragraph", PARAGRAPH),
        ],
    )


# ---------- Choose a Voice ----------


class NamedEngine(FakeEngine):
    def __init__(self, name, voices=("Aaron", "Ana", "Emily"), ok=True, why=""):
        super().__init__(voices)
        self.name = name
        self.install_hint = "Install with: uv sync"
        self._ok, self._why = ok, why

    def available(self):
        return self._ok, self._why


def _wav(path):
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(24000)
        f.writeframes(b"\0\0" * 240)
    return path


@pytest.fixture
def renders(tmp_path):
    """A preview render that makes a tiny WAV and records (engine, voice)."""
    calls = []

    def render(engine, voice):
        calls.append((engine.name, voice))
        return _wav(tmp_path / f"{engine.name}-{voice}.wav")

    render.calls = calls
    return render


def _picker_rows(dialog) -> list:
    picker = dialog.picker
    picker.resize(320, 40)
    menu = picker.open_menu()
    rows = [menu.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(menu.list.count())]
    menu.close()
    return rows


def test_choose_voice_layout(host, renders):
    dialog = d.ChooseVoiceDialog(host, NamedEngine("Pocket-TTS"), "Aaron", renders)
    assert dialog.windowTitle() == "LivroSound | Choose a Voice"
    assert dialog.title_label.text() == "Choose a Voice"
    assert dialog.subtitle_label.text() == "Pick the narrator who will read your book aloud."
    assert dialog.choice() == "Aaron" and dialog.continue_button.isEnabled()
    assert dialog.note.isHidden()
    # Grouped like voices.grouped(), only the engine's voices: Aaron and Ana first, then Women, Men.
    assert _picker_rows(dialog) == [None, "Aaron", "Ana", None, "Emily"]
    assert _wait_until(lambda: ("Pocket-TTS", "Aaron") in renders.calls)  # warmed
    dialog.picker.set_current("Emily")
    assert dialog.choice() == "Emily"
    dialog.previews.toggle("Emily")
    assert _wait_until(lambda: ("Pocket-TTS", "Emily") in renders.calls)
    dialog.done(QDialog.DialogCode.Rejected)
    assert dialog.previews.state("Emily") == "idle"
    dialog.previews.shutdown()


def test_choose_voice_unknown_voice_falls_back(host, renders):
    dialog = d.ChooseVoiceDialog(host, NamedEngine("Pocket-TTS", voices=("Ana", "Emily")), "Ronald", renders)
    assert dialog.choice() == "Ana"  # no Aaron (the default) either: the first voice listed
    dialog.done(0)
    dialog.previews.shutdown()


def test_choose_voice_engine_unavailable(host, renders):
    why = "Pocket-TTS is not installed (torch missing). Install with: uv sync"
    dialog = d.ChooseVoiceDialog(host, NamedEngine("Pocket-TTS", ok=False, why=why), "Aaron", renders)
    assert dialog.choice() is None and not dialog.continue_button.isEnabled()
    assert not dialog.picker.isEnabled()
    assert not dialog.note.isHidden() and dialog.note.text() == why
    assert renders.calls == []  # nothing to preview with
    dialog.done(0)
    dialog.previews.shutdown()
    dialog = d.ChooseVoiceDialog(host, NamedEngine("Pocket-TTS", ok=False), "Aaron", renders)
    assert dialog.note.text() == "Install with: uv sync"  # no reason given: the install hint
    dialog.done(0)
    dialog.previews.shutdown()


def test_choose_voice_without_voices_cannot_continue(host, renders):
    dialog = d.ChooseVoiceDialog(host, NamedEngine("Pocket-TTS", voices=()), "Aaron", renders)
    assert dialog.choice() is None
    assert not dialog.continue_button.isEnabled()
    dialog.done(0)
    dialog.previews.shutdown()


def test_choose_voice_ask_and_shared_previews(host, monkeypatch, renders):
    engine = NamedEngine("Pocket-TTS")
    seen = []

    def pick(dlg):
        seen.append(dlg.previews)
        dlg.picker.set_current("Emily")
        dlg.continue_button.click()

    _answer(monkeypatch, d.ChooseVoiceDialog, pick)
    paused = []
    assert d.ChooseVoiceDialog.ask(host, engine, "Aaron", renders, before_play=lambda: paused.append(True)) == "Emily"
    _answer(monkeypatch, d.ChooseVoiceDialog, lambda dlg: (seen.append(dlg.previews), dlg.cancel_button.click()))
    assert d.ChooseVoiceDialog.ask(host, engine, "Aaron", renders) is None
    # One VoicePreviews per window, outliving each dialog.
    assert seen[0] is seen[1]
    assert host.findChildren(w.VoicePreviews) == [seen[0]]
    seen[0].shutdown()


def test_previews_survive_a_late_finished_signal(host, tmp_path):
    """A replaced worker's late "finished" must not drop the worker now running
    (plain widgets.VoicePreviews does, and Qt aborts the app)."""
    gate = threading.Event()

    def render(voice):
        gate.wait(5)
        return _wav(tmp_path / f"{voice}.wav")

    previews = d.ChooseVoiceDialog._shared_previews(host)
    previews.set_render(render)
    previews.warm(["Ana", "Emily"])  # the worker waits on Ana; Emily is pending
    running = previews._worker

    class OldWorker(QObject):
        finished = Signal()

    old = OldWorker()
    old.finished.connect(previews._on_worker_finished)
    old.finished.emit()
    assert previews._worker is running and running.isRunning()
    gate.set()
    assert _wait_until(lambda: previews.is_ready("Ana") and previews.is_ready("Emily"))
    previews.shutdown()


# ---------- Customize Audio ----------


def test_customize_toggles_save_settings(host):
    settings = Settings()
    dialog = d.CustomizeAudioDialog(host, settings, (True, ""))
    assert dialog.windowTitle() == "LivroSound | Customize Audio"
    assert dialog.title_label.text() == "Customize Audio"
    assert dialog.subtitle_label.text() == "Fine-tune the reading, then press Generate Audio to start."
    labels = [s.parent().label.text() for s in dialog.switches.values()]
    assert labels == ["Highlight sentences during playback", "Read chapter titles", "Read footnotes",
                      "Check every sentence with Whisper (slower)"]
    switches = dialog.switches
    assert switches["highlight"].isChecked() and switches["read_headings"].isChecked()
    assert not switches["read_footnotes"].isChecked() and not switches["validate_audio"].isChecked()

    switches["highlight"].click()
    switches["read_headings"].click()
    switches["read_footnotes"].click()
    switches["validate_audio"].click()
    assert (settings.highlight, settings.read_headings, settings.read_footnotes, settings.validate_audio) == (
        False, False, True, True)
    saved = Settings.load()
    assert (saved.highlight, saved.read_headings, saved.read_footnotes, saved.validate_audio) == (
        False, False, True, True)

    assert dialog.pause_label.text() == "Pause between paragraphs: 0.6 s"
    assert (dialog.pause_slider.minimum(), dialog.pause_slider.maximum()) == (2, 20)
    dialog.pause_slider.setValue(14)
    assert dialog.pause_label.text() == "Pause between paragraphs: 1.4 s"
    assert settings.paragraph_pause == pytest.approx(1.4)
    assert Settings.load().paragraph_pause == pytest.approx(1.4)
    assert settings.speech_options().paragraph_pause_ms == 1400


def test_customize_whisper_unavailable(host):
    why = "Whisper is not installed. Install with: uv sync --extra validate"
    settings = Settings(validate_audio=True, paragraph_pause=5.0)
    dialog = d.CustomizeAudioDialog(host, settings, (False, why))
    switch = dialog.switches["validate_audio"]
    assert not switch.isEnabled() and not switch.isChecked()
    row = switch.parent()
    assert row.hint.text() == why and not row.hint.isHidden()
    assert dialog.pause_slider.value() == 20  # clamped to 2.0 s


def test_customize_ask(host, monkeypatch):
    settings = Settings()
    _answer(monkeypatch, d.CustomizeAudioDialog, lambda dlg: dlg.generate_button.click())
    assert d.CustomizeAudioDialog.ask(host, settings, (True, "")) == "generate"
    _answer(monkeypatch, d.CustomizeAudioDialog, lambda dlg: dlg.back_button.click())
    assert d.CustomizeAudioDialog.ask(host, settings, (True, "")) == "back"
    _answer(monkeypatch, d.CustomizeAudioDialog, lambda dlg: dlg.reject())  # Esc / close
    assert d.CustomizeAudioDialog.ask(host, settings, (True, "")) == "cancel"
    assert d.CustomizeAudioDialog(host, settings, (True, "")).generate_button.isDefault()


# ---------- Your Books ----------


def _shelve(library: Library, book_id: str, title: str, *, days_ago: int, position: int = 0, audio: float = 0.0,
            kind: str = "epub") -> None:
    blocks = [Block("heading", "Contents", role="contents")] + [Block("paragraph", "x" * 100) for _ in range(100)]
    Book(book_id, title, "An Author", f"/x/{book_id}.{kind}", kind, blocks=blocks).save(paths.book_dir(book_id))
    opened = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    library.upsert(BookRecord(book_id, title, "An Author", f"/x/{book_id}.{kind}", kind, position_block=position,
                              generated_fraction=audio, last_opened=opened))


@pytest.fixture
def library():
    lib = Library()
    _shelve(lib, "old", "The Trial", days_ago=30, position=72, audio=1.0, kind="docx")
    _shelve(lib, "new", "Anna Karenina", days_ago=0, position=0, audio=0.12)
    _shelve(lib, "mid", "Artful Sentences", days_ago=1, position=35, audio=0.0, kind="pdf")
    lib.save()
    return lib


def test_book_details_shows_what_is_known_and_edits_tags_and_collections(host, library, monkeypatch):
    record = library.get("old")
    record.pages, record.series, record.series_index = 212, "The Collected Kafka", 2
    record.subjects, record.tags, record.collections = ["Fiction", "Law"], ["classic"], []
    record.read_fraction, record.spoken_chars, record.voice = 0.71, 10_000, "Aaron"
    dialog = d.BookDetailsDialog(host, record, ["classic", "to read"], ["Summer"])
    assert dialog.title_label.text() == "The Trial"
    rows = dict(d.detail_rows(record, date.today()))
    assert rows["Format"] == "DOCX" and rows["Pages"] == "212"
    assert rows["Series"] == "The Collected Kafka, book 2" and rows["Subjects"] == "Fiction, Law"
    assert rows["Progress"].startswith("71% read · about") and rows["Audio"] == "100% made with Aaron"
    assert "Published" not in rows  # unknown: left out
    assert dialog.tags.text() == "classic"
    dialog.tags.setText("classic,  to read , Classic")
    dialog.collections.setText("Summer")
    assert dialog.names() == (["classic", "to read"], ["Summer"])
    _answer(monkeypatch, d.BookDetailsDialog, lambda dlg: (dlg.tags.setText("new"), dlg.save_button.click()))
    assert d.BookDetailsDialog.ask(host, record, [], []) == (["new"], [])
    _answer(monkeypatch, d.BookDetailsDialog, lambda dlg: dlg.cancel_button.click())
    assert d.BookDetailsDialog.ask(host, record, [], []) is None


def test_opened_text():
    today = date(2026, 10, 23)
    assert d.opened_text("2026-10-23T09:15:00", today) == "today"
    assert d.opened_text("2026-10-22T23:59:59", today) == "yesterday"
    assert d.opened_text("2026-10-02T10:00:00", today) == "Oct 2"
    assert d.opened_text("2025-12-31T10:00:00", today) == "Dec 31, 2025"
    assert d.opened_text("", today) == ""


# ---------- every dialog in both themes ----------


@pytest.mark.parametrize("key", ["original", "dark"])
def test_every_dialog_paints(host, key, library, renders):
    themes.set_current(THEMES[key])
    host.setStyleSheet(stylesheet(THEMES[key]))
    progress = d.ImportProgressDialog(host, "Reading “Artful Sentences”")
    progress.set_progress(12, 316, "Reading page 12 of 316…")
    voice = d.ChooseVoiceDialog(host, NamedEngine("Pocket-TTS"), "Ana", renders)
    dialogs = [
        progress,
        voice,
        d.CustomizeAudioDialog(host, Settings(), (False, "Whisper is not installed.")),
        d.BookDetailsDialog(host, library.get("mid"), [], []),
    ]
    for dialog in dialogs:
        dialog.show()
        QApplication.processEvents()
        image = dialog.grab().toImage()
        assert not image.isNull()
        # The dialog takes the theme's window color from the main window's stylesheet.
        assert image.pixelColor(QPoint(3, 3)).name().upper() == THEMES[key].window.upper()
        dialog.done(0)
    voice.previews.shutdown()
    themes.set_current(THEMES["original"])


# ---------- Settings ----------


def test_settings_tabs_change_settings_live(host):
    settings = Settings()
    changes = []
    dialog = d.SettingsDialog(host, settings, add_font=lambda: [], pick_color=lambda current, title: "#102030")
    dialog.changed.connect(changes.append)
    assert [b.text() for b in dialog.tab_buttons.values()] == list(d.SettingsDialog.TABS)
    dialog.show_tab("Layout")
    assert dialog.pages.currentIndex() == 1 and not dialog.override_box.isHidden()
    assert dialog.default_box.isHidden()  # no book open: these are the defaults
    assert not dialog.controls["line_spacing"].isEnabled()  # until Override book layout is on
    dialog.override_switch.setChecked(True)
    assert settings.override_layout and changes[-1] == "override" and dialog.controls["line_spacing"].isEnabled()
    dialog.controls["line_spacing"].setValue(20)
    assert settings.line_spacing == 2.0 and changes[-1] == "line_spacing"
    dialog.controls["word_spacing"].setValue(3)
    assert settings.word_spacing == 3.0
    dialog.controls["justify"].setChecked(True)
    assert settings.justify
    # Margins: centered (at the text width) until the switch is turned off, then the sliders set them.
    assert not dialog.controls["margin_left"].isEnabled() and dialog.controls["text_width"].isEnabled()
    dialog.controls["text_width"].setValue(70)
    assert settings.text_width == 70
    dialog.controls["centered"].setChecked(False)
    assert settings.margin_left == 64 and dialog.controls["margin_left"].isEnabled()
    assert not dialog.controls["text_width"].isEnabled()
    dialog.controls["margin_left"].setValue(50)
    assert settings.margin_left == 200
    dialog.controls["centered"].setChecked(True)
    assert (settings.margin_left, settings.margin_right) == (-1, -1)
    dialog.controls["font_weight"].setValue(6)
    assert settings.font_weight == 600
    dialog.font_combo.addItem("Courier", "Courier")
    dialog.font_combo.setCurrentIndex(dialog.font_combo.count() - 1)
    assert settings.font_family == "Courier"
    dialog.theme_cards["calm"].click()
    assert settings.theme == "calm"
    dialog._choose_color("custom_page", "Paper…")
    assert settings.custom_page == "#102030"
    dialog.show_tab("Color")
    assert dialog.override_box.isHidden()
    page_buttons = dialog.controls["page_view"].findChildren(d.QToolButton)
    next(b for b in page_buttons if b.text() == "Double").click()
    assert settings.page_view == "double"
    unit = next(b for b in dialog.controls["highlight_unit"].findChildren(d.QToolButton) if b.text() == "Word")
    unit.click()
    assert settings.highlight_unit == "word"
    for field in ("open_last_book", "auto_import", "always_show_status_bar", "show_page_numbers"):
        dialog.controls[field].toggle()
    assert settings.open_last_book and not settings.auto_import and settings.always_show_status_bar
    assert not settings.show_page_numbers
    assert {"page_view", "highlight_unit", "custom_page", "theme"} <= set(changes)


def test_custom_theme_colors(qapp):
    custom = themes.set_custom_colors("#1E2A20", "#E8F0E0")
    assert themes.theme_for("custom") is custom and custom.dark and custom.page == "#1e2a20"
    light = themes.set_custom_colors("#FAF0DC", "#202020")
    assert not light.dark and light.text == "#202020" and light.highlight != light.page
    themes.set_custom_colors("not a color", "#000000")
    assert themes.theme_for("custom") is light


def test_generate_audio_dialog_asks_about_exporting(host, monkeypatch):
    dialog = d.GenerateAudioDialog(host, "m4b", "/music", choose_folder=lambda current: "/books")
    assert dialog.title_label.text() == "Generate audio from the beginning?"
    assert dialog.export_row.switch.isChecked() and dialog.format_buttons["m4b"].isChecked()
    assert dialog.plan() == {"format": "m4b", "folder": "/music"}
    dialog.format_buttons["mp3"].click()
    dialog.folder_row.change()
    assert dialog.plan() == {"format": "mp3", "folder": "/books"} and "/books" in dialog.folder_row.label.text()
    dialog.export_row.switch.setChecked(False)
    assert dialog.plan() == {} and not dialog.format_row.isEnabled()
    _answer(monkeypatch, d.GenerateAudioDialog, lambda dlg: dlg.yes_button.click())
    assert d.GenerateAudioDialog.ask(host, "m4b", "/m") == (True, {"format": "m4b", "folder": "/m"})
    _answer(monkeypatch, d.GenerateAudioDialog, lambda dlg: dlg.no_button.click())
    assert d.GenerateAudioDialog.ask(host, "mp3", "/m") == (False, {"format": "mp3", "folder": "/m"})


def test_export_audio_dialog(host, monkeypatch):
    rows = [("Chapter One", "12 min", True), ("Chapter Two", "15 min", False), ("Chapter Three", "9 min", False)]
    dialog = d.ExportAudioDialog(host, rows, "m4b", "/music", book_ready=False, eta=lambda i: "about 2 h",
                                 choose_folder=lambda current: "/elsewhere")
    assert dialog.mode == "book" and dialog.card.isHidden() and dialog.format_buttons["m4b"].isChecked()
    assert dialog.export_button.text() == "Generate && Export" and "about 2 h" in dialog.summary.text()
    dialog.mode_buttons["chapters"].click()
    assert dialog.mode == "chapters" and not dialog.card.isHidden() and not dialog.export_button.isEnabled()
    dialog.list.item(0).setCheckState(Qt.CheckState.Checked)
    assert dialog.export_button.text() == "Export" and "ready to save" in dialog.summary.text()
    dialog.list.item(1).setCheckState(Qt.CheckState.Checked)
    assert dialog.export_button.text() == "Generate && Export"
    dialog.all_button.click()
    assert dialog.chosen() == [0, 1, 2]
    _answer(monkeypatch, d.ExportAudioDialog, lambda dlg: (dlg.format_buttons["mp3"].click(), dlg.folder_row.change(),
                                                          dlg.export_button.click()))
    assert d.ExportAudioDialog.ask(host, rows, "m4b", "/m", choose_folder=lambda c: "/x") == ("book", [], "mp3", "/x")
    _answer(monkeypatch, d.ExportAudioDialog, lambda dlg: dlg.cancel_button.click())
    assert d.ExportAudioDialog.ask(host, rows, "m4b", "/m") is None

def test_settings_for_one_book_or_as_the_default(host):
    from livrosound.library import StyleStore

    settings = Settings()
    record = BookRecord("bk", "A Book", "", "/x/a.epub", "epub")
    styles = StyleStore(settings, record)
    dialog = d.SettingsDialog(host, settings, styles=styles)
    assert not dialog.default_box.isHidden() and not dialog.default_box.isChecked()
    dialog.override_switch.setChecked(True)
    assert record.layout == "livrosound" and not settings.override_layout  # this book only
    dialog.controls["line_spacing"].setValue(20)
    assert record.style == {"line_spacing": 2.0} and settings.line_spacing == 1.5
    dialog.default_box.setChecked(True)  # Set as default for all books
    assert settings.override_layout and settings.line_spacing == 2.0
    assert record.style == {} and record.layout == ""  # the book follows the defaults now
    dialog.controls["word_spacing"].setValue(4)
    assert settings.word_spacing == 4.0 and record.style == {}
    other = BookRecord("b2", "Another", "", "/x/b.epub", "epub", layout="original")
    assert not StyleStore(settings, other).overridden()  # a book with its own choice keeps it
    assert StyleStore(settings, BookRecord("b3", "New", "", "/x/c.epub", "epub")).overridden()
