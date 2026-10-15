"""Головне вікно: робот поверх усіх вікон + панель чату + трей."""

from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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
from .characters import CHARACTERS, make_character
from .icon import app_icon
from .winplatform import IS_WINDOWS, WinHotkey, autostart_enabled, set_autostart
from .panel import ChatPanel
from .settings import Settings, new_session_id


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


class SettingsDialog(QDialog):
    def __init__(self, s: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Налаштування Kru")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        form = QFormLayout()
        self.url = QLineEdit(s.server_url)
        self.url.setPlaceholderText("ws://192.168.1.10:8765/ws")
        self.token = QLineEdit(s.token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.scale = QDoubleSpinBox(minimum=0.5, maximum=2.0, singleStep=0.1, value=s.scale)
        self.character = QComboBox()
        for key, (title, _cls) in CHARACTERS.items():
            self.character.addItem(title, key)
        self.character.setCurrentIndex(max(0, self.character.findData(s.character)))
        self.think = QCheckBox("Думати перед відповіддю (розумніше, але повільніше)")
        self.think.setChecked(s.think)
        self.thinking = QCheckBox("Показувати хід думок моделі")
        self.thinking.setChecked(s.show_thinking)
        self.thinking.setEnabled(s.think)  # без міркувань показувати нічого
        self.think.toggled.connect(self.thinking.setEnabled)
        self.tools = QCheckBox("Показувати дії агента (🔧 read_file, web_search…)")
        self.tools.setChecked(s.show_tools)
        self.auto_files = QCheckBox("Запис файлів у робочій теці — без запиту (старі версії → .kru-backup)")
        self.auto_files.setToolTip(
            "Агент записує й змінює файли лише у своїй робочій теці на сервері, не питаючи щоразу.\n"
            "Перед перезаписом стара версія копіюється в .kru-backup (зберігається 7 днів).\n"
            "Команди shell і Python, як і раніше, просять дозволу."
        )
        self.auto_files.setChecked(s.auto_files)
        form.addRow("Сервер (WebSocket):", self.url)
        form.addRow("Токен:", self.token)
        form.addRow("Персонаж:", self.character)
        form.addRow("Розмір:", self.scale)
        form.addRow("", self.think)
        form.addRow("", self.thinking)
        form.addRow("", self.tools)
        form.addRow("", self.auto_files)
        self.autostart = None
        if IS_WINDOWS:
            self.autostart = QCheckBox("Запускати разом із Windows")
            self.autostart.setChecked(autostart_enabled())
            form.addRow("", self.autostart)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self.resize(460, 0)


# що показувати замість технічної назви інструмента, коли дії агента приховані
def local_timezone() -> str:
    """Часовий пояс цього ПК (IANA, напр. Europe/Kyiv): агент бачить і планує час саме в ньому."""
    from PySide6.QtCore import QTimeZone

    try:
        return bytes(QTimeZone.systemTimeZoneId()).decode()
    except Exception:  # noqa: BLE001
        return ""


TOOL_ACTIVITY = {
    "web_search": "🔎 Шукаю в інтернеті…",
    "fetch_url": "🌐 Читаю сторінку…",
    "deep_research": "📚 Досліджую тему…",
    "delegate": "🤝 Працює субагент…",
    "update_plan": "📋 Оновлюю план…",
    "mark_step": "📋 Відмічаю виконане…",
    "schedule_task": "⏰ Планую завдання…",
    "send_telegram": "✈️ Надсилаю в Telegram…",
    "read_file": "📖 Читаю файл…",
    "list_dir": "📂 Переглядаю файли…",
    "search_files": "📂 Шукаю у файлах…",
    "write_file": "✍️ Записую файл…",
    "edit_file": "✍️ Редагую файл…",
    "run_shell": "⚙️ Виконую команду…",
    "run_python": "🧮 Рахую…",
    "remember": "🧠 Запам'ятовую…",
    "recall": "🧠 Пригадую…",
    "forget": "🧠 Забуваю…",
    "view_image": "👁 Дивлюся на зображення…",
    "current_datetime": "🕒 Дивлюся на годинник…",
}


class RobotWindow(QWidget):
    def __init__(self, settings: Settings, demo: bool = False) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.settings = settings
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Kru")

        self.robot = make_character(settings.character, settings.scale, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.robot)
        self.adjustSize()

        self.panel = ChatPanel()
        self.panel.show_tools = settings.show_tools
        self.conn = AgentConnection(settings.server_url, settings.token, self)
        self.busy = False
        self.last_activity = time.monotonic()
        self.thinking_buf = ""

        # сигнали
        self._connect_character()
        self.panel.task_submitted.connect(self.send_task)
        self.panel.height_changed.connect(lambda: self.panel.isVisible() and self.position_panel())
        self.panel.chats_requested.connect(self.show_chats)
        # чати зберігає сервер автоматично; settings.session_id — id поточного чату
        self.chat_title = ""
        self._restoring = False  # після підключення підтягуємо поточний чат у панель
        self.panel.history_open.connect(self.open_chat)
        self.panel.history_export.connect(lambda cid: self.export_chat(cid))
        self.panel.history_delete.connect(self._delete_chat)
        self.panel.history_rename.connect(self.rename_chat)
        self.panel.new_chat_requested.connect(self.new_chat)
        self.panel.image_pasted.connect(lambda path: self.upload_files([path]))
        self.panel.cancel_requested.connect(self.cancel_task)
        self.panel.confirm_answered.connect(self.answer_confirm)
        self.conn.event.connect(self.on_event)
        self.conn.connected.connect(self.on_connected)
        self.conn.disconnected.connect(self.on_disconnected)

        self._build_menu()
        self._build_tray()
        # Windows: Ctrl+Alt+B через RegisterHotKey (на Linux клавішу реєструє інсталятор у KDE/GNOME)
        self.hotkey = WinHotkey(lambda: self.handle_command("toggle"))
        if IS_WINDOWS and not demo:
            self.hotkey.register(int(self.winId()))
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
        if self.panel.shown:
            self.position_panel()

    def save_position(self) -> None:
        self.settings.pos_x, self.settings.pos_y = self.x(), self.y()
        self.settings.save()

    def _panel_target(self) -> QPoint:
        """Де має стояти панель: ліворуч від робота, якщо є місце, інакше праворуч; «хвостик» — до голови."""
        self.panel.adjustSize()
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        geo: QRect = screen.availableGeometry()
        pw, ph = self.panel.width(), self.panel.height()
        left_ok = self.x() - pw + 6 >= geo.left()
        x = self.x() - pw + 6 if left_ok else self.x() + self.width() - 6
        y = self.y() + self.height() - ph
        x = max(geo.left(), min(x, geo.right() - pw))
        y = max(geo.top(), min(y, geo.bottom() - ph))
        head_y = self.y() + int(85 * self.robot.scale_factor)  # рівень обличчя робота
        self.panel.set_tail("right" if left_ok else "left", head_y - y)
        return QPoint(x, y)

    def position_panel(self) -> None:
        self.panel.move_to(self._panel_target())

    def show_panel(self) -> None:
        self.panel.show_panel(self._panel_target())
        QTimer.singleShot(0, self.position_panel)

    # ---------------- меню / трей ----------------
    def _build_menu(self) -> None:
        self.menu = QMenu()
        self.menu.addAction("💬 Відкрити чат", self.show_panel)
        self.menu.addAction("📝 Новий чат", self.new_chat)
        self.menu.addAction("📂 Усі чати…", self.show_chats)
        self.menu.addAction("✏ Перейменувати чат…", lambda: self.rename_chat(None, self.chat_title))
        self.menu.addAction("📤 Експорт розмови в Markdown…", lambda: self.export_chat(None))
        self.menu.addAction("■ Зупинити задачу", self.cancel_task)
        self.menu.addSeparator()
        self.menu.addAction("⚙ Налаштування…", self.open_settings)
        self.act_hide = QAction("🙈 Сховати робота", self)
        self.act_hide.triggered.connect(self.toggle_visible)
        self.menu.addAction(self.act_hide)
        self.menu.addSeparator()
        self.menu.addAction("✕ Вийти", QApplication.quit)

    def _connect_character(self) -> None:
        self.robot.clicked.connect(self.on_robot_clicked)
        self.robot.petted.connect(self.on_petted)
        self.robot.files_dropped.connect(self.upload_files)
        self.robot.text_dropped.connect(self.on_text_dropped)
        self.robot.drag_moved.connect(self.on_drag)
        self.robot.drag_finished.connect(self.save_position)
        self.robot.context_requested.connect(lambda pos: self.menu.popup(pos.toPoint()))

    def set_character(self, name: str) -> None:
        """Перемкнути персонажа на льоту: стан і позиція зберігаються."""
        if name not in CHARACTERS:
            return
        old = self.robot
        self.robot = make_character(name, self.settings.scale, self)
        self.robot.set_state(old.state)
        self.layout().replaceWidget(old, self.robot)
        old._timer.stop()
        old.deleteLater()
        self._connect_character()
        self.robot.set_state("happy", 1.2, then=old.state if old.state != "happy" else "idle")
        self.robot.show()
        icon = app_icon(name)
        QApplication.instance().setWindowIcon(icon)
        self.setWindowIcon(icon)
        if self.tray:
            self.tray.setIcon(icon)

    def _build_tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(self.settings.character), self)
        self.tray.setToolTip("Kru")
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
        s.think = dlg.think.isChecked()
        s.show_thinking = dlg.thinking.isChecked()
        s.show_tools = dlg.tools.isChecked()
        s.auto_files = dlg.auto_files.isChecked()
        self.panel.set_show_tools(s.show_tools)
        if dlg.autostart is not None and dlg.autostart.isChecked() != autostart_enabled():
            set_autostart(dlg.autostart.isChecked())
        character = dlg.character.currentData()
        if character != s.character:
            s.character = character
            self.set_character(character)
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
        self.conn.reconnect_now()
        if self.robot.state == "sleeping":
            self.robot.set_state("happy", 1.2)
        if self.panel.shown:
            self.panel.hide_panel()
        else:
            self.show_panel()

    def send_task(self, text: str) -> None:
        self.touch()
        if not self.conn.send({"type": "task", "text": text, "session_id": self.settings.session_id,
                           "think": self.settings.think, "auto_files": self.settings.auto_files,
                           "tz": local_timezone()}):
            # немає з'єднання: пробуємо підключитися зараз і надішлемо задачу, щойно вийде
            self._pending_task = text
            self.conn.reconnect_now()
            self.panel.add_entry("user", text)
            self.panel.set_activity("⏳ Підключаюся до сервера… надішлю, щойно з'єднаюсь")
            QTimer.singleShot(15000, self._pending_timeout)
            return
        self.panel.add_entry("user", text)
        if text != self.settings.last_task:
            self.settings.last_task = text
            self.settings.save()
        self.busy = True
        self.panel.set_busy(True)
        self.robot.set_state("thinking")

    def _pending_timeout(self) -> None:
        if getattr(self, "_pending_task", None):
            self._pending_task = None
            self.panel.add_entry("error", "Не вдалося підключитися до сервера — перевір налаштування "
                                          "(ПКМ по роботу → Налаштування) і чи запущено NetBird.")
            self.robot.set_state("sad", 2.5, then="offline")

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
            images = all(p.lower().endswith(IMAGE_EXT) for p in self._uploaded)
            single = len(self._uploaded) == 1
            self._uploaded = []
            self.panel.set_activity("")
            if images:  # агент сам прикріпить картинки, згадані в задачі
                self.panel.prefill(f"Що на зображенні {files}? " if single else f"Подивись на зображення {files}: ")
            else:
                self.panel.prefill(f"Проаналізуй {'файл' if single else 'файли'} {files}: ")
            if not self.busy:
                self.robot.set_state("happy", 1.2)

    # ---------------- гаряча клавіша / команди ----------------
    def handle_command(self, cmd: str) -> None:
        """Команди від `kru-robot --toggle | --show | --ask ТЕКСТ` (другий запуск)."""
        name, _, arg = cmd.strip().partition(" ")
        if not self.isVisible():
            self.toggle_visible()
        if name == "raise":  # повторний запуск без параметрів — просто показуємо робота
            self.raise_()
        elif name == "toggle":
            if self.panel.shown:
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

    def new_chat(self) -> None:
        """Новий чат; попередній уже збережено на сервері (його видно в «Усі чати»)."""
        self.touch()
        self.show_panel()
        if self.busy:
            self.panel.set_activity("⏳ Спершу дочекайся завершення задачі або зупини її (■)")
            return
        had_messages = bool(self.panel.entries)
        self._switch_chat(new_session_id(), "")
        self.panel.clear()
        self.panel.set_plan([])
        if self.panel.history_shown:
            self.panel.show_chat()
        if had_messages:
            self.panel.set_activity("📝 Новий чат. Попередній збережено — він у «Усі чати»")
        # після того як Qt застосує приховування стрічки — ще раз підігнати висоту й позицію
        # (раніше це «доробляло» лише повторне натискання «Новий чат»)
        QTimer.singleShot(0, self._refit_panel)
        QTimer.singleShot(120, self._refit_panel)

    def _refit_panel(self) -> None:
        self.panel._fit()
        self.position_panel()

    def _switch_chat(self, chat_id: str, title: str) -> None:
        self.settings.session_id = chat_id
        self.settings.save()
        self.chat_title = title

    # ---------------- чати (усе — всередині панелі) ----------------
    def show_chats(self) -> None:
        """Сторінка «Чати» в панелі; повторне натискання — назад до розмови."""
        self.touch()
        if self.panel.shown and self.panel.history_shown:
            self.panel.show_chat()
            QTimer.singleShot(0, self.position_panel)
            return
        self.panel.cancel_ask()
        self.show_panel()
        self.panel.show_history()
        self.conn.send({"type": "chat_list"})
        QTimer.singleShot(0, self.position_panel)

    def open_chat(self, chat_id: str) -> None:
        if self.busy:
            self.panel.set_activity("⏳ Спершу дочекайся завершення задачі або зупини її (■)")
            return
        self.conn.send({"type": "chat_open", "id": chat_id})

    def rename_chat(self, chat_id: str | None, current: str) -> None:
        """Перейменувати чат (None — поточний)."""
        self.touch()
        self.show_panel()
        target = chat_id or self.settings.session_id
        if chat_id is None and not self.panel.entries:
            self.panel.set_activity("У цьому чаті ще немає повідомлень")
            return
        self.panel.ask(
            "Назва чату:",
            [("cancel", "Скасувати", "secondary"), ("save", "Зберегти", "primary")],
            lambda key, text: key == "save" and bool(text.strip()) and self.conn.send(
                {"type": "chat_rename", "id": target, "title": text.strip()}),
            input_text=current or "",
            default="save",
        )

    def export_chat(self, chat_id: str | None) -> None:
        self.touch()
        self.conn.send({"type": "chat_export", "id": chat_id or self.settings.session_id})

    def _delete_chat(self, chat_id: str) -> None:
        self.conn.send({"type": "chat_delete", "id": chat_id})
        if chat_id == self.settings.session_id:  # видалили відкритий чат — починаємо новий
            self._switch_chat(new_session_id(), "")
            self.panel.clear()

    @staticmethod
    def export_dir():
        from pathlib import Path

        from PySide6.QtCore import QStandardPaths

        # системна тека «Документи» (у Fedora з українською — ~/Документи, у Windows — Documents)
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        return Path(docs or Path.home()) / "Kru"

    def _write_export(self, ev: dict) -> None:
        """Експорт без системних вікон: одразу у ~/Документи/Kru/<назва>.md, далі — повідомлення в панелі."""
        from pathlib import Path

        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        folder = self.export_dir()
        name = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in ev["title"])[:60].strip() or "чат"
        path, n = folder / f"{name}.md", 1
        while path.exists():
            n += 1
            path = folder / f"{name} ({n}).md"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(ev["markdown"], encoding="utf-8")
        except OSError as e:
            self.panel.ask(f"⚠ Не вдалося записати {path}: {e}", [("ok", "Гаразд", "primary")], lambda *_: None)
            return

        def answer(key: str, _text: str) -> None:
            if key == "folder":
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
            elif key == "file":
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

        self.show_panel()
        self.panel.ask(
            f"📤 Експортовано: {path.name}\nу {str(folder).replace(str(Path.home()), '~', 1)}",
            [("folder", "Відкрити теку", "secondary"), ("file", "Відкрити файл", "secondary"), ("ok", "Гаразд", "primary")],
            answer,
        )

    def _on_chat_event(self, ev: dict) -> None:
        t = ev["type"]
        restoring, self._restoring = self._restoring, False
        if t == "chat_renamed":
            if ev["id"] == self.settings.session_id:
                self.chat_title = ev["title"]
            self.panel.set_activity(f"✏ Назва: «{ev['title']}»")
        elif t == "chat_list":
            self.panel.set_history(ev["chats"], self.settings.session_id)
            current = next((c for c in ev["chats"] if c["id"] == self.settings.session_id), None)
            if current:
                self.chat_title = current["title"]
            if self.panel.history_shown:
                QTimer.singleShot(0, self.position_panel)
        elif t == "chat_opened":
            self._switch_chat(ev["id"], ev["title"])
            self.panel.clear()
            for item in ev["transcript"]:
                self.panel.add_entry(item["role"], item["text"])
            self.panel.set_plan(ev.get("plan", []))
            if ev.get("running"):  # задача в цьому чаті ще йде (наприклад, після розриву зв'язку)
                self.busy = True
                self.panel.set_busy(True)
                self.robot.set_state("thinking")
                self.panel.set_activity("🔄 Зв'язок відновлено — агент ще працює над задачею…")
            elif self.busy and restoring:
                self.busy = False  # задача завершилась, поки не було зв'язку: відповідь уже в стрічці
                self.panel.set_busy(False)
            if restoring:
                return  # тихо відновили поточний чат після запуску
            self.panel.show_chat()
            self.show_panel()
            self.panel.set_activity(f"📂 «{ev['title']}» — можна продовжувати розмову")
            QTimer.singleShot(100, self.position_panel)
        elif t == "chat_export":
            self._write_export(ev)
        elif t == "chat_error":
            if restoring and self.busy:  # чату на сервері немає — і задачі в ньому теж
                self.busy = False
                self.panel.set_busy(False)
            if restoring:
                return  # поточний чат ще порожній — нічого відновлювати
            self.show_panel()
            self.panel.ask(f"⚠ {ev['message']}", [("ok", "Гаразд", "primary")], lambda *_: None)

    def answer_confirm(self, cid: str, approved: bool, scope: str = "") -> None:
        self.touch()
        msg = {"type": "confirm_response", "id": cid, "approved": approved}
        if scope:
            msg["scope"] = scope
        self.conn.send(msg)
        self.robot.set_state("working" if approved else "thinking")

    # ---------------- події сервера ----------------
    def on_connected(self) -> None:
        self.robot.set_state("happy", 1.5)
        text, self._pending_task = getattr(self, "_pending_task", None), None
        self._sent_on_connect = bool(text)  # тоді hello не перезавантажує чат: задача щойно пішла
        if text and self.conn.send({"type": "task", "text": text, "session_id": self.settings.session_id,
                           "think": self.settings.think, "auto_files": self.settings.auto_files,
                           "tz": local_timezone()}):
            if text != self.settings.last_task:
                self.settings.last_task = text
                self.settings.save()
            self.busy = True
            self.panel.set_busy(True)
            self.robot.set_state("thinking")

    def on_disconnected(self, reason: str, raw: str = "") -> None:
        self.panel.set_status(f"немає з'єднання: {reason}", False, details=f"{self.conn.url}\n{raw}")
        if self.busy:
            # задача доробляється на сервері; після перепідключення чат відкриється знову й покаже її хід
            self.panel.drop_confirms()
            self.panel.clear_draft()
            self.panel.set_activity("📡 Зв'язок втрачено — задача продовжується на сервері, чекаю на з'єднання…")
        self.robot.set_state("offline")

    def on_event(self, ev: dict) -> None:  # noqa: C901
        self.touch()
        t = ev.get("type")
        if t == "vision":
            if ev.get("state") == "attached":
                self.panel.set_activity(f"👁 Дивлюся на зображення: {', '.join(ev.get('images', []))}")
            else:
                self.panel.add_entry(
                    "error",
                    f"Модель зараз не бачить зображень: {ev.get('reason', '')}. "
                    "Перевір, що поруч із моделлю лежить mmproj-файл, і перезапусти її: deploy/model.sh start",
                )
            return
        if t.startswith("chat_"):
            self._on_chat_event(ev)
            return
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
            name = ev.get("name") or "Kru"
            model = str(ev.get("model", "")).split("/")[-1]
            eye = {True: " · 👁", False: " · без зору"}.get(ev.get("vision"), "")
            user = f" · {ev['user']}" if ev.get("user") and ev.get("user") != "owner" else ""
            self.panel.set_status(f"{name} · підключено · {model}{eye}{user}", True)
            if "user" not in ev:  # сервер ще старої версії — чати працюватимуть неправильно
                self.panel.add_entry(
                    "error",
                    "Сервер агента старої версії: чати не відкриватимуться. Онови його: "
                    "cd ~/bot && ./update.sh",
                )
            elif getattr(self, "_sent_on_connect", False):
                self._sent_on_connect = False  # стрічка вже актуальна, події нової задачі підуть самі
            else:
                # підтягнути поточний чат із сервера: після запуску ПК — розмову, на якій зупинились;
                # після розриву зв'язку — усе, що агент встиг зробити, і (якщо задача ще йде) її подальший хід
                self._restoring = True
                self.conn.send({"type": "chat_open", "id": self.settings.session_id})
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
                tool = ev.get("tool", "")
                if self.settings.show_tools:
                    self.panel.set_activity(f"🔧 Працюю: {tool}")
                else:
                    self.panel.set_activity(TOOL_ACTIVITY.get(tool, "🔧 Працюю…"))
            elif state == "summarizing":
                self.robot.set_state("thinking")
                self.panel.set_activity("🗜 Стискаю довгу розмову, щоб нічого не забути…")
            elif state == "waiting_confirm":
                self.robot.set_state("waiting")
                self.panel.set_activity("⏳ Чекаю на твій дозвіл")
                self.show_panel()
            elif state == "idle":
                self.busy = False
                self.panel.set_busy(False)
        elif t == "plan":
            self.panel.set_plan(ev.get("steps", []))
        elif t == "progress":
            icon = {"deep_research": "📚", "delegate": "🤝"}.get(ev.get("tool"), "🔧")
            self.panel.set_activity(f"{icon} {ev.get('text', '')}"[:120])
        elif t == "thinking":
            if self.settings.show_thinking:
                self.thinking_buf = (self.thinking_buf + ev.get("text", ""))[-160:]
                self.panel.set_activity("💭 " + " ".join(self.thinking_buf.split())[-120:])
        elif t == "token":
            self.robot.set_state("talking")
            self.panel.append_draft(ev.get("text", ""))
        elif t == "tool_call":
            self.panel.clear_draft()
            if ev.get("name") in {"update_plan", "mark_step"}:
                pass  # план показує окремий чек-лист
            elif ev.get("name") == "delegate":
                args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
                self.panel.add_entry("tool", f"🤝 Доручаю субагенту: {' '.join(str(args.get('task', '')).split())[:200]}")
            elif ev.get("sub"):
                summary = " ".join(str(ev.get("summary", "")).split())[:120]
                self.panel.add_entry("tool", f"↳ 🔧 {ev.get('name')}: {summary}")
            elif ev.get("name") == "remember":
                args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
                fact = " ".join(str(args.get("content", "")).split())[:200]
                self.panel.add_entry("tool", f"🧠 Запам'ятав: {fact}")
            else:
                summary = " ".join(str(ev.get("summary", "")).split())[:140]
                self.panel.add_entry("tool", f"🔧 {ev.get('name')}: {summary}")
        elif t == "tool_result":
            if not ev.get("ok"):
                self.panel.add_entry("tool", f"✗ {ev.get('name')}: {str(ev.get('preview', ''))[:140]}")
        elif t == "notify":
            self._on_notify(ev)
        elif t == "confirm_request":
            summary = ev.get("summary", "")
            if ev.get("scheduled"):  # дозволу просить заплановане завдання, а не поточний чат
                summary = f"{ev['scheduled']}\n\n{summary}"
            self.panel.ask_confirm(ev["id"], summary, str(ev.get("tool", "")))
            self.robot.set_state("waiting")
            self.show_panel()
        elif t == "final":
            text = ev.get("text", "")
            self.thinking_buf = ""
            self.panel.add_entry("assistant", text)
            talk = min(6.0, 1.0 + len(text) / 120)
            self.robot.set_state("talking", talk, then="happy")
            QTimer.singleShot(int(talk * 1000) + 1800, self._back_to_idle)
            if not self.panel.shown:
                self.show_panel()
            else:
                QTimer.singleShot(80, self.position_panel)
        elif t == "error":
            self.panel.add_entry("error", ev.get("message", "помилка"))
            self.robot.set_state("sad", 3.0)

    def _on_notify(self, ev: dict) -> None:
        """Заплановане завдання виконано: сповіщення з коротким результатом і кнопкою «Відкрити чат»."""
        title, body = str(ev.get("title", "⏰ Завдання")), " ".join(str(ev.get("text", "")).split())
        preview = body[:280] + ("…" if len(body) > 280 else "")
        self.robot.set_state("sad" if ev.get("error") else "happy", 2.5)
        if self.tray is not None:
            self.tray.showMessage(title, preview or "Готово")
        chat_id = ev.get("chat_id")

        def answer(key: str, _text: str) -> None:
            if key == "open" and chat_id:
                self.open_chat(chat_id)

        self.show_panel()
        buttons = [("ok", "Гаразд", "secondary")] + ([("open", "Відкрити чат", "primary")] if chat_id else [])
        self.panel.ask(f"{title} — {'не вдалося' if ev.get('error') else 'виконано'}\n\n{preview}", buttons, answer,
                       default="open" if chat_id else "ok")

    def _back_to_idle(self) -> None:
        if self.robot.state == "happy" and not self.busy:
            self.robot.set_state("idle")

    def _check_sleep(self) -> None:
        if (
            self.robot.state == "idle"
            and not self.busy
            and not self.panel.shown
            and time.monotonic() - self.last_activity > self.settings.sleep_after_sec
        ):
            self.robot.set_state("sleeping")


