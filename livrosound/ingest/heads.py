"""Running heads and page numbers: printed-page furniture that is never part of the book.

EPUBs and TXTs made from scanned books (the Internet Archive's, OCR dumps) keep
every printed page's header and footer: "10 ARTFUL SENTENCES" ([page number]
[book title]) on even pages, "SHORT SENTENCES 19" ([chapter title][page
number]) on odd ones, a lone "14", or "[54]" at the end of an Internet Archive
page. They come as their own short paragraphs or glued inside one ("…the walls
24 ARTFUL SENTENCES are white…"). The user asked that they are never read or
highlighted, so they are cut from the blocks (and, by the parsers, from the
original-layout HTML).

What counts as a running head, so real text is never cut:
- A page number (1–1500 arabic, or a roman numeral) next to a phrase in
  capitals or title case, where the phrase is the book's title, a chapter's
  title, or recurs as such a head with at least three different numbers that
  mostly increase, at about page-sized intervals ("In 1984 George Orwell",
  "Henry VIII", "1. TEMPERANCE." never qualify). Case, spacing, letter-spacing
  ("A R T F U L") and punctuation are ignored when comparing phrases.
- A chapter heading is never a running head (user correction): "CHAPTER ONE",
  "Chapter 3", "CHAPTER IV" (textutil.HEADING_RE) are read, their number is not a
  page number; nor is a phrase right after Chapter/Part/Book + number.
- A paragraph that is only a page number, when such paragraphs recur with
  mostly increasing numbers a page's worth of words apart (numbered sections
  restart and are thousands of words apart).
- A proven head phrase standing alone as its own paragraph (its number on
  another line), after the phrase's first numbered appearance (before it, it is
  the chapter's heading or a title page).
- With page_starts (an Internet Archive EPUB: one file per printed page), the
  heads are the capitals that recur at page starts ("THE OVERCOAT", OCR
  variants merged), and each page's opening is compared loosely with them,
  since OCR garbles them ("THE OVIERGOAT", "Ie GARR LAGE"); a bracketed page
  number ends each page ("…in some gov[7]": glued to a word, the word goes on
  after the page break). Only runs of mostly head-like tokens (capitals, OCR
  debris) up to the first plain lowercase word are tried, so a page opening
  with real text keeps it. A section starts where its head first matches well
  and the next pages confirm it: a clean first head becomes the section's
  heading (Cut.heading), a garbled one is reported for the contents (Cut.section).
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from livrosound.book import Block
from livrosound.sentences import split_sentences
from livrosound.textutil import (
    HEADING_RE,
    PAGE_NUMBER_RE,
    TERMINAL,
    is_word,
    roman_to_int,
)

from .textmap import MappedText

MAX_PAGE = 1500
MAX_PHRASE = 6  # words in a head's phrase
WORDS_PER_PAGE_MAX = 900  # a printed page holds far fewer words
FUZZY_MIN = 0.6  # similarity of a garbled page-start head to a clean one
FUZZY_LOOSE = 0.35  # …on a page that continues the previous page's sentence
PARAGRAPH_MIN, PARAGRAPH_MAX = 600, 1500  # characters: the paragraphs a scan's pages are cut into

# Words that make "<word> <number>" a reference or a heading, not a running head.
_KEYWORDS = frozenset(
    """chapter chap book part section canto act scene letter essay volume vol stave
    fytte psalm lecture lesson page pages p pp fig figure table plate note notes verse
    line lines number no nos appendix step day week year room""".split()
)
_SMALL = frozenset("a an the and of in on at to for by with from or nor".split())  # allowed lowercase in a title-case head
_STOPS = (".", "?", "!", ";", ":")
_NAME_CAPS = re.compile(r"^(?:Mc|Mac|Di|De|Da|Du|La|Le|Van|Von|O)[A-Z][a-z]{2,}$")
_COMMON_SHORT = frozenset(
    """A I O AN AS AT BE BY DO GO HE IF IN IS IT ME MR MY NO OF OH ON OR SO TO UP US WE YE AND ANY ARE BUT CAN DAY
    DID FOR GOT HAD HAS HER HIM HIS HOW ITS LET MAN MAY MEN MRS NEW NOR NOT NOW OFF OLD ONE OUR OUT OWN PUT SAW SAY
    SEE SHE SIR THE TOO TWO WAS WAY WHO WHY YES YET YOU ALL GET""".split()
)
_TOKEN = re.compile(r"\S+")
_ARABIC = re.compile(r"^[\[(]?(\d{1,4})[\])]?$")
_ROMAN_TOKEN = re.compile(r"^[\[(]?([ivxlc]{1,7}|[IVXLC]{2,7})[\])]?$")
# The page number closing an Internet Archive page: "[7]", "62]", "{197 ]", "(97]", "[152]:", "[81] “@".
_BRACKETED = re.compile(r"[\[({]\s*(\d{1,4})\s*[\])}](?:\s*[|\\](?!\S))?")
_PAGE_TAIL = re.compile(r"\s*(?:[\[({|]\s*\d{1,4}\s*[\])}|]?|\d{1,4}\s*[\])}])\s*(?:[^\w\s]{1,2}\s*)?$")


@dataclass
class Cut:
    block: int
    start: int
    end: int
    text: str
    kind: str = "head"  # "head" | "page-number"
    glued: bool = False  # a page number stuck to a word: the word goes on after the page break
    heading: bool = False  # the first (clean) head of a new section: keep it as the section's heading
    section: str = ""  # the first (garbled) head of a new section: its clean text, for the contents


def letters_key(text: str) -> str:
    """Capital ASCII letters only: 'A R T F U L  Sentences' and 'ARTFUL SENTENCES' compare equal."""
    text = unicodedata.normalize("NFKD", text).upper()
    return "".join(c for c in text if "A" <= c <= "Z")


def find_running_heads(
    texts: list[str],
    kinds: list[str],
    title: str,
    chapter_titles: list[str],
    page_starts: list[int] | None = None,
) -> list[Cut]:
    """The running heads and page numbers in a book's blocks, as cuts (block, start, end)."""
    known = _known_keys(title, chapter_titles)
    words_at = _word_positions(texts)
    cuts: list[Cut] = []
    numbered = _numbered_heads(texts, kinds, known, words_at)
    cuts += numbered
    head_keys = {letters_key(_phrase_of(c.text)) for c in numbered}
    cuts += _lone_page_numbers(texts, kinds, words_at)
    cuts += _bare_heads(texts, kinds, head_keys, cuts)
    if page_starts:
        cuts += _page_heads(texts, kinds, page_starts, known | head_keys, cuts)
    return _resolve(cuts)


