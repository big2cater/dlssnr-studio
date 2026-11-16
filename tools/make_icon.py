"""Draw the application icon and write build/app.ico.

    python3 tools/make_icon.py

No image assets are checked in: the mark is four lines of painting so it stays
crisp at 16 px and can be retinted without re-exporting anything.

The .ico holds PNG-compressed frames (Vista and later read those natively), and
the 256 px frame is what Explorer shows in large-icon views.
"""
from __future__ import annotations

import pathlib
import struct
import sys

OUT = pathlib.Path(__file__).resolve().parents[1] / "build/app.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

# Deep indigo to cyan, the same family the window uses for its headers.
TOP = (0x1B, 0x1F, 0x3B)
BOTTOM = (0x0D, 0x2A, 0x3F)
ACCENT = (0x4F, 0xD1, 0xC5)


def paint(size: int) -> "QImage":
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen

    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)

    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing, True)

    # Rounded tile with a vertical gradient.
    rect = QRectF(0, 0, size, size)
    radius = size * 0.24
    gradient = QLinearGradient(QPointF(0, 0), QPointF(0, size))
    gradient.setColorAt(0.0, QColor(*TOP))
    gradient.setColorAt(1.0, QColor(*BOTTOM))
    p.setPen(Qt.NoPen)
    p.setBrush(gradient)
    p.drawRoundedRect(rect, radius, radius)

    # A ring standing in for the lens, open at the top right so the mark reads
    # as motion rather than a closed circle. Drawn as geometry, not text: a
    # glyph would rely on the font being resolvable in a headless build.
    inset = size * 0.21
    ring = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    pen = QPen(QColor(*ACCENT))
    pen.setWidthF(max(1.6, size * 0.085))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawArc(ring, 35 * 16, 285 * 16)

    # Inner dot: the detail the renderer is meant to recover.
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(*ACCENT))
    dot = size * 0.10
    p.drawEllipse(QPointF(size / 2, size / 2), dot, dot)

    p.end()
    return image


def main() -> int:
    import os

    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtGui import QGuiApplication

    # QFont needs a live application object; painting runs headless here.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv[:1])  # noqa: F841 - must outlive paint()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames: list[bytes] = []
    for size in SIZES:
        image = paint(size)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QBuffer.WriteOnly)
        image.save(buffer, "PNG")
        buffer.close()
        frames.append(bytes(data))

    header = struct.pack("<HHH", 0, 1, len(SIZES))
    entry_size = 16
    offset = len(header) + entry_size * len(SIZES)
    entries = b""
    for size, blob in zip(SIZES, frames):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)

    OUT.write_bytes(header + entries + b"".join(frames))
    print(f"{OUT}  {OUT.stat().st_size / 1024:.1f} KB  ({len(SIZES)} sizes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
