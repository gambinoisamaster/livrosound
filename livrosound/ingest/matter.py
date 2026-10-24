"""Front and back matter: which blocks are the book, and the book's own contents page.

Books open with pages nobody wants narrated (the cover, praise and blurbs,
title pages, the copyright page, acknowledgements, the printed table of
contents, an e-book maker's notice) and may close with an index or a
publisher's note. The user asked that only the book itself is read, so these
blocks keep their place (shown, muted) but get a Block.role in
book.SKIPPED_ROLES. Exactly one block, the title-page title, gets TITLE_ROLE
and announces the book ("<title>, by <author>.").

Signals, strongest first:
- declared roles passed in per block by the EPUB parser (epub:type and ARIA
  roles, the landmarks nav or <guide>, the labels of the book's own TOC such as
  "Copyright Page", file names);
- cue headings — "Contents", "Acknowledgments", "Index" and "Bibliography" (back
  half only), "Praise for…", "Also by…", "About the Author", a heading starting
  "Copyright" — open a skipped section that runs until the next heading of the
  same or a higher level, and never past the end of its unit (an EPUB file);
  without units a "Contents" section also ends at the first long paragraph,
  since books without heading markup would otherwise lose everything after it.
  In EPUBs the first block of a file counts as its heading even when it is a
  plain paragraph ("BY THE SAME AUTHOR");
- notices anywhere: scanners' and libraries' (textutil.looks_like_boilerplate),
  the Internet Archive's, Project Gutenberg's and Distributed Proofreaders',
  "Thank you for downloading this eBook"; in the front zone also their
  "Title:" / "Author:" / "Date first posted:" lines;
- in the front zone (before the body starts): copyright clusters ("Copyright",
  "©", "All rights reserved", "ISBN"…), contents lists whose lines end in page
  numbers, praise pages (quotes signed "—The New York Times", "Bestseller"),
  copies of the title, "by …" and author-name lines beside it, and the rest of
  a short title page;
- "THE END" near the end of the book: what follows it up to the next heading
  (a publisher's note, back-cover copy) is "blurb".
Dedications ("Dedicated to…", "For Lucien", "To my wife…"), epigraphs,
prefaces, forewords, introductions and prologues always stay readable,
whatever page they are on (a dedication often shares the title page's file).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

from livrosound.book import SKIPPED_ROLES, TITLE_ROLE, Block, TocEntry
from livrosound.textutil import (
    HEADING_RE,
    looks_like_boilerplate,
    roman_to_int,
    title_case,
)

from .heads import letters_key

READ = "read"  # declared readable (dedication, preface, a chapter…)
FRONT = "front"  # declared front matter of no specific kind
TITLEPAGE = "titlepage"  # declared title page: front matter that holds the title

_READ_CUE = re.compile(
    r"^(?:dedication|epigraph|preface|foreword|fore-word|introduction|prologue|preamble|proem|overture"
    r"|(?:an? )?(?:author|translator|editor)['’]?s?['’]? (?:note|preface|introduction)"
    r"|(?:a )?note on (?:the |this )?(?:text|translation|sources|names|spelling)"
    r"|invocation|argument|prelude|epilogue|afterword|postscript)\b",
    re.I,
)
_CUES = [
    ("contents", re.compile(r"^(?:table of contents|contents|content|toc|list of (?:illustrations|plates|figures|maps))$", re.I)),
    ("acknowledgments", re.compile(r"^(?:acknowledge?ments?|acknowledge?ments and thanks|thanks)$", re.I)),
    ("copyright", re.compile(r"^(?:copyright\b.*|©.*|imprint|colophon|uncopyright|credits)$", re.I)),
    (
        "blurb",
        re.compile(
            r"^(?:(?:praise|acclaim) for\b.*|(?:also|other books|more books|books) by\b.*|(?:also|now) available\b.*"
            r"|by the same author|about the (?:author|authors|translator|editor|publisher)s?|about the book"
            r"|(?:a )?reading group guide.*|discussion questions|questions for discussion|(?:a )?note on the type(?:face)?"
            r"|newsletter|sign up.*|(?:an )?excerpt from\b.*|coming soon.*|sneak peek.*|a preview of\b.*)$",
            re.I,
        ),
    ),
]
_BACK_CUES = [
    ("index", re.compile(r"^(?:general |subject |name )?index(?: of [\w ]+)?$", re.I)),
    # A bibliography is reference matter, not the book: "index" is the closest skipped role.
    ("index", re.compile(r"^(?:select(?:ed)? |annotated )?bibliography$|^works cited$|^references$|^further reading$|^sources$", re.I)),
]
_LABELS = [  # TOC labels naming front matter files: "Cover Page", "Title Page"
    ("cover", re.compile(r"^(?:front )?cover(?: page| image)?$", re.I)),
    (TITLEPAGE, re.compile(r"^(?:full |half[- ])?title(?: page)?$", re.I)),
]
_COPYRIGHT_RE = re.compile(
    r"copyright\b|©|\(c\)\s*(?:19|20)\d\d|all rights reserved|\bisbn\b|library of congress|cataloging[- ]in[- ]publication"
    r"|\bpublished by\b|\bprinted in\b|\bprinted and bound\b|manufactured in\b|first (?:published|edition|printing)\b"
    r"|\bthis (?:e-?book|edition) (?:was|is)\b|\bebook edition\b|\bpublished in\b.*\b(?:19|20)\d\d\b|\bpublic domain\b"
    r"|\bset up,? (?:and )?electrotyped\b|\bcover (?:art|design|illustration)\b|\bwork of fiction\b|\bany resemblance to\b"
    r"|t[íi]tulo original|traducci[óo]n|tradu[çc][ãa]o|primera edici[óo]n|primeira edi[çc][ãa]o|derechos reservados"
    r"|direitos reservados|impreso en|impresso no|dep[óo]sito legal|d[ée]p[ôo]t l[ée]gal|tous droits r[ée]serv[ée]s",
    re.I,
)
# Copyright text even in a long paragraph: "No part of this book may be reproduced…".
_STRONG_COPYRIGHT_RE = re.compile(
    r"no part of this (?:publication|book|e-?book|work) may be|protected by copyright|all rights reserved"
    r"|unauthorized (?:reproduction|distribution)|cataloging[- ]in[- ]publication",
    re.I,
)
_BYLINE_RE = re.compile(
    r"^(?:(?:written|translated|edited|illustrated|introduced|selected|compiled|adapted|retold)(?: and \w+)? (?:by|from)\b"
    r"|by\s+[A-Z]|with an? (?:introduction|foreword|preface|afterword) by\b|an? (?:novel|memoir|story|play|poem|romance)\b)",
    re.I,
)
_END_RE = re.compile(r"^(?:the end|finis|fin|end)\W*$", re.I)
_NOTICE_RE = re.compile(
    r"produced in epub format by the internet archive|created with hocr-to-epub|internet archive was founded in 1996"
    r"|the text on this page is estimated to be only|book pages were scanned and converted"
    r"|distributed proofreaders|project gutenberg|gutenberg\.org|fadedpage|faded page e-?book"
    r"|this e-?book is (?:made available at no cost|for the use of anyone)|this (?:e-?book|file) was produced (?:by|from)"
    r"|thank you for (?:downloading|purchasing|buying|reading) this\b|join our mailing list|sign up for our newsletter"
    r"|\bclick here\b",
    re.I,
)
_FRONT_NOTICE_RE = re.compile(
    r"^(?:title|author|translator|editor|illustrator|release date|date of first publication|date first posted"
    r"|date last updated|language|character set encoding|produced by|e-?text prepared by)\s*:",
    re.I,
)
# A praise page: "Bestseller", or a quotation signed with a dash ("…a masterpiece."—The New Yorker).
_PRAISE_RE = re.compile(
    r"\bbest-?sell(?:er|ing)\b|\bwinner of\b|\bfinalist for\b|(?:praise|acclaim) for\b"
    r"|[”\"’.!]\s*[—–-]{1,2}\s*[A-Z][^\s—–]*(?:\s+\S+){0,8}\s*$|^\s*[—–]{1,2}\s*[A-Z][\w.'’&]*(?:\s+\S+){0,8}\s*$"
    r"|[”’\"]\s+[A-Z][\w.'’&-]*(?:[ ,]+(?:[A-Z0-9][\w.'’&-]*|of|the|and|de))*\s*$",  # …intelligence’ Joseph O’Neill, The Times
    re.I | re.M,
)
_SIGNATURE_RE = re.compile(r"^\s*[—–]{1,2}\s*[A-Z][\w.'’&]*(?:\s+\S+){0,8}\s*$")
_PRAISE_WORDS = re.compile(r"\bbest-?sell(?:er|ing)\b|\bwinner of\b|\bfinalist for\b|(?:praise|acclaim) for\b", re.I)
# What a trailing "(…)" on a metadata title says when it isn't part of the title.
_SERIES_TAG = re.compile(
    r"\b(?:collection|classics?|library|series|edition|editions|international|press|books|publishing|imprint"
    r"|penguin|vintage|picador|oxford|everyman|bantam|signet|norton|harper|anchor|modern library|world'?s classics"
    r"|unabridged|abridged|annotated|illustrated|revised|reprint|novel|book \d+|vol(?:ume)?\.? ?\d+|#\s*\d+"
    r"|z-lib|libgen|ebook|e-book|epub|kindle|retail|mobi)\b|\.(?:org|com|net)\b",
    re.I,
)
_CHAPTER_HEADING = re.compile(r"^\s*(?:chapter|chap\.|stave|letter|section|canto)\b", re.I)
_SECTION_WORD = re.compile(r"^(?:part|book|chapter|volume|section|act|canto|letter|stave)$", re.I)
_CONTENTS_LINE = re.compile(r"(?:\.{3,}|…|\s{3,}|\t)\s*(?:\d{1,4}|[ivxlc]{1,7})\s*$", re.I)
_LEADERS = re.compile(r"(?:[\s.·…_-]{2,}|\s+)(?:\d{1,4}|[ivxlc]{1,7})\s*$", re.I)


def heading_cue(text: str, back: bool = False) -> str | None:
    """The role a heading starts ("contents", "index"…), READ for a readable front
    heading (Preface, Dedication…), or None."""
    s = re.sub(r"[\s.:]+$", "", " ".join(text.split()))
    if not s or len(s) > 60:
        return None
    if _READ_CUE.match(s):
        return READ
    for role, pattern in _CUES + (_BACK_CUES if back else []):
        if pattern.match(s):
            return role
    return None


def label_role(label: str) -> str | None:
    """The role a TOC label gives the file it points at: "Copyright Page" → copyright."""
    s = re.sub(r"[\s.:]+$", "", " ".join(label.split()))
    for role, pattern in _LABELS:
        if pattern.match(s):
            return role
    return heading_cue(s, back=True)


def is_notice(text: str, front: bool = False) -> bool:
    """A scanner's, library's or e-book maker's notice (front: also its "Title:" lines)."""
    if looks_like_boilerplate(text) or _NOTICE_RE.search(text) is not None:
        return True
    return front and _FRONT_NOTICE_RE.match(text.strip()) is not None


