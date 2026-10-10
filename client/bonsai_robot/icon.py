"""Іконка програми — той самий милий робот, намальований тим самим кодом, що й на екрані.

python -m bonsai_robot.icon ~/.local/share/icons   — згенерувати PNG у hicolor (робить інсталятор)
python -m bonsai_robot.icon --preview icons.png    — аркуш з усіма розмірами (для перевірки)
python -m bonsai_robot.icon --ico robot.ico        — іконка Windows (усі розміри в одному .ico)
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)
SMALL = 32  # до цього розміру включно — лише голова (весь робот на 16 px не читається)

# області в базових координатах робота (200×220)
FULL_BOX = QRectF(14, 0, 172, 212)  # увесь робот разом з антеною й тінню
HEAD_BOX = QRectF(24, 2, 152, 130)  # голова з антеною й «вушками»
FACE_BOX = QRectF(26, 34, 148, 96)  # лише голова, без антени — для найменших розмірів


def _posed_robot():
    from .robot import STATE_COLORS, RobotWidget

    w = RobotWidget(1.0)
    w._timer.stop()  # статичний кадр: без анімації
    w.state = "idle"
    w._color = QColor(STATE_COLORS["idle"])
    w._hover = True  # рум'янець і усмішка
    w._wave = 0.0
    w._blink = 0.0
    w._gaze.setX(0.0)
    w._gaze.setY(0.15)  # трохи дивиться вниз — на користувача
    w.t = 0.0  # без підстрибування й нахилу
    return w


def render_icon(size: int) -> QPixmap:
    box = FACE_BOX if size <= 24 else HEAD_BOX if size <= SMALL else FULL_BOX
    w = _posed_robot()
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    scale = size / max(box.width(), box.height())
    # центруємо область у квадраті
    p.translate((size - box.width() * scale) / 2, (size - box.height() * scale) / 2)
    p.scale(scale, scale)
    p.translate(-box.left(), -box.top())
    p.setClipRect(box)  # лише вибрана область — без шматків антени чи тулуба
    w.render(p, QPoint(0, 0))
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(render_icon(size))
    return icon


def install(icons_root: Path) -> None:
    for size in SIZES:
        target = icons_root / "hicolor" / f"{size}x{size}" / "apps" / "bonsai-robot.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        render_icon(size).save(str(target), "PNG")
    print(f"Іконки встановлено в {icons_root}/hicolor/*/apps/bonsai-robot.png")


def write_ico(path: Path, sizes: tuple[int, ...] = (16, 20, 24, 32, 40, 48, 64, 128, 256)) -> None:
    """Багаторозмірний .ico (кожен розмір — PNG усередині; так підтримує Windows Vista+)."""
    import struct

    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    blobs = []
    for size in sizes:
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        render_icon(size).save(buf, "PNG")
        blobs.append((size, bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, payload = b"", b""
    for size, png in blobs:
        dim = 0 if size >= 256 else size  # 0 означає 256
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(payload))
        payload += png
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + payload)
    print(f"Іконку Windows записано: {path}")


def preview(path: Path) -> None:
    pad = 16
    width = sum(SIZES[:-1]) + pad * len(SIZES)
    sheet = QPixmap(width, 256 + 2 * pad)
    sheet.fill(QColor("#E9EEF5"))
    p = QPainter(sheet)
    x = pad
    for size in SIZES[:-1]:
        p.drawPixmap(x, pad + (256 - size) // 2, render_icon(size))
        x += size + pad
    p.end()
    sheet.save(str(path))


def main(argv: list[str]) -> None:
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    if len(argv) >= 2 and argv[0] == "--preview":
        preview(Path(argv[1]))
    elif len(argv) >= 2 and argv[0] == "--ico":
        write_ico(Path(argv[1]).expanduser())
    elif argv:
        install(Path(argv[0]).expanduser())
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
