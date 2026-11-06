"""ReflowView: exact char maps, clicks, highlights that hug the block's own words,
centering, themes, the original layout's alignment, and speed on a 2 M-char book."""

from __future__ import annotations

import random
import time
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from livrosound import paths
from livrosound.book import Block, Book
from livrosound.ui import theme as themes
from livrosound.ui.reader_view import centered_scroll_value, outside_band, too_tall
from livrosound.ui.reflow_view import _FOLD, SEPARATOR, ReflowView, align_blocks, prepare_original_html

EBOOKS = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Ebooks"
WORDS = (
    "the of and a to in he was that it his her with as had for she not at but on is by be "
    "him which Levin Anna Vronsky Kitty said Stepan Arkadyevitch\u2019s \u201cWell,\u201d caf\u00e9 na\u00efve"
).split()


def _sentence(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."


def _paragraphs(rng: random.Random, count: int) -> list[Block]:
    return [Block("paragraph", " ".join(_sentence(rng, rng.randint(4, 14)) for _ in range(3))) for _ in range(count)]


def _sample_book(book_id: str = "sample") -> Book:
    rng = random.Random(7)
    blocks = [
        Block("heading", "Chapter One", level=1),
        Block("heading", "Short Sentences", level=2),
        Block("quote", "The short sentence is the mind\u2019s quickest messenger.", styles=[(4, 9, "i")]),
        *_paragraphs(rng, 6),
        Block("separator", "* * *"),
        Block("paragraph", "Small caps and a note mark\u00b9 and bold.", styles=[(0, 10, "sc"), (26, 27, "sup"), (32, 36, "b")]),
        Block("verse", "Tyger Tyger, burning bright,\nIn the forests of the night;\nWhat immortal hand or eye"),
        Block("note", "1. Burgess said this in 1972."),
        Block("paragraph", "Wide chars: \U0001d49c and \U0001f600 sit in the text."),
        *_paragraphs(rng, 4),
    ]
    return Book(book_id, "Sample", "Someone", "", "txt", blocks=blocks)


def _view(qapp, book: Book | None = None, mode: str = "livrosound", size=(900, 700)) -> ReflowView:
    view = ReflowView()
    view.resize(*size)
    view.show()
    qapp.processEvents()
    if book is not None:
        view.set_book(book, mode)
        qapp.processEvents()
    return view


def _displayed(ch: str) -> str:
    return "\u2028" if ch == "\n" else ch


def _char_at(view: ReflowView, pos: int) -> str:
    return view.document().characterAt(pos)


def _selections(view: ReflowView) -> list[tuple[int, int]]:
    return [(s.cursor.selectionStart(), s.cursor.selectionEnd()) for s in view.extraSelections()]


def _hover(view: ReflowView, point: QPoint) -> None:
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(view.viewport().mapToGlobal(point)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), event)


def _point_of(view: ReflowView, pos: int) -> QPoint:
    """A viewport point inside the char at a document position."""
    rect = view.cursorRect(view._cursor(pos))
    nxt = view.cursorRect(view._cursor(pos + 1))
    x = (rect.left() + nxt.left()) // 2 if nxt.top() == rect.top() and nxt.left() > rect.left() else rect.left() + 2
    return QPoint(x, rect.center().y())


# ---------- pure helpers ----------


def test_centered_scroll_value_centers_and_clamps():
    assert centered_scroll_value(120, 240, 280, 400, 1000) == 180
    assert centered_scroll_value(0, 10, 30, 400, 1000) == 0
    assert centered_scroll_value(950, 500, 540, 400, 1000) == 1000


def test_follow_band_rules():
    assert not outside_band(100, 300, 400)  # inside the middle 70%
    assert outside_band(20, 60, 400)  # above it
    assert outside_band(300, 380, 400)  # below it
    assert too_tall(0, 300, 400) and not too_tall(0, 200, 400)


def test_prepare_original_html_moves_anchors_inside_and_drops_colors():
    html = (
        '<style>body { color: #000; background: white; font-size: 1em }</style>'
        '<a name="ls-b0"></a><h1>T</h1>\n<a id="ls-b1"/>\n<div class="x"><p style="color:red; margin: 0">One</p></div>'
        '<a name="ls-b2"></a><img src="res/a.png"/><font color="#123456">x</font>'
    )
    out = prepare_original_html(html)
    assert '<h1><a name="ls-b0"></a>T</h1>' in out
    assert '<div class="x"><p style=" margin: 0"><a name="ls-b1"></a>One</p></div>' in out
    assert '<a name="ls-b2"></a><img src="res/a.png"/>' in out  # nothing to move into
    assert "color" not in out and "background" not in out
    assert "font-size: 1em" in out


def test_prepare_original_html_gives_qt_margins_in_px():
    html = '<style>.b { margin: 6pt 0; text-indent: 1.5em; font-size: 0.8em }</style><p style="margin-top:3pt">x</p>'
    out = prepare_original_html(html, em_px=20)
    assert ".b { margin: 8px 0; text-indent: 30px; font-size: 0.8em }" in out
    assert 'style="margin-top:4px"' in out