def is_contents_line(text: str) -> bool:
    """'Chapter 1 ........ 3', 'Short Sentences   19': a line of a printed contents page."""
    return _CONTENTS_LINE.search(text) is not None


def contents_lines(kinds: list[str], texts: list[str]) -> set[int]:
    """Blocks that are lines of a printed contents page: those under a "Contents" heading
    (until the next heading or a long paragraph) and any "Title ...... 12" line. Running-head
    detection leaves them alone ("Short Sentences 19" there is an entry, not a head)."""
    out = {i for i, t in enumerate(texts) if is_contents_line(t)}
    inside = False
    for i, (kind, text) in enumerate(zip(kinds, texts)):
        if kind == "heading" or (i and kind == "paragraph" and text.lower().strip() in ("contents", "table of contents")):
            inside = heading_cue(text) == "contents"
        elif inside and len(text.split()) > 40:
            inside = False
        elif inside:
            out.add(i)
    return out


def is_byline(text: str) -> bool:
    """'by Ana Lima', 'Translated by Breon Mitchell', 'A Novel'."""
    s = text.strip()
    return len(s.split()) <= 12 and _BYLINE_RE.match(s) is not None


def title_keys(title: str) -> set[str]:
    """The ways a book's title is printed: 'Blood Meridian (Picador Collection)', 'Oblivion:
    Stories', 'Queer by William S Burroughs' all match their short title too."""
    variants = {title, re.split(r"\s*[(:;\[]|\s+[—–-]\s+", title)[0], re.sub(r"\s+by\s+.+$", "", title, flags=re.I)}
    variants |= {re.sub(r"^(?:the|a|an)\s+", "", v, flags=re.I) for v in set(variants)}
    return {k for k in (letters_key(v) for v in variants) if len(k) >= 3}


