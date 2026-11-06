"""Where each sentence of a block starts and ends.

LivroSound voices a book one sentence at a time (speech.py) and highlights the
sentence being heard, so the sentence is the unit of both. Ported from Script
Radio's splitter, with rules for books:

- A sentence ends at . ! ? or an ellipsis (plus closing quotes, brackets and
  Gutenberg's _italic_ / *bold* marks) followed by whitespace and then a capital,
  a digit, a quote or an opening bracket: "So... I don't know" is two sentences,
  "Wait... what?" is one.
- Never after an abbreviation ("Mr. Smith", "cf. Burgess", "pp. 12", "Ph.D. thesis")
  or an initial ("J. R. R. Tolkien"). "I." is the exception: it ends a sentence
  ("So did I. Then...") unless another initial follows ("I. M. Pei").
- Reference words that are also ordinary words hold a sentence together only
  before a number: "No. 5", "fig. 3", "Art. IV" — but "No. I won't." and "He ate a
  fig. It was ripe." are two sentences each. "ed." counts only in lowercase
  ("2nd ed."), since Ed is a name.
"""

from __future__ import annotations

import re

# Words whose trailing period never ends a sentence. Script Radio's list plus the
# reference and title abbreviations of books.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "mx", "dr", "st", "jr", "sr", "vs", "prof", "mt", "ft",
    "sgt", "lt", "capt", "cpt", "col", "gen", "det", "ofc", "rev", "hon", "fr", "sen",
    "rep", "gov", "pres", "int", "ext", "approx", "dept", "etc", "e.g", "i.e", "a.m", "p.m",
    "messrs", "mme", "mlle", "bros", "co", "inc", "ltd", "ch", "vol", "vols", "eds",
    "trans", "cf", "viz", "pp", "ibid", "op", "cit", "esp", "ca", "al",
}
# Abbreviations that are also ordinary words: they hold only before a number
# (or, except no/nos, a Roman numeral: "Chap. IV", "Art. II").
_BEFORE_NUMBER = {"no", "nos", "chap", "fig", "figs", "art", "sec"}
_DIGITS_ONLY = {"no", "nos"}  # "No. I won't." is two sentences
_LOWERCASE_ONLY = {"ed"}  # "2nd ed. London" holds; "I saw Ed. He waved." splits

# Sentence-ending punctuation (plus closers) followed by a space.
_END_RE = re.compile(r"[.!?\u2026]+[\"'\u2019\u201d\u00bb)\]_*]*(?=\s)")
_WORD_BEFORE_RE = re.compile(r"([A-Za-z][A-Za-z.'\u2019]*)$")  # "won’t." is a word, not the initial "t."
_OPENERS = "\"'(\u201c\u2018[\u00ab_*¿¡"
_INITIAL_RE = re.compile(r"[A-Z]\.(?:\s|$)")
_NEXT_RE = re.compile(r"\s*(?=\S)")
_NUMBER_RE = re.compile(r"\d")
_REFERENCE_RE = re.compile(r"\d|[IVXLC]+\b")


def _holds(word: str, following: str) -> bool:
    """True when the period after `word` belongs to an abbreviation or initial."""
    lower = word.lower()
    if word == "I":
        return bool(_INITIAL_RE.match(following))
    if len(word) == 1 or "." in word or lower in _ABBREVIATIONS:
        return True  # "J. R.", "U.S. Army", "Mr. Smith"
    if lower in _BEFORE_NUMBER:
        return bool((_NUMBER_RE if lower in _DIGITS_ONLY else _REFERENCE_RE).match(following))
    return word in _LOWERCASE_ONLY


def split_sentences(text: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    """Character spans (start, end) of each sentence in text[start:end], whitespace trimmed."""
    end = len(text) if end is None else end
    spans: list[tuple[int, int]] = []
    cursor = start
    for m in _END_RE.finditer(text, start, end):
        gap = _NEXT_RE.match(text, m.end(), end)
        if not gap:
            continue
        following = text[gap.end():min(end, gap.end() + 16)]  # enough to see "5", "IV" or "M. Pei"
        if not (following[0].isupper() or following[0].isdigit() or following[0] in _OPENERS):
            continue
        if text[m.start()] == ".":
            word = _WORD_BEFORE_RE.search(text[cursor:m.start()])
            if word and _holds(word.group(1), following):
                continue
        spans.append((cursor, m.end()))
        cursor = m.end()
    spans.append((cursor, end))
    trimmed = []
    for a, b in spans:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if a < b:
            trimmed.append((a, b))
    return trimmed or [(start, end)]


_WORD_SPAN_RE = re.compile(r"\S+")
_VOWELS_RE = re.compile(r"[aeiouy]+", re.I)


def word_at(text: str, start: int, end: int, fraction: float) -> tuple[int, int]:
    """The word of text[start:end] being said `fraction` (0..1) of the way through it,
    for the word-by-word highlighter. Pocket-TTS gives no word timings, so each word
    is given time for its syllables, and punctuation after it for a breath."""
    words = [(m.start() + start, m.end() + start, m.group()) for m in _WORD_SPAN_RE.finditer(text[start:end])]
    if not words:
        return start, end
    weights = []
    for _, _, word in words:
        syllables = max(1, len(_VOWELS_RE.findall(word)))
        pause = 1.5 if word[-1] in ".!?;:" else 0.7 if word[-1] in ",—–)" else 0.0
        weights.append(syllables + 0.6 + pause)
    target = max(0.0, min(1.0, fraction)) * sum(weights)
    reached = 0.0
    for (a, b, _), weight in zip(words, weights):
        reached += weight
        if target < reached:
            return a, b
    return words[-1][0], words[-1][1]
