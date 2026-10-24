"""Text repair: dictionary, line joins, OCR fusions, headings, notices.

The run-together cases come from the real text layer of Artful_Sentences.pdf
(a scanned book with an old OCR layer); the "never split" cases are real words
and names that look like fusions ("island", "Atwood", "Socrates").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from livrosound.textutil import (
    HEADING_RE,
    PAGE_NUMBER_RE,
    TERMINAL,
    Vocab,
    fix_spaced_letters,
    is_word,
    join_mode,
    looks_like_boilerplate,
    normalize_chars,
    roman_to_int,
    title_case,
)

ARTFUL = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Ebooks/Artful_Sentences.pdf"


# ---------------------------------------------------------------------------
# Dictionary


@pytest.mark.parametrize(
    "word",
    [
        "sentence", "sentences", "modifiers", "running", "studies", "happiest", "wolves", "using", "flies",
        "earlier", "author's", "didn't", "can't", "has", "began", "became", "women", "held",
        "withdrew", "forgave", "online", "colour", "offence", "centre", "realise", "travelled", "fair-weather",
        "self-conceit", "Isabel", "Anton",
    ],
)
def test_is_word_accepts_words_inflections_and_irregulars(word):
    assert is_word(word)


@pytest.mark.parametrize("word", ["isthe", "asan", "xqzt", "ication", "lueless", "riations", "Tufte", "", "''"])
def test_is_word_rejects_fusions_tails_and_unknown_names(word):
    assert not is_word(word)


def test_is_word_trusts_the_books_own_words():
    vocab = Vocab.from_texts(["Virginia Tufte wrote Artful Sentences."])
    assert is_word("Tufte", vocab)
    assert is_word("tufte", vocab)


def test_vocab_counts_whole_words_only():
    vocab = Vocab.from_texts(["We met to-day at noon.\nThe inde-\npendent man was there", "and To-day again."])
    assert vocab.count("to-day") == 2  # case-insensitive, across texts
    assert vocab.count("inde") == 0  # broken at the line end
    assert vocab.count("pendent") == 0  # a lowercase line start may be a broken word's tail
    assert vocab.count("man") == 1
    assert vocab.count("missing") == 0


# ---------------------------------------------------------------------------
# Characters and lines


def test_normalize_chars():
    assert normalize_chars("ﬁne ﬂow é") == "fine flow é"  # ligatures; NFC
    assert normalize_chars("a b c　d") == "a b c d"  # odd spaces
    assert normalize_chars("﻿zero​width‍") == "zerowidth"
    assert normalize_chars("one\r\ntwo\rthree four") == "one\ntwo\nthree\nfour"
    assert normalize_chars("\tIndented") == "    Indented"


def test_normalize_chars_soft_hyphens():
    # Mid-word soft hyphens are invisible hints: removed.
    assert normalize_chars("wa­ter") == "water"
    # At a line end inside the text it marks a break: kept as "-" for join_mode.
    assert normalize_chars("independ­\nent") == "independ-\nent"
    assert normalize_chars("independ¬\nent") == "independ-\nent"  # OCR's ¬
    # At the very end of the string it is removed (the contract: no soft hyphens out).
    assert normalize_chars("end­") == "end"


@pytest.mark.parametrize(
    "line, fixed",
    [
        ("A R T F U L", "ARTFUL"),
        ("A R T F U L  S E N T E N C E S", "ARTFUL  SENTENCES"),
        ("N o w is the time", "Now is the time"),
        ("O n the hill", "On the hill"),
        ("C I C E R O spoke", "CICERO spoke"),
        ("w h o", "who"),
        ("a b", "a b"),  # two letters that aren't a word
        ("I a m", "I a m"),  # "Iam" is no word
        ("Plan B a", "Plan B a"),
    ],
)
def test_fix_spaced_letters(line, fixed):
    assert fix_spaced_letters(line) == fixed


# ---------------------------------------------------------------------------
# Joining lines


@pytest.mark.parametrize(
    "prev, nxt, mode",
    [
        # syllable breaks: the hyphen goes
        ("independ-", "ent", "drop-hyphen"),
        ("inalien-", "able", "drop-hyphen"),
        ("friend-", "ships", "drop-hyphen"),
        ("sen-", "tences", "drop-hyphen"),
        ("imme-", "diately", "drop-hyphen"),
        ("so-", "lution", "drop-hyphen"),
        ("ex-", "ample", "drop-hyphen"),
        ("in-", "to", "drop-hyphen"),  # "into", not a suspended hyphen
        ("pho-", "to", "drop-hyphen"),
        ("Toron-", "to", "drop-hyphen"),
        ("PHILOSO-", "PHY", "drop-hyphen"),
        ("ar-", "rang’d", "drop-hyphen"),  # "arranged"
        # compounds: the hyphen stays
        ("self-", "conceit", "join"),
        ("fair-", "weather", "join"),
        ("well-", "known", "join"),
        ("twenty-", "one", "join"),
        ("so-", "called", "join"),
        ("boundary-", "line", "join"),
        ("Anglo-", "Saxon", "join"),
        ("nineteenth-", "and twentieth-century", "join"),  # suspended
        ("pre-", "and post-war", "join"),
        ("1914-", "1918", "join"),
        # dashes
        ("friendship—", "what", "join"),
        ("friendship —", "what", "space"),
        ("How so?", "—and the dog", "join"),
        ("He said", "— and then", "space"),
        ("word--", "next", "join"),
        ("word --", "next", "space"),
        ("paper -", "- and", "space"),
        ("paper-", "-and", "join"),
        # the rest
        ("plain", "line", "space"),
        ("soft­", "ware", "drop-hyphen"),
        ("", "first line", "join"),
        ("last line", "", "join"),
    ],
)
def test_join_mode(prev, nxt, mode):
    assert join_mode(prev, nxt) == mode


def test_join_mode_follows_the_books_own_spelling():
    assert join_mode("to-", "day") == "drop-hyphen"  # "today" by the dictionary
    old_style = Vocab.from_texts(["We met to-day, and to-day was fine."])
    assert join_mode("to-", "day", old_style) == "join"
    modern = Vocab.from_texts(["The touchscreen froze; the touchscreen was new."])
    assert join_mode("touch-", "screen") == "join"  # web2 (1934) lacks "touchscreen"
    assert join_mode("touch-", "screen", modern) == "drop-hyphen"


# ---------------------------------------------------------------------------
# Headings, page numbers, titles, notices


@pytest.mark.parametrize(
    "line",
    [
        "CHAPTER IV", "Chapter 12", "Chapter 12.", "Book the First", "BOOK ONE", "PREFACE", "Introduction",
        "Table of Contents", "Appendix A", "Acknowledgments", "Epilogue.", "IV", "IV.", "XII", "PART I",
        "Part Two", "Chapter Twenty-One", "Chapter the Last", "Letter 4", "Canto III", "Stave One", "CHAPTER",
        "CHAPTER IV. THE STORM", "CHAPTER IV THE STORM", "Chapter 1: The Boy Who Lived",
        "CHAPTER I. Down the Rabbit-Hole",
    ],
)
def test_heading_re_matches_headings(line):
    assert HEADING_RE.match(line)


@pytest.mark.parametrize(
    "line",
    [
        "Part of the problem was that he never listened.",
        "Chapter 3 The results were clear and the",  # a wrapped prose line
        "In the beginning",
        "I went home.",
        "IV. The storm broke over the town and everyone ran",
        "civil", "MIX", "MD", "Chapter Mix",
        "CHAPTER " + "X" * 90,
    ],
)
def test_heading_re_ignores_prose(line):
    assert not HEADING_RE.match(line)


@pytest.mark.parametrize("line", ["14", "- 14 -", "— 7 —", "xii", "iv.", "[p. 3]", "p. 3", "Page 14", "(12)", "l4"])
def test_page_number_re_matches_page_numbers(line):
    assert PAGE_NUMBER_RE.match(line)


@pytest.mark.parametrize("line", ["I", "IV", "Il", "civil", "ill", "mix", "14 ARTFUL SENTENCES", "SHORT SENTENCES 19"])
def test_page_number_re_ignores_the_rest(line):
    assert not PAGE_NUMBER_RE.match(line)


def test_terminal_includes_closing_quotes():
    for ch in ".?!:;”’\")…":
        assert ch in TERMINAL
    assert "," not in TERMINAL
    assert "and.".endswith(TERMINAL) and not "and,".endswith(TERMINAL)


@pytest.mark.parametrize(
    "token, value",
    [("IV", 4), ("iv", 4), ("xlii", 42), ("XII.", 12), ("MCMLXXXIV", 1984), (" ix ", 9), ("I", 1), ("MMMCMXCIX", 3999)],
)
def test_roman_to_int(token, value):
    assert roman_to_int(token) == value


@pytest.mark.parametrize("token", ["", "IIII", "VX", "IC", "MMMM", "abc", "IV2", "."])
def test_roman_to_int_rejects_malformed(token):
    assert roman_to_int(token) is None


@pytest.mark.parametrize(
    "text, cased",
    [
        ("SHORT SENTENCES", "Short Sentences"),
        ("THE LORD OF THE RINGS", "The Lord of the Rings"),
        ("CHAPTER IV", "Chapter IV"),
        ("CHAPTER ONE: SHORT SENTENCES", "Chapter One: Short Sentences"),
        ("OF MICE AND MEN", "Of Mice and Men"),
        ("WHAT DREAMS ARE MADE OF", "What Dreams Are Made Of"),
        ("GONE WITH THE WIND", "Gone with the Wind"),
        ("WORLD WAR II", "World War II"),
        ("HENRY VIII", "Henry VIII"),
        ("MOTHER-IN-LAW", "Mother-in-Law"),
        ("O'NEILL AND D'ARCY", "O'Neill and D'Arcy"),
        ("DON'T LOOK NOW", "Don't Look Now"),
        ("THE 19TH CENTURY", "The 19th Century"),
        ("“THE DEAD”", "“The Dead”"),
        ("the lord of the rings", "The Lord of the Rings"),
        ("the iPhone age", "The iPhone Age"),  # inner capitals are kept
        ("  SPACED   OUT  ", "  Spaced   Out  "),  # whitespace is kept
        ("", ""),
    ],
)
def test_title_case(text, cased):
    assert title_case(text) == cased


@pytest.mark.parametrize(
    "line",
    [
        "Digitized by the Internet Archive",
        "in 2022 with funding from",
        "Kahle/Austin Foundation",
        "https://archive.org/details/artfulsentencessOO00Otuft",
        "Digitized by the Internet Archive in 2022 with funding from Kahle/Austin Foundation",
        "This page intentionally left blank",
        "[This page has been intentionally left blank]",
        "Digitized by Google",
        "Original from UNIVERSITY OF MICHIGAN",
        "This is a digital copy of a book that was preserved for generations on library shelves before it was",
        "Generated for jdoe (University of X) on 2021-03-03 14:32 GMT / https://hdl.handle.net/2027/x",
        "Public Domain, Google-digitized / http://www.hathitrust.org/access_use#pd-google",
        "www.edwardtufte.com",
        "Scanned with CamScanner",
        "This content downloaded from 128.1.2.3 on Mon, 01 Jan 2020 12:00:00 UTC",
    ],
)
def test_looks_like_boilerplate(line):
    assert looks_like_boilerplate(line)


@pytest.mark.parametrize(
    "line",
    [
        "The manuscript was digitized by volunteers in 2010 with funding from the state.",
        "He had funding from his uncle.",
        "Foundation",
        "The book was printed and bound in the United States of America.",
        "Visit archive.org for more.",
        "",
    ],
)
def test_looks_like_boilerplate_spares_prose(line):
    assert not looks_like_boilerplate(line)
