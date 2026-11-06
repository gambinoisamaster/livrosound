"""Reading themes, the UI and reading fonts, and the stylesheet built from them.

Four themes, picked in Themes & Settings (the "Aa" button), as in Script Radio:

    Original  white page, black text
    Quiet     #4A4A4D page, #EBEBF5 text
    Calm      #F1E1C9 page, #34281B text
    Dark      black page, white text

Every color around the page comes from the color wheel:

Highlighter (the sentence being read) — the page's complement, so it stands
out from the page without clashing:

    Calm   page is warm sand (hue ~36°)       -> cornflower blue (~216°)
    Quiet  page is cool slate (hue ~240°)     -> warm amber (~48°)
    Original / Dark  pages have no hue, so the complement of the app's fern-green
           accent (hue ~106°) is used instead -> lilac / plum (~286°)

Frame (the toolbar, the player and the status bar, all one color) — also the
page's complement, at least 3:1 against the page (WCAG's minimum for telling
UI shapes apart), so the frame never melts into the book:

    Calm   sand page   -> deep navy (~216°)
    Quiet  slate page  -> warm khaki (~44°)
    Original / Dark    -> slate indigo (~223°): a triad with the fern-green play
           button (106° + 120°), so the play button stands out on it

Book title (centered in the toolbar) — the frame's complement: gold or amber
on the indigo and navy frames, indigo on the khaki one, at least 4.5:1.

Highlighted text keeps at least 4.5:1 too (8:1 or more on the light themes).
tests/test_theme.py holds the themes to all of these numbers.

Fonts: the UI is SF Pro, the macOS system font (what Apple Books and the rest
of macOS use, with optical sizes built in). The book itself is set in a reading
serif like Apple Books': Iowan Old Style, falling back to New York, Charter and
Georgia, all of which ship with macOS. Never put a font-family in the
stylesheet: it would override the per-widget fonts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtGui import QColor, QFont, QFontDatabase

# Qt draws square corners when a border-radius is more than half a widget's
# height, so every radius here stays at or under half of what it rounds.

# Kept green on purpose as a "ready to listen" signal — the play button,
# switches, Upload New Book and Generate Audio share it in every theme.
ACCENT = "#4F7942"  # fern green
ACCENT_HOVER = "#5c8c4d"
ACCENT_PRESSED = "#436838"
ON_ACCENT = "#ffffff"
SINGLE_VOICE_ACCENT = "#ed7e7e"  # Script Radio's "one voice" salmon, kept for anything that needs a second switch color
CANCEL_RED = "#E8432B"  # the Cancel Generation button
CANCEL_RED_HOVER = "#F05A43"
# Generation progress: light blue at the start of the bar, darkest at 100%.
PROGRESS_BLUES = ("#DCEBFF", "#8DBDF7", "#2F6FD0", "#0A2A6B")
EXPORT_BLUE = "#1A4FA8"  # Export MP3 takes the finished bar's place, in its blue

# The book's typeface, best first (Apple Books' default is Iowan; the rest are fallbacks).
READING_FAMILIES = ["Iowan Old Style", "New York", "Charter", "Georgia"]


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    page: str  # book background
    text: str  # book text, and the text of dialogs and popovers
    highlight: str  # the sentence being read
    chrome: str  # the frame: toolbar, player, status bar
    on_chrome: str  # text and icons on the frame
    chrome_raised: str  # buttons on the frame
    chrome_hover: str
    chrome_line: str
    title: str  # the book's title, centered in the toolbar
    window: str  # dialogs, and the landing page's edges
    panel: str  # popovers and menus
    raised: str  # buttons and input bubbles in dialogs
    raised_hover: str
    line: str  # borders
    hairline: str  # separators between areas
    muted: str  # secondary text: hints, captions
    scroll: str  # scroll bar handle
    scroll_hover: str
    dark: bool
    partner: str  # the theme the light/dark button switches to
    play_ring: bool = False  # ring the play button where green alone won't stand out on the frame


THEMES: dict[str, Theme] = {
    t.key: t
    for t in (
        Theme(
            "original", "Original", page="#FFFFFF", text="#000000", highlight="#F4D1FF",
            chrome="#1B2130", on_chrome="#F2F4F8", chrome_raised="#2A3245", chrome_hover="#364057",
            chrome_line="#3D4760", title="#F5C451",
            window="#F5F5F7", panel="#FFFFFF", raised="#FFFFFF", raised_hover="#EDEDF0",
            line="#D5D5DA", hairline="#E2E2E6", muted="#6E6E73",
            scroll="rgba(0, 0, 0, 0.24)", scroll_hover="rgba(0, 0, 0, 0.42)",
            dark=False, partner="dark",
        ),
        Theme(
            "quiet", "Quiet", page="#4A4A4D", text="#EBEBF5", highlight="#73642B",
            chrome="#BCAE86", on_chrome="#24211B", chrome_raised="#CDC09A", chrome_hover="#D8CDAA",
            chrome_line="#A3956D", title="#1C2760",
            window="#3B3B3E", panel="#434346", raised="#55555A", raised_hover="#606065",
            line="#626267", hairline="#303033", muted="#B4B4BE",
            scroll="rgba(235, 235, 245, 0.28)", scroll_hover="rgba(235, 235, 245, 0.46)",
            dark=True, partner="calm", play_ring=True,
        ),
        Theme(
            "calm", "Calm", page="#F1E1C9", text="#34281B", highlight="#B1C9ED",
            chrome="#14233A", on_chrome="#F6EEDF", chrome_raised="#20334F", chrome_hover="#2B4060",
            chrome_line="#33496B", title="#F4BD55",
            window="#E8D5B8", panel="#F8EEDF", raised="#FBF4E8", raised_hover="#F2E6D3",
            line="#D3BD9C", hairline="#DCC7A8", muted="#7A6650",
            scroll="rgba(52, 40, 27, 0.26)", scroll_hover="rgba(52, 40, 27, 0.44)",
            dark=False, partner="quiet",
        ),
        Theme(
            "dark", "Dark", page="#000000", text="#FFFFFF", highlight="#5E2C6D",
            chrome="#4E5A78", on_chrome="#FFFFFF", chrome_raised="#5D6A8A", chrome_hover="#6A7899",
            chrome_line="#7380A0", title="#FFD66B",
            window="#121213", panel="#1C1C1E", raised="#2C2C2E", raised_hover="#38383B",
            line="#3A3A3C", hairline="#262628", muted="#98989F",
            scroll="rgba(255, 255, 255, 0.26)", scroll_hover="rgba(255, 255, 255, 0.44)",
            dark=True, partner="original", play_ring=True,
        ),
    )
}


def _luminance(color: str) -> float:
    c = QColor(color)
    return (0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF())


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = QColor(a), QColor(b)
    channels = (round(x + (y - x) * t) for x, y in ((ca.red(), cb.red()), (ca.green(), cb.green()), (ca.blue(), cb.blue())))
    return "#{:02X}{:02X}{:02X}".format(*channels)


def custom_theme(page: str, text: str) -> Theme:
    """The Custom theme: the user's paper and ink, with the frame and panels of
    Original (a light paper) or Dark (a dark one), and a highlighter mixed from the paper."""
    dark = _luminance(page) < 0.45
    base = THEMES["dark" if dark else "original"]
    highlight = _mix(page, "#C77DFF" if dark else "#B05CE0", 0.45 if dark else 0.3)
    return replace(base, key="custom", name="Custom", page=page, text=text, highlight=highlight,
                   muted=_mix(text, page, 0.4), partner="dark" if not dark else "original")


_custom = custom_theme("#F6F1E7", "#2B2621")


def set_custom_colors(page: str, text: str) -> Theme:
    """The user picked the Custom theme's colors (Settings ▸ Color)."""
    global _custom
    if QColor(page).isValid() and QColor(text).isValid():
        _custom = custom_theme(QColor(page).name(), QColor(text).name())
    return _custom