# ---------- the original layout's alignment ----------


def _check_map(doc: str, texts: list[str], positions, matched, offsets):
    for i, text in enumerate(texts):
        m = positions[offsets[i] : offsets[i + 1]]
        assert len(m) == len(text) + 1
        assert np.all(np.diff(m) >= 0), f"block {i} map goes backwards"
        for k, ch in enumerate(text):
            if matched[offsets[i] + k]:
                assert doc[m[k]].lower() == ch.lower()


def test_align_blocks_exact_with_extra_whitespace():
    texts = ["Chapter One", "It was a dark night.", "Then  it rained."]
    doc = "\n  Chapter One\n\n    It was a\ndark   night.\nThen it\n rained.\n"
    anchors = {0: doc.index("Chapter"), 1: doc.index("It"), 2: doc.index("Then")}
    positions, matched, offsets = align_blocks(doc, texts, anchors)
    _check_map(doc, texts, positions, matched, offsets)
    for i, text in enumerate(texts):
        m = positions[offsets[i] : offsets[i + 1]]
        assert all(matched[offsets[i] + k] for k, ch in enumerate(text) if not ch.isspace())
        assert m[0] == anchors[i]


def test_align_blocks_skips_a_running_head_left_in_the_page():
    text = "The walls are white and the floor is cold."
    doc = "The walls 24 ARTFUL SENTENCES are white and the floor is cold."
    positions, matched, offsets = align_blocks(doc, [text], {0: 0})
    _check_map(doc, [text], positions, matched, offsets)
    head = range(doc.index("24"), doc.index(" are"))
    assert not any(positions[k] in head for k in range(len(text)) if matched[k])
    assert positions[text.index("are")] == doc.index("are")


def test_align_blocks_folds_ligatures_quotes_dashes_and_case():
    text = "\u201cFind it\u201d \u2014 SHORT sentences"
    doc = '"\ufb01nd it" - Short Sentences'
    positions, matched, offsets = align_blocks(doc, [text], {0: 0})
    assert positions[text.index("nd")] == doc.index("nd")
    assert positions[text.index("SHORT")] == doc.index("Short")
    assert matched[text.index("F")] and not matched[text.index("i")]
    assert positions[len(text)] == len(doc)


def test_align_blocks_never_runs_away_on_text_the_page_lacks():
    texts = ["Alpha beta QQQQQQQQ gamma delta.", "Next block."]
    doc = "Alpha beta gamma delta. Next block."
    positions, matched, offsets = align_blocks(doc, texts, {0: 0, 1: doc.index("Next")})
    m = positions[offsets[0] : offsets[1]]
    q = texts[0].index("Q")
    assert m[q] <= doc.index("gamma")  # parked just after "beta", not at the end
    assert m[texts[0].index("gamma")] == doc.index("gamma")
    assert positions[offsets[1]] == doc.index("Next")


def test_align_blocks_edges():
    texts = ["", " Lead", "Gone", "Tail"]
    doc = "xx Lead yy Tail"
    positions, matched, offsets = align_blocks(doc, texts, {0: 1, 1: 3, 3: doc.index("Tail")})
    assert positions[offsets[0]] == 1  # an empty block sits at its anchor
    assert positions[offsets[1]] == 3 and positions[offsets[1] + 1] == doc.index("Lead")
    gone = positions[offsets[2] : offsets[3]]
    assert not matched[offsets[2] : offsets[3] - 1].any()
    assert set(gone.tolist()) == {doc.index("Lead") + 4}  # parked right after "Lead"
    assert positions[offsets[4] - 1] == len(doc)


def test_align_blocks_without_anchors_continue_from_the_block_before():
    texts = ["One two.", "Three four.", "Five."]
    doc = "One two. Three four. Five."
    positions, matched, offsets = align_blocks(doc, texts, {0: 0})
    assert positions[offsets[1]] == doc.index("Three")
    assert positions[offsets[2]] == doc.index("Five")


def test_align_blocks_random_noise_stays_monotonic_and_exact_where_matched():
    rng = random.Random(3)
    texts = [_sentence(rng, rng.randint(3, 20)) for _ in range(60)]
    parts, anchors = [], {}
    pos = 0
    for i, text in enumerate(texts):
        noise = rng.choice(["", " 12 ", "\n", " ARTFUL SENTENCES 19 ", "  "])
        chunk = noise + text.replace(" ", rng.choice([" ", "  ", "\n"])) + "\n"
        if rng.random() < 0.8:
            anchors[i] = pos
        parts.append(chunk)
        pos += len(chunk)
    doc = "".join(parts)
    positions, matched, offsets = align_blocks(doc, texts, anchors)
    _check_map(doc, texts, positions, matched, offsets)
    assert matched.sum() >= 0.98 * sum(len(t.replace(" ", "")) for t in texts)


