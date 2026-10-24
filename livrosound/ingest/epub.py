"""EPUB → Book: the spine's XHTML walked like a browser lays it out.

Read with zipfile + lxml (no ebooklib): META-INF/container.xml → the OPF
(title, creators, manifest, spine, EPUB2 <guide>) → the EPUB3 nav (toc and
landmarks) or the EPUB2 NCX → every linear spine document in order.

The walk turns block-level elements into Blocks: h1–h6 headings (levels later
ranked among the levels the book uses), paragraphs (p, div, li, td…),
blockquote → quote, pre or a poem's lines or a paragraph with two or more
<br> → verse, footnotes (aside/section epub:type footnote/endnote, classes
like "footnote") → note, img / svg <image> → image (copied to res/), hr or a
paragraph of only "* * *", "***", "⁂", "#" → separator. Inline em/i/cite (and
CSS classes that italicize) → "i", strong/b → "b", small caps → "sc", note
references and note-mark superscripts → "sup". Whitespace collapses exactly
as a browser shows it, and every block remembers which text nodes its
characters came from (textmap), so original.html loses exactly what the blocks
lose and gets each block's anchor right before its first character.

Anchors: a block records "file.xhtml" (its manifest href) when it is the first
block of its spine file and "file.xhtml#id" for every id on it, inside it, or
on an empty element just before it; TOC entries map to the block carrying the
anchor (else the first block of that file).

original.html is one document: the book's CSS (linked stylesheets and <style>
blocks, minus @-rules, namespaced selectors and each document's body/html
rules, which would style the whole book) + each spine body in order (its classes
on a div), sectioning elements turned into divs, images pointing at their res/
copies, internal links pointing at block anchors, and block i's anchor
<a name="ls-b{i}">…</a> holding block i's first character, inside its element.
Qt's rich text keeps a named anchor only in front of a character: one between
two block elements, opening loose text after a paragraph, or before an image is
dropped. An image's or rule's anchor holds a no-break space (in a 1px paragraph
before a rule).

Internet Archive EPUBs (one file per scanned page, "hocr-to-epub") get their
own treatment: the per-page "estimated to be only N% accurate" notices are
removed, pages under 50% are stray marks (boilerplate), running heads and the
bracketed page numbers are cut (heads.py), paragraphs split by a page break are
joined again ("gov" + "ernment"), and since the source has one <p> per page,
the text is then cut into paragraph-sized blocks at sentence ends.
"""

from __future__ import annotations

import hashlib
import html
import posixpath
import re
import shutil
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from html.entities import name2codepoint
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree
from lxml import html as lxml_html

from livrosound import paths
from livrosound.book import ANCHOR_PREFIX, Block, Book, TocEntry
from livrosound.textutil import HEADING_RE, TERMINAL, join_mode

from . import BookImportError, ImportCancelled, matter
from .heads import (
    PARAGRAPH_MAX,
    find_running_heads,
    letters_key,
    page_break_join,
    paragraph_spans,
    widen_cuts,
)
from .textmap import MappedText, TextBuilder, edit_source

OPS_NS = "http://www.idpf.org/2007/ops"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

_SKIP_TAGS = frozenset("head script style noscript template title meta link object embed iframe audio video math button input select textarea".split())
_BLOCK_TAGS = frozenset(
    """p div h1 h2 h3 h4 h5 h6 blockquote li pre dt dd figcaption td th caption address center section article
    aside header footer nav main figure ul ol dl table tbody thead tfoot tr body hgroup details summary form
    fieldset legend html""".split()
)
_DIV_TAGS = frozenset("section article aside header footer nav main figure hgroup details summary form fieldset legend address center".split())
_ITALIC_TAGS = frozenset("em i cite var dfn".split())
_BOLD_TAGS = frozenset(("strong", "b"))
_VOID = frozenset(("br", "hr", "img"))
_KEEP_ATTRS = frozenset("class style align dir colspan rowspan width height alt start type value".split())
_FONT_EXT = (".ttf", ".otf", ".woff", ".woff2")
_SEPARATOR_RE = re.compile(r"^\s*(?:(?:[*⁂•·∗✻✶⁕§~]\s*)+|#|(?:-\s*){3,}|(?:[—–]\s*){2,})\s*$")
_NOTE_MARK_RE = re.compile(r"^\s*[\[(]?(?:\d{1,3}|[*†‡§¶#]+|[a-z]|[ivx]{1,5})[\])]?\s*$")
_POEM_RE = re.compile(r"poem|verse|stanza|poetry|lyric|song|hymn", re.I)
_NOTE_CLASS_RE = re.compile(r"(?:^|[\s_-])(?:foot|end|rear)?notes?(?:$|[\s_-])|footnote|endnote", re.I)
_SMALLCAPS_CLASS_RE = re.compile(r"small-?caps|smcap|(?:^|\s)sc(?:$|\s)", re.I)
_IA_PAGE_RE = re.compile(r"(?:^|/)page_\d+\.x?html?$")
_IA_ACCURACY_RE = re.compile(r"estimated to be only\s+([\d.]+)%\s+accurate", re.I)

# epub:type / ARIA role → declared role (matter.READ / FRONT / TITLEPAGE or a skipped role)
_TYPE_ROLES = {
    "cover": "cover",
    "titlepage": matter.TITLEPAGE, "halftitlepage": matter.TITLEPAGE, "fulltitle": matter.TITLEPAGE,
    "title-page": matter.TITLEPAGE, "seriespage": "frontmatter", "other-credits": "frontmatter",
    "contributors": "frontmatter", "errata": "frontmatter", "frontispiece": "frontmatter",
    "copyright-page": "copyright", "copyright": "copyright", "imprint": "copyright", "colophon": "copyright",
    "imprimatur": "copyright",
    "toc": "contents", "toc-brief": "contents", "loi": "contents", "lot": "contents", "landmarks": "contents",
    "page-list": "contents",
    "acknowledgments": "acknowledgments", "acknowledgements": "acknowledgments",
    "index": "index",
    "dedication": matter.READ, "epigraph": matter.READ, "preface": matter.READ, "foreword": matter.READ,
    "introduction": matter.READ, "prologue": matter.READ, "preamble": matter.READ, "epilogue": matter.READ,
    "afterword": matter.READ, "conclusion": matter.READ, "chapter": matter.READ, "part": matter.READ,
    "division": matter.READ, "volume": matter.READ, "bodymatter": matter.READ, "subchapter": matter.READ,
    "frontmatter": matter.FRONT,
}
_GUIDE_ROLES = {
    "cover": "cover", "title-page": matter.TITLEPAGE, "titlepage": matter.TITLEPAGE, "toc": "contents",
    "copyright-page": "copyright", "acknowledgements": "acknowledgments", "acknowledgments": "acknowledgments",
    "index": "index", "colophon": "copyright", "loi": "contents", "lot": "contents",
    "dedication": matter.READ, "epigraph": matter.READ, "preface": matter.READ, "foreword": matter.READ,
}
_FILE_ROLES = [
    (re.compile(r"^(?:.*[_-])?cover(?:[_-]?(?:page|image)?\d*)?$", re.I), "cover"),
    (re.compile(r"^(?:.*[_-])?(?:title[_-]?page|half[_-]?title|title)(?:[_-]?\d+)?$", re.I), matter.TITLEPAGE),
    (re.compile(r"^(?:.*[_-])?(?:copyright|imprint|colophon)(?:[_-]?\d+)?$", re.I), "copyright"),
    (re.compile(r"^(?:.*[_-])?(?:toc|contents|nav)(?:[_-]?\d+)?$", re.I), "contents"),
    (re.compile(r"^(?:.*[_-])?acknowledge?ments?(?:[_-]?\d+)?$", re.I), "acknowledgments"),
]


