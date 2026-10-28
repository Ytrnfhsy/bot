---
name: kru-dev
description: Розробка самого Кру — як влаштований проєкт (сервер, робот, деплой), де що лежить, як змінювати, тестувати й оновлювати; діагностика, коли Кру чи робот працюють не так
---

# Розробка Кру

Ти допомагаєш змінювати й лагодити власний код. Обережно: помилка тут ламає тебе самого.

## Де що

Репозиторій — зазвичай `~/bot` на сервері (знайди: `git -C ~/bot rev-parse --show-toplevel`; якщо ні —
`find ~ -maxdepth 3 -name update.sh -path '*bot*'`). Працюй з ним через run_shell (він поза робочою текою).

- `server/kru_agent/` — сервер (Python, FastAPI):
  - `agent.py` — цикл агента, системний промпт, план, субагенти, стиснення історії;
  - `llm.py` — клієнти llama-server / Ollama; `server.py` — WebSocket-протокол (опис угорі файлу), задачі;
  - `tools/` — інструменти (кожен модуль має список `TOOLS`, реєстрація — `tools/__init__.py`);
  - `skills.py` + `builtin_skills/` — навички; `scheduler.py` — завдання за розкладом;
  - `telegram*.py` — бот і форматування; `voice.py` — Whisper і HolosTTS; `wakeword.py` — навчання фрази;
  - `storage.py` — SQLite (чати, пам'ять, розклад); `config.py` — усі змінні KRU_*; `cli.py` — команда `kru`.
- `server/tests/` — тести (pytest, без мережі й моделі).
- `client/kru_robot/` — десктоп-робот (PySide6): `app.py` вікно й логіка, `panel.py` чат, `robot.py`
  малювання, `wake.py` режим «Кру», `vad.py` детектор голосу, `audio.py`, `connection.py`;
  `selftest.py` — самоперевірка (`kru-robot --selftest звіт.json`).
- `deploy/` — `model.sh` (Qwen у Docker), `voice.sh`, `holos.sh`, `searxng.sh`, `sandbox.sh`, `tune.sh`.
- `update.sh` — оновлення: git pull + перевстановлення у venv + перезапуск.

Встановлено не з репозиторію напряму, а у venv (`~/.local/share/kru/venv`): зміни в `~/bot` НЕ діють,
доки не виконано `./update.sh`. Налаштування — `~/.config/kru.env`, дані — `~/.local/share/kru/`.

## Як змінювати

1. Спершу зрозумій: прочитай потрібні файли (grep -n, sed -n по рядках), знайди, як зроблено схоже.
   Пиши в тому ж стилі: коментарі й повідомлення — українською, без зайвих абстракцій.
2. Зміна — мінімальна, під задачу. Новий інструмент — модуль у `tools/` зі списком `TOOLS` + реєстрація.
3. Перевір перед тим, як казати «готово»:
   - сервер: `cd ~/bot/server && ~/.local/share/kru/venv/bin/python -m pytest -q` (якщо pytest немає —
     `…/venv/bin/pip install pytest pytest-asyncio`);
   - синтаксис/помилки: `python -m py_compile <файл>`; робот — `--selftest` на машині з роботом.
   - додай тест на нову поведінку поруч зі схожими.
4. Покажи користувачу diff (`git diff`) і що перевірено. Комітити, пушити й запускати `update.sh` — ЛИШЕ коли
   він погодить. Якщо репозиторій на GitHub із PR-процесом — працюй у гілці, не в main.
5. **Не перезапускай `kru` сам** (`update.sh`, `systemctl --user restart kru`): це зупинить і тебе посеред
   задачі. Скажи користувачу виконати це самому (або зроби останнім кроком, попередивши, що зв'язок урветься).
6. Не чіпай секрети (`~/.config/kru.env`, токени) і не виводь їх у чат.

## Діагностика

- Журнал сервера: `journalctl --user -u kru -n 200 --no-pager` (кроки агента: рядки «Крок N: … т/с»).
- Модель: `curl -s http://127.0.0.1:8085/health`, `docker logs --tail 100 qwen36`; швидкість — `kru health`.
- Голос: `deploy/voice.sh status`, `deploy/holos.sh status`.
- Робот на ПК: `KRU_WAKE_DEBUG=1 kru-robot` (режим «Кру»), падіння — `~/.local/share/kru-robot/crash.log`.
- Знайшов причину — поясни її простими словами, запропонуй виправлення, і лише потім змінюй код.
