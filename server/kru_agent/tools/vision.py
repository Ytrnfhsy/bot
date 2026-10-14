"""Інструмент «подивитися на зображення»."""

from __future__ import annotations

from ..images import IMAGE_EXT, is_image
from . import Tool, ToolContext, ToolError


async def view_image(ctx: ToolContext, path: str, detail: str = "normal") -> str:
    if not ctx.vision_ok:
        raise ToolError("Поточна модель не вміє працювати із зображеннями (зір вимкнено)")
    p = ctx.resolve_path(path)
    if not p.is_file():
        raise ToolError(f"Файл не знайдено: {path}")
    if not is_image(p):
        raise ToolError(f"Це не зображення. Підтримуються: {', '.join(sorted(IMAGE_EXT))}")
    ws = ctx.workspace.resolve()
    rel = p.relative_to(ws).as_posix() if ws in p.parents else str(p)
    ctx.pending_images.append(rel)
    if str(detail).lower() == "high":
        ctx.pending_high = True
    return f"Зображення {rel} додано до розмови наступним повідомленням — роздивись його й продовжуй задачу."


TOOLS = [
    Tool(
        name="view_image",
        description=(
            "Дає тобі подивитися на зображення з workspace (png, jpg, webp, gif, bmp): скриншот, фото, графік, схему. "
            "Файли, які передав користувач, лежать в uploads/. Картинки, згадані в задачі, ти бачиш і без цього інструмента. "
            "Картинки зменшуються до ~1 Мп; якщо не можеш розібрати дрібний текст — подивись ще раз з detail=\"high\"."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Шлях до зображення (відносно робочої директорії)"},
                "detail": {"type": "string", "enum": ["normal", "high"],
                           "description": "high — більша роздільність (дрібний текст), повільніше"},
            },
            "required": ["path"],
        },
        func=view_image,
    ),
]
