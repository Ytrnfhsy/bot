"""Самоперевірка робота на чистій системі (запускається в CI на Windows і Linux).

kru-robot --selftest report.json   → JSON-звіт + знімки екрана поруч, код виходу 0 — усе гаразд.
"""

from __future__ import annotations

import json
import os
import sys
import time as _time
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtWidgets import QApplication


def run_selftest(app: QApplication, report_path: str) -> int:
    report = Path(report_path).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    # сторож: якщо щось зависне — через 90 с записати стеки всіх потоків і завершити процес
    import faulthandler

    hang_log = open(report.with_name("selftest-hang.txt"), "w", encoding="utf-8")  # noqa: SIM115
    faulthandler.dump_traceback_later(90, exit=True, file=hang_log)
    log = lambda msg: print(f"[selftest] {msg}", flush=True)  # noqa: E731
    log(f"старт, Qt platform={app.platformName()}")
    # окремий тимчасовий профіль — не чіпаємо справжні налаштування
    tmp = tempfile.mkdtemp(prefix="kru-selftest-")
    os.environ["APPDATA"] = tmp
    os.environ["XDG_CONFIG_HOME"] = tmp

    from . import app as appmod
    from .icon import app_icon
    from .settings import Settings, config_path
    from .winplatform import IS_WINDOWS, autostart_command, autostart_enabled, post_hotkey_for_test, set_autostart

    results: dict[str, dict[str, Any]] = {}
    state: dict[str, Any] = {}

    def check(name: str, fn: Callable[[], Any]) -> None:
        try:
            detail = fn()
            results[name] = {"ok": True, "detail": detail}
        except Exception as e:  # noqa: BLE001
            results[name] = {"ok": False, "detail": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}

    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise AssertionError(msg)

    # ---- синхронні перевірки ----
    def icon_check():
        sizes = sorted(s.width() for s in app_icon().availableSizes())
        need(16 in sizes and 256 in sizes, f"розміри: {sizes}")
        return sizes

    check("icon", icon_check)
    check("settings_path", lambda: (need(str(config_path()).startswith(tmp), str(config_path())), str(config_path()))[1])
    check("ipc_name", lambda: appmod._ipc_name())
    check("export_dir", lambda: str(appmod.RobotWindow.export_dir()))
    check("timezone", lambda: (need("/" in appmod.local_timezone() or appmod.local_timezone() == "UTC",
                                    f"пояс: {appmod.local_timezone()!r}"), appmod.local_timezone())[1])

    log("синхронні перевірки готові; створюю вікно робота")
    # локальний TCP-сервер: перевіряємо, що робот справді відкриває WebSocket-з'єднання
    # (з'єднання із закритим портом на Windows може «висіти» десятки секунд — на це не покладаємось)
    from PySide6.QtNetwork import QHostAddress, QTcpServer

    handshakes: list[str] = []
    tcp = QTcpServer()
    need_listen = tcp.listen(QHostAddress.SpecialAddress.LocalHost, 0)
    log(f"тестовий сервер: {need_listen}, порт {tcp.serverPort()}")

    def on_tcp() -> None:
        sock = tcp.nextPendingConnection()
        if sock is None:
            return
        state.setdefault("socks", []).append(sock)

        def read() -> None:
            head = bytes(sock.readAll()).decode("latin-1", "replace")
            if "upgrade: websocket" in head.lower():
                handshakes.append(head.splitlines()[0])
                sock.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                sock.disconnectFromHost()

        sock.readyRead.connect(read)

    tcp.newConnection.connect(on_tcp)
    state["tcp"] = tcp
    settings = Settings.load()
    settings.server_url = f"ws://127.0.0.1:{tcp.serverPort()}/ws"
    win = appmod.RobotWindow(settings)
    win.show()
    state["win"] = win
    log("вікно робота створено")
    disconnects: list[str] = []
    win.conn.disconnected.connect(lambda reason, raw="": disconnects.append(f"{reason} [{raw}]"))

    def window_check():
        need(win.isVisible(), "вікно робота не видно")
        need(bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint), "немає «поверх усіх вікон»")
        pm = win.robot.grab()
        img = pm.toImage()
        opaque = sum(1 for x in range(0, img.width(), 7) for y in range(0, img.height(), 7) if img.pixelColor(x, y).alpha() > 0)
        need(opaque > 50, f"робот не намалювався (непрозорих точок: {opaque})")
        pm.save(str(report.with_name("selftest-robot.png")))
        return {"platform": app.platformName(), "opaque_samples": opaque}

    # ---- кроки з паузами (анімації, мережа) ----
    steps: list[tuple[int, Callable[[], None]]] = []

    def step(delay: int, fn: Callable[[], None]) -> None:
        steps.append((delay, fn))

    step(300, lambda: check("window", window_check))
    step(50, lambda: win.on_robot_clicked())

    def wait_panel_shown(timeout: float = 3.0) -> None:
        """Дочекатися кінця анімації появи (на повільній машині CI вона може затягнутися)."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not (win.panel.shown and win.panel.windowOpacity() > 0.95):
            QApplication.processEvents()
            time.sleep(0.02)

    step(300, wait_panel_shown)
    step(50, lambda: check("panel_show", lambda: (need(win.panel.shown, "панель не з'явилась"),
                                                   need(win.panel.windowOpacity() > 0.95, f"прозорість {win.panel.windowOpacity()}"),
                                                   win.panel.grab().save(str(report.with_name("selftest-panel.png"))),
                                                   "ok")[-1]))

    def ask_flow():
        got: list[tuple[str, str]] = []
        win.panel.ask("Назва чату:", [("cancel", "Скасувати", "secondary"), ("save", "Зберегти", "primary")],
                      lambda k, t: got.append((k, t)), input_text="тест", default="save")
        need(win.panel.ask_box.isVisible(), "вбудований запит не з'явився")
        win.panel.ask_input.setText("Мій чат")
        win.panel._ask_answer("save")
        need(got == [("save", "Мій чат")], f"відповідь: {got}")
        return got

    step(50, lambda: check("ask_inline", ask_flow))

    def history_flow():
        win.panel.show_history()
        current = win.settings.session_id
        win.panel.set_history([{"id": current, "title": "Тест", "updated_at": 0, "count": 1},
                               {"id": "інший", "title": "Інший", "updated_at": 0, "count": 2}], current)
        need(win.panel.history_shown and win.panel.history_list.count() == 2, "сторінка історії")
        need(win.panel.history_list.item(0).text().startswith("● Тест"), "поточний чат не позначено")
        # пошук: одразу — за назвою, потім — відповідь сервера з уривком тексту
        sent = []
        orig = win.conn.send
        win.conn.send = lambda msg: sent.append(msg) or True
        try:
            box = win.panel.history_search_box
            need(box.isVisible(), "немає поля пошуку")
            box.setText("інш")
            need(win.panel.history_list.count() == 1, "локальний фільтр за назвою не спрацював")
            win.panel._search_timer.timeout.emit()  # не чекаємо паузи
            need({"type": "chat_search", "query": "інш"} in sent, f"запит пошуку: {sent}")
            win.on_event({"type": "chat_search", "query": "застарілий", "chats": []})  # ігнорується
            need(win.panel.history_list.count() == 1, "застаріла відповідь перебила результат")
            win.on_event({"type": "chat_search", "query": "інш", "chats": [
                {"id": "інший", "title": "Інший", "updated_at": 0, "count": 2, "snippet": "щось інше тут"},
                {"id": "x", "title": "Третій", "updated_at": 0, "count": 1, "snippet": "та інші"}]})
            need(win.panel.history_list.count() == 2 and "щось інше тут" in win.panel.history_list.item(0).text(),
                 "результати сервера не показано")
            box.setText("немає такого")
            need(win.panel.history_empty.isVisible() and "Нічого" in win.panel.history_empty.text(), "порожній пошук")
            box.clear()
            need(win.panel.history_list.count() == 2, "після очищення має бути повний список")
        finally:
            win.conn.send = orig
        win.panel.show_chat()
        need(not win.panel.history_shown, "назад до чату")
        win.panel.add_entry("user", "привіт")
        win.new_chat()
        need(win.settings.session_id != current and not win.panel.entries, "новий чат не почався")
        return "ok"

    step(50, lambda: check("history_page", history_flow))

    def memory_line_flow():
        win.panel.set_show_tools(False)
        win.on_event({"type": "tool_call", "name": "remember", "args": {"content": "Люблю каву"}, "summary": "x"})
        win.on_event({"type": "tool_call", "name": "read_file", "args": {"path": "a"}, "summary": "a"})
        win.panel._render()
        md = win.panel.view.toMarkdown()
        need("Запам'ятав: Люблю каву" in md and "read_file" not in md, f"стрічка: {md[:200]}")
        win.on_event({"type": "status", "state": "summarizing"})
        win.on_event({"type": "progress", "tool": "deep_research", "text": "читаю джерело 1"})
        win.new_chat()
        return "ok"

    step(50, lambda: check("memory_line", memory_line_flow))

    def overlap_flow():
        # панель згорнута, а агент тим часом працює; потім користувач її відкриває —
        # поле вводу не має налізти на стрічку (окрема панель, щоб відтворити саме цей порядок подій)
        from PySide6.QtCore import QPoint

        from .panel import ChatPanel

        p = ChatPanel()
        p.set_show_tools(True)
        p.show_panel(QPoint(100, 100))
        QApplication.processEvents()
        p.hide()
        QApplication.processEvents()
        for _ in range(8):
            p.add_entry("tool", "🔧 read_file: path=/home/ai/kru-workspace/uploads/дуже_довга_назва_файлу.txt, "
                                "start_line=1515, max_lines=100")
            p._render()
        p.set_busy(True)
        p.set_activity("🔧 Працюю: run_shell")
        p.show_panel(QPoint(100, 100))
        overlap_state["panel"] = p
        # висота, яку панель попросила у вікна, має вміщати весь вміст (раніше бралась застаріла, менша)
        asked, needed = p.maximumHeight(), p.layout().minimumSize().height()
        need(asked >= needed, f"панель просить {asked} px, а вмісту треба {needed}")
        return {"asked": asked, "needed": needed}

    overlap_state: dict[str, Any] = {}

    def overlap_check():
        # перевіряємо трохи згодом: на Windows вікно змінює розмір асинхронно (WM_SIZE)
        from PySide6.QtCore import QPoint

        import time

        p = overlap_state["panel"]

        def measure() -> dict:
            view_bottom = p.view.mapTo(p, QPoint(0, 0)).y() + p.view.height()
            return {"view_bottom": view_bottom, "input_top": p.input.mapTo(p, QPoint(0, 0)).y(),
                    "height": p.height(), "needed": p.layout().minimumSize().height()}

        def settled(r: dict) -> bool:
            return r["view_bottom"] <= r["input_top"] and r["height"] >= r["needed"]

        # перевіряємо кінцевий стан, який бачить користувач: на повільному CI Qt інколи ще не переклав
        # дочірні віджети після зміни розміру вікна — чекаємо до 3 с, поки компонування устаткується
        deadline = time.monotonic() + 3.0
        result = measure()
        while not settled(result) and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
            result = measure()
        p.hide()
        p.deleteLater()
        need(settled(result), f"поле вводу налазить на стрічку: {result}")
        return result

    def big_confirm_flow():
        # агент просить дозволу на великий скрипт — кнопки мають лишатися на екрані
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QGuiApplication

        code = "Виконати Python-код:\n" + "\n".join(f"print('рядок {i}', {i} * {i})  # коментар" for i in range(400))
        win.on_event({"type": "confirm_request", "id": "big", "tool": "run_python", "summary": code})
        QApplication.processEvents()
        p = win.panel
        screen_h = (p.screen() or QGuiApplication.primaryScreen()).availableGeometry().height()
        allow_bottom = p.btn_allow.mapTo(p, QPoint(0, 0)).y() + p.btn_allow.height()
        result = {"panel": p.maximumHeight(), "screen": screen_h, "allow_bottom": allow_bottom,
                  "code_box": p.confirm_text.height(), "full_text": len(p.confirm_text.toPlainText()) == len(code)}
        need(p.maximumHeight() <= screen_h, f"панель вища за екран: {result}")
        need(p.btn_allow.isVisible() and allow_bottom <= p.maximumHeight(), f"кнопок не видно: {result}")
        need(result["full_text"], "код показано не повністю")
        p._answer(False)
        return result

    step(50, lambda: check("big_confirm", big_confirm_flow))

    def plan_flow():
        steps = [{"title": "Знайти дані", "status": "done"}, {"title": "Порахувати", "status": "in_progress"},
                 {"title": "Написати звіт", "status": "pending"}]
        win.panel.set_show_tools(False)
        win.on_event({"type": "plan", "steps": steps})
        win.on_event({"type": "tool_call", "name": "delegate", "args": {"task": "Прочитай файли"}, "summary": "x"})
        win.on_event({"type": "tool_call", "name": "read_file", "args": {"path": "a"}, "summary": "a", "sub": True})
        win.panel._render()
        md = win.panel.view.toMarkdown()
        need(win.panel.plan.isVisible() and "1/3" in win.panel.plan.text(), f"чек-лист: {win.panel.plan.text()[:120]}")
        need("Доручаю субагенту" in md and "read_file" not in md, f"стрічка: {md[:200]}")
        win.new_chat()
        need(not win.panel.plan.isVisible(), "чек-лист лишився в новому чаті")
        return "ok"

    step(50, lambda: check("plan_checklist", plan_flow))

    def notify_flow():
        opened = []
        orig = win.open_chat
        win.open_chat = lambda cid: opened.append(cid)
        try:
            win.on_event({"type": "notify", "title": "⏰ Курс долара", "text": "41.5 грн", "chat_id": "sched-1-a"})
            need(win.panel.ask_box.isVisible() and "Курс долара" in win.panel.ask_text.text(), "сповіщення не видно")
            win.panel._ask_answer("open")
            need(opened == ["sched-1-a"], f"чат не відкрився: {opened}")
        finally:
            win.open_chat = orig
        return "ok"

    step(50, lambda: check("scheduled_notify", notify_flow))

    def allow_task_flow():
        sent = []
        orig = win.conn.send
        win.conn.send = lambda msg: sent.append(msg) or True
        try:
            win.on_event({"type": "confirm_request", "id": "c1", "tool": "write_file", "summary": "Записати файл a.txt"})
            win.on_event({"type": "confirm_request", "id": "c2", "tool": "write_file", "summary": "Записати файл b.txt"})
            QApplication.processEvents()
            need(win.panel.btn_allow_task.isVisible(), "немає кнопки «До кінця задачі» для запису файлу")
            win.panel.btn_allow_task.click()
            win.on_event({"type": "confirm_request", "id": "c3", "tool": "run_shell", "summary": "Виконати команду"})
            QApplication.processEvents()
            need(not win.panel.btn_allow_task.isVisible(), "для shell кнопки «До кінця задачі» бути не повинно")
            win.panel._answer(False)
        finally:
            win.conn.send = orig
        replies = [(m["id"], m["approved"], m.get("scope")) for m in sent if m["type"] == "confirm_response"]
        need(replies == [("c1", True, "task"), ("c2", True, "task"), ("c3", False, None)], f"відповіді: {replies}")
        return replies

    step(50, lambda: check("allow_for_task", allow_task_flow))

    def file_flow():
        """Файл від агента: кнопка в стрічці → file_get → файл у «Завантаження/Kru» → «відкрити»."""
        import base64

        sent = []
        orig, orig_dir = win.conn.send, appmod.RobotWindow.download_dir
        win.conn.send = lambda msg: sent.append(msg) or True
        appmod.RobotWindow.download_dir = staticmethod(lambda: Path(tmp) / "Downloads" / "Kru")
        try:
            win.panel.set_show_tools(False)
            win.on_event({"type": "file", "path": "out/звіт_1.md", "name": "звіт_1.md", "size": 2048})
            win.panel._render()
            md = win.panel.view.toMarkdown()
            need("Завантажити" in md and "звіт" in md and "2 КБ" in md, f"кнопки файлу немає: {md[-200:]}")
            fid = next(k for k, v in win.panel.files.items() if v["path"] == "out/звіт_1.md")
            win.panel._on_link(QUrl(f"kru-file:{fid}"))
            req = [m for m in sent if m["type"] == "file_get"]
            need(req == [{"type": "file_get", "id": fid, "path": "out/звіт_1.md"}], f"запит: {req}")
            win.on_event({"type": "file_data", "id": fid, "name": "звіт_1.md", "size": 5,
                          "data": base64.b64encode("Звіт".encode()).decode()})
            saved = Path(tmp) / "Downloads" / "Kru" / "звіт_1.md"
            need(saved.read_text(encoding="utf-8") == "Звіт", "файл не збережено")
            win.panel._render()
            need("відкрити" in win.panel.view.toMarkdown(), "немає посилання «відкрити»")
            win.on_event({"type": "file", "path": "x.pdf", "name": "x.pdf", "size": 1})
            fid2 = str(max(int(k) for k in win.panel.files))
            win.panel.download_file(fid2)
            win.on_event({"type": "file_error", "id": fid2, "path": "x.pdf", "message": "Файл не знайдено"})
            win.panel._render()
            need("ще раз" in win.panel.view.toMarkdown(), "помилку завантаження не показано")
        finally:
            win.conn.send, appmod.RobotWindow.download_dir = orig, orig_dir
        return str(saved)

    step(50, lambda: check("file_download", file_flow))

    def menu_icons_flow():
        """Пункти меню — з намальованими іконками й без емодзі в тексті (меню трею KDE емодзі не показує)."""
        acts = [a for a in win.menu.actions() if not a.isSeparator()]
        need(all(not a.icon().isNull() for a in acts), "є пункти без іконки")
        bad = [a.text() for a in acts if any(ord(ch) > 0x2000 and ch not in "…" for ch in a.text())]
        need(not bad, f"емодзі в меню: {bad}")
        return len(acts)

    step(50, lambda: check("menu_icons", menu_icons_flow))

    def retry_flow():
        """«↻ Повторити» під відповіддю: прибирає попередню спробу й просить сервер виконати задачу заново."""
        sent = []
        orig = win.conn.send
        win.conn.send = lambda msg: sent.append(msg) or True
        try:
            win.new_chat()
            win.panel.set_show_tools(True)
            win.busy = False
            win.panel.set_busy(False)
            win.panel.add_entry("user", "Знайди фото")
            win.panel.add_entry("tool", "🔧 web_search: фото")
            win.panel.add_entry("assistant", "⏹ Задачу скасовано.")
            win.panel._render()
            need("Повторити" in win.panel.view.toMarkdown(), "немає кнопки «Повторити»")
            win.panel._on_link(QUrl("kru-retry:last"))
            msg = next((m for m in sent if m.get("type") == "task"), None)
            need(msg and msg.get("retry") is True and msg.get("text") == "Знайди фото", f"запит: {msg}")
            need([r for r, _ in win.panel.entries] == ["user"], f"стрічка: {win.panel.entries}")
            win.panel._render()
            need("Повторити" not in win.panel.view.toMarkdown(), "кнопка видна, поки агент працює")
            win.panel._on_link(QUrl("kru-retry:last"))  # повторний клік, поки працює, — нічого не шле
            need(sum(1 for m in sent if m.get("type") == "task") == 1, "задачу надіслано двічі")
        finally:
            win.conn.send = orig
            win.busy = False
            win.panel.set_busy(False)
            win.new_chat()
        return "ok"

    step(50, lambda: check("retry_button", retry_flow))

    def thinking_flow():
        """Хід думок: записується завжди, показується приглушеним блоком за вмикачем у меню."""
        was = win.settings.show_thinking
        try:
            win.new_chat()
            win.set_show_thinking(False)
            win.panel.add_entry("user", "2+2?")
            for chunk in ("Користувач питає **суму**. ", "Це <просто>: 2+2=4."):
                win.on_event({"type": "thinking", "text": chunk})
            win.on_event({"type": "final", "text": "4"})
            win.panel._render()
            need("Користувач питає" not in win.panel.view.toPlainText(), "думки видно при вимкненому показі")
            win.act_thinking.setChecked(True)  # вмикач у меню
            need(win.settings.show_thinking and win.panel.show_thinking, "вмикач не спрацював")
            plain = win.panel.view.toPlainText()
            need("Користувач питає **суму**" in plain and "<просто>" in plain, f"думок не видно: {plain[:200]}")
            html_text = win.panel.view.toHtml().lower()
            need("#8a94a6" in html_text and "italic" in html_text, "думки не приглушені")
            need(plain.index("Користувач") < plain.index("4"), "думки мають бути перед відповіддю")
            # довгі міркування на багатьох кроках не роздувають стрічку: давні — одним рядком, останнє — хвіст
            for step in range(3):
                win.on_event({"type": "tool_call", "name": "read_file", "args": {}, "summary": "x"})
                for _ in range(40):
                    win.on_event({"type": "thinking", "text": f"крок {step}: міркую над перекладом розділу. "})
            win.panel._render()
            plain = win.panel.view.toPlainText()
            need(len(plain) < 1400, f"думки займають забагато місця: {len(plain)} символів")
            need("крок 2" in plain and plain.count(" …") >= 2, "давні думки не згорнуто")
            win.act_thinking.setChecked(False)
            need("Користувач питає" not in win.panel.view.toPlainText(), "не сховались після вимкнення")
        finally:
            win.set_show_thinking(was)
            win.new_chat()
        return "ok"

    step(50, lambda: check("thinking_toggle", thinking_flow))

    def think_mode_check():
        """Міркування: авто/завжди/ніколи → що йде на сервер; заміри відповіді — у рядку під чатом."""
        from . import app as appmod

        sent = []
        orig, mode = win.conn.send, win.settings.think_mode
        win.conn.send = lambda m: sent.append(m) or True
        try:
            for m, want in (("auto", "auto"), ("on", True), ("off", False)):
                win.settings.think_mode = m
                win.send_task(f"тест {m}")
                win.on_event({"type": "final", "text": "ок", "stats": {"seconds": 3.24, "steps": 2, "think_steps": 0,
                                                                     "thinking": 0, "gen_tps": 43.4}})
                win.on_event({"type": "status", "state": "idle"})
                task = next(x for x in reversed(sent) if x.get("type") == "task")
                need(task["think"] == want, f"{m}: {task['think']}")
            need(win.panel.activity_text == "⏱ 3,2 с · 2 кроки · без міркувань · 43 т/с",
                 f"заміри: {win.panel.activity_text!r}")
            line = appmod.stats_line({"seconds": 12, "steps": 5, "think_steps": 3, "thinking": 812})
            need(line == "⏱ 12,0 с · 5 кроків · міркування ~812 т", line)
            need(appmod.stats_line(None) == "", "без замірів (старий сервер)")
        finally:
            win.conn.send, win.settings.think_mode = orig, mode
            win.busy = False
            win.panel.set_busy(False)
        return "ok"

    step(50, lambda: check("think_mode", think_mode_check))

    def feed_icons_flow():
        """Службові позначки стрічки — намальовані іконки, а не емодзі (емодзі залежать від шрифтів системи)."""
        import re

        win.new_chat()
        win.panel.set_show_tools(True)
        win.panel.add_entry("user", "Задача\n📎 uploads/a.txt")
        for t in ("🔧 read_file: a", "↳ 🔧 web_search: b", "🧠 Запам'ятав: c", "🤝 Доручаю субагенту: d", "✗ x: e"):
            win.panel.add_entry("tool", t)
        win.panel.add_entry("error", "помилка")
        win.panel.add_file("a.pdf", "a.pdf", 10)
        win.panel.set_activity("💭 Думаю… (крок 2)")
        win.panel._render()
        plain = win.panel.view.toPlainText()
        emoji = re.findall("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B07\u21BB\u23F3]", plain)
        need(not emoji, f"емодзі в позначках стрічки: {emoji}")
        imgs = re.findall(r'src="kru-icon:(\w+)"', win.panel.view.toHtml())
        need({"tool", "memory", "delegate", "warn", "file", "download"} <= set(imgs), f"іконки: {imgs}")
        need(imgs[0] == "file", f"вкладення в бульбашці без іконки: {imgs[:2]}")
        need("<img" in win.panel.activity.text() and "💭" not in win.panel.activity.text(), "рядок стану без іконки")
        win.new_chat()
        return sorted(set(imgs))

    step(50, lambda: check("feed_icons", feed_icons_flow))

    def my_messages_flow():
        """Мої повідомлення — бульбашки з фоном праворуч; Alt+↑ / Alt+↓ — перехід між ними в довгому чаті."""
        p = win.panel
        win.new_chat()
        for n in range(6):
            p.add_entry("user", f"Питання номер {n} <b>не тег</b>")
            p.add_entry("assistant", "Довга відповідь.\n\n" + "\n\n".join(f"Абзац {k} відповіді." for k in range(8)))
        p._render()
        h = p.view.toHtml()
        need(h.count("#dcebff") >= 6 or h.count("#DCEBFF") >= 6, "немає фону в моїх повідомленнях")
        need("&lt;b&gt;не тег" in h, "текст повідомлення не екранований")
        sb = p.view.verticalScrollBar()
        sb.setValue(sb.maximum())
        start = sb.value()
        need(p.jump_to_mine(-1) and sb.value() < start, f"Alt+↑ не перейшов: {start} → {sb.value()}")
        up = sb.value()
        need(p.jump_to_mine(-1) and sb.value() < up, "другий Alt+↑ не перейшов вище")
        need(p.jump_to_mine(1) and sb.value() > sb.minimum(), "Alt+↓ не перейшов нижче")
        sb.setValue(0)
        need(not p.jump_to_mine(-1), "вище першого повідомлення нікуди")
        win.new_chat()
        return "ok"

    step(50, lambda: check("my_messages", my_messages_flow))

    def resize_font_flow():
        """Тягнути вікно чату за край і міняти шрифт: усе запам'ятовується; подвійний клік — як було."""
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent

        panel, card = win.panel, win.panel.card

        def mouse(kind, local, glob):
            btn = Qt.MouseButton.LeftButton
            ev = QMouseEvent(kind, QPointF(local), QPointF(glob), btn,
                             btn if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton,
                             Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(card, ev)

        def drag(local, dx, dy):
            g = card.mapToGlobal(local)
            mouse(QEvent.Type.MouseButtonPress, local, g)
            mouse(QEvent.Type.MouseMove, local, g + QPoint(dx, dy))
            mouse(QEvent.Type.MouseButtonRelease, local, g + QPoint(dx, dy))

        from PySide6.QtCore import QPoint

        win.new_chat()
        panel.add_entry("user", "привіт")
        panel.add_entry("assistant", "Відповідь")
        panel._render()
        w0 = panel.panel_width
        drag(QPoint(card.width() - 2, card.height() // 2), 80, 0)  # правий край — ширше
        need(panel.panel_width == w0 + 80 and win.settings.panel_width == w0 + 80, f"ширина {panel.panel_width}")
        need(panel.width() == w0 + 80, "вікно не змінило ширину")
        drag(QPoint(card.width() // 2, card.height() - 2), 0, 150)  # нижній край — вища стрічка
        need(win.settings.panel_feed_height > 0 and panel.view.height() == panel.feed_height,
             f"висота стрічки {panel.view.height()} / {panel.feed_height}")
        panel.set_font_size(18, user=True)
        need(win.settings.font_size == 18 and panel.view.icon_px == 20, "шрифт не застосовано")
        need("18px" in panel.styleSheet(), "стиль без нового розміру шрифту")
        mouse(QEvent.Type.MouseButtonDblClick, QPoint(card.width() - 2, 40), card.mapToGlobal(QPoint(card.width() - 2, 40)))
        need(panel.panel_width == panel.WIDTH and panel.feed_height == 0 and win.settings.panel_feed_height == 0,
             "подвійний клік не повернув розмір")
        panel.set_font_size(13, user=True)
        win.new_chat()
        return {"font": win.settings.font_size, "width": win.settings.panel_width}

    step(50, lambda: check("resize_font", resize_font_flow))

    def draft_visible_flow():
        """Відповідь під час набору видно повністю (раніше Qt-Markdown викидав текст разом із <br> в HTML)."""
        win.new_chat()
        win.set_show_thinking(True)
        win.panel.add_entry("user", "питання")
        win.on_event({"type": "thinking", "text": "перший рядок думки\nдругий <рядок> & ще"})
        win.on_event({"type": "token", "text": "## Огляд\n\n| a | b |\n|---|---|\n- **пункт** один\n> цитата"})
        win.panel._render()
        plain = win.panel.view.toPlainText()
        for piece in ("перший рядок думки", "другий <рядок> & ще", "## Огляд", "| a | b |", "- **пункт** один"):
            need(piece in plain, f"не видно «{piece}»: {plain!r}")
        win.on_event({"type": "final", "text": "## Огляд\n\n- **пункт** один"})
        win.panel._render()
        need("**" not in win.panel.view.toPlainText(), "готова відповідь не відформатована")
        win.set_show_thinking(False)
        win.new_chat()
        return "ok"

    step(50, lambda: check("draft_visible", draft_visible_flow))

    def audio_helpers():
        import array
        import struct

        from .audio import envelope, make_wav, peak, to_mono16

        stereo = struct.pack("<4f", 0.5, -1.0, -0.25, 1.0)  # 2 кадри × 2 канали, float32
        mono = array.array("h", to_mono16(stereo, "Float", 2))
        need(list(mono) == [16383, -8191], f"float→int16: {list(mono)}")
        need(len(to_mono16(bytes([128, 255]), "UInt8", 1)) == 4, "uint8")
        loud = array.array("h", [0] * 1600 + [20000, -20000] * 800).tobytes()  # 0,1 с тиші, 0,1 с звуку
        levels, ms = envelope(make_wav(loud, 16000))
        need(ms == 40 and levels[0] < 0.05 and max(levels) > 0.9, f"обвідна: {levels}")
        need(peak(array.array("h", [0, 32767]).tobytes()) > 0.9 and peak(b"") == 0.0, "пік")
        return f"{len(levels)} кадрів"

    check("audio_helpers", audio_helpers)

    def mic_graceful():
        """Справжній мікрофон: на машині без нього (CI) — зрозуміла помилка, а не падіння."""
        from .audio import Recorder

        rec = Recorder()
        errors: list[str] = []
        rec.failed.connect(errors.append)
        ok = rec.start()
        rec.stop(send=False)
        need(ok or errors, "ні запису, ні пояснення")
        return "записує" if ok else errors[0]

    def voice_hold_start():
        """Утримання персонажа → «слухаю»; відпустив → запис іде на сервер, сказане стає задачею."""
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest

        from .audio import Recorder, make_wav

        class FakeRecorder(Recorder):
            on = False

            @property
            def active(self):
                return self.on

            def start(self):
                self.on, self.level = True, 0.6
                return True

            def stop(self, send=True):
                self.on, self.level = False, 0.0
                self.finished.emit(make_wav(b"\0\0" * 16000, 16000))

        win.new_chat()
        win._set_voice_caps(None)  # сервер без голосу — утримання працює як звичайний клік
        need(not win.robot.hold_enabled and win.panel.btn_mic.isHidden(), "голос увімкнено без сервера")
        state["voice_sent"] = sent = []
        state["voice_orig"] = (win.conn.send, win.recorder, win.player.play)
        win.conn.send = lambda msg: sent.append(msg) or True
        win.recorder = FakeRecorder(win)
        win.recorder.finished.connect(win._on_recorded)
        win.recorder.failed.connect(win._on_record_failed)
        win.settings.voice_reply = "voice"
        win._set_voice_caps({"stt": True, "tts": True})
        need(win.robot.hold_enabled and not win.panel.btn_mic.isHidden(), "кнопки мікрофона немає")
        win.busy = False
        win.robot.set_state("idle")
        center = QPoint(win.robot.width() // 2, win.robot.height() // 2)
        QTest.mousePress(win.robot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        return "ok"

    def voice_hold_release():
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest

        from .audio import make_wav

        sent = state["voice_sent"]
        win.on_disconnected("перевірка", "")  # обрив зв'язку під час запису не збиває «слухаю»
        need(win.robot.state == "listening", f"стан: {win.robot.state}")
        need(win.panel.btn_mic.property("recording") is True, "кнопка не показує запис")
        need(win.robot.voice_level > 0, "персонаж не чує гучності")
        center = QPoint(win.robot.width() // 2, win.robot.height() // 2)
        QTest.mouseRelease(win.robot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        vin = next((m for m in sent if m.get("type") == "voice_in"), None)
        need(vin is not None and len(vin["data"]) > 1000, f"запис не надіслано: {[m.get('type') for m in sent]}")
        need(win.robot.state == "thinking" and win.panel.shown is not None, f"стан: {win.robot.state}")
        win.on_event({"type": "voice_text", "id": vin["id"], "text": "яка погода в Києві"})
        task = next((m for m in sent if m.get("type") == "task"), None)
        need(task and task["text"] == "яка погода в Києві", f"задача: {task}")
        win.on_event({"type": "final", "text": "У Києві **сонячно**."})
        win.on_event({"type": "status", "state": "idle"})
        tts = next((m for m in sent if m.get("type") == "tts"), None)
        need(tts and "сонячно" in tts["text"], f"озвучення не попрошено: {tts}")
        need((tts["voice"], tts["effect"], tts["speed"]) == ("", "natural", 1.0), f"параметри голосу: {tts}")
        played = []

        def fake_play(wav, volume=1.0):
            played.append(volume)
            win.player.playing = True
            return True

        win.player.play = fake_play
        wav = make_wav(b"\0\0" * 8000, 16000)
        import base64

        need(tts.get("stream") is True, "озвучення не частинами")
        data = base64.b64encode(wav).decode()
        # відповідь частинами: друга приходить, поки звучить перша, — чекає черги
        win.on_event({"type": "tts_audio", "id": tts["id"], "format": "wav", "part": 0, "last": False, "data": data})
        need(played and win.robot.state == "talking", f"не говорить: {win.robot.state}")
        win.on_event({"type": "tts_audio", "id": tts["id"], "format": "wav", "part": 1, "last": False, "data": data})
        need(len(played) == 1, "друга частина перебила першу")
        win.player.stop()
        need(len(played) == 2 and win.robot.state == "talking", f"друга частина: {len(played)}, {win.robot.state}")
        win.player.stop()  # третя ще синтезується — робот «говорить» далі
        need(win.robot.state == "talking", f"не дочекався третьої частини: {win.robot.state}")
        win.on_event({"type": "tts_audio", "id": "чуже", "format": "wav", "part": 0, "last": True, "data": data})
        need(len(played) == 2, "зіграв чуже озвучення")
        win.on_event({"type": "tts_audio", "id": tts["id"], "format": "wav", "part": 2, "last": True, "data": data})
        need(len(played) == 3, "третя частина не зіграла")
        win.player.stop()
        need(win.robot.state == "happy", f"після озвучення: {win.robot.state}")
        # текстова задача — без озвучення (режим «коли питаю голосом»)
        sent.clear()
        win.send_task("а завтра?")
        win.on_event({"type": "final", "text": "Дощ."})
        win.on_event({"type": "status", "state": "idle"})
        need(not any(m.get("type") == "tts" for m in sent), "озвучено текстову відповідь")
        return "ok"

    def voice_settings_flow():
        """Налаштування: голос/звучання/темп від сервера, «▶ Прослухати» — пробна фраза саме з ними."""
        sent = state["voice_sent"]
        sent.clear()
        win._set_voice_caps({"stt": True, "tts": True, "engine": "holos",
                             "voices": ["Speaker_67", "Гаська Шиян", "Speaker_0"],
                             "effects": {"natural": "Звичайний", "robot": "М'який робот", "droid": "Маленький дроїд"}})
        dlg = appmod.SettingsDialog(win.settings, voice_caps=win.voice_caps, preview=win.preview_voice)
        need(dlg.voice_effect.count() == 3 and dlg.voice_name.count() == 4, "списки голосів")  # + «типовий»
        need(dlg.voice_effect.currentData() == "natural", f"типове звучання: {dlg.voice_effect.currentData()}")
        need(dlg.voice_name.itemText(1) == "Голос 67", f"назва голосу: {dlg.voice_name.itemText(1)}")
        dlg.voice_name.setCurrentIndex(dlg.voice_name.findData("Гаська Шиян"))
        dlg.voice_effect.setCurrentIndex(dlg.voice_effect.findData("droid"))
        dlg.voice_speed.setValue(1.2)
        dlg.voice_volume.setValue(40)
        need(dlg.btn_preview.isEnabled(), "кнопка «Прослухати» вимкнена")
        win.busy = False
        dlg.btn_preview.click()
        tts = next((m for m in sent if m.get("type") == "tts"), None)
        need(tts and (tts["voice"], tts["effect"], tts["speed"]) == ("Гаська Шиян", "droid", 1.2), f"пробна фраза: {tts}")
        need(win._speech_volume == 40 and win.settings.voice_sound == "natural", "налаштування змінились до OK")
        dlg.deleteLater()
        old = appmod.SettingsDialog(win.settings, voice_caps={"stt": False, "tts": False})
        need(not old.btn_preview.isEnabled() and old.voice_effect.count() == 5, "без голосу на сервері")
        old.deleteLater()
        return "ok"

    def voice_cleanup():
        send, rec, play = state.pop("voice_orig")
        win.conn.send, win.recorder = send, rec
        win.player.play = play
        win._set_voice_caps(None)
        win.busy = False
        win.panel.set_busy(False)
        win.new_chat()

    step(50, lambda: check("mic_graceful", mic_graceful))
    step(50, lambda: check("voice_hold", voice_hold_start))
    step(700, lambda: check("voice_roundtrip", voice_hold_release))
    step(50, lambda: check("voice_settings", voice_settings_flow))

    def wake_logic_check():
        """Логіка «Кру» на синтетичному звуці: фраза → запис до паузи; уточнення; тайм-аут."""
        import array
        import math as _m

        from .wake import WakeLogic

        rate = 16000

        def tone(sec):
            return array.array("h", [int(8000 * _m.sin(2 * _m.pi * 220 * i / rate)) for i in range(int(rate * sec))]).tobytes()

        def quiet(sec):
            return b"\0\0" * int(rate * sec)

        class FakeDet:  # «чує» фразу після ~0,3 с гучного звуку (on=False — у звуці фрази немає)
            threshold = 0.5
            loud = 0
            on = True

            def reset(self):
                self.loud = 0

            def feed(self, frame):
                self.loud += max(abs(x) for x in array.array("h", frame)) > 2000
                return 1.0 if self.on and self.loud >= 10 else 0.0

        lg = WakeLogic(rate, FakeDet())
        need(lg.feed(quiet(1.0)) == [], "тиша щось викликала")
        ev = lg.feed(tone(2.0))
        need([e[0] for e in ev] == ["wake"], f"фраза: {ev}")
        ev = lg.feed(quiet(1.5))
        need(len(ev) == 1 and ev[0][0] == "utterance" and ev[0][2] is True, f"фраза: {[e[0] for e in ev]}")
        need(3.0 < (len(ev[0][1]) - 44) / 2 / rate < 3.6, "довжина запису (з початком фрази)")
        need(lg.feed(tone(1.0)) == [], "після фрази чекає на застосунок")
        lg.detector.on = False  # уточнення нижче — без «Чуєш, Кру»
        lg.await_command(0.5)
        need(lg.feed(quiet(0.8)) == [("timeout",)], "тайм-аут уточнення")
        lg.await_command(3)
        ev = lg.feed(quiet(0.3) + tone(1.0) + quiet(1.5))
        need([(e[0], e[2] if len(e) > 2 else None) for e in ev] == [("speech", None), ("utterance", False)],
             f"уточнення: {ev}")
        lg.await_command(3)
        ev = lg.feed(tone(0.2) + quiet(1.5))  # стук чи шум — не уточнення
        need([e[0] for e in ev] == ["speech"] and lg.state == "await", f"шум як уточнення: {ev}, {lg.state}")
        ev = lg.feed(tone(1.0) + quiet(1.5))
        need([(e[0], e[2] if len(e) > 2 else None) for e in ev] == [("speech", None), ("utterance", False)],
             f"уточнення після шуму: {ev}")
        lg.pause()
        need(lg.feed(tone(2.0)) == [], "на паузі слухає")
        lg = WakeLogic(rate, FakeDet())
        need([e[0] for e in lg.feed(tone(16.0))] == ["wake", "utterance"], "задовга фраза не обрізалась")
        lg.feed(quiet(2.0))
        lg.listen()
        # тихі склади посеред фрази (гучніші за шум, але тихіші за поріг початку) не рвуть її
        soft = array.array("h", [int(420 * _m.sin(2 * _m.pi * 220 * i / rate)) for i in range(int(rate * 1.5))]).tobytes()
        ev = lg.feed(tone(0.5) + soft + tone(0.3) + quiet(1.5))
        need([e[0] for e in ev] == ["wake", "utterance"] and (len(ev[-1][1]) - 44) / 2 / rate > 2.2,
             f"фраза обірвалась на тихому: {[(e[0], (len(e[1]) - 44) / 2 / rate if len(e) > 1 else 0) for e in ev]}")
        # команди інтерфейсу, що запізнилися, не скасовують запис питання, який уже йде
        lg = WakeLogic(rate, FakeDet())
        need([e[0] for e in lg.feed(quiet(1.0) + tone(0.4))] == ["wake"], "фраза")
        lg.listen()
        lg.barge()
        lg.await_command(6)
        ev = lg.feed(tone(1.0) + quiet(1.5))
        need(lg.state == "busy" and [(e[0], e[2]) for e in ev] == [("utterance", True)],
             f"команда скасувала запис: {[e[0] for e in ev]}, {lg.state}")
        # уточнення, що почалося з «Чуєш, Кру», — з фразою (сервер прибере звертання)
        lg.await_command(3)
        ev = lg.feed(tone(1.0) + quiet(1.5))
        need([(e[0], e[2] if len(e) > 2 else None) for e in ev] == [("speech", None), ("utterance", True)],
             f"уточнення з фразою: {ev}")
        # «Чуєш, Кру» … пауза … питання: саму фразу не відправляє, чекає на питання
        lg = WakeLogic(rate, FakeDet())
        ev = lg.feed(quiet(1.0) + tone(0.4))
        need([e[0] for e in ev] == ["wake"], f"фраза: {ev}")
        need(lg.feed(quiet(2.5)) == [], "відправив саму фразу, не дочекавшись питання")
        ev = lg.feed(tone(1.0) + quiet(1.5))
        need([e[0] for e in ev] == ["utterance"] and ev[0][2] is True and (len(ev[0][1]) - 44) / 2 / rate > 3.5,
             f"питання після паузи: {[(e[0], (len(e[1]) - 44) / 2 / rate if len(e) > 1 else 0) for e in ev]}")
        lg.listen()
        lg.feed(tone(0.4))
        need(lg.feed(quiet(5.5)) == [("timeout",)], "фраза без питання — мовчки назад до чекання")
        # після питання загудів фон (вентилятор, телевізор): запис не тягнеться до 15 с
        lg = WakeLogic(rate, FakeDet())
        hum = array.array("h", [int(850 * _m.sin(2 * _m.pi * 120 * i / rate)) for i in range(int(rate * 4))]).tobytes()
        ev = lg.feed(quiet(1.0) + tone(1.5) + hum)
        need([e[0] for e in ev] == ["wake", "utterance"], f"фон затягнув запис: {[e[0] for e in ev]}, фон {lg.floor:.0f}")
        return "ok"

    check("wake_logic", wake_logic_check)

    def hacker_theme_check():
        """Хакерська тема: темна панель, код наживо в «терміналі» (шматками JSON, як від сервера), вивід
        команди під ним; Кру чорний і «програмує»; назад до світлої — термінал зникає."""
        import json as _json

        theme = win.settings.theme
        term = win.panel.terminal
        try:
            win.settings.theme = "hacker"
            win.apply_theme()
            need("#00FF66" in win.panel.styleSheet() and win.robot.hacker, "тема не застосувалась")
            args = _json.dumps({"path": "app.py", "content": 'print("привіт")\nx = 1\n'}, ensure_ascii=False)
            for i in range(0, len(args), 7):  # шматками по 7 символів — посеред слів і екранувань
                win.on_event({"type": "tool_stream", "index": 0, "name": "write_file", "delta": args[i:i + 7]})
            need(term.isVisibleTo(win.panel) and win.robot.coding, "термінал не показався")
            need(term.plain() == '✎ app.py\nprint("привіт")\nx = 1\n', f"код у терміналі: {term.plain()!r}")
            win.on_event({"type": "tool_result", "id": "1", "name": "write_file", "ok": True, "preview": "Записано",
                          "output": "Записано 22 символів у app.py"})
            win.on_event({"type": "status", "state": "thinking", "step": 2})  # наступний крок моделі
            shell = _json.dumps({"command": "python app.py"})
            win.on_event({"type": "tool_stream", "index": 0, "name": "run_shell", "delta": shell})
            win.on_event({"type": "tool_call", "id": "2", "name": "run_shell", "args": {"command": "python app.py"},
                          "summary": "python app.py"})  # уже показано шматками — вдруге не дописує
            win.on_event({"type": "tool_result", "id": "2", "name": "run_shell", "ok": True, "preview": "привіт",
                          "output": "привіт\n"})
            need(term.plain().endswith("$ python app.py\n── вивід ──\nпривіт\n"), f"вивід: {term.plain()[-80:]!r}")
            # без потоку шматками (Ollama, JSON-режим) — код показується цілим із самого виклику
            win.on_event({"type": "status", "state": "thinking", "step": 3})
            win.on_event({"type": "tool_call", "id": "3", "name": "run_python", "args": {"code": "print(2 + 2)"},
                          "summary": "print(2 + 2)"})
            need(term.plain().endswith("▶ python\nprint(2 + 2)\n"), f"цілий виклик: {term.plain()[-60:]!r}")
            win.on_event({"type": "status", "state": "idle"})
            need(not win.robot.coding, "Кру «програмує» й після кінця задачі")
        finally:
            win.settings.theme = theme
            win.apply_theme()
        need(not term.isVisibleTo(win.panel) and not win.robot.hacker, "світла тема лишила термінал/матрицю")
        return "ok"

    check("hacker_theme", hacker_theme_check)

    def vad_check():
        """Детектор голосу: модель із пакета вантажиться; гул, шум і клацання — не мова; запис закінчується,
        щойно стих голос, хоч фон і гучний."""
        import array
        import math as _m

        from . import vad as vadmod
        from .wake import WakeLogic

        rate = 16000
        detail = "немає numpy/onnxruntime — лише логіка"
        if not __import__("kru_robot.wakeword", fromlist=["available"]).available():
            need(vadmod.MODEL.is_file(), f"немає моделі {vadmod.MODEL}")
            v = vadmod.SileroVad()
            hum = array.array("h", [int(3000 * _m.sin(2 * _m.pi * 120 * i / rate)) for i in range(rate * 3)])
            noise = array.array("h", [(i * 7919 % 4001) - 2000 for i in range(rate * 3)])  # «білий» шум
            clicks = array.array("h", [20000 if i % 4000 == 0 else 0 for i in range(rate * 3)])
            for name, a in (("гул", hum), ("шум", noise), ("клацання", clicks)):
                v.reset()
                pcm = a.tobytes()
                top = max(v.feed(pcm[i:i + 960]) for i in range(0, len(pcm), 960))
                need(top < 0.5, f"{name} — схоже на мову: {top:.2f}")
            detail = "ok"

        class FakeVad:  # «голос» — гучний тон, решта — ні
            def feed(self, frame):
                return 0.9 if max(abs(x) for x in array.array("h", frame)) > 2000 else 0.05

        class FakeDet:
            threshold = 0.5
            loud = 0
            on = True

            def reset(self):
                self.loud = 0

            def feed(self, frame):
                self.loud += max(abs(x) for x in array.array("h", frame)) > 2000
                return 1.0 if self.on and self.loud >= 10 else 0.0

        def tone(sec, amp=8000):
            return array.array("h", [int(amp * _m.sin(2 * _m.pi * 220 * i / rate)) for i in range(int(rate * sec))]).tobytes()

        lg = WakeLogic(rate, FakeDet(), FakeVad())
        ev = lg.feed(b"\0\0" * rate + tone(1.5))
        need([e[0] for e in ev] == ["wake"], f"фраза з детектором голосу: {ev}")
        hum = tone(3.0, amp=1500)  # гучний гул (гучніший за поріг тиші), але не голос
        ev = lg.feed(hum[: int(rate * 1.4) * 2])
        need([e[0] for e in ev] == ["utterance"], f"гул затягнув запис: {[e[0] for e in ev]}")
        # перебивання: поки робот говорить — лише фраза, у запис іде те, що після неї
        lg = WakeLogic(rate, FakeDet(), FakeVad())
        lg.barge()
        lg.detector.on = False
        need(lg.feed(tone(2.0)) == [], "записав голос самого робота")  # «говорить сам робот»
        lg.detector.on = True
        lg.detector.reset()
        ev = lg.feed(tone(0.4))
        need([e[0] for e in ev] == ["wake"] and lg.state == "capture", f"перебивання: {ev}, {lg.state}")
        ev = lg.feed(tone(1.0) + b"\0\0" * int(rate * 1.5))
        need([e[0] for e in ev] == ["utterance"] and (len(ev[0][1]) - 44) / 2 / rate < 3.3,
             f"у запис потрапив голос робота: {[(e[0], (len(e[1]) - 44) / 2 / rate if len(e) > 1 else 0) for e in ev]}")
        return detail

    check("vad", vad_check)

    def wakeword_check():
        """Своя фраза: потоковий детектор (заглушки мереж відбитків) і його місце в логіці слухання."""
        from . import wakeword as wwmod

        if wwmod.available():
            return "немає numpy/onnxruntime — пропущено"
        import numpy as np

        from .wake import WakeLogic

        calls = {"mel": 0, "emb": 0}

        def mel(x):
            calls["mel"] += 1
            need(x.shape == (1, 1760), f"шматок для спектрограми: {x.shape}")
            return np.full((1, 1, 8, 32), float(np.abs(x).mean() > 1000), np.float32)

        def emb(x):
            calls["emb"] += 1
            need(x.shape == (1, 76, 32, 1), f"вікно для відбитка: {x.shape}")
            return np.full((1, 1, 1, 96), (float(x[0, -8:].mean()) - 2) * 10, np.float32)  # спектр мав /10 + 2

        rng = np.random.default_rng(0)
        d = 16 * 96
        params = {"w1": np.ones((d, 4), np.float32) / d, "b1": np.zeros(4, np.float32),
                  "w2": np.eye(4, dtype=np.float32), "b2": np.zeros(4, np.float32),
                  "w3": np.ones((4, 1), np.float32) * 20, "b3": np.array([-6.0], np.float32),
                  "mean": np.zeros(d, np.float32), "std": np.ones(d, np.float32), "threshold": np.float32(0.6)}
        det = wwmod.Detector(mel=mel, emb=emb, params=params, sensitivity="high")
        need(abs(det.threshold - 0.45) < 1e-6, f"чутливість зсуває поріг: {det.threshold}")
        loud = (np.sin(np.arange(16000) / 3) * 8000).astype("<i2").tobytes()
        quiet = bytes(32000)
        need(det.feed(quiet[:2000]) == 0.0, "рахує, не дочекавшись 80 мс")
        need(det.feed(quiet) < 0.1, "тиша — не фраза")
        scores = [det.feed(loud[i:i + 960]) for i in range(0, len(loud), 960)]
        need(max(scores) > 0.9, f"фраза не впізнана: {max(scores):.2f}")
        det.reset()
        need(det.feed(quiet[:2560]) < 0.1, "після reset пам'ятає стару фразу")
        lg = WakeLogic(16000, det)
        ev = lg.feed(bytes(16000) + loud)
        need([e[0] for e in ev] == ["wake"], f"фраза → wake: {ev}")
        lg.listen()
        need(lg.feed(bytes(3200)) == [], "після listen спрацював на давню фразу")
        _ = rng
        return "ok"

    check("wakeword", wakeword_check)

    def double_click_check():
        """Клік відкриває панель (з паузою ~0,3 с), подвійний — режим «Кру», а не два кліки."""
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest

        got = []
        r = win.robot
        r.clicked.connect(lambda: got.append("click"))
        r.double_clicked.connect(lambda: got.append("double"))
        orig_toggle, orig_click = win.toggle_wake, win.on_robot_clicked
        r.clicked.disconnect(win.on_robot_clicked)
        r.double_clicked.disconnect(win.toggle_wake)
        try:
            c = QPoint(r.width() // 2, r.height() // 2)
            QTest.mouseClick(r, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c)
            QTest.mouseClick(r, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c)
            QTest.qWait(400)
            need(got == ["double"], f"подвійний: {got}")
            got.clear()
            QTest.mouseClick(r, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c)
            need(got == [], "одиночний клік спрацював, не дочекавшись другого")
            QTest.qWait(400)
            need(got == ["click"], f"одиночний: {got}")
        finally:
            r.clicked.connect(orig_click)
            r.double_clicked.connect(orig_toggle)
        return "ok"

    def wake_flow():
        """«Чуєш, Кру, яка погода?»: модель фрази — із сервера, фразу шукає ПК, сервер прибирає звертання →
        задача → відповідь голосом (навіть коли «відповідати голосом: ні») → уточнення без фрази;
        хибне спрацювання — мовчки; подвійний клік вимикає; удруге модель не забирає."""
        import base64 as _b64
        import tempfile
        from pathlib import Path

        from PySide6.QtCore import QObject, Signal

        from . import wakeword as wwmod
        from .audio import make_wav

        class FakeWake(QObject):
            woke = Signal()
            speech = Signal()
            utterance = Signal(bytes, bool)
            timeout = Signal()
            failed = Signal(str)
            ready = Signal()

            def __init__(self):
                super().__init__()
                self.active, self.level, self.calls = False, 0.3, []

            def start(self, phrase_model, sens):
                self.active, self.phrase_model = True, phrase_model
                self.calls.append(("start", sens))
                return True

            def stop(self):
                self.active = False
                self.calls.append(("stop",))

            def listen(self):
                self.calls.append(("listen",))

            def await_command(self, sec):
                self.calls.append(("await", sec))

            def pause(self):
                self.calls.append(("pause",))

            def barge(self):
                self.calls.append(("barge",))

        sent, played = [], []
        orig = (win.conn.send, win.wake, win.player.play, win.chime.play)
        ww_orig = (wwmod.folder, wwmod.available)
        tmp = Path(tempfile.mkdtemp(prefix="kru-ww-"))
        wwmod.folder, wwmod.available = (lambda: tmp), (lambda: "")
        win.conn.send = lambda msg: sent.append(msg) or True
        fake = FakeWake()
        win._wire_wake(fake)
        win.chime.play = lambda wav, volume=1.0: played.append("chime") or True

        def fake_play(wav, volume=1.0):
            played.append("speech")
            win.player.playing = True
            win.player.started.emit()
            return True

        win.player.play = fake_play
        voice_reply = win.settings.voice_reply
        win.stop_speech(quiet=True)  # озвучення з попередніх перевірок не має змішуватися з цією
        QTest_wait(400)
        try:
            win.new_chat()
            win.busy = False
            win.settings.voice_reply = "off"  # у режимі «Кру» відповідь однаково голосом
            win._set_voice_caps({"stt": True, "tts": True})
            win.robot.double_clicked.emit()
            need(not fake.active and not win.robot.wake_mode, "увімкнувся без навченої фрази")
            win._set_voice_caps({"stt": True, "tts": True, "wakeword": {"phrase": "Чуєш, Кру", "version": 5}})
            sent.clear()
            win.robot.double_clicked.emit()
            need(sent and sent[-1] == {"type": "wake_model"} and not fake.active, f"модель фрази: {sent[-1:]}")
            b64 = lambda data: _b64.b64encode(data).decode()  # noqa: E731
            win.on_event({"type": "wake_model", "phrase": "Чуєш, Кру", "version": 5, "threshold": 0.6,
                          "files": {"model": b64(b"npz"), "melspectrogram": b64(b"mel"), "embedding": b64(b"emb")}})
            need(fake.active and fake.phrase_model == tmp and wwmod.cached_version() == 5,
                 f"запуск зі своєю фразою: {fake.calls[-1:]}, {wwmod.cached_version()}")
            need(win.robot.wake_mode and win.act_wake.isChecked(), "режим не ввімкнувся")
            need(win.settings.wake_enabled, "не запам'ятав увімкнення")
            fake.woke.emit()
            need(win.robot.state == "listening" and "chime" in played, f"почув фразу: {win.robot.state}")
            fake.utterance.emit(b"RIFF-not-really", True)
            vin = sent[-1]
            need(vin["type"] == "voice_in" and vin["wake"] == "detected", f"запит: {vin}")
            win.on_event({"type": "voice_text", "id": vin["id"], "text": "", "ignored": True})
            need(fake.calls[-1] == ("listen",) and not any(m["type"] == "task" for m in sent), "хибне спрацювання")
            fake.utterance.emit(b"RIFF", True)
            win.on_event({"type": "voice_text", "id": sent[-1]["id"], "text": "", "wake_only": True})
            need(win.robot.state == "listening", "«Чуєш, Кру…» з паузою — має слухати питання")
            QTest_wait(400)
            need(fake.calls[-1] == ("await", 6), f"чекає питання: {fake.calls[-1]}")
            fake.utterance.emit(b"RIFF", False)
            need(sent[-1].get("wake") == "followup", "уточнення без фрази")
            win.on_event({"type": "voice_text", "id": sent[-1]["id"], "text": "Яка сьогодні погода?"})
            task = sent[-1]
            need(task["type"] == "task" and task["text"] == "Яка сьогодні погода?", f"задача: {task}")
            win.on_event({"type": "final", "text": "Сонячно, +12."})
            win.on_event({"type": "status", "state": "idle"})
            tts = sent[-1]
            need(tts["type"] == "tts", "відповідь не озвучується")
            win.on_event({"type": "tts_audio", "id": tts["id"], "format": "wav",
                          "data": _b64.b64encode(make_wav(b"\0\0" * 800, 16000)).decode()})
            need(fake.calls[-1] == ("barge",), f"поки говорить — лише чекає фразу: {fake.calls[-1:]}")
            need(win.panel.btn_mute.isVisibleTo(win.panel), "немає кнопки «Замовкни»")
            win.player.stop()
            QTest_wait(400)
            need(fake.calls[-1] == ("await", 6), f"уточнення після відповіді: {fake.calls[-1]}")
            need(not win.panel.btn_mute.isVisibleTo(win.panel), "кнопка «Замовкни» лишилась")
            # «Чуєш, Кру» посеред відповіді — замовкає й слухає; «стоп» — без задачі
            fake.utterance.emit(b"RIFF", False)
            win.on_event({"type": "voice_text", "id": sent[-1]["id"], "text": "А завтра?"})
            win.on_event({"type": "final", "text": "Завтра дощ."})
            win.on_event({"type": "status", "state": "idle"})
            win.on_event({"type": "tts_audio", "id": sent[-1]["id"], "format": "wav", "last": False,
                          "data": _b64.b64encode(make_wav(b"\0\0" * 800, 16000)).decode()})
            need(win.player.playing, "друга відповідь не звучить")
            fake.woke.emit()
            QTest_wait(400)
            need(not win.player.playing and sent[-1]["type"] == "tts_stop", f"не замовк: {sent[-1]}")
            need(fake.calls[-1] == ("barge",), f"перебили — а він повернувся до чекання: {fake.calls[-1:]}")
            fake.utterance.emit(b"RIFF", True)
            need(sent[-1].get("wake") == "detected", f"після перебивання: {sent[-1]}")
            n_sent = len(sent)
            win.on_event({"type": "voice_text", "id": sent[-1]["id"], "text": "Стоп."})
            need(len(sent) == n_sent and fake.calls[-1] == ("listen",), f"«стоп» став задачею: {sent[n_sent:]}")
            # запис пішов на сервер, а зв'язок упав — не глухне
            fake.woke.emit()
            fake.utterance.emit(b"RIFF", True)
            need(win._wake_pending, "запис не пішов на сервер")
            fake.calls.clear()
            win.on_disconnected("тест")
            need(not win._wake_pending and fake.calls == [("listen",)], f"після обриву: {fake.calls}")
            # слухач упав сам (мікрофон відключили) — режим вимикається повністю, і після перезапуску теж
            fake.active = False
            fake.failed.emit("мікрофон зник")
            need(not win.robot.wake_mode and not win.settings.wake_enabled and not win.act_wake.isChecked(),
                 "збій слухача лишив режим увімкненим")
            win.robot.double_clicked.emit()  # знову ввімкнути для перевірок нижче
            need(fake.active and win.robot.wake_mode, "не ввімкнувся знову після збою")
            win.robot.double_clicked.emit()
            need(not fake.active and not win.robot.wake_mode and not win.act_wake.isChecked(), "не вимкнувся")
            need(not win.settings.wake_enabled, "вимкнення не запам'яталось")
            sent.clear()
            win.robot.double_clicked.emit()  # модель уже є (та сама версія) — без запиту до сервера
            need(fake.active and not any(m.get("type") == "wake_model" for m in sent), "забирав модель удруге")
            win.robot.double_clicked.emit()
        finally:
            wwmod.folder, wwmod.available = ww_orig
            win.conn.send = orig[0]
            win._wire_wake(orig[1])
            win.player.play, win.chime.play = orig[2], orig[3]
            win.settings.voice_reply = voice_reply
            win.settings.wake_enabled = False
            win.robot.wake_mode = False
            win.busy = False
            win.panel.set_busy(False)
            win.new_chat()
        return "ok"

    def QTest_wait(ms):
        from PySide6.QtTest import QTest

        QTest.qWait(ms)

    def listener_start():
        """Справжній WakeListener (потік + черга подій) зі штучним мікрофоном і детектором фрази:
        модель вантажиться у фоні, інтерфейс не завмирає, фраза → сигнал woke, стоп — без падіння."""
        import array
        import math as _m

        from PySide6.QtCore import QByteArray, QObject, Signal

        from . import audio as audiomod
        from . import wake as wakemod
        from . import vad as vadmod
        from . import wakeword as wwmod

        rate = 16000

        class FakeIO(QObject):
            readyRead = Signal()

            def __init__(self):
                super().__init__()
                self.buf = b""

            def readAll(self):
                data, self.buf = self.buf, b""
                return QByteArray(data)

        class FakeSource(QObject):
            def __init__(self):
                super().__init__()
                self.io = FakeIO()

            def start(self):
                return self.io

            def stop(self):
                pass

        class FakeDet:
            threshold = 0.5

            def __init__(self, path, sensitivity):
                import time as _t

                _t.sleep(0.5)  # як справжня модель: вантажиться помітний час — інтерфейс має жити
                self.loud = 0

            def reset(self):
                self.loud = 0

            def feed(self, frame):
                self.loud += max(abs(x) for x in array.array("h", frame)) > 2000
                return 1.0 if self.loud >= 10 else 0.0

        src = FakeSource()
        state["listener_orig"] = (audiomod.open_input, wwmod.Detector, vadmod.load)
        audiomod.open_input = lambda parent=None, rate=16000: (src, ("Int16", 1, rate))
        wwmod.Detector = FakeDet
        vadmod.load = lambda: None  # тут мовлення — синтетичний тон, детектор голосу його справедливо не впізнає
        lst = wakemod.WakeListener()
        got = state["listener_got"] = []
        lst.ready.connect(lambda: got.append("ready"))
        lst.woke.connect(lambda: got.append("woke"))
        lst.utterance.connect(lambda wav, need: got.append(("utterance", need)))
        lst.failed.connect(lambda m: got.append(("failed", m)))
        t0 = _time.monotonic()
        need(lst.start(Path("/fake"), "medium"), "не стартував")
        need(_time.monotonic() - t0 < 0.2, "start() завмер на завантаженні моделі")
        state["listener"], state["listener_src"] = lst, src
        tone = array.array("h", [int(8000 * _m.sin(2 * _m.pi * 220 * i / rate)) for i in range(rate * 2)]).tobytes()
        state["listener_audio"] = b"\0\0" * rate + tone + b"\0\0" * int(rate * 1.6)
        return "ok"

    def listener_feed():
        src = state["listener_src"]
        need("ready" in state["listener_got"], f"модель не «завантажилась»: {state['listener_got']}")
        audio = state["listener_audio"]
        for i in range(0, len(audio), 3200):  # як мікрофон: шматками по 100 мс
            src.io.buf += audio[i:i + 3200]
            src.io.readyRead.emit()
        return "ok"

    def listener_check():
        from . import audio as audiomod
        from . import vad as vadmod
        from . import wakeword as wwmod

        lst, got = state["listener"], state["listener_got"]
        try:
            need("woke" in got and ("utterance", True) in got, f"події: {got}")
            lst.listen()
            lst.stop()
            need(not lst.active, "не зупинився")
        finally:
            audiomod.open_input, wwmod.Detector, vadmod.load = state.pop("listener_orig")
        return got

    step(50, lambda: check("wake_listener_start", listener_start))
    step(900, lambda: check("wake_listener_feed", listener_feed))
    step(900, lambda: check("wake_listener", listener_check))
    step(50, lambda: check("double_click", double_click_check))
    step(50, lambda: check("wake_flow", wake_flow))
    step(50, voice_cleanup)
    step(50, lambda: check("panel_height", overlap_flow))
    step(300, lambda: check("no_overlap", overlap_check))
    step(50, lambda: win.on_robot_clicked())
    step(600, lambda: check("panel_hide", lambda: (need(not win.panel.isVisible(), "панель не сховалась"), "ok")[-1]))

    if IS_WINDOWS:
        def hotkey_registered():
            need(win.hotkey.registered, "RegisterHotKey(Ctrl+Alt+B) не вдався — комбінація зайнята?")
            return "Ctrl+Alt+B"

        step(50, lambda: check("hotkey_registered", hotkey_registered))
        step(50, lambda: post_hotkey_for_test(int(win.winId())))
        step(700, lambda: check("hotkey_toggle", lambda: (need(win.panel.shown, "WM_HOTKEY не відкрив панель"), "ok")[-1]))

        def autostart_flow():
            set_autostart(True)
            need(autostart_enabled(), "запис у реєстрі не з'явився")
            cmd = autostart_command()
            set_autostart(False)
            need(not autostart_enabled(), "запис у реєстрі не видалився")
            return cmd

        step(50, lambda: check("autostart", autostart_flow))

    def network_check():
        need(handshakes, "робот не надіслав WebSocket-запит на тестовий сервер за 10 с")
        need(handshakes[0].startswith("GET /ws"), f"запит: {handshakes[0]}")
        need(disconnects, "немає події про розрив з'єднання")
        return {"request": handshakes[0], "disconnect": disconnects[0]}

    # чекаємо події до 10 с, а не фіксовану паузу
    def wait_network(polls_left: int = 40) -> None:
        if (handshakes and disconnects) or polls_left <= 0:
            check("network", network_check)
            run_from(network_index + 1)
        else:
            QTimer.singleShot(250, lambda: wait_network(polls_left - 1))

    network_index = len(steps)
    step(0, lambda: None)  # місце кроку мережі; далі run() передає керування wait_network

    def finish() -> None:
        ok = all(r["ok"] for r in results.values())
        payload = {"ok": ok, "platform": sys.platform, "qt_platform": app.platformName(), "results": results}
        report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for name, r in results.items():
            print(("✓" if r["ok"] else "✗"), name, "—", r["detail"], flush=True)
        if IS_WINDOWS:
            win.hotkey.unregister()
        faulthandler.cancel_dump_traceback_later()
        log("готово" if ok else "є помилки")
        app.exit(0 if ok else 1)

    def run(i: int = 0) -> None:
        if i >= len(steps):
            QTimer.singleShot(50, finish)
            return
        delay, fn = steps[i]

        def go() -> None:
            log(f"крок {i + 1}/{len(steps)}")
            if i == network_index:
                wait_network()  # сам продовжить послідовність, коли прийде подія (або вийде час)
                return
            try:
                fn()
            finally:
                run(i + 1)

        QTimer.singleShot(delay, go)

    run_from = run

    run()
    QTimer.singleShot(60000, lambda: (results.setdefault("timeout", {"ok": False, "detail": "60 с"}), finish()))
    return app.exec()