def parse_epub(path: Path, book_id: str, progress=lambda done, total, message: None, cancelled=lambda: False) -> Book:
    """Parse an EPUB (2 or 3, zipped or an unzipped folder); images and fonts are copied
    to book_dir(book_id)/res/."""
    try:
        archive = _Folder(path) if path.is_dir() else zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise BookImportError("This EPUB is damaged or DRM-protected.") from e
    with archive:
        return _EpubReader(archive, path, book_id, progress, cancelled).read()


class _Folder:
    """An unzipped EPUB: macOS shows such a folder as a book, so it opens like one."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def namelist(self) -> list[str]:
        return [p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()]

    def read(self, name: str) -> bytes:
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root):
            raise KeyError(name)
        return path.read_bytes()

    def __enter__(self) -> _Folder:
        return self

    def __exit__(self, *exc) -> None:
        pass


# ---------------------------------------------------------------------------
# Data gathered while walking


@dataclass
class _Draft:
    kind: str
    mapped: MappedText
    file: int
    level: int = 0
    align: str = ""
    anchors: list[str] = field(default_factory=list)
    declared: str = ""
    src: str = ""  # image: zip path
    element: object = None  # image/separator: the element its anchor goes before
    hrefs: list[str] = field(default_factory=list)  # links inside (contents pages), as anchor keys
    whole_bold: bool = False
    section: str = ""  # scans: a section starts here; its clean title, for the contents
    joinable: bool = False  # scans: a page of the body, which may continue the previous page's paragraph
    glued: bool = False  # scans: its page number was stuck to its last word, which goes on on the next page
    by_name: bool = False  # declared only from its file's name (cover.xhtml), which the guide may overrule
    front_label: bool = False  # the guide or a TOC label points here as front matter ("Copyright Page")


@dataclass
class _Ctx:
    tag: str
    kind: str = "paragraph"
    level: int = 0
    align: str = ""
    declared: str = ""
    styles: frozenset = frozenset()
    pre: bool = False
    poem: bool = False
    typed: bool = False  # declared by the markup (epub:type, role), not the file name


@dataclass
class _Doc:
    index: int
    key: str  # manifest href (relative to the OPF folder): "text/chapter-1.xhtml"
    zip_path: str
    body: object  # lxml element
    accuracy: float | None = None  # Internet Archive pages: the estimated OCR accuracy


class _EpubReader:
    def __init__(self, zf: zipfile.ZipFile | _Folder, path: Path, book_id: str, progress, cancelled) -> None:
        self.zf = zf
        self.path = path
        self.book_dir = paths.book_dir(book_id)
        self.book_id = book_id
        self.progress = progress
        self.cancelled = cancelled
        self.names = set(zf.namelist())
        self.nodes: list[tuple[object, str]] = []  # source id → (element, "text" | "tail")
        self.css_text: list[str] = []
        self.css_seen: set[str] = set()
        self.class_styles: dict[str, tuple[frozenset, str, bool]] = {}
        self._style_cache: dict[tuple[str, str, str], tuple[frozenset, str, bool]] = {}
        self.head_cuts: list = []  # what heads.py cut (kept for reports and tests)

    # ---------- package ----------

    def read(self) -> Book:
        self._check_drm()
        title, authors, uid = self._package()
        title = matter.clean_title(title)  # "Blood Meridian (Picador Collection)" → "Blood Meridian"
        drafts, docs = self._walk_spine()
        toc_raw = self._toc(self.nav_path, self.ncx_path)
        targets = _resolve_toc(toc_raw, drafts)
        _promote_headings(drafts, targets)
        self._declare(drafts, targets)

        page_starts = self._scan_pages(drafts, docs) if self.scan else None
        if not title:  # e.g. an Internet Archive scan: "OVERCOAT and Other Stories by Nikolay Gogol"
            for d in drafts[:60]:
                if d.kind != "image" and not d.declared and " by " in d.mapped.text and len(d.mapped.text.split()) <= 30:
                    title, byline_author = matter.split_byline(d.mapped.text)
                    if title:
                        authors = authors or ([byline_author] if byline_author else [])
                        break
        title = title or _title_from_name(self.path)
        chapter_titles = [label for label, _, _ in toc_raw] + [d.mapped.text for d in drafts if d.kind == "heading"]
        deletions: dict[int, list[tuple[int, int]]] = defaultdict(list)
        drafts = self._cut_heads(drafts, title, chapter_titles, page_starts, deletions)
        if self.scan:
            drafts = _split_pages(self._join_pages(drafts, deletions))
        drafts = matter.number_headings(drafts, lambda ranges: [deletions[src].append((a, b)) for src, a, b in ranges])

        anchor_index = _anchor_index(drafts)
        targets = _resolve_toc(toc_raw, drafts)
        blocks = [
            Block(
                kind=d.kind, text=d.mapped.text, level=d.level, align=d.align, styles=list(d.mapped.styles),
                anchors=list(dict.fromkeys(d.anchors)),
            )
            for d in drafts
        ]
        authors = [a for a in dict.fromkeys(matter.clean_author(a) for a in authors) if a]
        if not authors and re.search(r"\s+by\s+\S", title):  # "Queer by William S Burroughs"
            title, byline_author = re.split(r"\s+by\s+", title, maxsplit=1, flags=re.I)
            authors = [byline_author.strip()]
        author = " and ".join(authors) if len(authors) <= 2 else ", ".join(authors[:-1]) + " and " + authors[-1]
        self._roles(blocks, drafts, title, author, anchor_index, targets)
        toc = self._contents(blocks, drafts, anchor_index, targets)

        res = self.book_dir / "res"
        shutil.rmtree(res, ignore_errors=True)
        res.mkdir(parents=True, exist_ok=True)
        images = self._copy_images(drafts, res)
        for d, b in zip(drafts, blocks):
            if d.kind == "image":
                b.src = images.get(d.src, "")
        fonts = self._copy_fonts(res, uid)
        original = self._original_html(docs, drafts, deletions, anchor_index, images, res)
        return Book(
            book_id=self.book_id, title=title, author=author, source_path=str(self.path), source_kind="epub",
            blocks=blocks, toc=toc, fonts=fonts, scanned=self.scan, original_html=original,
        )

    def _package(self) -> tuple[str, list[str], str]:
        """Read the OPF: metadata, manifest, spine, nav/NCX, landmarks. Returns (title, authors, identifier)."""
        opf_path = self._opf_path()
        opf = _parse_xml(self.zf.read(opf_path))
        if opf is None:
            raise BookImportError("This EPUB is damaged or DRM-protected.")
        self.opf_dir = posixpath.dirname(opf_path)
        title, authors, uid, cover_id = _metadata(opf)
        manifest = {}  # id → (zip path, media type, properties)
        for item in _all(opf, "item"):
            href = item.get("href")
            if href and item.get("id"):
                manifest[item.get("id")] = (
                    self._join(self.opf_dir, href),
                    (item.get("media-type") or "").lower(),
                    (item.get("properties") or "").split(),
                )
        self.cover_image = next((p for p, _, props in manifest.values() if "cover-image" in props), None)
        if self.cover_image is None and cover_id in manifest:
            self.cover_image = manifest[cover_id][0]
        self.spine = []
        for ref in _all(opf, "itemref"):
            item = manifest.get(ref.get("idref") or "")
            if item is None or (ref.get("linear") or "").lower() == "no":
                continue
            if "html" not in item[1] and not item[0].lower().endswith((".html", ".htm", ".xhtml")):
                continue
            if item[0] in self.names:
                self.spine.append(item[0])
        if not self.spine:
            raise BookImportError("This EPUB is damaged or DRM-protected.")
        self.nav_path = next((p for p, _, props in manifest.values() if "nav" in props), None)
        spine_el = next(iter(_all(opf, "spine")), None)
        ncx_id = spine_el.get("toc") if spine_el is not None else None
        self.ncx_path = manifest[ncx_id][0] if ncx_id in manifest else next(
            (p for p, t, _ in manifest.values() if t == "application/x-dtbncx+xml"), None
        )
        self.references, self.body_anchor = self._landmarks(opf, self.nav_path)
        self.fonts = [p for p, t, _ in manifest.values() if "font" in t or p.lower().endswith(_FONT_EXT)]
        pages = sum(1 for p in self.spine if _IA_PAGE_RE.search(p))
        self.scan = pages >= max(5, len(self.spine) // 2)  # Internet Archive: one file per scanned page
        return title, authors, uid

    def _walk_spine(self) -> tuple[list[_Draft], list[_Doc]]:
        drafts: list[_Draft] = []
        docs: list[_Doc] = []
        carry: list[str] = []  # anchors of a file with no blocks go to the next block
        for i, zip_path in enumerate(self.spine):
            if self.cancelled():
                raise ImportCancelled()
            self.progress(i, len(self.spine), "Reading the book…")
            doc = self._load_doc(i, zip_path)
            if doc is None:
                continue
            docs.append(doc)
            walker = _Walker(self, doc, _file_name_role(zip_path), carry)
            drafts += walker.run()
            carry = walker.pending_ids
        if carry and drafts:
            drafts[-1].anchors += carry
        self.progress(len(self.spine), len(self.spine), "Reading the book…")
        return drafts, docs

    def _declare(self, drafts: list[_Draft], targets) -> None:
        """Front matter the <guide> / landmarks and the book's own TOC labels name ("Copyright
        Page", "Title Page"): declared from the place they point at to the next TOC entry or the
        file's end (a single-file EPUB must not become one big copyright page)."""
        index = _anchor_index(drafts)
        named = [(_resolve_anchor(index, key), role) for key, role in self.references]
        named += [(t, matter.label_role(label)) for label, t, _ in targets if t is not None]
        stops = sorted({t for _, t, _ in targets if t is not None} | {t for t, _ in named if t is not None})
        for t, role in named:
            if t is None or role is None or role == matter.READ:
                continue
            drafts[t].front_label = True
            end = next((s for s in stops if s > t), len(drafts))
            for k in range(t, end):
                if drafts[k].file != drafts[t].file:
                    break
                if drafts[k].declared in ("", matter.FRONT) or drafts[k].by_name:
                    drafts[k].declared = role

    def _roles(self, blocks, drafts, title, author, anchor_index, targets) -> None:
        declared = [d.declared for d in drafts]
        for i, d in enumerate(drafts):
            if d.kind == "image" and d.src and d.src == self.cover_image and not declared[i]:
                declared[i] = "cover"
        first_text = next((i for i, b in enumerate(blocks) if b.text.strip()), 0)
        body_start = _resolve_anchor(anchor_index, self.body_anchor) if self.body_anchor else None
        if body_start is not None and body_start <= first_text:
            body_start = None  # a "Start" pointing at the first page says nothing
        if body_start is None:
            front = ("cover", "contents", "copyright", "frontmatter", "boilerplate", "blurb", matter.TITLEPAGE, matter.FRONT)
            firsts = [t for _, t, _ in targets if t is not None and not drafts[t].front_label and declared[t] not in front]
            body_start = matter.guess_body_start(blocks, declared, firsts, title)
        units = [i for i, d in enumerate(drafts) if i == 0 or d.file != drafts[i - 1].file]
        matter.assign_roles(blocks, title, author=author, declared=declared, body_start=body_start, units=units)
        matter.rank_levels(blocks)

    def _contents(self, blocks, drafts, anchor_index, targets) -> list[TocEntry]:
        """Links of the book's own contents page, and Book.toc: the nav/NCX (with numbered
        chapter headings added when it lists only the parts), else the contents page's
        links, else the headings (and a scan's sections)."""
        links = {}
        for i, d in enumerate(drafts):
            if blocks[i].role == "contents" and d.hrefs:
                target = next((t for t in (_resolve_anchor(anchor_index, h) for h in d.hrefs) if t is not None), None)
                if target is not None:
                    links[i] = target
        matter.link_contents(blocks, links)
        toc = _toc_entries(targets, blocks)
        if toc:
            top = min(e.level for e in toc)  # entries under a dropped "Maurice" entry move up
            for e in toc:
                e.level -= top - 1
            # A nav that lists only the parts: their chapters, numbered "1", "2"… with no
            # names (the Maurice of calibre), come from the headings.
            covered = {e.block for e in toc}
            extra = [
                e for e in matter.toc_from_headings(blocks)
                if e.block not in covered and matter.is_number(blocks[e.block].text)
            ]
            if len(extra) >= 3:
                toc = sorted(toc + extra, key=lambda e: e.block)
        if not toc:
            toc = _dedupe_toc([
                TocEntry(matter.display_title(blocks[b.link].text), b.link, 1)
                for b in blocks if b.link >= 0 and blocks[b.link].kind == "heading"
            ])
        if not toc:
            sections = [TocEntry(matter.display_title(d.section), i, 1) for i, d in enumerate(drafts) if d.section and not blocks[i].role]
            toc = sorted(matter.toc_from_headings(blocks) + sections, key=lambda e: e.block)
        return toc

    def _check_drm(self) -> None:
        if "META-INF/rights.xml" in self.names or "META-INF/sinf.xml" in self.names:
            raise BookImportError("This EPUB is protected by DRM, so LivroSound can't read it.")
        if "META-INF/encryption.xml" in self.names:
            enc = _parse_xml(self.zf.read("META-INF/encryption.xml"))
            if enc is None:
                return
            for data in _all(enc, "EncryptedData"):
                method = next(iter(_all(data, "EncryptionMethod")), None)
                algo = method.get("Algorithm", "") if method is not None else ""
                ref = next(iter(_all(data, "CipherReference")), None)
                uri = unquote(ref.get("URI", "")) if ref is not None else ""
                if algo in (_IDPF_FONT, _ADOBE_FONT) or uri.lower().endswith(_FONT_EXT):
                    continue  # font obfuscation, not DRM
                raise BookImportError("This EPUB is protected by DRM, so LivroSound can't read it.")

    def _opf_path(self) -> str:
        if "META-INF/container.xml" in self.names:
            container = _parse_xml(self.zf.read("META-INF/container.xml"))
            if container is not None:
                for rf in _all(container, "rootfile"):
                    p = unquote(rf.get("full-path") or "")
                    if p in self.names:
                        return p
        opfs = sorted(n for n in self.names if n.lower().endswith(".opf"))
        if not opfs:
            raise BookImportError("This EPUB is damaged or DRM-protected.")
        return opfs[0]

    def _join(self, base_dir: str, href: str) -> str:
        return posixpath.normpath(posixpath.join(base_dir, unquote(urlsplit(href).path))).lstrip("/")

    def _key(self, zip_path: str) -> str:
        """A spine file's name as its manifest href (relative to the OPF folder)."""
        return posixpath.relpath(zip_path, self.opf_dir) if self.opf_dir else zip_path

    def _landmarks(self, opf, nav_path: str | None) -> tuple[list[tuple[str, str]], str | None]:
        """Front matter the EPUB2 <guide> / EPUB3 landmarks point at, as (anchor key, role),
        and the anchor key of where the body starts ("text"/"bodymatter")."""
        refs: list[tuple[str, str]] = []
        body = None
        for ref in _all(opf, "reference"):
            kind = (ref.get("type") or "").lower()
            href = ref.get("href") or ""
            if kind in ("text", "bodymatter", "start"):
                body = body or self._anchor_key(self.opf_dir, href)
            elif kind in _GUIDE_ROLES and href:
                refs.append((self._anchor_key(self.opf_dir, href), _GUIDE_ROLES[kind]))
        nav = _parse_doc(self.zf.read(nav_path)) if nav_path and nav_path in self.names else None
        if nav is not None:
            base = posixpath.dirname(nav_path)
            for el in nav.iter():
                if _local(el) != "nav" or "landmarks" not in _types(el):
                    continue
                for a in el.iter():
                    if _local(a) != "a" or not a.get("href"):
                        continue
                    for t in _types(a):
                        if t in ("bodymatter", "text", "start"):
                            body = self._anchor_key(base, a.get("href"))
                        elif _TYPE_ROLES.get(t) not in (None, matter.READ, matter.FRONT):
                            refs.append((self._anchor_key(base, a.get("href")), _TYPE_ROLES[t]))
        return refs, body or None

    def _load_doc(self, index: int, zip_path: str) -> _Doc | None:
        root = _parse_doc(self.zf.read(zip_path))
        if root is None:
            return None
        body = next((el for el in root.iter() if _local(el) == "body"), None)
        if body is None:
            return None
        base = posixpath.dirname(zip_path)
        for el in root.iter():
            name = _local(el)
            if name == "link" and "stylesheet" in (el.get("rel") or "").lower() and el.get("href"):
                self._add_css(self._join(base, el.get("href")))
            elif name == "style" and el.text:
                self._add_css_text(el.text)
        accuracy = None
        if self.scan:
            for el in list(body.iter()):
                if _local(el) in ("b", "p", "div") and el is not body:
                    m = _IA_ACCURACY_RE.search("".join(el.itertext()))
                    if m and len("".join(el.itertext())) < 120:
                        accuracy = float(m.group(1))
                        _remove_keep_tail(el)
        return _Doc(index, self._key(zip_path), zip_path, body, accuracy)

    # ---------- CSS ----------

    def _add_css(self, zip_path: str) -> None:
        if zip_path in self.css_seen or zip_path not in self.names:
            return
        self.css_seen.add(zip_path)
        self._add_css_text(self.zf.read(zip_path).decode("utf-8", errors="replace"))

    def _add_css_text(self, text: str) -> None:
        css = _sanitize_css(text)
        if css in self.css_seen:  # calibre repeats one <style> in every file
            return
        self.css_seen.add(css)
        self.css_text.append(css)
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            effect = _effect(_declarations(body))
            if effect == (frozenset(), "", False):
                continue
            for sel in selectors.split(","):
                m = re.fullmatch(r"([A-Za-z0-9]*)\.([\w-]+)", sel.strip())
                if m:
                    key = f"{m.group(1).lower()}.{m.group(2)}"
                    self.class_styles[key] = _combine(self.class_styles.get(key), effect)
        self._style_cache.clear()

    def styles_of(self, el, tag: str) -> tuple[frozenset, str, bool]:
        """(inline styles, text-align, hidden) from the element's classes and style attribute."""
        cls = el.get("class") or ""
        inline = el.get("style") or ""
        cache_key = (tag, cls, inline)
        hit = self._style_cache.get(cache_key)
        if hit is not None:
            return hit
        effect = (frozenset(), "", False)
        for c in cls.split():
            for k in (f".{c}", f"{tag}.{c}"):
                if k in self.class_styles:
                    effect = _combine(effect, self.class_styles[k])
        if inline:
            effect = _combine(effect, _effect(_declarations(inline)))
        if _SMALLCAPS_CLASS_RE.search(cls):
            effect = (effect[0] | {"sc"}, effect[1], effect[2])
        self._style_cache[cache_key] = effect
        return effect

    # ---------- TOC ----------

    def _toc(self, nav_path: str | None, ncx_path: str | None) -> list[tuple[str, str, int]]:
        """(label, "file#frag" key, depth) in reading order: the EPUB3 nav, else the NCX."""
        entries: list[tuple[str, str, int]] = []
        if nav_path and nav_path in self.names:
            nav = _parse_doc(self.zf.read(nav_path))
            if nav is not None:
                base = posixpath.dirname(nav_path)
                toc_nav = next((el for el in nav.iter() if _local(el) == "nav" and "toc" in _types(el)), None)
                if toc_nav is None:
                    toc_nav = next((el for el in nav.iter() if _local(el) == "nav" and (el.get("id") or "") == "toc"), None)
                if toc_nav is not None:
                    ol = next((c for c in toc_nav if _local(c) in ("ol", "ul")), None)
                    if ol is not None:
                        self._nav_list(ol, base, 1, entries)
        if not entries and ncx_path and ncx_path in self.names:
            ncx = _parse_xml(self.zf.read(ncx_path))
            if ncx is not None:
                base = posixpath.dirname(ncx_path)
                nav_map = next(iter(_all(ncx, "navMap")), None)
                if nav_map is not None:
                    self._ncx_points(nav_map, base, 1, entries)
        return entries

    def _nav_list(self, ol, base: str, depth: int, out: list) -> None:
        for li in ol:
            if _local(li) != "li":
                continue
            link = next((c for c in li if _local(c) in ("a", "span")), None)
            if link is not None:
                label = " ".join("".join(link.itertext()).split())
                href = link.get("href") if _local(link) == "a" else None
                if href:
                    out.append((label, self._anchor_key(base, href), depth))
            for c in li:
                if _local(c) in ("ol", "ul"):
                    self._nav_list(c, base, depth + 1, out)

    def _ncx_points(self, parent, base: str, depth: int, out: list) -> None:
        for point in parent:
            if _local(point) != "navpoint":
                continue
            label_el = next(iter(_all(point, "text")), None)
            content = next((c for c in point if _local(c) == "content"), None)
            label = " ".join("".join(label_el.itertext()).split()) if label_el is not None else ""
            if content is not None and content.get("src"):
                out.append((label, self._anchor_key(base, content.get("src")), depth))
            self._ncx_points(point, base, depth + 1, out)

    def _anchor_key(self, base: str, href: str) -> str:
        """An href inside the book as an anchor key ("text/chapter-3.xhtml#sec2"); "" for outside links."""
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            return ""
        target = self._join(base, href) if parts.path else ""
        if parts.path and not target:
            return ""
        return (self._key(target) if target else "") + ("#" + unquote(parts.fragment) if parts.fragment else "")

    # ---------- Internet Archive scans ----------

    def _scan_pages(self, drafts: list[_Draft], docs: list[_Doc]) -> list[int]:
        """Mark stray-mark pages (OCR under 50% accurate) as boilerplate, turn a page's lone
        short line in capitals into a heading ("THE CARRIAGE"), and return the first block
        of each page."""
        accuracy = {d.index: d.accuracy for d in docs}
        starts, seen_files = [], set()
        lines: dict[int, list[int]] = defaultdict(list)
        for i, d in enumerate(drafts):
            acc = accuracy.get(d.file)
            if d.kind != "image" and acc is not None and acc < 50:
                d.declared = d.declared or "boilerplate"
            if d.file not in seen_files:
                seen_files.add(d.file)
                starts.append(i)
            if d.kind != "image":
                lines[d.file].append(i)
        for idx in lines.values():
            d = drafts[idx[0]]
            words = d.mapped.text.split()
            letters = [c for c in d.mapped.text if c.isalpha()]
            if (
                len(idx) == 1 and d.kind == "paragraph" and not d.declared and 1 <= len(words) <= 6 and letters
                and sum(c.isupper() for c in letters) >= 0.8 * len(letters) and not d.mapped.text.rstrip().endswith(".")
            ):
                d.kind, d.level = "heading", 1
        return starts

    def _cut_heads(self, drafts, title, chapter_titles, page_starts, deletions) -> list[_Draft]:
        """Cut running heads and page numbers out of the drafts (and, through deletions, the HTML)."""
        listed = matter.contents_lines([d.kind for d in drafts], [d.mapped.text for d in drafts])
        texts = [
            d.mapped.text if d.declared not in ("contents", "index", "boilerplate") and i not in listed else ""
            for i, d in enumerate(drafts)
        ]
        cuts = find_running_heads(texts, [d.kind for d in drafts], title, chapter_titles, page_starts)
        self.head_cuts = cuts
        by_block = defaultdict(list)
        for cut in cuts:
            by_block[cut.block].append(cut)
        # scans: pages from the first one with a cut head or page number on are the book's own
        first_head = min((c.block for c in cuts if page_starts), default=len(drafts))
        out: list[_Draft] = []
        carry: list[str] = []  # anchors of a draft that was all head
        for i, d in enumerate(drafts):
            d.joinable = page_starts is not None and i >= first_head
            d.anchors = carry + d.anchors
            carry = []
            block_cuts = by_block.get(i, [])
            plain = [c for c in block_cuts if not c.heading]
            heading_cut = next((c for c in block_cuts if c.heading), None)
            for src, a, b in d.mapped.delete(widen_cuts(d.mapped.text, [(c.start, c.end) for c in plain])):
                deletions[src].append((a, b))
            d.glued = any(c.glued for c in plain)
            d.section = next((c.section for c in plain if c.section), "")
            if heading_cut is not None:
                end = heading_cut.end - sum(c.end - c.start for c in plain if c.end <= heading_cut.start)
                head = d.mapped.slice(0, end)
                head.strip()
                d.mapped = d.mapped.slice(end, len(d.mapped.text))
                out.append(_Draft("heading", head, d.file, level=1, anchors=d.anchors, declared=d.declared))
                d.anchors = []
            d.mapped.strip()  # whitespace stays in the HTML: it may separate the words around an anchor
            if d.mapped.text.strip() or d.kind in ("image", "separator"):
                out.append(d)
            else:
                carry = d.anchors
        if carry and out:
            out[-1].anchors += carry
        return out

    def _join_pages(self, drafts: list[_Draft], deletions) -> list[_Draft]:
        """Join paragraphs split by a page break ("…in some gov" / "ernment office…")."""
        out: list[_Draft] = []
        for d in drafts:
            prev = out[-1] if out else None
            if (
                prev is not None and prev.joinable and d.joinable and prev.kind == d.kind == "paragraph"
                and prev.file != d.file and not prev.declared and not d.declared
            ):
                left, right = prev.mapped.text, d.mapped.text
                sep = page_break_join(left, right, prev.glued)
                if sep is not None and left.endswith("-") and not prev.glued:
                    mode = join_mode(left, right)
                    if mode == "drop-hyphen":
                        for src, a, b in prev.mapped.delete([(len(left) - 1, len(left))]):
                            deletions[src].append((a, b))
                    sep = "" if mode in ("drop-hyphen", "join") else " "
                if sep is not None:
                    prev.mapped = prev.mapped.join(d.mapped, sep)
                    prev.anchors += d.anchors
                    prev.file, prev.glued = d.file, d.glued
                    continue
            out.append(d)
        return out

    # ---------- resources ----------

    def _copy_images(self, drafts: list[_Draft], res: Path) -> dict[str, str]:
        names: dict[str, str] = {}
        used: set[str] = set()
        for d in drafts:
            if d.kind != "image" or not d.src or d.src in names or d.src not in self.names:
                continue
            name = _safe_name(posixpath.basename(d.src), used)
            (res / name).write_bytes(self.zf.read(d.src))
            names[d.src] = name
        self._used_names = used
        return names

    def _copy_fonts(self, res: Path, uid: str) -> list[str]:
        out = []
        obfuscated = self._obfuscated()
        for zip_path in self.fonts:
            if zip_path not in self.names:
                continue
            data = self.zf.read(zip_path)
            algo = obfuscated.get(zip_path)
            if algo:
                data = _deobfuscate(data, algo, uid)
            name = _safe_name(posixpath.basename(zip_path), self._used_names)
            (res / name).write_bytes(data)
            out.append(name)
        return out

    def _obfuscated(self) -> dict[str, str]:
        if "META-INF/encryption.xml" not in self.names:
            return {}
        enc = _parse_xml(self.zf.read("META-INF/encryption.xml"))
        out = {}
        if enc is None:
            return out
        for data in _all(enc, "EncryptedData"):
            method = next(iter(_all(data, "EncryptionMethod")), None)
            ref = next(iter(_all(data, "CipherReference")), None)
            if method is not None and ref is not None:
                out[posixpath.normpath(unquote(ref.get("URI", "")))] = method.get("Algorithm", "")
        return out

    # ---------- original.html ----------

    def _original_html(self, docs, drafts, deletions, anchor_index, images, res: Path) -> str:
        anchors: dict[int, list[tuple[int, str]]] = defaultdict(list)
        element_anchors: list[tuple[object, str]] = []
        for i, d in enumerate(drafts):
            name = f"{ANCHOR_PREFIX}{i}"
            start = d.mapped.source_start()
            if start is not None:
                anchors[start[0]].append((start[1], name))
            elif d.element is not None:
                element_anchors.append((d.element, name))
        for src in set(deletions) | set(anchors):
            el, attr = self.nodes[src]
            text = (el.text if attr == "text" else el.tail) or ""
            pieces = edit_source(text, deletions.get(src, []), anchors.get(src, []))
            _apply_pieces(el, attr, pieces)
        for el, name in element_anchors:
            if el.getparent() is None:
                continue
            # Qt keeps a named anchor only in front of a character (not an image, not a rule):
            # give it a no-break space, in a 1px paragraph before a rule.
            a = etree.Element("a", name=name)
            a.tail = "\u00a0"
            if _local(el) == "hr":
                holder = etree.Element("p", style="margin:0;font-size:1px")
                holder.append(a)
                el.addprevious(holder)
            else:
                el.addprevious(a)
        out = []
        ctx = _SerializeCtx(self, anchor_index, images, res)
        for doc in docs:
            classes = doc.body.get("class") or ""
            out.append(f'<div class="{html.escape(classes)}">' if classes else "<div>")
            ctx.doc = doc
            ctx.children(doc.body, out)
            out.append("</div>\n")
        css = "\n".join(self.css_text)
        return (
            '<!DOCTYPE html>\n<html><head><meta charset="utf-8"/>'
            f"<style>\n{css}\n</style></head><body>\n" + "".join(out) + "</body></html>\n"
        )