def strip_running_heads(
    blocks: list[Block], title: str, chapter_titles: list[str], page_starts: list[int] | None = None
) -> list[Cut]:
    """Cut running heads and page numbers out of blocks in place (style runs follow;
    blocks left empty are removed). Returns what was cut, with the original block indices."""
    cuts = find_running_heads([b.text for b in blocks], [b.kind for b in blocks], title, chapter_titles, page_starts)
    by_block: dict[int, list[Cut]] = defaultdict(list)
    for cut in cuts:
        if not cut.heading:
            by_block[cut.block].append(cut)
    emptied = set()
    for index, block_cuts in by_block.items():
        block = blocks[index]
        mapped = MappedText(block.text, [], list(block.styles))
        mapped.delete(widen_cuts(block.text, [(c.start, c.end) for c in block_cuts]))
        mapped.strip()
        block.text, block.styles = mapped.text, mapped.styles
        if not block.text.strip():
            emptied.add(index)
    blocks[:] = [b for i, b in enumerate(blocks) if i not in emptied]
    return cuts


def widen_cuts(text: str, cuts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Each cut plus one neighbouring space, so 'walls 24 ARTFUL are' becomes 'walls are'."""
    out = []
    for a, b in cuts:
        if b < len(text) and text[b] == " ":
            b += 1
        elif a > 0 and text[a - 1] == " ":
            a -= 1
        out.append((a, b))
    return out


def page_break_join(left: str, right: str, glued: bool) -> str | None:
    """How a paragraph split by a page break joins up: "" (no space: a word broken
    across the page, "gov" + "ernment"), " ", or None when right starts a new paragraph."""
    left, right = left.rstrip(), right.lstrip()
    if not left or not right:
        return None
    lower = right[:1].islower()
    tail = re.search(r"([A-Za-z]+)$", left)
    head = re.match(r"([a-z]+)", right)
    if tail and head:
        a, b = tail.group(1), head.group(1)
        if glued or (not is_word(a) and not is_word(b) and is_word(a + b)):
            return ""
    if lower:
        return " "
    if left.endswith(TERMINAL):
        return None
    last, first = left.split()[-1], right.split()[0]
    if any(c.islower() for c in last) and any(c.islower() for c in first):
        return " "  # "…said to the" / "Ferson of Consequence…"
    return None


def paragraph_spans(text: str, short: int = PARAGRAPH_MIN, long: int = PARAGRAPH_MAX) -> list[tuple[int, int]]:
    """A page-sized run of text cut into paragraph-sized pieces at sentence ends, for
    scans whose paragraphs were lost (an Internet Archive EPUB has one <p> per page):
    past `short` characters, at a turn of dialogue ("…said.” / “Why…") or past the
    middle of the range; past `long`, at the next sentence end whatever it is."""
    spans = split_sentences(text)
    if len(text) <= long or len(spans) < 2:
        return [(0, len(text))]
    pieces, start = [], 0
    middle = (short + long) // 2
    for (a, b), (c, _) in zip(spans, spans[1:]):
        size = b - start
        turn = text[a:b].rstrip().endswith(("”", '"')) or text[c : c + 1] in ("“", '"')
        if size >= long or (size >= short and (turn or size >= middle)):
            pieces.append((start, b))
            start = c
    if pieces and len(text) - start < short // 3:  # a short tail stays with the piece before it
        start = pieces.pop()[0]
    pieces.append((start, len(text)))
    return pieces


# ---------------------------------------------------------------------------
# Number + phrase heads


@dataclass
class _Occurrence:
    block: int
    start: int
    end: int
    number: int
    key: str
    caps: bool
    length: int  # words in the phrase
    word: int  # position of the number in the book, in words


def _numbered_heads(texts: list[str], kinds: list[str], known: set[str], words_at: list[int]) -> list[Cut]:
    occurrences: list[_Occurrence] = []
    for b, text in enumerate(texts):
        if kinds[b] in ("image", "separator") or not any(c.isdigit() for c in text) and not _has_roman(text):
            continue
        tokens = [(m.start(), m.end(), m.group()) for m in _TOKEN.finditer(text)]
        whole_heading = HEADING_RE.match(text.strip()) is not None
        for i, (s, e, tok) in enumerate(tokens):
            number = _page_number(tok)
            if number is None or whole_heading:
                continue
            if i and tokens[i - 1][2].lower().strip(".:,") in _KEYWORDS:
                continue  # "Chapter 3 THE STORM", "page 12"
            for side in (1, -1):
                for k in range(1, MAX_PHRASE + 1):
                    j = i + side * k
                    if not 0 <= j < len(tokens):
                        break
                    phrase = tokens[i + 1 : j + 1] if side == 1 else tokens[j:i]
                    edge = phrase[0][2] if side == 1 else phrase[-1][2]
                    if _page_number(phrase[-1 if side == 1 else 0][2]) is not None:
                        break
                    if not _capitalised(edge) or edge.lower().strip(".:,") in _KEYWORDS:
                        break
                    # a phrase never runs across a sentence's end
                    if side == 1 and k > 1 and phrase[-2][2].endswith(_STOPS):
                        break
                    if side == -1 and phrase[0][2].endswith(_STOPS):
                        break
                    words = [t for _, _, t in phrase]
                    key = letters_key(" ".join(words))
                    if len(key) < 3:
                        continue
                    start = s if side == 1 else phrase[0][0]
                    end = phrase[-1][1] if side == 1 else e
                    if HEADING_RE.match(text[start:end]):
                        continue
                    caps = all(not any(c.islower() for c in w) for w in words)
                    if not caps and not all(_capitalised(w) or w.lower() in _SMALL for w in words):
                        break
                    occurrences.append(_Occurrence(b, start, end, number, key, caps, k, words_at[b] + i))
    if not occurrences:
        return []
    by_key: dict[str, list[_Occurrence]] = defaultdict(list)
    for occ in occurrences:
        by_key[occ.key].append(occ)
    good = {key for key, occs in by_key.items() if _qualifies(key, occs, known)}
    best: dict[tuple[int, int], _Occurrence] = {}
    for occ in occurrences:
        if occ.key not in good:
            continue
        slot = (occ.block, occ.word)
        if slot not in best or occ.length > best[slot].length:
            best[slot] = occ
    return [Cut(o.block, o.start, o.end, texts[o.block][o.start : o.end]) for o in best.values()]


def _qualifies(key: str, occs: list[_Occurrence], known: set[str]) -> bool:
    numbers = {o.number for o in occs}
    if key in known:
        return len(numbers) >= 2
    if len(numbers) < 3 or sum(o.caps for o in occs) < 0.8 * len(occs):
        return False
    ordered = sorted(occs, key=lambda o: o.word)
    # one occurrence per number: the same head is matched once per page
    seen, steps = set(), []
    for o in ordered:
        if o.number not in seen:
            seen.add(o.number)
            steps.append(o)
    rising = sum(b.number > a.number for a, b in zip(steps, steps[1:]))
    if rising < 0.6 * (len(steps) - 1):
        return False
    per_page = [
        (b.word - a.word) / (b.number - a.number) for a, b in zip(steps, steps[1:]) if b.number > a.number
    ]
    return bool(per_page) and statistics.median(per_page) <= WORDS_PER_PAGE_MAX


# ---------------------------------------------------------------------------
# Lone page numbers and bare head phrases


def _lone_page_numbers(texts: list[str], kinds: list[str], words_at: list[int]) -> list[Cut]:
    found = []
    for b, text in enumerate(texts):
        s = text.strip()
        if kinds[b] == "heading" or not s or not PAGE_NUMBER_RE.match(s):
            continue
        value = _page_value(s)
        if value is not None:
            found.append((b, value))
    if len(found) < 3:
        return []
    rising = sum(v2 > v1 for (_, v1), (_, v2) in zip(found, found[1:]))
    if rising < 0.7 * (len(found) - 1):
        return []
    per_page = [
        (words_at[b2] - words_at[b1]) / (v2 - v1) for (b1, v1), (b2, v2) in zip(found, found[1:]) if v2 > v1
    ]
    if not per_page or statistics.median(per_page) > WORDS_PER_PAGE_MAX:
        return []
    return [Cut(b, 0, len(texts[b]), texts[b], kind="page-number") for b, _ in found]


def _bare_heads(texts: list[str], kinds: list[str], head_keys: set[str], cuts: list[Cut]) -> list[Cut]:
    """A proven head's phrase alone in its own paragraph, after its first appearance."""
    if not head_keys:
        return []
    first_numbered = {}
    for c in cuts:
        key = letters_key(_phrase_of(c.text))
        first_numbered[key] = min(first_numbered.get(key, c.block), c.block)
    out = []
    for b, text in enumerate(texts):
        if kinds[b] in ("image", "separator") or len(text) > 120:
            continue
        key = letters_key(text)
        if key not in head_keys or HEADING_RE.match(text.strip()):
            continue
        if b > first_numbered.get(key, len(texts)):  # before it: the chapter's heading, a title page
            out.append(Cut(b, 0, len(text), text))
    return out


# ---------------------------------------------------------------------------
# Page-aware heads (one source file per printed page)


def _page_heads(
    texts: list[str], kinds: list[str], page_starts: list[int], known: set[str], cuts: list[Cut]
) -> list[Cut]:
    out: list[Cut] = []
    starts = sorted(set(page_starts))
    bounds = starts + [len(texts)]
    pages = []  # (first text block, last text block, page index) of each page with text
    for p, first in enumerate(starts):
        text_blocks = [b for b in range(first, bounds[p + 1]) if kinds[b] not in ("image", "separator") and texts[b].strip()]
        if text_blocks:
            pages.append((text_blocks[0], text_blocks[-1], p))
    # The page number closing each page; glued to a word, the word goes on on the next page.
    ends = {}  # last block → its text without the page number
    offsets = []  # page index − printed number: the scan's numbering
    numbered = []  # pages (positions in pages) that end in a page number
    for position, (_, last, index) in enumerate(pages):
        text = texts[last]
        m = _PAGE_TAIL.search(text)
        value = _page_value(m.group()) if m else None
        if m and m.start() > 0 and value and value <= MAX_PAGE:
            glued = not text[m.start()].isspace() and text[m.start() - 1].isalpha()
            out.append(Cut(last, m.start(), len(text), m.group(), kind="page-number", glued=glued))
            ends[last] = (text[: m.start()], glued)
            offsets.append(index - value)
            numbered.append(position)
    # Bracketed numbers elsewhere on a page that fit that numbering ("among [11] | the department"
    # when OCR put a drop-cap opening line after the page number).
    if offsets:
        offset = statistics.median(offsets)
        tails = {(c.block, c.start) for c in out}
        for first, last, index in pages:
            for b in range(first, last + 1):
                for m in _BRACKETED.finditer(texts[b]):
                    if (b, m.start()) not in tails and abs(index - int(m.group(1)) - offset) <= 2:
                        out.append(Cut(b, m.start(), m.end(), m.group(), kind="page-number"))
    pages = [(first, last) for first, last, _ in pages]
    # Heads opening each page: the capitals that recur at page starts are the clean heads;
    # OCR variants of one head ("THE OVERGOAT" three times) join the commonest.
    counts: dict[str, int] = defaultdict(int)
    raws: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for first, _ in pages:
        for key, raw in _caps_prefixes(texts[first]):
            counts[key] += 1
            raws[key][raw] += 1
    recurring = [k for k, n in counts.items() if n >= 3 and len(k) >= 6]
    # "A MADMAN'S" and "A MADMAN'S DIARY" both recur: keep the longer.
    recurring = [k for k in recurring if not any(o != k and o.startswith(k) and counts[o] >= 0.6 * counts[k] for o in recurring)]
    if not recurring:
        return out
    canon: dict[str, str] = {}
    for key in sorted(recurring, key=lambda k: -counts[k]):
        canon[key] = next((c for c in dict.fromkeys(canon.values()) if _similar(key, c)), key)
    for key in known:
        if len(key) >= 6 and key not in canon:
            canon[key] = next((c for c in dict.fromkeys(canon.values()) if _similar(key, c)), key)
    titles = {c: max(raws[c], key=raws[c].get) for c in set(canon.values()) if c in raws}
    body = next((i for i, (first, _) in enumerate(pages) if _opens_body(texts[first], recurring)), len(pages))
    done = {(c.block, c.start) for c in cuts}
    matches: dict[int, tuple[int, str, bool, float]] = {}
    current: set[str] = set()  # the head of the section being read, with its variants
    for p in range(body, len(pages)):
        first = pages[p][0]
        if (first, 0) in done:
            continue
        prev_end, glued = ends.get(pages[p - 1][1], (texts[pages[p - 1][1]], False)) if p else ("", False)
        continuing = glued or bool(prev_end.rstrip() and not prev_end.rstrip().endswith(TERMINAL))
        match = _best_prefix(texts[first], set(canon), continuing, current)
        if match is not None:
            matches[p] = match
            if match[3] >= FUZZY_MIN:
                current = {k for k, c in canon.items() if c == canon[match[1]]}
    good = {p: canon[m[1]] for p, m in matches.items() if m[3] >= FUZZY_MIN}
    seen: set[str] = set()
    headings = {b: letters_key(texts[b]) for b in range(len(texts)) if kinds[b] == "heading"}
    titled = set(canon) | {k for k in known if len(k) >= 4}
    # Debris of a head no clean head matches ("Marl Dee NG Veo id: PRO'S RIE IGr)") is cut
    # too, on every printed page (from the first numbered one, a translator's note included).
    start = min([body, *numbered]) if numbered else body
    position = 0
    for p in range(start, len(pages)):
        first = pages[p][0]
        if (first, 0) in done:
            continue
        debris = _garbled_run(texts[first], titled)
        if p not in matches:
            if debris:
                out.append(Cut(first, 0, debris, texts[first][:debris]))
            continue
        for b in range(position, first):
            if b in headings:
                seen |= {c for c in set(canon.values()) if _similar(headings[b], c)}
        position = first
        end, key, exact, ratio = matches[p]
        cluster = canon[key]
        # A section starts where its head first matches well, confirmed by the next pages:
        # a clean copy of the head, or two more good matches (a short head like "THE NOSE"
        # is easily matched by debris once).
        ahead = [q for q in range(p, p + 5) if good.get(q) == cluster]
        confirmed = len(ahead) >= 3 or any(matches[q][2] and canon[matches[q][1]] == cluster for q in ahead)
        new = cluster not in seen and good.get(p) == cluster and confirmed
        if new:
            seen.add(cluster)
        whole = end >= len(texts[first].rstrip())
        if new and exact and not whole:
            out.append(Cut(first, 0, end, texts[first][:end], heading=True))
        elif not (new and whole):  # a lone title on its page is the section's heading
            end = max(end, debris)  # "at BE OVERCOAT:" matched as far as "OVERCOAT"; debris may run on
            out.append(Cut(first, 0, end, texts[first][:end], section=titles.get(cluster, "") if new else ""))
    return out


def _token_kind(token: str) -> str:
    """'lower' (a lowercase word), 'frag' (lowercase but no word: debris, or the end of a word
    broken across the page), 'word' (a capitalised word), 'caps' (a word in capitals: THE,
    OVERCOAT), or 'garbled' (OCR debris: EVeSSCY, NG, Veo, IGr), '|'). The dictionary knows
    too many short words ("se", "ole", "rie"), so a short capitalised token is a word only
    when it is a common one."""
    letters = "".join(c for c in token if c.isalpha())
    if not letters:
        return "garbled"
    if letters.islower():
        return "lower" if is_word(letters) else "frag"
    if _NAME_CAPS.match(letters):
        return "word"  # McDonald, DiCaprio, O'Neill
    if any(c.isupper() for c in letters[1:]) and not letters.isupper():
        return "garbled"
    known = letters.upper() in _COMMON_SHORT if len(letters) <= 3 else is_word(letters)
    if letters.isupper():
        return "caps" if known else "garbled"
    return "word" if known else "garbled"


def _garbled_run(text: str, keys: set[str]) -> int:
    """Where OCR debris of a running head ends at the start of a page (0 = none): up to 10
    tokens ending on debris or capitals, mostly debris, and mostly capitals or with two
    tokens capitalised inside ("HESSEN EVeSSCY eS P ROS PE Cor", "Tor Beet eaVveor iene
    benRaOloek"); or close to a known head or title by letter trigrams ("at BE OVERCOAT:",
    "LEE Nose"). Prose ("The overcoat was…"), names ("Akaky Akakyevitch"), a lone acronym
    ("CIA agents…") and the end of a word broken across the page ("ernment") are left alone."""
    tokens = list(_TOKEN.finditer(text[:400]))[:11]
    kinds = [_token_kind(t.group()) for t in tokens]
    best, closest = 0, (0.0, 0)  # the last debris-shaped run; the run most like a title
    for k in range(1, min(10, len(tokens) - 1) + 1):
        if kinds[k - 1] in ("lower", "frag"):
            continue
        run = [t.group() for t in tokens[:k]]
        odd = "".join(c for t, kind in zip(run, kinds) if kind in ("caps", "garbled") for c in t if c.isalpha())
        prose = sum(kind in ("lower", "word") for kind in kinds[:k]) / k
        garbled = sum(kind in ("garbled", "frag") for kind in kinds[:k])
        upper = sum(c.isupper() for c in odd) / len(odd) if odd else 0.0
        inner = sum(1 for t in run if re.search(r"[a-z][A-Z]", t))
        shaped = (
            kinds[k - 1] != "word" and k >= 2 and garbled >= 2 and garbled >= 0.5 * k and prose <= 0.4
            and (upper >= 0.5 or inner >= 2)
        )
        if shaped:
            best = tokens[k - 1].end()
        key = letters_key(" ".join(run))
        if len(key) >= 4 and prose <= 0.5:
            dice = max((_trigram_dice(key, t) for t in keys), default=0.0)
            if dice >= 0.5 and dice > closest[0]:  # "THE OVERCOAT", not "THE OVERCOAT It"
                closest = (dice, tokens[k - 1].end())
    return max(best, closest[1])


def _trigram_dice(a: str, b: str) -> float:
    ta = {a[i : i + 3] for i in range(len(a) - 2)}
    tb = {b[i : i + 3] for i in range(len(b) - 2)}
    return 2 * len(ta & tb) / (len(ta) + len(tb)) if ta and tb else 0.0


def _opens_body(text: str, recurring: list[str]) -> bool:
    """A page of the book proper: a recurring head, then a page's worth of text (a title
    page reading "THE STORY and Other Tales" is not one)."""
    words = text.split()
    for key, raw in _caps_prefixes(text):
        if key in recurring and len(words) - len(raw.split()) >= 12:
            return True
    return False


def _caps_prefixes(text: str) -> list[tuple[str, str]]:
    """(key, raw text) of each run of capitalised words opening text: "THE OVERCOAT"."""
    out = []
    words = text.split()[:MAX_PHRASE]
    for k in range(1, len(words) + 1):
        letters = [c for c in words[k - 1] if c.isalpha()]
        if not letters or sum(c.isupper() for c in letters) * 2 < len(letters) * 1.2:
            break  # not (mostly) capitals: "THe" is, "The" is not
        raw = " ".join(words[:k])
        key = letters_key(raw)
        if len(key) >= 4:
            out.append((key, raw))
    return out


def _head_like(token: str, first: bool) -> bool:
    """Could this token be part of an OCR'd running head ("OVERCOAT", "HeEesOn", "(OWE", "|", "oY")?"""
    letters = "".join(c for c in token if c.isalpha())
    if not letters or not any(c.islower() for c in letters):
        return True
    if any(c.isupper() for c in letters[1:]):
        return True  # capitals inside a word: OCR debris
    if not letters[0].isupper():
        return False
    if first and len(letters) <= 4:
        return True  # "Te", "Ie", "Tee": a garbled "THE"
    return len(letters) <= 6 and not is_word(letters)


def _best_prefix(
    text: str, keys: set[str], continuing: bool, current: set[str] = frozenset()
) -> tuple[int, str, bool, float] | None:
    """The page-start prefix that best matches a known head: (end offset, key, exact, similarity).
    Prefixes are runs of mostly head-like tokens ending on one. Within a section its own
    head (current) wins unless another matches clearly better; on a page that continues
    the previous page's sentence a looser match is enough (such a page always opens
    with its head)."""
    tokens = []
    for t in list(_TOKEN.finditer(text[:240]))[:10]:
        letters = "".join(c for c in t.group() if c.isalpha())
        if len(letters) >= 2 and letters.islower() and is_word(letters):
            break  # a plain lowercase word: the page's own text has begun
        tokens.append(t)
    flags = [_head_like(t.group(), k == 0) for k, t in enumerate(tokens)]
    scored = []
    for k in range(1, len(tokens) + 1):
        if not flags[k - 1] or sum(flags[:k]) < 0.6 * k:
            continue
        pkey = letters_key(text[: tokens[k - 1].end()])
        for key in keys:
            if pkey and 0.5 * len(key) <= len(pkey) <= 1.8 * len(key) + 2:
                ratio = 1.0 if pkey == key else SequenceMatcher(None, pkey, key, autojunk=False).ratio()
                scored.append((ratio, k, key))
    if not scored:
        return None
    def acceptable(pick: tuple[float, int, str]) -> bool:
        return pick[0] >= FUZZY_MIN or (continuing and pick[0] >= FUZZY_LOOSE and pick[1] >= 2)

    pick = _pick(scored)
    own = [s for s in scored if s[2] in current]
    if own and pick[0] < 0.85 and pick[2] not in current and acceptable(_pick(own)):
        pick = _pick(own)
    if not acceptable(pick):
        return None
    ratio, k, key = pick
    end = tokens[k - 1].end()
    return end, key, letters_key(text[:end]) == key, ratio


def _pick(scored: list[tuple[float, int, str]]) -> tuple[float, int, str]:
    """The best match; of near-equal ones, the longest prefix."""
    top = max(r for r, _, _ in scored)
    return max((s for s in scored if s[0] >= top - 0.03), key=lambda s: (s[1], s[0]))


def _similar(a: str, b: str) -> bool:
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 6 and (a in b or b in a)) or SequenceMatcher(None, a, b).ratio() >= 0.8)


