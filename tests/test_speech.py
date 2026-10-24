"""What the narrator says: block chunking (spans the reader highlights) and speakable text."""

import re
from pathlib import Path

import pytest

from livrosound.book import Block, Book
from livrosound.sentences import split_sentences
from livrosound.speech import (
    MAX_CHUNK_WORDS,
    MAX_SPOKEN_CHARS,
    SpeechOptions,
    block_chunks,
    is_read,
    lead_in_ms,
    speakable,
)

ARTFUL = (
    "ANTHONY BURGESS is right: it is the words that shine and sparkle and glitter, sometimes radiant "
    "with an author\u2019s inspired choice. But it is syntax that gives words the power to relate to each "
    "other in a sequence, to create rhythms and emphasis, to carry meaning\u2014of whatever kind\u2014as well "
    "as glow individually in just the right place."
)
WORD = re.compile(r"\S*[^\W_]\S*")
CURLY = "\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f"
SAMPLE_BOOKS = Path("/Users/stevemacario/Library/Mobile Documents/com~apple~CloudDocs/Ebooks")


def book_of(*blocks):
    return Book("b", "Title", "Author", "", "epub", blocks=list(blocks))


def chunks_of(block, options=None):
    return block_chunks(book_of(block), 0, options or SpeechOptions())


def assert_valid_chunks(book, options=SpeechOptions()):
    """The contract every chunk keeps: a trimmed, ordered, non-overlapping span of
    Block.text, at most MAX_CHUNK_WORDS words, voicing one clean ASCII line."""
    total = 0
    for i, block in enumerate(book.blocks):
        chunks = block_chunks(book, i, options)
        assert bool(chunks) == (is_read(block, options) and block.kind != "separator")
        end = 0
        for c in chunks:
            span = block.text[c.start:c.end]
            assert c.block == i
            assert span and span == span.strip(), (span, c)
            assert c.start >= end, "chunks overlap or are out of order"
            end = c.end
            assert len(WORD.findall(span)) <= MAX_CHUNK_WORDS, span
            assert c.text and c.text == c.text.strip(), c
            assert "\n" not in c.text and "  " not in c.text, c
            assert not any(q in c.text for q in CURLY), c
            assert all(ord(ch) < 128 or ch.isalpha() for ch in c.text), c
            assert c.pause_after_ms > 0
        total += len(chunks)
    return total


# ---------- speakable ----------


def test_artful_sentences_opening_reads_naturally():
    assert speakable(ARTFUL) == (
        "Anthony Burgess is right: it is the words that shine and sparkle and glitter, sometimes radiant "
        "with an author's inspired choice. But it is syntax that gives words the power to relate to each "
        "other in a sequence, to create rhythms and emphasis, to carry meaning, of whatever kind, as well "
        "as glow individually in just the right place."
    )


def test_typographic_punctuation_becomes_ascii():
    assert speakable("\u201cCan\u2019t,\u201d she said\u2026 \u2018Wait.\u2019 \u00abNon\u00bb") == (
        "\"Can't,\" she said... 'Wait.' \"Non\""
    )
    assert speakable("And so . . . it ends") == "And so... it ends"
    assert speakable("o\ufb03ce æon soft\u00adware") == "office aeon software"


def test_dashes_are_pauses():
    assert speakable("He said\u2014quietly\u2014no.") == "He said, quietly, no."
    assert speakable("Gutenberg -- style dash") == "Gutenberg, style dash"
    assert speakable("A spaced \u2013 en dash") == "A spaced, en dash"
    assert speakable("\u2014Yes, he said.") == "Yes, he said."
    assert speakable("PART II.\u2014THE ESCAPE") == "Part two. The Escape"
    assert speakable("the London\u2013Paris train") == "the London-Paris train"


def test_years_ranges_and_numbers():
    assert speakable("The war of 1914\u20131918 ended; in 1984 it was cold.") == (
        "The war of nineteen fourteen to nineteen eighteen ended; in nineteen eighty-four it was cold."
    )
    assert speakable("From 1914-18 and 2001.") == "From nineteen fourteen to eighteen and two thousand and one."
    assert speakable("It had 1,000,000 steps and 42 rooms.") == "It had one million steps and forty-two rooms."
    assert speakable("See page 1984.") == "See page one thousand nine hundred and eighty-four."
    assert speakable("Pi is 3.14 or .5 less.") == "Pi is three point one four or zero point five less."


