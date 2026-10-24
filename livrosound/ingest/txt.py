"""TXT → Book: paragraphs, headings and front matter from plain text.

Plain text comes in two shapes: hard-wrapped (Project Gutenberg, OCR dumps:
most lines 40–85 characters, paragraphs between blank lines or opened by an
indent) and one paragraph per line (exports, copy-pastes). Either way a line
that doesn't end a sentence, followed by one starting in lowercase, is the
same paragraph (a short file of headings can look unwrapped). Hard-wrapped lines
are joined with textutil.join_mode, so "independ-" + "ent" becomes one word
and "fair-" + "weather" keeps its hyphen; text copied out of a PDF often keeps
those syllable hyphens mid-line ("Rus-sians", "giv-ing"), and when a book has
many of them they are joined too.

Headings are lines on their own that match textutil.HEADING_RE ("CHAPTER ONE",
"Chapter 12", "PREFACE") or are short and in capitals; a short line in title
case right after a chapter heading is that chapter's title ("CHAPTER TWO" /
"My First Glimpse of Dusty…"). A numbered line ("1. TEMPERANCE.") is not a
heading. The first lines, up to the first blank line, are the title page: the
first is the book's title (TITLE_ROLE), the rest front matter. Project
Gutenberg's header and licence are left out (its "Title:" and "Author:" lines
give the title and author).

original.html is the text itself (the part that is the book) in a monospace,
wrapping <pre>, with <a name="ls-b{i}"></a> right before each block's first
character. Running heads and the mid-line hyphens joined above are removed
from it too; line-end hyphens stay, as the text shows them.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

from livrosound.book import ANCHOR_PREFIX, Block, Book
from livrosound.textutil import HEADING_RE, TERMINAL, Vocab, join_mode

from . import BookImportError, matter
from .heads import find_running_heads, page_break_join, widen_cuts
from .textmap import MappedText, TextBuilder, edit_source

_GUTENBERG_START = re.compile(r"^\*{3}\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*$", re.I | re.M)
_GUTENBERG_END = re.compile(r"^\*{3}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*$", re.I | re.M)
_LIST_ITEM = re.compile(r"^\s*(?:\d{1,3}|[a-z]|[ivxlc]{1,5})[.)]\s", re.I)
_SEPARATOR = re.compile(r"^\s*(?:(?:[*⁂•·∗✻✶⁕§~]\s*)+|#|(?:-\s*){3,}|(?:[—–]\s*){2,}|(?:_\s*){3,})\s*$")
_PART_WORD = re.compile(r"^\s*(?:volume|vol\.|book|part)\b", re.I)
_MIDLINE_HYPHEN = re.compile(r"(?<![\w-])([A-Za-z]{2,})-([a-z]{2,})(?![\w-])")


@dataclass
class _Para:
    lines: list[tuple[int, int]]  # (start, end) offsets in the text of each line
    group: int  # blank-line-delimited group it belongs to
    mapped: MappedText = field(default_factory=MappedText)
    kind: str = "paragraph"
    level: int = 0


def parse_txt(path: Path, book_id: str) -> Book:
    """Parse a plain-text book (UTF-8, Windows-1252 or Latin-1; any line endings)."""
    text = _decode(path.read_bytes())
    meta_title, meta_author = _gutenberg_meta(text)
    meta_title = matter.clean_title(meta_title)
    start, end = _body_range(text)
    lines = _lines(text, start, end)
    vocab = Vocab.from_texts(text[a:b] for a, b in lines)
    paras = _paragraphs(text, lines)
    for p in paras:
        p.mapped = _build(text, p.lines, vocab)
    paras = [p for p in paras if p.mapped.text.strip()]
    deletions: list[tuple[int, int]] = []
    _repair_midline_hyphens(paras, vocab, deletions)
    _classify(paras)

    title = meta_title
    title_group: list[int] = []
    if paras:
        first_group = [i for i, p in enumerate(paras) if p.group == paras[0].group]
        # "by Ana Lima" after a blank line still belongs to the title page
        while len(first_group) < len(paras) and matter.is_byline(paras[len(first_group)].mapped.text):
            first_group.append(len(first_group))
        top = paras[0].mapped.text.strip()
        caps = top.upper() == top
        if (
            len(first_group) <= 4 and all(len(paras[i].mapped.text) <= 100 for i in first_group)
            and len(top.split()) <= 15 and (caps or not top.endswith((".", "!", "?", ",", ";", ":")))
        ):
            title_group = first_group
            title = title or matter.display_title(top)
            if not meta_author:
                meta_author = next(
                    (re.sub(r"^by\s+", "", paras[i].mapped.text.strip(), flags=re.I) for i in first_group[1:]
                     if re.match(r"^by\s+\S", paras[i].mapped.text.strip(), re.I)),
                    "",
                )
            if paras[0].kind == "paragraph":
                paras[0].kind, paras[0].level = "heading", 1
    title = title or path.stem.replace("_", " ").strip()

    chapter_titles = [p.mapped.text for p in paras if p.kind == "heading"]
    paras, title_group = _cut_heads(paras, title, chapter_titles, deletions, title_group)
    kept = [paras[i] for i in title_group]
    paras = matter.number_headings(paras, lambda ranges: deletions.extend((a, b) for _, a, b in ranges))
    title_group = [paras.index(p) for p in kept if p in paras]

    blocks = [Block(kind=p.kind, text=p.mapped.text, level=p.level, styles=list(p.mapped.styles)) for p in paras]
    if not any(b.text.strip() for b in blocks):
        raise BookImportError("No readable text was found in this book.")
    meta_author = matter.clean_author(meta_author)
    matter.assign_roles(blocks, title, author=meta_author, title_group=title_group)
    matter.rank_levels(blocks)
    matter.link_contents(blocks)
    toc = matter.toc_from_headings(blocks)
    return Book(
        book_id=book_id, title=title, author=meta_author, source_path=str(path), source_kind="txt",
        blocks=blocks, toc=toc, original_html=_original_html(text, start, end, paras, deletions),
    )


# ---------------------------------------------------------------------------
# Reading the text


def _decode(raw: bytes) -> str:
    """UTF-8 (with or without BOM), else Windows-1252, else Latin-1 (UTF-16 by its BOM); LF line ends."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    elif raw[:4096].count(b"\x00") > 8:
        raise BookImportError("This doesn't look like a text file.")
    else:
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _gutenberg_meta(text: str) -> tuple[str, str]:
    m = _GUTENBERG_START.search(text)
    if not m:
        return "", ""
    header = text[: m.start()]
    title = re.search(r"^Title:\s*(.+(?:\n[ \t]+.+)*)", header, re.M)
    author = re.search(r"^Author:\s*(.+)", header, re.M)
    return (" ".join(title.group(1).split()) if title else ""), (author.group(1).strip() if author else "")