# ---------------------------------------------------------------------------
# Helpers


def _resolve(cuts: list[Cut]) -> list[Cut]:
    """One cut per stretch of text: overlapping cuts in a block merge into the widest."""
    by_block: dict[int, list[Cut]] = defaultdict(list)
    for cut in cuts:
        by_block[cut.block].append(cut)
    out = []
    for block in sorted(by_block):
        merged: list[Cut] = []
        for cut in sorted(by_block[block], key=lambda c: (c.start, -c.end)):
            if merged and cut.start < merged[-1].end:
                last = merged[-1]
                if cut.end > last.end:
                    last.end = cut.end
                    last.text = last.text + cut.text[len(last.text) - (cut.start - last.start) :]
                last.glued = last.glued or cut.glued
                continue
            merged.append(cut)
        out += merged
    return out


def _known_keys(title: str, chapter_titles: list[str]) -> set[str]:
    keys = set()
    for t in [title, *chapter_titles]:
        if not t:
            continue
        for variant in (t, re.split(r"[:;—–]|\s-\s", t)[0], re.sub(r"^(?:the|a|an)\s+", "", t, flags=re.I)):
            key = letters_key(variant)
            if len(key) >= 4:
                keys.add(key)
        # "Chapter One: Short Sentences" -> "Short Sentences"
        m = re.match(r"^\s*(?:chapter|part|book)\s+\S+\s*[:.—–-]\s*(.+)$", t, re.I)
        if m and len(letters_key(m.group(1))) >= 4:
            keys.add(letters_key(m.group(1)))
    return keys


def _word_positions(texts: list[str]) -> list[int]:
    out, total = [], 0
    for text in texts:
        out.append(total)
        total += len(text.split())
    return out


def _page_number(token: str) -> int | None:
    m = _ARABIC.match(token)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= MAX_PAGE else None
    m = _ROMAN_TOKEN.match(token)
    if m:
        return roman_to_int(m.group(1))
    return None


def _page_value(s: str) -> int | None:
    digits = re.sub(r"[^0-9lI]", "", s)
    if digits and any(c.isdigit() for c in digits):
        n = int(digits.replace("l", "1").replace("I", "1"))
        return n if n <= MAX_PAGE else None
    return roman_to_int(re.sub(r"[^ivxlcIVXLC]", "", s))


def _has_roman(text: str) -> bool:
    return re.search(r"(?<!\S)[\[(]?(?:[ivxlc]{1,7}|[IVXLC]{2,7})[\])]?(?!\S)", text) is not None


def _capitalised(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    return bool(letters) and letters[0].isupper()


def _phrase_of(head: str) -> str:
    """The words of a head without its page number."""
    return " ".join(t for t in head.split() if _page_number(t) is None)
