"""Головне вікно: робот поверх усіх вікон + панель чату + трей."""

from __future__ import annotations

import base64
import math
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
    QSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .audio import Player, Recorder, make_wav
from .connection import AgentConnection, normalize_url
from .icon import app_icon
from .winplatform import IS_WINDOWS, WinHotkey, autostart_enabled, set_autostart
from .panel import ChatPanel
from .robot import RobotWidget
from . import wake as wakemod
from . import wakeword as wakewordmod
from .wake import WakeListener
from .settings import Settings, new_session_id


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


# що сервер уміє, якщо він ще не сказав (старіший сервер чи немає з'єднання)
DEFAULT_EFFECTS = {"natural": "Звичайний", "robot": "М'який робот", "digital": "Цифровий помічник",
                   "droid": "Маленький дроїд", "mech": "Великий робот"}
PREVIEW_TEXT = "Привіт! Я Kru. Ось так тепер звучить мій голос. Чим можу допомогти?"


def stats_line(st) -> str:
    """Заміри відповіді від сервера → рядок під чатом: «⏱ 4,2 с · 2 кроки · без міркувань · 43 т/с»."""
    if not isinstance(st, dict) or "seconds" not in st:
        return ""
    steps = int(st.get("steps") or 0)
    parts = [f"⏱ {float(st['seconds']):.1f} с".replace(".", ",")]
    if steps > 1:
        few = steps % 10 in (2, 3, 4) and steps % 100 not in (12, 13, 14)
        parts.append(f"{steps} {'кроки' if few else 'кроків'}")
    if int(st.get("think_steps") or 0):
        parts.append(f"міркування ~{int(st.get('thinking') or 0)} т")
    else:
        parts.append("без міркувань")
    if st.get("gen_tps"):
        parts.append(f"{float(st['gen_tps']):.0f} т/с")
    return " · ".join(parts)


