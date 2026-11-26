"""Версія релізу (з тегу v0.2.0) → у pyproject.toml і __init__.py сервера й робота.

    python .github/set_version.py 0.2.0
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "server/pyproject.toml": r'(?m)^version = "[^"]*"',
    "client/pyproject.toml": r'(?m)^version = "[^"]*"',
    "server/kru_agent/__init__.py": r'(?m)^__version__ = "[^"]*"',
    "client/kru_robot/__init__.py": r'(?m)^__version__ = "[^"]*"',
}


def main() -> None:
    version = sys.argv[1].removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+([.-]?[0-9A-Za-z.]+)?", version):
        raise SystemExit(f"Дивна версія: {version!r} (очікую 0.2.0 чи 0.2.0-rc1)")
    for rel, pattern in FILES.items():
        path = ROOT / rel
        text = path.read_text("utf-8")
        new, n = re.subn(pattern, lambda m: m.group(0).split("=")[0] + f'= "{version}"', text, count=1)
        if n != 1:
            raise SystemExit(f"У {rel} не знайшов рядка з версією")
        path.write_text(new, "utf-8")
        print(f"{rel}: {version}")


if __name__ == "__main__":
    main()
