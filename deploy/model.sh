#!/usr/bin/env bash
# Модель агента Kru — Qwen3.6-35B-A3B у llama-server (deploy/qwen36/docker-compose.yml).
#   deploy/model.sh start    — запустити (і надалі підніматиметься сама після перезавантаження)
#   deploy/model.sh stop     — зупинити
#   deploy/model.sh status   — що запущено і скільки зайнято VRAM
#
# Змінні: MODEL_DIR — де лежать .gguf (типово корінь репозиторію).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
COMPOSE="${HERE}/qwen36/docker-compose.yml"
MODEL_DIR="${MODEL_DIR:-${REPO}}"
MODEL_FILE="${MODEL_FILE:-Qwen3.6-35B-A3B-UD-Q4_K_M.gguf}"
MODEL_URL="https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/${MODEL_FILE}"
MMPROJ_FILE="${MMPROJ_FILE:-mmproj-Qwen3.6-35B-A3B-F16.gguf}"
MMPROJ_URL="https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/mmproj-F16.gguf"
ENV_FILE="${HOME}/.config/bonsai-agent.env"
LLM_URL="http://127.0.0.1:8085"
export MODEL_DIR

set_key() {  # set_key KEY VALUE — оновити (або додати) KRU_KEY у конфігу агента
  [[ -f "${ENV_FILE}" ]] || return 0
  sed -i -e "/^BONSAI_$1=/d" "${ENV_FILE}"
  if grep -q "^KRU_$1=" "${ENV_FILE}"; then
    sed -i "s|^KRU_$1=.*|KRU_$1=$2|" "${ENV_FILE}"
  else
    echo "KRU_$1=$2" >> "${ENV_FILE}"
  fi
}

retire_bonsai() {  # старий контейнер Bonsai займав той самий порт 8085 і піднімався після перезавантаження
  local old="${REPO}/docker-compose.yml"
  if [[ -f "${old}" ]] && grep -qi bonsai "${old}"; then
    echo "Прибираю старий контейнер Bonsai (${old}); файли моделі не чіпаю."
    docker compose -f "${old}" down || true
  fi
}

wait_ready() {
  echo -n "Чекаю, поки модель завантажиться"
  for _ in $(seq 1 120); do
    if curl -sf "${LLM_URL}/health" >/dev/null 2>&1; then
      echo " — готово."
      return 0
    fi
    echo -n "."
    sleep 5
  done
  echo
  echo "Модель не піднялася за 10 хв. Лог: docker compose -f ${COMPOSE} logs --tail 50"
  exit 1
}

case "${1:-}" in
  start)
    if [[ ! -f "${MODEL_DIR}/${MODEL_FILE}" ]]; then
      echo "Не знайдено ${MODEL_DIR}/${MODEL_FILE} (~22 ГБ). Завантаж:"
      echo "  curl -L -C - -o '${MODEL_DIR}/${MODEL_FILE}' '${MODEL_URL}'"
      exit 1
    fi
    if [[ -f "${MODEL_DIR}/${MMPROJ_FILE}" ]]; then
      echo "👁 Зір: увімкнено (${MMPROJ_FILE})"
    else
      echo "ℹ Зір вимкнено — для розуміння зображень завантаж (~0.9 ГБ) і запусти знову:"
      echo "  curl -L -C - -o '${MODEL_DIR}/${MMPROJ_FILE}' '${MMPROJ_URL}'"
    fi
    retire_bonsai
    docker compose -f "${COMPOSE}" up -d
    wait_ready
    set_key MODEL qwen3.6-35b-a3b
    set_key LLM_URL "${LLM_URL}"
    set_key BACKEND openai
    set_key VISION auto
    set_key MAX_HISTORY_CHARS 120000
    set_key REQUEST_TIMEOUT 1200
    if systemctl --user cat bonsai-agent.service >/dev/null 2>&1; then
      systemctl --user restart bonsai-agent.service && echo "Агента Kru перезапущено."
    fi
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null \
      | awk '{print "VRAM зайнято: " $0}' || true
    ;;
  stop)
    docker compose -f "${COMPOSE}" stop
    ;;
  status)
    docker ps --filter name=qwen36 --format '{{.Names}}: {{.Status}}'
    curl -s "${LLM_URL}/v1/models" | grep -o '"id":"[^"]*"' | head -1 || echo "llama-server не відповідає на ${LLM_URL}"
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null \
      | awk '{print "VRAM зайнято: " $0}' || true
    ;;
  *)
    echo "Використання: $0 start | stop | status"
    exit 1
    ;;
esac
