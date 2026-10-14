"""Часовий пояс користувача: модель бачить і планує час саме в ньому, а не в часі сервера (часто UTC).

Пояс приходить від клієнта (робот передає системний пояс ПК, напр. Europe/Kyiv) і запам'ятовується
за користувачем; запасні варіанти — KRU_TIMEZONE і пояс самого сервера.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover — Python без zoneinfo
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]


def get_tz(name: str | None) -> tzinfo | None:
    """IANA-пояс («Europe/Kyiv») або None, якщо назва порожня чи невідома."""
    name = (name or "").strip()
    if not name or ZoneInfo is None or len(name) > 64:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def server_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo


def describe_tz(tz: tzinfo, name: str = "") -> str:
    """«Europe/Kyiv, UTC+03:00» — для системного промпту й відповідей."""
    offset = datetime.now(tz).strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    return f"{name}, {offset}" if name else offset


def local_tz_name() -> str:
    """IANA-назва поясу цієї машини (для CLI): з TZ або з посилання /etc/localtime; '' — не вдалося."""
    import os
    from pathlib import Path

    name = os.environ.get("TZ", "").lstrip(":")
    if get_tz(name):
        return name
    try:
        target = str(Path("/etc/localtime").resolve())
    except OSError:
        return ""
    if "zoneinfo/" in target:
        name = target.split("zoneinfo/", 1)[1]
        return name if get_tz(name) else ""
    return ""