def _body_range(text: str) -> tuple[int, int]:
    """The book's part of the text: between Project Gutenberg's START and END lines, if any."""
    start, end = 0, len(text)
    m = _GUTENBERG_START.search(text)
    if m:
        start = m.end()
    m = _GUTENBERG_END.search(text, start)
    if m:
        end = m.start()
    return start, end


def _lines(text: str, start: int, end: int) -> list[tuple[int, int]]:
    out, pos = [], start
    while pos < end:
        nl = text.find("\n", pos, end)
        stop = end if nl < 0 else nl
        out.append((pos, stop))
        pos = stop + 1
    return out


def _paragraphs(text: str, lines: list[tuple[int, int]]) -> list[_Para]:
    """Group lines into paragraphs (see the module docstring)."""
    filled = [(a, b) for a, b in lines if text[a:b].strip()]
    if not filled:
        return []
    # Judge the wrapping by lines that have a neighbour: headings and short lines stand alone.
    blank = [not text[a:b].strip() for a, b in lines]
    grouped = [
        lines[k] for k in range(len(lines))
        if not blank[k] and ((k > 0 and not blank[k - 1]) or (k + 1 < len(lines) and not blank[k + 1]))
    ]
    sample = grouped if len(grouped) >= 0.3 * len(filled) else filled
    lengths = sorted(len(text[a:b].rstrip()) for a, b in sample)
    wrapped = (
        sum(40 <= n <= 85 for n in lengths) >= 0.6 * len(lengths) and sum(n > 100 for n in lengths) <= 0.05 * len(lengths)
    )
    median = lengths[len(lengths) // 2]
    few_blanks = sum(1 for a, b in lines if not text[a:b].strip()) < 0.05 * len(lines)
    paras: list[_Para] = []
    group, current = 0, None
    for a, b in lines:
        line = text[a:b]
        if not line.strip():
            if current is not None:
                group += 1
            current = None
            continue
        stripped = line.strip()
        prev_line = text[current.lines[-1][0] : current.lines[-1][1]].strip() if current is not None else ""
        new = (
            current is None or not wrapped or line[:1] in (" ", "\t") or _SEPARATOR.match(stripped)
            or HEADING_RE.match(stripped) or _SEPARATOR.match(prev_line) or HEADING_RE.match(prev_line)
        )
        if not new and few_blanks:
            prev = text[current.lines[-1][0] : current.lines[-1][1]].rstrip()
            new = prev.endswith(TERMINAL) and len(prev) < 0.75 * median
        if (
            new and current is not None and stripped[:1].islower() and not prev_line.endswith(TERMINAL)
            and not HEADING_RE.match(prev_line) and not _SEPARATOR.match(prev_line)
        ):
            new = False  # a sentence running on to a lowercase line is one paragraph, wrapped or not
        if new:
            current = _Para([(a, b)], group)
            paras.append(current)
        else:
            current.lines.append((a, b))
    return paras


def _build(text: str, lines: list[tuple[int, int]], vocab: Vocab) -> MappedText:
    """One paragraph's text, its lines joined as join_mode says, mapped to the source."""
    builder = TextBuilder()
    for k, (a, b) in enumerate(lines):
        line_end = b
        if k + 1 < len(lines):
            nxt = text[lines[k + 1][0] : lines[k + 1][1]]
            mode = join_mode(text[a:b], nxt, vocab)
            stripped_end = a + len(text[a:b].rstrip())
            if mode == "drop-hyphen":
                builder.add(text[a : stripped_end - 1], 0, a)
                continue
            if mode == "join":
                builder.add(text[a:stripped_end], 0, a)
                continue
            line_end = lines[k + 1][0]  # the newline between the lines collapses to a space
        builder.add(text[a:line_end], 0, a)
    return builder.build()


def _repair_midline_hyphens(paras: list[_Para], vocab: Vocab, deletions: list[tuple[int, int]]) -> None:
    """Join syllable hyphens a PDF copy left mid-line ("Rus-sians"), when the text has many."""
    found = []
    total = 0
    for p in paras:
        for m in _MIDLINE_HYPHEN.finditer(p.mapped.text):
            total += 1
            hyphenated, joined = m.group(0), m.group(1) + m.group(2)
            # The book's own spelling decides first: "be-cause" 3 times but "because" 90 is an
            # artifact, Gutenberg's "to-day" (and never "today") is the author's.
            usual = vocab.count(joined) > vocab.count(hyphenated) or vocab.count(hyphenated) <= 2
            if usual and join_mode(m.group(1) + "-", m.group(2)) == "drop-hyphen":
                found.append((p, m.start(1) + len(m.group(1))))
    if len(found) < 5 or len(found) < 0.3 * total:
        return
    by_para: dict[int, list[int]] = {}
    for p, at in found:
        by_para.setdefault(id(p), []).append(at)
    for p in paras:
        cuts = [(at, at + 1) for at in by_para.get(id(p), [])]
        if cuts:
            deletions += [(a, b) for _, a, b in p.mapped.delete(cuts)]


def _classify(paras: list[_Para]) -> None:
    """Headings and separators among the paragraphs."""
    for i, p in enumerate(paras):
        s = p.mapped.text.strip()
        if len(p.lines) != 1:
            continue
        if _SEPARATOR.match(s):
            p.kind = "separator"
            continue
        if matter.is_contents_line(s):
            continue  # "Chapter 1 ........ 3": a line of a contents page
        if HEADING_RE.match(s) and not _LIST_ITEM.match(s):
            p.kind, p.level = "heading", 1 if _PART_WORD.match(s) else 2
            continue
        letters = [c for c in s if c.isalpha()]
        caps = len(letters) >= 4 and sum(c.isupper() for c in letters) >= 0.8 * len(letters)
        if caps and len(s) <= 80 and len(s.split()) <= 10 and not _LIST_ITEM.match(s) and not s.rstrip("”\"'’").endswith(("!", "?", ",", ";")):
            p.kind, p.level = "heading", 2
    # A chapter's own title on the line after "CHAPTER TWO".
    for prev, p in zip(paras, paras[1:]):
        s = p.mapped.text.strip()
        chapter = prev.kind == "heading" and HEADING_RE.match(prev.mapped.text.strip()) and not matter.heading_cue(prev.mapped.text)
        if (
            chapter and p.kind == "paragraph" and len(p.lines) == 1 and len(s) <= 80
            and not s.endswith(TERMINAL[:-1]) and not matter.is_contents_line(s) and _title_like(s)
        ):
            p.kind, p.level = "heading", prev.level + 1


def _title_like(s: str) -> bool:
    words = [w for w in s.split() if any(c.isalpha() for c in w)]
    if not words or len(words) > 12:
        return False
    capitalised = sum(w.lstrip("“\"'‘(")[:1].isupper() for w in words)
    return capitalised >= 0.6 * len(words)


def _cut_heads(paras, title, chapter_titles, deletions, title_group):
    """Cut running heads and page numbers; rejoin a paragraph a removed head split in two."""
    listed = matter.contents_lines([p.kind for p in paras], [p.mapped.text for p in paras])
    texts = [p.mapped.text if i not in listed else "" for i, p in enumerate(paras)]
    cuts = find_running_heads(texts, [p.kind for p in paras], title, chapter_titles)
    by_para: dict[int, list] = {}
    for c in cuts:
        by_para.setdefault(c.block, []).append(c)
    keep_title = set(title_group)
    out: list[_Para] = []
    new_group: list[int] = []
    removed_since = False
    for i, p in enumerate(paras):
        block_cuts = by_para.get(i, [])
        if block_cuts:
            deletions += [(a, b) for _, a, b in p.mapped.delete(widen_cuts(p.mapped.text, [(c.start, c.end) for c in block_cuts]))]
            p.mapped.strip()
        if not p.mapped.text.strip():
            removed_since = True
            continue
        prev = out[-1] if out else None
        if removed_since and prev is not None and prev.kind == p.kind == "paragraph":
            sep = page_break_join(prev.mapped.text, p.mapped.text, False)
            if sep is not None:
                prev.mapped = prev.mapped.join(p.mapped, sep)
                prev.lines += p.lines
                removed_since = False
                continue
        removed_since = False
        if i in keep_title:
            new_group.append(len(out))
        out.append(p)
    return out, new_group


# ---------------------------------------------------------------------------
# original.html


def _original_html(text: str, start: int, end: int, paras: list[_Para], deletions: list[tuple[int, int]]) -> str:
    anchors = []
    for i, p in enumerate(paras):
        first = p.mapped.source_start()
        if first is not None:
            anchors.append((first[1] - start, f"{ANCHOR_PREFIX}{i}"))
    body = text[start:end]
    pieces = edit_source(body, [(a - start, b - start) for a, b in deletions], anchors)
    out = []
    for piece in pieces:
        if isinstance(piece, tuple):
            out.append(f'<a name="{piece[0]}">{html.escape(piece[1], quote=False)}</a>')
        else:
            out.append(html.escape(piece, quote=False))
    content = "".join(out).strip("\n")
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"/>'
        "<style>pre { white-space: pre-wrap; font-family: 'Courier New', Courier, monospace; }</style>"
        f"</head><body><pre>{content}</pre></body></html>\n"
    )
