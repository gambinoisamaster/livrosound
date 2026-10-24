"""Text repair shared by the ingesters: what a book's text needs before it can
be shown and read aloud.

Text out of PDFs (and ebooks made from them) arrives broken: words split across
lines ("independ-" / "ent"), ligature glyphs (ﬁ), invisible marks, letter-spaced
capitals ("A R T F U L"), running heads, page numbers and scanner notices.
Ported from scan-to-book's textfix.py.

The rules that aren't obvious:
- The dictionary is macOS's /usr/share/dict (Webster's 1934 web2 plus proper
  names) with common inflections and British spellings, plus the irregular
  forms and newer words it lacks ("has", "began", "withdrew", "online"). A
  word also counts when it appears whole elsewhere in the book (Vocab): the
  book is the best judge of its own spelling ("to-day").
- A line-end hyphen is dropped only for a syllable break ("inalien-" / "able").
  Compounds keep it ("fair-" / "weather", "self-" / "conceit"), and so do
  suspended hyphens ("nineteenth-" / "and twentieth-century") and capitalised
  halves ("Anglo-" / "Saxon").
- normalize_chars removes soft hyphens, except one at a line end inside the
  text, which becomes "-" so join_mode can still decide that line break.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

DICT_DIR = Path("/usr/share/dict")

LETTER = "A-Za-zÀ-ÖØ-öø-ÿĀ-žæœÆŒ"
WORD_RE = re.compile(rf"[{LETTER}]+(?:[-'’][{LETTER}]+)*")
SOFT_HYPHEN = "\u00ad"

TERMINAL = tuple(".?!:;\"”’')]»…")

_ROMAN = r"(?=[IVXLCDM])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
_SMALL_ROMAN = r"(?=[IVXLC])C{0,3}(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"  # I..CCCXCIX: chapter numbers
_TENS = r"(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
_UNITS = r"(?:one|two|three|four|five|six|seven|eight|nine)"
_NUMBER_WORD = (
    rf"(?:{_TENS}(?:[- ]{_UNITS})?|{_UNITS}|ten|eleven|twelve|thirteen|fourteen|fifteen"
    r"|sixteen|seventeen|eighteen|nineteen|hundred)"
)
_ORDINAL = (
    rf"(?:{_TENS}[- ](?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)"
    r"|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth"
    r"|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|nineteenth"
    r"|twentieth|thirtieth|fortieth|fiftieth|last|final)"
)
_KEYWORD = (
    r"(?:chapter|chap\.|book|part|section|canto|act|scene|letter|essay|volume|vol\."
    r"|stave|fytte|psalm|lecture|lesson)"
)
_DESIGNATOR = rf"(?:{_SMALL_ROMAN}|\d{{1,3}}|{_NUMBER_WORD}|(?:the\s+)?{_ORDINAL})"
_STANDALONE = (
    rf"(?:preface|introduction|prologue|epilogue|(?:table\s+of\s+)?contents"
    rf"|appendix(?:\s+(?:[A-Z]|\d{{1,2}}|{_SMALL_ROMAN}))?"
    r"|foreword|afterword|acknowledge?ments|dedication|postscript|interlude|glossary"
    r"|bibliography|index|notes|endnotes|epigraph|conclusion)"
)
# A title may follow after a separator ("Chapter IV. The Storm") or, in an
# all-caps heading, after a space ("CHAPTER IV THE STORM"). Never a lowercase
# run without a separator: "Part of the problem" is prose.
_TITLE_TAIL = r"(?:\s*[.:—–-]\s*\S.*|(?-i:\s+[^a-z]+))"
HEADING_RE = re.compile(
    rf"^(?=.{{1,80}}$)(?:"
    rf"{_KEYWORD}(?:\s+{_DESIGNATOR}{_TITLE_TAIL}?)?"
    rf"|{_STANDALONE}(?:\s*[.:—–-]\s*\S.*)?"
    rf"|(?-i:{_SMALL_ROMAN}(?:\.\s+[^a-z]+)?)"  # a lone "IV", "IV.", "IV. THE STORM"
    r")[.:]?\s*$",
    re.I,
)
# "14", "[p. 3]", "Page 14", "- 14 -", "xii". An OCR'd page number may read l
# or I for 1 ("l4") but needs a digit; roman page numbers are lowercase (front
# matter), since a lone uppercase "IV" is a heading.
PAGE_NUMBER_RE = re.compile(
    r"^(?:[\[(]?(?:p(?:age)?\.?\s*)?(?=[lI]*\d)[0-9lI]{1,4}[\])]?"
    r"|[-–—]\s*\d{1,4}\s*[-–—]"
    r"|(?-i:(?=[ivxlc])c{0,3}(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3}))\.?)$",
    re.I,
)

# ---------------------------------------------------------------------------
# Dictionary

# Words web2 lacks: irregular forms (it lists base forms), contractions, and
# newer words. Without them "began" would split into "be gan", "withdrew" into
# "with drew", "online" into "on line", "offence" into "of fence".
_EXTRA_WORDS = frozenset(
    """
    am are is was were been being has had having does did done doing
    became become began begun beheld befell befallen begat begot begotten bespoke betook bethought
    arose arisen awoke awoken abode
    blew flew drew grew knew threw withdrew withdrawn outgrew overgrew
    blown flown grown known thrown drawn shown sewn sown mown
    came overcame went gone ran sang sank swam rang drank
    sung sunk swum rung drunk shrank shrunk sprang sprung stank stunk
    wrote written spoke spoken broke broken chose chosen froze frozen stole stolen
    woke wore worn tore torn bore borne swore sworn shook shaken took taken
    mistook mistaken undertook undertaken partook partaken
    forgot forgotten forgave forgiven forgone forbade forbore forborne forsook forsaken
    forswore forsworn foresaw foreseen foretold
    got gotten gave given ate eaten fell fallen saw seen
    rode ridden rose risen strove striven drove driven dove wove woven hid hidden
    bit bitten beat beaten lain lay slain slew
    held withheld withstood upheld
    paid laid said made meant sent spent lent bent built dealt felt kept left slept swept wept knelt
    lost met led misled fed fled sped bled bred shot lit slid sat spat stood understood
    hung clung flung slung strung stung swung wrung dug struck stuck won spun shone heard
    sought bought brought caught taught thought fought
    told sold inlaid inbred overheard
    women men children feet teeth geese mice oxen lice
    can't won't shan't ain't
    online offline inbox email website internet okay anymore anytime byproduct
    lifestyle teenager laptop smartphone cellphone byline onscreen onstage onboard inline
    infighting innards ahold aforementioned apolitical anhedonia anhedonic asshole martini
    indie helipad heatwave awright
    """.split()
)

# (suffix, replacement): "studies" -> "study", "hoping" -> "hope", "wolves" -> "wolf".
_SUFFIXES = (
    ("s", ""), ("es", ""), ("ies", "y"), ("ied", "y"), ("ier", "y"), ("iest", "y"),
    ("ily", "y"), ("iness", "y"), ("ves", "f"), ("ves", "fe"), ("men", "man"),
    ("ed", ""), ("ed", "e"), ("d", ""), ("ing", ""), ("ing", "e"),
    ("er", ""), ("er", "e"), ("ers", ""), ("ers", "e"), ("est", ""), ("est", "e"),
    ("ly", ""), ("ly", "le"), ("ness", ""), ("ment", ""), ("ments", ""), ("ful", ""),
    ("less", ""), ("ity", ""), ("ation", "e"), ("ations", "e"), ("ish", ""), ("ism", ""),
    ("ist", ""), ("able", ""), ("able", "e"), ("eth", ""), ("eth", "e"),
)
# Only plain inflections, for judging capitalised tokens: "Atwater" must not
# pass as "a" + "twat-er".
_STRICT_SUFFIXES = (
    ("s", ""), ("es", ""), ("ies", "y"), ("ied", "y"), ("ed", ""), ("ed", "e"),
    ("ing", ""), ("ing", "e"), ("ly", ""),
)
# Derivational suffixes need a stem of four letters or more, or word tails pass
# as words: "ication" is not "ice" + "ation", "lueless" not "lue" + "less".
_DERIVATIONAL = frozenset("ation ations ness ment ments ful less ity ish ism ist able ily iness".split())
_CONTRACTIONS = ("n't", "'s", "'ll", "'re", "'ve", "'d", "'m")
# web2 is American: "offence" -> "offense", "colour" -> "color", "centre" -> "center".
_BRITISH = (
    ("our", "or"), ("ence", "ense"), ("tre", "ter"), ("ise", "ize"), ("yse", "yze"),
    ("ae", "e"), ("oe", "e"), ("ogue", "og"), ("lled", "led"), ("lling", "ling"),
)


def _read_dict(*names: str) -> list[str]:
    """Words of the named lists; on macOS "words" is a link to "web2", read once."""
    words: list[str] = []
    for path in dict.fromkeys((DICT_DIR / name).resolve() for name in names):
        if path.exists():
            words += path.read_text(encoding="utf-8", errors="ignore").split()
    return words


@functools.lru_cache(maxsize=1)
def _words() -> frozenset[str]:
    return frozenset(_EXTRA_WORDS | {w.lower() for w in _read_dict("web2", "words", "propernames")})


@functools.lru_cache(maxsize=1)
def _lowercase_words() -> frozenset[str]:
    """The dictionary without words that exist only as names ("Asa", "Indian"):
    in lowercase, "asa" is "as a"."""
    return frozenset(_EXTRA_WORDS | {w for w in _read_dict("web2", "words") if w.islower()})


@functools.lru_cache(maxsize=1)
def _compounds() -> frozenset[str]:
    """Hyphenated compounds from web2a ("fair-weather")."""
    return frozenset(w.lower() for w in _read_dict("web2a") if "-" in w)


def _in_dictionary(w: str, words: frozenset[str], strict: bool) -> bool:
    if w in words:
        return True
    for suffix, replacement in _STRICT_SUFFIXES if strict else _SUFFIXES:
        if not w.endswith(suffix):
            continue
        stem = w[: -len(suffix)] + replacement
        if len(stem) < (4 if suffix in _DERIVATIONAL else 3):
            continue
        if stem in words:
            return True
        # doubled consonant: "running" -> "run", "stopped" -> "stop"
        if suffix in ("ing", "ed", "er", "est") and len(stem) >= 4 and stem[-1] == stem[-2] and stem[:-1] in words:
            return True
    return False


@functools.lru_cache(maxsize=1 << 17)
def _known(w: str, lowercase_only: bool = False, strict: bool = False) -> bool:
    """Dictionary check of a lowercase word, allowing inflections (only plain
    ones when strict) and British spellings; with lowercase_only, words that
    exist only as names don't count."""
    words = _lowercase_words() if lowercase_only else _words()
    if "-" in w:
        return w in _compounds() or all(part and _known(part, lowercase_only, strict) for part in w.split("-"))
    if "'" in w or "’" in w:
        w = w.replace("’", "'")
        if w in words:
            return True
        for ending in _CONTRACTIONS:
            if w.endswith(ending) and len(w) > len(ending):
                return _known(w[: -len(ending)], lowercase_only, strict)
        return False
    if _in_dictionary(w, words, strict):
        return True
    return any(old in w and _in_dictionary(w.replace(old, new), words, strict) for old, new in _BRITISH)


