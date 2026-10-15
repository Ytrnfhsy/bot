"""Самоперевірка робота на чистій системі (запускається в CI на Windows і Linux).

kru-robot --selftest report.json   → JSON-звіт + знімки екрана поруч, код виходу 0 — усе гаразд.
"""

from __future__ import annotations

import json
import os
import sys
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
        win.panel.show_chat()
        need(not win.panel.history_shown, "назад до чату")
        win.panel.add_entry("user", "привіт")
        win.new_chat()
        need(win.settings.session_id != current and not win.panel.entries, "новий чат не почався")
        return "ok"

    step(50, lambda: check("history_page", history_flow))

    def character_flow():
        from .dog import DogWidget

        win.set_character("dog")
        need(isinstance(win.robot, DogWidget) and win.robot.isVisible(), "таксик не з'явився")
        img = win.robot.grab().toImage()
        opaque = sum(1 for x in range(0, img.width(), 7) for y in range(0, img.height(), 7) if img.pixelColor(x, y).alpha() > 0)
        need(opaque > 50, f"таксик не намалювався ({opaque})")
        win.robot.grab().save(str(report.with_name("selftest-dog.png")))
        win.set_character("robot")
        need(type(win.robot).__name__ == "RobotWidget", "не повернувся робот")
        return {"dog_opaque_samples": opaque}

    step(50, lambda: check("character_switch", character_flow))

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

        p = overlap_state["panel"]
        view_bottom = p.view.mapTo(p, QPoint(0, 0)).y() + p.view.height()
        input_top = p.input.mapTo(p, QPoint(0, 0)).y()
        result = {"view_bottom": view_bottom, "input_top": input_top, "height": p.height(),
                  "needed": p.layout().minimumSize().height()}
        p.hide()
        p.deleteLater()
        need(view_bottom <= input_top and result["height"] >= result["needed"], f"поле вводу налазить на стрічку: {result}")
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
