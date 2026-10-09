"""Головне вікно: робот поверх усіх вікон + панель чату + трей."""

from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .connection import AgentConnection
from .panel import ChatPanel
from .robot import RobotWidget
from .settings import Settings


class SettingsDialog(QDialog):
    def __init__(self, s: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Налаштування Bonsai Robot")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        form = QFormLayout()
        self.url = QLineEdit(s.server_url)
        self.url.setPlaceholderText("ws://192.168.1.10:8765/ws")
        self.token = QLineEdit(s.token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.scale = QDoubleSpinBox(minimum=0.5, maximum=2.0, singleStep=0.1, value=s.scale)
        self.thinking = QCheckBox("Показувати хід думок моделі")
        self.thinking.setChecked(s.show_thinking)
        form.addRow("Сервер (WebSocket):", self.url)
        form.addRow("Токен:", self.token)
        form.addRow("Розмір робота:", self.scale)
        form.addRow("", self.thinking)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self.resize(460, 0)


class RobotWindow(QWidget):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.settings = settings
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Bonsai Robot")

        self.robot = RobotWidget(settings.scale, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.robot)
        self.adjustSize()

        self.panel = ChatPanel()
        self.conn = AgentConnection(settings.server_url, settings.token, self)
        self.busy = False
        self.last_activity = time.monotonic()
        self.thinking_buf = ""

        # сигнали
        self.robot.clicked.connect(self.on_robot_clicked)
        self.robot.drag_moved.connect(self.on_drag)
        self.robot.drag_finished.connect(self.save_position)
        self.robot.context_requested.connect(lambda pos: self.menu.popup(pos.toPoint()))
        self.panel.task_submitted.connect(self.send_task)
        self.panel.cancel_requested.connect(self.cancel_task)
        self.panel.reset_requested.connect(self.reset_chat)
        self.panel.confirm_answered.connect(self.answer_confirm)
        self.conn.event.connect(self.on_event)
        self.conn.connected.connect(self.on_connected)
        self.conn.disconnected.connect(self.on_disconnected)

        self._build_menu()
        self._build_tray()
        self._place_initial()

        self._sleep_timer = QTimer(self, interval=5000)
        self._sleep_timer.timeout.connect(self._check_sleep)
        self._sleep_timer.start()

        self.conn.open()

    # ---------------- розміщення ----------------
    def _place_initial(self) -> None:
        s = self.settings
        if s.pos_x is not None and s.pos_y is not None:
            pos = QPoint(s.pos_x, s.pos_y)
            if any(sc.availableGeometry().contains(pos) for sc in QGuiApplication.screens()):
                self.move(pos)
                return
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.move(geo.right() - self.width() - 24, geo.bottom() - self.height() - 24)

    def on_drag(self, delta: QPointF) -> None:
        self.move(self.pos() + delta.toPoint())
        if self.panel.isVisible():
            self.position_panel()

    def save_position(self) -> None:
        self.settings.pos_x, self.settings.pos_y = self.x(), self.y()
        self.settings.save()

    def position_panel(self) -> None:
        self.panel.adjustSize()
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        geo: QRect = screen.availableGeometry()
        pw, ph = self.panel.width(), self.panel.height()
        # ліворуч від робота, якщо є місце, інакше праворуч
        x = self.x() - pw + 10 if self.x() - pw + 10 >= geo.left() else self.x() + self.width() - 10
        y = self.y() + self.height() - ph
        x = max(geo.left(), min(x, geo.right() - pw))
        y = max(geo.top(), min(y, geo.bottom() - ph))
        self.panel.move(x, y)

    def show_panel(self) -> None:
        self.position_panel()
        self.panel.show_panel()
        QTimer.singleShot(0, self.position_panel)

    # ---------------- меню / трей ----------------
    def _build_menu(self) -> None:
        self.menu = QMenu()
        self.menu.addAction("💬 Відкрити чат", self.show_panel)
        self.menu.addAction("⟲ Нова розмова", self.reset_chat)
        self.menu.addAction("■ Зупинити задачу", self.cancel_task)
        self.menu.addSeparator()
        self.menu.addAction("⚙ Налаштування…", self.open_settings)
        self.act_hide = QAction("🙈 Сховати робота", self)
        self.act_hide.triggered.connect(self.toggle_visible)
        self.menu.addAction(self.act_hide)
        self.menu.addSeparator()
        self.menu.addAction("✕ Вийти", QApplication.quit)

    def _build_tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        pix = QPixmap(self.robot.size())
        pix.fill(Qt.GlobalColor.transparent)
        self.robot.render(pix)
        self.tray = QSystemTrayIcon(QIcon(pix), self)
        self.tray.setToolTip("Bonsai Robot")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(
            lambda r: self.toggle_visible() if r == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
            self.panel.hide()
            self.act_hide.setText("🤖 Показати робота")
        else:
            self.show()
            self.act_hide.setText("🙈 Сховати робота")

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        s = self.settings
        changed_conn = (dlg.url.text().strip(), dlg.token.text().strip()) != (s.server_url, s.token)
        s.server_url, s.token = dlg.url.text().strip(), dlg.token.text().strip()
        s.show_thinking = dlg.thinking.isChecked()
        if abs(dlg.scale.value() - s.scale) > 1e-3:
            s.scale = dlg.scale.value()
            self.robot.scale_factor = s.scale
            self.robot.setFixedSize(int(200 * s.scale), int(220 * s.scale))
            self.adjustSize()
        s.save()
        if changed_conn:
            self.conn.configure(s.server_url, s.token)

    # ---------------- дії користувача ----------------
    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def on_robot_clicked(self) -> None:
        self.touch()
        if self.robot.state == "sleeping":
            self.robot.set_state("happy", 1.2)
        if self.panel.isVisible():
            self.panel.hide_panel()
        else:
            self.show_panel()

    def send_task(self, text: str) -> None:
        self.touch()
        if not self.conn.send({"type": "task", "text": text, "session_id": self.settings.session_id}):
            self.panel.add_entry("error", "Немає з'єднання з сервером — перевір налаштування (ПКМ по роботу → Налаштування).")
            self.robot.set_state("sad", 2.5, then="offline")
            return
        self.panel.add_entry("user", text)
        self.busy = True
        self.panel.set_busy(True)
        self.robot.set_state("thinking")

    def cancel_task(self) -> None:
        self.conn.send({"type": "cancel"})
        self.panel.drop_confirms()

    def reset_chat(self) -> None:
        self.panel.clear()
        self.conn.send({"type": "reset", "session_id": self.settings.session_id})

    def answer_confirm(self, cid: str, approved: bool) -> None:
        self.touch()
        self.conn.send({"type": "confirm_response", "id": cid, "approved": approved})
        self.robot.set_state("working" if approved else "thinking")

    # ---------------- події сервера ----------------
    def on_connected(self) -> None:
        self.robot.set_state("happy", 1.5)

    def on_disconnected(self, reason: str) -> None:
        self.panel.set_status(f"немає з'єднання: {reason}", False)
        if self.busy:
            self.busy = False
            self.panel.set_busy(False)
            self.panel.drop_confirms()
        self.robot.set_state("offline")

    def on_event(self, ev: dict) -> None:  # noqa: C901
        self.touch()
        t = ev.get("type")
        if t == "hello":
            self.panel.set_status(f"підключено · {str(ev.get('model', '')).split('/')[-1]}", True)
        elif t == "status":
            state = ev.get("state")
            if state == "thinking":
                self.robot.set_state("thinking")
                self.panel.set_activity(f"💭 Думаю… (крок {ev.get('step', 1)})")
            elif state == "working":
                self.robot.set_state("working")
                self.panel.set_activity(f"🔧 Працюю: {ev.get('tool', '')}")
            elif state == "waiting_confirm":
                self.robot.set_state("waiting")
                self.panel.set_activity("⏳ Чекаю на твій дозвіл")
                self.show_panel()
            elif state == "idle":
                self.busy = False
                self.panel.set_busy(False)
        elif t == "thinking":
            if self.settings.show_thinking:
                self.thinking_buf = (self.thinking_buf + ev.get("text", ""))[-160:]
                self.panel.set_activity("💭 " + " ".join(self.thinking_buf.split())[-120:])
        elif t == "token":
            self.robot.set_state("talking")
            self.panel.append_draft(ev.get("text", ""))
        elif t == "tool_call":
            self.panel.clear_draft()
            summary = " ".join(str(ev.get("summary", "")).split())[:140]
            self.panel.add_entry("tool", f"🔧 {ev.get('name')}: {summary}")
        elif t == "tool_result":
            if not ev.get("ok"):
                self.panel.add_entry("tool", f"✗ {ev.get('name')}: {str(ev.get('preview', ''))[:140]}")
        elif t == "confirm_request":
            self.panel.ask_confirm(ev["id"], ev.get("summary", ""))
            self.robot.set_state("waiting")
            self.show_panel()
        elif t == "final":
            text = ev.get("text", "")
            self.thinking_buf = ""
            self.panel.add_entry("assistant", text)
            talk = min(6.0, 1.0 + len(text) / 120)
            self.robot.set_state("talking", talk, then="happy")
            QTimer.singleShot(int(talk * 1000) + 1800, self._back_to_idle)
            if not self.panel.isVisible():
                self.show_panel()
            else:
                QTimer.singleShot(80, self.position_panel)
        elif t == "error":
            self.panel.add_entry("error", ev.get("message", "помилка"))
            self.robot.set_state("sad", 3.0)

    def _back_to_idle(self) -> None:
        if self.robot.state == "happy" and not self.busy:
            self.robot.set_state("idle")

    def _check_sleep(self) -> None:
        if (
            self.robot.state == "idle"
            and not self.busy
            and not self.panel.isVisible()
            and time.monotonic() - self.last_activity > self.settings.sleep_after_sec
        ):
            self.robot.set_state("sleeping")


def main() -> None:
    # На Wayland (типово у Fedora) вікна не можуть самі тримати себе поверх інших
    # і позиціонуватися — тому використовуємо XWayland (xcb), якщо користувач не вказав інше.
    if os.environ.get("WAYLAND_DISPLAY") and "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "xcb"
    app = QApplication(sys.argv)
    app.setApplicationName("Bonsai Robot")
    app.setQuitOnLastWindowClosed(False)
    settings = Settings.load()
    win = RobotWindow(settings)
    win.show()
    if "--demo" in sys.argv:
        _demo(win)
    sys.exit(app.exec())


def _demo(win: RobotWindow) -> None:
    """Прогін усіх анімацій без сервера: bonsai-robot --demo"""
    states = ["idle", "thinking", "working", "waiting", "talking", "happy", "sad", "sleeping", "offline"]
    for i, st in enumerate(states):
        QTimer.singleShot(i * 2500, lambda st=st: win.robot.set_state(st))


if __name__ == "__main__":
    main()
