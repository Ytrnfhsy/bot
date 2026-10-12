#!/usr/bin/env bash
# Встановлення сервера агента Kru як systemd user-сервісу.
#   ./install_server.sh
# Модель (Qwen3.6 у llama-server) запускається окремо: deploy/model.sh start
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../deploy/venv-lib.sh
source "${HERE}/../deploy/venv-lib.sh"
PREFIX="${HOME}/.local/share/kru"
VENV="${PREFIX}/venv"
ENV_FILE="${HOME}/.config/kru.env"
UNIT_DIR="${HOME}/.config/systemd/user"

mkdir -p "${PREFIX}" "${UNIT_DIR}" "${HOME}/.local/bin" "${HOME}/.config"
echo "==> Середовище ${VENV}"
kru_venv_install "${VENV}" "${HERE}" kru_agent kru kru_agent.cli:main
ln -sf "${VENV}/bin/kru" "${HOME}/.local/bin/kru"

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${HERE}/.env.example" "${ENV_FILE}"
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  sed -i "s|^KRU_TOKEN=.*|KRU_TOKEN=${TOKEN}|" "${ENV_FILE}"
  echo "Створено ${ENV_FILE}"
fi
chmod 600 "${ENV_FILE}"
# shellcheck disable=SC1090
source "${ENV_FILE}"
LLM_URL="${KRU_LLM_URL:-http://127.0.0.1:8085}"
if curl -sf "${LLM_URL%/}/v1/models" >/dev/null; then
  echo "Модель ${LLM_URL} відповідає"
else
  echo "⚠ ${LLM_URL} поки не відповідає — запусти модель: deploy/model.sh start (агент підхопить її сам)"
fi

sed -e "s|@VENV@|${VENV}|g" -e "s|@ENV_FILE@|${ENV_FILE}|g" "${HERE}/../deploy/kru.service" > "${UNIT_DIR}/kru.service"
systemctl --user daemon-reload
systemctl --user enable kru.service
systemctl --user restart kru.service
# щоб сервіс працював і без активного входу користувача:
loginctl enable-linger "$(id -un)" 2>/dev/null || true

echo
echo "Сервер Kru запущено: ws://$(hostname -I 2>/dev/null | awk '{print $1}'):${KRU_PORT:-8765}/ws"
echo "Токен: ${KRU_TOKEN}"
echo "Логи: journalctl --user -u kru -f"