class _Walker:
    """Walks one spine document's body into drafts."""

    def __init__(self, reader: _EpubReader, doc: _Doc, file_role: str, carried: list[str]) -> None:
        self.reader = reader
        self.doc = doc
        self.base = posixpath.dirname(doc.zip_path)
        self.drafts: list[_Draft] = []
        self.builder = TextBuilder()
        self.hrefs: list[str] = []
        self.pending_ids: list[str] = [*carried, doc.key]
        self.stack = [_Ctx("root", declared=file_role)]
        self.bold_chars = 0

    def run(self) -> list[_Draft]:
        """The file's drafts; ids after its last block (pending_ids) are left for the caller."""
        self._block(self.doc.body, "body", frozenset())
        if self.pending_ids and self.drafts:
            self.drafts[-1].anchors += self.pending_ids
            self.pending_ids = []
        return self.drafts

    # ---------- elements ----------

    def _walk(self, el, styles: frozenset) -> None:
        if not isinstance(el.tag, str):
            return
        tag = _local(el)
        if tag in _SKIP_TAGS or el.get("hidden") is not None:
            return
        if tag == "svg":
            self._note_ids(el)
            image = next((c for c in el.iter() if _local(c) == "image"), None)
            if image is not None:
                self._image(el, image.get(XLINK_HREF) or image.get("href") or "")
            return
        cls_styles, align, hidden = self.reader.styles_of(el, tag)
        if hidden or "display:none" in (el.get("style") or "").replace(" ", ""):
            return
        self._note_ids(el)
        if tag == "br":
            self.builder.line_break()
            return
        if tag == "img":
            self._image(el, el.get("src") or "")
            return
        if tag == "hr":
            self._flush()
            self._separator(el)
            return
        if tag in _BLOCK_TAGS:
            self._block(el, tag, styles)
            return
        st = styles | cls_styles | self._tag_styles(el, tag)
        if tag == "a" and el.get("href"):
            key = self.reader._anchor_key(self.base, el.get("href"))
            if key:
                self.hrefs.append(key)
        self._content(el, st)

    def _block(self, el, tag: str, styles: frozenset) -> None:
        self._flush()
        parent = self.stack[-1]
        cls_styles, align, _ = self.reader.styles_of(el, tag)
        ctx = _Ctx(tag, parent.kind, parent.level, align or el.get("align", "").lower() or parent.align,
                   parent.declared, styles | cls_styles, parent.pre, parent.poem, parent.typed)
        if tag == "center":
            ctx.align = "center"
        types = _types(el)
        for t in types:
            role = _TYPE_ROLES.get(t.split(":")[-1]) or _TYPE_ROLES.get(t)
            if role:
                ctx.declared, ctx.typed = role, True
        aria = (el.get("role") or "").removeprefix("doc-")
        if aria in _TYPE_ROLES:
            ctx.declared, ctx.typed = _TYPE_ROLES[aria], True
        cls = el.get("class") or ""
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            ctx.kind, ctx.level = "heading", int(tag[1])
        elif (
            any(t.split(":")[-1] in ("footnote", "endnote", "rearnote", "note", "footnotes", "endnotes", "rearnotes") for t in types)
            or aria in ("footnote", "endnote", "endnotes")
            or (_NOTE_CLASS_RE.search(cls) and "noteref" not in cls.lower())
        ):
            if ctx.kind != "heading":
                ctx.kind = "note"
        elif tag == "blockquote" and ctx.kind == "paragraph":
            ctx.kind = "quote"
        if tag == "pre":
            ctx.pre, ctx.kind = True, "verse" if ctx.kind != "heading" else ctx.kind
        if _POEM_RE.search(cls) or any(_POEM_RE.search(t) for t in types):
            ctx.poem = True
        self.stack.append(ctx)
        self._content(el, ctx.styles)
        self._flush()
        self.stack.pop()

    def _content(self, el, styles: frozenset) -> None:
        self._text(el, "text", styles)
        for child in el:
            self._walk(child, styles)
            if child.tail:
                self._text(child, "tail", styles)

    def _text(self, el, attr: str, styles: frozenset) -> None:
        s = el.text if attr == "text" else el.tail
        if not s:
            return
        src = len(self.reader.nodes)
        self.reader.nodes.append((el, attr))
        if self.stack[-1].pre and "\n" in s:
            pos = 0
            for k, line in enumerate(s.split("\n")):
                if k:
                    self.builder.line_break()
                self.builder.add(line, src, pos, styles)
                pos += len(line) + 1
        else:
            self.builder.add(s, src, 0, styles)
        if "b" in styles:
            self.bold_chars += len(s.strip())

    def _tag_styles(self, el, tag: str) -> frozenset:
        if tag in _ITALIC_TAGS:
            return frozenset(("i",))
        if tag in _BOLD_TAGS:
            return frozenset(("b",))
        types = _types(el)
        if tag == "sup" or "noteref" in types or (el.get("role") or "") == "doc-noteref" or "noteref" in (el.get("class") or "").lower():
            text = "".join(el.itertext())
            if tag != "sup" or _NOTE_MARK_RE.match(text):
                return frozenset(("sup",))
        return frozenset()

    def _note_ids(self, el) -> None:
        ident = el.get("id") or (el.get("name") if _local(el) == "a" else None)
        if ident:
            self.pending_ids.append(f"{self.doc.key}#{ident}")

    # ---------- blocks ----------

    def _flush(self) -> None:
        builder, self.builder = self.builder, TextBuilder()
        hrefs, self.hrefs = self.hrefs, []
        bold, self.bold_chars = self.bold_chars, 0
        if builder.empty:
            return
        ctx = self.stack[-1]
        kind = ctx.kind
        verse = kind == "verse" or (kind in ("paragraph", "quote") and (builder.breaks >= 2 or ctx.poem))
        mapped = builder.build(verse=verse and kind != "heading")
        if not mapped.text.strip():
            return
        if verse and kind in ("paragraph", "quote"):
            kind = "verse"
        if kind in ("paragraph", "quote") and _SEPARATOR_RE.match(mapped.text):
            kind = "separator"
        draft = _Draft(kind, mapped, self.doc.index, level=ctx.level, align=ctx.align, declared=ctx.declared,
                       hrefs=hrefs, whole_bold=bold >= len(mapped.text.replace(" ", "")) > 0)
        self._add(draft)

    def _image(self, el, href: str) -> None:
        self._flush()
        if not href or href.startswith("data:"):
            return
        target = self.reader._join(self.base, href)
        ctx = self.stack[-1]
        self._add(_Draft("image", MappedText(""), self.doc.index, align=ctx.align, declared=ctx.declared, src=target, element=el))

    def _separator(self, el) -> None:
        ctx = self.stack[-1]
        self._add(_Draft("separator", MappedText(""), self.doc.index, declared=ctx.declared, element=el))

    def _add(self, draft: _Draft) -> None:
        draft.by_name = bool(draft.declared) and not self.stack[-1].typed
        draft.anchors = self.pending_ids
        self.pending_ids = []
        self.drafts.append(draft)


