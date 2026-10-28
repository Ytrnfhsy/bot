"""Картки з інтервальним повторенням (система Лейтнера) — опора навички flashcards.

Колоди — JSON у робочій теці: навчання/картки/<колода>.json. Кожна картка в «коробці» 0–6: правильна
відповідь пересуває її далі (повтор через 1 / 3 / 7 / 14 / 30 / 60 днів), помилка — назад у першу
(повтор сьогодні ж). Дати рахує код, а не модель: вона лише питає й оцінює.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

from . import Tool, ToolContext, ToolError

DECKS_DIR = "навчання/картки"
INTERVALS = [0, 1, 3, 7, 14, 30, 60]  # днів до наступного повтору в коробці N
MASTERED = len(INTERVALS) - 1


def _deck_path(ctx: ToolContext, deck: str):
    deck = str(deck).strip().lower()
    if not re.fullmatch(r"[\w-]{1,60}", deck):
        raise ToolError("Назва колоди — одне слово (літери, цифри, дефіс), напр. nist-csp")
    return ctx.resolve_path(f"{DECKS_DIR}/{deck}.json")


def _load(ctx: ToolContext, deck: str) -> dict:
    p = _deck_path(ctx, deck)
    if not p.is_file():
        return {"cards": [], "next_id": 1}
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise ToolError(f"Колода {deck} пошкоджена: {e}") from e


def _save(ctx: ToolContext, deck: str, data: dict) -> None:
    p = _deck_path(ctx, deck)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)  # не лишити напівзаписану колоду


def _today(ctx: ToolContext) -> str:
    return datetime.now(ctx.tz).date().isoformat()


def _decks(ctx: ToolContext) -> list[str]:
    root = ctx.resolve_path(DECKS_DIR)
    return sorted(p.stem for p in root.glob("*.json")) if root.is_dir() else []


async def flashcards_add(ctx: ToolContext, deck: str, cards: list) -> str:
    data = _load(ctx, deck)
    known = {c["front"].strip().lower() for c in data["cards"]}
    added = 0
    for card in cards if isinstance(cards, list) else []:
        front = str((card or {}).get("front", "")).strip() if isinstance(card, dict) else ""
        back = str((card or {}).get("back", "")).strip() if isinstance(card, dict) else ""
        if not front or not back or front.lower() in known:
            continue
        data["cards"].append({"id": data["next_id"], "front": front, "back": back, "box": 0,
                              "due": _today(ctx), "right": 0, "wrong": 0})
        data["next_id"] += 1
        known.add(front.lower())
        added += 1
    if not added:
        raise ToolError("Жодної нової картки: потрібні front і back, і такого питання ще не має бути в колоді")
    _save(ctx, deck, data)
    return f"Додано {added} карток у колоду «{deck}» (усього {len(data['cards'])}); перші повторення — сьогодні"


async def flashcards_due(ctx: ToolContext, deck: str = "", limit: int = 10) -> str:
    today = _today(ctx)
    decks = [deck] if deck else _decks(ctx)
    due = []
    for d in decks:
        for c in _load(ctx, d)["cards"]:
            if c["due"] <= today:
                due.append((c["box"], c["due"], d, c))
    if not due:
        return "Сьогодні повторювати нічого — усі картки вивчені на сьогодні"
    due.sort(key=lambda x: (x[0], x[1]))  # спершу ті, що в нижчих коробках (гірше відомі)
    lines = [f"До повторення: {len(due)} (показую {min(len(due), int(limit))}). Питай по одній; відповідь "
             "НЕ показуй, доки користувач не відповість:"]
    for _, _, d, c in due[: max(1, int(limit))]:
        lines.append(f"- [{d} #{c['id']}, коробка {c['box']}] Питання: {c['front']}\n  Відповідь: {c['back']}")
    return "\n".join(lines)


async def flashcards_grade(ctx: ToolContext, deck: str, card_id: int, correct: bool) -> str:
    data = _load(ctx, deck)
    card = next((c for c in data["cards"] if c["id"] == int(card_id)), None)
    if card is None:
        raise ToolError(f"У колоді «{deck}» немає картки #{card_id}")
    if correct:
        card["box"] = min(MASTERED, card["box"] + 1)
        card["right"] += 1
    else:
        card["box"] = 0
        card["wrong"] += 1
    days = INTERVALS[card["box"]] if correct else 0
    card["due"] = (datetime.now(ctx.tz).date() + timedelta(days=max(days, 1 if correct else 0))).isoformat()
    card["seen"] = int(time.time())
    _save(ctx, deck, data)
    return f"#{card_id}: {'✓' if correct else '✗'}, коробка {card['box']}, наступний повтор {card['due']}"


async def flashcards_stats(ctx: ToolContext, deck: str = "") -> str:
    today = _today(ctx)
    rows = []
    for d in [deck] if deck else _decks(ctx):
        cards = _load(ctx, d)["cards"]
        if not cards:
            continue
        due = sum(c["due"] <= today for c in cards)
        learned = sum(c["box"] >= 4 for c in cards)
        hard = sorted(cards, key=lambda c: c["wrong"] - c["right"], reverse=True)[:3]
        hard_text = "; ".join(c["front"][:50] for c in hard if c["wrong"])
        rows.append(f"«{d}»: {len(cards)} карток, на сьогодні {due}, засвоєно {learned}"
                    + (f"; найважчі: {hard_text}" if hard_text else ""))
    return "\n".join(rows) or "Колод ще немає"


_DECK = {"type": "string", "description": "назва колоди, напр. nist-csp"}

TOOLS = [
    Tool(
        name="flashcards_add",
        description="Додає картки в колоду (інтервальне повторення). Питання — коротке й однозначне, відповідь — "
                    "стисла (термін, визначення, перелік).",
        parameters={"type": "object", "properties": {
            "deck": _DECK,
            "cards": {"type": "array", "items": {"type": "object", "properties": {
                "front": {"type": "string", "description": "питання"},
                "back": {"type": "string", "description": "правильна відповідь"}}, "required": ["front", "back"]}},
        }, "required": ["deck", "cards"]},
        func=flashcards_add,
    ),
    Tool(
        name="flashcards_due",
        description="Картки, які пора повторити сьогодні (без deck — з усіх колод), разом з відповідями для перевірки.",
        parameters={"type": "object", "properties": {"deck": _DECK, "limit": {"type": "integer"}}},
        func=flashcards_due,
    ),
    Tool(
        name="flashcards_grade",
        description="Записує результат відповіді на картку: correct=true — далі за розкладом, false — повторити сьогодні.",
        parameters={"type": "object", "properties": {
            "deck": _DECK, "card_id": {"type": "integer"}, "correct": {"type": "boolean"},
        }, "required": ["deck", "card_id", "correct"]},
        func=flashcards_grade,
    ),
    Tool(
        name="flashcards_stats",
        description="Скільки карток у колодах, скільки на сьогодні, що засвоєно, які найважчі.",
        parameters={"type": "object", "properties": {"deck": _DECK}},
        func=flashcards_stats,
    ),
]
