"""Пошук пам'яті за змістом: ембединги від окремої маленької моделі (llama-server --embedding).

Якщо модель ембедингів недоступна, усе працює як раніше — пошуком за словами (FTS).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from array import array
from typing import Any

import httpx

from .config import Config
from .storage import Storage

log = logging.getLogger("kru.semantic")

RETRY_AFTER = 60.0  # як довго не смикати недоступний сервер ембедингів


def normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def to_blob(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def from_blob(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


def dot(a, b) -> float:
    return math.fsum(x * y for x, y in zip(a, b))


class Embedder:
    """Клієнт OpenAI-сумісного /v1/embeddings. Вектори нормалізовані (косинус = скалярний добуток)."""

    def __init__(self, config: Config) -> None:
        self.url = config.embed_url.rstrip("/")
        self.model = config.embed_model
        self.query_prefix = config.embed_query_prefix
        self._down_until = 0.0
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=3.0)) if self.url else None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def embed(self, texts: list[str], query: bool = False) -> list[list[float]] | None:
        """None — сервер ембедингів вимкнено або недоступний (тоді шукаємо за словами)."""
        if not self._client or not texts or time.monotonic() < self._down_until:
            return None
        inputs = [(self.query_prefix + t) if query else t for t in texts]
        try:
            resp = await self._client.post(f"{self.url}/v1/embeddings", json={"model": self.model, "input": inputs})
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
            vectors = [normalize([float(x) for x in d["embedding"]]) for d in data]
            if len(vectors) != len(texts):
                raise ValueError(f"отримано {len(vectors)} векторів замість {len(texts)}")
            return vectors
        except Exception as e:  # noqa: BLE001
            if self._down_until == 0.0 or time.monotonic() - self._down_until > RETRY_AFTER:
                log.warning("Модель ембедингів недоступна (%s) — пам'ять шукаю за словами", e)
            self._down_until = time.monotonic() + RETRY_AFTER
            return None

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()


class MemoryIndex:
    """Гібридний пошук у пам'яті: за змістом (ембединги) + за словами (FTS), об'єднані через RRF."""

    def __init__(self, storage: Storage, embedder: Embedder | None, min_score: float = 0.45) -> None:
        self.storage = storage
        self.embedder = embedder if embedder and embedder.enabled else None
        self.min_score = min_score

    async def _backfill(self, owner: str) -> None:
        """Проіндексувати записи без вектора (старі або збережені, поки модель ембедингів лежала)."""
        assert self.embedder
        for _ in range(20):  # не більше 20×32 записів за раз
            todo = await asyncio.to_thread(self.storage.memories_without_embedding, owner, self.embedder.model, 32)
            if not todo:
                return
            vectors = await self.embedder.embed([t for _, t in todo])
            if vectors is None:
                return
            await asyncio.to_thread(
                self.storage.set_embeddings, [(mid, to_blob(v)) for (mid, _), v in zip(todo, vectors)],
                self.embedder.model,
            )

    async def index(self, memory_id: int, text: str) -> list[float] | None:
        if not self.embedder:
            return None
        vectors = await self.embedder.embed([text])
        if vectors:
            await asyncio.to_thread(self.storage.set_embeddings, [(memory_id, to_blob(vectors[0]))], self.embedder.model)
            return vectors[0]
        return None

    async def similar(self, vector: list[float], owner: str, limit: int = 3, exclude: int | None = None) -> list[tuple[float, dict]]:
        """Найближчі записи до вектора: [(схожість, запис)], від найсхожішого."""
        assert self.embedder
        model = self.embedder.model

        def score() -> list[tuple[float, dict]]:  # у потоці: тисячі векторів не блокують сервер
            rows = self.storage.memory_vectors(owner, model)
            scored = [(dot(vector, from_blob(blob)), row) for row, blob in rows if row["id"] != exclude]
            scored.sort(key=lambda x: x[0], reverse=True)
            return scored[:limit]

        return await asyncio.to_thread(score)

    async def search(self, query: str, owner: str, limit: int = 6) -> list[dict[str, Any]]:
        keyword = await asyncio.to_thread(self.storage.search_memories, query, limit * 2, owner)
        if not query.strip() or not self.embedder:
            return keyword[:limit]
        await self._backfill(owner)
        qv = await self.embedder.embed([query], query=True)
        if qv is None:
            return keyword[:limit]
        semantic = [row for score, row in await self.similar(qv[0], owner, limit * 2) if score >= self.min_score]
        # Reciprocal Rank Fusion: запис, знайдений обома способами, піднімається вище
        scores: dict[int, float] = {}
        rows: dict[int, dict[str, Any]] = {}
        for ranked in (semantic, keyword):
            for rank, row in enumerate(ranked):
                scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (60 + rank)
                rows[row["id"]] = row
        best = sorted(scores, key=lambda i: scores[i], reverse=True)[:limit]
        return [rows[i] for i in best]
