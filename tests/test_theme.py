"""The reading themes: highlighters complementary to the page, legible, never blending in;
a frame that stands apart from the page; the stylesheet and fonts the rest of the UI relies on."""

from __future__ import annotations

import colorsys
import math
import re

from PySide6.QtGui import QFont

from livrosound.ui import theme as themes
from livrosound.ui.theme import ACCENT, READING_FAMILIES, THEMES, reading_font, stylesheet, theme_for, ui_font


def _rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))


def _linear(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(color):
    r, g, b = (_linear(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _lab(color):
    r, g, b = (_linear(c) for c in _rgb(color))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116  # noqa: E731
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _hue(color):
    r, g, b = (c / 255 for c in _rgb(color))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360


def _hue_distance(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _tinted(color):
    r, g, b = (c / 255 for c in _rgb(color))
    return colorsys.rgb_to_hls(r, g, b)[2] > 0.005  # saturation: the color has a hue


def test_the_four_themes_as_specified():
    assert [(t.name, t.page, t.text) for t in THEMES.values()] == [
        ("Original", "#FFFFFF", "#000000"),
        ("Quiet", "#4A4A4D", "#EBEBF5"),
        ("Calm", "#F1E1C9", "#34281B"),
        ("Dark", "#000000", "#FFFFFF"),
    ]


def test_highlighted_text_stays_legible():
    for t in THEMES.values():
        assert contrast(t.text, t.highlight) >= 4.5, t.name  # WCAG AA
        if not t.dark:
            assert contrast(t.text, t.highlight) >= 7, t.name


def test_highlight_never_blends_into_the_page():
    for t in THEMES.values():
        assert math.dist(_lab(t.highlight), _lab(t.page)) > 25, t.name


def test_highlight_is_the_pages_complement_on_the_color_wheel():
    for t in THEMES.values():
        source = _hue(t.page) if _tinted(t.page) else _hue(ACCENT)  # neutral pages complement the accent
        assert _hue_distance(_hue(t.highlight), source) > 150, t.name


def test_unknown_theme_keys_fall_back_to_original():
    assert theme_for("dark").key == "dark"
    assert theme_for("nonsense").key == "original"


def test_partners_switch_between_light_and_dark():
    for t in THEMES.values():
        partner = THEMES[t.partner]
        assert partner.dark != t.dark, t.name


def test_the_frame_stands_apart_from_the_page():
    """Toolbar, player and status bar share one color, distinct from the page
    (WCAG's 3:1 for UI shapes), with readable labels on it."""
    for t in THEMES.values():
        assert contrast(t.chrome, t.page) >= 3, t.name
        assert contrast(t.on_chrome, t.chrome) >= 4.5, t.name


def test_the_frame_complements_the_page():
    for t in THEMES.values():
        if _tinted(t.page):
            assert _hue_distance(_hue(t.chrome), _hue(t.page)) > 150, t.name
        else:  # neutral pages: slate indigo, a triad with the fern-green play button
            assert 100 < _hue_distance(_hue(t.chrome), _hue(ACCENT)) < 140, t.name


def test_the_title_is_prominent_on_the_frame():
    for t in THEMES.values():
        assert contrast(t.title, t.chrome) >= 4.5, t.name
        assert _hue_distance(_hue(t.title), _hue(t.chrome)) > 150, t.name  # the frame's complement


def test_the_play_button_never_disappears_into_the_frame():
    for t in THEMES.values():
        assert contrast(ACCENT, t.chrome) >= 3 or t.play_ring, t.name


def test_dialog_text_is_readable():
    for t in THEMES.values():
        for surface in (t.window, t.panel, t.raised):
            assert contrast(t.text, surface) >= 4.5, t.name  # WCAG AA
        assert contrast(t.muted, t.window) >= 3.5, t.name


def test_white_on_the_accent_is_readable():
    assert contrast("#FFFFFF", ACCENT) >= 4.5


def test_stylesheet_never_sets_a_font_family():
    for t in THEMES.values():
        css = stylesheet(t)
        assert "font-family" not in css, t.name
        assert css.count("{") == css.count("}"), t.name


def test_stylesheet_radii_stay_within_half_the_height():
    """Qt draws square corners when a radius exceeds half the widget's height."""
    css = stylesheet(THEMES["original"])
    for name in ("transportButton", "transportButtonPrimary", "skipButton"):
        rule = re.search(r"QPushButton#" + name + r" \{([^}]*)\}", css).group(1)
        radius = int(re.search(r"border-radius: (\d+)px", rule).group(1))
        height = int(re.search(r"min-height: (\d+)px", rule).group(1))
        assert radius * 2 <= height, name


def test_stylesheet_styles_the_livrosound_widgets():
    css = stylesheet(THEMES["calm"])
    for name in (
        "QLabel#bookTitle", "QTextEdit#readerView", "QWidget#readerStage", "QAbstractScrollArea#pageView",
        "QPushButton#transportButton", "QPushButton#transportButtonPrimary", "QPushButton#skipButton",
        "QPushButton#cancelButton", "QPushButton#exportButton", "QPushButton#resumeButton",
        "QPushButton#generateAudioButton", "QPushButton#goBackButton", "QLabel#castingTitle",
        "QPushButton#landingPrimary", "QPushButton#landingSecondary", "QLabel#wordmark",
    ):
        assert name in css, name
    assert ACCENT in re.search(r"QPushButton#resumeButton \{([^}]*)\}", css).group(1)


def test_current_theme_round_trip():
    before = themes.current()
    try:
        themes.set_current(THEMES["dark"])
        assert themes.current().key == "dark"
    finally:
        themes.set_current(before)


def test_fonts(qapp):
    assert ui_font(15, QFont.Weight.DemiBold).pointSizeF() == 15
    assert ui_font(15, QFont.Weight.DemiBold).weight() == QFont.Weight.DemiBold
    serif = reading_font(20)
    assert serif.families() == READING_FAMILIES
    assert serif.pointSizeF() == 20