def clean_title(title: str) -> str:
    """A metadata title without a trailing series, publisher or download-site tag:
    'Blood Meridian (Picador Collection)' → 'Blood Meridian'; real subtitles stay
    ('Oblivion: Stories', 'Artful Sentences: Syntax as Style')."""
    s = " ".join(title.split())
    while True:
        m = re.search(r"\s*[(\[]([^()\[\]]*)[)\]]\s*$", s)
        if not m or not m.start() or not (_SERIES_TAG.search(m.group(1)) or re.fullmatch(r"\s*\d{4}\s*", m.group(1))):
            return s
        s = s[: m.start()].rstrip(" -–—,;:")


def is_dedication(text: str) -> bool:
    """'Dedicated to a Happier Year', 'For Lucien', 'To my wife, Helen…', 'In memory of…':
    a dedication, which is read even on a title page."""
    s = " ".join(text.split())
    if not s or len(s.split()) > 30:
        return False
    return bool(
        re.match(r"^(?:(?:dedicated|inscribed) to\b|dedication\b|in (?:loving )?memory\b)", s, re.I)
        or re.match(r"^(?:For|To|FOR|TO)\s+(?:my\b|our\b|the memory\b|no one\b|everyone\b|[A-Z“\"‘'])", s)
    )


def _dedication(block: Block, keys: set[str]) -> bool:
    """A dedication line, not a copy of the title ("To the Lighthouse")."""
    return block.kind != "heading" and is_dedication(block.text) and letters_key(block.text) not in keys