class SettingsDialog(QDialog):
    def __init__(self, s: Settings, parent: QWidget | None = None, voice_caps: dict | None = None,
                 preview=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Налаштування Kru")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        form = QFormLayout()
        self.url = QLineEdit(s.server_url)
        self.url.setPlaceholderText("ws://192.168.1.10:8765/ws")
        self.token = QLineEdit(s.token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.scale = QDoubleSpinBox(minimum=0.5, maximum=2.0, singleStep=0.1, value=s.scale)
        self.think = QComboBox()
        for key, title in THINK_MODES.items():
            self.think.addItem(title, key)
        self.think.setCurrentIndex(max(0, self.think.findData(s.think_mode)))
        self.think.setToolTip("Міркування перед відповіддю: розумніше, але кожні ~40 слів роздумів — це секунда "
                              "очікування. «Авто» — думати лише над кодом, розрахунками, аналізом, планами")
        self.thinking = QCheckBox("Показувати хід думок моделі")
        self.thinking.setChecked(s.show_thinking)
        self.thinking.setEnabled(s.think_mode != "off")  # без міркувань показувати нічого
        self.think.currentIndexChanged.connect(lambda: self.thinking.setEnabled(self.think.currentData() != "off"))
        self.stats = QCheckBox("Після відповіді показувати, скільки вона тривала")
        self.stats.setChecked(s.show_stats)
        self.font_px = QSpinBox(minimum=10, maximum=24, value=s.font_size, suffix=" px")
        self.font_px.setToolTip("Шрифт стрічки чату й поля вводу. Швидко: Ctrl + колесо миші над чатом, Ctrl+= / Ctrl+− / Ctrl+0")
        self.tools = QCheckBox("Показувати дії агента (read_file, web_search…)")
        self.tools.setChecked(s.show_tools)
        self.auto_files = QCheckBox("Запис файлів у робочій теці — без запиту (старі версії → .kru-backup)")
        self.auto_files.setToolTip(
            "Агент записує й змінює файли лише у своїй робочій теці на сервері, не питаючи щоразу.\n"
            "Перед перезаписом стара версія копіюється в .kru-backup (зберігається 7 днів).\n"
            "Команди shell і Python, як і раніше, просять дозволу."
        )
        self.auto_files.setChecked(s.auto_files)
        self.voice_reply = QComboBox()
        for key, title in VOICE_REPLY.items():
            self.voice_reply.addItem(title, key)
        self.voice_reply.setCurrentIndex(max(0, self.voice_reply.findData(s.voice_reply)))
        self.voice_reply.setToolTip("Голосом питаєш — утримуй персонажа й говори (або кнопка мікрофона в чаті).\n"
                                    "Потрібно встановити голос на сервері: deploy/voice.sh install")
        self.voice_volume = QSpinBox(minimum=0, maximum=100, value=s.voice_volume, suffix=" %")
        caps = voice_caps or {}
        # голоси — ті, що має сервер (HolosTTS: «Гаська Шиян», Speaker_0…)
        self.voice_name = QComboBox()
        self.voice_name.addItem("типовий голос сервера", "")
        voices = list(caps.get("voices") or caps.get("speakers") or [])
        if s.voice_name and s.voice_name not in voices:
            voices.append(s.voice_name)  # сервер зараз недоступний — вибір не губимо
        for name in voices:
            self.voice_name.addItem(str(name).replace("Speaker_", "Голос "), str(name))
        self.voice_name.setCurrentIndex(max(0, self.voice_name.findData(s.voice_name)))
        self.voice_name.setToolTip("HolosTTS (deploy/holos.sh start) — 27 природних голосів")
        self.voice_effect = QComboBox()
        for key, title in (caps.get("effects") or DEFAULT_EFFECTS).items():
            self.voice_effect.addItem(title, key)
        self.voice_effect.setCurrentIndex(max(0, self.voice_effect.findData(s.voice_sound)))
        self.voice_effect.setToolTip("Обробка голосу: «Звичайний» — як є, решта — різні відтінки робота")
        self.voice_speed = QDoubleSpinBox(minimum=0.7, maximum=1.5, singleStep=0.05, value=s.voice_speed,
                                          suffix=" ×", decimals=2)
        self.voice_speed.setToolTip("Темп мовлення: більше — швидше")
        self.btn_preview = QPushButton("▶ Прослухати")
        self.btn_preview.setToolTip("Озвучити пробну фразу вибраним голосом" if caps.get("tts") else
                                    "Озвучення ще не налаштовано на сервері: deploy/holos.sh start")
        self.btn_preview.setEnabled(bool(caps.get("tts")) and preview is not None)
        if preview is not None:
            self.btn_preview.clicked.connect(lambda: preview(self.voice_name.currentData(),
                                                             self.voice_effect.currentData(),
                                                             round(self.voice_speed.value(), 2),
                                                             self.voice_volume.value()))
        phrase = (caps.get("wakeword") or {}).get("phrase") if isinstance(caps.get("wakeword"), dict) else ""
        self.wake_sensitivity = QComboBox()
        for key, title in WAKE_SENSITIVITY.items():
            self.wake_sensitivity.addItem(title, key)
        self.wake_sensitivity.setCurrentIndex(max(0, self.wake_sensitivity.findData(s.wake_sensitivity)))
        self.wake_sensitivity.setToolTip(f"Як легко робот відгукується на «{phrase or 'Чуєш, Кру'}». "
                                         "Якщо реагує на схожі слова — знизь"
                                         + ("" if phrase else ". Фразу спершу навчи на сервері: kru wakeword train"))
        self.wake_followup = QCheckBox(f"Після відповіді ще {FOLLOWUP_SEC} с слухати уточнення без імені")
        self.wake_followup.setChecked(s.wake_followup)
        voice_row = QHBoxLayout()
        voice_row.addWidget(self.voice_name, 3)
        voice_row.addWidget(self.voice_effect, 2)
        speed_row = QHBoxLayout()
        speed_row.addWidget(self.voice_speed)
        speed_row.addWidget(self.btn_preview, 1)
        form.addRow("Сервер (WebSocket):", self.url)
        form.addRow("Токен:", self.token)
        form.addRow("Розмір:", self.scale)
        form.addRow("Шрифт чату:", self.font_px)
        form.addRow("Відповідати голосом:", self.voice_reply)
        form.addRow("Гучність голосу:", self.voice_volume)
        form.addRow("Голос і звучання:", voice_row)
        form.addRow("Темп мовлення:", speed_row)
        form.addRow("Чутливість до «Кру»:", self.wake_sensitivity)
        form.addRow("", self.wake_followup)
        form.addRow("Міркування:", self.think)
        form.addRow("", self.stats)
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


VOICE_REPLY = {
    "voice": "коли питаю голосом",
    "always": "завжди",
    "off": "ні, лише текстом",
}


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
    "send_file": "📎 Передаю файл…",
    "search_chat": "🔎 Шукаю в історії чату…",
    "read_chat": "🔎 Перечитую розмову…",
    "read_file": "📖 Читаю файл…",
    "list_dir": "📂 Переглядаю файли…",
    "search_files": "📂 Шукаю у файлах…",
    "write_file": "✍️ Записую файл…",
    "edit_file": "✍️ Редагую файл…",
    "run_shell": "⚙️ Виконую команду…",
    "ssh_run": "🖥 Працюю на віддаленій машині…",
    "ssh_upload": "🖥 Передаю файли на машину…",
    "ssh_download": "🖥 Забираю файли з машини…",
    "run_python": "🧮 Рахую…",
    "remember": "🧠 Запам'ятовую…",
    "recall": "🧠 Пригадую…",
    "forget": "🧠 Забуваю…",
    "view_image": "👁 Дивлюся на зображення…",
    "current_datetime": "🕒 Дивлюся на годинник…",
}


THINK_VALUE = {"on": True, "off": False}  # auto → "auto" (сервер вирішує сам)
THINK_MODES = {"auto": "авто — лише складні задачі (швидко)", "on": "завжди (розумніше, повільніше)",
               "off": "ніколи (найшвидше)"}
WAKE_SENSITIVITY = {"low": "низька — рідше помиляється", "medium": "середня", "high": "висока — чує здалеку"}
FOLLOWUP_SEC = 6  # стільки слухаємо уточнення без імені після відповіді
BUSY_PHRASE = "Зачекай хвилинку, я ще працюю над попередньою задачею."


def _chime(up: bool) -> bytes:
    """Короткий м'який сигнал «слухаю» (вгору) / «почув» (вниз)."""
    import array

    rate, out = 24000, array.array("h")
    for freq in ((660, 880) if up else (880, 660)):
        n = int(rate * 0.09)
        for i in range(n):
            env = math.sin(math.pi * i / n)  # без клацань
            out.append(int(9000 * env * math.sin(2 * math.pi * freq * i / rate)))
    if sys.byteorder == "big":
        out.byteswap()
    return make_wav(out.tobytes(), rate)


class RobotWindow(QWidget):
    def __init__(self, settings: Settings, demo: bool = False) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.settings = settings
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Kru")

        self.robot = RobotWidget(settings.scale, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.robot)
        self.adjustSize()

        self.panel = ChatPanel()
        self.panel.show_tools = settings.show_tools
        self.panel.show_thinking = settings.show_thinking
        self.panel.set_font_size(settings.font_size)
        self.panel.set_panel_size(settings.panel_width, settings.panel_feed_height)
        self.conn = AgentConnection(settings.server_url, settings.token, self)
        self.busy = False
        self.last_activity = time.monotonic()
        self.thinking_buf = ""
        self._last_stats = ""  # заміри останньої відповіді — показати, коли агент звільниться
        # голос: запис (утримання персонажа / кнопка мікрофона) → сервер розпізнає → задача;
        # відповідь сервер озвучує, персонаж «говорить» у такт
        self.voice_caps = {"stt": False, "tts": False}
        self._speech_volume = settings.voice_volume  # пробна фраза в налаштуваннях — зі ще не збереженою гучністю
        self.recorder = Recorder(self)
        self.recorder.finished.connect(self._on_recorded)
        self.recorder.failed.connect(self._on_record_failed)
        # «завжди слухаю»: подвійний клік — робот чекає на «Кру, …» (див. wake.py)
        self.chime = Player(self)
        self._chimes = {True: _chime(True), False: _chime(False)}
        self._wake_turn = False  # поточна задача — з режиму «Кру»: відповідь голосом, потім уточнення
        self._wake_pending: str | None = None  # id запису з режиму «Кру», що розпізнається на сервері
        self._wake_loading = False
        self._wire_wake(WakeListener(self))
        self.player = Player(self)
        self.player.stopped.connect(self._on_speech_done)
        self._tts_id: str | None = None  # озвучення, що приходить частинами (по реченнях)
        self._tts_queue: list[bytes] = []  # частини, що чекають своєї черги
        self._tts_done = True  # остання частина вже прийшла
        self.player.started.connect(lambda: self.wake.active and self.wake.pause())  # не слухати самого себе
        self._voice_task = False  # поточну задачу поставлено голосом — відповідь озвучити
        self._voice_seq = 0
        self._level_timer = QTimer(self, interval=33)
        self._level_timer.timeout.connect(self._feed_level)

        # сигнали
        self._connect_robot()
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
        self.panel.history_search.connect(lambda q: self.conn.send({"type": "chat_search", "query": q}))
        self.panel.new_chat_requested.connect(self.new_chat)
        self.panel.image_pasted.connect(lambda path: self.upload_files([path]))
        self.panel.cancel_requested.connect(self.cancel_task)
        self.panel.confirm_answered.connect(self.answer_confirm)
        self.panel.file_requested.connect(self.download_file)
        self.panel.retry_requested.connect(self.retry_task)
        self.panel.resized.connect(self._on_panel_resized)
        self.panel.font_changed.connect(self._on_font_changed)
        self.panel.mic_toggled.connect(self.toggle_mic)
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
        from .menuicons import menu_icon

        # без емодзі в тексті: меню трею KDE їх не показує — замість них власні іконки
        self.menu = QMenu()
        self.menu.addAction(menu_icon("chat"), "Відкрити чат", self.show_panel)
        self.menu.addAction(menu_icon("new"), "Новий чат", self.new_chat)
        self.menu.addAction(menu_icon("chats"), "Усі чати…", self.show_chats)
        self.menu.addAction(menu_icon("rename"), "Перейменувати чат…", lambda: self.rename_chat(None, self.chat_title))
        self.menu.addAction(menu_icon("export"), "Експорт розмови в Markdown…", lambda: self.export_chat(None))
        self.menu.addAction(menu_icon("stop"), "Зупинити задачу", self.cancel_task)
        self.menu.addSeparator()
        self.act_wake = QAction(menu_icon("mic"), "Завжди слухати «Кру» (подвійний клік)", self, checkable=True)
        self.act_wake.toggled.connect(self.set_wake)
        self.menu.addAction(self.act_wake)
        self.act_thinking = QAction(menu_icon("think"), "Показувати хід думок", self, checkable=True)
        self.act_thinking.setChecked(self.settings.show_thinking)
        self.act_thinking.toggled.connect(self.set_show_thinking)
        self.menu.addAction(self.act_thinking)
        self.menu.addAction(menu_icon("settings"), "Налаштування…", self.open_settings)
        self.act_hide = QAction(menu_icon("hide"), "Сховати робота", self)
        self.act_hide.triggered.connect(self.toggle_visible)
        self.menu.addAction(self.act_hide)
        self.menu.addSeparator()
        self.menu.addAction(menu_icon("quit"), "Вийти", QApplication.quit)

    def set_show_thinking(self, show: bool) -> None:
        """Вмикач «Показувати хід думок»: міркування моделі — приглушеним блоком у стрічці."""
        if self.settings.show_thinking != show:
            self.settings.show_thinking = show
            self.settings.save()
        if self.act_thinking.isChecked() != show:
            self.act_thinking.setChecked(show)
        self.panel.set_show_thinking(show)
        if show and self.settings.think_mode == "off":
            self.panel.set_activity("💭 Модель зараз не міркує — увімкни «Думати перед відповіддю» в налаштуваннях")
        QTimer.singleShot(0, self.position_panel)

    def _connect_robot(self) -> None:
        self.robot.clicked.connect(self.on_robot_clicked)
        self.robot.petted.connect(self.on_petted)
        self.robot.files_dropped.connect(self.upload_files)
        self.robot.text_dropped.connect(self.on_text_dropped)
        self.robot.drag_moved.connect(self.on_drag)
        self.robot.drag_finished.connect(self.save_position)
        self.robot.context_requested.connect(lambda pos: self.menu.popup(pos.toPoint()))
        self.robot.hold_started.connect(self.start_listening)
        self.robot.hold_finished.connect(self.stop_listening)
        self.robot.double_clicked.connect(self.toggle_wake)
        self.robot.hold_enabled = self.voice_caps["stt"]

    def _build_tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("Kru")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(
            lambda r: self.toggle_visible() if r == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()

    def toggle_visible(self) -> None:
        from .menuicons import menu_icon

        if self.isVisible():
            self.hide()
            self.panel.hide()
            self.act_hide.setText("Показати робота")
            self.act_hide.setIcon(menu_icon("show"))
        else:
            self.show()
            self.act_hide.setText("Сховати робота")
            self.act_hide.setIcon(menu_icon("hide"))

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, voice_caps=self.voice_caps, preview=self.preview_voice)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        s = self.settings
        url = normalize_url(dlg.url.text())  # зберігаємо вже виправлену адресу — видно, куди підключаємось
        changed_conn = (url, dlg.token.text().strip()) != (s.server_url, s.token)
        s.server_url, s.token = url, dlg.token.text().strip()
        s.think_mode = dlg.think.currentData()
        s.show_stats = dlg.stats.isChecked()
        self.set_show_thinking(dlg.thinking.isChecked())
        s.show_tools = dlg.tools.isChecked()
        s.auto_files = dlg.auto_files.isChecked()
        s.font_size = dlg.font_px.value()
        s.voice_reply = dlg.voice_reply.currentData()
        s.voice_volume = dlg.voice_volume.value()
        restart_wake = dlg.wake_sensitivity.currentData() != s.wake_sensitivity and \
            (self.wake.active or self._wake_loading)
        s.wake_sensitivity = dlg.wake_sensitivity.currentData()
        s.wake_followup = dlg.wake_followup.isChecked()
        if restart_wake:
            self.set_wake(False)
            self.set_wake(True)
        s.voice_name = dlg.voice_name.currentData()
        s.voice_sound = dlg.voice_effect.currentData()
        s.voice_speed = round(dlg.voice_speed.value(), 2)
        self.panel.set_font_size(s.font_size)
        self.panel.set_show_tools(s.show_tools)
        if dlg.autostart is not None and dlg.autostart.isChecked() != autostart_enabled():
            set_autostart(dlg.autostart.isChecked())
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
        if self.player.playing or (self._tts_id and self.robot.state == "talking"):  # клік під час озвучення — замовкнути
            self.stop_speech()
            return
        self.conn.reconnect_now()
        if self.robot.state == "sleeping":
            self.robot.set_state("happy", 1.2)
        if self.panel.shown:
            self.panel.hide_panel()
        else:
            self.show_panel()

    def send_task(self, text: str, by_voice: bool = False) -> None:
        self.touch()
        self.stop_speech(quiet=True)
        self._voice_task = by_voice
        if not self.conn.send({"type": "task", "text": text, "session_id": self.settings.session_id,
                           "think": THINK_VALUE.get(self.settings.think_mode, "auto"), "auto_files": self.settings.auto_files,
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

    # ---------------- голос ----------------
    def start_listening(self) -> None:
        """Утримує персонажа (чи натиснув 🎤) — слухаємо мікрофон."""
        self.touch()
        if not self.voice_caps["stt"]:
            return
        if self.busy:
            self.panel.set_activity("⏳ Я ще працюю над попередньою задачею — скажеш, щойно закінчу")
            self.robot.set_state("waiting", 1.5, then=self.robot.state)
            return
        self.stop_speech()
        if self.wake.active:
            self.wake.pause()
        if self.recorder.start():
            self._state_before_listen = self.robot.state if self.robot.state != "listening" else "idle"
            self.robot.set_state("listening")
            self.panel.set_recording(True)
            self.panel.set_activity("🎤 Слухаю… відпусти, коли договориш")
            self._level_timer.start()
            self._feed_level()  # одразу, не чекаючи першого тіку таймера

    def stop_listening(self) -> None:
        if self.recorder.active:
            self.recorder.stop()

    def toggle_mic(self) -> None:
        if self.recorder.active:
            self.stop_listening()
        else:
            self.start_listening()

    def _end_listening(self) -> None:
        self.panel.set_recording(False)
        if self.wake.active:
            self.wake.listen()
        elif not self.player.playing:
            self._level_timer.stop()
            self.robot.voice_level = 0.0

    def _on_recorded(self, wav: bytes) -> None:
        self._end_listening()
        self._voice_seq += 1
        if not self.conn.send({"type": "voice_in", "id": f"v{self._voice_seq}",
                               "data": base64.b64encode(wav).decode("ascii")}):
            self._on_record_failed("немає з'єднання з сервером")
            return
        self.robot.set_state("thinking")
        self.panel.set_activity("🎤 Розпізнаю сказане…")

    def _on_record_failed(self, reason: str) -> None:
        self._end_listening()
        self.panel.set_activity("")
        self.panel.add_entry("error", f"Голос: {reason}")
        self.robot.set_state("sad", 2.0, then="idle")
        self.show_panel()

    def _on_voice_text(self, ev: dict) -> None:
        if self._wake_pending is not None and ev.get("id") == self._wake_pending:
            self._on_wake_text(ev)
            return
        text = " ".join(str(ev.get("text", "")).split())
        if not text:
            self.panel.set_activity("🎤 Не почув слів — спробуй ще раз, ближче до мікрофона")
            self.robot.set_state("sad", 1.5, then="idle")
            return
        self.panel.set_activity("")
        self.show_panel()
        self.send_task(text, by_voice=True)

    def _wants_speech(self) -> bool:
        mode = self.settings.voice_reply
        return self.voice_caps["tts"] and (self._wake_turn or mode == "always" or (mode == "voice" and self._voice_task))

    def _speak(self, text: str, voice=None, effect=None, speed=None, volume=None) -> None:
        s = self.settings
        if self._tts_id and not self._tts_done:
            self.conn.send({"type": "tts_stop", "id": self._tts_id})  # попереднє озвучення вже не потрібне
        self._tts_queue = []
        self._voice_seq += 1
        self._speech_volume = s.voice_volume if volume is None else volume
        self._tts_id, self._tts_done = f"t{self._voice_seq}", False
        # stream — частинами: перше речення звучить, поки сервер озвучує решту
        if not self.conn.send({"type": "tts", "id": self._tts_id, "text": text, "stream": True,
                               "voice": s.voice_name if voice is None else voice,
                               "effect": s.voice_sound if effect is None else effect,
                               "speed": s.voice_speed if speed is None else speed}):
            self._tts_id, self._tts_done = None, True

    def stop_speech(self, quiet: bool = False) -> None:
        """Замовкнути: зупинити звук і сказати серверу не озвучувати решту відповіді.
        quiet — без завершальних дій (стан робота, режим «Кру»): далі все одно піде нова задача."""
        waiting = self._tts_id is not None and not self.player.playing and not quiet  # звуку ще нема, а робот «говорить»
        if self._tts_id and not self._tts_done:
            self.conn.send({"type": "tts_stop", "id": self._tts_id})
        self._tts_id, self._tts_queue, self._tts_done = None, [], True
        if self.player.playing:
            self.player.stop()  # далі — _on_speech_done
        elif waiting:
            self._on_speech_done()

    def preview_voice(self, voice: str, effect: str, speed: float, volume: int) -> None:
        """«▶ Прослухати» в налаштуваннях: пробна фраза ще не збереженими голосом і звучанням."""
        if self.busy or self.recorder.active:
            return
        self._speak(PREVIEW_TEXT, voice, effect, speed, volume)

    def _on_tts_audio(self, ev: dict) -> None:
        if ev.get("id") != self._tts_id:
            return  # частина озвучення, яке вже перебили
        if ev.get("last", True):
            self._tts_done = True
        try:
            wav = base64.b64decode(str(ev.get("data", "")))
        except ValueError:
            return
        if self.recorder.active or self.busy:
            self.stop_speech()  # (і повертає режим «Кру» до слухання)
            return  # уже говорить сам користувач або пішла нова задача — стара відповідь неактуальна
        if self.player.playing:
            self._tts_queue.append(wav)  # попередня частина ще звучить
            return
        self._play_part(wav)

    def _play_part(self, wav: bytes) -> None:
        if self.player.play(wav, self._speech_volume / 100):
            self.robot.set_state("talking")
            self._level_timer.start()
        else:
            self.stop_speech()

    def _on_speech_done(self) -> None:
        if self._tts_id is not None:
            if self._tts_queue:  # наступна частина вже тут — одразу далі
                self._play_part(self._tts_queue.pop(0))
                return
            if not self._tts_done:
                return  # сервер ще озвучує наступне речення — «говорить» далі
            self._tts_id = None
        if not self.wake.active:
            self._level_timer.stop()
        self.robot.voice_level = 0.0
        if self.robot.state == "talking":
            self.robot.set_state("happy", 1.5)
            QTimer.singleShot(3300, self._back_to_idle)
        if self._wake_turn:
            self._after_wake_reply()
        elif self.wake.active and not self._wake_pending:
            QTimer.singleShot(300, self._wake_listen)  # відлуння власного голосу встигне затихнути

    def _feed_level(self) -> None:
        if self.recorder.active:
            self.robot.voice_level = self.recorder.level
        elif self.player.playing:
            self.robot.voice_level = self.player.level
        else:
            self.robot.voice_level = self.wake.level if self.wake.active else 0.0

    # ---------------- «завжди слухаю» ----------------
    def _wire_wake(self, listener) -> None:
        self.wake = listener
        listener.woke.connect(self._on_wake)
        listener.speech.connect(self._on_wake_speech)
        listener.utterance.connect(self._on_wake_utterance)
        listener.timeout.connect(self._on_wake_timeout)
        listener.failed.connect(self._on_wake_failed)
        listener.ready.connect(lambda: self.panel.set_activity("👂 Слухаю. Поклич: «Кру, …» — подвійний клік вимикає"))

    def toggle_wake(self) -> None:
        self.set_wake(not (self.wake.active or self._wake_loading))

    def _sync_wake_action(self, on: bool) -> None:
        if self.act_wake.isChecked() != on:
            self.act_wake.blockSignals(True)
            self.act_wake.setChecked(on)
            self.act_wake.blockSignals(False)

    def set_wake(self, on: bool, auto: bool = False) -> None:
        """Подвійний клік / пункт меню: робот постійно слухає й відгукується на «Кру, …»."""
        self.touch()
        if on == (self.wake.active or self._wake_loading):
            self._sync_wake_action(on)
            return
        if not on:
            self.wake.stop()
            self._wake_loading = self._wake_turn = False
            self._wake_pending = None
            self.robot.wake_mode = False
            self._sync_wake_action(False)
            if not self.player.playing and not self.recorder.active:
                self._level_timer.stop()
            self.settings.wake_enabled = False
            self.settings.save()
            self.panel.set_activity("👂 Режим «Кру» вимкнено")
            return
        problem = ""
        if not self.voice_caps["stt"]:
            problem = ("на сервері ще не встановлено розпізнавання мовлення: deploy/voice.sh install"
                       if self.conn.is_connected else "немає з'єднання з сервером")
        else:
            problem = wakewordmod.available() or (
                "" if self.voice_caps.get("wakeword") else
                "на сервері ще не навчено фразу — виконай там: kru wakeword train")
        if problem:
            self._sync_wake_action(False)
            if not auto:
                self.panel.add_entry("error", f"Режим «Кру»: {problem}")
                self.show_panel()
            return
        self.settings.wake_enabled = True
        self.settings.save()
        self.robot.wake_mode = True
        self._sync_wake_action(True)
        if wakewordmod.cached_version() == (self.voice_caps.get("wakeword") or {}).get("version"):
            self._start_wake()
            return
        self._wake_loading = True  # модель фрази (новіша) — забираємо з сервера, далі wake_model
        self.panel.set_activity("👂 Забираю з сервера модель фрази…")
        if not self.conn.send({"type": "wake_model"}):
            self._on_wake_model(OSError("немає з'єднання з сервером"))

    def _on_wake_model(self, error: Exception | None) -> None:
        if not self._wake_loading:
            return  # поки вантажилось, режим вимкнули
        self._wake_loading = False
        if error is not None:
            self.robot.wake_mode = False
            self._sync_wake_action(False)
            self.settings.wake_enabled = False
            self.settings.save()
            self.panel.add_entry("error", f"Режим «Кру»: не вдалося завантажити модель — {error}")
            self.show_panel()
            return
        self._start_wake()

    def _on_wake_bundle(self, ev: dict) -> None:
        """Сервер надіслав модель своєї фрази."""
        if not self._wake_loading:
            return
        if ev.get("error"):
            self._on_wake_model(RuntimeError(str(ev["error"])))
            return
        try:
            wakewordmod.save_bundle(ev)
        except (OSError, ValueError) as e:
            self._on_wake_model(e)
            return
        self._on_wake_model(None)

    def _start_wake(self) -> None:
        if not self.wake.start(wakewordmod.folder(), self.settings.wake_sensitivity):
            return  # причину покаже failed
        self.robot.wake_mode = True
        self._sync_wake_action(True)
        self._level_timer.start()
        self.panel.set_activity("👂 Вмикаю режим «Кру»…")  # модель вантажиться у фоні — далі сигнал ready

    def _wake_listen(self) -> None:
        if self.wake.active and not self.player.playing and not self.recorder.active:
            self.wake.listen()

    def _on_wake_failed(self, reason: str) -> None:
        self.set_wake(False)
        self.panel.add_entry("error", f"Режим «Кру»: {reason}")
        self.show_panel()

    def _on_wake(self) -> None:
        """Почув фразу — слухає питання."""
        self.touch()
        self.chime.play(self._chimes[True], self.settings.voice_volume / 200)
        self.robot.set_state("listening")
        self.panel.set_activity("🎤 Слухаю…")

    def _on_wake_speech(self) -> None:
        self.touch()
        self.robot.set_state("listening")
        self.panel.set_activity("🎤 Слухаю…")

    def _on_wake_timeout(self) -> None:
        if self.robot.state == "listening":
            self.robot.set_state("idle")
        self.panel.set_activity("")
        self.wake.listen()

    def _on_wake_utterance(self, wav: bytes, need_name: bool) -> None:
        """Фраза записана — на сервер (Whisper розпізнає, звертання прибере)."""
        if self.busy:  # агент ще працює — нову задачу не беремо (як і з чату)
            self.robot.set_state("waiting", 2.0, then="thinking")
            self.panel.set_activity("⏳ " + BUSY_PHRASE)
            self.wake.listen()
            return
        self._voice_seq += 1
        vid = f"w{self._voice_seq}"
        msg = {"type": "voice_in", "id": vid, "data": base64.b64encode(wav).decode("ascii")}
        # detected — фразу впізнала модель на ПК; followup — уточнення без неї: сервер відсіє вигадки Whisper на шумі
        msg["wake"] = "detected" if need_name else "followup"
        if not self.conn.send(msg):
            self.robot.set_state("idle")
            self.panel.set_activity("📡 Немає з'єднання з сервером")
            self.wake.listen()
            return
        self._wake_pending = vid
        self.robot.set_state("thinking")
        self.panel.set_activity("🎤 Розпізнаю…")

    def _on_wake_text(self, ev: dict) -> None:
        self._wake_pending = None
        text = " ".join(str(ev.get("text", "")).split())
        if ev.get("wake_only"):  # «Кру…» і пауза — чекаємо на саме питання
            self.chime.play(self._chimes[True], self.settings.voice_volume / 200)
            self.robot.set_state("listening")
            self.panel.set_activity("🎤 Так? Слухаю…")
            QTimer.singleShot(300, lambda: self.wake.active and self.wake.await_command(FOLLOWUP_SEC))
            return
        if ev.get("ignored") or not text:  # почулося — мовчки повертаємось до чекання
            if ev.get("heard"):
                wakemod.debug(f"сервер почув: {ev['heard']!r} — схоже на шум")
            if self.robot.state in {"thinking", "listening"}:
                self.robot.set_state("idle")
            self.panel.set_activity("")
            self.wake.listen()
            return
        self.chime.play(self._chimes[False], self.settings.voice_volume / 200)
        self._wake_turn = True
        self.show_panel()
        self.send_task(text, by_voice=True)

    def _after_wake_reply(self) -> None:
        """Відповідь на «Кру, …» прозвучала — ще кілька секунд слухаємо уточнення без імені."""
        self._wake_turn = False
        if not self.wake.active:
            return
        if self.settings.wake_followup:
            QTimer.singleShot(300, lambda: self.wake.active and self.wake.await_command(FOLLOWUP_SEC))
        else:
            QTimer.singleShot(300, self._wake_listen)

    def _set_voice_caps(self, caps) -> None:
        caps = caps if isinstance(caps, dict) else {}
        self.voice_caps = {"stt": caps.get("stt") is True, "tts": caps.get("tts") is True,
                           "effects": caps.get("effects") if isinstance(caps.get("effects"), dict) else {},
                           "voices": [v for v in (caps.get("voices") or caps.get("speakers") or [])
                                      if isinstance(v, str)],
                           "wakeword": caps.get("wakeword") if isinstance(caps.get("wakeword"), dict) else None}
        self.robot.hold_enabled = self.voice_caps["stt"]
        self.panel.set_voice(self.voice_caps["stt"])
        if self.settings.wake_enabled and self.voice_caps["stt"] and not (self.wake.active or self._wake_loading):
            self.set_wake(True, auto=True)  # режим був увімкнений до перезапуску

    def _on_panel_resized(self, width: int, feed_height: int) -> None:
        self.settings.panel_width, self.settings.panel_feed_height = width, feed_height
        self.settings.save()
        QTimer.singleShot(0, self.position_panel)

    def _on_font_changed(self, px: int) -> None:
        self.settings.font_size = px
        self.settings.save()
        QTimer.singleShot(0, self.position_panel)

    def retry_task(self) -> None:
        """«↻ Повторити»: сервер прибирає попередню спробу з чату й виконує останню задачу заново."""
        self.touch()
        text = self.panel.last_user_text()
        if self.busy or not text:
            return
        # text — для старого сервера, який не знає retry (він просто виконає задачу ще раз)
        if not self.conn.send({"type": "task", "text": text, "retry": True, "session_id": self.settings.session_id,
                               "think": THINK_VALUE.get(self.settings.think_mode, "auto"), "auto_files": self.settings.auto_files,
                               "tz": local_timezone()}):
            self.conn.reconnect_now()
            self.panel.set_activity("📡 Немає зв'язку з сервером — повтори, коли з'єднаюсь")
            return
        self.panel.trim_to_last_task()
        self.panel.set_plan([])
        self.busy = True
        self.panel.set_busy(True)
        self.panel.set_activity("↻ Повторюю задачу…")
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

    @staticmethod
    def download_dir():
        from pathlib import Path

        from PySide6.QtCore import QStandardPaths

        # системна тека «Завантаження» (у Fedora з українською — ~/Завантаження, у Windows — Downloads)
        down = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        return Path(down or Path.home()) / "Kru"

    def download_file(self, fid: str, path: str) -> None:
        """Кнопка «⬇ Завантажити» біля файлу від агента: просимо його в сервера (file_get)."""
        self.touch()
        if not self.conn.send({"type": "file_get", "id": fid, "path": path}):
            self.panel.file_failed(fid, "немає зв'язку з сервером")

    def _save_download(self, ev: dict) -> None:
        import base64
        import binascii
        from pathlib import Path

        fid = str(ev.get("id", ""))
        folder = self.download_dir()
        name = Path(str(ev.get("name") or "file").replace("\\", "/")).name.lstrip(".") or "file"
        name = "".join("_" if ch in '<>:"|?*' or ord(ch) < 32 else ch for ch in name)[:150]
        target, n = folder / name, 1
        while target.exists():
            n += 1
            target = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        try:
            data = base64.b64decode(str(ev.get("data", "")), validate=True)
            folder.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except (binascii.Error, ValueError, OSError) as e:
            self.panel.file_failed(fid, f"не вдалося записати: {e}")
            return
        self.panel.file_saved(fid, str(target))
        self.panel.set_activity(f"⬇ Збережено: {str(target).replace(str(Path.home()), '~', 1)}")
        self.robot.set_state("happy", 1.5)

    def _on_chat_event(self, ev: dict) -> None:
        t = ev["type"]
        if t == "chat_search":  # не чіпає відновлення поточного чату
            self.panel.set_search_results(ev.get("query", ""), ev.get("chats", []), self.settings.session_id)
            QTimer.singleShot(0, self.position_panel)
            return
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
                if isinstance(item.get("file"), dict):  # файл від агента — знову кнопка завантаження
                    self.panel.add_file(str(item["file"].get("path", "")), str(item["file"].get("name", "файл")))
                else:
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
    def _voice_active(self) -> bool:
        """Слухає чи говорить: обрив чи відновлення з'єднання не перебиває цю анімацію."""
        return self.recorder.active or self.player.playing

    def on_connected(self) -> None:
        if not self._voice_active():
            self.robot.set_state("happy", 1.5)
        text, self._pending_task = getattr(self, "_pending_task", None), None
        self._sent_on_connect = bool(text)  # тоді hello не перезавантажує чат: задача щойно пішла
        if text and self.conn.send({"type": "task", "text": text, "session_id": self.settings.session_id,
                           "think": THINK_VALUE.get(self.settings.think_mode, "auto"), "auto_files": self.settings.auto_files,
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
        if not self._voice_active():
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
        if t == "file":  # агент передав файл (send_file) — кнопка завантаження в стрічці
            self.panel.clear_draft()
            self.panel.add_file(str(ev.get("path", "")), str(ev.get("name", "файл")), ev.get("size"))
            return
        if t == "file_data":
            self._save_download(ev)
            return
        if t == "file_error":
            self.panel.file_failed(str(ev.get("id", "")), str(ev.get("message", "помилка")))
            return
        if t == "voice_text":
            self._on_voice_text(ev)
            return
        if t == "wake_model":
            self._on_wake_bundle(ev)
            return
        if t in {"voice_error", "tts_error"}:
            if t == "voice_error" and self._wake_pending is not None and ev.get("id") == self._wake_pending:
                self._wake_pending = None
                self.robot.set_state("sad", 1.5, then="idle")
                self.panel.set_activity(f"🎤 {ev.get('message', 'не вдалося розпізнати')}")
                self.wake.listen()
            elif t == "voice_error":
                self._on_record_failed(str(ev.get("message", "помилка")))
            else:
                if ev.get("id") != self._tts_id:
                    return
                self.panel.set_activity(f"🔇 {ev.get('message', 'не вдалося озвучити')}")
                self._tts_done = True
                if not self.player.playing and (self.robot.state == "talking" or self._wake_turn):
                    self._on_speech_done()
            return
        if t == "tts_audio":
            self._on_tts_audio(ev)
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
            self._set_voice_caps(ev.get("voice"))
            mic = " · 🎤" if self.voice_caps["stt"] else ""
            self.panel.set_status(f"{name} · підключено · {model}{eye}{mic}{user}", True)
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
                if self._last_stats:
                    self.panel.set_activity(self._last_stats)
                    self._last_stats = ""
        elif t == "plan":
            self.panel.set_plan(ev.get("steps", []))
        elif t == "progress":
            icon = {"deep_research": "📚", "delegate": "🤝"}.get(ev.get("tool"), "🔧")
            self.panel.set_activity(f"{icon} {ev.get('text', '')}"[:120])
        elif t == "thinking":
            # записуємо завжди: увімкнеш показ пізніше — міркування цієї розмови теж з'являться
            self.panel.append_thinking(ev.get("text", ""))
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
            self._last_stats = stats_line(ev.get("stats")) if self.settings.show_stats else ""
            self.panel.add_entry("assistant", text)
            if self._wants_speech() and text.strip():
                self._speak(text)  # «говорить» почне, щойно прийде звук
                self.robot.set_state("talking", 30, then="happy")
            else:
                talk = min(6.0, 1.0 + len(text) / 120)
                self.robot.set_state("talking", talk, then="happy")
                QTimer.singleShot(int(talk * 1000) + 1800, self._back_to_idle)
                if self._wake_turn:
                    self._after_wake_reply()
            self._voice_task = False
            if not self.panel.shown:
                self.show_panel()
            else:
                QTimer.singleShot(80, self.position_panel)
        elif t == "error":
            self.panel.add_entry("error", ev.get("message", "помилка"))
            self.robot.set_state("sad", 3.0)
            if self._wake_turn:
                self._after_wake_reply()

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


def _crash_log() -> None:
    """Якщо робот впаде на рівні C (Qt, звук, onnxruntime) — стек Python запишеться в crash.log, а не зникне."""
    import faulthandler

    base = os.environ.get("LOCALAPPDATA") if IS_WINDOWS else os.environ.get(
        "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share"))
    try:
        folder = os.path.join(base or os.path.expanduser("~"), "kru-robot")
        os.makedirs(folder, exist_ok=True)
        log = open(os.path.join(folder, "crash.log"), "a", encoding="utf-8")  # noqa: SIM115 — живе до кінця
        log.write(f"\n=== запуск {time.strftime('%Y-%m-%d %H:%M:%S')}, Python {sys.version.split()[0]} ===\n")
        log.flush()
        faulthandler.enable(file=log, all_threads=True)
        _crash_log.file = log  # не дати збирачу сміття закрити файл
    except OSError:
        faulthandler.enable()


def main() -> None:
    # На Wayland (типово у Fedora) вікна не можуть самі тримати себе поверх інших
    # і позиціонуватися — тому використовуємо XWayland (xcb), якщо користувач не вказав інше.
    if os.environ.get("WAYLAND_DISPLAY") and "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "xcb"
    _crash_log()
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
    app.setWindowIcon(app_icon())
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
    ("listening", "слухає (утримуєш робота й говориш)"),
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

    # «голос» для станів слухає/говорить: гучність хвилями, як у живому мовленні
    voice = QTimer(win, interval=40)

    def feed() -> None:
        t = time.monotonic()
        live = win.robot.state in {"listening", "talking"}
        win.robot.voice_level = (0.35 + 0.35 * math.sin(t * 9) * math.sin(t * 2.3)) if live else 0.0

    voice.timeout.connect(feed)
    voice.start()
    show()
    win.show_panel()
    if len(states) > 1:
        timer = QTimer(win, interval=4000)
        timer.timeout.connect(show)
        timer.start()
