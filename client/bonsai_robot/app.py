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

from .connection import AgentConnection, normalize_url
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
    def __init__(self, settings: Settings, demo: bool = False) -> None:
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
        self.robot.double_clicked.connect(self.repeat_last_task)
        self.robot.petted.connect(self.on_petted)
        self.robot.files_dropped.connect(self.upload_files)
        self.robot.text_dropped.connect(self.on_text_dropped)
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
        if not demo:  # у демо — без сервера і без автозасинання, щоб ніщо не перебивало анімації
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
        url = normalize_url(dlg.url.text())  # зберігаємо вже виправлену адресу — видно, куди підключаємось
        changed_conn = (url, dlg.token.text().strip()) != (s.server_url, s.token)
        s.server_url, s.token = url, dlg.token.text().strip()
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
        if text != self.settings.last_task:
            self.settings.last_task = text
            self.settings.save()
        self.busy = True
        self.panel.set_busy(True)
        self.robot.set_state("thinking")

    def repeat_last_task(self) -> None:
        """Подвійний клік: повторити останню задачу."""
        self.touch()
        last = self.settings.last_task
        if self.busy:
            self.show_panel()
            self.panel.set_activity("⏳ Я ще працюю над попередньою задачею")
            return
        if not last:
            self.show_panel()
            self.panel.set_activity("Подвійний клік повторює останню задачу — але її ще не було 🙂")
            return
        self.show_panel()
        self.send_task(last)

    def on_petted(self) -> None:
        self.touch()
        if self.robot.state == "sleeping":
            self.robot.set_state("idle")  # прокидається від гладження і муркоче далі

    # ---------------- перетягування файлів ----------------
    MAX_UPLOAD = 25 * 1024 * 1024

    def upload_files(self, paths: list) -> None:
        import base64
        import uuid
        from pathlib import Path

        self.touch()
        self.show_panel()
        if not self.conn.is_connected:
            self.panel.add_entry("error", "Немає з'єднання з сервером — файл нікуди передати.")
            self.robot.set_state("sad", 2.5, then="offline")
            return
        self._pending_uploads = getattr(self, "_pending_uploads", {})
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                self.panel.add_entry("error", f"{path.name}/ — це тека; перетягни окремі файли.")
                continue
            try:
                size = path.stat().st_size
                if size > self.MAX_UPLOAD:
                    self.panel.add_entry("error", f"{path.name}: завеликий ({size // 1024 // 1024} МБ, максимум 25 МБ).")
                    continue
                data = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError as e:
                self.panel.add_entry("error", f"{path.name}: не вдалося прочитати ({e}).")
                continue
            uid = uuid.uuid4().hex[:8]
            self._pending_uploads[uid] = path.name
            self.conn.send({"type": "upload", "id": uid, "name": path.name, "data": data})
            self.panel.set_activity(f"📤 Передаю {path.name}…")
        self.robot.set_state("working")

    def on_text_dropped(self, text: str) -> None:
        self.touch()
        text = text.strip()
        if not text:
            return
        if text.startswith(("http://", "https://")) and "\n" not in text:
            self.panel.prefill(f"Відкрий і коротко перекажи: {text}")
        elif len(text) > 4000:
            # великий фрагмент — передаємо як файл, щоб не роздувати поле вводу
            import tempfile

            tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="текст-", delete=False, encoding="utf-8")
            tmp.write(text)
            tmp.close()
            self.upload_files([tmp.name])
            return
        else:
            self.panel.prefill(f"Ось текст:\n«{text}»\n\nЩо з ним зробити: ")
        self.show_panel()

    def _on_uploaded(self, ev: dict) -> None:
        self._pending_uploads = getattr(self, "_pending_uploads", {})
        self._pending_uploads.pop(ev.get("id"), None)
        self._uploaded = getattr(self, "_uploaded", [])
        self._uploaded.append(ev["path"])
        size_kb = max(1, int(ev.get("size", 0)) // 1024)
        self.panel.add_entry("tool", f"📎 {ev['path']} ({size_kb} КБ) — на сервері")
        if not self._pending_uploads:  # усе передано — підказуємо задачу
            files = ", ".join(self._uploaded)
            noun = "файл" if len(self._uploaded) == 1 else "файли"
            self._uploaded = []
            self.panel.set_activity("")
            self.panel.prefill(f"Проаналізуй {noun} {files}: ")
            if not self.busy:
                self.robot.set_state("happy", 1.2)

    # ---------------- гаряча клавіша / команди ----------------
    def handle_command(self, cmd: str) -> None:
        """Команди від `bonsai-robot --toggle | --show | --ask ТЕКСТ` (другий запуск)."""
        name, _, arg = cmd.strip().partition(" ")
        if not self.isVisible():
            self.toggle_visible()
        if name == "raise":  # повторний запуск без параметрів — просто показуємо робота
            self.raise_()
        elif name == "toggle":
            if self.panel.isVisible():
                self.panel.hide_panel()
            else:
                self.show_panel()
                self.panel.activateWindow()
        elif name == "show":
            self.show_panel()
        elif name == "ask" and arg:
            self.show_panel()
            if self.busy:
                self.panel.prefill(arg)
            else:
                self.send_task(arg)
        elif name == "quit":
            QApplication.quit()

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

    def on_disconnected(self, reason: str, raw: str = "") -> None:
        self.panel.set_status(f"немає з'єднання: {reason}", False, details=f"{self.conn.url}\n{raw}")
        if self.busy:
            self.busy = False
            self.panel.set_busy(False)
            self.panel.drop_confirms()
        self.robot.set_state("offline")

    def on_event(self, ev: dict) -> None:  # noqa: C901
        self.touch()
        t = ev.get("type")
        if t == "uploaded":
            self._on_uploaded(ev)
            return
        if t == "upload_error":
            self._pending_uploads = getattr(self, "_pending_uploads", {})
            self._pending_uploads.pop(ev.get("id"), None)
            self.panel.add_entry("error", f"Не вдалося передати {ev.get('name')}: {ev.get('message')}")
            self.robot.set_state("sad", 2.0)
            return
        if t == "hello":
            name = ev.get("name") or "Bonsai"
            model = str(ev.get("model", "")).split("/")[-1]
            self.panel.set_status(f"{name} · підключено · {model}", True)
            self.setWindowTitle(name)
            if self.tray:
                self.tray.setToolTip(name)
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


def _ipc_name() -> str:
    return f"bonsai-robot-{os.getuid()}"


def _command_from_argv(argv: list[str]) -> str | None:
    """--toggle | --show | --ask ТЕКСТ | --quit → команда для вже запущеного робота."""
    for flag in ("--toggle", "--show", "--quit"):
        if flag in argv:
            return flag[2:]
    if "--ask" in argv:
        text = " ".join(argv[argv.index("--ask") + 1 :]).strip()
        return f"ask {text}" if text else "toggle"
    return None


def _send_to_running(cmd: str) -> bool:
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(_ipc_name())
    if not sock.waitForConnected(300):
        return False
    sock.write((cmd.replace("\n", " ") + "\n").encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def _listen_for_commands(win: RobotWindow) -> None:
    from PySide6.QtNetwork import QLocalServer

    server = QLocalServer(win)
    QLocalServer.removeServer(_ipc_name())  # прибираємо «завислий» сокет після аварійного виходу
    server.listen(_ipc_name())

    def on_new() -> None:
        conn = server.nextPendingConnection()

        def read() -> None:
            while conn.canReadLine():
                win.handle_command(bytes(conn.readLine()).decode("utf-8", "replace"))

        conn.readyRead.connect(read)
        conn.disconnected.connect(conn.deleteLater)
        read()

    server.newConnection.connect(on_new)


def main() -> None:
    # На Wayland (типово у Fedora) вікна не можуть самі тримати себе поверх інших
    # і позиціонуватися — тому використовуємо XWayland (xcb), якщо користувач не вказав інше.
    if os.environ.get("WAYLAND_DISPLAY") and "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "xcb"
    app = QApplication(sys.argv)
    app.setApplicationName("Bonsai Robot")
    app.setQuitOnLastWindowClosed(False)
    demo = "--demo" in sys.argv
    cmd = _command_from_argv(sys.argv)

    if not demo:
        # робот уже запущений? — передаємо йому команду (гаряча клавіша) і виходимо
        if _send_to_running(cmd or "raise"):
            return
        if cmd == "quit":
            return

    settings = Settings.load()
    win = RobotWindow(settings, demo=demo)
    win.show()
    if demo:
        args = sys.argv[sys.argv.index("--demo") + 1 :]
        _demo(win, args[0] if args and not args[0].startswith("-") else None)
    else:
        _listen_for_commands(win)
        if cmd:
            QTimer.singleShot(400, lambda: win.handle_command(cmd))
    sys.exit(app.exec())


DEMO_STATES = [
    ("idle", "чекає (наведи курсор — помахає, поводь мишкою — стежитиме очима)"),
    ("thinking", "думає"),
    ("working", "працює з інструментом"),
    ("waiting", "чекає на твій дозвіл"),
    ("talking", "відповідає"),
    ("happy", "задачу виконано"),
    ("sad", "помилка"),
    ("sleeping", "спить"),
    ("offline", "немає з'єднання"),
]


def _demo(win: RobotWindow, only: str | None = None) -> None:
    """Перегляд анімацій без сервера.

    bonsai-robot --demo            — усі стани по колу, по 4 с кожен
    bonsai-robot --demo thinking   — один стан, щоб роздивитися
    """
    names = dict(DEMO_STATES)
    states = [(only, names.get(only, only))] if only else DEMO_STATES
    step = {"i": 0}

    def show() -> None:
        name, title = states[step["i"] % len(states)]
        win.robot.set_state(name)
        num = f"{step['i'] % len(states) + 1}/{len(states)} · " if len(states) > 1 else ""
        win.panel.set_status(f"Демо {num}{name} — {title}", True)
        print(f"[demo] {name}: {title}", flush=True)
        step["i"] += 1

    show()
    win.show_panel()
    if len(states) > 1:
        timer = QTimer(win, interval=4000)
        timer.timeout.connect(show)
        timer.start()
