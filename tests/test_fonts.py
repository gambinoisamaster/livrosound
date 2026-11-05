"""Fonts the user adds are copied into LivroSound's own fonts and registered."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QFontDatabase

from livrosound import paths
from livrosound.ui import fonts


def _a_font_file() -> Path | None:
    for folder in (Path("/usr/share/fonts"), Path("/System/Library/Fonts"), Path("/Library/Fonts")):
        if folder.is_dir():
            for path in sorted(folder.rglob("*.ttf")):
                return path
    return None


def test_adding_a_font_copies_and_registers_it(qapp, tmp_path):
    source = _a_font_file()
    if source is None:
        pytest.skip("no .ttf font on this machine")
    families = fonts.add_font(source)
    assert families and (paths.fonts_dir() / source.name).is_file()
    assert families[0] in QFontDatabase.families() and families[0] in fonts.user_families()
    assert fonts.add_font(source) == families  # again: the same copy
    assert families[0] in fonts.load_user_fonts()


def test_not_a_font(qapp, tmp_path):
    with pytest.raises(ValueError):
        fonts.add_font(tmp_path / "notes.txt")
    fake = tmp_path / "fake.ttf"
    fake.write_bytes(b"nope")
    with pytest.raises(ValueError):
        fonts.add_font(fake)
    assert not (paths.fonts_dir() / "fake.ttf").exists()
    assert fonts.families() == sorted(fonts.families(), key=str.casefold)
