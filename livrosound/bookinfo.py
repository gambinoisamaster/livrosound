"""What the library shows about a book besides its text: subjects, series, when
it was published, how many pages, and its cover.

Why a module of its own: the library page lists every book at once, and a big
book's book.json is megabytes of blocks. So these facts are read once, straight
from the source file (the EPUB's OPF, the PDF's metadata and first page, the
DOCX's core properties), kept on the BookRecord, and the cover is saved as
book_dir/cover.jpg. Reading them never touches the parsed blocks, so it needs
no new PARSER_VERSION (which would drop every book's audio).

Rules:
- Everything here is a nicety: any failure leaves the field empty (and a
  generated cover is drawn instead); it never raises.
- Covers: the EPUB's declared cover image (or a file named like one), a PDF's
  first page; DOCX and TXT get the library's drawn cover.
- Runs off the UI thread (it opens files and renders a PDF page); saving the
  cover JPEG uses Qt's QImage, which is fine off the UI thread.
- INFO_VERSION is kept on the record: bump it when something new is read, and
  older records are read again the next time the library is shown.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import paths

INFO_VERSION = 1
COVER_NAME = "cover.jpg"
COVER_HEIGHT = 600  # px: sharp in the grid on a Retina screen
WORDS_PER_PAGE = 275  # a printed page, for books without pages of their own


@dataclass
class BookInfo:
    subjects: list[str] = field(default_factory=list)
    series: str = ""
    series_index: float = 0.0
    published: str = ""  # ISO date or year, as the file says it ("1877", "2003-05-01")
    pages: int = 0  # PDF: its pages; others: words / WORDS_PER_PAGE (0 = unknown)
    cover: bytes = b""  # image file bytes as found (any format QImage reads)


def cover_path(book_id: str) -> Path:
    return paths.book_dir(book_id) / COVER_NAME


def read_info(path: str | Path, kind: str, *, words: int = 0) -> BookInfo:
    """The book's metadata and cover image from its file (empty fields where unknown).
    words: the book's word count, for the page estimate of a reflowable book."""
    path = Path(path)
    info = BookInfo()
    try:
        if kind == "epub":
            _epub(path, info)
        elif kind == "pdf":
            _pdf(path, info)
        elif kind == "docx":
            _docx(path, info)
    except Exception:  # a nicety: a damaged file just shows less
        pass
    if not info.pages and words:
        info.pages = max(1, round(words / WORDS_PER_PAGE))
    info.subjects = _clean_subjects(info.subjects)
    return info