# ---------- the LivroSound layout ----------


def test_char_maps_are_exact(qapp):
    book = _sample_book()
    view = _view(qapp, book)
    rng = random.Random(1)
    for i, block in enumerate(book.blocks):
        if block.kind in ("separator", "image"):
            continue
        for k in rng.sample(range(len(block.text)), min(25, len(block.text))):
            ch = block.text[k]
            if ord(ch) > 0xFFFF:  # Qt shows it as two UTF-16 units: its high surrogate comes first
                ch = ch.encode("utf-16-le")[:2].decode("utf-16-le", "surrogatepass")
            assert _char_at(view, view._doc_pos(i, k)) == _displayed(ch)
    wide = next(i for i, b in enumerate(book.blocks) if "\U0001f600" in b.text)
    text = book.blocks[wide].text
    after = view._doc_pos(wide, text.index(" sit"))
    assert _char_at(view, after) == " "
    assert view._doc_pos(wide, len(text)) - view._doc_pos(wide, 0) == len(text) + 2  # two chars outside the BMP


def test_document_text_and_styles(qapp):
    book = _sample_book()
    view = _view(qapp, book)
    text = view.document().toRawText()
    sep = next(i for i, b in enumerate(book.blocks) if b.kind == "separator")
    assert _char_at(view, view._starts[sep]) == SEPARATOR
    styled = next(i for i, b in enumerate(book.blocks) if b.styles and b.kind == "paragraph")
    fmt = lambda k: view._cursor(view._doc_pos(styled, k) + 1).charFormat()  # noqa: E731
    assert fmt(2).fontFeatures() == {QFont.Tag("smcp"): 1}
    assert fmt(26).verticalAlignment() == fmt(26).VerticalAlignment.AlignSuperScript
    assert fmt(33).fontWeight() >= 700
    quote = view._cursor(view._doc_pos(2, 5) + 1).charFormat()
    assert quote.fontItalic()
    assert "Tyger Tyger, burning bright,\u2028In the forests" in text


def test_first_line_indents_follow_book_rules(qapp):
    book = _sample_book()
    view = _view(qapp, book)
    doc = view.document()

    def indent(i):
        return doc.findBlock(view._starts[i]).blockFormat().textIndent()

    assert indent(3) == 0  # after the chapter's epigraph
    paragraphs = [i for i, b in enumerate(book.blocks) if b.kind == "paragraph"]
    assert indent(paragraphs[1]) > 0  # a paragraph after a paragraph
    after_separator = next(i for i, b in enumerate(book.blocks) if i and book.blocks[i - 1].kind == "separator")
    assert indent(after_separator) == 0
    heading = doc.findBlock(view._starts[0])
    assert heading.blockFormat().alignment() & Qt.AlignmentFlag.AlignHCenter


def test_click_round_trip(qapp):
    book = _sample_book()
    view = _view(qapp, book, size=(900, 4000))
    got = []
    view.clicked.connect(lambda b, c: got.append((b, c)))
    rng = random.Random(5)
    for i, block in enumerate(book.blocks):
        if block.kind != "paragraph":
            continue
        for k in rng.sample([k for k, ch in enumerate(block.text) if ch.isalpha()], 4):
            got.clear()
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             _point_of(view, view._doc_pos(i, k)))
            assert got == [(i, k)]


def test_a_drag_is_not_a_click(qapp):
    book = _sample_book()
    view = _view(qapp, book)
    got = []
    view.clicked.connect(lambda b, c: got.append((b, c)))
    a = _point_of(view, view._doc_pos(3, 2))
    b = _point_of(view, view._doc_pos(3, 40))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
    QTest.mouseMove(view.viewport(), b)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)
    assert got == []


def test_highlight_hugs_the_sentence(qapp):
    book = _sample_book()
    view = _view(qapp, book)
    text = book.blocks[3].text
    end = text.index(".") + 1
    view.show_highlight(3, 0, end + 1)  # a trailing space is not highlighted
    assert _selections(view) == [(view._doc_pos(3, 0), view._doc_pos(3, end))]
    selections = view.extraSelections()
    fmt = selections[0].format
    assert fmt.background().color() == QColor(themes.current().highlight)
    view.clear_highlight()
    assert view.extraSelections() == []


def test_verse_highlight_splits_at_line_breaks(qapp):
    book = _sample_book()
    view = _view(qapp, book)
    verse = next(i for i, b in enumerate(book.blocks) if b.kind == "verse")
    text = book.blocks[verse].text
    view.show_highlight(verse, 0, len(text))
    runs = _selections(view)
    lines = text.split("\n")
    assert len(runs) == len(lines)
    for (a, b), line in zip(runs, lines):
        assert view.document().toRawText()[a:b] == line


