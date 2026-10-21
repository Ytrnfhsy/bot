#!/usr/bin/env bash
# Голос Kru через HolosTTS (deploy/holos/docker-compose.yml, 127.0.0.1:8890): природна українська мова,
# 27 голосів, числа й наголоси — сам. Розпізнавання мовлення лишається за Whisper (deploy/voice.sh install).
#   deploy/holos.sh start   — завантажити й запустити (перший раз — ~3,4 ГБ моделей), підключити до агента
#   deploy/holos.sh stop    — зупинити (агент відповідатиме лише текстом)
#   deploy/holos.sh status  — чи працює, які голоси
#   deploy/holos.sh test [голос] "текст"  — озвучити фразу у файл holos-test.wav
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="${HERE}/holos/docker-compose.yml"
ENV_FILE="${HOME}/.config/kru.env"
PORT="${HOLOS_PORT:-8890}"
URL="http://127.0.0.1:${PORT}"
DATA_DIR="$(grep -s '^KRU_DATA_DIR=' "${ENV_FILE}" | cut -d= -f2- || true)"
DATA_DIR="${DATA_DIR:-${HOME}/.local/share/kru}"
export HOLOS_DATA="${HOLOS_DATA:-${DATA_DIR/#\~/${HOME}}/holos}"
export HOLOS_PORT="${PORT}" PUID="$(id -u)" PGID="$(id -g)"

set_key() {  # set_key KEY VALUE — оновити (або додати) KRU_KEY у конфігу агента
  [[ -f "${ENV_FILE}" ]] || return 0
  if grep -q "^KRU_$1=" "${ENV_FILE}"; then
    sed -i "s|^KRU_$1=.*|KRU_$1=$2|" "${ENV_FILE}"
  else
    echo "KRU_$1=$2" >> "${ENV_FILE}"
  fi
}

restart_agent() {
  if systemctl --user cat kru.service >/dev/null 2>&1; then
    systemctl --user restart kru.service && echo "Агента Kru перезапущено."
  fi
}

voices() {
  curl -sf "${URL}/v1/audio/voices" | python3 -c 'import json,sys; print(", ".join(json.load(sys.stdin)["voices"]))'
}

case "${1:-}" in
  start)
    mkdir -p "${HOLOS_DATA}"  # інакше Docker створить теку від root, і контейнер не зможе писати моделі
    docker compose -f "${COMPOSE}" pull
    docker compose -f "${COMPOSE}" up -d
    echo "Перший запуск завантажує ~3,4 ГБ моделей у ${HOLOS_DATA} — це може тривати кілька хвилин."
    echo -n "Чекаю на HolosTTS"
    for _ in $(seq 1 180); do
      if curl -sf "${URL}/health" >/dev/null 2>&1 && voices >/dev/null 2>&1; then
        echo " — готово."
        echo "Голоси: $(voices)"
        echo -n "Пробна фраза (прогрів моделей)… "
        curl -sf -o /dev/null -X POST "${URL}/v1/audio/speech" -H 'Content-Type: application/json' \
          -d '{"input": "Привіт! Я Kru.", "response_format": "wav"}' --max-time 300 && echo "гаразд" || echo "ще не готово"
        set_key TTS_URL "${URL}"
        restart_agent
        echo "У роботі: Налаштування → Голос — вибери голос і натисни «▶ Прослухати»."
        exit 0
      fi
      echo -n "."
      sleep 5
    done
    echo
    echo "HolosTTS не відповів за 15 хвилин. Лог: docker logs --tail 50 kru-holos"
    exit 1
    ;;
  stop)
    docker compose -f "${COMPOSE}" stop
    set_key TTS_URL ""
    restart_agent
    ;;
  status)
    docker ps --filter name=kru-holos --format '{{.Names}}: {{.Status}}'
    if curl -sf "${URL}/health"; then
      echo
      echo "Голоси: $(voices)"
    else
      echo "HolosTTS не відповідає на ${URL}"
    fi
    grep -s '^KRU_TTS_' "${ENV_FILE}" || echo "KRU_TTS_URL не задано — агент не озвучує відповіді"
    ;;
  test)
    shift
    voice=""
    if [[ $# -ge 2 ]]; then voice="$1"; shift; fi
    text="${1:-Привіт! Я Kru, твій помічник. Сьогодні 20 жовтня, на вулиці +12 градусів.}"
    body="$(python3 -c 'import json,sys; d={"input": sys.argv[1], "response_format": "wav"}; sys.argv[2] and d.update(voice=sys.argv[2]); print(json.dumps(d))' "${text}" "${voice}")"
    curl -sf -X POST "${URL}/v1/audio/speech" -H 'Content-Type: application/json' -d "${body}" -o holos-test.wav \
      --max-time 300 && echo "Збережено holos-test.wav" || { echo "Не вдалося — deploy/holos.sh status"; exit 1; }
    ;;
  *)
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
