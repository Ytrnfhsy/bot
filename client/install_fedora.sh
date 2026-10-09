#!/usr/bin/env bash
# Встановлення десктоп-робота Bonsai на Fedora.
#   ./install_fedora.sh [ws://SERVER:8765/ws] [TOKEN]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${HOME}/.local/share/bonsai-robot"
VENV="${PREFIX}/venv"
BIN="${HOME}/.local/bin"
APPS="${HOME}/.local/share/applications"
AUTOSTART="${HOME}/.config/autostart"

echo "==> Системні залежності (потрібен sudo)"
# xcb-util-cursor потрібен Qt6 для X11/XWayland; решта — для прозорих вікон і шрифтів
sudo dnf install -y python3 python3-pip xcb-util-cursor xcb-util-wm xcb-util-keysyms \
    libxkbcommon-x11 mesa-libEGL google-noto-emoji-color-fonts || true

echo "==> Віртуальне середовище: ${VENV}"
mkdir -p "${PREFIX}" "${BIN}" "${APPS}" "${AUTOSTART}"
python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --upgrade pip >/dev/null
"${VENV}/bin/pip" install "${HERE}"

echo "==> Лаунчер ${BIN}/bonsai-robot"
cat > "${BIN}/bonsai-robot" <<EOF
#!/usr/bin/env bash
# XWayland потрібен, щоб робот був поверх усіх вікон і пам'ятав позицію на GNOME/KDE Wayland
export QT_QPA_PLATFORM="\${QT_QPA_PLATFORM:-xcb}"
exec "${VENV}/bin/bonsai-robot" "\$@"
EOF
chmod +x "${BIN}/bonsai-robot"

sed "s|@EXEC@|${BIN}/bonsai-robot|" "${HERE}/packaging/bonsai-robot.desktop" > "${APPS}/bonsai-robot.desktop"
cp "${APPS}/bonsai-robot.desktop" "${AUTOSTART}/bonsai-robot.desktop"

echo "==> Гаряча клавіша Ctrl+Alt+B → поле вводу робота"
cat > "${APPS}/bonsai-robot-toggle.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Bonsai Robot — поле вводу
Comment=Показати/сховати поле вводу задачі
Exec=${BIN}/bonsai-robot --toggle
Icon=face-smile-big
Terminal=false
Categories=Utility;
X-KDE-Shortcuts=Ctrl+Alt+B
EOF
update-desktop-database "${APPS}" 2>/dev/null || true

HOTKEY_OK=0
case "${XDG_CURRENT_DESKTOP:-}" in
  *KDE*)
    KWC="$(command -v kwriteconfig6 || command -v kwriteconfig5 || true)"
    if [[ -n "${KWC}" ]]; then
      "${KWC}" --file kglobalshortcutsrc --group services --group bonsai-robot-toggle.desktop \
        --key _launch "Ctrl+Alt+B" && HOTKEY_OK=1
      # перезапуск служби глобальних шорткатів, щоб підхопила зміни
      systemctl --user restart plasma-kglobalaccel.service 2>/dev/null || true
    fi
    ;;
  *GNOME*)
    if command -v gsettings >/dev/null; then
      KB=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/bonsai-robot/
      LIST="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)"
      if [[ "${LIST}" != *"${KB}"* ]]; then
        NEW="$(python3 -c "import ast,sys; l=ast.literal_eval(sys.argv[1].replace('@as ','')); l.append(sys.argv[2]); print(str(l))" "${LIST}" "${KB}")"
        gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "${NEW}"
      fi
      S="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:${KB}"
      gsettings set "${S}" name "Bonsai Robot"
      gsettings set "${S}" command "${BIN}/bonsai-robot --toggle"
      gsettings set "${S}" binding "<Control><Alt>b"
      HOTKEY_OK=1
    fi
    ;;
esac

if [[ $# -ge 1 ]]; then
  CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/bonsai-robot"
  mkdir -p "${CFG_DIR}"
  "${VENV}/bin/python" - "$@" <<'PY'
import sys
from bonsai_robot.settings import Settings
s = Settings.load()
s.server_url = sys.argv[1]
if len(sys.argv) > 2:
    s.token = sys.argv[2]
s.save()
print("Налаштування збережено")
PY
fi

echo
echo "Готово! Запуск: bonsai-robot   (або знайди «Bonsai Robot» у меню)"
echo "Робот стартуватиме автоматично при вході в систему (вимкнути: rm ${AUTOSTART}/bonsai-robot.desktop)."
echo "Адресу сервера й токен можна змінити: ПКМ по роботу → Налаштування."
if [[ "${HOTKEY_OK}" == "1" ]]; then
  echo "Гаряча клавіша: Ctrl+Alt+B (якщо не спрацює одразу — перезайди в систему)."
else
  echo "Гарячу клавішу признач вручну: Параметри системи → Клавіатура → Скорочення →"
  echo "  додати застосунок «Bonsai Robot — поле вводу» (команда: ${BIN}/bonsai-robot --toggle)."
fi
