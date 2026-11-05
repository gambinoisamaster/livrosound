"""The custom widgets, built offscreen: their signals, their order, their states."""

from __future__ import annotations

import threading
import time
import wave

import pytest
import shiboken6
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QWidget

from livrosound.ui import theme as themes
from livrosound.ui import widgets as w
from livrosound.ui.landing import LivroSoundLanding
from livrosound.ui.theme import THEMES, stylesheet

GROUPS = [("Women", ["Emily", "Ana", "Gertrude"]), ("Men", ["Kyle", "Aaron", "Ronald"])]


def _wait_until(condition, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def _closed(popover) -> bool:
    """Popovers delete themselves when they close."""
    return not shiboken6.isValid(popover) or not popover.isVisible()


def _recorder(signal) -> list:
    seen = []
    signal.connect(lambda *args: seen.append(args[0] if len(args) == 1 else args))
    return seen


@pytest.fixture
def host(qapp):
    """A parent window carrying the app stylesheet, as the main window would."""
    themes.set_current(THEMES["original"])
    widget = QWidget()
    widget.setStyleSheet(stylesheet(THEMES["original"]))
    widget.resize(600, 400)
    yield widget
    widget.close()
    widget.deleteLater()


# ---------- Themes & Settings ----------


def test_themes_popover_reports_every_choice(host):
    pop = w.ThemesPopover(host, "original", layout="original", highlight=True)
    themes_seen = _recorder(pop.theme_chosen)
    sizes = _recorder(pop.text_size_step)
    layouts = _recorder(pop.layout_chosen)
    highlights = _recorder(pop.highlight_toggled)

    assert pop.cards["original"].isChecked()
    assert pop.layout_buttons["original"].isChecked() and not pop.layout_buttons["livrosound"].isChecked()
    assert pop.highlight_switch.isChecked()

    pop.larger.click()
    pop.smaller.click()
    assert sizes == [1, -1]

    pop.cards["calm"].click()
    pop.appearance.click()  # the light/dark partner of the shown theme
    assert themes_seen == ["calm", THEMES["original"].partner]

    pop.layout_buttons["livrosound"].click()
    pop.layout_buttons["livrosound"].click()  # already chosen: nothing new
    pop.layout_buttons["original"].click()
    assert layouts == ["livrosound", "original"]

    pop.highlight_switch.click()
    assert highlights == [False]


def test_themes_popover_setters_do_not_signal(host):
    pop = w.ThemesPopover(host, "dark", layout="livrosound", highlight=False)
    layouts = _recorder(pop.layout_chosen)
    highlights = _recorder(pop.highlight_toggled)
    assert not pop.highlight_switch.isChecked()
    pop.set_layout("original")
    pop.set_highlight(True)
    pop.set_theme("quiet")
    assert pop.layout_buttons["original"].isChecked()
    assert pop.highlight_switch.isChecked()
    assert pop.cards["quiet"].isChecked() and not pop.cards["dark"].isChecked()
    assert layouts == [] and highlights == []
    # After set_layout, choosing the other layout is a change again.
    pop.layout_buttons["livrosound"].click()
    assert layouts == ["livrosound"]


# ---------- Contents ----------

ENTRIES = [
    ("Preface", 1, True),
    ("Chapter One: Short Sentences", 1, True),
    ("The Single Line", 2, False),
    ("Chapter Two: Long Sentences, With a Title Long Enough to Wrap Onto Another Line in the Popover", 1, False),
]


def test_contents_popover_rows_are_links(host):
    pop = w.ContentsPopover(host, ENTRIES, current=1)
    picked = _recorder(pop.picked)
    pop.show()
    QApplication.processEvents()
    assert pop.list.count() == len(ENTRIES)
    assert pop.list.currentRow() == 1
    assert pop.list.viewport().cursor().shape() == Qt.CursorShape.PointingHandCursor
    heights = [pop.list.item(i).sizeHint().height() for i in range(pop.list.count())]
    assert heights[3] > heights[0]  # the long title wraps
    ready = [pop.list.item(i).data(w._READY_ROLE) for i in range(pop.list.count())]
    assert ready == [True, True, False, False]

    rect = pop.list.visualItemRect(pop.list.item(2))
    QTest.mouseClick(pop.list.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    assert picked == [2]
    assert _wait_until(lambda: _closed(pop))


def test_contents_popover_paints_every_theme(host):
    for key in THEMES:
        themes.set_current(THEMES[key])
        pop = w.ContentsPopover(host, ENTRIES, current=0)
        pop.show()
        assert not pop.grab().isNull()
        pop.close()
    themes.set_current(THEMES["original"])


def test_contents_popover_without_a_toc(host):
    pop = w.ContentsPopover(host, [], current=None)
    pop.show()
    QApplication.processEvents()
    assert pop.list.isHidden()
    assert any("no table of contents" in label.text() for label in pop.findChildren(w.QLabel))


# ---------- voices ----------


def _wav(path, seconds=0.05):
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(24000)
        f.writeframes(b"\x00\x00" * int(24000 * seconds))
    return path


@pytest.fixture
def previews(qapp, tmp_path):
    rendered = []

    def render(voice):
        rendered.append(voice)
        return _wav(tmp_path / f"{voice}.wav")

    p = w.VoicePreviews(render)
    p.player.play = lambda: None  # no sound card needed
    p.rendered = rendered
    yield p
    p.shutdown()


def test_voice_preview_renders_then_plays(previews):
    states = _recorder(previews.state_changed)
    paused = []
    previews.before_play = lambda: paused.append(True)
    previews.toggle("Ana")
    assert previews.state("Ana") == "loading"
    assert _wait_until(lambda: previews.state("Ana") == "playing")
    assert states == [("Ana", "loading"), ("Ana", "playing")]
    assert paused == [True]
    assert previews.is_ready("Ana")
    previews.toggle("Ana")  # the same voice again stops it
    assert previews.state("Ana") == "idle"
    previews.toggle("Ana")  # cached: plays at once
    assert previews.state("Ana") == "playing"
    assert previews.rendered == ["Ana"]


def test_voice_preview_warm_and_failures(qapp, previews):
    previews.warm(["Emily", "Kyle"])
    assert _wait_until(lambda: previews.is_ready("Emily") and previews.is_ready("Kyle"))

    def broken(voice):
        raise RuntimeError("model missing")

    previews.set_render(broken)
    assert not previews.is_ready("Emily")  # another engine's clips
    errors = _recorder(previews.failed)
    previews.toggle("Emily")
    assert _wait_until(lambda: errors)
    assert "Emily" in errors[0] and "model missing" in errors[0]
    assert previews.state("Emily") == "idle"


def test_voice_preview_drops_clips_from_a_replaced_render(qapp, tmp_path):
    def slow(voice):
        time.sleep(0.2)
        return _wav(tmp_path / f"old-{voice}.wav")

    previews = w.VoicePreviews(slow)
    previews.player.play = lambda: None
    previews.toggle("Ana")
    previews.set_render(lambda voice: _wav(tmp_path / f"new-{voice}.wav"))
    _wait_until(lambda: False, timeout=0.5)  # let the old render finish
    assert not previews.is_ready("Ana")
    assert previews.state("Ana") == "idle"
    previews.shutdown()


def test_voice_picker_choices(host, previews):
    picker = w.VoicePicker(GROUPS, "Aaron", previews, host)
    changed = _recorder(picker.changed)
    assert picker.currentText() == "Aaron"
    picker.set_current("Nobody")
    picker.set_current("Aaron")  # unchanged
    picker.set_current("Gertrude")
    assert changed == ["Gertrude"]
    assert picker.currentText() == "Gertrude"
    picker.edit.setText("ana")
    picker.edit.editingFinished.emit()
    assert picker.currentText() == "Ana"
    picker.edit.setText("gibberish")
    picker.edit.editingFinished.emit()
    assert picker.edit.text() == "Ana"
    assert changed == ["Gertrude", "Ana"]
    assert w.VoicePicker(GROUPS, "Unknown", previews, host).currentText() == "Emily"


def test_voice_menu_groups_under_headers(host, previews):
    picker = w.VoicePicker(GROUPS + [("Other", [])], "Aaron", previews, host)
    picker.resize(320, 40)
    menu = picker.open_menu()
    QApplication.processEvents()
    lst = menu.list
    rows = [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count())]
    assert rows == [None, "Emily", "Ana", "Gertrude", None, "Kyle", "Aaron", "Ronald"]  # no empty "Other"
    headers = [lst.itemWidget(lst.item(i)).text() for i in (0, 4)]
    assert headers == ["Women", "Men"]
    assert lst.item(0).flags() == Qt.ItemFlag.NoItemFlags
    assert lst.currentItem().data(Qt.ItemDataRole.UserRole) == "Aaron"

    # Every row's ▶ is fern like the picker's own, not a black outline.
    play = lst.itemWidget(lst.item(1)).findChild(w.PreviewButton)
    image = play.grab().toImage()
    edge = image.pixelColor(round(4 * image.devicePixelRatio()), image.height() // 2)
    assert edge.name().upper() == w.ACCENT.upper()

    lst.itemClicked.emit(lst.item(0))  # a header does nothing
    assert menu.isVisible()
    lst.itemClicked.emit(lst.item(7))
    assert picker.currentText() == "Ronald"
    assert _wait_until(lambda: _closed(menu))


def test_previews_shutdown_without_waiting(qapp, tmp_path):
    """Quitting mustn't block on a clip that takes long to make (the first Pocket-TTS load):
    shutdown(wait=False) returns at once, rendering() says a thread still runs, wait() ends it."""
    started = threading.Event()

    def slow(voice):
        started.set()
        time.sleep(0.6)
        return _wav(tmp_path / f"{voice}.wav")

    previews = w.VoicePreviews(slow)
    previews.player.play = lambda: None
    previews.warm(["Aaron", "Ana"])
    assert started.wait(5) and previews.rendering()
    t0 = time.monotonic()
    previews.shutdown(wait=False)
    assert time.monotonic() - t0 < 0.3 and previews.rendering()
    assert previews.wait(5000) and not previews.rendering()


# ---------- player capsule ----------


def test_player_capsule_buttons_in_order(host):
    capsule = w.PlayerCapsule(host)
    buttons = capsule.buttons()
    assert [b.objectName() for b in buttons] == [
        "skipButton", "transportButton", "transportButtonPrimary", "transportButton", "skipButton",
    ]
    assert buttons[0].text() == "−10 sec" and buttons[4].text() == "+10 sec"
    assert buttons[0].width() == buttons[4].width()  # Play stays centered

    capsule.show()
    QApplication.processEvents()
    xs = [b.geometry().x() for b in buttons]
    assert xs == sorted(xs)

    skips, paragraphs, plays = _recorder(capsule.skip_clicked), _recorder(capsule.paragraph_clicked), []
    capsule.play_clicked.connect(lambda: plays.append(True))
    for button in buttons:
        button.click()
    assert skips == [-w.SKIP_MS, w.SKIP_MS]
    assert paragraphs == [-1, 1]
    assert plays == [True]


def test_player_capsule_states(host):
    capsule = w.PlayerCapsule(host)
    assert capsule.play_button.toolTip().startswith("Play")
    capsule.set_playing(True)
    assert capsule.play_button.toolTip().startswith("Pause")
    capsule.set_play_enabled(False)
    capsule.set_navigation_enabled(False)
    assert not any(b.isEnabled() for b in capsule.buttons())
    capsule.set_navigation_enabled(True)
    assert [b.isEnabled() for b in capsule.buttons()] == [True, True, False, True, True]
    for key in THEMES:  # repaints with every theme's icons
        themes.set_current(THEMES[key])
        capsule.refresh_icons()
        assert not capsule.grab().isNull()
    themes.set_current(THEMES["original"])


# ---------- progress, switch, icons ----------


def test_progress_bar_paints_from_empty_to_full(host):
    bar = w.GradientProgressBar(host)
    bar.setFixedSize(240, 22)
    bar.setFormat("Generating  %p%")
    for value in (0, 5, 55, 100):
        bar.setValue(value)
        assert not bar.grab().isNull()


def test_toggle_switch(host):
    switch = w.ToggleSwitch(host)
    toggles = _recorder(switch.toggled)
    switch.click()
    switch.setChecked(False)
    assert toggles == [True, False]
    switch.setChecked(True)
    assert switch._knob() == 1.0  # not shown, so no slide
    switch.setEnabled(False)
    assert not switch.grab().isNull()


def test_icons_are_drawn(qapp):
    for icon in (w.aa_icon("#000000"), w.list_icon("#000000"), w.chevron_icon("#000000"), w.half_circle_icon("#000")):
        assert not icon.isNull()
    book = w.book_pixmap(60)
    assert book.width() == 120  # 2× for Retina
    assert w.APP_ICON.is_file()


# ---------- alerts ----------


def _answer_with(monkeypatch, object_name: str, seen: dict):
    def fake_exec(alert):
        buttons = alert.findChildren(QPushButton)
        seen["order"] = [b.text() for b in sorted(buttons, key=lambda b: b.geometry().x())]
        seen["default"] = next((b.text() for b in buttons if b.isDefault()), None)
        seen["title"] = alert.title_label.text()
        alert.show()
        QApplication.processEvents()
        seen["order"] = [b.text() for b in sorted(buttons, key=lambda b: b.mapTo(alert, QPoint(0, 0)).x())]
        next(b for b in buttons if b.objectName() == object_name).click()
        return alert.result()

    monkeypatch.setattr(w.Alert, "exec", fake_exec)


def test_confirm_yes_and_no(host, monkeypatch):
    seen = {}
    _answer_with(monkeypatch, "primaryButton", seen)
    assert w.confirm(host, "Generate audio from the beginning?", "") is True
    assert seen["order"] == ["No", "Yes"]  # No on the left, Yes on the right
    assert seen["default"] == "Yes"
    assert seen["title"] == "Generate audio from the beginning?"

    _answer_with(monkeypatch, "secondaryButton", seen)
    assert w.confirm(host, "Audio not ready yet", "Start generating audio from this point?") is False


def test_confirm_destructive_defaults_to_no(host, monkeypatch):
    seen = {}
    _answer_with(monkeypatch, "dangerButton", seen)
    assert w.confirm(host, "Cancel generation?", "The audio made so far is kept.",
                     yes="Cancel Generation", no="Keep Going", destructive=True) is True
    assert seen["order"] == ["Keep Going", "Cancel Generation"]
    assert seen["default"] == "Keep Going"


def test_inform_has_one_ok(host, monkeypatch):
    seen = {}
    _answer_with(monkeypatch, "primaryButton", seen)
    assert w.inform(host, "Export finished", "Saved to Music.") is None
    assert seen["order"] == ["OK"]


def test_alert_hides_an_empty_text(host):
    alert = w.Alert(host, "Generate audio from the beginning?", "")
    assert alert.text_label.isHidden()
    assert alert.windowTitle() == "LivroSound"
    assert isinstance(alert, QDialog)


# ---------- landing ----------


def test_landing_buttons(host):
    landing = LivroSoundLanding(host)
    uploads, libraries = [], []
    landing.upload_clicked.connect(lambda: uploads.append(True))
    landing.library_clicked.connect(lambda: libraries.append(True))
    assert landing.library_button.isEnabled()  # the library page says when it's empty
    landing.upload_button.click()
    assert uploads == [True]
    landing.library_button.click()
    assert libraries == [True]
    assert landing.upload_button.text() == "Upload New Book"
    assert landing.library_button.text() == "Go to Library"
    assert landing.upload_button.width() == landing.library_button.width()


def test_landing_follows_the_theme(host):
    landing = LivroSoundLanding(host)
    landing.resize(1100, 780)
    landing.show()
    for key in ("original", "dark"):
        themes.set_current(THEMES[key])
        host.setStyleSheet(stylesheet(THEMES[key]))
        image = landing.grab().toImage()
        assert image.pixelColor(5, 5).name().upper() == THEMES[key].panel.upper()
    themes.set_current(THEMES["original"])
    # The book, wordmark and buttons are stacked top to bottom, centered.
    order = [landing.icon, landing.upload_button]
    assert order[0].geometry().bottom() < order[1].geometry().top()
    center = landing.width() / 2
    assert abs(landing.icon.geometry().center().x() - center) <= 1