def is_word(word: str, vocab: Vocab | None = None) -> bool:
    """True if `word` is in the dictionary (allowing common inflections), or
    already appears whole elsewhere in the book."""
    w = word.lower().strip("'’")
    if not w:
        return False
    if vocab is not None and vocab.count(w):
        return True
    return _known(w)


@dataclass
class Vocab:
    """Word counts of the book itself, ignoring words broken at line ends."""

    counts: Counter[str] = field(default_factory=Counter)

    @classmethod
    def from_texts(cls, texts: Iterable[str]) -> Vocab:
        vocab = cls()
        for text in texts:
            vocab.add(text)
        return vocab

    def add(self, text: str) -> None:
        for line in text.split("\n"):
            line = line.rstrip()
            tokens = WORD_RE.findall(line)
            if tokens and line.endswith(("-", SOFT_HYPHEN)):
                tokens = tokens[:-1]
            if tokens and line[:1].islower():
                tokens = tokens[1:]  # may be the tail of a word broken on the line above
            self.counts.update(t.lower() for t in tokens)

    def count(self, word: str) -> int:
        return self.counts.get(word.lower(), 0)


# ---------------------------------------------------------------------------
# Characters and lines

_CHAR_MAP = str.maketrans(
    {
        "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",  # ligatures
        "\ufb05": "st", "\ufb06": "st",
        "\u200b": None, "\u200c": None, "\u200d": None, "\u2060": None, "\ufeff": None,  # zero-width marks, BOM
        "\u2028": "\n", "\u2029": "\n",
        "\t": "    ",  # keeps first-line indents measurable
    }
)
# Æ/æ and Œ/œ are real letters in older English ("prætorship"), so they stay.
_ODD_SPACES = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")


