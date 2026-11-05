"""The fonts a book can be read in: everything installed, plus the user's own.

Settings ▸ Font ▸ Add Font… copies a .ttf/.otf/.ttc file into paths.fonts_dir()
and registers it with Qt, so it can be chosen at once and is there again at
the next launch (main.py calls load_user_fonts() before the window opens).
The user's file is copied, never moved or referenced, so it can be deleted
afterwards. Needs a QGuiApplication.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtGui import QFontDatabase

from .. import paths

FONT_SUFFIXES = (".ttf", ".otf", ".ttc")
_loaded: dict[str, list[str]] = {}  # file in fonts_dir -> its families


def load_user_fonts() -> list[str]:
    """Register every font the user added; their family names."""
    folder = paths.fonts_dir()
    families: list[str] = []
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() in FONT_SUFFIXES:
                families += _register(path)
    return list(dict.fromkeys(families))


def add_font(path: str | Path) -> list[str]:
    """Copy a font file into LivroSound's fonts and register it. Returns its family
    names; ValueError when the file isn't a font Qt can read."""
    source = Path(path)
    if source.suffix.lower() not in FONT_SUFFIXES:
        raise ValueError("Choose a font file (.ttf, .otf or .ttc).")
    folder = paths.fonts_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    if target.resolve() != source.resolve():
        n = 1
        while target.exists() and target.read_bytes() != source.read_bytes():
            target = folder / f"{source.stem}-{n}{source.suffix}"
            n += 1
        shutil.copyfile(source, target)
    families = _register(target)
    if not families:
        target.unlink(missing_ok=True)
        _loaded.pop(str(target), None)
        raise ValueError("This file isn't a font LivroSound can use.")
    return families


def families() -> list[str]:
    """Every font family that can be chosen, by name (hidden system families left out)."""
    names = [f for f in QFontDatabase.families() if not f.startswith(".") and not QFontDatabase.isPrivateFamily(f)]
    return sorted(set(names), key=str.casefold)


def user_families() -> list[str]:
    return sorted({f for fams in _loaded.values() for f in fams}, key=str.casefold)


def _register(path: Path) -> list[str]:
    key = str(path)
    if key not in _loaded:
        font_id = QFontDatabase.addApplicationFont(key)
        _loaded[key] = list(QFontDatabase.applicationFontFamilies(font_id)) if font_id >= 0 else []
    return _loaded[key]
