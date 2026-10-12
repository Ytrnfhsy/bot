#!/usr/bin/env bash
# Встановлення сервера агента Kru (Linux) як systemd user-сервісу.
#   ./install_server.sh
# Модель (Qwen3.6 у llama-server) запускається окремо: deploy/model.sh start
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# назви тек і сервісу — історичні (bonsai-*): так наявні чати, пам'ять і налаштування лишаються на місці
PREFIX="${HOME}/.local/share/bonsai-agent"
VENV="${PREFIX}/venv"
ENV_FILE="${HOME}/.config/bonsai-agent.env"

mkdir -p "${PREFIX}" "${HOME}/.config/systemd/user" "${HOME}/.local/bin"
python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --upgrade pip >/dev/null
"${VENV}/bin/pip" install "${HERE}"
ln -sf "${VENV}/bin/kru" "${HOME}/.local/bin/kru"
rm -f "${HOME}/.local/bin/bonsai"  # стара назва команди

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${HERE}/.env.example" "${ENV_FILE}"
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  sed -i "s|^KRU_TOKEN=.*|KRU_TOKEN=${TOKEN}|" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  echo "Створено ${ENV_FILE}"
else
  sed -i 's/^BONSAI_/KRU_/' "${ENV_FILE}"  # старі назви змінних → KRU_*
fi
# shellcheck disable=SC1090
source "${ENV_FILE}"

if curl -sf "${KRU_LLM_URL%/}/v1/models" >/dev/null; then
  echo "Модель ${KRU_LLM_URL} відповідає"
else
  echo "⚠ ${KRU_LLM_URL} поки не відповідає — запусти модель: deploy/model.sh start (агент підхопить її сам)"
fi

sed -e "s|@VENV@|${VENV}|g" -e "s|@ENV_FILE@|${ENV_FILE}|g" \
  "${HERE}/../deploy/bonsai-agent.service" > "${HOME}/.config/systemd/user/bonsai-agent.service"
systemctl --user daemon-reload
systemctl --user enable --now bonsai-agent.service
systemctl --user restart bonsai-agent.service
# щоб сервіс працював і без активного входу користувача:
loginctl enable-linger "$(id -un)" 2>/dev/null || true

echo
echo "Сервер Kru запущено: ws://$(hostname -I 2>/dev/null | awk '{print $1}'):${KRU_PORT:-8765}/ws"
echo "Токен: ${KRU_TOKEN}"
echo "Логи: journalctl --user -u bonsai-agent -f"