def _ipc_name() -> str:
    import getpass
    import re as _re

    user = _re.sub(r"[^\w.-]", "_", getpass.getuser() or "user")  # os.getuid() немає на Windows
    return f"kru-robot-{user}"


def _command_from_argv(argv: list[str]) -> str | None:
    """--toggle | --show | --ask ТЕКСТ | --quit → команда для вже запущеного робота."""
    for flag in ("--toggle", "--show", "--quit"):
        if flag in argv:
            return flag[2:]
    if "--ask" in argv:
        text = " ".join(argv[argv.index("--ask") + 1 :]).strip()
        return f"ask {text}" if text else "toggle"
    return None


def _code_hash() -> str:
    """Відбиток коду робота — щоб відрізнити запущену стару версію від щойно встановленої."""
    import hashlib
    from pathlib import Path

    h = hashlib.sha1()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.read_bytes())
    if getattr(sys, "frozen", False):  # зібраний .exe: модулі всередині архіву — беремо час збірки exe
        h.update(str(Path(sys.executable).stat().st_mtime_ns).encode())
    return h.hexdigest()[:12]


CODE_HASH = _code_hash()  # обчислюється при старті процесу й далі не змінюється


def _send_to_running(cmd: str) -> bool:
    """True — команду передано вже запущеному роботу (і тоді цей процес не потрібен).

    Якщо запущений робот — стара версія (після оновлення), просимо його завершитися і стартуємо самі.
    """
    import time as _time

    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(_ipc_name())
    if not sock.waitForConnected(300):
        return False
    sock.write(b"ver\n")
    sock.flush()
    reply = ""
    if sock.waitForReadyRead(800):
        reply = bytes(sock.readLine()).decode("utf-8", "replace").strip()
    if reply != f"ver {CODE_HASH}":
        print("[kru-robot] запущено стару версію робота — замінюю її новою", flush=True)
        sock.write(b"quit\n")
        sock.flush()
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        for _ in range(40):  # чекаємо, поки старий процес завершиться
            _time.sleep(0.1)
            probe = QLocalSocket()
            probe.connectToServer(_ipc_name())
            if not probe.waitForConnected(100):
                break
            probe.disconnectFromServer()
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
                line = bytes(conn.readLine()).decode("utf-8", "replace").strip()
                if line == "ver":  # новий запуск питає версію: якщо відрізняється — попросить нас завершитися
                    conn.write(f"ver {CODE_HASH}\n".encode())
                    conn.flush()
                else:
                    win.handle_command(line)

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
    app.setApplicationName("Kru")
    app.setDesktopFileName("kru-robot")  # панель задач KDE/GNOME бере іконку з kru-robot.desktop
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)
    from .emoji import setup_emoji_font

    setup_emoji_font(app)  # інакше на Fedora значки 🔧 💬 📋 у меню й чаті можуть бути порожніми
    if "--selftest" in sys.argv:  # перевірка на чистій системі (CI): вікно, панель, клавіша, автозапуск
        from .selftest import run_selftest

        args = sys.argv[sys.argv.index("--selftest") + 1 :]
        sys.exit(run_selftest(app, args[0] if args else "selftest-report.json"))
    demo = "--demo" in sys.argv
    cmd = _command_from_argv(sys.argv)

    if not demo:
        # робот уже запущений? — передаємо йому команду (гаряча клавіша) і виходимо
        if _send_to_running(cmd or "raise"):
            return
        if cmd == "quit":
            return

    settings = Settings.load()
    if "--character" in sys.argv:  # лише на цей запуск, напр. --demo --character dog
        args = sys.argv[sys.argv.index("--character") + 1 :]
        if args and args[0] in CHARACTERS:
            settings.character = args[0]
    app.setWindowIcon(app_icon(settings.character))
    win = RobotWindow(settings, demo=demo)
    win.show()
    if demo:
        args = sys.argv[sys.argv.index("--demo") + 1 :]
        _demo(win, args[0] if args and not args[0].startswith("-") else None)
    else:
        _listen_for_commands(win)
        if cmd:
            QTimer.singleShot(400, lambda: win.handle_command(cmd))
        elif getattr(settings, "first_run", False):
            QTimer.singleShot(700, win.open_settings)  # перший запуск: одразу вказати сервер і токен
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

    kru-robot --demo            — усі стани по колу, по 4 с кожен
    kru-robot --demo thinking   — один стан, щоб роздивитися
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
