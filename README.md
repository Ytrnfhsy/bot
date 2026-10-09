# 🤖 Bonsai Agent

Власний автономний агент на базі **Ollama** з моделлю **Ternary Bonsai 2 27B** і милим
десктоп-роботом, який живе поверх усіх вікон на Fedora.

```
┌──────────── Клієнт (Fedora) ────────────┐        ┌──────────────── Сервер ────────────────┐
│  🤖 bonsai-robot (PySide6)               │  WS    │  bonsai serve (FastAPI)                │
│  • завжди поверх вікон, анімації         │◀──────▶│  • цикл агента (LLM ↔ інструменти)     │
│  • клік → поле вводу задачі              │ token  │  • web_search / fetch_url (HTML, PDF)  │
│  • кнопки «Дозволити / Відхилити»        │        │  • файли, shell, Python                │
└──────────────────────────────────────────┘        │  • пам'ять (SQLite FTS5), сесії        │
                                                    │           │                            │
   bonsai chat --server ws://… (CLI/REPL) ─────────▶│  llama-server :8085 (Docker) / Ollama  │
                                                    └────────────────────────────────────────┘
```

## Можливості

| Інструмент | Що робить | Підтвердження |
|---|---|---|
| `web_search` | пошук в інтернеті (DuckDuckGo або власний SearXNG), режим новин | — |
| `fetch_url` | читає сторінку чи PDF як текст, з пагінацією | — |
| `read_file`, `list_dir`, `search_files` | читання та пошук по файлах у workspace | — |
| `write_file`, `edit_file` | створення та редагування файлів | ✅ |
| `run_shell` | команди bash у workspace (з таймаутом) | ✅ |
| `run_python` | виконання Python-коду (обчислення, аналіз даних) | ✅ |
| `remember`, `recall`, `forget` | довготривала пам'ять між сесіями | — |
| `current_datetime` | поточні дата й час | — |

- **Пам'ять.** Перед кожною задачею агент сам підтягує релевантні записи з пам'яті. Історія розмов зберігається по сесіях.
- **Безпека.** Небезпечні дії виконуються тільки після явного дозволу. Файлові операції обмежені робочою директорією `BONSAI_WORKSPACE`. Доступ до сервера захищений токеном.
- **Сумісність із моделлю.** Агент використовує нативні tool calls Ollama. Якщо модель їх не підтримує, він автоматично переходить на текстовий JSON-протокол (`BONSAI_TOOL_MODE=auto`).

## Робот-оболонка

![Стани робота](docs/robots.png)

![Панель](docs/panel.png)

- **Клік** відкриває або ховає поле вводу задачі. **Enter** надсилає задачу, **Shift+Enter** переносить рядок.
- **Перетягування** переміщує робота. Позиція запам'ятовується.
- **Права кнопка миші** відкриває меню: нова розмова, зупинити задачу, налаштування, сховати, вийти. Те саме меню є в треї.
- Очі стежать за курсором. Під час роздумів навколо голови обертаються точки, під час роботи крутяться шестерні. Коли агент чекає дозволу, над роботом з'являється «?». Якщо робот довго простоює, він засинає (Zzz).
- Подивитися всі анімації без сервера: `bonsai-robot --demo`.

---

## 1. Сервер

### 1.1 Модель

Агент вміє працювати з двома бекендами. Обидва дають ту саму функціональність.

**Варіант А — llama-server у Docker (форк PrismML).** Рекомендовано для ternary-моделей.
Агент розрахований на наявний `docker-compose.yml` з `llama-server` на `127.0.0.1:8085`,
`--alias bonsai-2-27b` і `--jinja`:

```bash
docker compose up -d                       # у директорії з compose і .gguf
curl -s http://127.0.0.1:8085/v1/models    # має показати "bonsai-2-27b"
```

Готовий конфіг агента лежить у [`server/.env.llama-server`](server/.env.llama-server):
`BONSAI_BACKEND=openai`, `BONSAI_LLM_URL=http://127.0.0.1:8085`, `BONSAI_MODEL=bonsai-2-27b`,
історія розрахована на `-c 81920`. Температуру агент не передає, тому діють `--temp/--top-p/--top-k`
з compose (задати свою можна через `BONSAI_TEMPERATURE`).

> Порт опублікований лише на `127.0.0.1`, тому агент-сервер має працювати на тій самій машині, що й
> контейнер. Агент сам віддаватиме WebSocket-порт 8765 клієнтам. Контейнер має `restart: "no"`:
> якщо модель не запущена, агент повідомить про це в логах і відповідях, а після `docker compose up -d`
> підхопить її без перезапуску.

