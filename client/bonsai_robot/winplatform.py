"""Windows-специфічне: глобальна гаряча клавіша (RegisterHotKey) та автозапуск (реєстр HKCU\\...\\Run).

На інших системах модуль імпортується без помилок, а функції нічого не роблять.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QTimer

IS_WINDOWS = sys.platform == "win32"

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
VK_B = 0x42
HOTKEY_ID = 0xB0B
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Kru"
OLD_RUN_VALUES = ("BonsaiRobot",)  # назви зі старих версій — прибираємо, щоб не було двох автозапусків


class WinHotkey(QAbstractNativeEventFilter):
    """Ctrl+Alt+B у будь-якій програмі → callback. Спрацьовує миттєво, без запуску нового процесу."""

    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self.callback = callback
        self.hwnd: int | None = None
        self.registered = False

    def register(self, hwnd: int) -> bool:
        if not IS_WINDOWS:
            return False
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.hwnd = hwnd
        self.registered = bool(
            user32.RegisterHotKey(wintypes.HWND(hwnd), HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_B)
        )
        if self.registered:
            QCoreApplication.instance().installNativeEventFilter(self)
        return self.registered

    def unregister(self) -> None:
        if IS_WINDOWS and self.registered:
            import ctypes
            from ctypes import wintypes

            ctypes.windll.user32.UnregisterHotKey(wintypes.HWND(self.hwnd), HOTKEY_ID)
            QCoreApplication.instance().removeNativeEventFilter(self)
            self.registered = False

    def nativeEventFilter(self, event_type, message):  # noqa: N802 — назва з Qt
        if bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                QTimer.singleShot(0, self.callback)
                return True, 0
        return False, 0


def autostart_command() -> str:
    """Команда для автозапуску: зібраний .exe або pythonw -m bonsai_robot (без вікна консолі)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else exe}" -m bonsai_robot'


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            for name in (RUN_VALUE, *OLD_RUN_VALUES):
                try:
                    winreg.QueryValueEx(key, name)
                    return True
                except OSError:
                    continue
            return False
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    if not IS_WINDOWS:
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        for old in OLD_RUN_VALUES:
            try:
                winreg.DeleteValue(key, old)
            except OSError:
                pass
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except OSError:
                pass


def post_hotkey_for_test(hwnd: int) -> None:
    """Для самоперевірки: імітувати натискання Ctrl+Alt+B (надіслати WM_HOTKEY вікну)."""
    import ctypes
    from ctypes import wintypes

    ctypes.windll.user32.PostMessageW(wintypes.HWND(hwnd), WM_HOTKEY, HOTKEY_ID, 0)
