"""Telegram: надсилання повідомлень користувачу через його бота (@BotFather → KRU_TELEGRAM_BOT_TOKEN).

Прив'язка чату: `kru telegram link` — користувач пише боту будь-що, і його chat_id зберігається.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .telegram_format import balance_fences, to_html

log = logging.getLogger("kru.telegram")

API = "https://api.telegram.org"
LIMIT = 4000  # Telegram приймає до 4096 символів в одному повідомленні
MD_LIMIT = 3500  # з розміткою: теги й екранування додають символів
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_MAX = 10 * 1024 * 1024  # більші фото Telegram не приймає — тоді документом


class TelegramError(Exception):
    pass


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Ділить довгий текст на частини — по абзацах/рядках, а не посеред слова."""
    text = text.strip()
    parts: list[str] = []
    while len(text) > limit:
        # спершу межа абзацу, потім рядка, потім слова — перша, що не надто близько до початку
        cut = next((c for sep in ("\n\n", "\n", " ") if (c := text.rfind(sep, 0, limit)) >= limit // 2), limit)
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n ") if text[cut:cut + 1] == " " else text[cut:].lstrip("\n")  # відступ коду лишаємо
    if text:
        parts.append(text)
    return parts


def keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


class TelegramBot:
    def __init__(self, token: str, timeout: float = 30) -> None:
        if not token:
            raise TelegramError(
                "Telegram не налаштовано: створи бота в @BotFather і додай KRU_TELEGRAM_BOT_TOKEN у ~/.config/kru.env, "
                "потім на сервері: kru telegram link"
            )
        self.base = f"{API}/bot{token}"
        self.timeout = timeout  # для getUpdates із long polling додається його власний timeout

    async def _call(self, method: str, **params: Any) -> Any:
        try:
            wait = self.timeout + float(params.get("timeout") or 0) if method == "getUpdates" else self.timeout
            async with httpx.AsyncClient(timeout=wait) as client:
                r = await client.post(f"{self.base}/{method}", json=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"Telegram недоступний: {e}") from e
        if not data.get("ok"):
            raise TelegramError(f"Telegram: {data.get('description') or r.status_code}")
        return data.get("result")

    async def send(self, chat_id: str, text: str, buttons: list[list[tuple[str, str]]] | None = None,
                   markdown: bool = False) -> int:
        """Текст (довгий — кількома повідомленнями). buttons — рядки кнопок [(напис, callback_data)] під останнім.
        markdown — текст моделі: розмітка перетворюється на HTML Telegram (якщо він його не прийме — простим текстом).
        Повертає message_id останнього повідомлення."""
        parts = split_message(text, MD_LIMIT if markdown else LIMIT)
        if not parts:
            raise TelegramError("Порожнє повідомлення")
        if markdown:
            parts = balance_fences(parts)
        msg_id = 0
        for i, part in enumerate(parts):
            params: dict[str, Any] = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if buttons and i == len(parts) - 1:
                params["reply_markup"] = keyboard(buttons)
            res = None
            try:
                formatted = to_html(part) if markdown else ""
            except Exception:  # noqa: BLE001 — помилка перетворення не має з'їсти відповідь
                log.exception("Не вдалося перетворити розмітку для Telegram")
                formatted = ""
            # Telegram рахує довжину в UTF-16 (емодзі — по 2)
            if formatted and len(formatted.encode("utf-16-le")) // 2 <= 4096:
                try:
                    res = await self._call("sendMessage", **{**params, "text": formatted, "parse_mode": "HTML"})
                except TelegramError as e:
                    if "entit" not in str(e).lower() and "parse" not in str(e).lower():
                        raise  # мережа, заблокований бот тощо — простий текст не допоможе
            if res is None:  # без parse_mode: довільний текст не зламає розмітку Telegram
                res = await self._call("sendMessage", **params)
            msg_id = int((res or {}).get("message_id") or 0)
        return msg_id

    async def send_file(self, chat_id: str, path, caption: str = "") -> None:
        """Картинку — фото (видно одразу в чаті), решту чи завелике фото — документом."""
        from pathlib import Path

        path = Path(path)
        if path.suffix.lower() in PHOTO_EXT and path.stat().st_size <= PHOTO_MAX:
            try:
                await self.send_document(chat_id, path, caption, kind="photo")
                return
            except TelegramError:
                pass  # Telegram не прийняв як фото (розміри, формат) — надішлемо файлом
        await self.send_document(chat_id, path, caption)

    async def send_document(self, chat_id: str, path, caption: str = "", kind: str = "document") -> None:
        """Файл документом (до 50 МБ — обмеження Telegram для ботів). Підпис — до 1024 символів."""
        from pathlib import Path

        path = Path(path)
        method = "sendPhoto" if kind == "photo" else "sendDocument"
        try:
            async with httpx.AsyncClient(timeout=max(self.timeout, 300)) as client:
                with path.open("rb") as f:
                    r = await client.post(f"{self.base}/{method}",
                                          data={"chat_id": str(chat_id), "caption": caption[:1024]},
                                          files={kind: (path.name, f)})
            data = r.json()
        except (httpx.HTTPError, ValueError, OSError) as e:
            raise TelegramError(f"Telegram недоступний: {e}") from e
        if not data.get("ok"):
            raise TelegramError(f"Telegram: {data.get('description') or r.status_code}")

    async def edit(self, chat_id: str, message_id: int, text: str) -> None:
        """Змінити текст повідомлення (і прибрати кнопки)."""
        await self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:LIMIT],
                         disable_web_page_preview=True)

    async def delete(self, chat_id: str, message_id: int) -> None:
        await self._call("deleteMessage", chat_id=chat_id, message_id=message_id)

    async def typing(self, chat_id: str) -> None:
        await self._call("sendChatAction", chat_id=chat_id, action="typing")

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def download(self, file_id: str, max_bytes: int = 20 * 1024 * 1024) -> tuple[bytes, str]:
        """Файл, надісланий боту: (вміст, шлях у Telegram — з нього видно розширення)."""
        info = await self._call("getFile", file_id=file_id)
        path = (info or {}).get("file_path") or ""
        if not path:
            raise TelegramError("Telegram не віддав файл (буває для файлів понад 20 МБ)")
        if int((info or {}).get("file_size") or 0) > max_bytes:
            raise TelegramError("файл завеликий (понад 20 МБ)")
        url = self.base.replace("/bot", "/file/bot", 1) + "/" + path
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise TelegramError(f"не вдалося завантажити файл: {e}") from e
        return r.content, path

    async def me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return await self._call("getUpdates", **params)
