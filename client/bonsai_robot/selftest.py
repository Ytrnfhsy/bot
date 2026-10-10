"""Самоперевірка робота на чистій системі (запускається в CI на Windows і Linux).

bonsai-robot --selftest report.json   → JSON-звіт + знімки екрана поруч, код виходу 0 — усе гаразд.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication


def run_selftest(app: QApplication, report_path: str) -> int:
    report = Path(report_path).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    # окремий тимчасовий профіль — не чіпаємо справжні налаштування
    tmp = tempfile.mkdtemp(prefix="bonsai-selftest-")
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

    settings = Settings.load()
    settings.server_url = "ws://127.0.0.1:9/ws"  # нікого немає — перевіряємо, що мережевий модуль живий
    win = appmod.RobotWindow(settings)
    win.show()
    state["win"] = win
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
    step(700, lambda: check("panel_show", lambda: (need(win.panel.shown, "панель не з'явилась"),
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
        win.panel.set_history([{"id": 1, "title": "Тест", "updated_at": 0, "count": 1}])
        need(win.panel.history_shown and win.panel.history_list.count() == 1, "сторінка історії")
        win.panel.show_chat()
        need(not win.panel.history_shown, "назад до чату")
        return "ok"

    step(50, lambda: check("history_page", history_flow))
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
        need(disconnects, "немає жодної події від мережевого модуля (QtWebSockets)")
        return disconnects[0]

    step(1500, lambda: check("network", network_check))

    def finish() -> None:
        ok = all(r["ok"] for r in results.values())
        payload = {"ok": ok, "platform": sys.platform, "qt_platform": app.platformName(), "results": results}
        report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for name, r in results.items():
            print(("✓" if r["ok"] else "✗"), name, "—", r["detail"], flush=True)
        if IS_WINDOWS:
            win.hotkey.unregister()
        app.exit(0 if ok else 1)

    def run(i: int = 0) -> None:
        if i >= len(steps):
            QTimer.singleShot(50, finish)
            return
        delay, fn = steps[i]

        def go() -> None:
            try:
                fn()
            finally:
                run(i + 1)

        QTimer.singleShot(delay, go)

    run()
    QTimer.singleShot(60000, lambda: (results.setdefault("timeout", {"ok": False, "detail": "60 с"}), finish()))
    return app.exec()
