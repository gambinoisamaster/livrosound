"""Single and double page: book pages lying on paper, like Readest's page views.

Both reader views draw their paged modes with these pieces (user's screenshots of
Readest): a soft paper texture filling the window, the page sheet(s) on it with a
light shadow (two sheets meet at the spine on a double page), the chapter's name
small at the top left and "24 / 254" at the bottom right.

sheet_rects() is pure geometry (tested directly). The texture is made once per
look (light or dark) with numpy: 1/f noise (soft clouds) plus fine grain, mapped
between two browns, and tiled seamlessly.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPixmap

from .theme import Theme, ui_font

SHEET_MARGIN = 14  # px between a sheet and the window's top and bottom
PAGE_ASPECT = 0.74  # width / height of a page sheet (a trade paperback)
SIDE_ROOM = 48  # px at least left and right of the sheets
TILE = 384  # px of texture before it repeats

_LIGHT = ((0x72, 0x62, 0x4E), (0xA8, 0x97, 0x7E))  # dark and light end of the brown paper
_DARK = ((0x17, 0x15, 0x13), (0x33, 0x2E, 0x29))
_tiles: dict[bool, QPixmap] = {}


def sheet_rects(width: float, height: float, pages: int, aspect: float = PAGE_ASPECT) -> list[QRectF]:
    """Where the page sheets go in a view of this size: one centered sheet, or two side by
    side meeting at the spine, as tall as the view allows and never wider than it."""
    h = max(40.0, height - 2 * SHEET_MARGIN)
    w = min(h * aspect, (width - 2 * SIDE_ROOM) / pages)
    w = max(80.0, w)
    h = min(h, w / aspect) if pages == 1 else h
    left = (width - w * pages) / 2
    top = (height - h) / 2
    return [QRectF(left + i * w, top, w, h) for i in range(pages)]


def _texture(dark: bool) -> QPixmap:
    """Seamless paper: white noise shaped in the frequency domain (1/f, so soft clouds with
    fine grain on top) — periodic by construction, so the tile repeats without seams."""
    rng = np.random.default_rng(7)
    noise = rng.standard_normal((TILE, TILE))
    fy = np.fft.fftfreq(TILE)[:, None]
    fx = np.fft.fftfreq(TILE)[None, :]
    radius = np.sqrt(fx * fx + fy * fy)
    radius[0, 0] = 1.0
    clouds = np.real(np.fft.ifft2(np.fft.fft2(noise) / radius ** 1.6))
    grain = rng.standard_normal((TILE, TILE))
    field = 0.8 * (clouds - clouds.mean()) / clouds.std() + 0.25 * grain
    field = np.clip(0.5 + field / 5, 0.0, 1.0)
    low, high = (np.array(c, np.float32) for c in (_DARK if dark else _LIGHT))
    rgb = (low + (high - low) * field[..., None].astype(np.float32)).astype(np.uint8)
    argb = (0xFF000000 | (rgb[..., 0].astype(np.uint32) << 16) | (rgb[..., 1].astype(np.uint32) << 8)
            | rgb[..., 2].astype(np.uint32))
    argb = np.ascontiguousarray(argb)
    image = QImage(argb.data, TILE, TILE, TILE * 4, QImage.Format.Format_RGB32).copy()
    return QPixmap.fromImage(image)


def paint_paper(p: QPainter, rect: QRectF, theme: Theme) -> None:
    """The textured paper the pages lie on."""
    tile = _tiles.get(theme.dark)
    if tile is None:
        tile = _tiles[theme.dark] = _texture(theme.dark)
    p.fillRect(rect, QBrush(tile))


def paint_sheet(p: QPainter, rect: QRectF, theme: Theme, *, fill: bool = True) -> None:
    """A page's shadow (and its paper, in the theme's page color)."""
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    for spread, alpha in ((6, 18), (3, 26), (1, 40)):
        p.setBrush(QColor(0, 0, 0, alpha))
        p.drawRect(rect.adjusted(-spread, -spread + 2, spread, spread + 2))
    if fill:
        p.fillRect(rect, QColor(theme.page))
    p.restore()


def paint_spine(p: QPainter, left: QRectF, theme: Theme) -> None:
    """The fold between two sheets: a soft shade either side of the join."""
    x = left.right()
    for i, alpha in enumerate((34, 18, 9)):
        shade = QColor(0, 0, 0, alpha)
        p.fillRect(QRectF(x - 1 - i * 3, left.top(), 3, left.height()), shade)
        p.fillRect(QRectF(x - 2 + i * 3, left.top(), 3, left.height()), shade)


def paint_labels(p: QPainter, view: QRectF, head: str, folio: str, theme: Theme) -> None:
    """The chapter at the top left of the window, the page number at its bottom right."""
    font: QFont = ui_font(11)
    p.save()
    p.setFont(font)
    color = QColor(theme.text if not theme.dark else "#D8D2C8")
    color.setAlphaF(0.55)
    p.setPen(color)
    metrics = QFontMetrics(font)
    if head:
        text = metrics.elidedText(f"•  {head}", Qt.TextElideMode.ElideRight, int(view.width() * 0.6))
        p.drawText(QRectF(view.left() + 20, view.top() + 6, view.width() * 0.6, 22),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
    if folio:
        p.drawText(QRectF(view.right() - 220, view.bottom() - 28, 200, 22),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, folio)
    p.restore()
