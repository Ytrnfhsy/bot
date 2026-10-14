"""Конфігурація агента: змінні середовища KRU_* та необов'язковий .env файл."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "qwen3.6-35b-a3b"  # --alias у deploy/qwen36/docker-compose.yml


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


def _get(name: str) -> str | None:
    return os.environ.get(f"KRU_{name}")


def _env(name: str, default: str) -> str:
    value = _get(name)
    return default if value is None else value


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _env_opt_bool(name: str) -> bool | None:
    value = _get(name)
    if value is None or value.strip() == "" or value.lower() == "auto":
        return None
    return value.lower() in {"1", "true", "yes", "on"}


def _env_opt_float(name: str) -> float | None:
    value = (_get(name) or "").strip()
    return None if value in {"", "auto"} else float(value)


@dataclass
class Config:
    # LLM: "openai" (типово) — OpenAI-сумісний сервер: llama-server з Qwen3.6 (deploy/qwen36);
    # "ollama" — якщо модель запущено в Ollama
    backend: str = "openai"
    llm_url: str = "http://127.0.0.1:8085"
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
    request_timeout: float = 1200.0  # з міркуваннями відповідь може йти довго

    # Агент
    max_steps: int = 30  # задача з планом (update_plan) отримує більше: ~8 кроків на пункт, до 150
    sub_max_steps: int = 20  # кроків у субагента (delegate)
    # контекст Qwen3.6 у compose — 65536 токенів: ~120k символів історії + промпт і результати інструментів
    max_history_chars: int = 120000
    max_tool_output_chars: int = 20000
    # Зір (розуміння зображень): auto — пробувати, і вимкнути, якщо модель не вміє; on; off
    vision: str = "auto"
    max_images_in_context: int = 3
    # зменшення картинок перед моделлю (пікселів): звичайний перегляд і «детальний» (view_image detail=high)
    image_max_pixels: int = 1_000_000
    image_max_pixels_high: int = 2_500_000  # скільки останніх повідомлень із картинками передавати моделі
    # довгі розмови: старі повідомлення стискаються моделлю в підсумок, а не відкидаються наосліп
    summarize: bool = True
    language: str = "українська"
    # часовий пояс користувачів за замовчуванням (IANA, напр. Europe/Kyiv); порожньо — пояс сервера.
    # Робот передає пояс свого ПК сам — цей параметр лише запасний (CLI, API).
    timezone: str = ""
    agent_name: str = "Kru"
    persona: str = ""  # характер/стиль, напр. "привітний, з легким гумором"

    # Інструменти
    workspace: Path = field(default_factory=lambda: Path.home() / "kru-workspace")
    allow_outside_workspace: bool = False  # лише для адміністраторів
    # робочі теки інших користувачів: <USERS_WORKSPACE>/<ім'я>/
    users_workspace: Path = field(default_factory=lambda: Path.home() / "kru-users")
    auto_approve: bool = False
    shell_timeout: int = 120
    python_timeout: int = 120
    searxng_url: str = ""
    # Telegram: токен бота від @BotFather; чат прив'язується командою `kru telegram link`
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""  # необов'язково: чат власника без прив'язки

    # Пам'ять за змістом: окрема маленька модель ембедингів (deploy/qwen36 → сервіс embed). Порожньо — вимкнено.
    embed_url: str = "http://127.0.0.1:8086"
    embed_model: str = "qwen3-embedding-0.6b"
    # Qwen3-Embedding радить інструкцію для запитів (для записів — без неї)
    embed_query_prefix: str = "Instruct: Given a question, retrieve stored facts that help answer it\nQuery: "
    memory_min_score: float = 0.45  # мінімальна схожість (косинус), щоб запис вважався доречним

    # Дані / сервер
    data_dir: Path = field(default_factory=lambda: Path.home() / ".local" / "share" / "kru")
    host: str = "0.0.0.0"
    port: int = 8765
    token: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "kru.sqlite3"

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
            sub_max_steps=int(_env("SUB_MAX_STEPS", str(d.sub_max_steps))),
            max_history_chars=int(_env("MAX_HISTORY_CHARS", str(d.max_history_chars))),
            max_tool_output_chars=int(_env("MAX_TOOL_OUTPUT_CHARS", str(d.max_tool_output_chars))),
            vision=_env("VISION", d.vision).lower(),
            max_images_in_context=int(_env("MAX_IMAGES_IN_CONTEXT", str(d.max_images_in_context))),
            image_max_pixels=int(_env("IMAGE_MAX_PIXELS", str(d.image_max_pixels))),
            image_max_pixels_high=int(_env("IMAGE_MAX_PIXELS_HIGH", str(d.image_max_pixels_high))),
            summarize=_env_bool("SUMMARIZE", d.summarize),
            language=_env("LANGUAGE", d.language),
            timezone=_env("TIMEZONE", d.timezone),
            agent_name=_env("AGENT_NAME", d.agent_name),
            persona=_env("PERSONA", d.persona),
            workspace=Path(_env("WORKSPACE", str(d.workspace))).expanduser(),
            allow_outside_workspace=_env_bool("ALLOW_OUTSIDE_WORKSPACE", d.allow_outside_workspace),
            users_workspace=Path(_env("USERS_WORKSPACE", str(d.users_workspace))).expanduser(),
            auto_approve=_env_bool("AUTO_APPROVE", d.auto_approve),
            shell_timeout=int(_env("SHELL_TIMEOUT", str(d.shell_timeout))),
            python_timeout=int(_env("PYTHON_TIMEOUT", str(d.python_timeout))),
            searxng_url=_env("SEARXNG_URL", d.searxng_url),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", d.telegram_bot_token),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID", d.telegram_chat_id),
            embed_url=_env("EMBED_URL", d.embed_url),
            embed_model=_env("EMBED_MODEL", d.embed_model),
            embed_query_prefix=_env("EMBED_QUERY_PREFIX", d.embed_query_prefix).replace("\\n", "\n"),
            memory_min_score=float(_env("MEMORY_MIN_SCORE", str(d.memory_min_score))),
            data_dir=Path(_env("DATA_DIR", str(d.data_dir))).expanduser(),
            host=_env("HOST", d.host),
            port=int(_env("PORT", str(d.port))),
            token=_env("TOKEN", d.token),
        )
        if not cfg.ollama_url.startswith(("http://", "https://")):
            cfg.ollama_url = "http://" + cfg.ollama_url
        if cfg.backend not in {"ollama", "openai"}:
            raise ValueError(f"KRU_BACKEND має бути ollama|openai, отримано {cfg.backend!r}")
        if cfg.tool_mode not in {"auto", "native", "json"}:
            raise ValueError(f"KRU_TOOL_MODE має бути auto|native|json, отримано {cfg.tool_mode!r}")
        return cfg

    def ensure_dirs(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