def test_money_and_percents():
    assert speakable("It cost $5.50, £20 and €1.") == (
        "It cost five dollars and fifty cents, twenty pounds and one euro."
    )
    assert speakable("$0.99, £2.01 and $2 million") == (
        "ninety-nine cents, two pounds and one penny and two million dollars"
    )
    assert speakable("Up 50% or 3.5 per cent.") == "Up fifty percent or three point five percent."


def test_ordinals_decades_times_and_fractions():
    assert speakable("the 1st, 2nd, 3rd and 21st") == "the first, second, third and twenty-first"
    assert speakable("the 1920s, the '60s and 100s of 747s") == (
        "the nineteen twenties, the sixties and hundreds of seven hundred and forty-sevens"
    )
    assert speakable("At 10:30, 5:05 and 6:00.") == "At ten thirty, five oh five and six o'clock."
    assert speakable("2½ miles, 3/4 of it, 9/11") == "two and a half miles, three quarters of it, nine/eleven"
    assert speakable("It was 90°F.") == "It was ninety degrees Fahrenheit."


def test_digit_strings():
    assert speakable("Agent 007, call 555-123-4567.") == (
        "Agent zero zero seven, call five five five, one two three, four five six seven."
    )
    assert speakable("B52 and mp3") == "B fifty-two and mp three"


def test_roman_numerals():
    assert speakable("Chapter XIV") == "Chapter fourteen"
    assert speakable("CHAPTER XIV") == "Chapter fourteen"
    assert speakable("Act IV, Scene ii, Part I") == "Act four, Scene two, Part one"
    assert speakable("World War II") == "World War two"
    assert speakable("the book I read") == "the book I read"
    assert speakable("Henry VIII met Louis XIV and Elizabeth I.") == (
        "Henry the Eighth met Louis the Fourteenth and Elizabeth the First."
    )
    assert speakable("Henry VIII's wives") == "Henry the Eighth's wives"
    assert speakable("Tell John I said so.") == "Tell John I said so."
    assert speakable("Malcolm X spoke after Rocky II.") == "Malcolm X spoke after Rocky two."
    assert speakable("XIV.") == "Fourteen."
    assert speakable("see p. xii") == "see page twelve"


def test_abbreviations():
    assert speakable("Mr. Smith met Mrs. Jones, Dr. Watson and Mr Darcy.") == (
        "Mister Smith met Missus Jones, Doctor Watson and Mister Darcy."
    )
    assert speakable("Fruit, e.g. apples, i.e. red ones, etc.") == (
        "Fruit, for example apples, that is red ones, et cetera."
    )
    assert speakable("Apples, etc. Then pears.") == "Apples, et cetera. Then pears."
    assert speakable("See p. 12 and pp. 14\u201318, cf. vol. 3, No. 5.") == (
        "See page twelve and pages fourteen to eighteen, compare volume three, number five."
    )
    assert speakable("Roe v. Wade vs. Smith & Co.") == "Roe versus Wade versus Smith and Company."
    assert speakable("St. Paul lived on Baker St.") == "Saint Paul lived on Baker Street."
    assert speakable("a Ph.D. from § 12") == "a PhD from section twelve"


def test_urls_and_emails_are_dropped():
    assert speakable("Visit https://www.gutenberg.org/ebooks/123 or www.example.com today.") == "Visit or today."
    assert speakable("Write to info@gutenberg.org.") == "Write to."
    assert speakable("http://example.com") == ""


def test_note_marks_are_dropped():
    assert speakable("This is a note.[1] And another*. Daggers† too¹.") == (
        "This is a note. And another. Daggers too."
    )
    assert speakable("the road behind28 the house, the end.12 Next") == "the road behind the house, the end. Next"
    assert speakable("[12]") == ""
    assert speakable("* * *") == ""


def test_capitals():
    assert speakable("CHAPTER ONE") == "Chapter One"
    assert speakable("THE FBI FILES") == "The FBI Files"
    assert speakable("The FBI and the CIA watched TV in the USA.") == "The FBI and the CIA watched TV in the USA."
    assert speakable("He said NO, then SCREAMED.") == "He said NO, then Screamed."
    assert speakable("a sign saying THE END IS NEAR") == "a sign saying The End Is Near"
    assert speakable("O'BRIEN'S DOG DIDN'T BARK") == "O'Brien's Dog Didn't Bark"
    assert speakable("the BBC and CCTV, ADHD and NYPD") == "the BBC and CCTV, ADHD and NYPD"
    assert speakable("\u00c9COLE NORMALE") == "Ecole Normale"


