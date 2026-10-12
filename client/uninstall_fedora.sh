#!/usr/bin/env bash
# Видалення десктоп-персонажа Kru (налаштування в ~/.config/bonsai-robot залишаються).
set -euo pipefail
rm -rf "${HOME}/.local/share/bonsai-robot"
for name in kru-robot bonsai-robot; do  # і нові, і старі назви
  rm -f "${HOME}/.local/bin/${name}" \
        "${HOME}/.local/share/applications/${name}.desktop" \
        "${HOME}/.local/share/applications/${name}-toggle.desktop" \
        "${HOME}/.config/autostart/${name}.desktop" \
        "${HOME}"/.local/share/icons/hicolor/*/apps/${name}.png
done
echo "Kru видалено."
