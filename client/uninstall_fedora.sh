#!/usr/bin/env bash
# Видалення десктоп-робота Bonsai (налаштування в ~/.config/bonsai-robot залишаються).
set -euo pipefail
rm -rf "${HOME}/.local/share/bonsai-robot"
rm -f "${HOME}/.local/bin/bonsai-robot" \
      "${HOME}/.local/share/applications/bonsai-robot.desktop" \
      "${HOME}/.config/autostart/bonsai-robot.desktop"
echo "Bonsai Robot видалено."
