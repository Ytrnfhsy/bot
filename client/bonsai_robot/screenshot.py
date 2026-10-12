"""Знімок екрана для аналізу моделлю.

На X11 і Windows вистачає Qt. На Wayland (Fedora KDE/GNOME) робот працює через XWayland і Qt бачить лише
X-вікна, тож знімаємо системними інструментами: spectacle (KDE), gnome-screenshot (GNOME), grim (wlroots).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication, QImage

MAX_SIDE = 2560  # більше моделі не треба (сервер однаково зменшує до ~1568), а файл легший


class ScreenshotError(Exception):
    pass


def _is_wayland() -> bool:
    return sys.platform.startswith("linux") and (
        bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"
    )


def _cli_commands(path: Path) -> list[list[str]]:
    """Системні інструменти у порядку переваги (лише ті, що встановлені)."""
    candidates = [
        ["spectacle", "--background", "--nonotify", "--current", "--output", str(path)],  # KDE: монітор під курсором
        ["gnome-screenshot", "--file", str(path)],
        ["grim", str(path)],
    ]
    return [c for c in candidates if shutil.which(c[0])]


def _grab_qt(area: QRect | None) -> QImage:
    screen = QGuiApplication.screenAt(area.center()) if area is not None else None
    screen = screen or QGuiApplication.primaryScreen()
    return screen.grabWindow(0).toImage()


def _mostly_black(img: QImage) -> bool:
    """XWayland без доступу до екрана віддає чорний кадр."""
    if img.isNull():
        return True
    step_x, step_y = max(1, img.width() // 24), max(1, img.height() // 24)
    for x in range(0, img.width(), step_x):
        for y in range(0, img.height(), step_y):
            c = img.pixelColor(x, y)
            if c.red() + c.green() + c.blue() > 30:
                return False
    return True


def capture(area: QRect | None = None) -> Path:
    """Знімає екран (той, де стоїть робот) і повертає шлях до PNG у тимчасовій теці."""
    out = Path(tempfile.gettempdir()) / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    img = QImage()
    errors: list[str] = []
    if _is_wayland():
        for cmd in _cli_commands(out):
            try:
                subprocess.run(cmd, check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except (OSError, subprocess.SubprocessError) as e:
                errors.append(f"{cmd[0]}: {e}")
                continue
            img = QImage(str(out))
            if not img.isNull():
                break
            errors.append(f"{cmd[0]}: порожній файл")
    if img.isNull():
        img = _grab_qt(area)
        # чорний кадр — ознака XWayland без доступу до екрана; на X11/Windows темний екран — це просто темний екран
        if img.isNull() or (_is_wayland() and _mostly_black(img)):
            hint = (
                "встанови spectacle (KDE) або gnome-screenshot: sudo dnf install spectacle"
                if _is_wayland()
                else "система не дала доступу до екрана"
            )
            raise ScreenshotError(f"Не вдалося зняти екран — {hint}" + (f" ({'; '.join(errors)})" if errors else ""))
    if max(img.width(), img.height()) > MAX_SIDE:
        img = img.scaled(MAX_SIDE, MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    if not img.save(str(out), "PNG"):
        raise ScreenshotError(f"Не вдалося записати {out}")
    return out