def clean_author(name: str) -> str:
    """'Cooper, Dennis, 1953-' → 'Dennis Cooper'; 'Unknown' → ''; 'Hallowell, Edward M.' flipped."""
    s = re.sub(r",?\s*\(?\b\d{4}\s*[-–]\s*(?:\d{4})?\)?", "", " ".join(name.split())).strip(" ,;")
    if s.lower() in ("unknown", "anonymous author", "unknown author", "author"):
        return ""
    parts = [p.strip() for p in s.split(",")]
    if len(parts) == 2 and all(parts) and len(parts[0].split()) <= 2 and len(parts[1].split()) <= 3:
        s = f"{parts[1]} {parts[0]}"
    return s.strip(" ,;")


def split_byline(text: str) -> tuple[str, str]:
    """'OVERCOAT and Other Stories by Nikolay Gogol ¢ ALFRED A. KNOPF' -> (title, author)."""
    m = re.match(r"^(.{3,120}?)\s+by\s+((?:[A-Z][\w.'’-]*\s*){1,4})", " ".join(text.split()))
    if not m:
        return "", ""
    author_words = []
    for w in m.group(2).split():
        if w.isupper() and len(w) > 2:  # the publisher in capitals: "ALFRED A. KNOPF"
            break
        author_words.append(w)
    t = m.group(1).strip(" ,.:;")
    if any(len(w) >= 4 and w.isupper() for w in t.split()):  # "OVERCOAT and Other Stories"
        t = title_case(t)
    return t, " ".join(author_words).strip(" ,.;")


