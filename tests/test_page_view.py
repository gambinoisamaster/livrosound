"""The original page layout of PDF books (ui/page_view.py), on a synthetic PDF whose
words sit at known places, and (slow) on born-digital books from the Ebooks folder.

The highlight rules are the user's: one band per printed line, built only from the
sentence's own words, covering whole words, never over a running head."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pymupdf
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from livrosound.book import Block, Book, PageInfo, TocEntry
from livrosound.sentences import split_sentences
from livrosound.ui import page_view as pv
from livrosound.ui import theme as themes
from livrosound.ui.page_view import PageView, crop_boxes, line_bands
from livrosound.ui.theme import THEMES, stylesheet

PAGE_W, PAGE_H = 400.0, 600.0
LEFT = 60.0
HEAD = "10 ARTFUL SENTENCES"
# (page, block, text) in reading order; block None = the running head, which no block holds.
LINES = [
    (0, 0, "Chapter One"),
    (0, 1, "It was a bright cold day in April, and the clocks"),
    (0, 1, "were striking thirteen. Winston Smith, his chin"),
    (0, 1, "nuzzled into his breast in an effort to escape the"),
    (0, 1, "vile wind, slipped quickly through the glass doors."),
    (0, 2, "The hallway smelt of boiled cabbage and old rag"),
    (0, 2, "mats. At one end of it a coloured poster, too large"),
    (1, None, HEAD),
    (1, 2, "for indoor display, had been tacked to the wall."),
    (1, 2, "It depicted simply an enormous face, more than a"),
    (1, 2, "metre wide: the face of a man of about forty-five."),
    (1, 3, "Contents ........ 1"),
    (2, 4, "Winston made for the stairs. It was no use trying"),
    (2, 4, "the lift. Even at the best of times it was seldom"),
    (2, 4, "working, and at present the electric current was"),
    (2, 4, "cut off during daylight hours."),
]
CROSSING = "At one end of it a coloured poster, too large for indoor display, had been tacked to the wall."
EBOOKS = Path("/Users/stevemacario/Library/Mobile Documents/com~apple~CloudDocs/Ebooks")
# Where the slow test saves its screenshots.
SHOTS = Path(os.environ.get("LIVROSOUND_SHOTS") or Path(tempfile.gettempdir()) / "livrosound-shots")


def _drawn_lines():
    """(page, block, text, baseline) of every line, as drawn (the head sits higher)."""
    counts = [0, 0, 0]
    for page, block, text in LINES:
        if block is None:
            yield page, block, text, 60.0
        else:
            yield page, block, text, 110.0 + 22.0 * counts[page]
            counts[page] += 1


def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page(width=PAGE_W, height=PAGE_H)
    for page, block, text, baseline in _drawn_lines():
        doc[page].insert_text((LEFT, baseline), text, fontsize=9 if block is None else 13)
    doc.save(path)


def _book_from_pdf(path: Path) -> Book:
    """Blocks from the PDF's own word boxes: what the ingester hands the page view."""
    with pymupdf.open(path) as doc:
        pages = [PageInfo(page.rect.width, page.rect.height) for page in doc]
        printed = [page.get_text("words", sort=True) for page in doc]
    texts: dict[int, str] = {}
    words: dict[int, list] = {}
    for page, block, _, baseline in _drawn_lines():
        if block is None:
            continue
        for x0, y0, x1, y1, word, *_ in printed[page]:
            if y0 < baseline <= y1 + 1:
                start = len(texts[block]) + 1 if block in texts else 0
                texts[block] = f"{texts[block]} {word}" if block in texts else word
                words.setdefault(block, []).append((page, x0, y0, x1, y1, start, start + len(word)))
    blocks = [
        Block("heading" if i == 0 else "paragraph", texts[i], level=int(i == 0), page=words[i][0][0], words=words[i])
        for i in range(len(texts))
    ]
    blocks[3].link = 4
    blocks[3].role = "contents"
    body = [w for b in blocks for w in b.words]
    text_box = (min(w[1] for w in body), min(w[2] for w in body), max(w[3] for w in body), max(w[4] for w in body))
    return Book(
        "synthetic", "Synthetic", "Nobody", str(path), "pdf", blocks=blocks,
        toc=[TocEntry("Chapter One", 0)], pages=pages, text_box=text_box,
    )


