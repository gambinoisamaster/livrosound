"""Block text with a map back to the source characters it came from.

A block's display text (Block.text) is its source text with whitespace collapsed
the way a browser collapses it, invisible marks (soft hyphens, zero-width
spaces, BOMs) dropped and ligatures spelled out; later running heads are cut
out of it and paragraphs split by a page break are joined. The original-layout
HTML must lose exactly the same running heads, and gets each block's anchor
(<a name="ls-b{i}">) right before the block's first character, so while a
block is being built every stretch of its text remembers where in the source
it came from: a source id chosen by the parser (an EPUB text node, a DOCX run,
the one string of a TXT file) and a character range in it.

Rules:
- Whitespace collapses like HTML's normal white-space: any run of spaces, tabs,
  newlines and no-break spaces is one space, none at the start or end of a
  block or around a line break.
- A line break (<br>) is "\n" while building; build(verse=False) turns breaks
  into spaces (Block.text has "\n" only inside verse).
- A span whose text and source have the same length maps char k to source
  char src_start + k; otherwise (a collapsed run of whitespace, a ligature) the
  whole span maps to the whole source range.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

NO_SOURCE = -1

_WS = " \t\n\r\f   -   　"
_DROPPED = "­​‌‍⁠﻿"
_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
_PIECE = re.compile(rf"(?P<ws>[{_WS}]+)|(?P<odd>[{_DROPPED}ﬀ-ﬆ])|(?P<text>[^{_WS}{_DROPPED}ﬀ-ﬆ]+)")
# Text that needs the slow path: anything but single spaces between visible characters.
_IRREGULAR = re.compile(rf"[\t\n\r\f   -   　{_DROPPED}ﬀ-ﬆ]|  ")


@dataclass(slots=True)
class Span:
    start: int  # in the text
    end: int
    src: int  # source id; NO_SOURCE for characters made up while building (a joining space)
    src_start: int
    src_end: int

    @property
    def linear(self) -> bool:
        return self.end - self.start == self.src_end - self.src_start


@dataclass
class MappedText:
    text: str = ""
    spans: list[Span] = field(default_factory=list)
    styles: list[tuple[int, int, str]] = field(default_factory=list)

    def source_start(self) -> tuple[int, int] | None:
        """(source id, offset) of the first character that has a source."""
        for span in self.spans:
            if span.src != NO_SOURCE and span.end > span.start:
                return span.src, span.src_start
        return None

    def source_ranges(self, a: int, b: int) -> list[tuple[int, int, int]]:
        """The source ranges (src, start, end) behind text[a:b]. A non-linear span
        counts only when the range covers all of it."""
        out = []
        for span in self.spans:
            if span.end <= a or span.start >= b or span.src == NO_SOURCE:
                continue
            if span.linear:
                lo, hi = max(a, span.start), min(b, span.end)
                out.append((span.src, span.src_start + lo - span.start, span.src_start + hi - span.start))
            elif a <= span.start and span.end <= b:
                out.append((span.src, span.src_start, span.src_end))
        return out

    def delete(self, cuts: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
        """Remove the text ranges in cuts; returns the source ranges removed with them."""
        cuts = _merged(cuts, len(self.text))
        if not cuts:
            return []
        removed = [r for a, b in cuts for r in self.source_ranges(a, b)]
        keep = _complement(cuts, len(self.text))
        new_spans: list[Span] = []
        for span in self.spans:
            for a, b in keep:
                lo, hi = max(a, span.start), min(b, span.end)
                if lo >= hi:
                    continue
                if span.linear:
                    s0 = span.src_start + lo - span.start
                    new_spans.append(Span(lo, hi, span.src, s0, s0 + hi - lo))
                else:
                    new_spans.append(Span(lo, hi, span.src, span.src_start, span.src_end))
        shift = _Shift(cuts)
        for span in new_spans:
            span.start, span.end = shift(span.start), shift(span.end)
        self.spans = new_spans
        self.styles = [
            (shift(s), shift(e), st) for s, e, st in self.styles if shift(e) > shift(s)
        ]
        self.text = "".join(self.text[a:b] for a, b in keep)
        return removed

    def strip(self) -> list[tuple[int, int, int]]:
        """Drop leading and trailing whitespace."""
        lead = len(self.text) - len(self.text.lstrip())
        trail = len(self.text.rstrip())
        return self.delete([(0, lead), (trail, len(self.text))])

    def slice(self, a: int, b: int) -> MappedText:
        """A copy of text[a:b] with its map and styles."""
        out = MappedText(self.text[a:b])
        for span in self.spans:
            lo, hi = max(a, span.start), min(b, span.end)
            if lo >= hi:
                continue
            if span.linear:
                s0 = span.src_start + lo - span.start
                out.spans.append(Span(lo - a, hi - a, span.src, s0, s0 + hi - lo))
            else:
                out.spans.append(Span(lo - a, hi - a, span.src, span.src_start, span.src_end))
        out.styles = [(max(s, a) - a, min(e, b) - a, st) for s, e, st in self.styles if min(e, b) > max(s, a)]
        return out

    def join(self, other: MappedText, sep: str = "") -> MappedText:
        """This text, then sep (made up, no source), then other."""
        base = len(self.text) + len(sep)
        out = MappedText(self.text + sep + other.text, list(self.spans), list(self.styles))
        if sep:
            out.spans.append(Span(len(self.text), base, NO_SOURCE, 0, 0))
        out.spans += [Span(s.start + base, s.end + base, s.src, s.src_start, s.src_end) for s in other.spans]
        out.styles += [(s + base, e + base, st) for s, e, st in other.styles]
        return out


class TextBuilder:
    """Collects a block's inline text piece by piece, collapsing whitespace like a browser."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._spans: list[Span] = []
        self._runs: list[list] = []  # [start, end, style]
        self._open: dict[str, list] = {}  # style -> its latest run
        self._len = 0
        self._last = ""  # the last character emitted
        self._pending: tuple[int, int, int] | None = None  # a collapsed space waiting for the next word
        self.breaks = 0  # line breaks added

    @property
    def empty(self) -> bool:
        return self._len == 0

    def add(self, s: str, src: int = NO_SOURCE, offset: int = 0, styles: frozenset[str] | tuple = ()) -> None:
        if not s:
            return
        if _IRREGULAR.search(s) is None:  # the common case: words and single spaces
            core = s.strip(" ")
            if not core:
                self._space(src, offset, len(s))
                return
            lead = len(s) - len(s.lstrip(" "))
            if lead:
                self._space(src, offset, lead)
            self._emit(core, src, offset + lead, offset + lead + len(core), styles)
            trail = len(s) - lead - len(core)
            if trail:
                self._space(src, offset + len(s) - trail, trail)
            return
        for m in _PIECE.finditer(s):
            kind, piece = m.lastgroup, m.group()
            at = offset + m.start()
            if kind == "ws":
                self._space(src, at, len(piece))
            elif kind == "text":
                self._emit(piece, src, at, at + len(piece), styles)
            elif piece in _LIGATURES:
                self._emit(_LIGATURES[piece], src, at, at + 1, styles)

    def line_break(self) -> None:
        """A <br>: "\n" (a space unless the block turns out to be verse)."""
        self._pending = None
        if self._len:
            self._put("\n", Span(self._len, self._len + 1, NO_SOURCE, 0, 0))
        self.breaks += 1

    def build(self, verse: bool = False) -> MappedText:
        out = MappedText("".join(self._parts), self._spans, [(a, b, st) for a, b, st in self._runs])
        text = out.text
        cuts = [(len(text.rstrip("\n ")), len(text))]
        if not verse:
            cuts += [(m.start() + 1, m.end()) for m in re.finditer(r"\n{2,}", text)]
        out.delete(cuts)
        if not verse:
            out.text = out.text.replace("\n", " ")
        else:
            # keep at most one empty line (a stanza break) between lines
            out.delete([(m.start() + 2, m.end()) for m in re.finditer(r"\n{3,}", out.text)])
        return out

    # ---------- internals ----------

    def _space(self, src: int, at: int, length: int) -> None:
        if self._len and self._last not in " \n" and self._pending is None:
            self._pending = (src, at, at + length)

    def _emit(self, piece: str, src: int, s0: int, s1: int, styles) -> None:
        if self._pending is not None:
            psrc, p0, p1 = self._pending
            self._pending = None
            self._put(" ", Span(self._len, self._len + 1, psrc, p0, p1))
        start = self._len
        self._put(piece, Span(start, start + len(piece), src, s0, s1))
        for style in styles:
            run = self._open.get(style)
            if run is not None and run[1] >= start - 1:  # contiguous (or across one space)
                run[1] = self._len
            else:
                run = [start, self._len, style]
                self._runs.append(run)
                self._open[style] = run

    def _put(self, piece: str, span: Span) -> None:
        last = self._spans[-1] if self._spans else None
        if (
            last is not None and last.src == span.src and span.src != NO_SOURCE
            and last.linear and span.linear and last.src_end == span.src_start
        ):
            last.end, last.src_end = span.end, span.src_end
        else:
            self._spans.append(span)
        self._parts.append(piece)
        self._len += len(piece)
        self._last = piece[-1]