def test_skipped_blocks_are_muted_and_never_highlighted(qapp):
    book = _sample_book()
    book.blocks[3].role = "copyright"
    view = _view(qapp, book)
    view.show_highlight(3, 0, 10)
    assert view.extraSelections() == []
    color = view._cursor(view._doc_pos(3, 2)).charFormat().foreground().color()
    assert color == QColor(themes.current().muted)
    view.set_theme(themes.THEMES["dark"])
    color = view._cursor(view._doc_pos(3, 2)).charFormat().foreground().color()
    assert color == QColor(themes.THEMES["dark"].muted)
    normal = view._cursor(view._doc_pos(4, 2)).charFormat()
    assert not normal.hasProperty(normal.Property.ForegroundBrush)


def test_themes_color_the_page_and_the_highlight(qapp):
    book = _sample_book()
    view = _view(qapp, book)
    view.show_highlight(4, 0, 12)
    for key in ("original", "calm", "dark"):
        t = themes.THEMES[key]
        view.set_theme(t)
        palette = view.viewport().palette()
        assert palette.color(palette.ColorRole.Base) == QColor(t.page)
        assert palette.color(palette.ColorRole.Text) == QColor(t.text)
        selections = view.extraSelections()
        assert selections[0].format.background().color() == QColor(t.highlight)
        image = view.viewport().grab().toImage()
        assert QColor(image.pixel(3, 3)) == QColor(t.page)


def test_scroll_to_centers_the_block(qapp):
    rng = random.Random(2)
    book = Book("long", "Long", "X", "", "txt", blocks=_paragraphs(rng, 120))
    view = _view(qapp, book)
    height = view.viewport().height()
    for block in (40, 77, 100):
        view.scroll_to(block, animate=False)
        positions = view._block_positions(block)
        top, bottom = view._span(int(positions[0]), int(positions[-1]))
        assert abs((top + bottom) / 2 - height / 2) <= 2
        assert view.current_block() <= block


def test_scroll_to_a_tall_block_centers_the_highlight(qapp):
    rng = random.Random(4)
    long_text = " ".join(_sentence(rng, 12) for _ in range(80))
    book = Book("tall", "Tall", "X", "", "txt", blocks=[*_paragraphs(rng, 10), Block("paragraph", long_text), *_paragraphs(rng, 10)])
    view = _view(qapp, book)
    start = long_text.index(".", 2000) + 2
    end = long_text.index(".", start) + 1
    view.show_highlight(10, start, end)
    view.scroll_to(10, animate=False)
    top, bottom = view._span(view._doc_pos(10, start), view._doc_pos(10, end))
    assert abs((top + bottom) / 2 - view.viewport().height() / 2) <= 2


def test_keep_in_view_recenters_only_when_needed(qapp):
    rng = random.Random(6)
    book = Book("follow", "F", "X", "", "txt", blocks=_paragraphs(rng, 80))
    view = _view(qapp, book)
    view.scroll_to(30, animate=False)
    value = view.verticalScrollBar().value()
    view.keep_in_view(30, 0, 20)
    assert view.verticalScrollBar().value() == value
    view.keep_in_view(45, 0, 20)  # far below: re-centers (animated)
    deadline = time.monotonic() + 2
    while view._scroll_animation is not None and time.monotonic() < deadline:
        QTest.qWait(30)
        if view.verticalScrollBar().value() == view.scroll_target():
            break
    QTest.qWait(600)
    top, bottom = view._span(view._doc_pos(45, 0), view._doc_pos(45, 20))
    assert abs((top + bottom) / 2 - view.viewport().height() / 2) <= 3