def test_one_clean_line():
    assert speakable("  two\n lines\t here  ") == "two lines here"
    assert speakable("word , and ( spaced ) ;") == "word, and (spaced);"
    assert speakable("...") == ""
    assert speakable("") == ""


# ---------- is_read / lead_in_ms ----------


def test_what_gets_read():
    opts = SpeechOptions()
    assert is_read(Block("paragraph", "Hello."), opts)
    assert is_read(Block("separator", ""), opts)
    assert not is_read(Block("image", "", src="a.png"), opts)
    assert not is_read(Block("paragraph", "   "), opts)
    assert not is_read(Block("paragraph", "* * *"), opts)
    assert not is_read(Block("paragraph", "https://www.gutenberg.org"), opts)
    assert not is_read(Block("paragraph", "12", styles=[(0, 2, "sup")]), opts)
    assert is_read(Block("heading", "Chapter 1", level=1), opts)
    assert not is_read(Block("heading", "Chapter 1", level=1), SpeechOptions(read_headings=False))
    assert not is_read(Block("note", "1. A footnote."), opts)
    assert is_read(Block("note", "1. A footnote."), SpeechOptions(read_footnotes=True))


def test_lead_in():
    opts = SpeechOptions()
    assert lead_in_ms(Block("heading", "Part One", level=1), opts) == 1200
    assert lead_in_ms(Block("heading", "A Section", level=2), opts) == 800
    assert lead_in_ms(Block("separator", ""), opts) == 1200
    assert lead_in_ms(Block("paragraph", "Text."), opts) == 0
    assert lead_in_ms(Block("heading", "Part One", level=1), SpeechOptions(read_headings=False)) == 0


# ---------- block_chunks ----------


def test_artful_paragraph_chunks():
    book = book_of(Block("paragraph", ARTFUL))
    chunks = block_chunks(book, 0, SpeechOptions(paragraph_pause_ms=700))
    spans = [ARTFUL[c.start:c.end] for c in chunks]
    assert spans == [
        "ANTHONY BURGESS is right: it is the words that shine and sparkle and glitter, sometimes radiant "
        "with an author\u2019s inspired choice.",
        "But it is syntax that gives words the power to relate to each other in a sequence,",
        "to create rhythms and emphasis, to carry meaning\u2014of whatever kind\u2014as well as glow "
        "individually in just the right place.",
    ]
    assert [c.pause_after_ms for c in chunks] == [350, 150, 700]
    assert chunks[0].text.startswith("Anthony Burgess is right:")
    assert chunks[2].text == (
        "to create rhythms and emphasis, to carry meaning, of whatever kind, as well as glow "
        "individually in just the right place."
    )
    assert_valid_chunks(book)


def test_headings_read_in_normal_case_with_a_closing_period():
    book = book_of(Block("heading", "CHAPTER ONE", level=1), Block("heading", "Chapter XIV", level=1),
                   Block("heading", "XIV", level=2), Block("heading", "Short Sentences", level=2))
    texts = [block_chunks(book, i, SpeechOptions())[0] for i in range(4)]
    assert [c.text for c in texts] == ["Chapter One.", "Chapter fourteen.", "Fourteen.", "Short Sentences."]
    assert all(c.pause_after_ms == 900 for c in texts)


def test_long_sentence_is_cut_at_the_clause_nearest_its_middle():
    clause = "the river ran on past the mill and the bridge"  # 10 words
    text = f"At first, {clause}, {clause}, {clause}; {clause}."  # 42 words
    chunks = chunks_of(Block("paragraph", text))
    spans = [text[c.start:c.end] for c in chunks]
    assert spans == [f"At first, {clause}, {clause},", f"{clause}; {clause}."]
    assert [c.pause_after_ms for c in chunks] == [150, 600]


def test_cut_recurses_until_every_chunk_fits():
    text = ", ".join(f"word{i} alpha beta gamma" for i in range(40)) + "."  # 160 words
    book = book_of(Block("paragraph", text))
    chunks = block_chunks(book, 0, SpeechOptions())
    assert len(chunks) >= 6
    assert_valid_chunks(book)
    assert [c.pause_after_ms for c in chunks[:-1]] == [150] * (len(chunks) - 1)


def test_sentence_without_clause_punctuation_is_cut_between_words():
    words = ["x"] * 50
    words[23] = "and"
    text = " ".join(words) + "."
    chunks = chunks_of(Block("paragraph", text))
    assert [text[c.start:c.end] for c in chunks] == [" ".join(words[:23]), " ".join(words[23:]) + "."]
    assert [c.pause_after_ms for c in chunks] == [150, 600]
    # With no conjunction near the middle, the cut falls before the middle word.
    text = " ".join(["x"] * 70) + "."
    book = book_of(Block("paragraph", text))
    chunks = block_chunks(book, 0, SpeechOptions())
    assert [len(text[c.start:c.end].split()) for c in chunks] == [17, 18, 17, 18]
    assert_valid_chunks(book)


