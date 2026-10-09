"""Конфігурація агента: змінні середовища з префіксом BONSAI_ (та необов'язковий .env файл)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "hf.co/prism-ml/Ternary-Bonsai-27B-gguf"


def _load_dotenv(path: Path) -> None:
    """Мінімальний .env-завантажувач: KEY=VALUE, без перезапису вже заданих змінних."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(name: str, default: str) -> str:
    return os.environ.get(f"BONSAI_{name}", default)


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _env_opt_bool(name: str) -> bool | None:
    value = os.environ.get(f"BONSAI_{name}")
    if value is None or value.strip() == "" or value.lower() == "auto":
        return None
    return value.lower() in {"1", "true", "yes", "on"}


def _env_opt_float(name: str) -> float | None:
    value = os.environ.get(f"BONSAI_{name}", "").strip()
    return None if value in {"", "auto"} else float(value)


@dataclass
class Config:
    # LLM: "ollama" (типово) або "openai" — OpenAI-сумісний сервер
    # (напр. llama-server з форку PrismML llama.cpp, якщо Ollama не вміє ternary-формат)
    backend: str = "ollama"
    llm_url: str = "http://127.0.0.1:8080"
    api_key: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = DEFAULT_MODEL
    # "auto" — нативні tool calls Ollama, з автоматичним відкатом на JSON-протокол;
    # "native" — тільки нативні; "json" — тільки текстовий JSON-протокол.
    tool_mode: str = "auto"
    think: bool | None = None  # None = не передавати параметр think
    num_ctx: int = 16384
    # None — не передавати: діють налаштування сервера моделі (напр. --temp у llama-server)
    temperature: float | None = None
    keep_alive: str = "30m"
    request_timeout: float = 600.0

    # Агент
    max_steps: int = 25
    max_history_chars: int = 48000
    max_tool_output_chars: int = 12000
    language: str = "українська"

    # Інструменти
    workspace: Path = field(default_factory=lambda: Path.home() / "bonsai-workspace")
    allow_outside_workspace: bool = False
    auto_approve: bool = False
    shell_timeout: int = 120
    python_timeout: int = 120
    searxng_url: str = ""

    # Дані / сервер
    data_dir: Path = field(default_factory=lambda: Path.home() / ".local" / "share" / "bonsai-agent")
    host: str = "0.0.0.0"
    port: int = 8765
    token: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bonsai.sqlite3"

    @classmethod
    def from_env(cls, dotenv: Path | None = None) -> "Config":
        _load_dotenv(dotenv or Path.cwd() / ".env")
        d = cls()
        cfg = cls(
            backend=_env("BACKEND", d.backend).lower(),
            llm_url=_env("LLM_URL", d.llm_url),
            api_key=_env("API_KEY", d.api_key),
            ollama_url=_env("OLLAMA_URL", os.environ.get("OLLAMA_HOST", d.ollama_url)),
            model=_env("MODEL", d.model),
            tool_mode=_env("TOOL_MODE", d.tool_mode).lower(),
            think=_env_opt_bool("THINK"),
            num_ctx=int(_env("NUM_CTX", str(d.num_ctx))),
            temperature=_env_opt_float("TEMPERATURE"),
            keep_alive=_env("KEEP_ALIVE", d.keep_alive),
            request_timeout=float(_env("REQUEST_TIMEOUT", str(d.request_timeout))),
            max_steps=int(_env("MAX_STEPS", str(d.max_steps))),
            max_history_chars=int(_env("MAX_HISTORY_CHARS", str(d.max_history_chars))),
            max_tool_output_chars=int(_env("MAX_TOOL_OUTPUT_CHARS", str(d.max_tool_output_chars))),
            language=_env("LANGUAGE", d.language),
            workspace=Path(_env("WORKSPACE", str(d.workspace))).expanduser(),
            allow_outside_workspace=_env_bool("ALLOW_OUTSIDE_WORKSPACE", d.allow_outside_workspace),
            auto_approve=_env_bool("AUTO_APPROVE", d.auto_approve),
            shell_timeout=int(_env("SHELL_TIMEOUT", str(d.shell_timeout))),
            python_timeout=int(_env("PYTHON_TIMEOUT", str(d.python_timeout))),
            searxng_url=_env("SEARXNG_URL", d.searxng_url),
            data_dir=Path(_env("DATA_DIR", str(d.data_dir))).expanduser(),
            host=_env("HOST", d.host),
            port=int(_env("PORT", str(d.port))),
            token=_env("TOKEN", d.token),
        )
        if not cfg.ollama_url.startswith(("http://", "https://")):
            cfg.ollama_url = "http://" + cfg.ollama_url
        if cfg.backend not in {"ollama", "openai"}:
            raise ValueError(f"BONSAI_BACKEND має бути ollama|openai, отримано {cfg.backend!r}")
        if cfg.tool_mode not in {"auto", "native", "json"}:
            raise ValueError(f"BONSAI_TOOL_MODE має бути auto|native|json, отримано {cfg.tool_mode!r}")
        return cfg

    def ensure_dirs(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
