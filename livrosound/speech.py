"""What the narrator says for each block, in chunks small enough to voice cleanly.

A block (a paragraph, a heading, a stanza) is voiced as a series of chunks: one
per sentence, a long sentence cut at its clause punctuation, one per verse line.
Each chunk is synthesized on its own, so the audio store knows exactly when each
one is heard and the highlight follows the voice without estimating (Script
Radio had to guess sentence times inside one long utterance). Chunks stay
below the length where Pocket-TTS starts splitting text internally (50 tokens
of its small English vocabulary, about 1.5 per word), so every join between two
pieces of audio is one we control (trimmed, declicked, a chosen pause): at most
MAX_CHUNK_WORDS words of the page and MAX_SPOKEN_CHARS characters of speech.

Every chunk has two texts:
- its span [start, end) of Block.text, what the reader highlights. Spans are
  trimmed, in order, never overlap, and together cover every spoken sentence.
  Note marks ("sup" style runs, [1], *, †) are never at a span's edge.
- the text the engine is asked to say (speakable): one line, ASCII punctuation,
  never empty. Pocket-TTS's tokenizer has no token for curly quotes and emits raw
  bytes instead ("Can’t" came out mangled, "Can't" is fine), so every engine sees
  ASCII quotes only — and plain letters: most accented ones ("ï", "É") are
  missing from its vocabulary too, so "naïve" is said as "naive".

Only the book itself is read: front and back matter (Block.role in
book.SKIPPED_ROLES: the cover, copyright page, printed contents…) never is, and
the one TITLE_ROLE block announces the book once as "<title>, by <author>.".

speakable reads the page the way a narrator would: numbers, years, money and
ordinals as words; "Chapter XIV" as "Chapter fourteen" and "Henry VIII" as
"Henry the Eighth"; the abbreviations of books expanded ("e.g.", "p. 12",
"Mr."); dashes as pauses; URLs, e-mail addresses and note marks dropped. All-caps
words of 4+ letters, and every word of an all-caps line or of a run of capitals
("CHAPTER ONE" -> "Chapter One", "a sign saying THE END IS NEAR"), are read as
normal words, but initialisms stay spelled out: vowel-less ones (TV, CCTV, BBC)
always, short ones (FBI, USA) when they stand alone, and a few well-known ones
(FBI, NYPD, USSR) always. A heading or paragraph ends on a period so the voice
falls ("Chapter One."), and a footnote (read only when asked) opens with "Footnote."
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from num2words import num2words

from .book import SPOKEN_KINDS, TITLE_ROLE, Block, Book
from .sentences import split_sentences

SPEECH_VERSION = 1
MAX_CHUNK_WORDS = 30
MAX_SPOKEN_CHARS = 140  # ~0.3 Pocket-TTS tokens per character: about 44 tokens

SENTENCE_PAUSE_MS = 350
CLAUSE_PAUSE_MS = 150
VERSE_LINE_PAUSE_MS = 300
HEADING_PAUSE_MS = 900


@dataclass
class SpeechOptions:
    paragraph_pause_ms: int = 600
    read_headings: bool = True
    read_footnotes: bool = False


@dataclass
class Chunk:
    block: int
    start: int
    end: int
    text: str
    pause_after_ms: int


# ---------- blocks → chunks ----------


def is_read(block: Block, options: SpeechOptions) -> bool:
    """Will this block get audio? A separator does (silence only); an image never;
    front/back matter (the cover, copyright page, contents…) never; the book's title
    announcement always."""
    if block.skipped:
        return False
    if block.role == TITLE_ROLE:
        return True
    if block.kind == "separator":
        return True
    if block.kind not in SPOKEN_KINDS:
        return False
    if (block.kind == "heading" and not options.read_headings) or (
        block.kind == "note" and not options.read_footnotes
    ):
        return False
    return _has_alnum(_URL_RE.sub(" ", _masked(block)))


def lead_in_ms(block: Block, options: SpeechOptions) -> int:
    """Silence before the block: a breath before a chapter, a section or a scene break."""
    if not is_read(block, options):
        return 0
    if block.kind == "heading":
        return 1200 if block.level == 1 else 800
    return 1200 if block.kind == "separator" else 0


def block_chunks(book: Book, index: int, options: SpeechOptions) -> list[Chunk]:
    """The chunks that voice block `index`, in order (none for a separator or an unread block)."""
    block = book.blocks[index]
    if block.kind == "separator" or not is_read(block, options):
        return []
    if block.role == TITLE_ROLE:
        return _title_chunks(book, index)
    text = _masked(block)
    if block.kind == "verse":
        units = [(m.start(), m.end(), VERSE_LINE_PAUSE_MS) for m in re.finditer(r"[^\n]+", text)]
    else:
        units = [(a, b, SENTENCE_PAUSE_MS) for a, b in split_sentences(text)]
    chunks: list[Chunk] = []
    for a, b, unit_pause in units:
        a, b = _trim(text, a, b)
        pieces = _pieces(text, a, b) if a < b else []
        for k, (s, e, spoken) in enumerate(pieces):
            pause = unit_pause if k == len(pieces) - 1 else CLAUSE_PAUSE_MS
            if spoken:
                chunks.append(Chunk(index, s, e, spoken, pause))
            elif chunks:  # a dropped piece (a bare URL) leaves its pause behind
                chunks[-1].pause_after_ms = max(chunks[-1].pause_after_ms, pause)
    if not chunks:
        return []
    last = chunks[-1]
    last.pause_after_ms = HEADING_PAUSE_MS if block.kind == "heading" else options.paragraph_pause_ms
    if last.text[-1].isalnum():
        last.text += "."  # a finished, falling close: "CHAPTER ONE" -> "Chapter One."
    if block.kind == "note":
        # Announced so the listener knows the story paused; Pocket-TTS swallows a bare "Note:".
        first = chunks[0]
        first.text = "Footnote. " + first.text[0].upper() + first.text[1:]
    return chunks


def _title_chunks(book: Book, index: int) -> list[Chunk]:
    """The book announces itself once, whatever its title page looks like
    ("ARTFUL SENTENCES:" over "Syntax as Style"): "<title>, by <author>."."""
    block = book.blocks[index]
    start, end = _trim(block.text, 0, len(block.text))
    title = book.title.strip() or block.text.strip()
    announcement = f"{title}, by {book.author.strip()}." if book.author.strip() else f"{title}."
    spoken = speakable(announcement)
    if start >= end or not spoken:
        return []
    return [Chunk(index, start, end, spoken, HEADING_PAUSE_MS)]


# Note marks: bracketed note numbers, asterisks and daggers, superscript digits, and
# note numbers glued to a word or to its punctuation ("Vestibulitis375", "end.12"),
# as EPUBs and PDF text layers often leave them.
_NOTE_REF_RE = re.compile(
    r"\[(?:\d{1,3}|[*†‡]+)\]|[*†‡¶⁂]+|[\u00b9\u00b2\u00b3\u2070-\u2079]+"
    r"|(?=\d)(?:(?<=[a-z]{3})|(?<=[a-z][.,;:!?])|(?<=[a-z][.,;:!?][\"'\u2019\u201d)]))"
    r"\d{1,3}(?=[\s.,;:!?\"'\u2019\u201d)]|$)"
)
_NOTE_MARKER_RE = re.compile(r"^\s*(?:\d{1,3}[.)]?|[*†‡]+)(?=\s)")  # a footnote's own "1." or "*"


def _masked(block: Block) -> str:
    """Block.text with every unspoken note mark blanked out. Same length, so offsets
    still index Block.text: sentences split cleanly around the marks, and spans
    trimmed of whitespace never start or end on one."""
    text = block.text
    regions = [(max(0, s), min(len(text), e)) for s, e, style in block.styles if style == "sup"]
    regions += [m.span() for m in _NOTE_REF_RE.finditer(text)]
    if block.kind == "note":
        marker = _NOTE_MARKER_RE.match(text)
        if marker:
            regions.append(marker.span())
    if not regions:
        return text
    chars = list(text)
    for s, e in regions:
        for i in range(s, e):
            if not chars[i].isspace():
                chars[i] = " "
    return "".join(chars)


def _trim(text: str, a: int, b: int) -> tuple[int, int]:
    while a < b and text[a].isspace():
        a += 1
    while b > a and text[b - 1].isspace():
        b -= 1
    return a, b


_WORD_RE = re.compile(r"\S*[^\W_]\S*")  # a word: a token with a letter or digit in it
# Clause punctuation a long sentence may be cut after: , ; : followed by a space,
# an em dash or "--", a spaced en dash or hyphen.
_CLAUSE_RE = re.compile(r"[,;:][\"'\u2019\u201d\u00bb)\]]*(?=\s)|[\u2014\u2015]+|--+|(?<=\s)[\u2013-](?=\s)")
# Words a sentence without clause punctuation is best cut before.
_CONJUNCTIONS = {
    "and", "but", "or", "nor", "yet", "so", "for", "because", "although", "though",
    "while", "whereas", "when", "whenever", "where", "which", "who", "whom", "whose",
    "that", "if", "unless", "until", "since", "as", "after", "before", "then",
}


def _word_count(text: str, a: int, b: int) -> int:
    return len(_WORD_RE.findall(text, a, b))


def _pieces(text: str, a: int, b: int) -> list[tuple[int, int, str]]:
    """(start, end, spoken) for trimmed spans covering text[a:b], each at most
    MAX_CHUNK_WORDS words and MAX_SPOKEN_CHARS spoken characters: cut at the clause
    punctuation nearest the middle, recursively, else between words."""
    spoken = speakable(text[a:b])
    total = _word_count(text, a, b)
    if total < 2 or (total <= MAX_CHUNK_WORDS and len(spoken) <= MAX_SPOKEN_CHARS):
        return [(a, b, spoken)]
    best: tuple[float, int] | None = None
    for m in _CLAUSE_RE.finditer(text, a, b):
        left = _word_count(text, a, m.end())
        if left and _word_count(text, m.end(), b):
            distance = abs(left - total / 2)
            if best is None or distance < best[0]:
                best = (distance, m.end())
    cut = best[1] if best else _word_cut(text, a, b)
    return _pieces(text, *_trim(text, a, cut)) + _pieces(text, *_trim(text, cut, b))


def _word_cut(text: str, a: int, b: int) -> int:
    """Where to cut a sentence with no clause punctuation: before a conjunction
    near the middle if there is one, else before the middle word."""
    words = list(_WORD_RE.finditer(text, a, b))
    middle = len(words) // 2
    reach = max(1, len(words) // 6)
    near = [
        i for i in range(max(1, middle - reach), min(len(words), middle + reach + 1))
        if words[i].group(0).lower() in _CONJUNCTIONS
    ]
    return words[min(near, key=lambda i: abs(i - middle)) if near else middle].start()


# ---------- speakable ----------

_CHAR_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "\u2039": "'", "\u203a": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"', "\u00ab": '"', "\u00bb": '"',
    "\u2026": "...", "\u2044": "/", "[": "(", "]": ")",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2212": "-", "\u2015": "\u2014",
    "\u00ad": None, "\u200b": None, "\u200c": None, "\u200d": None, "\u2060": None, "\ufeff": None,
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
    "\ufb05": "st", "\ufb06": "st",
    "æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe", "ß": "ss", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L",
    "đ": "d", "Đ": "D", "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D", "ı": "i",
})
# Accented Latin letters -> their base letter: "é" -> "e", "Ö" -> "O".
_CHAR_MAP.update({
    code: base
    for code in range(0xC0, 0x250)
    if chr(code).isalpha() and (base := unicodedata.normalize("NFD", chr(code))[0]).isascii()
})
_URL_RE = re.compile(
    r"(?:https?://|ftp://|www\.)[^\s<>\"]*[^\s<>\".,;:!?)\]']|[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I
)
_SPACED_DOTS_RE = re.compile(r"\.(?: \.){2,}")  # ". . ." (a typeset ellipsis)
_DASH_RE = re.compile(r"\s*(?:\u2014+|--+)\s*|(?:^|\s+)[\u2013-]\s+")
_SLASH_WORDS_RE = re.compile(r"(?<=[A-Za-z]{2})/(?=[A-Za-z]{2})")  # "he/she" -> "he or she"
_JUNK_RE = re.compile(r"[_*~|^<>{}\\#@`]")


def speakable(text: str) -> str:
    """The words a narrator says for one passage: one line, ASCII punctuation, or ""
    when nothing in it is spoken (a bare URL, a lone note mark)."""
    text = _NOTE_REF_RE.sub(" ", unicodedata.normalize("NFC", text)).translate(_CHAR_MAP)
    text = _SPACED_DOTS_RE.sub("...", _URL_RE.sub(" ", text))
    lone = _lone_numeral(text)
    if lone:
        return lone
    all_caps = _is_all_caps(text)
    text = _SLASH_WORDS_RE.sub(" or ", _abbreviations(text).replace("and/or", "and or"))
    text = _numbers(_romans(text))
    text = _DASH_RE.sub(_dash, text).replace("\u2013", "-").replace("&", " and ")
    text = _JUNK_RE.sub(" ", text)
    text = "".join(" " if ord(c) > 127 and unicodedata.category(c)[0] in "PS" else c for c in text)
    text = _normal_case(_tidy(text), all_caps)
    return text if _has_alnum(text) else ""


def _dash(m: re.Match) -> str:
    """A dash between words is a pause, said as a comma; after a sentence's end
    ("PART II.—THE ESCAPE", "Stop!—") the period already is one."""
    before = m.string[: m.start()].rstrip("\"')")
    return " " if before.endswith((".", "!", "?", ";", ":", ",")) else ", "


def _has_alnum(text: str) -> bool:
    return any(c.isalnum() for c in text)


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r" ([,.;:!?)])", r"\1", text)  # "word ." left by a dropped note mark
    text = re.sub(r"\( ", "(", text)
    text = re.sub(r"\(\)", "", text)
    text = re.sub(r",(?: ?,)+", ",", text)
    text = re.sub(r", ?([.!?;:])", r"\1", text)  # "kind—." -> "kind."
    text = re.sub(r"\b([a-z]{3,})\.(?=[,;:])", r"\1", text)  # "chap. ii.," -> "chapter two,"
    return re.sub(r"\s+", " ", text).strip().lstrip(",;: ")


# ---------- abbreviations ----------


def _ends_sentence(text: str, i: int) -> bool:
    """Does a new sentence (or nothing) follow text[:i]? Then an expanded "etc."
    keeps its period."""
    rest = text[i:].lstrip("\"')").lstrip()
    return not rest or rest[0].isupper() or rest[0] in "\"'("


def _keep_period(word: str):
    return lambda m: word + ("." if _ends_sentence(m.string, m.end()) else "")


def _title(abbreviation: str, bare: bool = False) -> str:
    """'Mr' -> a pattern for "Mr." / "MR." before a name; with bare, also British "Mr"."""
    optional = "?" if bare else ""
    return rf"\b(?:{abbreviation}\.{optional}|{abbreviation.upper()}\.)(?=\s+[A-Z])"


_ABBREVIATION_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(p, flags), r) for p, r, flags in [
        (r"\be\.\s?g\.", "for example", re.I),
        (r"\bi\.\s?e\.", "that is", re.I),
        (r"&\s?c\.", _keep_period("et cetera"), 0),
        (r"\betc\.", _keep_period("et cetera"), re.I),
        (r"\bvs\.?(?=\s)", "versus", 0),
        (r"(?<=[a-z] )v\.(?=\s+[A-Z])", "versus", 0),  # "Roe v. Wade"
        (r"\bcf\.", "compare", 0),
        (r"\bviz\.", "namely", 0),
        (r"\besp\.", "especially", 0),
        (r"\bapprox\.", "approximately", re.I),
        (r"\bca?\.\s?(?=\d)", "circa ", 0),
        (r"\bop\.\s?cit\.", _keep_period("op cit"), re.I),
        (r"\bibid\.", _keep_period("ibid"), re.I),
        (r"\bet al\.", _keep_period("et al"), 0),
        (r"\bpp\.\s?", "pages ", 0),
        (r"\bp\.\s?(?=\d|[ivxlc]+\b)", "page ", 0),
        (r"\bnos\.\s?(?=\d)", "numbers ", re.I),
        (r"\bno\.\s?(?=\d)", "number ", re.I),
        (r"#\s?(?=\d)", "number ", 0),
        (r"#s\b", "numbers", 0),
        (r"§§\s?", "sections ", 0),
        (r"§\s?", "section ", 0),
        (r"\bvols\.", "volumes", re.I),
        (r"\bvol\.\s?", "volume ", re.I),
        (r"\b(?:chap|ch)\.\s?", "chapter ", re.I),
        (r"\bfigs\.", "figures", re.I),
        (r"\bfig\.\s?", "figure ", re.I),
        (r"\b(\d+(?:st|nd|rd|th))\s+ed\.", r"\1 edition", 0),
        (r"\beds\.", _keep_period("editors"), 0),
        (r"\bed\.(?=\s*\))", "editor", 0),
        (r"\bed\.", "edited by", 0),
        (r"\btrans\.", "translated by", 0),
        (r"\bPh\.\s?D\.", _keep_period("PhD"), 0),
        (_title("Mrs", bare=True), "Missus", 0),
        (_title("Mr", bare=True), "Mister", 0),
        (_title("Ms", bare=True), "Miz", 0),
        (_title("Dr", bare=True), "Doctor", 0),
        (_title("Messrs"), "Messieurs", 0),
        (_title("Mme"), "Madame", 0),
        (_title("Mlle"), "Mademoiselle", 0),
        (_title("Prof"), "Professor", 0),
        (_title("Gen"), "General", 0),
        (_title("Gov"), "Governor", 0),
        (_title("Sen"), "Senator", 0),
        (_title("Rev"), "Reverend", 0),
        (_title("Capt"), "Captain", 0),
        (_title("Col"), "Colonel", 0),
        (_title("Lt"), "Lieutenant", 0),
        (_title("Sgt"), "Sergeant", 0),
        (_title("Hon"), "Honorable", 0),
        (_title("Pres"), "President", 0),
        (_title("Fr"), "Father", 0),
        (_title("Mt"), "Mount", 0),
        (_title("Ft"), "Fort", 0),
        (_title("St", bare=True), "Saint", 0),  # "St. Paul"; "Baker St." (no name after it) is a street
        (r"\bSt\.", _keep_period("Street"), 0),
        (r"\bJr\.", _keep_period("Junior"), re.I),
        (r"\bSr\.", _keep_period("Senior"), re.I),
        (r"\bBros\.", _keep_period("Brothers"), re.I),
        (r"\bCo\.", _keep_period("Company"), 0),
        (r"\bInc\.", _keep_period("Incorporated"), 0),
        (r"\bLtd\.", _keep_period("Limited"), 0),
        (r"(?<=\d)\s?ft\.", " feet", 0),
        (r"(?<=\d)\s?lbs?\.", " pounds", 0),
        (r"(?<=\d)\s?oz\.", " ounces", 0),
    ]
]


def _abbreviations(text: str) -> str:
    for pattern, repl in _ABBREVIATION_RULES:
        text = pattern.sub(repl, text)
    return text


# ---------- Roman numerals ----------

_ROMAN_RE = re.compile(r"M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})")
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_LONE_NUMERAL_RE = re.compile(r"\s*([IVXLC]{2,}|[IVX])\.?\s*")  # a section number on its own: "XIV."
# Words followed by a number: "Chapter XIV" -> "Chapter fourteen", "World War II" -> "World War two".
_NUMBERED_WORDS = (
    "chapter|book|part|volume|section|act|scene|canto|stave|letter|appendix|article|psalm|"
    "lecture|lesson|plate|figure|table|episode|phase|stage|type|title|class|grade|level|"
    "war|page|pages"
)
_NUMBERED_RE = re.compile(rf"\b((?i:{_NUMBERED_WORDS}))(\s+)([IVXLCDM]+|[ivxlc]+)\b(?!'\w)")
_NAMED_RE = re.compile(r"\b([A-Z][a-z]+|[A-Z]{2,})(\s+)([IVX]{1,6})\b(?!'(?!s\b))")
# Monarchs and popes: their numbers are ordinals ("Henry VIII" -> "Henry the Eighth").
# After any other name a numeral of two letters or more is a plain number
# ("Rocky II", "Apollo XI"); a lone I, V or X is left alone ("Malcolm X", "When I").
_REGNAL_NAMES = {
    "adrian", "ahmed", "alexander", "alfonso", "alfred", "amenhotep", "anne", "antiochus",
    "basil", "benedict", "boniface", "callixtus", "carl", "carlos", "casimir", "catherine",
    "celestine", "charles", "christian", "clement", "constantine", "cyrus", "darius", "david",
    "edmund", "edward", "elizabeth", "eric", "erik", "eugene", "felipe", "ferdinand", "fernando",
    "francis", "frederick", "friedrich", "george", "gregory", "gustav", "gustavus", "haakon",
    "harald", "henry", "herod", "honorius", "innocent", "isabella", "ivan", "james", "john",
    "joseph", "juan", "julius", "justinian", "karl", "leo", "leopold", "louis", "ludwig",
    "magnus", "manuel", "martin", "mary", "maximilian", "mehmed", "michael", "murad", "napoleon",
    "nicholas", "olaf", "otto", "paul", "pedro", "peter", "philip", "philippe", "pius",
    "ptolemy", "ramesses", "ramses", "richard", "robert", "rudolf", "selim", "sixtus",
    "stephen", "suleiman", "thutmose", "umberto", "urban", "victor", "victoria", "wilhelm",
    "william", "xerxes",
}


def _roman_value(token: str) -> int | None:
    """'XIV' -> 14 (strict: 'IIII', 'VX' and '' are not numerals)."""
    token = token.upper()
    if not token or not _ROMAN_RE.fullmatch(token):
        return None
    total = 0
    for i, ch in enumerate(token):
        value = _ROMAN_VALUES[ch]
        total += -value if i + 1 < len(token) and _ROMAN_VALUES[token[i + 1]] > value else value
    return total


def _lone_numeral(text: str) -> str:
    """'XIV.' (a passage that is only a section number) -> 'Fourteen.', else ''."""
    m = _LONE_NUMERAL_RE.fullmatch(text)
    value = _roman_value(m.group(1)) if m else None
    return _cardinal(value).capitalize() + "." if value else ""


def _numbered(m: re.Match) -> str:
    word, space, numeral = m.groups()
    value = _roman_value(numeral)
    # "Part I", "Act V"; but not "the book I read", nor a lone lowercase "v" or "x".
    if value is None or (len(numeral) == 1 and (numeral.islower() or not word[0].isupper())):
        return m.group(0)
    return f"{word}{space}{_cardinal(value)}"


def _named(m: re.Match) -> str:
    name, space, numeral = m.groups()
    value = _roman_value(numeral)
    if value is None:
        return m.group(0)
    if name.lower() not in _REGNAL_NAMES:
        return m.group(0) if len(numeral) == 1 else f"{name}{space}{_cardinal(value)}"
    if numeral == "I" and re.match(r"\s+[A-Za-z]", m.string[m.end():]):
        return m.group(0)  # "Tell John I said so"
    return f"{name}{space}the {_ordinal(value).capitalize()}"


def _romans(text: str) -> str:
    return _NAMED_RE.sub(_named, _NUMBERED_RE.sub(_numbered, text))


# ---------- numbers ----------

_DIGIT_WORDS = "zero one two three four five six seven eight nine".split()
_CURRENCIES = {
    "$": ("dollar", "dollars", "cent", "cents"),
    "£": ("pound", "pounds", "penny", "pence"),
    "€": ("euro", "euros", "cent", "cents"),
    "¥": ("yen", "yen", "", ""),
}
_AMOUNT = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_MONEY_RE = re.compile(rf"([$£€¥])\s?{_AMOUNT}(?:\s+(thousand|million|billion|trillion)\b)?")
_PERCENT_RE = re.compile(rf"{_AMOUNT}\s?(?:%|per\s?cent\b)")
_FRACTIONS = {
    "½": (1, 2), "¼": (1, 4), "¾": (3, 4), "⅓": (1, 3), "⅔": (2, 3),
    "⅛": (1, 8), "⅜": (3, 8), "⅝": (5, 8), "⅞": (7, 8),
}
_UNICODE_FRACTION_RE = re.compile(r"(\d+)?\s?([½¼¾⅓⅔⅛⅜⅝⅞])")
_FRACTION_RE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_DIGIT_GROUPS_RE = re.compile(r"(?<![\d-])\d+(?:-\d+){2,}(?![\d-])")  # a phone or ID number
_RANGE_RE = re.compile(r"(?<![\d.,])(\d{1,4})\s?[\u2013\u2014]\s?(\d{1,4})(?!\d|,\d)")
_YEAR_RANGE_RE = re.compile(r"(?<![\d.,])(1[1-9]\d\d|20\d\d)-(\d\d|\d{4})(?!\d|,\d)")
_PAGE_RANGE_RE = re.compile(r"\b(pages\s+\d+)-(\d+)\b")
_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d\d)(?![\d:])(\s*[ap]\.?m\b)?", re.I)
_DEGREES_RE = re.compile(r"(\d)\s?°(?:\s?([CF])\b)?")
_ORDINAL_RE = re.compile(r"(?<![\d.,])(\d+)(?:st|nd|rd|th)\b", re.I)
_PLURAL_RE = re.compile(r"(?<![\d.,])'?(\d{1,4})(')?s\b")  # "1920s", "'60s", "747s", "1984's"
_GROUPED_RE = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d|,\d|\.\d)")
_DECIMAL_RE = re.compile(r"(?<![\w.])(\d*)\.(\d+)(?!\d|\.\d)")
_INTEGER_RE = re.compile(r"\d+")
# Numbers after these words are counted, never read as years: "page 1984".
_COUNTED_RE = re.compile(
    r"\b(?:page|pages|number|numbers|line|lines|verse|verses|figure|table|room|chapter|volume|part|section)"
    r"\s*$",
    re.I,
)


def _cardinal(n: int) -> str:
    """num2words without its commas: "one thousand two hundred and five"."""
    return num2words(n).replace(",", "")


def _ordinal(n: int) -> str:
    return num2words(n, to="ordinal").replace(",", "")


def _digits(digits: str) -> str:
    """'007' -> 'zero zero seven'."""
    return " ".join(_DIGIT_WORDS[int(d)] for d in digits)


def _decimal(whole: str, frac: str) -> str:
    """'3', '14' -> 'three point one four'."""
    return f"{_cardinal(int(whole or 0))} point {_digits(frac)}"


def _amount(number: str) -> str:
    whole, _, frac = number.replace(",", "").partition(".")
    return _decimal(whole, frac) if frac else _cardinal(int(whole))


def _plural(words: str) -> str:
    """'nineteen twenty' -> 'nineteen twenties', 'six' -> 'sixes', 'nine' -> 'nines'."""
    if words.endswith("y"):
        return words[:-1] + "ies"
    return words + ("es" if words.endswith("x") else "s")


def _padded(m: re.Match, words: str) -> str:
    """Words replacing a number, spaced off from letters it touched: "B52" -> "B fifty-two"."""
    s = m.string
    before = " " if m.start() and s[m.start() - 1].isalpha() else ""
    after = " " if m.end() < len(s) and s[m.end()].isalpha() else ""
    return before + words + after


def _money(m: re.Match) -> str:
    symbol, number, scale = m.groups()
    one, many, cent, cents = _CURRENCIES[symbol]
    if scale:
        return f"{_amount(number)} {scale} {many}"
    whole, _, frac = number.replace(",", "").partition(".")
    units = int(whole or 0)
    if frac and (len(frac) != 2 or not cent):
        return f"{_decimal(whole, frac)} {many}"
    minor = int(frac or 0)
    words = f"{_cardinal(units)} {one if units == 1 else many}"
    if minor:
        coins = f"{_cardinal(minor)} {cent if minor == 1 else cents}"
        words = f"{words} and {coins}" if units else coins
    return _padded(m, words)


def _fraction(numerator: int, denominator: int) -> str:
    """3/4 -> 'three quarters', 1/2 -> 'one half', 2/3 -> 'two thirds'."""
    if denominator == 2:
        name = "half" if numerator == 1 else "halves"
    elif denominator == 4:
        name = "quarter" if numerator == 1 else "quarters"
    else:
        name = _ordinal(denominator) + ("s" if numerator != 1 else "")
    return f"{_cardinal(numerator)} {name}"


def _unicode_fraction(m: re.Match) -> str:
    whole, symbol = m.groups()
    numerator, denominator = _FRACTIONS[symbol]
    if not whole:
        return _padded(m, _fraction(numerator, denominator))
    part = "a half" if denominator == 2 else _fraction(numerator, denominator)
    return _padded(m, f"{_cardinal(int(whole))} and {part}")


def _ascii_fraction(m: re.Match) -> str:
    numerator, denominator = int(m.group(1)), int(m.group(2))
    if not 2 <= denominator <= 10 or numerator >= denominator:
        return m.group(0)  # "9/11", "24/7": not fractions
    return _fraction(numerator, denominator)


def _time(m: re.Match) -> str:
    hour, minute = int(m.group(1)), m.group(2)
    meridiem = " " + m.group(3).strip() if m.group(3) else ""
    if hour > 24 or int(minute) > 59:
        return m.group(0)  # a ratio or a verse reference, read as numbers
    if minute == "00":
        spoken = _cardinal(hour) + ("" if meridiem else " o'clock")
    elif minute.startswith("0"):
        spoken = f"{_cardinal(hour)} oh {_cardinal(int(minute))}"
    else:
        spoken = f"{_cardinal(hour)} {_cardinal(int(minute))}"
    return spoken + meridiem


def _degrees(m: re.Match) -> str:
    scale = {"C": " degrees Celsius", "F": " degrees Fahrenheit"}.get(m.group(2) or "", " degrees")
    return m.group(1) + scale


def _plural_number(m: re.Match) -> str:
    """Decades and plural numbers: "1920s" / "1920's" -> "nineteen twenties"; but a
    possessive keeps its "'s": "1984's" -> "nineteen eighty-four's"."""
    digits, apostrophe = m.group(1), m.group(2)
    n = int(digits)
    words = num2words(n, to="year") if len(digits) == 4 and 1100 <= n <= 2099 else _cardinal(n)
    if apostrophe and not digits.endswith("0"):
        return _padded(m, words + "'s")
    if n in (100, 1000):
        words = words.removeprefix("one ")  # "100s" -> "hundreds"
    return _padded(m, _plural(words))


def _integer(m: re.Match) -> str:
    digits = m.group(0)
    n = int(digits)
    if (len(digits) > 1 and digits.startswith("0")) or len(digits) > 9:
        words = _digits(digits)  # "007", an ISBN
    elif len(digits) == 4 and 1100 <= n <= 2099 and not _COUNTED_RE.search(m.string[: m.start()][-12:]):
        words = num2words(n, to="year")
    else:
        words = _cardinal(n)
    return _padded(m, words)


def _numbers(text: str) -> str:
    text = _UNICODE_FRACTION_RE.sub(_unicode_fraction, text)
    text = _MONEY_RE.sub(_money, text)
    text = _PERCENT_RE.sub(lambda m: _padded(m, _amount(m.group(1)) + " percent"), text)
    text = _DIGIT_GROUPS_RE.sub(lambda m: ", ".join(_digits(g) for g in m.group(0).split("-")), text)
    text = _RANGE_RE.sub(r"\1 to \2", text)
    text = _YEAR_RANGE_RE.sub(r"\1 to \2", text)
    text = _PAGE_RANGE_RE.sub(r"\1 to \2", text)
    text = _TIME_RE.sub(_time, text)
    text = _DEGREES_RE.sub(_degrees, text)
    text = _FRACTION_RE.sub(_ascii_fraction, text)
    text = _ORDINAL_RE.sub(lambda m: _padded(m, _ordinal(int(m.group(1)))), text)
    text = _PLURAL_RE.sub(_plural_number, text)
    text = _GROUPED_RE.sub(lambda m: _padded(m, _amount(m.group(0))), text)
    text = _DECIMAL_RE.sub(lambda m: _padded(m, _decimal(m.group(1), m.group(2))), text)
    return _INTEGER_RE.sub(_integer, text)


# ---------- capitals ----------

_VOWELS = set("AEIOUY")
# Initialisms with a vowel, spelled out even in an all-caps line or at 4+ letters.
_INITIALISMS = {
    "FBI", "CIA", "USA", "UK", "EU", "UN", "NYC", "USSR", "UAE", "OK", "IOU", "IQ", "IRA", "IRS",
    "UFO", "ICU", "CEO", "CFO", "ATM", "MIT", "NYU", "ESP", "EKG", "MRI", "DUI", "AOL", "NYPD",
    "LAPD", "NAACP", "ACLU", "ESPN", "YMCA", "NCAA", "HIV", "ISBN", "ADHD", "ASPCA", "LGBTQIA", "UCLA",
    "USAF", "USMC",
}


def _is_all_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 2 and not any(c.islower() for c in letters)


def _title_word(word: str) -> str:
    """'BEL-AIR' -> 'Bel-Air', "O'BRIEN'S" -> "O'Brien's", "DON'T" -> "Don't"."""
    word = re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), word)
    # Contraction/possessive tails after an apostrophe stay lowercase.
    return re.sub(r"'([^\W\d_]{1,2})\b", lambda m: "'" + m.group(1).lower(), word)


def _caps_letters(word: str) -> str:
    """The letters of an all-caps word of two letters or more, else ""."""
    letters = "".join(c for c in word if c.isalpha())
    return letters if len(letters) >= 2 and letters.isupper() else ""


def _normal_case(text: str, all_caps: bool) -> str:
    """All-caps words read as words: those of 4+ letters, and every word of an
    all-caps line or of a run of capitals ("a sign saying THE END IS NEAR")."""
    words = text.split(" ")
    caps = [_caps_letters(w) for w in words]
    for i, letters in enumerate(caps):
        if not letters or letters in _INITIALISMS or not _VOWELS & set(letters):
            continue  # TV, CCTV, BBC: spelled out
        if set(letters) <= set("IVXLC") and _roman_value(letters):
            continue  # a Roman numeral, not a shouted word
        in_run = (i > 0 and caps[i - 1]) or (i + 1 < len(caps) and caps[i + 1])
        if all_caps or in_run or len(letters) >= 4:
            words[i] = _title_word(words[i])
    return " ".join(words)