def assign_roles(
    blocks: list[Block],
    title: str,
    *,
    author: str = "",
    declared: list[str] | None = None,
    body_start: int | None = None,
    title_group: list[int] | None = None,
    units: list[int] | None = None,
) -> None:
    """Set Block.role on front/back matter and the one title block (see the module docstring).
    declared: per block, a role from the file's markup (or READ / FRONT / TITLEPAGE);
    units: the first block of each unit (an EPUB's spine files), which sections never cross;
    title_group: the title page's blocks when the parser knows them (TXT's first lines)."""
    n = len(blocks)
    if not n:
        return
    declared = declared or [""] * n
    readable = [d == READ for d in declared]
    starts = sorted(set(units or []) | {0})
    unit_of = _unit_index(starts, n)
    unit_end = [starts[unit_of[i] + 1] if unit_of[i] + 1 < len(starts) else n for i in range(n)]

    keys = title_keys(title)
    for i, b in enumerate(blocks):
        if declared[i] in SKIPPED_ROLES:
            b.role = declared[i]
        elif declared[i] == TITLEPAGE:
            if _dedication(b, keys):
                readable[i] = True  # "Dedicated to…" printed on the title page
            else:
                b.role = "frontmatter"

    def mark(start: int, end: int, cue: str) -> None:
        for k in range(start, end):
            if cue == READ:  # a preface, even in a file marked only "frontmatter"
                readable[k] = True
            elif not blocks[k].role and not readable[k]:
                blocks[k].role = cue

    # The first block of each unit is its heading, whatever its markup ("BY THE SAME AUTHOR");
    # a unit opening with a notice ("Thank you for downloading…") is all notice.
    if units:
        for start in starts:
            first = next((k for k in range(start, unit_end[start]) if blocks[k].text.strip()), None)
            if first is None:
                continue
            cue = heading_cue(blocks[first].text.split("\n")[0], back=first > n // 2) if blocks[first].kind != "heading" else None
            if cue is None and _NOTICE_RE.search(blocks[first].text):
                cue = "boilerplate"
            if cue is not None:
                mark(first, unit_end[first], cue)
    # Cue headings open sections.
    for i, b in enumerate(blocks):
        if b.kind == "heading":
            cue = heading_cue(b.text, back=i > n // 2)
            if cue is not None:
                mark(i, _section_end(blocks, i, unit_end[i] if units else n, contents=cue == "contents" and not units), cue)

    body = body_start if body_start is not None and not blocks[min(body_start, n - 1)].role else None
    if body is None:
        body = guess_body_start(blocks, declared, title=title)
    for i in range(min(body, n)):
        if not blocks[i].role and _dedication(blocks[i], keys):
            readable[i] = True
        if declared[i] == FRONT and not blocks[i].role and not readable[i]:
            blocks[i].role = "frontmatter"
    for i, b in enumerate(blocks):
        if not b.role and b.kind != "image" and not readable[i] and is_notice(b.text, front=i < body):
            b.role = "boilerplate"

    _front_zone(blocks, body, readable, starts if units else None, title_keys(title) | {letters_key(author)})
    _title(blocks, title, author, body, declared, readable, title_group, starts if units else None)
    _after_the_end(blocks)


def number_headings(items: list, deleted: Callable[[list[tuple[int, int, int]]], None]) -> list:
    """Headings whose number is set apart, as calibre and many publishers print them:
    - "PART" + "One" → one heading "PART One";
    - "1" (a paragraph or a heading) + the heading "Are You Smart…" → one heading
      "1 Are You Smart…";
    - a lone numeral right after "Chapter 3" that only repeats its number is dropped
      (deleted() gets its source ranges, for original.html);
    - lone numerals that open long stretches of text, in sequence ("1", "2", "3"… under
      the parts of Maurice), are the chapter headings, a level below the heading before
      them. Numbered list items and rating scales are close together and stay as they are.
    items: drafts with .kind, .level and .mapped (and .anchors when they have them)."""
    n = len(items)
    texts = [it.mapped.text.strip() for it in items]
    numeral = [it.kind == "paragraph" and is_number(t) for it, t in zip(items, texts)]
    join_back: set[int] = set()  # joins the heading before it
    join_on: set[int] = set()  # takes in the heading after it
    dropped: set[int] = set()
    for i in range(n - 1):
        word, nxt = texts[i].rstrip(" .:"), texts[i + 1]
        if (
            items[i].kind == "heading" and _SECTION_WORD.match(word) and items[i + 1].kind in ("paragraph", "heading")
            and 0 < len(nxt.split()) <= 4 and HEADING_RE.match(f"{word} {nxt}")
        ):
            join_back.add(i + 1)
    for i in range(1, n):
        if (
            numeral[i] and i not in join_back and items[i - 1].kind == "heading" and _CHAPTER_HEADING.match(texts[i - 1])
            and _number_value(texts[i]) == _last_number(texts[i - 1])
        ):
            dropped.add(i)
    for i in range(n - 1):
        number = numeral[i] or (items[i].kind == "heading" and is_number(texts[i]))
        if number and i not in join_back | dropped and items[i + 1].kind == "heading" and not is_number(texts[i + 1]):
            join_on.add(i)
    # Lone numerals opening at least 150 words of text before the next numeral or heading.
    words, candidates = 0, []
    for i in range(n - 1, -1, -1):
        if numeral[i] and i not in join_back | join_on | dropped:
            if words >= 150:
                candidates.append(i)
            words = 0
        elif items[i].kind == "heading":
            words = 0
        else:
            words += len(texts[i].split())
    candidates.reverse()
    values = [_number_value(texts[i]) for i in candidates]
    steps = list(zip(values, values[1:]))
    if len(candidates) >= 3 and sum(b == a + 1 or b == 1 for a, b in steps) >= 0.6 * len(steps):
        named = 1
        chosen = set(candidates)
        for i in range(n):
            if items[i].kind == "heading" and not is_number(texts[i]):
                named = items[i].level or 1
            elif i in chosen:
                items[i].kind, items[i].level = "heading", named + 1
    out: list = []
    for i, it in enumerate(items):
        if i in join_back or (i - 1 in join_on):
            head = out[-1]
            if i - 1 in join_on:
                head.kind, head.level = "heading", it.level
            head.mapped = head.mapped.join(it.mapped, " ")
            if hasattr(head, "anchors"):
                head.anchors += it.anchors
        elif i in dropped:
            deleted(it.mapped.delete([(0, len(it.mapped.text))]))
            if hasattr(it, "anchors") and out:
                out[-1].anchors += it.anchors
        else:
            out.append(it)
    return out


def guess_body_start(blocks: list[Block], declared: list[str] | None = None, targets: list[int] = (), title: str = "") -> int:
    """Where the book itself probably starts: a declared readable section, the first
    chapter-like heading, the first contents target past the front matter, or the first
    real paragraph that isn't a notice, a copyright page or a contents list."""
    n = len(blocks)
    declared = declared or [""] * n
    limit = max(30, n // 5)
    keys = title_keys(title)
    for i, b in enumerate(blocks[:limit]):
        if declared[i] == READ:
            return i
        if b.kind != "heading" or b.role or letters_key(b.text) in keys:
            continue
        cue = heading_cue(b.text)
        if cue == READ or (cue is None and HEADING_RE.match(b.text.strip())):
            return i
    first_text = next((i for i, b in enumerate(blocks) if b.text.strip()), 0)
    later = [t for t in targets if first_text < t < limit]  # "Start" → the first page says nothing
    if later:
        return min(later)

    def long(k: int, words: int = 40) -> bool:
        return blocks[k].kind in ("paragraph", "quote", "verse") and len(blocks[k].text.split()) >= words

    def plain(i: int) -> bool:
        text = blocks[i].text.strip()
        return (
            bool(text) and not blocks[i].role and declared[i] not in SKIPPED_ROLES and not is_notice(text, front=True)
            and not _COPYRIGHT_RE.search(text[:200]) and not re.match(r"(?:table of )?contents\b", text, re.I)
            and heading_cue(text) is None and not any(letters_key(text).startswith(k) for k in keys)
            and not _PRAISE_RE.search(text)
        )

    # the first real paragraph, or a heading opening one; else the first block that is none of the above
    for i, b in enumerate(blocks[:limit]):
        opens = b.kind == "heading" and any(long(k, 25) for k in range(i + 1, min(i + 4, n)))
        if (long(i) or opens) and plain(i):
            return i
    return next((i for i in range(min(limit, n)) if plain(i)), 0)


def link_contents(blocks: list[Block], targets: dict[int, int] | None = None) -> None:
    """Block.link for each line of the book's own contents page: the given targets
    (EPUB <a href>), else the heading the line names ("Chapter 3 ...... 41" → "Chapter 3")."""
    targets = targets or {}
    headings = _heading_keys(blocks)
    for i, b in enumerate(blocks):
        if b.role != "contents" or (b.kind == "heading" and heading_cue(b.text) == "contents"):
            continue
        target = targets.get(i)
        if target is None:
            keys = [k for k in (_entry_key(b.text), _entry_key(_LEADERS.sub("", b.text))) if k]
            target = next((h for key in keys for h, names in headings if h > i and key in names), None)
        if target is not None and target != i:
            b.link = target


def toc_from_headings(blocks: list[Block], max_level: int = 2) -> list[TocEntry]:
    """Contents entries from the book's headings; "CHAPTER ONE" followed by its
    title heading makes one entry, "Chapter One: Short Sentences"."""
    entries: list[TocEntry] = []
    skip = set()
    for i, b in enumerate(blocks):
        if i in skip or b.kind != "heading" or b.role or not b.text.strip() or b.level > max_level:
            continue
        text = display_title(b.text)
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if (
            nxt is not None and nxt.kind == "heading" and not nxt.role and HEADING_RE.match(b.text.strip())
            and not HEADING_RE.match(nxt.text.strip()) and _number_value(nxt.text) is None
            and nxt.level >= b.level and len(nxt.text) <= 120
        ):
            text = f"{text.rstrip('.:')}: {display_title(nxt.text)}"
            skip.add(i + 1)
        entries.append(TocEntry(text, i, b.level))
    return entries


def rank_levels(blocks: list[Block]) -> None:
    """Heading levels as ranks among the levels the book itself uses (top = 1)."""
    used = sorted({b.level for b in blocks if b.kind == "heading" and not b.role and b.level > 0})
    rank = {lvl: k + 1 for k, lvl in enumerate(used)}
    for b in blocks:
        if b.kind != "heading":
            b.level = 0
        elif b.level in rank:
            b.level = rank[b.level]
        else:
            b.level = max([r for lvl, r in rank.items() if lvl <= b.level] or [1])


def display_title(text: str) -> str:
    """A heading as a contents entry: one line, all-caps turned to title case."""
    s = " ".join(text.split())
    letters = [c for c in s if c.isalpha()]
    first = s.split()[0] if s else ""
    if letters and (sum(c.isupper() for c in letters) >= 0.8 * len(letters) or (first.isupper() and _SECTION_WORD.match(first))):
        s = title_case(s)  # "CHAPTER ONE", "PART One" → "Part One"
    return s


# ---------------------------------------------------------------------------


_WORD_NUMBERS = {
    w: k for k, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen"
        " seventeen eighteen nineteen twenty".split()
    )
}
_TENS_WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _number_value(text: str) -> int | None:
    """'12', 'XII', 'xii.', 'Twelve', 'Twenty-One' → the number; None for anything else."""
    s = text.strip().rstrip(".").strip()
    if re.fullmatch(r"\d{1,3}", s):
        return int(s)
    if re.fullmatch(r"[IVXLC]{1,7}|[ivxlc]{1,7}", s) and len(s) > 1 or s in ("I", "V", "X"):
        return roman_to_int(s)
    words = re.split(r"[\s-]+", s.lower())
    if len(words) == 1 and words[0] in _WORD_NUMBERS:
        return _WORD_NUMBERS[words[0]]
    if len(words) == 2 and words[0] in _TENS_WORDS and words[1] in _WORD_NUMBERS and _WORD_NUMBERS[words[1]] < 10:
        return _TENS_WORDS[words[0]] + _WORD_NUMBERS[words[1]]
    if len(words) == 1 and words[0] in _TENS_WORDS:
        return _TENS_WORDS[words[0]]
    return None


def is_number(text: str) -> bool:
    """'12', 'XII', 'Twelve': a heading that is only a chapter number."""
    return _number_value(text) is not None


def _last_number(heading: str) -> int | None:
    """The number a heading ends with: 'Chapter 3' → 3, 'CHAPTER THREE' → 3, 'Part Twenty-One' → 21."""
    words = heading.strip(" .:").split()
    for k in (2, 1):
        if len(words) > k:
            value = _number_value(" ".join(words[-k:]))
            if value is not None:
                return value
    return None


def _unit_index(starts: list[int], n: int) -> list[int]:
    out, u = [], 0
    for i in range(n):
        while u + 1 < len(starts) and starts[u + 1] <= i:
            u += 1
        out.append(u)
    return out


def _section_end(blocks: list[Block], i: int, limit: int, contents: bool = False) -> int:
    """Where a section opened by heading i ends: the next heading of the same or a higher
    level, the end of its unit (limit), or for a contents page without units, its first
    long paragraph."""
    level = blocks[i].level or 1
    for k in range(i + 1, limit):
        b = blocks[k]
        if b.kind == "heading" and (b.level or 1) <= level:
            return k
        if contents and b.kind != "heading" and len(b.text.split()) > 40:
            return k
    return limit


def _front_zone(blocks: list[Block], body: int, readable: list[bool], starts: list[int] | None, names: set[str]) -> None:
    """Contents, praise, copyright and the cover before the body (names: the title's and
    author's keys, which stay for the title page)."""
    front = range(min(body, len(blocks)))
    # A contents page without a heading: one block opening "CONTENTS", or lines ending in page numbers.
    for i in front:
        b = blocks[i]
        if (
            not b.role and not readable[i] and b.kind != "heading" and len(b.text.split()) > 1
            and re.match(r"^\s*(?:table of )?contents\b", b.text, re.I)
        ):
            b.role = "contents"
    run: list[int] = []
    for i in [*front, None]:
        b = blocks[i] if i is not None else None
        if b is not None and not b.role and b.kind != "heading" and len(b.text) < 120 and _LEADERS.search(b.text):
            run.append(i)
            continue
        if len(run) >= 3:
            for k in run:
                blocks[k].role = "contents"
        run = []
    # Praise: a front unit mostly made of signed quotations (three or more, or "Bestseller"),
    # or such quotations close together; one or two signed quotes are an epigraph.
    unit_starts = set(starts or [])
    for group in _groups(front, starts):
        texts = [
            k for k in group
            if blocks[k].text.strip() and not blocks[k].role and not readable[k] and letters_key(blocks[k].text) not in names
        ]
        # a quotation whose signature is the next block ("—Alfred Kazin") is praise too
        praised = [k for k in texts if _PRAISE_RE.search(blocks[k].text) or (k + 1 in texts and _SIGNATURE_RE.match(blocks[k + 1].text))]
        strong = len(praised) >= 3 or any(_PRAISE_WORDS.search(blocks[k].text) for k in praised)
        if not strong:
            continue
        if starts is not None and len(praised) * 2 >= len(texts):
            for k in texts:
                blocks[k].role = "blurb"
        elif starts is None:
            for a, b in zip(praised, praised[1:]):
                if b - a <= 3:
                    for k in range(a, b + 1):
                        if k in texts:
                            blocks[k].role = "blurb"
    # Copyright: a cluster of short blocks with at least one marker.
    cluster: list[int] = []

    def close() -> None:
        if any(_COPYRIGHT_RE.search(blocks[k].text) for k in cluster):
            for k in cluster:
                blocks[k].role = "copyright"
        cluster.clear()

    for i in front:
        b = blocks[i]
        if i in unit_starts:
            close()
        plain = (
            b.kind not in ("heading", "image") and b.text.strip() and not b.role and not readable[i]
            and not is_byline(b.text)
        )
        if plain and len(b.text.split()) <= 80:
            cluster.append(i)
        else:
            close()
            if plain and _STRONG_COPYRIGHT_RE.search(b.text):
                b.role = "copyright"
    close()
    # Images before the body: the first is the cover.
    if not any(b.role == "cover" for b in blocks):
        first_image = next((i for i in front if blocks[i].kind == "image"), None)
        if (
            first_image is not None and not blocks[first_image].role
            and not any(blocks[k].text.strip() and not blocks[k].role for k in range(first_image))
        ):
            blocks[first_image].role = "cover"


def _groups(front: range, starts: list[int] | None) -> list[list[int]]:
    """The front zone split into units (or one group without units)."""
    if starts is None:
        return [list(front)]
    out: list[list[int]] = []
    for i in front:
        if not out or i in starts:
            out.append([])
        out[-1].append(i)
    return out


def _title(
    blocks: list[Block], title: str, author: str, body: int, declared: list[str], readable: list[bool],
    title_group: list[int] | None, starts: list[int] | None,
) -> None:
    keys = title_keys(title)
    chosen: int | None = None
    if title_group:
        chosen = title_group[0]
        for k in title_group[1:]:
            if not blocks[k].role and not readable[k]:
                blocks[k].role = "frontmatter"
    author_key = letters_key(author)
    candidates = [i for i, b in enumerate(blocks) if b.text.strip() and b.kind != "image"]
    front = [i for i in candidates if i < body]
    if chosen is None:  # a heading on a declared title page
        chosen = next((i for i in candidates if declared[i] == TITLEPAGE and blocks[i].kind == "heading"), None)
    if chosen is None and keys:
        # A heading carrying the title: the copy with the author beside it (the title page
        # proper, not a half-title or a contents page's heading), else the last one.
        copies = [i for i in candidates if i <= max(body, 0) + 2 and blocks[i].kind == "heading" and letters_key(blocks[i].text) in keys]
        signed = [i for i in copies if any(_names_author(blocks[k].text, author_key) for k in range(i + 1, min(i + 4, len(blocks))))]
        chosen = (signed or copies[-1:] or [None])[0]
    if chosen is None and keys:  # a short front block opening with the title ("Anna Karenina by Leo Tolstoy")
        chosen = next(
            (
                i for i in front
                if len(blocks[i].text.split()) <= 25 and any(letters_key(blocks[i].text).startswith(k) for k in keys)
                and blocks[i].role in ("", "frontmatter", "copyright")
            ),
            None,
        )
    if chosen is None and keys and starts is not None:
        # A title page that spells the title over several lines ("EAST" / "OF EDEN"): its first such line.
        for u, end in zip(starts, starts[1:] + [len(blocks)]):
            unit = [k for k in range(u, min(end, body)) if blocks[k].text.strip()]
            joined = letters_key(" ".join(blocks[k].text for k in unit))
            if unit and sum(len(blocks[k].text.split()) for k in unit) <= 80 and any(k in joined for k in keys):
                chosen = next((k for k in unit if any(t.startswith(letters_key(blocks[k].text)) for t in keys)), unit[0])
                break
    if chosen is None:
        return
    blocks[chosen].role = TITLE_ROLE
    # Text pages before the title page are a blurb (back-cover copy, praise).
    if chosen < body:
        for k in range(chosen):
            if not blocks[k].role and not readable[k] and blocks[k].text.strip():
                blocks[k].role = "blurb"
    # A short title page is title-page matter all through: author, "A NOVEL", the publisher.
    if starts is not None:
        u = max(s for s in starts if s <= chosen)
        end = next((s for s in starts if s > chosen), len(blocks))
        if sum(len(blocks[k].text.split()) for k in range(u, end)) <= 80:
            for k in range(u, end):
                if k != chosen and not blocks[k].role and not readable[k]:
                    blocks[k].role = "frontmatter"
    # Every other copy of the title before the body, and "by …" or author lines beside it.
    for i in range(min(body, len(blocks))):
        b = blocks[i]
        if i == chosen or b.role or readable[i]:
            continue
        key = letters_key(b.text)
        if key in keys:
            b.role = "frontmatter"
        elif abs(i - chosen) <= 3 and _names_author(b.text, author_key):
            b.role = "frontmatter"


def _names_author(text: str, author_key: str) -> bool:
    """'by Ana Lima', 'ANA LIMA', 'Translated by…'."""
    return is_byline(text) or bool(author_key and letters_key(text) == author_key)


def _after_the_end(blocks: list[Block]) -> None:
    n = len(blocks)
    for i in range(n - 1, max(-1, n - max(60, n // 20)), -1):
        if blocks[i].kind != "heading" and _END_RE.match(blocks[i].text.strip()):
            for k in range(i + 1, n):
                if blocks[k].kind == "heading":
                    break
                if not blocks[k].role:
                    blocks[k].role = "blurb"
            return


def _heading_keys(blocks: list[Block]) -> list[tuple[int, set[str]]]:
    """Each heading and the names a contents line may give it (with its title heading joined)."""
    out = []
    for i, b in enumerate(blocks):
        if b.kind != "heading" or b.role:
            continue
        names = {_entry_key(b.text)}
        if i + 1 < len(blocks) and blocks[i + 1].kind == "heading":
            names |= {_entry_key(b.text + " " + blocks[i + 1].text), _entry_key(blocks[i + 1].text)}
        out.append((i, {k for k in names if k}))
    return out


def _entry_key(text: str) -> str:
    """Letters and digits only, in capitals: "Chapter 3: The Storm" → "CHAPTER3THESTORM"."""
    return re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKD", text).upper())
