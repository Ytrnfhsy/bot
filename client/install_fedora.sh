#!/usr/bin/env bash
# Встановлення десктоп-персонажа Kru на Fedora.
#   ./install_fedora.sh [ws://SERVER:8765/ws] [TOKEN]
#   ./install_fedora.sh --refresh   — лише оновити лаунчер, ярлики, іконку й гарячу клавішу (так робить update.sh)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# назва теки історична (bonsai-robot) — так оновлення не губить середовище й налаштування
PREFIX="${HOME}/.local/share/bonsai-robot"
VENV="${PREFIX}/venv"
BIN="${HOME}/.local/bin"
APPS="${HOME}/.local/share/applications"
AUTOSTART="${HOME}/.config/autostart"
REFRESH=0
[[ "${1:-}" == "--refresh" ]] && { REFRESH=1; shift; }

mkdir -p "${PREFIX}" "${BIN}" "${APPS}" "${AUTOSTART}"
if [[ ${REFRESH} == 0 ]]; then
  echo "==> Системні залежності (потрібен sudo)"
  # xcb-util-cursor потрібен Qt6 для X11/XWayland; решта — для прозорих вікон і шрифтів
  sudo dnf install -y python3 python3-pip xcb-util-cursor xcb-util-wm xcb-util-keysyms \
      libxkbcommon-x11 mesa-libEGL google-noto-emoji-color-fonts || true

  echo "==> Віртуальне середовище: ${VENV}"
  python3 -m venv "${VENV}"
  "${VENV}/bin/pip" install --upgrade pip >/dev/null
  "${VENV}/bin/pip" install "${HERE}"
fi
# без нової назви в venv (оновлення без доступу до pypi) — створюємо її з наявної
[[ -x "${VENV}/bin/kru-robot" ]] || cp "${VENV}/bin/bonsai-robot" "${VENV}/bin/kru-robot"

echo "==> Прибираю старі ярлики Bonsai Robot"
rm -f "${BIN}/bonsai-robot" "${APPS}/bonsai-robot.desktop" "${APPS}/bonsai-robot-toggle.desktop" \
      "${AUTOSTART}/bonsai-robot.desktop" "${HOME}"/.local/share/icons/hicolor/*/apps/bonsai-robot.png

echo "==> Лаунчер ${BIN}/kru-robot"
cat > "${BIN}/kru-robot" <<LAUNCHER
#!/usr/bin/env bash
# XWayland потрібен, щоб персонаж був поверх усіх вікон і пам'ятав позицію на GNOME/KDE Wayland
export QT_QPA_PLATFORM="\${QT_QPA_PLATFORM:-xcb}"
exec "${VENV}/bin/kru-robot" "\$@"
LAUNCHER
chmod +x "${BIN}/kru-robot"

echo "==> Іконка програми"
QT_QPA_PLATFORM=offscreen "${VENV}/bin/python" -m bonsai_robot.icon "${HOME}/.local/share/icons"
gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" 2>/dev/null || true

sed "s|@EXEC@|${BIN}/kru-robot|" "${HERE}/packaging/kru-robot.desktop" > "${APPS}/kru-robot.desktop"
cp "${APPS}/kru-robot.desktop" "${AUTOSTART}/kru-robot.desktop"

echo "==> Гаряча клавіша Ctrl+Alt+B → поле вводу"
cat > "${APPS}/kru-robot-toggle.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Kru — поле вводу
Comment=Показати/сховати поле вводу задачі
Exec=${BIN}/kru-robot --toggle
Icon=kru-robot
Terminal=false
Categories=Utility;
X-KDE-Shortcuts=Ctrl+Alt+B
DESKTOP
update-desktop-database "${APPS}" 2>/dev/null || true
# KDE: оновити кеш меню, щоб нова іконка з'явилась одразу
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 --noincremental >/dev/null 2>&1 || true

HOTKEY_OK=0
case "${XDG_CURRENT_DESKTOP:-}" in
  *KDE*)
    KWC="$(command -v kwriteconfig6 || command -v kwriteconfig5 || true)"
    if [[ -n "${KWC}" ]]; then
      # стара прив'язка займала б ту саму комбінацію
      "${KWC}" --file kglobalshortcutsrc --group services --group bonsai-robot-toggle.desktop \
        --key _launch --delete 2>/dev/null || true
      "${KWC}" --file kglobalshortcutsrc --group services --group kru-robot-toggle.desktop \
        --key _launch "Ctrl+Alt+B" && HOTKEY_OK=1
      # перезапуск служби глобальних шорткатів, щоб підхопила зміни
      systemctl --user restart plasma-kglobalaccel.service 2>/dev/null || true
    fi
    ;;
  *GNOME*)
    if command -v gsettings >/dev/null; then
      BASE=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
      KB="${BASE}/kru-robot/"
      OLD="${BASE}/bonsai-robot/"
      LIST="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)"
      NEW="$(python3 -c "import ast,sys; l=[x for x in ast.literal_eval(sys.argv[1].replace('@as ','')) if x!=sys.argv[3]]; l+=[sys.argv[2]] if sys.argv[2] not in l else []; print(str(l))" "${LIST}" "${KB}" "${OLD}")"
      gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "${NEW}"
      S="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:${KB}"
      gsettings set "${S}" name "Kru"
      gsettings set "${S}" command "${BIN}/kru-robot --toggle"
      gsettings set "${S}" binding "<Control><Alt>b"
      HOTKEY_OK=1
    fi
    ;;
esac

if [[ $# -ge 1 ]]; then
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

[[ ${REFRESH} == 1 ]] && exit 0
echo
echo "Готово! Запуск: kru-robot   (або знайди «Kru» у меню)"
echo "Kru стартуватиме автоматично при вході в систему (вимкнути: rm ${AUTOSTART}/kru-robot.desktop)."
echo "Адресу сервера й токен можна змінити: ПКМ по персонажу → Налаштування."
if [[ "${HOTKEY_OK}" == "1" ]]; then
  echo "Гаряча клавіша: Ctrl+Alt+B (якщо не спрацює одразу — перезайди в систему)."
else
  echo "Гарячу клавішу признач вручну: Параметри системи → Клавіатура → Скорочення →"
  echo "  додати застосунок «Kru — поле вводу» (команда: ${BIN}/kru-robot --toggle)."
fi