def save_cover(book_id: str, data: bytes) -> Path | None:
    """Save a cover image (scaled to COVER_HEIGHT) as the book's cover.jpg."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    if not data:
        return None
    image = QImage()
    if not image.loadFromData(data) or image.isNull():
        return None
    if image.height() > COVER_HEIGHT:
        image = image.scaledToHeight(COVER_HEIGHT, Qt.TransformationMode.SmoothTransformation)
    target = cover_path(book_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp.jpg")
    if not image.convertToFormat(QImage.Format.Format_RGB32).save(str(tmp), "JPG", 88):
        return None
    tmp.replace(target)
    return target


def _clean_subjects(subjects: list[str]) -> list[str]:
    """Split "Fiction; Russia -- History" lists, drop codes and duplicates, keep order."""
    out: list[str] = []
    for raw in subjects:
        for part in re.split(r"\s*(?:;|\s--\s|/|\|)\s*", raw):
            part = " ".join(part.split()).strip(" .,")
            if len(part) < 2 or re.fullmatch(r"[\d.\s]+|[A-Z]{2,4}\d+", part):
                continue
            if part.lower() not in {s.lower() for s in out}:
                out.append(part[0].upper() + part[1:])
    return out[:12]


# ---------- EPUB ----------


def _epub(path: Path, info: BookInfo) -> None:
    from lxml import etree

    if path.is_dir():
        root = path.resolve()

        def read(name: str) -> bytes:
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise KeyError(name)
            return target.read_bytes()

        names = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        _epub_from(read, names, info, etree)
        return
    with zipfile.ZipFile(path) as zf:
        _epub_from(zf.read, set(zf.namelist()), info, etree)


def _epub_from(read, names: set[str], info: BookInfo, etree) -> None:
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=True)
    opf_path = ""
    if "META-INF/container.xml" in names:
        container = etree.fromstring(read("META-INF/container.xml"), parser)
        for el in container.iter():
            if _local(el) == "rootfile" and unquote(el.get("full-path") or "") in names:
                opf_path = unquote(el.get("full-path"))
                break
    if not opf_path:
        opfs = sorted(n for n in names if n.lower().endswith(".opf"))
        if not opfs:
            return
        opf_path = opfs[0]
    opf = etree.fromstring(read(opf_path), parser)
    if opf is None:
        return
    base = posixpath.dirname(opf_path)
    cover_id = ""
    refines: dict[str, dict[str, str]] = {}
    for el in opf.iter():
        name = _local(el)
        text = " ".join("".join(el.itertext()).split()) if isinstance(el.tag, str) else ""
        if name == "subject" and text:
            info.subjects.append(text)
        elif name == "date" and text and not info.published:
            event = (el.get("{http://www.idpf.org/2007/opf}event") or el.get("event") or "").lower()
            if event in ("", "publication", "original-publication"):
                info.published = text[:10]
        elif name == "meta":
            meta_name, content = el.get("name") or "", el.get("content") or ""
            prop = el.get("property") or ""
            if meta_name == "cover":
                cover_id = content
            elif meta_name == "calibre:series" and content:
                info.series = content
            elif meta_name == "calibre:series_index" and content:
                info.series_index = _number(content)
            elif prop == "belongs-to-collection" and text:
                info.series = info.series or text
                if el.get("id"):
                    refines.setdefault(el.get("id"), {})
            elif prop in ("group-position", "collection-type") and el.get("refines"):
                refines.setdefault(el.get("refines").lstrip("#"), {})[prop] = text
    for values in refines.values():
        if values.get("group-position") and not info.series_index:
            info.series_index = _number(values["group-position"])
    cover_href = ""
    for el in opf.iter():
        if _local(el) != "item":
            continue
        props = (el.get("properties") or "").split()
        if "cover-image" in props or (cover_id and el.get("id") == cover_id):
            cover_href = el.get("href") or ""
            if "cover-image" in props:
                break
    if not cover_href:  # many EPUB 2 books just name the file "cover"
        images = sorted(n for n in names if re.search(r"(?:^|/)[^/]*cover[^/]*\.(?:jpe?g|png|gif|webp)$", n, re.I))
        if images:
            info.cover = read(images[0])
        return
    target = posixpath.normpath(posixpath.join(base, unquote(urlsplit(cover_href).path))).lstrip("/")
    if target in names:
        info.cover = read(target)


def _local(el) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _number(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return 0.0


# ---------- PDF ----------


def _pdf(path: Path, info: BookInfo) -> None:
    import pymupdf

    with pymupdf.open(path) as doc:
        info.pages = doc.page_count
        meta = doc.metadata or {}
        for key in ("keywords", "subject"):
            value = (meta.get(key) or "").strip()
            if value and len(value) < 300:
                info.subjects.extend(re.split(r"\s*[;,]\s*", value))
        date = meta.get("creationDate") or ""  # "D:20030501120000Z"
        match = re.match(r"D:(\d{4})(\d{2})?(\d{2})?", date)
        if match:
            info.published = "-".join(g for g in match.groups() if g)
        if doc.page_count:
            page = doc[0]
            zoom = COVER_HEIGHT / max(1.0, page.rect.height)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=False, alpha=False)
            info.cover = pix.tobytes("png")


# ---------- DOCX ----------


def _docx(path: Path, info: BookInfo) -> None:
    import docx

    document = docx.Document(str(path))
    props = document.core_properties
    for value in (props.keywords, props.subject, props.category):
        if value:
            info.subjects.extend(re.split(r"\s*[;,]\s*", value))
    if props.created is not None:
        info.published = props.created.date().isoformat()
    # No cover: a document's first picture is rarely one (the library draws a cover).
