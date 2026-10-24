"""Shared test setup: an isolated cache, headless Qt, and a fake TTS engine.

Every test gets its own LIVROSOUND_CACHE, so nothing ever touches the user's
library. Qt runs offscreen so UI tests need no display.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    cache = tmp_path / "livrosound-cache"
    monkeypatch.setenv("LIVROSOUND_CACHE", str(cache))
    return cache


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
