#!/usr/bin/env bash
# Встановлення сервера Bonsai Agent (Linux) як systemd user-сервісу.
#   ./install_server.sh                 — модель через Ollama
#   ./install_server.sh --llama-server  — модель через llama-server у Docker (пресет .env.llama-server)
#   PULL_MODEL=1 ./install_server.sh    — заодно завантажити модель у Ollama
set -euo pipefail

PRESET=".env.example"
[[ "${1:-}" == "--llama-server" ]] && PRESET=".env.llama-server"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${HOME}/.local/share/bonsai-agent"
VENV="${PREFIX}/venv"
ENV_FILE="${HOME}/.config/bonsai-agent.env"

mkdir -p "${PREFIX}" "${HOME}/.config/systemd/user" "${HOME}/.local/bin"
python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --upgrade pip >/dev/null
"${VENV}/bin/pip" install "${HERE}"
ln -sf "${VENV}/bin/bonsai" "${HOME}/.local/bin/bonsai" 2>/dev/null || true

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${HERE}/${PRESET}" "${ENV_FILE}"
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  sed -i "s|^BONSAI_TOKEN=.*|BONSAI_TOKEN=${TOKEN}|" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  echo "Створено ${ENV_FILE}"
fi
# shellcheck disable=SC1090
source "${ENV_FILE}"

if [[ "${BONSAI_BACKEND:-ollama}" == "openai" ]]; then
  if curl -sf "${BONSAI_LLM_URL%/}/v1/models" >/dev/null; then
    echo "LLM-сервер ${BONSAI_LLM_URL} відповідає"
  else
    echo "⚠ ${BONSAI_LLM_URL} поки не відповідає — запусти модель: docker compose up -d (агент підхопить її сам)"
  fi
else
  command -v ollama >/dev/null || {
    echo "Ollama не знайдено. Встанови: curl -fsSL https://ollama.com/install.sh | sh"
    echo "(або використовуй llama-server: ./install_server.sh --llama-server)"; exit 1; }
  if [[ "${PULL_MODEL:-0}" == "1" ]]; then
    ollama pull "${BONSAI_MODEL}"
  fi
fi

sed -e "s|@VENV@|${VENV}|g" -e "s|@ENV_FILE@|${ENV_FILE}|g" \
  "${HERE}/../deploy/bonsai-agent.service" > "${HOME}/.config/systemd/user/bonsai-agent.service"
systemctl --user daemon-reload
systemctl --user enable --now bonsai-agent.service
# щоб сервіс працював і без активного входу користувача:
loginctl enable-linger "$(id -un)" 2>/dev/null || true

echo
echo "Сервер запущено: ws://$(hostname -I 2>/dev/null | awk '{print $1}'):${BONSAI_PORT:-8765}/ws"
echo "Токен: ${BONSAI_TOKEN}"
echo "Логи: journalctl --user -u bonsai-agent -f"
