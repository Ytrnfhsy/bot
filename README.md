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
   bonsai chat --server ws://… (CLI/REPL) ─────────▶│     Ollama / llama-server              │
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

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull hf.co/prism-ml/Ternary-Bonsai-27B-gguf     # або конкретний квант: …:Q2_0
```

> ⚠️ **Важливо про ternary-формат.** За даними PrismML, ternary GGUF (Q2_0, група 128) потребує
> їхнього форку llama.cpp, і деякі релізи Ollama можуть відмовлятися завантажувати модель.
> Якщо `ollama pull` або `ollama run` завершується помилкою, запусти модель через `llama-server`
> з форку PrismML (OpenAI-сумісний API) і перемкни агента на нього:
>
> ```bash
> ./llama-server -m Ternary-Bonsai-27B-Q2_0.gguf --jinja -c 16384 --port 8080
> ```
> ```ini
> BONSAI_BACKEND=openai
> BONSAI_LLM_URL=http://127.0.0.1:8080
> ```
> Прапорець `--jinja` вмикає шаблон чату з підтримкою tool calls.

### 1.2 Агент

Швидкий запуск:

```bash
cd server
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env          # і задай BONSAI_TOKEN: openssl rand -hex 24
bonsai health                 # перевірка доступності моделі
bonsai serve                  # ws://0.0.0.0:8765/ws
```

Або одразу як systemd-сервіс (автозапуск, логи в journald):

```bash
cd server && ./install_server.sh          # PULL_MODEL=1 ./install_server.sh — заодно завантажить модель
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