def _wait_until(condition, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def _recorder(signal) -> list:
    seen = []
    signal.connect(lambda *args: seen.append(args))
    return seen


@pytest.fixture
def pdf_book(tmp_path) -> Book:
    path = tmp_path / "synthetic.pdf"
    _make_pdf(path)
    return _book_from_pdf(path)


@pytest.fixture
def view(qapp, pdf_book):
    themes.set_current(THEMES["original"])
    v = PageView()
    v.setStyleSheet(stylesheet(THEMES["original"]))
    v.resize(520, 400)
    v.show()
    v.set_book(pdf_book, "original")
    QApplication.processEvents()
    yield v
    v.shutdown()
    v.close()
    v.deleteLater()


def _word_box(book: Book, block: int, word: str, nth: int = 0):
    """(page, (x0, y0, x1, y1), start char) of the nth occurrence of a word in a block."""
    found = [w for w in book.blocks[block].words if book.blocks[block].text[w[5] : w[6]] == word]
    w = found[nth]
    return w[0], (w[1], w[2], w[3], w[4]), w[5]


def _page_words(book: Book, page: int) -> list[tuple[str, tuple]]:
    with pymupdf.open(book.source_path) as doc:
        return [(w[4], tuple(w[:4])) for w in doc[page].get_text("words")]


# ---------- geometry ----------


def test_one_band_per_line_from_first_to_last_word(pdf_book):
    block = pdf_book.blocks[1]
    start = block.text.index("were striking")
    end = block.text.index("glass doors") + len("glass doors")
    bands = line_bands(block.words, start, end)
    assert len(bands) == 3  # three printed lines
    for band, first, last in zip(bands, ("were", "nuzzled", "vile"), ("chin", "the", "doors.")):
        _, first_box, _ = _word_box(pdf_book, 1, first)
        _, last_box, _ = _word_box(pdf_book, 1, last, 1 if last == "the" else 0)
        page, box = band.page, band.box
        assert page == 0
        assert box[0] <= first_box[0] and box[2] >= last_box[2]
        assert box[0] > first_box[0] - 5 and box[2] < last_box[2] + 5  # padded, not stretched
        assert box[1] <= first_box[1] + 1 and box[3] >= first_box[3] - 1
    # Lines don't overlap: a gap is kept between their bands.
    assert all(a.box[3] < b.box[1] for a, b in zip(bands, bands[1:]))


def test_bands_cover_whole_words_never_half(pdf_book):
    """A range that starts and ends inside words ("dus[t ... impuritie]s") still covers both words whole."""
    block = pdf_book.blocks[1]
    start = block.text.index("bright") + 3
    end = block.text.index("April") + 2
    (band,) = line_bands(block.words, start, end)
    box = band.box
    _, bright, _ = _word_box(pdf_book, 1, "bright")
    _, april, _ = _word_box(pdf_book, 1, "April,")
    assert box[0] <= bright[0] and box[2] >= april[2]
    # ...and covers nothing of the neighbouring words.
    _, a, _ = _word_box(pdf_book, 1, "a")
    _, and_, _ = _word_box(pdf_book, 1, "and")
    assert box[0] > a[2] and box[2] < and_[0]


def test_padding_stops_halfway_to_the_next_word():
    """Tight spacing: a band's padding never reaches past the middle of the space to a
    word of the next sentence."""
    words = [
        (0, 10.0, 100.0, 40.0, 120.0, 0, 5),  # "Done."
        (0, 43.0, 100.0, 60.0, 120.0, 6, 9),  # "Yes"  (3 pt to its left, 2 pt to its right)
        (0, 62.0, 100.0, 90.0, 120.0, 10, 15),  # "next."
    ]
    (band,) = line_bands(words, 6, 9)
    assert band.box[0] == pytest.approx(41.5) and band.box[2] == pytest.approx(61.0)
    (band,) = line_bands(words, 0, 5)  # nothing before it on the line: the full padding
    assert band.box[0] == pytest.approx(10 - 0.14 * 20) and band.box[2] == pytest.approx(41.5)


def test_sentence_across_a_page_break_skips_the_running_head(view, pdf_book):
    block = pdf_book.blocks[2]
    start = block.text.index(CROSSING)
    view.show_highlight(2, start, start + len(CROSSING))
    rects = view.highlight_rects()
    assert [page for page, _ in rects] == [0, 1]  # "mats. At … too large" | "for indoor … wall."
    head = [box for word, box in _page_words(pdf_book, 1) if word in HEAD.split()]
    assert len(head) == 3
    for page, rect in rects:
        for box in head if page == 1 else []:
            assert not rect.intersects(pv.QRectF(QPointF(box[0], box[1]), QPointF(box[2], box[3])))
        # Every word on the page is either wholly inside the band's span or wholly outside it.
        for word, box in _page_words(pdf_book, page):
            middle = (box[1] + box[3]) / 2
            if not rect.top() <= middle <= rect.bottom():
                continue
            inside = rect.left() <= box[0] and box[2] <= rect.right()
            outside = box[2] <= rect.left() or box[0] >= rect.right()
            assert inside or outside, word
    # The first band starts at "At", not at the start of the line ("mats.").
    _, at_box, _ = _word_box(pdf_book, 2, "At")
    _, mats_box, _ = _word_box(pdf_book, 2, "mats.")
    assert rects[0][1].left() > mats_box[2] and rects[0][1].left() <= at_box[0]


def test_a_band_never_spills_onto_the_next_sentences_word(view, pdf_book):
    """"were striking thirteen. Winston …": the band ends before "Winston" (at most at
    the middle of the space), padding included."""
    block = pdf_book.blocks[1]
    start = block.text.index("were")
    end = block.text.index("thirteen.") + len("thirteen.")
    view.show_highlight(1, start, end)
    ((_, rect),) = view.highlight_rects()
    _, thirteen, _ = _word_box(pdf_book, 1, "thirteen.")
    _, winston, _ = _word_box(pdf_book, 1, "Winston")
    assert rect.right() >= thirteen[2] and rect.right() <= (thirteen[2] + winston[0]) / 2 + 0.01
    # The next sentence's band starts no earlier than that middle either.
    view.show_highlight(1, block.text.index("Winston"), block.text.index("chin") + 4)
    ((_, rect),) = view.highlight_rects()
    assert rect.left() >= (thirteen[2] + winston[0]) / 2 - 0.01


def test_a_line_sliced_by_the_crop_is_painted_over(qapp, pdf_book):
    """A running head just at the crop's edge would show in half: it is hidden instead."""
    tb = pdf_book.text_box
    head_top, head_bottom = next((b, d) for a, b, c, d, w, *_ in pymupdf.open(pdf_book.source_path)[1].get_text("words") if w == "ARTFUL")
    edge = (head_top + head_bottom) / 2  # put the crop's top edge through the head
    y0 = (edge + pv.CROP_MARGIN * tb[3]) / (1 + pv.CROP_MARGIN)
    pdf_book.text_box = (tb[0], y0, tb[2], tb[3])
    v = PageView()
    v.resize(520, 400)
    v.show()
    v.set_book(pdf_book, "original")
    assert v.crop_box(1)[1] == pytest.approx(edge, abs=0.5)
    v.verticalScrollBar().setValue(int(v.page_rect(1).top()) + v.verticalScrollBar().value() - 20)
    assert _wait_until(lambda: v.cached_image(1) is not None)
    image = v.cached_image(1)
    rows = int((head_bottom - edge) * image.height() / (v.crop_box(1)[3] - v.crop_box(1)[1])) + 2
    top = {image.pixelColor(x, y).lightness() for x in range(image.width()) for y in range(rows)}
    assert min(top) > 200  # paper only: no half letters
    v.shutdown()
    v.close()


def test_skipped_blocks_are_never_highlighted(view):
    view.show_highlight(3, 0, 8)  # the contents line
    assert view.highlight_rects() == []
    view.show_highlight(1, 0, 5)
    assert len(view.highlight_rects()) == 1
    view.clear_highlight()
    assert view.highlight_rects() == []


def test_crop_is_the_text_box_plus_a_margin_scaled_to_the_width(view, pdf_book):
    tb = pdf_book.text_box
    mx, my = (tb[2] - tb[0]) * pv.CROP_MARGIN, (tb[3] - tb[1]) * pv.CROP_MARGIN
    crop = view.crop_box(0)
    assert crop == pytest.approx((tb[0] - mx, tb[1] - my, tb[2] + mx, tb[3] + my))
    width = view.viewport().width()
    assert view.page_rect(0).width() == pytest.approx(width - 2 * pv.GUTTER, abs=1)
    assert view.horizontalScrollBar().maximum() == 0
    view.set_scale(1.5)
    assert view.page_rect(0).width() == pytest.approx(1.5 * (width - 2 * pv.GUTTER), abs=2)
    assert view.horizontalScrollBar().maximum() > 0
    view.set_scale(5)
    assert view.zoom() == pv.MAX_ZOOM
    view.set_scale(0.1)
    assert view.zoom() == pv.MIN_ZOOM
    # Pages follow each other with a gap, all the same size here.
    a, b = view.page_rect(0), view.page_rect(1)
    assert b.top() - a.bottom() == pytest.approx(pv.PAGE_GAP, abs=1.5)


def test_crop_clamps_to_the_page_and_falls_back_to_the_whole_page():
    book = Book("b", "B", "", "", "pdf", pages=[PageInfo(100, 200), PageInfo(100, 200)], text_box=(0, 0, 100, 200))
    assert crop_boxes(book) == [(0, 0, 100, 200)] * 2
    book.text_box = None
    assert crop_boxes(book) == [(0, 0, 100, 200)] * 2
    book.text_box = (10, 20, 90, 180)
    # Words past the usual text box (a footnote low on the page, a line set further right) grow that page's crop.
    crops = crop_boxes(book, {1: [(20, 185, 60, 195), (60, 100, 99, 110)]})
    assert crops[0][3] < 195 and crops[1][3] >= 195
    assert crops[0][2] < 99 and crops[1][2] == 100  # clamped to the page


def test_a_wider_page_is_scaled_to_fit_without_a_sideways_scroll(qapp, pdf_book):
    words = list(pdf_book.blocks[4].words)  # page 2: one line runs far past the text box
    w = words[-1]
    words[-1] = (w[0], w[1], w[2], PAGE_W - 5, w[4], w[5], w[6])
    pdf_book.blocks[4].words = words
    v = PageView()
    v.resize(520, 400)
    v.show()
    v.set_book(pdf_book, "original")
    QApplication.processEvents()
    assert v.crop_box(2)[2] > v.crop_box(0)[2]
    assert v.page_rect(2).width() <= v.viewport().width() - 2 * pv.GUTTER + 1
    assert v.page_rect(2).width() == pytest.approx(v.page_rect(0).width(), abs=1)
    assert v.horizontalScrollBar().maximum() == 0
    v.shutdown()
    v.close()


def test_click_maps_back_to_the_word(view, pdf_book):
    clicks = _recorder(view.clicked)
    for block, word in ((1, "cold"), (1, "thirteen."), (2, "poster,"), (2, "display,"), (4, "stairs.")):
        page, box, start = _word_box(pdf_book, block, word)
        view.scroll_to(block, start, animate=False)
        point = view.map_box(page, box).center().toPoint()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        assert clicks[-1] == (block, start), word
        # A click a few pixels off the word still hits it; one in the empty margin hits nothing.
        assert view.hit_test(point + QPoint(0, 4)) == (block, start)
    assert view.hit_test(QPoint(2, 2)) is None


def test_contents_lines_show_a_pointing_hand(view, pdf_book):
    page, box, start = _word_box(pdf_book, 3, "Contents")
    view.scroll_to(3, start, animate=False)
    point = view.map_box(page, box).center().toPoint()
    QTest.mouseMove(view.viewport(), point)
    assert view.viewport().cursor().shape() == Qt.CursorShape.PointingHandCursor
    page, box, start = _word_box(pdf_book, 2, "metre")
    QTest.mouseMove(view.viewport(), view.map_box(page, box).center().toPoint())
    assert view.viewport().cursor().shape() != Qt.CursorShape.PointingHandCursor


def _centered(view, rect) -> bool:
    bar = view.verticalScrollBar()
    if 0 < bar.value() < bar.maximum():
        return abs(rect.center().y() - view.viewport().height() / 2) <= 4  # (a page's ink offset may land mid-scroll)
    return 0 <= rect.top() and rect.bottom() <= view.viewport().height()  # clamped at an end, but on screen


def test_scroll_to_centers_a_block_or_in_a_tall_one_the_sentence(view, pdf_book):
    # A short block is centered whole.
    view.scroll_to(1, pdf_book.blocks[1].text.index("vile"), animate=False)
    whole = view._span([(w[0], w[1:5]) for w in pdf_book.blocks[1].words])
    assert _centered(view, whole)
    # A block too tall for that (it runs over a page break, in a short window): the line holding the char...
    view.resize(520, 220)
    QApplication.processEvents()
    block = pdf_book.blocks[2]
    whole = view._span([(w[0], w[1:5]) for w in block.words])
    assert pv.too_tall(int(whole.top()), int(whole.bottom()), view.viewport().height())
    page, box, start = _word_box(pdf_book, 2, "display,")
    view.scroll_to(2, start, animate=False)
    assert _centered(view, view.map_box(page, box))
    # ...or the sentence highlighted in it.
    start = block.text.index("It depicted")
    view.show_highlight(2, start, len(block.text))
    view.scroll_to(2, start, animate=False)
    assert _centered(view, view._span([(b.page, b.box) for b in view._bands]))
    # Animated, it gets there too.
    page, box, start = _word_box(pdf_book, 4, "cut")
    view.verticalScrollBar().setValue(0)
    view.scroll_to(4, start)
    whole = lambda: view._span([(w[0], w[1:5]) for w in pdf_book.blocks[4].words])  # noqa: E731
    assert _wait_until(lambda: view.scroll_target() == view.verticalScrollBar().value() and _centered(view, whole()), 3)


def test_keep_in_view_recenters_only_when_the_sentence_drifts(view, pdf_book):
    block = pdf_book.blocks[2]
    start = block.text.index("It depicted")
    end = len(block.text)
    view.scroll_to(2, start, animate=False)
    value = view.verticalScrollBar().value()
    view.keep_in_view(2, start, end)
    assert view.verticalScrollBar().value() == value  # already in the middle
    view.verticalScrollBar().setValue(0)
    view.keep_in_view(2, start, end)
    assert _wait_until(lambda: view.scroll_target() == view.verticalScrollBar().value(), 3)
    rects = [view.map_box(band.page, band.box) for band in line_bands(block.words, start, end)]
    middle = (rects[0].top() + rects[-1].bottom()) / 2
    assert middle == pytest.approx(view.viewport().height() / 2, abs=3)


def test_current_block_is_the_first_visible_near_the_top(view, pdf_book):
    view.verticalScrollBar().setValue(0)
    assert view.current_block() == 0
    for block, word in ((2, "depicted"), (3, "Contents")):
        page, box, _ = _word_box(pdf_book, block, word)
        target = view.map_box(page, box)
        view.verticalScrollBar().setValue(view.verticalScrollBar().value() + int(target.top()) - 2)
        assert view.map_box(page, box).top() == pytest.approx(2, abs=1)  # not clamped by the scroll range
        assert view.current_block() == block


def test_scrolling_by_hand_is_reported(view):
    scrolled = _recorder(view.user_scrolled)
    wheel = QWheelEvent(
        QPointF(50, 50), view.mapToGlobal(QPointF(50, 50)), QPoint(0, -40), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(view.viewport(), wheel)
    assert len(scrolled) == 1
    seen = len(scrolled)
    QTest.keyClick(view, Qt.Key.Key_End)
    assert len(scrolled) > seen and view.verticalScrollBar().value() == view.verticalScrollBar().maximum()
    seen = len(scrolled)
    QTest.keyClick(view, Qt.Key.Key_PageUp)
    assert len(scrolled) > seen
    seen = len(scrolled)
    view.scroll_to(1, 0, animate=False)  # the app scrolling is not the user
    assert len(scrolled) == seen


def test_pages_render_lazily_off_the_ui_thread(view):
    assert _wait_until(lambda: view.cached_image(0) is not None)
    image = view.cached_image(0)
    rect = view.page_rect(0)
    dpr = view.viewport().devicePixelRatioF()
    assert abs(image.width() - rect.width() * dpr) <= 1 and abs(image.height() - rect.height() * dpr) <= 1
    # Ink on white paper, as printed in the Original theme.
    colors = {image.pixelColor(x, y).name() for x in range(0, image.width(), 3) for y in range(0, image.height(), 3)}
    assert "#ffffff" in colors and len(colors) > 4
    # A far page is not rendered until it is scrolled to.
    assert view.cached_image(2) is None or view.verticalScrollBar().maximum() < view.page_rect(2).top()
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    assert _wait_until(lambda: view.cached_image(2) is not None)


def test_zoom_rerenders_sharp_pages_and_keeps_the_place(view, pdf_book):
    assert _wait_until(lambda: view.cached_image(0) is not None)
    page, box, start = _word_box(pdf_book, 1, "nuzzled")
    view.scroll_to(1, start, animate=False)
    before = view.map_box(page, box).center().y()
    view.set_scale(1.4)
    assert view.cached_image(0) is None  # stale until the debounced render
    assert view.map_box(page, box).center().y() == pytest.approx(before, abs=view.viewport().height() * 0.2)
    assert _wait_until(lambda: view.cached_image(0) is not None)
    assert view.cached_image(0).width() == pytest.approx(view.page_rect(0).width(), abs=1)


def test_a_huge_zoom_renders_softer_but_still_fills_the_page(view, monkeypatch):
    monkeypatch.setattr(pv, "MAX_PAGE_PIXELS", 40_000)
    view.set_scale(2.0)
    view._sharpen()
    assert _wait_until(lambda: view.cached_image(0) is not None)
    view.verticalScrollBar().setValue(0)
    view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum())
    image, rect = view.cached_image(0), view.page_rect(0)
    assert image.width() * image.height() <= 41_000 and image.width() < rect.width()
    shot = view.grab().toImage()
    dpr = shot.devicePixelRatio()
    # The page's white paper reaches its far corner (in its top margin), not a strip of it.
    corner = shot.pixelColor(int((min(rect.right(), view.viewport().width()) - 4) * dpr), int((rect.top() + 3) * dpr))
    assert corner == QColor("#ffffff")


def test_dark_theme_tones_the_page(view):
    dark = THEMES["dark"]
    themes.set_current(dark)
    view.setStyleSheet(stylesheet(dark))
    view.set_theme(dark)
    assert _wait_until(lambda: view.cached_image(0) is not None)
    image = view.cached_image(0)
    corner = image.pixelColor(2, 2)
    assert corner == QColor(dark.page)  # the paper takes the theme's page color
    shot = view.grab().toImage()
    assert shot.pixelColor(2, 2) == QColor(dark.window)
    themes.set_current(THEMES["original"])


def test_highlight_is_blended_onto_the_page(view, pdf_book):
    assert _wait_until(lambda: view.cached_image(0) is not None)
    page, box, start = _word_box(pdf_book, 1, "cold")
    view.scroll_to(1, start, animate=False)
    view.show_highlight(1, start, start + 4)
    QApplication.processEvents()
    shot = view.grab().toImage()
    page, rect = view.highlight_rects()[0]
    band = view.map_box(page, rect.getCoords())
    dpr = shot.devicePixelRatio()
    paper = shot.pixelColor(int((band.left() + 1) * dpr), int(band.center().y() * dpr))
    assert paper == QColor(THEMES["original"].highlight)  # white paper multiplied by the highlighter
    ink = min(
        (shot.pixelColor(int(x * dpr), int(band.center().y() * dpr)) for x in range(int(band.left()), int(band.right()))),
        key=lambda c: c.lightness(),
    )
    assert ink.lightness() < 60  # the letters under the band stay dark


def _blend(paper: QColor, band: QColor, mode) -> QColor:
    if mode == pv.QPainter.CompositionMode.CompositionMode_Multiply:
        return QColor(*(round(a * b / 255) for a, b in zip(paper.getRgb()[:3], band.getRgb()[:3])))
    return QColor(*(round(255 - (255 - a) * (255 - b) / 255) for a, b in zip(paper.getRgb()[:3], band.getRgb()[:3])))


def test_bands_turn_the_paper_the_themes_highlight_color():
    """Exactly on white or black paper; as near as a blend can get on the others
    (multiply only darkens, screen only lightens), and the letters keep their contrast."""
    for key in ("original", "dark", "calm", "quiet"):
        t = THEMES[key]
        color, mode = pv.band_blend(t, as_printed=False)
        paper = QColor("#FFFFFF") if pv.page_tone(t) is None else QColor(t.page)
        got, want = _blend(paper, color, mode), QColor(t.highlight)
        miss = [abs(a - b) for a, b in zip(got.getRgb()[:3], want.getRgb()[:3])]
        if key in ("original", "dark"):
            assert max(miss) <= 2, key
        else:
            before = [abs(a - b) for a, b in zip(paper.getRgb()[:3], want.getRgb()[:3])]
            assert sum(miss) < 0.6 * sum(before), key
        ink = _blend(QColor(t.text), color, mode)
        assert abs(ink.lightness() - QColor(t.text).lightness()) < 40, key
    # A page kept as printed (light paper) gets the light highlighter even in Dark.
    color, mode = pv.band_blend(THEMES["dark"], as_printed=True)
    assert _blend(QColor("#FFFFFF"), color, mode) == QColor(THEMES["original"].highlight)


def test_hiding_the_view_gives_the_images_back(view):
    assert _wait_until(lambda: view.cached_image(0) is not None)
    view.hide()
    assert view.cached_image(0) is None
    view.show()
    assert _wait_until(lambda: view.cached_image(0) is not None)


def _long_book(path: Path, count: int = 30) -> Book:
    """A book of `count` pages, one paragraph per page."""
    doc = pymupdf.open()
    for n in range(count):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        for k in range(12):
            page.insert_text((LEFT, 110 + 22 * k), f"Page {n} line {k} says something worth reading.", fontsize=13)
    doc.save(path)
    blocks = []
    with pymupdf.open(path) as doc:
        for page in doc:
            text, spans = "", []
            for x0, y0, x1, y1, word, *_ in page.get_text("words", sort=True):
                start = len(text) + 1 if text else 0
                text = f"{text} {word}" if text else word
                spans.append((page.number, x0, y0, x1, y1, start, start + len(word)))
            blocks.append(Block("paragraph", text, page=page.number, words=spans))
    body = [w for b in blocks for w in b.words]
    text_box = (min(w[1] for w in body), min(w[2] for w in body), max(w[3] for w in body), max(w[4] for w in body))
    pages = [PageInfo(PAGE_W, PAGE_H)] * count
    return Book("long", "Long", "", str(path), "pdf", blocks=blocks, pages=pages, text_box=text_box)


def test_a_long_jump_renders_where_the_view_is_heading_first(qapp, tmp_path):
    """A Contents pick far away: the destination's previews are asked for first, then
    those pages sharp, and the view never lands on blank pages."""
    v = PageView()
    v.resize(520, 400)
    v.show()
    v.set_book(_long_book(tmp_path / "long.pdf"), "original")
    assert _wait_until(lambda: v.cached_image(0) is not None)
    asked = _recorder_of(v._render.worker, "set_jobs")
    v.scroll_to(25, 0)  # animated
    heading = list(v._visible_pages(v.scroll_target()))
    assert 25 in heading and 0 not in heading
    jobs = asked[0]
    assert jobs[0].key.preview and jobs[0].page in heading
    sharp = [j for j in jobs if not j.key.preview]
    assert sharp[0].page in heading and {j.page for j in jobs[: 2 * len(heading)]} <= set(heading)
    assert _wait_until(lambda: v.scroll_target() == v.verticalScrollBar().value(), 3)
    assert all(v._shown(p, v._key()) is not None for p in v._visible_pages())  # landed on pages, not placeholders
    assert _wait_until(lambda: all(v.cached_image(p) is not None for p in v._visible_pages()))
    v.shutdown()
    v.close()


def _recorder_of(worker, name: str) -> list:
    """Every argument list a worker method is called with (the call still goes through)."""
    seen = []
    method = getattr(worker, name)

    def spy(jobs):
        seen.append(list(jobs))
        method(jobs)

    setattr(worker, name, spy)
    return seen


def test_a_page_seen_before_is_never_blank_again(view):
    """Every sharp render leaves a preview: when the sharp image is gone (evicted, or
    being re-rendered at a new zoom), the page shows the preview, not a blank page."""
    assert _wait_until(lambda: view.cached_image(0) is not None and view.preview_image(0) is not None)
    view._cache.clear()
    view.verticalScrollBar().setValue(0)
    shot = view.grab().toImage()
    rect, dpr = view.page_rect(0), shot.devicePixelRatio()
    colors = {
        shot.pixelColor(int(x * dpr), int(y * dpr)).lightness()
        for x in range(int(rect.left()) + 2, int(rect.right()) - 2, 2)
        for y in range(int(rect.top()) + 2, int(min(rect.bottom(), view.viewport().height())) - 2, 2)
    }
    assert min(colors) < 128  # ink on the page


def test_a_new_book_replaces_the_old(view, pdf_book, tmp_path):
    assert _wait_until(lambda: view.cached_image(0) is not None)
    other = tmp_path / "other.pdf"
    doc = pymupdf.open()
    doc.new_page(width=300, height=300).insert_text((50, 100), "Only page", fontsize=20)
    doc.save(other)
    book = Book("o", "O", "", str(other), "pdf", blocks=[Block("paragraph", "Only page", page=0)], pages=[PageInfo(300, 300)])
    view.set_book(book, "original")
    assert view.highlight_rects() == [] and view.cached_image(0) is None
    assert _wait_until(lambda: view.cached_image(0) is not None)
    assert view.current_block() == 0
    view.scroll_to(0, 0, animate=False)  # a block without word boxes: its page


def test_a_missing_file_leaves_placeholders(qapp, pdf_book):
    pdf_book.source_path = "/nonexistent/book.pdf"
    v = PageView()
    v.resize(400, 300)
    v.show()
    v.set_book(pdf_book, "original")
    QApplication.processEvents()
    assert v.grab().width() > 0 and v.cached_image(0) is None
    v.shutdown()
    v.close()


# ---------- the real book (slow) ----------

DIGITAL = {
    "hill_house": (EBOOKS / "The Haunting of Hill House by Shirley Jackson.pdf", range(0, 40)),
    "secret_history": (EBOOKS / "The Secret History by Donna Tartt (z-lib.org).epub.pdf", range(0, 200)),
    "elements_of_style": (EBOOKS / "THE ELEMENTS OF STYLE UPDATED 2011 EDITION William.pdf", range(0, 30)),
}


def _digital_book(path: Path, pages: range) -> Book:
    """A born-digital PDF as the ingester makes it, else a quick one from the text
    layer's own blocks."""
    try:
        from livrosound.ingest import pdf as ingest_pdf

        return ingest_pdf.parse_pdf(path, "digital", pages=pages)
    except Exception:
        return _quick_book(path, pages)


def _quick_book(path: Path, pages: range) -> Book:
    """One block per text-layer block, the top and bottom 7% of each page (running
    heads, page numbers) left out."""
    blocks: list[Block] = []
    with pymupdf.open(path) as doc:
        infos = [PageInfo(p.rect.width, p.rect.height) for p in doc]
        for number in pages:
            page = doc[number]
            groups: dict[int, list] = {}
            for x0, y0, x1, y1, word, b, *_ in page.get_text("words", sort=True):
                if 0.07 * page.rect.height < y0 and y1 < 0.93 * page.rect.height:
                    groups.setdefault(b, []).append((x0, y0, x1, y1, word))
            for ws in groups.values():
                text, spans = "", []
                for x0, y0, x1, y1, word in ws:
                    start = len(text) + 1 if text else 0
                    text = f"{text} {word}" if text else word
                    spans.append((number, x0, y0, x1, y1, start, start + len(word)))
                blocks.append(Block("paragraph", text, page=number, words=spans))
    body = [w for b in blocks for w in b.words]
    text_box = (min(w[1] for w in body), min(w[2] for w in body), max(w[3] for w in body), max(w[4] for w in body))
    return Book("digital", path.stem, "", str(path), "pdf", blocks=blocks, pages=infos, text_box=text_box)


def _shoot(view: PageView, name: str) -> Path:
    QApplication.processEvents()
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / name
    view.grab().save(str(path))
    return path


def _settle(view: PageView) -> None:
    _wait_until(lambda: view.scroll_target() == view.verticalScrollBar().value(), 5)
    _wait_until(lambda: all(view.cached_image(p) is not None for p in view._visible_pages()), 20)


def _check_bands(view: PageView, book: Book, block: int, start: int, end: int) -> None:
    """The user's rules on the printed page: no word that isn't the sentence's own
    (a running head, a page number, the next sentence) is touched by a band, and
    every word a band touches is inside it whole."""
    own = [w for w in book.blocks[block].words if w[5] < end and w[6] > start]
    with pymupdf.open(book.source_path) as doc:
        for page, rect in view.highlight_rects():
            for x0, y0, x1, y1, word, *_ in doc[page].get_text("words"):
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                mine = any(w[0] == page and w[1] - 1 <= cx <= w[3] + 1 and w[2] - 1 <= cy <= w[4] + 1 for w in own)
                inner = pv.QRectF(QPointF(x0 + 0.5, y0 + 0.5), QPointF(x1 - 0.5, y1 - 0.5))
                if not mine:
                    assert not rect.intersects(inner), f"band over {word!r} on page {page}"
                elif rect.top() <= cy <= rect.bottom():
                    assert rect.left() <= x0 + 0.01 and x1 - 0.01 <= rect.right(), f"{word!r} cut"


@pytest.mark.slow
@pytest.mark.parametrize("name", sorted(DIGITAL))
def test_digital_pdfs_screenshots(qapp, name):
    path, pages = DIGITAL[name]
    if not path.is_file():
        pytest.skip(f"{path.name} is not on this Mac")
    book = _digital_book(path, pages)
    crossing = [
        (i, s, e)
        for i, b in enumerate(book.blocks)
        if not b.skipped
        for s, e in split_sentences(b.text)
        if len({w[0] for w in b.words if w[5] < e and w[6] > s}) == 2
    ]
    assert crossing, "no sentence runs over a page break in these pages"
    block, start, end = crossing[0]
    for key in ("original", "dark", "calm"):
        t = THEMES[key]
        themes.set_current(t)
        v = PageView()
        v.setStyleSheet(stylesheet(t))
        v.set_theme(t)
        v.resize(1100, 780)
        v.show()
        started = time.monotonic()
        v.set_book(book, "original")
        first_page = book.blocks[block].words[0][0]
        v.show_highlight(block, start, end)
        v.scroll_to(block, start, animate=False)
        assert _wait_until(lambda: v.cached_image(first_page) is not None, 20)
        print(f"{name} {key}: page on screen {(time.monotonic() - started) * 1000:.0f} ms after set_book")
        _settle(v)
        assert {p for p, _ in v.highlight_rects()} == {w[0] for w in book.blocks[block].words if w[5] < end and w[6] > start}
        _check_bands(v, book, block, start, end)
        for b, (s, e) in [(b, se) for b, bl in enumerate(book.blocks[:200]) if not bl.skipped for se in split_sentences(bl.text)[:2]]:
            v.show_highlight(b, s, e)
            _check_bands(v, book, b, s, e)
        v.show_highlight(block, start, end)
        _shoot(v, f"digital_{name}_{key}.png")
        if key == "original" and name == "secret_history":
            # A long jump (a Contents pick): previews land with the scroll, sharp pages soon after.
            far = next(i for i, b in enumerate(book.blocks) if b.words and b.words[0][0] >= 150)
            started = time.monotonic()
            v.scroll_to(far, 0)
            heading = v._visible_pages(v.scroll_target())
            assert _wait_until(lambda: all(v._shown(p, v._key()) is not None for p in heading), 2)
            shown_ms = (time.monotonic() - started) * 1000
            assert _wait_until(lambda: all(v.cached_image(p) is not None for p in heading), 5)
            sharp_ms = (time.monotonic() - started) * 1000
            assert _wait_until(lambda: v.scroll_target() == v.verticalScrollBar().value(), 2)
            print(f"jump to page {book.blocks[far].words[0][0]}: preview {shown_ms:.0f} ms, sharp {sharp_ms:.0f} ms (scroll 450 ms)")
            assert shown_ms < 450  # before the scroll animation ends
            _shoot(v, "digital_secret_history_after_jump.png")
        v.shutdown()
        v.close()
    themes.set_current(THEMES["original"])


# ---------- single and double page ----------


def test_single_and_double_page_show_whole_pages_and_turn_them(view, pdf_book, qapp):
    pages = len(pdf_book.pages)
    assert view.page_info()[2] == pages
    view.set_page_mode("single")
    qapp.processEvents()
    height = view.viewport().height()
    assert view.spread_step() == height and list(view._visible_pages()) == [0]
    rect = view.page_rect(0)
    assert rect.top() >= 0 and rect.bottom() <= height and rect.right() <= view.viewport().width()  # whole page
    view.page_step(1)
    assert view.verticalScrollBar().value() == height and view.page_info() == (2, 2, pages)
    view.set_page_mode("double")
    qapp.processEvents()
    assert list(view._visible_pages()) == [0, 1] or list(view._visible_pages()) == [2, 3]
    view.turn_to(0)
    left, right = view.page_rect(0), view.page_rect(1)
    assert left.right() < right.left() and abs(left.top() - right.top()) < height / 4
    assert view.page_info() == (1, 2, pages)
    # A word on the right page is clicked like any other.
    block = next(i for i, b in enumerate(pdf_book.blocks) if b.words and b.words[0][0] == 1)
    word = pdf_book.blocks[block].words[0]
    point = view.map_box(1, word[1:5]).center().toPoint()
    assert view.hit_test(point)[0] == block
    # Auto-follow turns to the spread holding the sentence.
    far = max(i for i, b in enumerate(pdf_book.blocks) if b.words)
    view.show_highlight(far, 0, 5)
    view.keep_in_view(far, 0, 5)
    assert pdf_book.blocks[far].words[0][0] in view._visible_pages()
    view.set_page_mode("scroll")
    assert view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOn


def test_margins_move_the_pages(view, qapp):
    from livrosound.ui.reader_view import ReadingStyle

    before = view.page_rect(0)
    view.set_style(ReadingStyle(margin_left=200, margin_right=24))
    qapp.processEvents()
    after = view.page_rect(0)
    assert after.left() >= 200 and after.width() < before.width()
