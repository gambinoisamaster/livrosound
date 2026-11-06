"""Find in the book, the reader's highlights and notes, and Look Up's links."""

from __future__ import annotations

from livrosound import dictionary
from livrosound.annotations import COLORS, Annotations, text_between
from livrosound.book import Block, Book
from livrosound.search import FoldedBook, fold, nearest


def _book() -> Book:
    return Book("srch", "S", "", "", "txt", blocks=[
        Block("heading", "Café Society"),
        Block("paragraph", "She said “don’t” at the café — twice. Straße and Strasse."),
        Block("paragraph", "Another CAFE, and the end."),
    ])


def test_fold_keeps_one_char_per_char():
    text = "Café “don’t” — Straße"
    assert len(fold(text)) == len(text)
    assert fold(text) == "cafe \"don't\" - straße"


def test_find_ignores_case_accents_and_curly_quotes():
    folded = FoldedBook(_book())
    hits = [(m.block, m.start, m.end) for m in folded.find("cafe")]
    assert hits == [(0, 0, 4), (1, 24, 28), (2, 8, 12)]
    book = _book()
    assert [book.blocks[b].text[s:e] for b, s, e in hits] == ["Café", "café", "CAFE"]
    [quote] = folded.find("don't")
    assert book.blocks[1].text[quote.start:quote.end] == "don’t"
    assert folded.find("  ") == [] and folded.find("zzz") == []
    assert len(folded.find("a", limit=3)) == 3
    assert nearest(folded.find("cafe"), 1) == 1 and nearest(folded.find("cafe"), 9) == 0


def test_highlights_are_kept_per_book(tmp_path):
    book = _book()
    notes = Annotations("srch")
    assert notes.all() == []
    first = notes.add(book, (1, 0), (1, 8), "green")
    span = notes.add(book, (2, 8), (1, 24), note="Two cafés")  # given backwards: put in order
    assert span.start_block == 1 and span.end_block == 2 and span.color == "yellow"
    assert span.text == text_between(book, (1, 24), (2, 8)) and span.text.endswith("Another ")
    assert span.spans(book) == [(1, 24, len(book.blocks[1].text)), (2, 0, 8)]
    assert notes.at(1, 2).id == first.id and notes.at(2, 3).id == span.id and notes.at(0, 0) is None
    notes.update(first.id, color="pink", note="  hers  ")
    notes.update(first.id, color="not a color")
    again = Annotations("srch")
    assert [(a.id, a.color, a.note) for a in again.all()] == [(first.id, "pink", "hers"), (span.id, "yellow", "Two cafés")]
    again.remove(first.id)
    assert [a.id for a in Annotations("srch").all()] == [span.id]
    assert set(COLORS) == {"yellow", "green", "blue", "pink", "purple"}


def test_a_broken_annotations_file_is_empty(tmp_path):
    from livrosound import paths

    folder = paths.book_dir("broken")
    folder.mkdir(parents=True)
    (folder / "annotations.json").write_text('[{"start_block": "x"}, 3, {"start_block": 0, "start_char": 0, '
                                             '"end_block": 0, "end_char": 2, "color": "teal"}]')
    [item] = Annotations("broken").all()
    assert item.color == "yellow"
    (folder / "annotations.json").write_text("not json")
    assert Annotations("broken").all() == []


def test_look_up_links_and_offline_definition():
    assert dictionary.merriam_webster_url("Serendipity") == "https://www.merriam-webster.com/dictionary/serendipity"
    assert dictionary.oxford_url("ice cream").endswith("/ice-cream")
    assert dictionary.dictionary_app_url("café") == "dict://caf%C3%A9"
    if not dictionary.available():
        assert dictionary.define("word") is None
    assert dictionary.define("   ") is None
