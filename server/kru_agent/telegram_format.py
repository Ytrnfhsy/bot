"""Markdown відповіді моделі → HTML, який розуміє Telegram (parse_mode=HTML).

Telegram знає лише <b> <i> <s> <u> <code> <pre> <a> <blockquote>, тож решту перетворюємо на те, що виглядає
схоже: заголовки — жирним, списки — «•», таблиці — моноширинним блоком з вирівняними стовпцями.
Усе інше екрануємо — довільний текст моделі не зламає повідомлення.
"""

from __future__ import annotations

import html
import re

_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+#.-]*)\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CHECK = re.compile(r"^\[( |x|X)\]\s+")

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])|(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def _inline(text: str) -> str:
    """Рядок без блокової розмітки: екранування + **жирний**, *курсив*, ~~закреслений~~, `код`, [посилання](url)."""
    keep: list[str] = []

    def stash(fragment: str) -> str:
        keep.append(fragment)
        return f"\x00{len(keep) - 1}\x00"

    text = _CODE_SPAN.sub(lambda m: stash(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)
    text = _LINK.sub(lambda m: stash(f'<a href="{html.escape(m.group(2))}">{_inline(m.group(1))}</a>'), text)
    text = html.escape(text, quote=False)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def _plain(text: str) -> str:
    """Комірка таблиці: без розмітки (у <pre> вона не діє)."""
    text = _LINK.sub(lambda m: m.group(1), text)
    text = re.sub(r"\*\*|__|~~|`", "", text)
    return re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", text).strip()


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [_plain(c) for c in line.split("|")]


def _table(rows: list[list[str]]) -> str:
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(cols)]
    lines = [" │ ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    lines.insert(1, "─┼─".join("─" * w for w in widths))
    return "<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>"


def to_html(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    quote: list[str] = []
    i = 0

    def flush_quote() -> None:
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    while i < len(lines):
        line = lines[i]
        if m := _FENCE.match(line):
            flush_quote()
            fence, lang = m.group(1), m.group(2)
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            i += 1  # закривна ``` (або кінець тексту)
            body = html.escape("\n".join(code), quote=False)
            attr = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{attr}>{body}</code></pre>")
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush_quote()
            rows = [_cells(line)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            out.append(_table(rows))
            continue
        if m := _QUOTE.match(line):
            quote.append(_inline(m.group(1)))
            i += 1
            continue
        flush_quote()
        if m := _HEADING.match(line):
            out.append(f"<b>{_inline(m.group(1))}</b>")
        elif _RULE.match(line):
            out.append("──────────")
        elif m := _BULLET.match(line):
            item = m.group(2)
            mark = "•"
            if c := _CHECK.match(item):
                mark, item = ("✅" if c.group(1).strip() else "☐"), item[c.end():]
            out.append(f"{'  ' * (len(m.group(1)) // 2)}{mark} {_inline(item)}")
        elif m := _NUMBERED.match(line):
            out.append(f"{'  ' * (len(m.group(1)) // 2)}{m.group(2)}. {_inline(m.group(3))}")
        else:
            out.append(_inline(line))
        i += 1
    flush_quote()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def balance_fences(parts: list[str]) -> list[str]:
    """Довгий текст ділиться на кілька повідомлень: блок коду, розрізаний між ними, закриваємо й відкриваємо знову."""
    out: list[str] = []
    carry = ""  # ```мова, якою треба продовжити наступну частину
    for part in parts:
        if carry:
            part = f"{carry}\n{part}"
        carry = ""
        opened = ""
        for line in part.split("\n"):
            if m := _FENCE.match(line):
                opened = "" if opened else f"{m.group(1)}{m.group(2)}"
        if opened:
            part += "\n" + opened[:3]
            carry = opened
        out.append(part)
    return out


def to_plain(text: str) -> str:
    """Без розмітки — для підпису до файлу (там форматування не вмикаємо)."""
    return "\n".join(_plain(line) if line.strip() else "" for line in text.split("\n")).strip()