def normalize_chars(text: str) -> str:
    """Unicode clean-up: line endings, ligatures, odd spaces, invisible marks, soft hyphens."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_CHAR_MAP)
    text = _ODD_SPACES.sub(" ", text)
    text = re.sub(r"¬ *\n", "-\n", text)  # some OCR marks line-end hyphens with ¬
    text = re.sub(rf"{SOFT_HYPHEN} *\n", "-\n", text)
    return text.replace(SOFT_HYPHEN, "")


# Common two-letter words, for rejoining letter-spaced OCR ("O n" -> "On").
# Two single letters are too ambiguous to join on dictionary evidence alone.
_TWO_LETTER_WORDS = frozenset(
    "ah am an as at be by do go he if in is it lo me my no of oh on or ox so to up us we ye".split()
)
_SPACED_RUN = re.compile(rf"(?<![\w'’])((?:[{LETTER}] )+[{LETTER}])(?![\w'’])")


def _segment(caps: str) -> str:
    """Split a run of capitals with lost word gaps ('MARCUSTULLIUS') into
    dictionary words when that fully works; otherwise leave it whole."""
    words, n = _words(), len(caps)
    best: list[tuple[int, list[str]] | None] = [None] * (n + 1)
    best[0] = (0, [])
    for i in range(n):
        if best[i] is None:
            continue
        for j in range(i + 2, min(n, i + 20) + 1):
            if caps[i:j].lower() in words:
                score = best[i][0] + (j - i) ** 2
                if best[j] is None or score > best[j][0]:
                    best[j] = (score, best[i][1] + [caps[i:j]])
    return " ".join(best[n][1]) if best[n] else caps


def fix_spaced_letters(line: str) -> str:
    """Rejoin letter-spaced words from old OCR: 'A R T F U L' -> 'ARTFUL', 'N o w' -> 'Now'."""

    def join(m: re.Match) -> str:
        run = m.group(1)
        letters = run.replace(" ", "")
        lower = letters.lower()
        if len(letters) == 2:
            ok = lower in _TWO_LETTER_WORDS and not (letters[1].isupper() and letters[0].islower())
        elif letters.isupper():
            ok = len(letters) >= 4 or is_word(lower)
        elif letters[1:].islower():  # "N o w", "w h o"
            ok = is_word(lower)
        else:
            ok = False
        if not ok:
            return run
        if letters.isupper() and len(letters) > 10 and not is_word(lower):
            return _segment(letters)
        return letters

    return _SPACED_RUN.sub(join, line)


# ---------------------------------------------------------------------------
# Joining lines

_TAIL_WORD = re.compile(rf"([{LETTER}'’]+)-$")
_HEAD_WORD = re.compile(rf"([{LETTER}]+(?:['’][a-z]{{1,2}}\b)?)")
# Prefixes that end a suspended hyphen without being words: "pre- and post-war".
_SUSPENDED_PREFIXES = frozenset("pre post anti pro over under inter intra semi sub super non multi mid".split())


def _keep_hyphen(left: str, right: str, vocab: Vocab | None) -> bool:
    """Decide a line-end hyphen: True keeps 'left-right', False joins 'leftright'."""
    if right[:1].isupper() and not (left.isupper() and right.isupper()):
        return True  # "Anglo-" / "Saxon"
    lo = left.lower()
    ro = re.sub(r"['’]d$", "ed", re.sub(r"['’]st$", "est", right.lower()))  # "ar-" / "rang’d" -> "arranged"
    hyphenated, joined = f"{lo}-{ro}", lo + ro
    if vocab is not None:
        h, j = vocab.count(hyphenated), vocab.count(joined)
        if h != j:
            return h > j
    if ro in ("and", "or", "nor", "to") and not _known(joined) and (_known(lo) or lo in _SUSPENDED_PREFIXES):
        return True  # suspended: "nineteenth- and twentieth-century"
    if hyphenated in _compounds():
        return True
    if _known(joined):
        return False
    if len(lo) >= 2 and len(ro) >= 2 and _known(lo) and _known(ro):
        return True  # two real words: "boundary-" / "line", "self-" / "conceit"
    return False  # a syllable break: "inalien-" / "able"


def join_mode(prev: str, nxt: str, vocab: Vocab | None = None) -> str:
    """How line `nxt` attaches to the paragraph so far, `prev`:
    "space" (the usual), "drop-hyphen" (drop prev's last character, a line-end
    hyphen or soft hyphen, and join: "independ-" + "ent"), or "join" (no space,
    keep every character: "friendship—" + "what", "fair-" + "weather").
    An empty side gives "join", so no stray space is added."""
    prev, nxt = prev.rstrip(), nxt.lstrip()
    if not prev or not nxt:
        return "join"
    if prev.endswith(SOFT_HYPHEN):
        return "drop-hyphen"
    if prev.endswith(("—", "–", "--")):
        before = prev[:-2] if prev.endswith("--") else prev[:-1]
        return "space" if not before or before.endswith(" ") else "join"
    if nxt.startswith(("—", "–")):
        return "space" if nxt[1:2] == " " else "join"  # "How so?" / "—and the dog": a closed dash
    if prev.endswith("-"):
        if prev == "-" or prev.endswith(" -"):
            return "space"
        if nxt.startswith("-"):
            return "join"  # a dash split over two lines: "paper -" / "-"
        tail, head = _TAIL_WORD.search(prev), _HEAD_WORD.match(nxt)
        if tail and head:
            return "join" if _keep_hyphen(tail.group(1), head.group(1), vocab) else "drop-hyphen"
        return "join"  # "1914-" / "1918"
    return "space"


# ---------------------------------------------------------------------------
# Headings and notices

_ROMAN_RE = re.compile(_ROMAN)
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(token: str) -> int | None:
    """'IV' -> 4, 'xlii' -> 42, 'XII.' -> 12; None unless a well-formed numeral."""
    s = token.strip().rstrip(".").upper()
    if not s or not _ROMAN_RE.fullmatch(s):
        return None
    values = [_ROMAN_VALUES[c] for c in s]
    return sum(-v if v < nxt else v for v, nxt in zip(values, values[1:] + [0]))


# Lowercase inside a title unless first or last (Chicago style, prepositions included).
_SMALL_WORDS = frozenset(
    "a an the and but or nor for as at by in of on per to via vs from into onto upon with than".split()
)
_EDGE_PUNCT = re.compile(rf"^([^{LETTER}0-9]*)(.*?)([^{LETTER}0-9]*)$", re.S)


def _case_word(word: str, capital: bool) -> str:
    if not any(c.isalpha() for c in word):
        return word
    if not (word.isupper() or word.islower() or (word[:1].isupper() and word[1:].islower())):
        return word  # inner capitals were meant: "McCarthy", "iPhone"
    if set(word.upper()) <= set("IVXLC") and roman_to_int(word):
        return word.upper()  # "Chapter IV", "Henry VIII"
    low = word.lower()
    if not capital and low in _SMALL_WORDS:
        return low
    if (m := re.fullmatch(r"([odl])(['’])(\w{2,})", low)) and low != "o'clock":
        return m.group(1).upper() + m.group(2) + m.group(3).capitalize()  # "O'Neill", "D'Arcy"
    if len(low) > 3 and low.startswith("mc"):
        return "Mc" + low[2:].capitalize()
    return low[:1].upper() + low[1:]


def title_case(text: str) -> str:
    """'SHORT SENTENCES' -> 'Short Sentences', 'THE LORD OF THE RINGS' -> 'The Lord
    of the Rings', 'CHAPTER IV' -> 'Chapter IV'. Small words stay lowercase except
    first, last and after a colon, period or dash; words with inner capitals are kept."""
    tokens = list(re.finditer(r"\S+", text))
    wordy = [i for i, t in enumerate(tokens) if any(c.isalpha() for c in t.group())]
    out, pos = [], 0
    for i, t in enumerate(tokens):
        out.append(text[pos : t.start()])
        pos = t.end()
        if i not in wordy:
            out.append(t.group())
            continue
        prev = tokens[i - 1].group() if i else ""
        capital = i in (wordy[0], wordy[-1]) or prev.endswith((":", ".", "?", "!", "—", "–", "-"))
        lead, core, trail = _EDGE_PUNCT.match(t.group()).groups()
        parts = core.split("-")
        cased = [_case_word(p, capital if k == 0 else False) for k, p in enumerate(parts)]
        out.append(lead + "-".join(cased) + trail)
    out.append(text[pos:])
    return "".join(out)


# Notices scanners and libraries stamp on pages. Specific phrases match anywhere;
# the generic ones only on a short line, since prose may use the same words.
_BOILERPLATE_RE = re.compile(
    r"digiti[sz]ed by (?:the )?(?:internet archive|google|microsoft)"
    r"|internet archive (?:in (?:19|20)\d\d|with funding)"
    r"|kahle/austin|archive\.org/details|hathitrust|google-digitized|books\.google\."
    r"|google book search|digital copy of (?:a|this) book that was preserved"
    r"|(?:page|leaf) (?:has been |is |was )?(?:intentionally|deliberately) (?:left )?blank"
    r"|intentionally left blank"
    r"|scanned (?:with|by) camscanner"
    r"|this content downloaded from|all use subject to \S*jstor"
    r"|^generated (?:for|on)\b.*\bgmt\b",  # HathiTrust's stamp
    re.I,
)
_SHORT_BOILERPLATE_RE = re.compile(
    r"^(?:(?:in (?:19|20)\d\d )?with funding from\b|digiti[sz]ed by\b|original from\b"
    r"|(?:https?://|www\.)\S+$"
    r"|[\w.-]+\.(?:org|com|net|edu|gov)(?:/\S*)?$)",
    re.I,
)


def looks_like_boilerplate(line: str) -> bool:
    """A scanner's or library's notice, not the book: "Digitized by the Internet
    Archive in 2022 with funding from Kahle/Austin Foundation", a bare URL,
    "This page intentionally left blank", Google Books and JSTOR stamps."""
    s = " ".join(line.split())
    if not s:
        return False
    if _BOILERPLATE_RE.search(s):
        return True
    return len(s.split()) <= 8 and bool(_SHORT_BOILERPLATE_RE.match(s))
