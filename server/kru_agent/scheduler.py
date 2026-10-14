"""Заплановані завдання: агент виконує задачу в заданий час (одноразово або з повтором).

Час — локальний час сервера. Кожен запуск — окремий чат «⏰ …»; про результат персонаж показує сповіщення.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from .timezones import get_tz

log = logging.getLogger("kru.scheduler")

REPEATS = {
    "": "одноразово",
    "hourly": "щогодини",
    "daily": "щодня",
    "weekdays": "по буднях",
    "weekly": "щотижня",
}
MISSED_GRACE = 6 * 3600  # пропущене (сервер був вимкнений) виконуємо, якщо запізнення не більше 6 год
_REL = re.compile(r"^\+?\s*(\d+)\s*(m|min|хв|h|год|d|д)\w*$", re.IGNORECASE)


def parse_when(when: str, now: datetime | None = None) -> datetime:
    """Локальний час: «2026-10-14 18:00», «2026-10-14T18:00», «18:00» (сьогодні/завтра), «+30m», «+2h», «+1d»."""
    now = now or datetime.now()
    text = str(when).strip()
    if m := _REL.match(text):
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}
        key = "m" if unit in {"m", "min", "хв"} else "h" if unit in {"h", "год"} else "d"
        return now + delta[key]
    if m := re.fullmatch(r"(\d{1,2}):(\d{2})", text):
        target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return target if target > now else target + timedelta(days=1)
    try:
        dt = datetime.fromisoformat(text.replace(" ", "T", 1) if "T" not in text else text)
    except ValueError as e:
        raise ValueError(f"Не розумію час {when!r}. Формат: 2026-10-14 18:00, 18:00 або +30m / +2h / +1d") from e
    if dt.tzinfo is not None:  # у локальний час сервера
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def next_after(ts: float, repeat: str, now: float | None = None, tz=None) -> float | None:
    """Наступний запуск після ts (і після now), None — одноразове завдання.

    tz — пояс завдання: «щодня о 8:00» лишається 8:00 за місцевим часом і після переходу на літній/зимовий.
    """
    if not repeat:
        return None
    now = time.time() if now is None else now
    dt = datetime.fromtimestamp(ts, tz)
    while True:
        if repeat == "hourly":
            dt += timedelta(hours=1)
        elif repeat == "weekly":
            dt += timedelta(days=7)
        else:
            dt += timedelta(days=1)
            if repeat == "weekdays":
                while dt.weekday() >= 5:
                    dt += timedelta(days=1)
        if dt.timestamp() > now:
            return dt.timestamp()


def describe(item: dict[str, Any]) -> str:
    tz = get_tz(item.get("tz"))
    when = datetime.fromtimestamp(item["next_run"], tz).strftime("%Y-%m-%d %H:%M (%a)")
    repeat = REPEATS.get(item.get("repeat", ""), item.get("repeat", ""))
    last = ""
    if item.get("last_run"):
        last = f"; востаннє {datetime.fromtimestamp(item['last_run'], tz):%d.%m %H:%M} — {item.get('last_status') or '?'}"
    return f"#{item['id']} · {when} · {repeat} · {item['task']}{last}"


RunFunc = Callable[[dict[str, Any], str], Awaitable[str]]  # (завдання, id чату) → відповідь


class Scheduler:
    """Раз на кілька секунд перевіряє розклад і запускає завдання, коли їх власник вільний."""

    def __init__(self, storage, run: RunFunc, is_busy: Callable[[str], bool], interval: float = 15) -> None:
        self.storage = storage
        self.run = run
        self.is_busy = is_busy
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._running: set[int] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Помилка планувальника")
            await asyncio.sleep(self.interval)

    async def tick(self, now: float | None = None) -> list[asyncio.Task]:
        """Запускає всі завдання, час яких настав. Повертає створені asyncio-задачі (для тестів)."""
        now = time.time() if now is None else now
        started = []
        for item in await asyncio.to_thread(self.storage.due_schedules, now):
            if item["id"] in self._running or self.is_busy(item["owner"]):
                continue  # власник зараз зайнятий іншою задачею — запустимо на наступному колі
            late = now - item["next_run"]
            if late > MISSED_GRACE:
                nxt = next_after(item["next_run"], item["repeat"], now, get_tz(item.get("tz")))
                log.info("Завдання #%s пропущено (запізнення %.0f год)", item["id"], late / 3600)
                self.storage.update_schedule(item["id"], next_run=nxt or item["next_run"], enabled=1 if nxt else 0,
                                             last_status="пропущено (сервер був вимкнений)")
                continue
            self._running.add(item["id"])
            started.append(asyncio.create_task(self._run_one(item, now)))
        return started

    async def _run_one(self, item: dict[str, Any], now: float) -> None:
        chat_id = f"sched-{item['id']}-{uuid.uuid4().hex[:6]}"
        nxt = next_after(item["next_run"], item["repeat"], now, get_tz(item.get("tz")))
        # наступний запуск фіксуємо одразу — навіть якщо виконання впаде, завдання не зациклиться
        self.storage.update_schedule(item["id"], next_run=nxt or item["next_run"], enabled=1 if nxt else 0,
                                     last_run=now, last_status="виконується", last_chat=chat_id)
        try:
            log.info("Запускаю заплановане завдання #%s: %s", item["id"], item["task"][:80])
            await self.run(item, chat_id)
            status = "виконано"
        except asyncio.CancelledError:
            status = "скасовано"
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Заплановане завдання #%s впало", item["id"])
            status = f"помилка: {e}"[:200]
        finally:
            self.storage.update_schedule(item["id"], last_status=status)
            self._running.discard(item["id"])
