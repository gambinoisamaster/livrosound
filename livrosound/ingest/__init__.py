"""Every book format → a Book (see book.py), cached per file.

load_book is the one entry point the app uses. It fingerprints the file
(book_id_for: book.file_book_id, or for an unzipped EPUB folder, which Finder
shows as a book, a fingerprint of its files), so a book opened again (even from
another folder) comes straight from its cache folder: book.json + original.html.
A cache from an older parser (Book.load returns None) is parsed again, and once
the new parse has succeeded its audio, keyed to block indices, is discarded (a
cancelled or failed re-import keeps the old book and its audio).

LivroSound reads books that are text already: PDFs with a text layer, EPUB,
DOCX and TXT. Scanned books are not supported for now (OCR proved too
imperfect): pdf.py refuses a scanned PDF with a BookImportError.

The parsers live beside this file: pdf.py (imported only for PDFs: it pulls in
PyMuPDF), epub.py, docx.py, txt.py. They share textmap.py (block text mapped
back to its source, so original.html loses what the blocks lose), heads.py
(running heads and page numbers, never part of the book) and matter.py
(front/back matter roles, the book's own contents page).

Errors reach the user as BookImportError with a plain, friendly message; a
cancelled import raises ImportCancelled.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from livrosound import paths
from livrosound.book import Book, file_book_id

from .heads import strip_running_heads

__all__ = [
    "SUPPORTED_SUFFIXES",
    "BookImportError",
    "ImportCancelled",
    "book_id_for",
    "load_book",
    "strip_running_heads",
]

SUPPORTED_SUFFIXES = (".pdf", ".epub", ".docx", ".txt")

_KIND_NAMES = {".pdf": "PDF", ".epub": "EPUB", ".docx": "Word document", ".txt": "text file"}
_AUDIO_FILES = ("audio.sqlite", "audio.sqlite-wal", "audio.sqlite-shm")


class BookImportError(Exception):
    """The file can't be turned into a book; str(error) is shown to the user."""


class ImportCancelled(Exception):
    """The user cancelled the import."""


def _noop_progress(done: int, total: int, message: str) -> None:
    pass


def _never() -> bool:
    return False


def load_book(
    path: str | Path,
    *,
    progress: Callable[[int, int, str], None] = _noop_progress,
    cancelled: Callable[[], bool] = _never,
) -> Book:
    """The Book for a PDF, EPUB, DOCX or TXT file, from the cache when it is still valid."""
    path = Path(path).expanduser()
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        shown = suffix or "this kind of"
        raise BookImportError(f"LivroSound can't open {shown} files. Choose a PDF, EPUB, Word (.docx) or text file.")
    if not path.is_file() and not _is_folder_epub(path):
        raise BookImportError("The file couldn't be found. It may have been moved or deleted.")
    try:
        book_id = book_id_for(path)
    except OSError as e:
        raise BookImportError("The file couldn't be read. Check that it's downloaded and that you can open it.") from e

    folder = paths.book_dir(book_id)
    source = str(path.resolve())
    cached = _cached(folder)
    if cached is not None:
        if cached.source_path != source:
            cached.source_path = source  # the same file opened from another folder
            cached.save(folder)
        return _without_skipped_toc(cached)

    folder.mkdir(parents=True, exist_ok=True)
    book = _parse(path, suffix, book_id, progress, cancelled)
    if not any(b.text.strip() for b in book.blocks):
        raise BookImportError("No readable text was found in this book.")
    book.book_id = book_id
    book.source_path = source
    _without_skipped_toc(book)
    # Only now that the new blocks exist: audio made for the old ones would play the wrong
    # text. A cancelled or failed re-import leaves the old book and its audio as they were.
    for name in _AUDIO_FILES:
        (folder / name).unlink(missing_ok=True)
    book.save(folder)
    return book


def book_id_for(path: str | Path) -> str:
    """A book's id, as load_book files it: book.file_book_id for a file, a fingerprint of
    its files' names and bytes for an unzipped EPUB folder. Raises OSError when unreadable."""
    path = Path(path).expanduser()
    return _folder_book_id(path) if _is_folder_epub(path) else file_book_id(path)


def _is_folder_epub(path: Path) -> bool:
    """An unzipped EPUB, which Finder shows as a book."""
    return path.suffix.lower() == ".epub" and path.is_dir()


def _without_skipped_toc(book: Book) -> Book:
    """Book.toc lists only the book itself, whatever the format: an entry on front or
    back matter (Acknowledgments, the Index) is dropped. Block.link keeps pointing
    there: a line of the book's own contents page still jumps where it says."""
    n = len(book.blocks)
    book.toc = [e for e in book.toc if 0 <= e.block < n and not book.blocks[e.block].skipped]
    return book


def _folder_book_id(path: Path) -> str:
    """file_book_id for an unzipped EPUB: a fingerprint of its files' names and bytes."""
    h = hashlib.sha1()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(f.relative_to(path).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _cached(folder: Path) -> Book | None:
    """The cached book, unless it's from an older parser."""
    return Book.load(folder)


def _parse(
    path: Path,
    suffix: str,
    book_id: str,
    progress: Callable[[int, int, str], None],
    cancelled: Callable[[], bool],
) -> Book:
    kind = _KIND_NAMES[suffix]
    try:
        if suffix == ".pdf":
            from . import pdf  # PyMuPDF: only when a PDF is opened

            return pdf.parse_pdf(path, book_id, progress, cancelled)
        if suffix == ".epub":
            from .epub import parse_epub

            return parse_epub(path, book_id, progress, cancelled)
        if cancelled():
            raise ImportCancelled()
        progress(0, 1, "Reading the book…")
        if suffix == ".docx":
            from .docx import parse_docx

            book = parse_docx(path, book_id)
        else:
            from .txt import parse_txt

            book = parse_txt(path, book_id)
        progress(1, 1, "Reading the book…")
        return book
    except (BookImportError, ImportCancelled):
        raise
    except MemoryError as e:
        raise BookImportError(f"This {kind} is too large to open.") from e
    except Exception as e:  # a damaged file can fail anywhere inside a parser
        raise BookImportError(f"This {kind} couldn't be read. It may be damaged.") from e