class _SerializeCtx:
    """Writes a (modified) spine body as Qt-friendly HTML."""

    def __init__(self, reader: _EpubReader, anchor_index: dict[str, int], images: dict[str, str], res: Path) -> None:
        self.reader = reader
        self.anchor_index = anchor_index
        self.images = images
        self.res = res
        self.doc: _Doc | None = None

    def children(self, el, out: list[str]) -> None:
        if el.text:
            out.append(_html_text(el.text))
        for child in el:
            self.element(child, out)
            if child.tail:
                out.append(_html_text(child.tail))

    def element(self, el, out: list[str]) -> None:
        if not isinstance(el.tag, str):
            return
        tag = _local(el)
        if tag in _SKIP_TAGS:
            return
        if tag == "svg":
            image = next((c for c in el.iter() if _local(c) == "image"), None)
            if image is not None:
                self._img(image.get(XLINK_HREF) or image.get("href") or "", {}, out)
            return
        if tag == "img":
            self._img(el.get("src") or "", el.attrib, out)
            return
        if tag == "a" and el.get("name", "").startswith(ANCHOR_PREFIX):
            out.append(f'<a name="{el.get("name")}">{_html_text(el.text or "")}</a>')
            return
        name = "div" if tag in _DIV_TAGS else tag
        attrs = []
        for k, v in el.attrib.items():
            if k in _KEEP_ATTRS and (k != "type" or name in ("ol", "ul", "li")):
                attrs.append(f' {k}="{html.escape(v)}"')
        if tag == "a" and el.get("href"):
            href = el.get("href")
            if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I):
                attrs.append(f' href="{html.escape(href)}"')
            else:
                key = self.reader._anchor_key(posixpath.dirname(self.doc.zip_path), href)
                target = _resolve_anchor(self.anchor_index, key)
                if target is not None:
                    attrs.append(f' href="#{ANCHOR_PREFIX}{target}"')
        if name in _VOID:
            out.append(f"<{name}{''.join(attrs)}/>")
            return
        out.append(f"<{name}{''.join(attrs)}>")
        self.children(el, out)
        out.append(f"</{name}>")

    def _img(self, href: str, attrib, out: list[str]) -> None:
        if not href:
            return
        target = self.reader._join(posixpath.dirname(self.doc.zip_path), href)
        name = self.images.get(target)
        if not name:
            return
        path = self.res / name
        attrs = [f' src="{html.escape(path.as_uri())}"']
        alt = attrib.get("alt") if attrib else None
        if alt:
            attrs.append(f' alt="{html.escape(alt)}"')
        width = _image_width(path)
        if width:
            attrs.append(f' width="{min(width, 600)}"')
        out.append(f"<img{''.join(attrs)}/>")


