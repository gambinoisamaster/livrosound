"""Sentence splitting: the unit LivroSound voices and highlights."""

from livrosound.sentences import split_sentences


def spans_text(text):
    return [text[a:b] for a, b in split_sentences(text)]


# ---------- ported from Script Radio ----------


def test_splits_on_sentence_ends():
    assert spans_text("Mrs. Carroll? Mrs. Carroll, it's Mare Sheehan. I'm standin' outside. In the cold.") == [
        "Mrs. Carroll?", "Mrs. Carroll, it's Mare Sheehan.", "I'm standin' outside.", "In the cold.",
    ]


def test_abbreviations_initials_and_trailing_ellipses_dont_split():
    assert spans_text("Det. Sheehan meets Dr. Ruiz and J. R. Ewing at the U.S. Mint.") == [
        "Det. Sheehan meets Dr. Ruiz and J. R. Ewing at the U.S. Mint."
    ]
    assert spans_text("Wait... what?") == ["Wait... what?"]
    assert spans_text("So... I don't know.") == ["So...", "I don't know."]


def test_spans_are_offsets_into_the_given_range():
    text = "For real? (beat) Not too out there? No."
    spans = split_sentences(text, 17, len(text))
    assert [text[a:b] for a, b in spans] == ["Not too out there?", "No."]


def test_quotes_close_the_sentence():
    assert spans_text('She reads the sign: "No entry." Mare goes in.') == [
        'She reads the sign: "No entry."', "Mare goes in.",
    ]


# ---------- books ----------


def test_artful_sentences_opening():
    text = (
        "ANTHONY BURGESS is right: it is the words that shine and sparkle and glitter, sometimes radiant "
        "with an author\u2019s inspired choice. But it is syntax that gives words the power to relate to each "
        "other in a sequence, to create rhythms and emphasis, to carry meaning\u2014of whatever kind\u2014as well "
        "as glow individually in just the right place."
    )
    spans = spans_text(text)
    assert len(spans) == 2
    assert spans[0].endswith("inspired choice.") and spans[1].startswith("But it is syntax")


def test_curly_quotes_and_guillemets_close_and_open_sentences():
    assert spans_text("\u201cStop.\u201d She ran. \u2018Why?\u2019 \u00abNon.\u00bb Fin.") == [
        "\u201cStop.\u201d", "She ran.", "\u2018Why?\u2019", "\u00abNon.\u00bb", "Fin.",
    ]


def test_dialogue_tags_stay_with_their_sentence():
    assert spans_text("\u201cStop!\u201d he cried. \u201cWhy?\u201d asked John.") == [
        "\u201cStop!\u201d he cried.", "\u201cWhy?\u201d asked John.",
    ]


def test_book_abbreviations_dont_split():
    for text in (
        "See Burgess, cf. Ch. 3 and pp. 12 to 15 of Vol. II for more.",
        "He wrote a Ph.D. thesis on it, trans. Smith, 2nd ed. London, 1990.",
        "Write to Smith Bros. Inc. about it, e.g. Mr. Jones or Gen. Lee of St. Louis.",
        "It was approx. three feet tall, viz. the size of a child, esp. in winter.",
        "Smith et al. Wrote this, and it is cited in ibid. Too.",
    ):
        assert spans_text(text) == [text], text


def test_reference_words_hold_only_before_a_number():
    assert spans_text("Read No. 5 and fig. 3 and Art. IV today.") == ["Read No. 5 and fig. 3 and Art. IV today."]
    assert spans_text("\u201cNo. I won\u2019t.\u201d He ate a fig. It was ripe. Chap. IV begins.") == [
        "\u201cNo.", "I won\u2019t.\u201d", "He ate a fig.", "It was ripe.", "Chap. IV begins.",
    ]


def test_ed_is_an_abbreviation_only_in_lowercase():
    assert spans_text("See the 2nd ed. London printing. I saw Ed. He waved.") == [
        "See the 2nd ed. London printing.", "I saw Ed.", "He waved.",
    ]


def test_I_ends_a_sentence_unless_an_initial_follows():
    assert spans_text("So did I. Then we left. I. M. Pei built it.") == [
        "So did I.", "Then we left.", "I. M. Pei built it.",
    ]


def test_numbers_and_gutenberg_italics_start_and_end_sentences():
    assert spans_text("It was 1984. 1985 was worse. He said _No._ Then _Why?_ she asked.") == [
        "It was 1984.", "1985 was worse.", "He said _No._", "Then _Why?_ she asked.",
    ]


def test_decimals_and_money_are_not_sentence_ends():
    assert spans_text("It cost $5.50 or 3.5 percent of it.") == ["It cost $5.50 or 3.5 percent of it."]


def test_whitespace_only_text_returns_the_whole_range():
    assert split_sentences("   ") == [(0, 3)]
    assert split_sentences("") == [(0, 0)]


def test_spans_are_trimmed_and_ordered():
    text = "  One.   Two!\n\nThree?  "
    spans = split_sentences(text)
    assert [text[a:b] for a, b in spans] == ["One.", "Two!", "Three?"]
    assert all(b1 <= a2 for (_, b1), (a2, _) in zip(spans, spans[1:]))