def test_long_spoken_text_is_cut_even_under_the_word_limit():
    # 24 words, but over MAX_SPOKEN_CHARS once said: Pocket-TTS would split it itself.
    text = (
        "Extraordinarily complicated administrative responsibilities overwhelmed the understaffed provincial "
        "headquarters, notwithstanding considerable governmental encouragement, throughout nineteen "
        "hundred and seventy-three."
    )
    assert len(text.split()) < MAX_CHUNK_WORDS and len(speakable(text)) > MAX_SPOKEN_CHARS
    chunks = chunks_of(Block("paragraph", text))
    assert len(chunks) == 2 and text[chunks[0].start:chunks[0].end].endswith("headquarters,")
    assert all(len(c.text) <= MAX_SPOKEN_CHARS for c in chunks)


def test_verse_is_one_chunk_per_line():
    poem = "Shall I compare thee to a summer\u2019s day?\n  Thou art more lovely and more temperate:\n\nRough winds do shake"
    book = book_of(Block("verse", poem))
    chunks = block_chunks(book, 0, SpeechOptions())
    assert [poem[c.start:c.end] for c in chunks] == [
        "Shall I compare thee to a summer\u2019s day?",
        "Thou art more lovely and more temperate:",
        "Rough winds do shake",
    ]
    assert [c.pause_after_ms for c in chunks] == [300, 300, 600]
    assert chunks[0].text == "Shall I compare thee to a summer's day?"
    assert chunks[-1].text == "Rough winds do shake."


def test_sup_note_marks_are_silent_but_spans_still_index_block_text():
    text = "It glows.12 Then it fades† away."
    block = Block("paragraph", text, styles=[(0, 2, "i"), (9, 11, "sup")])
    chunks = chunks_of(block)
    assert [text[c.start:c.end] for c in chunks] == ["It glows.", "Then it fades† away."]
    assert [c.text for c in chunks] == ["It glows.", "Then it fades away."]


def test_footnotes_are_read_only_when_asked_and_announced():
    note = Block("note", "1 Burgess, Joysprick, p. 12.")
    assert chunks_of(note) == []
    chunks = chunks_of(note, SpeechOptions(read_footnotes=True))
    assert [c.text for c in chunks] == ["Footnote. Burgess, Joysprick, page twelve."]
    assert note.text[chunks[0].start:chunks[0].end] == "Burgess, Joysprick, p. 12."
    chunks = chunks_of(Block("note", "* ibid., p. 3."), SpeechOptions(read_footnotes=True))
    assert [c.text for c in chunks] == ["Footnote. Ibid, page three."]


def test_unread_blocks_have_no_chunks():
    book = book_of(Block("separator", "* * *"), Block("image", "", src="x.png"),
                   Block("heading", "Chapter 1", level=1), Block("paragraph", "[1]"))
    assert block_chunks(book, 0, SpeechOptions()) == []
    assert block_chunks(book, 1, SpeechOptions()) == []
    assert block_chunks(book, 2, SpeechOptions(read_headings=False)) == []
    assert block_chunks(book, 3, SpeechOptions()) == []


def test_a_sentence_that_says_nothing_is_dropped_and_its_pause_kept():
    text = "Read it here. https://www.gutenberg.org/ebooks/1342"
    chunks = chunks_of(Block("paragraph", text))
    assert [c.text for c in chunks] == ["Read it here."]
    assert chunks[-1].pause_after_ms == 600