# ---------------------------------------------------------------------------
# Helpers


_INVISIBLE = str.maketrans({c: None for c in "\u00ad\u200b\u200c\u200d\u2060\ufeff"})


def _html_text(text: str) -> str:
    """Text for original.html: escaped, without the invisible marks Block.text drops either."""
    return html.escape(text.translate(_INVISIBLE), quote=False)


def _apply_pieces(el, attr: str, pieces: list) -> None:
    """Rewrite one text node from edit_source pieces, inserting the anchors as elements."""
    lead, anchors = "", []  # anchors: [element, text after it]
    for piece in pieces:
        if isinstance(piece, tuple):
            a = etree.Element("a", name=piece[0])
            a.text = piece[1] or None
            anchors.append([a, ""])
        elif anchors:
            anchors[-1][1] += piece
        else:
            lead += piece
    for a, tail in anchors:
        a.tail = tail or None
    if attr == "text":
        el.text = lead or None
        for k, (a, _) in enumerate(anchors):
            el.insert(k, a)
    else:
        el.tail = lead or None
        parent = el.getparent()
        if parent is None:
            return
        at = parent.index(el) + 1
        for k, (a, _) in enumerate(anchors):
            parent.insert(at + k, a)


def _split_pages(drafts: list[_Draft]) -> list[_Draft]:
    """A scan's pages, joined back into one run of text, cut into paragraph-sized blocks at
    sentence ends (the source has one <p> per page and no other cue), so ⏮/⏭ step a
    paragraph, not a page. The pieces share the page's <p> in original.html, each with its
    own anchor."""
    out: list[_Draft] = []
    for d in drafts:
        if d.kind != "paragraph" or d.declared or len(d.mapped.text) <= PARAGRAPH_MAX:
            out.append(d)
            continue
        whole = d.mapped
        for k, (a, b) in enumerate(paragraph_spans(whole.text)):
            piece = whole.slice(a, b)
            piece.strip()
            if not piece.text:
                continue
            if k == 0:
                d.mapped = piece
                out.append(d)
            else:
                out.append(_Draft("paragraph", piece, d.file, align=d.align, declared=d.declared))
    return out