def theme_for(key: str) -> Theme:
    if key == "custom":
        return _custom
    return THEMES.get(key, THEMES["original"])


_current = THEMES["original"]


def current() -> Theme:
    """The theme on screen; custom-painted widgets read it when they paint."""
    return _current


def set_current(theme: Theme) -> None:
    global _current
    _current = theme


def ui_font(size: float = 13, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """The UI typeface (SF Pro, via the system font) at a size and weight."""
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    font.setPointSizeF(size)
    font.setWeight(weight)
    return font


def reading_font(size: float, weight: QFont.Weight = QFont.Weight.Normal, family: str = "") -> QFont:
    """The book's serif (Iowan Old Style or its first installed fallback), or the
    user's preferred family (Settings ▸ Font) with the serif behind it."""
    font = QFont()
    font.setFamilies([family, *READING_FAMILIES] if family else READING_FAMILIES)
    font.setPointSizeF(size)
    font.setWeight(weight)
    return font


def tooltip_stylesheet(t: Theme) -> str:
    """Small, thin tooltips like macOS's own. Tooltips are separate windows, so
    this goes on the application rather than the main window."""
    if t.dark:
        bg, fg, border = "#2C2C2E", "#F2F2F7", "rgba(255, 255, 255, 0.16)"
    else:
        bg, fg, border = "#F2F2F2", "#1D1D1F", "rgba(0, 0, 0, 0.16)"
    return (
        f"QToolTip {{ background: {bg}; color: {fg}; border: 1px solid {border};"
        f" border-radius: 4px; padding: 1px 5px; font-size: 11px; }}"
    )


def stylesheet(t: Theme) -> str:
    """The main window's stylesheet for a theme. No font-family anywhere: the UI
    font is set on the application, and the reader views set their own serif."""
    on_chrome_soft = _alpha(t.on_chrome, 0.72)
    # The selected segment of a segmented control: a white chip on light themes,
    # a lighter one on dark themes (as in macOS).
    segment_on = _alpha(t.text, 0.2) if t.dark else t.panel
    return f"""
QMainWindow, QDialog {{ background: {t.window}; color: {t.text}; }}
QWidget#transportWidget, QWidget#readerStage {{ background: {t.page}; }}
QLabel {{ color: {t.text}; background: transparent; }}
QCheckBox {{ color: {t.text}; spacing: 8px; }}
QRadioButton {{ color: {t.text}; spacing: 8px; }}

QToolBar {{ background: {t.chrome}; border: none; border-bottom: 1px solid {t.chrome_line};
           spacing: 8px; padding: 7px 10px; }}
QToolBar QToolButton {{ background: {t.chrome_raised}; color: {t.on_chrome}; border: 1px solid {t.chrome_line};
           border-radius: 10px; padding: 6px 14px; font-weight: 500; }}
QToolBar QToolButton:hover {{ background: {t.chrome_hover}; }}
QToolBar QToolButton:pressed, QToolBar QToolButton:checked {{ background: {t.chrome_line}; }}
QToolBar QToolButton:disabled {{ color: {_alpha(t.on_chrome, 0.45)}; }}
QToolBar QToolButton#pillButton {{ border-radius: 15px; padding: 5px 12px; }}
QToolBar QLabel {{ color: {t.on_chrome}; }}
QToolBar QLabel#bookTitle {{ color: {t.title}; }}

QStatusBar {{ background: {t.chrome}; color: {on_chrome_soft}; border-top: 1px solid {t.chrome_line}; }}
QStatusBar QLabel {{ color: {on_chrome_soft}; }}
QStatusBar::item {{ border: none; }}

QTextEdit#readerView {{ background: {t.page}; color: {t.text}; border: none;
           selection-background-color: {t.highlight}; selection-color: {t.text}; }}
QAbstractScrollArea#pageView {{ background: {t.page}; border: none; }}

QPushButton {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.line};
           border-radius: 12px; padding: 6px 14px; }}
QPushButton:hover {{ background: {t.raised_hover}; }}
QPushButton:disabled {{ color: {t.muted}; }}
QPushButton#transportButton {{ background: {t.chrome_raised}; color: {t.on_chrome}; border: 1px solid {t.chrome_line};
           border-radius: 22px; min-width: 44px; min-height: 44px; padding: 0; }}
QPushButton#transportButton:hover {{ background: {t.chrome_hover}; }}
QPushButton#transportButton:disabled {{ color: {_alpha(t.on_chrome, 0.4)}; }}
QPushButton#skipButton {{ background: {t.chrome_raised}; color: {t.on_chrome}; border: 1px solid {t.chrome_line};
           border-radius: 16px; min-height: 32px; padding: 0 13px; font-weight: 600; }}
QPushButton#skipButton:hover {{ background: {t.chrome_hover}; }}
QPushButton#skipButton:disabled {{ color: {_alpha(t.on_chrome, 0.4)}; }}
QPushButton#transportButtonPrimary {{ background: {ACCENT}; color: {ON_ACCENT};
           border: {"2px solid " + t.on_chrome if t.play_ring else "1px solid " + ACCENT};
           border-radius: 28px; min-width: 56px; min-height: 56px; padding: 0; }}
QPushButton#transportButtonPrimary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#transportButtonPrimary:disabled {{ background: {t.chrome_raised}; color: {_alpha(t.on_chrome, 0.4)};
           border: 1px solid {t.chrome_line}; }}
QPushButton#cancelButton {{ background: {CANCEL_RED}; color: #111111; border: none; border-radius: 14px;
           padding: 0 18px; font-weight: 700; }}
QPushButton#cancelButton:hover {{ background: {CANCEL_RED_HOVER}; }}
QPushButton#exportButton {{ background: {EXPORT_BLUE}; color: #FFFFFF; border: none; border-radius: 11px;
           padding: 3px 18px; font-weight: 600; }}
QPushButton#exportButton:hover {{ background: {PROGRESS_BLUES[2]}; }}
QPushButton#resumeButton {{ background: {ACCENT}; color: {ON_ACCENT}; border: none; border-radius: 11px;
           padding: 3px 18px; font-weight: 600; }}
QPushButton#resumeButton:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#resumeButton:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#generateAudioButton {{ background: {ACCENT}; color: {ON_ACCENT}; border: 1px solid {ACCENT};
           border-radius: 16px; padding: 10px 24px; font-weight: 600; font-size: 15px; }}
QPushButton#generateAudioButton:hover {{ background: {ACCENT_HOVER}; border: 1px solid {ACCENT_HOVER}; }}
QPushButton#generateAudioButton:disabled {{ background: {t.raised_hover}; color: {t.muted}; border: 1px solid {t.line}; }}
QPushButton#goBackButton {{ background: transparent; color: {t.text}; border: 1px solid {t.line};
           border-radius: 16px; padding: 10px 22px; }}
QPushButton#goBackButton:hover {{ background: {t.raised_hover}; }}
QPushButton#primaryButton {{ background: {ACCENT}; color: {ON_ACCENT}; border: 1px solid {ACCENT};
           border-radius: 14px; padding: 6px 20px; font-weight: 600; }}
QPushButton#primaryButton:hover {{ background: {ACCENT_HOVER}; border: 1px solid {ACCENT_HOVER}; }}
QPushButton#primaryButton:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#secondaryButton {{ background: transparent; color: {t.text}; border: 1px solid {t.line};
           border-radius: 14px; padding: 6px 20px; }}
QPushButton#secondaryButton:hover {{ background: {t.raised_hover}; }}
QPushButton#dangerButton {{ background: {CANCEL_RED}; color: #111111; border: none; border-radius: 14px;
           padding: 6px 20px; font-weight: 700; }}
QPushButton#dangerButton:hover {{ background: {CANCEL_RED_HOVER}; }}

QPushButton#landingPrimary {{ background: {ACCENT}; color: {ON_ACCENT}; border: none; border-radius: 22px;
           padding: 0 30px; }}
QPushButton#landingPrimary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#landingPrimary:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#landingSecondary {{ background: transparent; color: {t.text}; border: 1px solid {_alpha(t.text, 0.22)};
           border-radius: 22px; padding: 0 30px; }}
QPushButton#landingSecondary:hover {{ background: {_alpha(t.text, 0.06)}; border: 1px solid {_alpha(t.text, 0.34)}; }}
QPushButton#landingSecondary:pressed {{ background: {_alpha(t.text, 0.1)}; }}
QPushButton#landingSecondary:disabled {{ color: {_alpha(t.text, 0.3)}; border: 1px solid {_alpha(t.text, 0.12)}; }}
QLabel#wordmark {{ color: {t.text}; }}
QLabel#tagline {{ color: {t.muted}; }}
QLabel#landingFootnote {{ color: {_alpha(t.muted, 0.8)}; }}

QLineEdit {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.line}; border-radius: 8px;
           padding: 4px 8px; selection-background-color: {t.highlight}; selection-color: {t.text}; }}
QComboBox {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.line}; border-radius: 10px; padding: 5px 12px; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {t.panel}; color: {t.text}; border: 1px solid {t.line};
           outline: none; padding: 4px; selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; }}
QComboBox#speedCombo {{ background: {t.chrome}; color: {t.on_chrome}; border: 1px solid {t.chrome_line};
           border-radius: 15px; padding: 4px 6px 4px 14px; min-height: 22px; }}
QComboBox#speedCombo:hover {{ background: {t.chrome_raised}; }}
QComboBox#speedCombo::drop-down {{ width: 16px; }}
QComboBox#speedCombo QAbstractItemView {{ background: {t.chrome}; color: {t.on_chrome}; border: 1px solid {t.chrome_line};
           selection-background-color: {t.chrome_hover}; selection-color: {t.on_chrome}; outline: none; padding: 4px; }}

QSlider::groove:horizontal {{ background: {t.line}; height: 6px; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {t.text}; border: none; width: 14px; margin: -4px 0; border-radius: 7px; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget#qt_scrollarea_viewport {{ background: transparent; }}
QWidget#qt_scrollarea_vcontainer, QWidget#qt_scrollarea_hcontainer {{ background: transparent; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QTableWidget, QTreeWidget {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.line}; border-radius: 10px;
           outline: none; selection-background-color: {t.highlight}; selection-color: {t.text}; }}
QHeaderView::section {{ background: {t.window}; color: {t.muted}; border: none; border-bottom: 1px solid {t.hairline};
           padding: 6px 8px; }}

QScrollBar:vertical {{ background: transparent; width: 14px; margin: 4px 3px 4px 3px; }}
QScrollBar::handle:vertical {{ background: {t.scroll}; min-height: 40px; border-radius: 4px; }}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{ background: {t.scroll_hover}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none; background: none; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ background: transparent; height: 14px; margin: 3px 4px 3px 4px; }}
QScrollBar::handle:horizontal {{ background: {t.scroll}; min-width: 40px; border-radius: 4px; }}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{ background: {t.scroll_hover}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; border: none; background: none; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}

QListWidget#popoverList::item {{ border-radius: 8px; color: {t.text}; }}
QListWidget#popoverList::item:hover {{ background: {t.raised_hover}; }}
QListWidget#popoverList::item:selected {{ background: {t.highlight}; }}
QListWidget#popoverList::item:disabled {{ background: transparent; }}
QLabel#popoverTitle {{ color: {t.muted}; }}
QLabel#groupHeader {{ color: {t.muted}; }}
QLabel#caption {{ color: {t.muted}; }}
QFrame#hairline {{ background: {t.hairline}; border: none; }}
QFrame#segmented {{ background: {t.raised_hover}; border: none; border-radius: 20px; }}
QFrame#segmented QToolButton {{ background: transparent; border: none; color: {t.text}; border-radius: 16px; }}
QFrame#segmented QToolButton:hover {{ background: {t.line}; }}
QFrame#segmented QToolButton:checked {{ background: {segment_on}; border: 1px solid {t.line}; }}
QFrame#segDivider {{ background: {t.line}; border: none; }}
QToolButton#roundButton {{ background: {t.raised_hover}; border: none; border-radius: 20px; }}
QToolButton#roundButton:hover {{ background: {t.line}; }}
QToolButton#pickerArrow {{ background: transparent; border: none; border-radius: 8px; }}
QToolButton#pickerArrow:hover {{ background: {t.raised_hover}; }}

QLineEdit#librarySearch {{ background: {t.raised_hover}; border: 1px solid transparent; border-radius: 16px;
           padding: 4px 14px; }}
QLineEdit#librarySearch:focus {{ border: 1px solid {t.line}; background: {t.raised}; }}
QToolButton#libraryTool {{ background: transparent; color: {t.text}; border: none; border-radius: 16px; padding: 4px 12px; }}
QToolButton#libraryTool:hover {{ background: {t.raised_hover}; }}
QToolButton#libraryTool:disabled {{ color: {t.muted}; }}
QLabel#libraryTitle {{ color: {t.text}; }}
QFrame#selectionBar {{ background: {t.panel}; border: 1px solid {t.line}; border-radius: 22px; }}
QToolButton#selectionAction {{ background: transparent; color: {t.text}; border: none; border-radius: 15px;
           padding: 6px 12px; }}
QToolButton#selectionAction:hover {{ background: {t.raised_hover}; }}
QToolButton#selectionAction:disabled {{ color: {_alpha(t.muted, 0.6)}; }}
QToolButton#menuToggle {{ background: transparent; color: {t.text}; border: none; padding: 4px 6px; }}
QMenu {{ background: {t.panel}; color: {t.text}; border: 1px solid {t.line}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 26px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT_HOVER}; color: {ON_ACCENT}; }}
QMenu::item:disabled {{ color: {t.muted}; }}
QMenu::separator {{ height: 1px; background: {t.hairline}; margin: 5px 8px; }}
QMenu::section {{ color: {t.muted}; padding: 6px 12px 2px 12px; }}
QMenu::indicator {{ width: 14px; left: 7px; }}

QLabel#readingInfo {{ color: {t.muted}; background: {_alpha(t.page, 0.9)}; border: 1px solid {t.hairline};
           border-radius: 10px; padding: 2px 9px; }}

QLabel#castingTitle, QLabel#alertTitle {{ color: {t.text}; }}
QLabel#castingSubtitle, QLabel#hint, QLabel#alertText {{ color: {t.muted}; }}
QFrame#voicePicker {{ background: {t.raised}; border: 1px solid {t.line}; border-radius: 12px; }}
QFrame#voicePicker:hover {{ border: 1px solid {t.muted}; }}
QFrame#voicePicker QLineEdit {{ background: transparent; border: none; padding: 0 4px; }}
QFrame#pickerDivider {{ background: {t.line}; border: none; }}
QFrame#card {{ background: {t.raised}; border: 1px solid {t.line}; border-radius: 14px; }}
QMessageBox {{ background: {t.window}; }}
"""


def _alpha(color: str, alpha: float) -> str:
    """A #RRGGBB color at an opacity, as a stylesheet rgba()."""
    color = color.lstrip("#")
    r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha})"
