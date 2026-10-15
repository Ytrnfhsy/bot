"""Кольорові емодзі в меню й чаті (🔧 💬 📋 …).

Qt бере їх із системного шрифту емодзі; якщо його немає (або Qt не вміє його формат), значки малюються
порожніми чи квадратиками. Тому інсталятор кладе Noto Color Emoji у теку програми, а тут він
підключається явно як шрифт для емодзі. На Windows є Segoe UI Emoji — нічого робити не треба.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FONT_FILE = "NotoColorEmoji.ttf"
FONT_URL = "https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf"


def font_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kru-robot" / "fonts"


def setup_emoji_font(app) -> str | None:
    """Підключити шрифт емодзі; повертає його назву або None, якщо підходящого немає."""
    if sys.platform == "win32":
        return "Segoe UI Emoji"
    from PySide6.QtGui import QFontDatabase

    families: list[str] = []
    bundled = font_dir() / FONT_FILE
    if bundled.is_file():
        fid = QFontDatabase.addApplicationFont(str(bundled))
        if fid >= 0:
            families = list(QFontDatabase.applicationFontFamilies(fid))
    if not families:  # системний (google-noto-color-emoji-fonts тощо)
        families = [f for f in QFontDatabase.families() if "emoji" in f.lower()]
        families.sort(key=lambda f: ("color" not in f.lower(), f))  # кольоровий — першим
    if not families:
        return None
    family = families[0]
    if hasattr(QFontDatabase, "setApplicationEmojiFontFamilies"):  # Qt ≥ 6.9: лише для емодзі
        QFontDatabase.setApplicationEmojiFontFamilies([family])
        return family
    # старіший Qt: запасний шрифт усього інтерфейсу. Першим — справжня назва основного шрифту, а не
    # псевдонім на кшталт «Sans Serif»: інакше цифри й пробіли малювалися б шрифтом емодзі
    from PySide6.QtGui import QFontInfo

    font = app.font()
    font.setFamilies([QFontInfo(font).family(), family])
    app.setFont(font)
    return family