**Варіант Б — Ollama.**

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull hf.co/prism-ml/Ternary-Bonsai-27B-gguf
```

> ⚠️ За даними PrismML, ternary GGUF потребує їхнього форку llama.cpp, і релізи Ollama можуть
> відмовлятися завантажувати модель. У такому разі використовуй варіант А.

### 1.2 Агент

Швидкий запуск:

```bash
cd server
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.llama-server .env     # варіант А; для Ollama — .env.example
                              # і задай BONSAI_TOKEN: openssl rand -hex 24
bonsai health                 # перевірка доступності моделі
bonsai serve                  # ws://0.0.0.0:8765/ws
```

Або одразу як systemd-сервіс (автозапуск, логи в journald):

```bash
cd server && ./install_server.sh --llama-server   # варіант А (Docker / llama-server)
cd server && ./install_server.sh                  # варіант Б (Ollama); PULL_MODEL=1 — заодно завантажить модель
journalctl --user -u bonsai-agent -f
```

Конфіг лежить у `~/.config/bonsai-agent.env`, там же згенерований токен. Усі параметри описані в [`server/.env.example`](server/.env.example).

> 🔒 Не відкривай порт 8765 в інтернет напряму. Використовуй локальну мережу, VPN (наприклад, Tailscale)
> або reverse-proxy з TLS (Caddy чи nginx). Тоді в клієнті адреса буде `wss://…`.

## 2. Клієнт на Fedora

```bash
cd client
./install_fedora.sh ws://IP_СЕРВЕРА:8765/ws ТОКЕН
bonsai-robot
```

Скрипт встановлює потрібні системні пакети (`xcb-util-cursor` тощо), створює venv з PySide6, лаунчер
`~/.local/bin/bonsai-robot`, пункт у меню та автозапуск при вході в систему. Видалення: `./uninstall_fedora.sh`.

**Чому робот працює через XWayland.** На Wayland (GNOME у Fedora) застосунок не може сам триматися
поверх усіх вікон і задавати свою позицію. Тому лаунчер запускає Qt з `QT_QPA_PLATFORM=xcb`. Якщо дуже
треба нативний Wayland, запусти так: `QT_QPA_PLATFORM=wayland bonsai-robot`. Але тоді «поверх усіх вікон»
залежить від композитора.

Налаштування клієнта зберігаються в `~/.config/bonsai-robot/config.json`. Їх також можна змінити через
ПКМ по роботу → Налаштування.

## 3. CLI / REPL

```bash
bonsai chat                                            # локально (агент у цьому ж процесі)
bonsai chat --server ws://IP:8765/ws --token ТОКЕН     # через сервер
bonsai ask "знайди 3 свіжі новини про Fedora 43 і коротко перекажи"
bonsai chat --thinking                                 # показувати міркування моделі
```

У REPL доступні команди `/reset` (нова розмова) і `/exit`. **Ctrl+C** скасовує поточну задачу.

## 4. API

**WebSocket** `ws://host:8765/ws?token=…` обмінюється JSON-повідомленнями. Повний опис протоколу є в
[`server/bonsai_agent/server.py`](server/bonsai_agent/server.py).

```jsonc
// клієнт → сервер
{"type": "task", "text": "…", "session_id": "default"}
{"type": "confirm_response", "id": "…", "approved": true}
{"type": "cancel"} | {"type": "reset"}
// сервер → клієнт
status | thinking | token | tool_call | tool_result | confirm_request | final | error
```

**REST** `POST /api/task` з тілом `{"text": "...", "session_id": "api"}` і заголовком
`Authorization: Bearer ТОКЕН` виконує задачу неінтерактивно. Небезпечні дії при цьому відхиляються,
якщо не встановлено `BONSAI_AUTO_APPROVE=true`. Перевірка стану: `GET /health`.

## Розробка

```bash
cd server && pip install -e '.[dev]' && pytest
```

Тести використовують фейкову LLM. Вони перевіряють цикл агента, відкат на JSON-протокол, інструменти,
пам'ять, пісочницю шляхів, парсинг стрімів Ollama та OpenAI і WebSocket-протокол із підтвердженнями.

```
server/bonsai_agent/
  agent.py      цикл агента, системний промпт, JSON-fallback
  llm.py        клієнти Ollama і OpenAI-сумісного API (стрімінг, tools, thinking)
  server.py     FastAPI + WebSocket
  cli.py        bonsai serve | chat | ask | health
  storage.py    SQLite: пам'ять (FTS5) і сесії
  tools/        web, files, shell, python_exec, memory, system
client/bonsai_robot/
  robot.py      малювання робота й анімації (QPainter)
  panel.py      спливаюча панель: ввід, стрічка, підтвердження
  app.py        вікно поверх усіх, меню, трей, обробка подій
  connection.py WebSocket з автоперепідключенням
```

### Як додати свій інструмент

Створи модуль у `server/bonsai_agent/tools/` зі списком `TOOLS = [Tool(name=…, description=…,
parameters={JSON Schema}, func=async_fn, dangerous=False)]` і додай модуль у `build_registry()`.