def _anchor_index(drafts: list[_Draft]) -> dict[str, int]:
    index: dict[str, int] = {}
    for i, d in enumerate(drafts):
        for a in d.anchors:
            index.setdefault(a, i)
    return index


def _resolve_anchor(index: dict[str, int], key: str) -> int | None:
    if key in index:
        return index[key]
    file = key.split("#", 1)[0]
    return index.get(file)


def _resolve_toc(toc_raw: list[tuple[str, str, int]], drafts: list[_Draft]) -> list[tuple[str, int | None, int]]:
    """TOC entries (label, anchor key, depth) → (label, draft index or None, depth)."""
    index = _anchor_index(drafts)
    return [(label, _resolve_anchor(index, key), depth) for label, key, depth in toc_raw]


def _promote_headings(drafts: list[_Draft], toc_targets) -> None:
    """Short paragraphs a TOC entry points at, or a "CHAPTER IV" opening its file, are headings."""
    for label, target, depth in toc_targets:
        if target is None:
            continue
        d = drafts[target]
        if d.kind != "paragraph" or len(d.mapped.text.split()) > 12:
            continue
        key, lkey = letters_key(d.mapped.text), letters_key(label)
        if key and lkey and (key == lkey or key in lkey or lkey in key):
            d.kind, d.level = "heading", depth
    seen = set()
    for d in drafts:
        if d.file in seen or d.kind == "image":
            continue
        seen.add(d.file)
        text = d.mapped.text.strip()
        if d.kind == "paragraph" and len(text.split()) <= 10 and (
            HEADING_RE.match(text) or ((d.align == "center" or d.whole_bold) and not text.endswith(TERMINAL[:-1]))
        ) and not _SEPARATOR_RE.match(text) and not matter.is_contents_line(text):
            d.kind, d.level = "heading", 1 if HEADING_RE.match(text) else 2


