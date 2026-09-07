#!/usr/bin/env python3
"""
Draw the app icon and write assets/icon.png + assets/icon.icns.

Run after changing the artwork below:

    .venv/bin/python make_icon.py

build_app.sh calls this automatically when the icon is missing.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import (
    QGuiApplication, QImage, QPainter, QPainterPath, QColor,
    QLinearGradient, QBrush, QPen,
)
from PyQt6.QtCore import QRectF, QPointF, Qt


S = 1024                       # master canvas, everything below is in these units
ASSETS = Path(__file__).parent / "assets"

WHITE = QColor(255, 255, 255)
AMBER = QColor(255, 199, 88)
SCREEN_TOP = QColor(46, 86, 214)      # inside of a display/phone screen
SCREEN_BOTTOM = QColor(32, 54, 168)


def screen_brush(rect: QRectF) -> QBrush:
    g = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
    g.setColorAt(0.0, SCREEN_TOP)
    g.setColorAt(1.0, SCREEN_BOTTOM)
    return QBrush(g)


def rounded(path_rect: QRectF, radius: float) -> QPainterPath:
    p = QPainterPath()
    p.addRoundedRect(path_rect, radius, radius)
    return p


def draw_background(p: QPainter) -> None:
    """macOS-style squircle tile with a blue -> indigo gradient."""
    g = QLinearGradient(0, 0, S * 0.35, S)
    g.setColorAt(0.0, QColor(74, 144, 255))
    g.setColorAt(0.55, QColor(45, 92, 226))
    g.setColorAt(1.0, QColor(38, 52, 168))

    # 12% inset all round: macOS leaves breathing room inside the 1024 grid
    tile = QRectF(S * 0.06, S * 0.06, S * 0.88, S * 0.88)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawPath(rounded(tile, S * 0.2237))

    # top highlight, keeps the tile from looking flat at large sizes
    hi = QLinearGradient(0, tile.top(), 0, tile.top() + tile.height() * 0.5)
    hi.setColorAt(0.0, QColor(255, 255, 255, 46))
    hi.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(hi))
    p.drawPath(rounded(tile, S * 0.2237))


def draw_mac(p: QPainter) -> None:
    """Left-hand device: a desktop display on a stand."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(WHITE)

    body = QRectF(140, 300, 300, 232)
    p.drawPath(rounded(body, 26))

    screen = QRectF(174, 334, 232, 150)
    p.setBrush(screen_brush(screen))
    p.drawPath(rounded(screen, 12))

    p.setBrush(WHITE)
    p.drawRect(QRectF(258, 532, 64, 62))          # neck
    p.drawPath(rounded(QRectF(196, 590, 188, 34), 17))   # foot


def draw_phone(p: QPainter) -> None:
    """Right-hand device: a phone, screen punched out to match the display."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(WHITE)

    body = QRectF(640, 214, 262, 500)
    p.drawPath(rounded(body, 48))

    screen = QRectF(672, 274, 198, 366)
    p.setBrush(screen_brush(screen))
    p.drawPath(rounded(screen, 20))
    p.drawPath(rounded(QRectF(736, 240, 70, 14), 7))     # earpiece slot

    p.setBrush(WHITE)
    p.drawPath(rounded(QRectF(722, 664, 98, 16), 8))     # home indicator


def arrow(p: QPainter, x0: float, x1: float, y: float, shaft: float, head: float) -> None:
    """Horizontal arrow from x0 to x1 (direction follows their order)."""
    facing = 1 if x1 > x0 else -1
    tip = x1
    base = x1 - facing * head

    path = QPainterPath()
    path.addRoundedRect(
        QRectF(min(x0, base), y - shaft / 2, abs(base - x0), shaft), shaft / 2, shaft / 2)

    headp = QPainterPath()
    headp.moveTo(QPointF(tip, y))
    headp.lineTo(QPointF(base, y - head * 0.72))
    headp.lineTo(QPointF(base, y + head * 0.72))
    headp.closeSubpath()

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(AMBER)
    p.drawPath(path.united(headp))


def draw_arrows(p: QPainter) -> None:
    arrow(p, 468, 618, 404, shaft=34, head=54)   # Mac -> phone
    arrow(p, 618, 468, 524, shaft=34, head=54)   # phone -> Mac


def render() -> QImage:
    img = QImage(S, S, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    p = QPainter(img)
    p.setRenderHints(QPainter.RenderHint.Antialiasing |
                     QPainter.RenderHint.SmoothPixmapTransform)
    draw_background(p)
    draw_mac(p)
    draw_phone(p)
    draw_arrows(p)
    p.end()
    return img


def build_icns(png: Path, icns: Path) -> None:
    """sips + iconutil, the Apple-sanctioned route (see assets/README.md)."""
    iconset = png.parent / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()

    for size, names in [
        (16, ["icon_16x16.png"]),
        (32, ["icon_16x16@2x.png", "icon_32x32.png"]),
        (64, ["icon_32x32@2x.png"]),
        (128, ["icon_128x128.png"]),
        (256, ["icon_128x128@2x.png", "icon_256x256.png"]),
        (512, ["icon_256x256@2x.png", "icon_512x512.png"]),
        (1024, ["icon_512x512@2x.png"]),
    ]:
        first = iconset / names[0]
        subprocess.run(["sips", "-z", str(size), str(size), str(png),
                        "--out", str(first)],
                       check=True, capture_output=True)
        for extra in names[1:]:
            shutil.copyfile(first, iconset / extra)

    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                   check=True)
    shutil.rmtree(iconset)


def main() -> int:
    QGuiApplication(sys.argv)
    ASSETS.mkdir(exist_ok=True)

    png = ASSETS / "icon.png"
    icns = ASSETS / "icon.icns"

    render().save(str(png), "PNG")
    build_icns(png, icns)

    print(f"wrote {png}")
    print(f"wrote {icns}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