def test_user_scrolling_is_reported_and_player_keys_pass_through(qapp):
    rng = random.Random(8)
    book = Book("keys", "K", "X", "", "txt", blocks=_paragraphs(rng, 60))

    class Window(QWidget):
        keys: list[int] = []

        def keyPressEvent(self, event):
            self.keys.append(event.key())

    window = Window()
    view = ReflowView(window)
    window.resize(800, 600)
    view.resize(800, 600)
    window.show()
    view.set_book(book, "livrosound")
    qapp.processEvents()
    scrolled = []
    view.user_scrolled.connect(lambda: scrolled.append(1))
    view.scroll_to(30, animate=False)
    assert scrolled == []
    wheel = QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(0, -120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view.viewport(), wheel)
    assert scrolled == [1]
    size = view.document().defaultFont().pointSizeF()
    zoom = QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(0, 120),
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view.viewport(), zoom)
    assert view.document().defaultFont().pointSizeF() == size  # no zooming behind set_scale's back
    QTest.keyClick(view, Qt.Key.Key_PageDown)
    assert scrolled == [1, 1]
    QTest.mouseClick(view.verticalScrollBar(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(5, view.verticalScrollBar().height() - 20))
    assert len(scrolled) == 3
    for key in (Qt.Key.Key_Space, Qt.Key.Key_Left, Qt.Key.Key_Right):
        QTest.keyClick(view, key)
    assert window.keys == [Qt.Key.Key_Space, Qt.Key.Key_Left, Qt.Key.Key_Right]


def test_contents_lines_act_like_links(qapp):
    book = _sample_book()
    book.blocks[4].role = "contents"
    book.blocks[4].link = 9
    view = _view(qapp, book, size=(900, 3000))
    point = _point_of(view, view._doc_pos(4, 3))
    _hover(view, point)
    assert view.viewport().cursor().shape() == Qt.CursorShape.PointingHandCursor
    selections = view.extraSelections()
    underlines = [s for s in selections if s.format.fontUnderline()]
    assert underlines and underlines[0].cursor.selectionStart() == view._doc_pos(4, 0)
    got = []
    view.clicked.connect(lambda b, c: got.append((b, c)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert got == [(4, 3)]
    _hover(view, _point_of(view, view._doc_pos(5, 3)))
    assert view.viewport().cursor().shape() != Qt.CursorShape.PointingHandCursor
    selections = view.extraSelections()
    assert not any(s.format.fontUnderline() for s in selections)


def test_set_scale_rebuilds_with_the_same_maps(qapp):
    book = _sample_book()
    view = _view(qapp, book)
    view.show_highlight(4, 0, 30)
    before = view.document().toRawText()
    view.set_scale(26)
    assert view.document().toRawText() == before
    assert view.document().defaultFont().pointSizeF() == 26
    assert _selections(view) and _selections(view)[0][0] == view._doc_pos(4, 0)


def test_switching_layouts_and_sizes_keeps_highlight_and_hover_valid(qapp):
    book = _original_book("switch")
    view = _view(qapp, book, size=(900, 2000))
    _hover(view, _point_of(view, view._doc_pos(4, 2)))  # a contents line: underlined
    view.show_highlight(1, 0, 20)
    view.set_scale(24)  # rebuilt: the selections must point into the new document
    selections = view.extraSelections()
    assert all(s.cursor.document() is view.document() for s in selections)
    assert any(s.format.fontUnderline() for s in selections)
    view.set_book(book, "original")
    assert view.extraSelections() == [] and view.viewport().cursor().shape() != Qt.CursorShape.PointingHandCursor
    view.show_highlight(1, 0, 20)
    view.set_book(_sample_book(), "livrosound")
    assert view.extraSelections() == []
    view.show_highlight(3, 0, 5)
    assert _selections(view) == [(view._doc_pos(3, 0), view._doc_pos(3, 5))]


def test_images_are_scaled_to_the_column(qapp):
    book = _sample_book("imgbook")
    res = paths.book_dir("imgbook") / "res"
    res.mkdir(parents=True)
    image = QImage(1600, 800, QImage.Format.Format_RGB32)
    image.fill(QColor("#4F7942"))
    image.save(str(res / "plate.png"))
    book.blocks.insert(3, Block("image", "", src="plate.png"))
    view = _view(qapp, book, size=(900, 700))
    fmt = view._cursor(view._starts[3] + 1).charFormat()
    assert fmt.isImageFormat()
    width = fmt.toImageFormat().width()
    assert width <= view.viewport().width() - 40
    assert fmt.toImageFormat().height() == pytest.approx(width / 2, abs=1)
    view.resize(500, 700)
    qapp.processEvents()
    assert view._cursor(view._starts[3] + 1).charFormat().toImageFormat().width() < width


def test_narrow_and_wide_windows_keep_a_centered_column(qapp):
    book = _sample_book()
    view = _view(qapp, book, size=(1400, 700))
    viewport = view.viewport().geometry()
    bar = view.verticalScrollBar().sizeHint().width()
    assert viewport.width() == int((1400 - bar) * 0.85)  # 85% of the window by default
    assert abs(viewport.left() - (view.width() - viewport.right())) < 30
    view.resize(500, 700)
    qapp.processEvents()
    assert view.viewport().geometry().left() >= 20
    assert view.viewport().width() < 500


# ---------- the original layout ----------

ORIGINAL_HTML = """<html><head><style>p { text-indent: 1.2em; color: #222; } h2 { text-align: center }</style></head>
<body bgcolor="#ffffff">
<a name="ls-b0"></a><h2>CHAPTER   ONE</h2>
<a name="ls-b1"></a>
<p>It was a <i>bright</i>   cold day in April,
   and the clocks were striking thirteen.</p>
<a name="ls-b2"></a><p class="x"><span>Winston Smith, his chin</span> nuzzled into his breast,
slipped quickly 24 ARTFUL SENTENCES through the glass doors.</p>
<a name="ls-b3"></a><div><blockquote><p>A quoted <b>line</b>.</p></blockquote></div>
<a name="ls-b4"></a><p><a href="#ch2">Chapter Two</a></p>
<p>Stray text that belongs to no block.</p>
<a name="ls-b5"></a><img src="res/plate.png"/>
<a name="ls-b6"></a><p>The ﬁnal line.</p>
</body></html>"""


def _original_book(book_id: str = "orig") -> Book:
    res = paths.book_dir(book_id) / "res"
    res.mkdir(parents=True, exist_ok=True)
    image = QImage(1400, 700, QImage.Format.Format_RGB32)
    image.fill(QColor("#888888"))
    image.save(str(res / "plate.png"))
    blocks = [
        Block("heading", "Chapter One", level=1),
        Block("paragraph", "It was a bright cold day in April, and the clocks were striking thirteen.", styles=[(9, 15, "i")]),
        Block("paragraph", "Winston Smith, his chin nuzzled into his breast, slipped quickly through the glass doors."),
        Block("quote", "A quoted line."),
        Block("paragraph", "Chapter Two", role="contents", link=6),
        Block("image", "", src="plate.png"),
        Block("paragraph", "The final line."),
    ]
    return Book(book_id, "Orig", "X", "", "epub", blocks=blocks, original_html=ORIGINAL_HTML)


def test_original_layout_alignment(qapp):
    book = _original_book()
    view = _view(qapp, book, mode="original")
    raw = view.document().toRawText()
    for i, block in enumerate(book.blocks):
        if block.kind == "image":
            continue
        for k, ch in enumerate(block.text):
            if view._matched[view._offsets[i] + k]:
                assert raw[view._doc_pos(i, k)].translate(_FOLD) == ch.translate(_FOLD), (i, k, ch)
            elif not ch.isspace():
                assert ch == "i" and i == 6  # "ﬁ" shows both letters as one char
    assert "ARTFUL" in raw  # left on the page...
    view.show_highlight(2, 0, len(book.blocks[2].text))
    runs = _selections(view)
    assert len(runs) == 2  # ...the highlight steps over it
    for a, b in runs:
        assert "ARTFUL" not in raw[a:b] and "24" not in raw[a:b]
        assert not raw[a].isspace() and not raw[b - 1].isspace()
    assert raw[runs[0][0] : runs[-1][1]].startswith("Winston") and raw[runs[-1][1] - 1] == "."


def test_original_layout_clicks_images_links_and_colors(qapp):
    book = _original_book("orig2")
    view = _view(qapp, book, mode="original", size=(900, 2000))
    got = []
    view.clicked.connect(lambda b, c: got.append((b, c)))
    k = book.blocks[1].text.index("clocks")
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     _point_of(view, view._doc_pos(1, k)))
    assert got == [(1, k)]
    images = [p for p, _ in view._images]
    assert len(images) == 1
    fmt = view._cursor(images[0] + 1).charFormat().toImageFormat()
    assert 0 < fmt.width() <= view.viewport().width() - 40
    link = view._cursor(view._doc_pos(4, 2) + 1).charFormat()
    assert link.anchorHref() == "#ch2" and not link.hasProperty(link.Property.ForegroundBrush)
    image = view.viewport().grab().toImage()
    assert QColor(image.pixel(3, 3)) == QColor(themes.current().page)


def test_a_pdf_book_in_original_mode_falls_back_to_the_livrosound_layout(qapp):
    book = _sample_book()
    view = _view(qapp, book, mode="original")
    assert view._doc_pos(3, 0) == view._starts[3]
    assert _char_at(view, view._doc_pos(3, 0)) == book.blocks[3].text[0]


# ---------- speed ----------


def _big_book(blocks: int = 12000) -> Book:
    rng = random.Random(11)
    out = []
    html = ["<html><body>"]
    for i in range(blocks):
        if i % 40 == 0:
            text = f"Chapter {i // 40 + 1}"
            out.append(Block("heading", text, level=1))
            html.append(f'<a name="ls-b{i}"></a><h2>{text}</h2>\n')
        else:
            text = " ".join(_sentence(rng, rng.randint(8, 20)) for _ in range(rng.randint(1, 4)))
            out.append(Block("paragraph", text, styles=[(0, 3, "i")] if i % 9 == 0 else []))
            html.append(f'<a name="ls-b{i}"></a><p>{text}</p>\n')
    html.append("</body></html>")
    return Book("big", "Big", "X", "", "epub", blocks=out, original_html="".join(html))


def _highlight_painted(view: ReflowView) -> int:
    """How many sampled viewport pixels show the highlight color."""
    image = view.viewport().grab().toImage()
    color = QColor(view._theme.highlight)
    return sum(
        QColor(image.pixel(x, y)) == color for y in range(0, image.height(), 4) for x in range(0, image.width(), 4)
    )


def test_two_million_chars_open_fast(qapp):
    book = _big_book()
    assert sum(len(b.text) for b in book.blocks) > 1_800_000
    res = paths.book_dir(book.book_id) / "res"
    res.mkdir(parents=True)
    for name, (width, height) in {"cover.png": (1600, 2560), "plate.png": (1200, 300)}.items():
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(QColor("#444444"))
        image.save(str(res / name))
    # Big images up front: resizing them once Qt has started its lazy layout left
    # the next few hundred blocks unpainted.
    book.blocks[0:0] = [Block("image", "", src="cover.png", role="cover"), Block("image", "", src="plate.png")]
    view = _view(qapp, size=(1100, 800))
    start = time.perf_counter()
    view.set_book(book, "livrosound")
    qapp.processEvents()
    opened = time.perf_counter() - start
    start = time.perf_counter()
    view.scroll_to(9001, animate=False)
    jumped = time.perf_counter() - start
    print(f"\n2M-char book (LivroSound layout): set_book {opened:.2f} s, first jump to 75% {jumped:.2f} s")
    assert opened < 4.0 and jumped < 4.0  # ~0.3 s and ~0.5 s on an idle M1
    assert view.current_block() in range(8990, 9002)
    view.show_highlight(9001, 0, 25)
    top, bottom = view._span(*_selections(view)[0])
    assert 0 < top < bottom < view.viewport().height()
    assert _highlight_painted(view) > 20  # the text there is really painted
    view.set_book(book, "livrosound")
    qapp.processEvents()
    view.show_highlight(200, 0, 25)
    view.scroll_to(200, animate=False)
    assert _highlight_painted(view) > 20


@pytest.mark.slow
def test_two_million_chars_open_in_the_original_layout(qapp):
    book = _big_book()
    view = _view(qapp, size=(1100, 800))
    start = time.perf_counter()
    view.set_book(book, "original")
    opened = time.perf_counter() - start
    print(f"\n2M-char book (original layout): set_book {opened:.2f} s")
    assert opened < 8.0  # ~2 s on an idle M1
    assert view._matched.sum() > 0.95 * sum(len(b.text.replace(" ", "")) for b in book.blocks)
    view.scroll_to(9001, animate=False)
    assert view.current_block() in range(8990, 9002)


@pytest.mark.slow
@pytest.mark.parametrize("name", ["leo-tolstoy_anna-karenina.epub", "Wuthering Heights by Emily Bront\u00eb.epub"])
def test_real_epub_opens_in_both_layouts(qapp, name):
    path = EBOOKS / name
    epub = pytest.importorskip("livrosound.ingest.epub")
    if not path.is_file():
        pytest.skip(f"{name} not found")
    from livrosound.book import file_book_id

    try:
        book = epub.parse_epub(path, file_book_id(path))
    except Exception as error:  # the EPUB ingester is another module; this test is about the view
        pytest.skip(f"parse_epub failed on {name}: {error!r}")
    chars = sum(len(b.text) for b in book.blocks)
    view = _view(qapp, size=(1100, 800))
    for mode in ("livrosound", "original"):
        start = time.perf_counter()
        view.set_book(book, mode)
        qapp.processEvents()
        opened = time.perf_counter() - start
        rng = random.Random(0)
        spoken = [i for i, b in enumerate(book.blocks) if b.spoken]
        raw = view.document().toRawText()
        checked = exact = 0
        for i in rng.sample(spoken, min(300, len(spoken))):
            for k, ch in enumerate(book.blocks[i].text):
                if ch.isspace() or not view._matched[view._offsets[i] + k]:
                    continue
                checked += 1
                exact += raw[view._doc_pos(i, k)].lower() == ch.lower()
        matched = view._matched.sum() / max(1, sum(len(b.text.replace(" ", "")) for b in book.blocks))
        start = time.perf_counter()
        middle = spoken[len(spoken) * 3 // 4]
        view.scroll_to(middle, animate=False)
        jumped = time.perf_counter() - start
        print(f"\n{name} ({chars:,} chars, {len(book.blocks):,} blocks) {mode}: set_book {opened:.2f} s, "
              f"jump {jumped:.2f} s, matched {matched:.1%}")
        assert exact == checked
        assert view.current_block() <= middle
        if mode == "original":
            assert matched > 0.97
        assert opened < 10.0


# ---------- reading style and page views ----------


def _long_book() -> Book:
    rng = random.Random(11)
    return Book("long", "Long", "", "", "txt", blocks=[Block("heading", "One", level=1), *_paragraphs(rng, 120)])


def test_style_sets_type_spacing_and_justification(qapp):
    from livrosound.ui.reader_view import ReadingStyle

    view = _view(qapp, _sample_book())
    paragraph = next(i for i, b in enumerate(view._book.blocks) if b.kind == "paragraph")
    before = view.document().documentLayout().blockBoundingRect(view.document().findBlock(view._starts[paragraph]))
    view.set_style(ReadingStyle(weight=600, line_spacing=2.0, paragraph_spacing=1.0, justify=True, word_spacing=2.0))
    qapp.processEvents()
    block = view.document().findBlock(view._starts[paragraph])
    assert block.blockFormat().alignment() & Qt.AlignmentFlag.AlignJustify
    assert view.document().defaultFont().weight() == QFont.Weight(600)
    assert block.charFormat().fontWordSpacing() == 2.0 or block.begin().fragment().charFormat().fontWordSpacing() == 2.0
    after = view.document().documentLayout().blockBoundingRect(block)
    assert after.height() > before.height() * 1.2  # looser lines
    _check_positions(view)
    view.close()


def _check_positions(view: ReflowView) -> None:
    for i, block in enumerate(view._book.blocks):
        if block.kind not in ("image", "separator") and block.text:
            assert _char_at(view, view._doc_pos(i, 0)) == _displayed(block.text[0])


def test_margins_and_column_width(qapp):
    from livrosound.ui.reader_view import ReadingStyle

    view = _view(qapp, _sample_book(), size=(1200, 700))
    bar = view.verticalScrollBar().sizeHint().width()
    view.set_style(ReadingStyle(width=50))
    assert view._column == int((1200 - bar) * 0.5)
    margins = view.viewportMargins()
    assert abs(margins.left() - margins.right()) <= bar + 1  # centered
    view.set_style(ReadingStyle(margin_left=40, margin_right=400))
    margins = view.viewportMargins()
    assert margins.left() == 40 and margins.right() >= 400
    assert view._column == 1200 - bar - 440
    view.close()


def test_single_page_is_a_sheet_on_paper_and_turns_whole_pages(qapp):
    view = _view(qapp, _long_book(), size=(1000, 700))
    view.set_page_mode("single")
    qapp.processEvents()
    assert view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    [sheet] = view._sheets
    assert sheet.height() > 600 and sheet.width() < sheet.height() and abs(sheet.center().x() - 500) < 2
    height = view._page_height
    assert view.document().pageSize().height() == height and view.spread_step() == height
    assert view._column < sheet.width()  # the text sits inside the sheet
    assert view.verticalScrollBar().value() == 0
    view.page_step(1)
    assert view.verticalScrollBar().value() == height
    QTest.keyClick(view, Qt.Key.Key_PageDown)
    assert view.verticalScrollBar().value() == 2 * height
    QTest.keyClick(view, Qt.Key.Key_Up)
    assert view.verticalScrollBar().value() == height
    total = view.count_pages()
    assert total > 5 and view.page_info() == (2, 2, total)
    # Auto-follow turns to the page with the sentence rather than scrolling.
    last = len(view._book.blocks) - 1
    view.show_highlight(last, 0, 10)
    view.keep_in_view(last, 0, 10)
    assert view.verticalScrollBar().value() % height == 0
    assert view._page_of(view._doc_pos(last, 0)) in view._pages_shown()
    QTest.keyClick(view, Qt.Key.Key_Home)
    assert view.verticalScrollBar().value() == 0
    # No line is cut by a page edge.
    layout, doc = view.document().documentLayout(), view.document()
    block = doc.begin()
    while block.isValid():
        rect = layout.blockBoundingRect(block)
        lines = block.layout()
        for i in range(lines.lineCount()):
            top = rect.top() + lines.lineAt(i).y()
            assert int(top // height) == int((top + lines.lineAt(i).height() - 0.01) // height)
        block = block.next()
    # The paper around the sheet is drawn, and the page number at the bottom right.
    image = view.viewport().grab().toImage()
    assert image.pixelColor(5, 350) != QColor(themes.current().page)
    assert view._running_head() == "Long"  # no contents: the book's title
    view.close()


def test_double_page_paints_two_sheets_and_clicks_the_right_one(qapp):
    view = _view(qapp, _long_book(), size=(1400, 800))
    view.set_page_mode("double")
    qapp.processEvents()
    left_sheet, right_sheet = view._sheets
    assert left_sheet.right() == right_sheet.left()  # they meet at the spine
    height = view._page_height
    assert view.spread_step() == 2 * height
    view.page_step(1)
    assert view.verticalScrollBar().value() == 2 * height
    view.count_pages()
    first, last, _ = view.page_info()
    assert (first, last) == (3, 4)
    clicks = []
    view.clicked.connect(lambda block, char: clicks.append((block, char)))
    right = view._origins[1]
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(int(right.x() + view._column / 2), int(right.y() + 60)))
    assert clicks and view._page_of(view._doc_pos(*clicks[0])) == 3
    left = view._origins[0]
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(int(left.x() + view._column / 2), int(left.y() + 60)))
    assert view._page_of(view._doc_pos(*clicks[1])) == 2
    image = view.viewport().grab().toImage()
    page = QColor(themes.current().page)

    def inked(sheet) -> bool:
        x0, x1 = int(sheet.left() + 40), int(sheet.right() - 40)
        return any(image.pixelColor(x, y) != page for x in range(x0, x1, 3) for y in range(100, height - 40, 7))

    assert inked(left_sheet) and inked(right_sheet)
    view.set_page_mode("scroll")
    assert view.document().pageSize().height() < 0 and view.viewportMargins().left() > 0
    view.close()
