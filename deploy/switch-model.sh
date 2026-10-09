#!/usr/bin/env bash
# Перемикання моделі агента однією командою:
#   deploy/switch-model.sh qwen     — Qwen3.6-35B-A3B (deploy/qwen36/docker-compose.yml)
#   deploy/switch-model.sh bonsai   — Ternary Bonsai 2 27B (твій docker-compose.yml у корені репозиторію)
#   deploy/switch-model.sh status   — що зараз запущено
#
# Змінні: BONSAI_COMPOSE — шлях до compose з Bonsai (типово ~/bot/docker-compose.yml),
#         MODEL_DIR — де лежить .gguf Qwen (типово корінь репозиторію).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
QWEN_COMPOSE="${HERE}/qwen36/docker-compose.yml"
BONSAI_COMPOSE="${BONSAI_COMPOSE:-${REPO}/docker-compose.yml}"
QWEN_FILE="${MODEL_FILE:-Qwen3.6-35B-A3B-UD-Q4_K_M.gguf}"
QWEN_URL="https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/${QWEN_FILE}"
MMPROJ_FILE="${MMPROJ_FILE:-mmproj-Qwen3.6-35B-A3B-F16.gguf}"
MMPROJ_URL="https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/mmproj-F16.gguf"
ENV_FILE="${HOME}/.config/bonsai-agent.env"
LLM_URL="http://127.0.0.1:8085"

set_key() {  # set_key KEY VALUE — оновити (або додати) змінну в конфігу агента
  if grep -q "^$1=" "${ENV_FILE}"; then
    sed -i "s|^$1=.*|$1=$2|" "${ENV_FILE}"
  else
    echo "$1=$2" >> "${ENV_FILE}"
  fi
}

wait_ready() {
  echo -n "Чекаю, поки модель завантажиться"
  for _ in $(seq 1 120); do
    if curl -sf "${LLM_URL}/health" >/dev/null 2>&1; then
      echo " — готово."
      curl -s "${LLM_URL}/v1/models" | grep -o '"id":"[^"]*"' | head -1
      return 0
    fi
    echo -n "."
    sleep 5
  done
  echo
  echo "Модель не піднялася за 10 хв. Дивись лог: docker compose -f $1 logs --tail 50"
  exit 1
}

restart_agent() {
  if systemctl --user cat bonsai-agent.service >/dev/null 2>&1; then
    systemctl --user restart bonsai-agent.service
    echo "Агента перезапущено. Порада: у роботі натисни ⟲ (нова розмова)."
  else
    echo "Сервіс bonsai-agent не знайдено — перезапусти «bonsai serve» вручну."
  fi
}

[[ -f "${ENV_FILE}" ]] || { echo "Немає ${ENV_FILE} — спершу server/install_server.sh --llama-server"; exit 1; }

case "${1:-}" in
  qwen)
    model_path="${MODEL_DIR:-${REPO}}/${QWEN_FILE}"
    if [[ ! -f "${model_path}" ]]; then
      echo "Не знайдено ${model_path} (~22 ГБ). Завантаж:"
      echo "  curl -L -C - -o '${model_path}' '${QWEN_URL}'"
      exit 1
    fi
    if [[ -f "${MODEL_DIR:-${REPO}}/${MMPROJ_FILE}" ]]; then
      echo "👁 Зір: увімкнено (${MMPROJ_FILE})"
    else
      echo "ℹ Зір вимкнено — для розуміння зображень завантаж (~0.9 ГБ) і перезапусти цей скрипт:"
      echo "  curl -L -C - -o '${MODEL_DIR:-${REPO}}/${MMPROJ_FILE}' '${MMPROJ_URL}'"
    fi
    mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
    if (( mem_gb < 30 )); then
      echo "⚠ У системі ${mem_gb} ГБ RAM — для Qwen3.6-35B-A3B бажано ≥32 ГБ (частина ваг живе в RAM)."
      echo "  Може працювати повільно або впасти. Продовжити? [y/N]"
      read -r ans; [[ "${ans}" == "y" ]] || exit 1
    fi
    [[ -f "${BONSAI_COMPOSE}" ]] && docker compose -f "${BONSAI_COMPOSE}" stop || true
    MODEL_DIR="${MODEL_DIR:-${REPO}}" docker compose -f "${QWEN_COMPOSE}" up -d
    wait_ready "${QWEN_COMPOSE}"
    set_key BONSAI_MODEL qwen3.6-35b-a3b
    set_key BONSAI_VISION auto
    set_key BONSAI_MAX_HISTORY_CHARS 120000
    set_key BONSAI_REQUEST_TIMEOUT 1200
    restart_agent
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null \
      | awk '{print "VRAM зайнято: " $0}' || true
    ;;
  bonsai)
    [[ -f "${BONSAI_COMPOSE}" ]] || { echo "Не знайдено ${BONSAI_COMPOSE} (задай BONSAI_COMPOSE=…)"; exit 1; }
    docker compose -f "${QWEN_COMPOSE}" stop 2>/dev/null || true
    docker compose -f "${BONSAI_COMPOSE}" up -d
    wait_ready "${BONSAI_COMPOSE}"
    set_key BONSAI_MODEL bonsai-2-27b
    set_key BONSAI_VISION off
    set_key BONSAI_MAX_HISTORY_CHARS 150000
    set_key BONSAI_REQUEST_TIMEOUT 900
    restart_agent
    ;;
  status)
    docker ps --filter name=bonsai --filter name=qwen36 --format '{{.Names}}: {{.Status}}'
    grep -E '^BONSAI_(MODEL|LLM_URL)=' "${ENV_FILE}"
    ;;
  *)
    echo "Використання: $0 qwen | bonsai | status"
    exit 1
    ;;
esac
