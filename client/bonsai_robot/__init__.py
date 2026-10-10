"""Bonsai Robot — десктоп-оболонка агента (милий робот поверх усіх вікон)."""

__version__ = "0.1.0"


def _utf8_stdio() -> None:
    """Консоль Windows часто в cp1252/cp866 — тоді print() з кирилицею падав би з UnicodeEncodeError.

    Перемикаємо вивід на UTF-8 (символи, яких консоль не вміє, замінюються, а не валять програму).
    У pythonw/зібраному .exe без консолі sys.stdout = None — тоді нічого не робимо.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_utf8_stdio()