def _toc_entries(targets, blocks: list[Block]) -> list[TocEntry]:
    entries = []
    for order, (label, target, depth) in enumerate(targets):
        if target is None or blocks[target].role:
            continue
        title = matter.display_title(label or blocks[target].text)
        if not title:
            continue
        entries.append((target, order, TocEntry(title, target, depth)))
    entries.sort(key=lambda e: (e[0], e[1]))
    return _dedupe_toc([e for _, _, e in entries])


def _dedupe_toc(entries: list[TocEntry]) -> list[TocEntry]:
    out, seen = [], set()
    for e in entries:
        k = (e.block, e.title)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _metadata(opf) -> tuple[str, list[str], str, str | None]:
    meta = next(iter(_all(opf, "metadata")), opf)
    title = ""
    for el in meta.iter():
        if _local(el) == "title" and "".join(el.itertext()).strip():
            title = " ".join("".join(el.itertext()).split())
            break
    refines = defaultdict(dict)
    for el in meta.iter():
        if _local(el) == "meta" and el.get("refines") and el.get("property"):
            refines[el.get("refines").lstrip("#")][el.get("property")] = (el.text or "").strip()
    authors, others = [], []
    for el in meta.iter():
        if _local(el) != "creator":
            continue
        name = " ".join("".join(el.itertext()).split())
        if not name:
            continue
        role = el.get(f"{{{_OPF_NS}}}role") or el.get("role") or refines.get(el.get("id", ""), {}).get("role", "")
        (authors if role in ("", "aut") else others).extend(re.split(r"\s*;\s*", name))
    uid_id = opf.get("unique-identifier")
    uid = ""
    for el in meta.iter():
        if _local(el) == "identifier" and (el.get("id") == uid_id or not uid):
            uid = (el.text or "").strip()
            if el.get("id") == uid_id:
                break
    cover_id = None
    for el in meta.iter():
        if _local(el) == "meta" and el.get("name") == "cover":
            cover_id = el.get("content")
    return title, authors or others[:1], uid, cover_id


