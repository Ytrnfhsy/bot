"""Підготовка зображень для моделі: пошук посилань у тексті, зменшення, base64."""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_SIDE = 1568  # довша сторона після зменшення: деталі видно, а токенів — помірно
MAX_BYTES = 20 * 1024 * 1024

_RAW_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_REF_RE = re.compile(r"[^\s\"'«»()\[\]<>,;]+\.(?:png|jpe?g|webp|gif|bmp)\b", re.IGNORECASE)


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXT


def find_image_refs(text: str) -> list[str]:
    """Шляхи до картинок, згадані в тексті задачі (напр. «що на uploads/скрін.png?»)."""
    seen: list[str] = []
    for m in _REF_RE.finditer(text):
        ref = m.group(0).rstrip(".:!?")
        if ref not in seen:
            seen.append(ref)
    return seen


def load_image(path: Path) -> dict[str, Any]:
    """{"mime", "data"(base64)}; велике зображення зменшується, рідкісні формати → PNG."""
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError(f"зображення завелике ({len(data) // 1024 // 1024} МБ)")
    try:
        from PIL import Image
    except ImportError:
        mime = _RAW_MIME.get(path.suffix.lower())
        if not mime:
            raise ValueError("для цього формату потрібен Pillow (pip install pillow)") from None
        return {"mime": mime, "data": base64.b64encode(data).decode("ascii")}

    with Image.open(io.BytesIO(data)) as im:
        im.seek(0)  # перший кадр gif
        if max(im.size) > MAX_SIDE:
            im.thumbnail((MAX_SIDE, MAX_SIDE))
        has_alpha = im.mode in {"RGBA", "LA", "P"}
        im = im.convert("RGBA" if has_alpha else "RGB")
        buf = io.BytesIO()
        if has_alpha:
            im.save(buf, "PNG", optimize=True)
            mime = "image/png"
        else:
            im.save(buf, "JPEG", quality=90)
            mime = "image/jpeg"
    return {"mime": mime, "data": base64.b64encode(buf.getvalue()).decode("ascii")}