def test_realistic_prose_keeps_every_rule():
    paragraphs = [
        ARTFUL,
        "\u201cWell, Mr. Smith,\u201d said she, \u201cit\u2019s 1914\u20131918 all over again\u2026 isn\u2019t it?\u201d "
        "He paid $5.50 (or £20, about 3.5%) on the 21st\u2014e.g. for Chapter XIV, see p. 12.",
        "In the reign of Henry VIII, i.e. from 1509, the U.S.A. didn\u2019t exist; etc. See "
        "https://www.gutenberg.org/ or write to help@gutenberg.org.[3] THE END.",
        "*** START OF THE PROJECT GUTENBERG EBOOK ***",
        "It was a long, slow, winding sentence that went on and on through the valley, past the mill, "
        "over the bridge, under the old oak tree where we used to sit, and along the river bank until it "
        "reached the sea at last, tired and grey and quite done with the whole business of rivers.",
    ]
    book = book_of(*(Block("paragraph", p) for p in paragraphs),
                   Block("verse", "One line\nTwo line \u2014 three\n"), Block("heading", "BOOK THE FIRST", level=1))
    assert assert_valid_chunks(book) > 10
    # Every sentence is covered: each of its letters lies inside some chunk span.
    for i, block in enumerate(book.blocks[:3]):
        covered = set()
        for c in block_chunks(book, i, SpeechOptions()):
            covered.update(range(c.start, c.end))
        for a, b in split_sentences(block.text):
            letters = [k for k in range(a, b) if block.text[k].isalpha()]
            urls = [m.span() for m in re.finditer(r"\S+(?:://|@)\S+", block.text)]
            letters = [k for k in letters if not any(s <= k < e for s, e in urls)]
            assert set(letters) <= covered, block.text[a:b]


@pytest.mark.parametrize("name", [
    "Harvard Classics Daily/Day 001 - Franklin's Advice for the New Year.txt",
    "all_male_college.txt",
])
def test_sample_books_keep_every_rule(name):
    path = SAMPLE_BOOKS / name
    if not path.is_file():
        pytest.skip("sample book not on this machine")
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = [Block("paragraph", " ".join(p.split())) for p in re.split(r"\n\s*\n", raw) if p.strip()]
    assert assert_valid_chunks(book_of(*blocks)) > 100


def test_chunks_fit_pocket_tts_without_its_own_split():
    """Pocket-TTS splits any text over 50 tokens itself (joins we don't control); the
    word and spoken-length caps keep ordinary prose under that."""
    tokenizers = pytest.importorskip("tokenizers")
    pattern = "models--kyutai--pocket-tts-without-voice-cloning/snapshots/*/languages/english/tokenizer.json"
    found = sorted((Path.home() / ".cache/huggingface/hub").glob(pattern))
    if not found:
        pytest.skip("Pocket-TTS tokenizer not in the Hugging Face cache")
    tokenizer = tokenizers.Tokenizer.from_file(str(found[-1]))
    paragraphs = [ARTFUL] * 2
    franklin = SAMPLE_BOOKS / "Harvard Classics Daily/Day 001 - Franklin's Advice for the New Year.txt"
    if franklin.is_file():
        raw = franklin.read_text(encoding="utf-8-sig", errors="replace")
        paragraphs += [" ".join(p.split()) for p in re.split(r"\n\s*\n", raw) if p.strip()]
    book = book_of(*(Block("paragraph", p) for p in paragraphs))
    for i in range(len(book.blocks)):
        for c in block_chunks(book, i, SpeechOptions()):
            assert len(tokenizer.encode(c.text).ids) <= 50, c.text


# ---------- front/back matter and the title announcement ----------


def _book(blocks, title="Artful Sentences: Syntax as Style", author="Virginia Tufte"):
    from livrosound.book import Book

    return Book("id", title, author, "", "pdf", blocks=blocks)


def test_front_and_back_matter_is_never_read():
    from livrosound.book import SKIPPED_ROLES

    options = SpeechOptions()
    for role in SKIPPED_ROLES:
        block = Block("paragraph", "Copyright © 2006 by Virginia Tufte. All rights reserved.", role=role)
        assert not is_read(block, options), role
        assert block_chunks(_book([block]), 0, options) == []
        assert lead_in_ms(block, options) == 0


def test_title_is_announced_once_with_its_author():
    from livrosound.book import TITLE_ROLE

    title = Block("heading", "ARTFUL SENTENCES:", level=1, role=TITLE_ROLE)
    copy = Block("heading", "ARTFUL SENTENCES:", level=1, role="frontmatter")
    book = _book([title, copy])
    chunks = block_chunks(book, 0, SpeechOptions(read_headings=False))  # announced even with chapter titles off
    assert [c.text for c in chunks] == ["Artful Sentences: Syntax as Style, by Virginia Tufte."]
    assert (chunks[0].start, chunks[0].end) == (0, len("ARTFUL SENTENCES:"))
    assert block_chunks(book, 1, SpeechOptions()) == []


def test_title_announcement_without_an_author():
    from livrosound.book import TITLE_ROLE

    book = _book([Block("heading", "Walden", level=1, role=TITLE_ROLE)], title="Walden", author="")
    assert [c.text for c in block_chunks(book, 0, SpeechOptions())] == ["Walden."]