_OPF_NS = "http://www.idpf.org/2007/opf"
_IDPF_FONT = "http://www.idpf.org/2008/embedding"
_ADOBE_FONT = "http://ns.adobe.com/pdf/enc#RC"


def _deobfuscate(data: bytes, algorithm: str, uid: str) -> bytes:
    """Undo EPUB font obfuscation (IDPF: SHA-1 of the identifier over 1040 bytes; Adobe: the UUID over 1024)."""
    if algorithm == _IDPF_FONT:
        key = hashlib.sha1("".join(uid.split()).encode("utf-8")).digest()
        n = 1040
    elif algorithm == _ADOBE_FONT:
        hexkey = re.sub(r"[^0-9a-fA-F]", "", uid.removeprefix("urn:uuid:"))
        if len(hexkey) < 32:
            return data
        key = bytes.fromhex(hexkey[:32])
        n = 1024
    else:
        return data
    head = bytes(b ^ key[i % len(key)] for i, b in enumerate(data[:n]))
    return head + data[n:]


def _types(el) -> list[str]:
    value = el.get(f"{{{OPS_NS}}}type") or el.get("epub:type") or ""
    return [t.lower() for t in value.split()]


def _local(el) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.split(":")[-1].lower()


def _all(root, name: str):
    return [el for el in root.iter() if isinstance(el.tag, str) and el.tag.rsplit("}", 1)[-1] == name]


_ENTITY = re.compile(rb"&([A-Za-z][A-Za-z0-9]{1,31});")
_XML_ENTITIES = {b"amp", b"lt", b"gt", b"quot", b"apos"}


def _fix_entities(data: bytes) -> bytes:
    """HTML named entities (&nbsp;, &mdash;) as numeric references, which XML parsers know."""

    def sub(m: re.Match) -> bytes:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        code = name2codepoint.get(name.decode("ascii"))
        return b"&#%d;" % code if code else m.group(0)

    return _ENTITY.sub(sub, data)


def _parse_xml(data: bytes):
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=True, remove_comments=True, remove_pis=True)
    try:
        return etree.fromstring(_fix_entities(data), parser)
    except (etree.XMLSyntaxError, ValueError):
        return None


def _parse_doc(data: bytes):
    """An XHTML document; tag soup falls back to the HTML parser."""
    root = _parse_xml(data)
    if root is not None and any(_local(el) == "body" for el in root.iter()):
        return root
    try:
        return lxml_html.document_fromstring(data)
    except (etree.ParserError, ValueError):
        return None


def _remove_keep_tail(el) -> None:
    parent = el.getparent()
    if parent is None:
        return
    if el.tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def _file_name_role(zip_path: str) -> str:
    stem = posixpath.splitext(posixpath.basename(zip_path))[0]
    for pattern, role in _FILE_ROLES:
        if pattern.match(stem):
            return role
    return ""


def _title_from_name(path: Path) -> str:
    return re.sub(r"[_]+", " ", path.stem).strip() or "Untitled"


def _safe_name(base: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    name, k = base, 1
    while name.lower() in used:
        stem, ext = posixpath.splitext(base)
        name = f"{stem}-{k}{ext}"
        k += 1
    used.add(name.lower())
    return name


def _image_width(path: Path) -> int | None:
    try:
        from PIL import Image  # optional: only to keep huge images inside the page
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            return im.width
    except Exception:
        return None


_ROOT_SELECTOR = re.compile(r"^(?:html|body|:root|\*)(?:[.#:][\w-]+)*$", re.I)
_AT_BLOCK = re.compile(r"@[\w-]+[^{;]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", re.S)
_AT_STATEMENT = re.compile(r"@[\w-]+[^{;]*;")


def _sanitize_css(css: str) -> str:
    """The book's CSS minus what Qt's rich text can't use: comments, @-rules, namespaced selectors."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = _AT_BLOCK.sub("", css)
    css = _AT_STATEMENT.sub("", css)
    rules = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        # body/html rules of one document (a cover page's "body {text-align: center}") would
        # style the whole book once the bodies are joined: each body's classes live on its div.
        kept = [
            s.strip() for s in selectors.split(",")
            if s.strip() and "|" not in s and "[" not in s and not _ROOT_SELECTOR.match(s.strip())
        ]
        if kept:
            rules.append(f"{', '.join(kept)} {{{body.strip()}}}")
    return "\n".join(rules)


def _declarations(body: str) -> dict[str, str]:
    props = {}
    for decl in body.split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            props[k.strip().lower()] = v.strip().lower().replace("!important", "").strip()
    return props


def _effect(props: dict[str, str]) -> tuple[frozenset, str, bool]:
    styles = set()
    if props.get("font-style") in ("italic", "oblique"):
        styles.add("i")
    weight = props.get("font-weight", "")
    if weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600):
        styles.add("b")
    if "small-caps" in props.get("font-variant", "") or "small-caps" in props.get("font-variant-caps", ""):
        styles.add("sc")
    align = props.get("text-align", "")
    align = align if align in ("left", "center", "right", "justify") else ""
    hidden = props.get("display") == "none" or props.get("visibility") == "hidden"
    return frozenset(styles), align, hidden


def _combine(a: tuple[frozenset, str, bool] | None, b: tuple[frozenset, str, bool]) -> tuple[frozenset, str, bool]:
    if a is None:
        return b
    return a[0] | b[0], b[1] or a[1], a[2] or b[2]
