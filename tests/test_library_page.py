"""The library page: covers in a grid or rows in a list, its ⋯ and ☰ menus, search,
selecting, collections and tags (offscreen, on a hand-made library)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from livrosound import bookinfo
from livrosound.library import BookRecord, Library, Settings
from livrosound.ui import theme as themes
from livrosound.ui.library_page import LibraryPage
from livrosound.ui.theme import THEMES, stylesheet


@pytest.fixture
def host(qapp):
    themes.set_current(THEMES["original"])
    widget = QWidget()
    widget.setStyleSheet(stylesheet(THEMES["original"]))
    widget.resize(1100, 780)
    yield widget
    widget.close()
    widget.deleteLater()


def _library() -> Library:
    lib = Library()
    for i, (title, author, kind) in enumerate((
        ("The Trial", "Franz Kafka", "docx"), ("Anna Karenina", "Leo Tolstoy", "epub"),
        ("A Boy's Own Story", "Edmund White", "pdf"), ("Writing Fiction", "Janet Burroway", "epub"),
    )):
        lib.upsert(BookRecord(f"b{i}", title, author, f"/x/{i}.{kind}", kind,
                              last_opened=f"2026-10-{20 + i:02d}T10:00:00", read_fraction=0.1 * i))
    return lib


@pytest.fixture
def page(host):
    page = LibraryPage(_library(), Settings(), host)
    page.resize(1100, 780)
    page.show()
    QApplication.processEvents()
    return page


def _shown(page) -> list[str]:
    return [r.book_id for r in page.shelf.books()]


def test_grid_newest_first_and_clicking_a_cover_opens_it(page):
    assert _shown(page) == ["b3", "b2", "b1", "b0"]  # Date Read, descending (the default)
    assert page.count.text() == "4 books" and page.search.placeholderText() == "Search in 4 books…"
    opened = []
    page.open_requested.connect(opened.append)
    rect = page.shelf.item_rect("b1")
    assert rect.height() > rect.width()  # a cover tile
    QTest.mouseClick(page.shelf.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert opened == ["b1"]


def test_view_menu_switches_list_grid_columns_group_and_sort(page):
    changes = []
    page.changed.connect(lambda: changes.append(True))
    menu = page.view_menu()
    labels = [a.text() for a in menu.actions() if a.text()]
    assert labels[:2] == ["List", "Grid"] and "Group by…" in labels and "Sort by…" in labels and "Then by…" in labels
    next(a for a in menu.actions() if a.text() == "List").trigger()
    assert page.settings.library_view == "list" and changes
    assert page.shelf.item_rect("b0").height() < page.shelf.item_rect("b0").width()  # a row
    sort_menu = next(a.menu() for a in menu.actions() if a.text() == "Sort by…")
    assert [a.text() for a in sort_menu.actions() if a.text()][:10] == [
        "Title", "Author", "Format", "Pages", "Series", "Date Read", "Date Added", "Date Published",
        "Progress Read", "Time Remaining",
    ]
    next(a for a in sort_menu.actions() if a.text() == "Title").trigger()
    next(a for a in sort_menu.actions() if a.text() == "Ascending").trigger()
    assert _shown(page) == ["b1", "b2", "b0", "b3"]
    group_menu = next(a.menu() for a in menu.actions() if a.text() == "Group by…")
    assert [a.text() for a in group_menu.actions()] == [
        "None", "Authors", "Tags", "Subjects", "Collections", "Series", "Status",
    ]
    next(a for a in group_menu.actions() if a.text() == "Authors").trigger()
    assert page.shelf.headers()[0] == "Janet Burroway  ·  1"
    page.settings.library_view = "grid"
    page._step_columns(-10)
    assert page.settings.library_columns == 1 and page.shelf.columns() == 1
    page._set("library_columns", 0)
    assert page.shelf.columns() > 3


def test_search_filters(page):
    page.search.setText("tolstoy")
    assert _shown(page) == ["b1"] and page.count.text() == "1 of 4"
    page.search.setText("zzz")
    assert _shown(page) == []


def test_select_collections_tags_and_remove(page):
    page.ask_text = lambda title, label, names: {"New Collection": "Summer", "Add a Tag": "classic"}[title]
    page.select_button.click()
    assert page.shelf.selecting() and not page.actions_bar.isHidden()
    assert not page.action_buttons["open"].isEnabled()
    for book_id in ("b0", "b1"):
        QTest.mouseClick(page.shelf.viewport(), Qt.MouseButton.LeftButton, pos=page.shelf.item_rect(book_id).center())
    assert page.shelf.selected() == ["b1", "b0"]
    assert page.action_buttons["tag"].isEnabled() and not page.action_buttons["details"].isEnabled()
    page._new_collection(page.shelf.selected())
    page._tag_selected()
    assert page.library.get("b0").collections == ["Summer"] and page.library.get("b1").tags == ["classic"]
    page.set_collection(["b1"], "summer", False)  # case doesn't matter
    assert page.library.get("b1").collections == []
    removed = []
    page.remove_requested.connect(removed.append)
    page.action_buttons["remove"].click()
    assert removed == [["b1", "b0"]]
    page.select_all_button.click()
    assert len(page.shelf.selected()) == 4
    page.action_buttons["cancel"].click()
    assert not page.shelf.selecting() and page.shelf.selected() == []


def test_context_menu_and_app_menu(page):
    menu = page.context_menu("b2")
    assert [a.text() for a in menu.actions() if a.text()] == [
        "Open", "Details…", "Add to Collection", "Add Tag…", "Show in Finder", "Remove from Library…",
    ]
    toggled = []
    page.option_toggled.connect(lambda field, on: toggled.append((field, on)))
    app = page.app_menu()
    labels = [a.text() for a in app.actions() if a.text()]
    assert labels == ["Open Last Book on Start", "Auto Import on File Open", "Always Show Status Bar", "Auto Mode",
                      "Settings…", "About LivroSound"]
    auto_import = next(a for a in app.actions() if a.text() == "Auto Import on File Open")
    assert auto_import.isChecked()  # on by default, as in Readest
    next(a for a in app.actions() if a.text() == "Open Last Book on Start").trigger()
    assert page.settings.open_last_book and toggled == [("open_last_book", True)]


def test_covers_drawn_or_loaded_and_empty_state(page, host):
    image = QImage(200, 300, QImage.Format.Format_RGB32)
    image.fill(QColor("#ff0000"))
    bookinfo.cover_path("b2").parent.mkdir(parents=True, exist_ok=True)
    image.save(str(bookinfo.cover_path("b2")), "JPG")
    page.cover_changed("b2")
    grab = page.shelf.grab().toImage()
    rect = page.shelf.item_rect("b2")
    center = grab.pixelColor(rect.center().x(), rect.top() + rect.width())
    assert center.red() > 200 and center.green() < 60  # the saved cover
    drawn = grab.pixelColor(page.shelf.item_rect("b0").left() + 20, page.shelf.item_rect("b0").top() + rect.width())
    assert drawn != QColor(THEMES["original"].panel)  # a drawn cover where there's none
    empty = LibraryPage(Library(), Settings(), host)
    assert empty.shelf.isHidden() and not empty.empty.isHidden() and not empty.select_button.isEnabled()


def test_right_click_opens_the_books_menu(page, monkeypatch):
    asked = []
    page.shelf.menu_requested.disconnect()
    page.shelf.menu_requested.connect(lambda book_id, pos: asked.append(book_id))
    QTest.mouseClick(page.shelf.viewport(), Qt.MouseButton.RightButton, pos=page.shelf.item_rect("b3").center())
    assert asked == ["b3"]
    QTest.mouseClick(page.shelf.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(2, 2))  # nothing there
