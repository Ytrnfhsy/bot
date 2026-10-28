"""Робота з файлами у робочій директорії агента."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re

from . import Tool, ToolContext, ToolError, preview, truncate

log = logging.getLogger("kru.files")

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic"}


def _office_to_text(p) -> str:
    """Текст із .docx / .odt без сторонніх бібліотек (обидва — zip з XML)."""
    import xml.etree.ElementTree as ET
    import zipfile

    inner = "word/document.xml" if p.suffix.lower() == ".docx" else "content.xml"
    try:
        with zipfile.ZipFile(p) as z:
            root = ET.fromstring(z.read(inner))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise ToolError(f"Не вдалося розібрати документ: {e}") from e
    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    paragraphs = []
    for el in root.iter():
        if local(el.tag) in {"p", "h"}:  # абзаци docx (w:p) та odt (text:p / text:h)
            if inner.startswith("word/"):
                paragraphs.append("".join(t.text or "" for t in el.iter() if local(t.tag) == "t"))
            else:
                paragraphs.append("".join(el.itertext()))
    return "\n".join(paragraphs)


def _slides_to_text(p) -> str:
    """Текст презентації .pptx / .odp по слайдах, з нотатками доповідача (у курсах там часто головне)."""
    import xml.etree.ElementTree as ET
    import zipfile

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def paragraphs(xml: bytes, para: set[str], run: str | None) -> list[str]:
        out = []
        for el in ET.fromstring(xml).iter():
            if local(el.tag) in para:
                text = "".join(t.text or "" for t in el.iter() if local(t.tag) == run) if run else "".join(el.itertext())
                if text.strip():
                    out.append(text.strip())
        return out

    try:
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            if p.suffix.lower() == ".odp":
                pages = [el for el in ET.fromstring(z.read("content.xml")).iter() if local(el.tag) == "page"]
                return "\n\n".join(f"=== Слайд {i} ===\n" + "\n".join(
                    t for t in ("".join(e.itertext()).strip() for e in page.iter() if local(e.tag) in {"p", "h"}) if t)
                    for i, page in enumerate(pages, 1))
            num = lambda n: int(re.search(r"(\d+)\.xml$", n).group(1))  # noqa: E731 — slide10 після slide9
            slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=num)
            out = []
            for n in slides:
                text = paragraphs(z.read(n), {"p"}, "t")
                note_file = f"ppt/notesSlides/notesSlide{num(n)}.xml"
                notes = paragraphs(z.read(note_file), {"p"}, "t") if note_file in names else []
                notes = [t for t in notes if not t.isdigit()]  # номер слайда в нотатках
                out.append(f"=== Слайд {num(n)} ===\n" + "\n".join(text)
                           + (("\nНотатки доповідача: " + " ".join(notes)) if notes else ""))
            return "\n\n".join(out)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise ToolError(f"Не вдалося розібрати презентацію: {e}") from e


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".kru_tmp", ".kru-backup"}


async def read_file(ctx: ToolContext, path: str, start_line: int = 1, max_lines: int = 400) -> str:
    p = ctx.resolve_path(path)
    if not p.is_file():
        raise ToolError(f"Файл не знайдено: {path}")
    if p.stat().st_size > 20 * 1024 * 1024:
        raise ToolError("Файл завеликий (>20 МБ)")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        from .web import _pdf_to_text

        text = await asyncio.to_thread(_pdf_to_text, p.read_bytes())
    elif suffix in {".docx", ".odt"}:
        text = await asyncio.to_thread(_office_to_text, p)
    elif suffix in {".pptx", ".odp"}:
        text = await asyncio.to_thread(_slides_to_text, p)
    else:
        data = p.read_bytes()
        if b"\x00" in data[:4096]:
            if suffix in _IMAGE_EXT:
                raise ToolError("Це зображення — щоб його побачити, використай view_image")
            raise ToolError("Це бінарний файл — прочитати його як текст неможливо")
        text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    start = max(1, int(start_line))
    end = min(len(lines), start - 1 + max(1, int(max_lines)))
    body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))
    more = f"\n…ще {len(lines) - end} рядків (start_line={end + 1})" if end < len(lines) else ""
    return truncate(f"{p} (рядки {start}-{end} з {len(lines)})\n{body}{more}", ctx.config.max_tool_output_chars)


BACKUP_DIR = ".kru-backup"
BACKUP_DAYS = 7
BACKUP_MAX_BYTES = 20 * 1024 * 1024


def backup(ctx: ToolContext, p) -> str:
    """Копія файлу перед перезаписом/зміною: <тека>/.kru-backup/<дата-час>/<шлях>. Старші за 7 днів прибираються."""
    import shutil
    import time
    from pathlib import Path

    if not p.is_file() or p.stat().st_size > BACKUP_MAX_BYTES:
        return ""
    ws = Path(ctx.workspace).resolve()
    root = ws / BACKUP_DIR
    if root.is_symlink() or (root.exists() and not root.resolve().is_relative_to(ws)):
        # .kru-backup підмінили посиланням назовні (shell у пісочниці бачить робочу теку): копії й прибирання
        # старих тек пішли б туди — краще без копії
        log.warning("%s — символьне посилання, резервні копії вимкнено", root)
        return ""
    try:
        rel = p.resolve().relative_to(ws)
    except ValueError:  # файл поза робочою текою (лише для адміна з дозволом)
        rel = Path("_outside", *p.resolve().parts[1:])
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
    target = root / stamp / rel
    n = 1
    while target.exists():  # кілька змін того самого файлу за мить — жодна копія не затирається
        n += 1
        target = root / f"{stamp}-{n}" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, target)
    cutoff = time.time() - BACKUP_DAYS * 86400
    for old in root.iterdir():
        if not old.is_symlink() and old.is_dir() and old.stat().st_mtime < cutoff:
            shutil.rmtree(old, ignore_errors=True)
    return target.relative_to(ws).as_posix()


async def write_file(ctx: ToolContext, path: str, content: str, append: bool = False) -> str:
    p = ctx.resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    saved = "" if append else await asyncio.to_thread(backup, ctx, p)
    with open(p, "a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    note = f" (попередня версія — {saved})" if saved else ""
    return f"{'Дописано' if append else 'Записано'} {len(content)} символів у {p}{note}"


async def edit_file(ctx: ToolContext, path: str, old_text: str, new_text: str) -> str:
    p = ctx.resolve_path(path)
    if not p.is_file():
        raise ToolError(f"Файл не знайдено: {path}")
    text = p.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        raise ToolError("old_text не знайдено у файлі — спершу прочитай файл через read_file")
    if count > 1:
        raise ToolError(f"old_text зустрічається {count} разів — додай більше контексту, щоб він був унікальним")
    saved = await asyncio.to_thread(backup, ctx, p)
    p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"Файл {p} змінено" + (f" (попередня версія — {saved})" if saved else "")


async def list_dir(ctx: ToolContext, path: str = ".", recursive: bool = False, pattern: str = "*") -> str:
    root = ctx.resolve_path(path)
    if not root.is_dir():
        raise ToolError(f"Директорію не знайдено: {path}")
    # обхід великої теки — у потоці: інакше на весь час обходу завмирав би весь сервер (робот, Telegram, інші)
    return await asyncio.to_thread(_list_dir, ctx, root, recursive, pattern)


LIST_MAX_DIRS = 20000  # рекурсивний обхід зупиняється тут, навіть якщо збігів мало (тека «/» чи ~)


def _list_dir(ctx: ToolContext, root, recursive: bool, pattern: str) -> str:
    entries: list[str] = []
    if recursive:
        dirs = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirs += 1
            if dirs > LIST_MAX_DIRS:
                entries.append(f"… (переглянуто {LIST_MAX_DIRS} тек — звузь path чи pattern)")
                break
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            rel = os.path.relpath(dirpath, root)
            for name in sorted(filenames):
                if fnmatch.fnmatch(name, pattern):
                    entries.append(os.path.normpath(os.path.join(rel, name)))
            if len(entries) > 2000:
                break
    else:
        for e in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if fnmatch.fnmatch(e.name, pattern):
                entries.append(e.name + "/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} B)")
    if not entries:
        return f"{root}: порожньо"
    return truncate(f"{root}:\n" + "\n".join(entries), ctx.config.max_tool_output_chars)


def _grep(ctx: ToolContext, regex: str, path: str, glob: str) -> str:
    root = ctx.resolve_path(path)
    try:
        rx = re.compile(regex, re.IGNORECASE)
    except re.error as e:
        raise ToolError(f"Невірний regex: {e}") from e
    hits: list[str] = []
    files = [root] if root.is_file() else None
    if files is None:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            files.extend(os.path.join(dirpath, n) for n in filenames if fnmatch.fnmatch(n, glob))
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="strict") as fh:
                for no, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(f, ctx.workspace)}:{no}: {line.rstrip()[:300]}")
                        if len(hits) >= 300:
                            break
        except (UnicodeDecodeError, OSError):
            continue
        if len(hits) >= 300:
            break
    return "\n".join(hits) if hits else "Збігів не знайдено"


async def search_files(ctx: ToolContext, regex: str, path: str = ".", glob: str = "*") -> str:
    out = await asyncio.to_thread(_grep, ctx, regex, path, glob)
    return truncate(out, ctx.config.max_tool_output_chars)


MAX_SHARE = 50 * 1024 * 1024  # більше Telegram однаково не прийме, а панелі важко передати одним повідомленням


def human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ"):
        if n < 1024 or unit == "МБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f}".removesuffix(".0").replace(".", ",") + f" {unit}"
        n /= 1024
    return f"{n} Б"


def shared_file(ctx: ToolContext, path: str):
    """Файл, який можна віддати користувачу: (повний шлях, шлях для показу — відносно робочої теки)."""
    p = ctx.resolve_path(path)
    if p.is_dir():
        raise ToolError(f"{path} — це тека. Запакуй її в zip (run_python: shutil.make_archive) і передай архів")
    if not p.is_file():
        raise ToolError(f"Файл не знайдено: {path}")
    if p.stat().st_size > MAX_SHARE:
        raise ToolError(f"Файл завеликий ({human_size(p.stat().st_size)}); можна передати до 50 МБ — стисни чи поділи його")
    ws = ctx.workspace.resolve()
    return p, (p.relative_to(ws).as_posix() if ws in p.parents else str(p))


async def send_file(ctx: ToolContext, path: str) -> str:
    p, shown = shared_file(ctx, path)
    size = p.stat().st_size
    if ctx.event is None:
        return f"Файл {shown} лежить на сервері ({human_size(size)}); кнопки завантаження тут немає — назви шлях."
    await ctx.event({"type": "file", "path": shown, "name": p.name, "size": size})
    return (f"Користувач отримав кнопку завантаження файлу {p.name} ({human_size(size)}). "
            "Не переписуй його вміст у відповідь — достатньо коротко сказати, що в ньому.")


_PATH = {"type": "string", "description": "Шлях (відносно робочої директорії)"}

TOOLS = [
    Tool(
        name="read_file",
        description=(
            "Читає файл як текст (з номерами рядків): звичайні текстові файли, а також PDF, DOCX, ODT і презентації "
            "PPTX/ODP (по слайдах, з нотатками доповідача). "
            "Для великих файлів використовуй start_line. Файли, які користувач передав, лежать в uploads/."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH,
                "start_line": {"type": "integer", "description": "Перший рядок (з 1)"},
                "max_lines": {"type": "integer", "description": "Скільки рядків читати"},
            },
            "required": ["path"],
        },
        func=read_file,
    ),
    Tool(
        name="write_file",
        description="Створює або перезаписує файл (або дописує при append=true). Потребує підтвердження користувача.",
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH,
                "content": {"type": "string", "description": "Повний вміст файлу"},
                "append": {"type": "boolean", "description": "Дописати в кінець замість перезапису"},
            },
            "required": ["path", "content"],
        },
        func=write_file,
        dangerous=True,
        summarize=lambda a: f"{'Дописати у' if a.get('append') else 'Записати'} файл {a.get('path')} "
        f"({len(str(a.get('content', '')))} символів):\n{preview(a.get('content', ''))}",
    ),
    Tool(
        name="edit_file",
        description="Замінює унікальний фрагмент old_text на new_text у файлі. Потребує підтвердження.",
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH,
                "old_text": {"type": "string", "description": "Точний фрагмент, який треба замінити"},
                "new_text": {"type": "string", "description": "Новий текст"},
            },
            "required": ["path", "old_text", "new_text"],
        },
        func=edit_file,
        dangerous=True,
        summarize=lambda a: f"Змінити файл {a.get('path')}:\n- {preview(a.get('old_text'), 8000)}\n+ {preview(a.get('new_text'), 8000)}",
    ),
    Tool(
        name="list_dir",
        description="Показує вміст директорії (recursive=true — рекурсивно, pattern — фільтр на кшталт '*.py').",
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH,
                "recursive": {"type": "boolean"},
                "pattern": {"type": "string", "description": "glob-фільтр імен файлів"},
            },
        },
        func=list_dir,
    ),
    Tool(
        name="search_files",
        description="Шукає regex у файлах (як grep). glob обмежує типи файлів, напр. '*.md'.",
        parameters={
            "type": "object",
            "properties": {
                "regex": {"type": "string", "description": "Регулярний вираз (без урахування регістру)"},
                "path": _PATH,
                "glob": {"type": "string"},
            },
            "required": ["regex"],
        },
        func=search_files,
    ),
    Tool(
        name="send_file",
        description=(
            "Передає файл з робочої теки користувачу: у чаті з'являється кнопка, що одним кліком зберігає його "
            "на ПК (у Telegram — надсилається документом). Для готових звітів, документів, таблиць, картинок, "
            "архівів. Теку спершу запакуй у zip. До 50 МБ."
        ),
        parameters={"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
        func=send_file,
    ),
]
