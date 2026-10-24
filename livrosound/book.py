"""The book model every part of LivroSound shares.

A Book is a flat list of Blocks in reading order (headings, paragraphs, block
quotes, verse, images, scene breaks) plus a table of contents pointing into it.
Whatever the source format, ingest/ produces this; the reader views display it,
speech.py turns each block into spoken chunks, the audio store keeps one piece
of audio per block, and every timeline position is (block index, ms into it).

Character offsets everywhere (highlight spans, click hits, cues) index into
Block.text, the text exactly as displayed in LivroSound's layout.

Original-layout geometry:
- PDF books carry, per block, the box of every word on its page (Block.words),
  so the page view can highlight a sentence on the page image and map a click
  on the page back to (block, char).
- EPUB/DOCX/TXT books carry an HTML rendering of the source (original.html,
  stored beside book.json). Every block's element in it is preceded by an empty
  anchor <a name="ls-b{index}"></a>, so the view can find where each block
  starts in the rendered document and align its characters from there.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Bump when ingestion changes the blocks it produces: cached book.json files
# from an older version are re-parsed (and their audio, keyed to block indices,
# is discarded).
PARSER_VERSION = 2

BLOCK_KINDS = ("heading", "paragraph", "quote", "verse", "note", "image", "separator")
# Kinds that are read aloud. "note" (footnotes) is read only when the user turns
# on "Read footnotes"; "separator" (a scene break) becomes a pause; "image" is silent.
SPOKEN_KINDS = ("heading", "paragraph", "quote", "verse", "note")

# Block.role: front/back matter that is shown but never read aloud or highlighted
# (user requirement): the cover, a blurb or back-cover copy, extra title pages,
# scan notices ("Digitized by the Internet Archive"), the copyright page,
# acknowledgements, the printed table of contents, the index.
SKIPPED_ROLES = ("cover", "blurb", "frontmatter", "boilerplate", "copyright", "acknowledgments", "contents", "index")
# The one block that announces the book: read once as "<title>, by <author>." and
# every other copy of the title is "frontmatter".
TITLE_ROLE = "title"

ANCHOR_PREFIX = "ls-b"  # <a name="ls-b12"></a> marks block 12 in original.html

# Word geometry tuple layout: (page, x0, y0, x1, y1, start_char, end_char)
Word = tuple[int, float, float, float, float, int, int]


@dataclass
class Block:
    kind: str  # one of BLOCK_KINDS
    text: str  # display text; "\n" appears only inside verse
    level: int = 0  # headings: 1 = part/chapter, 2 = section, 3 = subsection; else 0
    align: str = ""  # "" (the layout's default) | "left" | "center" | "right" | "justify"
    # Inline style runs over text: (start, end, style) with style one of
    # "i" (italic), "b" (bold), "sc" (small caps), "sup" (superscript: a note mark, never spoken).
    styles: list[tuple[int, int, str]] = field(default_factory=list)
    # Ids that point at this block (EPUB "chapter3.xhtml#sec2", "chapter3.xhtml"); used to map the TOC.
    anchors: list[str] = field(default_factory=list)
    page: int = -1  # PDF: page index (0-based) the block starts on; -1 for other formats
    # PDF only: the box of each word on its page, in PDF points from the page's top-left,
    # with the character span [start_char, end_char) of text that the word covers.
    words: list[Word] = field(default_factory=list)
    src: str = ""  # image blocks: file name under the book's res/ folder
    role: str = ""  # "" = the book itself; TITLE_ROLE; or one of SKIPPED_ROLES
    # A line of the book's own table of contents that links to a chapter: clicking it
    # jumps to this block index (-1 = not a link).
    link: int = -1

    @property
    def skipped(self) -> bool:
        return self.role in SKIPPED_ROLES

    @property
    def spoken(self) -> bool:
        return self.kind in SPOKEN_KINDS and not self.skipped and bool(self.text.strip())


@dataclass
class TocEntry:
    title: str
    block: int  # index of the block the entry jumps to
    level: int = 1  # 1 = top level


@dataclass
class PageInfo:
    """A PDF page's size in points (the page view renders pages from the source PDF)."""

    width: float
    height: float


@dataclass
class Book:
    book_id: str
    title: str
    author: str
    source_path: str
    source_kind: str  # "pdf" | "epub" | "docx" | "txt"
    blocks: list[Block] = field(default_factory=list)
    toc: list[TocEntry] = field(default_factory=list)
    pages: list[PageInfo] = field(default_factory=list)  # PDF only
    # PDF only: (x0, y0, x1, y1) in points that holds the body text on a typical page.
    # The page view zooms to this box (like zooming in on the page in Preview) so the
    # text is big enough to read and follow.
    text_box: tuple[float, float, float, float] | None = None
    scanned: bool = False  # EPUB: text that came from a scan (Internet Archive), cleaned of page debris
    fonts: list[str] = field(default_factory=list)  # EPUB: embedded font files under res/
    parser_version: int = PARSER_VERSION
    # original.html is stored beside book.json, not inside it (it can be large).
    original_html: str = field(default="", repr=False)

    # ---------- queries ----------

    def spoken_blocks(self) -> list[int]:
        """Indices of blocks that have something to read (before user options)."""
        return [i for i, b in enumerate(self.blocks) if b.spoken]

    def chapter_of(self, block_index: int) -> TocEntry | None:
        """The last TOC entry at or before a block."""
        best = None
        for entry in self.toc:
            if entry.block <= block_index:
                best = entry
            else:
                break
        return best

    # ---------- persistence ----------

    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        html = data.pop("original_html")
        _atomic_write(folder / "book.json", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        _atomic_write(folder / "original.html", html)

    @classmethod
    def load(cls, folder: Path) -> "Book | None":
        """The cached book in folder, or None if missing, unreadable or from an older parser."""
        try:
            data = json.loads((folder / "book.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("parser_version") != PARSER_VERSION:
            return None
        try:
            html_path = folder / "original.html"
            html = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
            blocks = [
                Block(
                    **{
                        **b,
                        "styles": [tuple(s) for s in b.get("styles", [])],
                        "words": [tuple(w) for w in b.get("words", [])],
                    }
                )
                for b in data.pop("blocks")
            ]
            toc = [TocEntry(**t) for t in data.pop("toc")]
            pages = [PageInfo(**p) for p in data.pop("pages")]
            text_box = data.pop("text_box")
            return cls(
                **data,
                blocks=blocks,
                toc=toc,
                pages=pages,
                text_box=tuple(text_box) if text_box else None,
                original_html=html,
            )
        except (TypeError, KeyError):
            return None


def file_book_id(path: str | Path) -> str:
    """A book's id: a fingerprint of the file's bytes, so re-uploading the same file
    (even from another folder) finds its cached text, OCR and audio."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
