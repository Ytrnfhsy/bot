#!/usr/bin/env bash
# Видалення десктоп-персонажа Kru (налаштування в ~/.config/kru-robot залишаються).
set -euo pipefail
pkill -f "kru-robot/venv/bin/" 2>/dev/null || true
rm -rf "${HOME}/.local/share/kru-robot"
rm -f "${HOME}/.local/bin/kru-robot" \
      "${HOME}/.local/share/applications/kru-robot.desktop" \
      "${HOME}/.local/share/applications/kru-robot-toggle.desktop" \
      "${HOME}/.config/autostart/kru-robot.desktop" \
      "${HOME}"/.local/share/icons/hicolor/*/apps/kru-robot.png
echo "Kru видалено."
