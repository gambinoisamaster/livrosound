"""Where LivroSound keeps things.

Everything the app remembers lives under CACHE_DIR (gitignored), like Script
Radio's cache/: settings, the library of books, and one folder per book with its
parsed text and generated audio. Delete the folder to start fresh.

Tests point LIVROSOUND_CACHE at a temporary folder so they never touch the
user's library.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = APP_DIR / "assets"
VOICES_DIR = ASSETS_DIR / "voices"
ICONS_DIR = ASSETS_DIR / "icons"


def cache_dir() -> Path:
    """Read on every call, so a test can set LIVROSOUND_CACHE after import."""
    return Path(os.environ.get("LIVROSOUND_CACHE") or APP_DIR / "cache")


def books_dir() -> Path:
    return cache_dir() / "books"


def book_dir(book_id: str) -> Path:
    """One folder per book: book.json, original.html, res/, audio.sqlite."""
    return books_dir() / book_id


def settings_path() -> Path:
    return cache_dir() / "settings.json"


def library_path() -> Path:
    return cache_dir() / "library.json"


def voice_previews_dir() -> Path:
    return cache_dir() / "voice_previews"
