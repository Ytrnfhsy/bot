#!/usr/bin/env bash
# Модель агента Kru — Qwen3.6-35B-A3B у llama-server (deploy/qwen36/docker-compose.yml).
#   deploy/model.sh start    — запустити (і надалі підніматиметься сама після перезавантаження)
#   deploy/model.sh stop     — зупинити
#   deploy/model.sh status   — що запущено і скільки зайнято VRAM
#   deploy/tune.sh           — підібрати --n-cpu-moe / контекст / ubatch під свою відеокарту
#                              (результат — у deploy/qwen36/.env, його читає compose і цей скрипт)
#
# Разом з основною моделлю запускається маленька модель ембедингів (CPU, :8086) — пам'ять агента
# шукається за змістом. Без неї агент шукає в пам'яті лише за словами.
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
EMBED_FILE="${EMBED_FILE:-Qwen3-Embedding-0.6B-Q8_0.gguf}"
EMBED_URL_DL="https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/${EMBED_FILE}"
ENV_FILE="${HOME}/.config/kru.env"
LLM_URL="http://127.0.0.1:8085"
EMBED_URL="http://127.0.0.1:8086"
export EMBED_FILE

TUNED="${HERE}/qwen36/.env"
CTX_SIZE="$(grep -s '^CTX_SIZE=' "${TUNED}" | tail -1 | cut -d= -f2 || true)"
CTX_SIZE="${CTX_SIZE:-65536}"
# ~1.8 символу на токен історії; решта контексту — системний промпт і результати інструментів
HISTORY_CHARS=$(( CTX_SIZE * 18 / 10 ))
export MODEL_DIR

set_key() {  # set_key KEY VALUE — оновити (або додати) KRU_KEY у конфігу агента
  [[ -f "${ENV_FILE}" ]] || return 0
  if grep -q "^KRU_$1=" "${ENV_FILE}"; then
    sed -i "s|^KRU_$1=.*|KRU_$1=$2|" "${ENV_FILE}"
  else
    echo "KRU_$1=$2" >> "${ENV_FILE}"
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
    SERVICES="qwen36"
    if [[ ! -f "${MODEL_DIR}/${EMBED_FILE}" ]]; then
      echo "🧠 Завантажую модель ембедингів для пам'яті за змістом (~0.6 ГБ)…"
      curl -fL -C - -o "${MODEL_DIR}/${EMBED_FILE}.part" "${EMBED_URL_DL}" \
        && mv "${MODEL_DIR}/${EMBED_FILE}.part" "${MODEL_DIR}/${EMBED_FILE}" \
        || echo "⚠ Не вдалося завантажити — пам'ять шукатиметься лише за словами. Спробуй пізніше: $0 start"
    fi
    if [[ -f "${MODEL_DIR}/${EMBED_FILE}" ]]; then
      SERVICES="qwen36 embed"
      echo "🧠 Пам'ять за змістом: увімкнено (${EMBED_FILE})"
    fi
    # shellcheck disable=SC2086
    docker compose -f "${COMPOSE}" up -d ${SERVICES}
    wait_ready
    set_key MODEL qwen3.6-35b-a3b
    set_key LLM_URL "${LLM_URL}"
    set_key BACKEND openai
    set_key VISION auto
    set_key MAX_HISTORY_CHARS "${HISTORY_CHARS}"
    set_key REQUEST_TIMEOUT 1200
    if [[ "${SERVICES}" == *embed* ]]; then
      set_key EMBED_URL "${EMBED_URL}"
    else
      set_key EMBED_URL ""
    fi
    if systemctl --user cat kru.service >/dev/null 2>&1; then
      systemctl --user restart kru.service && echo "Агента Kru перезапущено."
    fi
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null \
      | awk '{print "VRAM зайнято: " $0}' || true
    ;;
  stop)
    docker compose -f "${COMPOSE}" stop
    ;;
  status)
    docker ps --filter name=qwen36 --filter name=kru-embed --format '{{.Names}}: {{.Status}}'
    docker logs qwen36 2>&1 | grep -m1 -- '--n-cpu-moe' | sed 's/^\[qwen36\] /налаштування: /' || true
    [[ -f "${TUNED}" ]] && grep -m1 '^# підібрано' "${TUNED}" | sed 's/^# //' || true
    curl -s "${LLM_URL}/v1/models" | grep -o '"id":"[^"]*"' | head -1 || echo "llama-server не відповідає на ${LLM_URL}"
    curl -sf "${EMBED_URL}/health" >/dev/null 2>&1 && echo "🧠 ембединги: працюють (${EMBED_URL})" \
      || echo "🧠 ембединги: не запущено — пам'ять шукається лише за словами"
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null \
      | awk '{print "VRAM зайнято: " $0}' || true
    ;;
  *)
    echo "Використання: $0 start | stop | status"
    exit 1
    ;;
esac
