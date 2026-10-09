"""Асинхронний клієнт Ollama /api/chat зі стрімінгом, tool calls та thinking."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

TokenCallback = Callable[[str, str], Awaitable[None]]  # (kind: "content"|"thinking", text)


class OllamaError(RuntimeError):
    pass


class ToolsNotSupported(OllamaError):
    """Модель/шаблон не підтримує нативні tool calls."""


@dataclass
class ChatResult:
    content: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def as_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        num_ctx: int = 16384,
        temperature: float | None = None,
        keep_alive: str = "30m",
        think: bool | None = None,
        timeout: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.options: dict[str, Any] = {"num_ctx": num_ctx}
        if temperature is not None:
            self.options["temperature"] = temperature
        self.keep_alive = keep_alive
        self.think = think
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> list[str]:
        r = await self._client.get(f"{self.base_url}/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    async def health(self) -> dict[str, Any]:
        try:
            models = await self.list_models()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        present = any(m == self.model or m.startswith(self.model + ":") for m in models)
        return {"ok": True, "model": self.model, "model_present": present}

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_token: TokenCallback | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": self.options,
            "keep_alive": self.keep_alive,
        }
        if tools:
            payload["tools"] = tools
        if self.think is not None:
            payload["think"] = self.think

        try:
            return await self._stream(payload, on_token)
        except OllamaError as e:
            text = str(e).lower()
            if "think" in text and "support" in text and "think" in payload:
                # Модель не вміє thinking — вимикаємо і пробуємо ще раз.
                self.think = None
                payload.pop("think")
                return await self._stream(payload, on_token)
            if tools and "tools" in text and "support" in text:
                raise ToolsNotSupported(str(e)) from e
            raise

    async def _stream(self, payload: dict[str, Any], on_token: TokenCallback | None) -> ChatResult:
        result = ChatResult()
        try:
            async with self._client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    try:
                        body = json.loads(body).get("error", body)
                    except ValueError:
                        pass
                    raise OllamaError(f"Ollama HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise OllamaError(chunk["error"])
                    msg = chunk.get("message") or {}
                    if msg.get("thinking"):
                        result.thinking += msg["thinking"]
                        if on_token:
                            await on_token("thinking", msg["thinking"])
                    if msg.get("content"):
                        result.content += msg["content"]
                        if on_token:
                            await on_token("content", msg["content"])
                    if msg.get("tool_calls"):
                        result.tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done"):
                        result.stats = {
                            k: chunk.get(k)
                            for k in ("total_duration", "prompt_eval_count", "eval_count")
                            if k in chunk
                        }
        except httpx.ConnectError as e:
            raise OllamaError(f"Не вдалося підключитися до Ollama ({self.base_url}): {e}") from e
        return result


class OpenAICompatClient:
    """Клієнт OpenAI-сумісного API (llama-server з форку PrismML llama.cpp, vLLM, LM Studio…).

    Історія агента зберігається у форматі Ollama; тут вона конвертується у формат OpenAI.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        temperature: float | None = None,
        timeout: float = 600.0,
    ) -> None:
        base = base_url.rstrip("/")
        self.base_url = base if base.endswith("/v1") else base + "/v1"
        self.model = model
        self.temperature = temperature
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), headers=headers)

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        try:
            r = await self._client.get(f"{self.base_url}/models")
            r.raise_for_status()
            models = [m.get("id", "") for m in r.json().get("data", [])]
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        # llama-server обслуговує одну модель (id = --alias); запити з іншою назвою він теж приймає
        return {"ok": True, "model": self.model, "model_present": not models or self.model in models, "served": models}

    @staticmethod
    def convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pending_ids: list[str] = []
        counter = 0
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                calls = []
                pending_ids = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    counter += 1
                    cid = tc.get("id") or f"call_{counter}"
                    pending_ids.append(cid)
                    args = fn.get("arguments", {})
                    calls.append({
                        "id": cid,
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
                        },
                    })
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            elif role == "tool":
                cid = pending_ids.pop(0) if pending_ids else f"call_{counter}"
                out.append({"role": "tool", "tool_call_id": cid, "content": m.get("content", "")})
            else:
                out.append({"role": role, "content": m.get("content", "")})
        return out

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_token: TokenCallback | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self.convert_messages(messages),
            "stream": True,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if tools:
            payload["tools"] = tools
        result = ChatResult()
        partial: dict[int, dict[str, Any]] = {}
        try:
            async with self._client.stream("POST", f"{self.base_url}/chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    if tools and "tool" in body.lower():
                        raise ToolsNotSupported(body)
                    raise OllamaError(f"LLM HTTP {resp.status_code}: {body[:500]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        continue  # не break: даємо стріму закритися штатно
                    chunk = json.loads(data)
                    if "error" in chunk:
                        raise OllamaError(str(chunk["error"]))
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            result.thinking += reasoning
                            if on_token:
                                await on_token("thinking", reasoning)
                        if delta.get("content"):
                            result.content += delta["content"]
                            if on_token:
                                await on_token("content", delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = partial.setdefault(tc.get("index", len(partial)), {"name": "", "arguments": ""})
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["arguments"] += fn.get("arguments") or ""
                    if chunk.get("usage"):
                        result.stats = chunk["usage"]
        except httpx.ConnectError as e:
            raise OllamaError(f"Не вдалося підключитися до LLM ({self.base_url}): {e}") from e
        for _, slot in sorted(partial.items()):
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
            except ValueError:
                args = {}
            result.tool_calls.append({"function": {"name": slot["name"], "arguments": args}})
        return result


def create_llm(config: Any) -> "OllamaClient | OpenAICompatClient":
    if config.backend == "openai":
        return OpenAICompatClient(
            config.llm_url, config.model, api_key=config.api_key,
            temperature=config.temperature, timeout=config.request_timeout,
        )
    return OllamaClient(
        config.ollama_url, config.model, num_ctx=config.num_ctx, temperature=config.temperature,
        keep_alive=config.keep_alive, think=config.think, timeout=config.request_timeout,
    )
