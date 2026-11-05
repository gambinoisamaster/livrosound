"""Find in the book (⌘F): every place a phrase appears, as (block, start, end) spans.

Matching ignores case and accents ("cafe" finds "Café") and treats curly and
straight quotes and dashes alike, but the spans index Block.text exactly, so
they highlight in either view. Each block's folded text keeps one char per
char of Block.text (a char that would fold to several, like "ß", keeps its
first), so a match's offsets are offsets of the real text. Folded texts are
built once per book (FoldedBook); a search is a str.find per block (Anna
Karenina, 2 M chars: a few milliseconds).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .book import Book

MAX_MATCHES = 5000  # a two-letter search in a long book stops here

_SAME = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    " ": " ", " ": " ", "\n": " ",
}


def _fold_char(ch: str) -> str:
    if ch in _SAME:
        return _SAME[ch]
    base = unicodedata.normalize("NFKD", ch)
    base = "".join(c for c in base if not unicodedata.combining(c)) or ch
    low = base.lower()
    return low[0] if low else ch


def fold(text: str) -> str:
    """text folded for matching, one char per char."""
    return "".join(_fold_char(c) if not ("a" <= c <= "z" or c == " ") else c for c in text)


@dataclass
class Match:
    block: int
    start: int
    end: int


class FoldedBook:
    """A book's blocks folded once, ready for any number of searches."""

    def __init__(self, book: Book):
        self.book = book
        self._texts = [fold(b.text) for b in book.blocks]

    def find(self, query: str, limit: int = MAX_MATCHES) -> list[Match]:
        """Every occurrence of query, in reading order (overlapping ones too)."""
        needle = " ".join(fold(query).split())
        if not needle:
            return []
        matches: list[Match] = []
        for index, text in enumerate(self._texts):
            at = text.find(needle)
            while at >= 0:
                matches.append(Match(index, at, at + len(needle)))
                if len(matches) >= limit:
                    return matches
                at = text.find(needle, at + 1)
        return matches


def nearest(matches: list[Match], block: int) -> int:
    """The index of the first match at or after a block (wrapping to the first)."""
    for i, m in enumerate(matches):
        if m.block >= block:
            return i
    return 0