def edit_source(
    text: str, deletions: list[tuple[int, int]], anchors: list[tuple[int, str]]
) -> list[str | tuple[str, str]]:
    """One source string after deleting ranges and inserting anchors: an ordered list
    of text pieces (str) and anchors (name, first character). Each anchor holds the
    character that follows it ("" at the very end): Qt's rich text keeps a named
    anchor reliably only when it wraps a character (an empty one opening loose text
    after a paragraph, or before an image, is dropped)."""
    deletions = _merged(deletions, len(text))
    keep = _complement(deletions, len(text))
    shift = _Shift(deletions)
    edited = "".join(text[a:b] for a, b in keep)
    out: list[str | tuple[str, str]] = []
    pos = 0
    for offset, name in sorted(anchors, key=lambda a: a[0]):
        at = max(shift(min(offset, len(text))), pos)
        if at > pos:
            out.append(edited[pos:at])
        out.append((name, edited[at : at + 1]))
        pos = min(at + 1, len(edited))
    if pos < len(edited) or not out:
        out.append(edited[pos:])
    return out


def _merged(cuts: list[tuple[int, int]], length: int) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted((max(0, a), min(length, b)) for a, b in cuts):
        if a >= b:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _complement(cuts: list[tuple[int, int]], length: int) -> list[tuple[int, int]]:
    keep, pos = [], 0
    for a, b in cuts:
        if a > pos:
            keep.append((pos, a))
        pos = b
    if pos < length:
        keep.append((pos, length))
    return keep


class _Shift:
    """Maps a position in a text to its position after the cuts (a position inside a cut → the cut's start)."""

    def __init__(self, cuts: list[tuple[int, int]]) -> None:
        self._starts = [a for a, _ in cuts]
        self._cuts = cuts
        self._removed_before = []
        total = 0
        for a, b in cuts:
            self._removed_before.append(total)
            total += b - a

    def __call__(self, x: int) -> int:
        k = bisect.bisect_right(self._starts, x) - 1
        if k < 0:
            return x
        a, b = self._cuts[k]
        if x < b:
            return a - self._removed_before[k]
        return x - self._removed_before[k] - (b - a)
