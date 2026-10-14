#!/usr/bin/env bash
# Власний пошуковик SearXNG для агента Kru (deploy/searxng/docker-compose.yml, 127.0.0.1:8888).
#   deploy/searxng.sh start    — запустити (і надалі підніматиметься сам після перезавантаження)
#   deploy/searxng.sh stop     — зупинити (агент повернеться до DuckDuckGo)
#   deploy/searxng.sh status   — чи працює і чи віддає результати
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="${HERE}/searxng/docker-compose.yml"
export SEARXNG_CONFIG="${SEARXNG_CONFIG:-${HOME}/.config/kru-searxng}"
ENV_FILE="${HOME}/.config/kru.env"
URL="http://127.0.0.1:8888"

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

write_settings() {
  mkdir -p "${SEARXNG_CONFIG}"
  local file="${SEARXNG_CONFIG}/settings.yml"
  [[ -f "${file}" ]] && return 0
  local secret
  secret="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat > "${file}" <<YAML
# Налаштування SearXNG для агента Kru (створено deploy/searxng.sh). Решта — типові налаштування SearXNG.
use_default_settings: true
general:
  instance_name: "Kru search"
search:
  safe_search: 0
  autocomplete: ""
  formats: [html, json]   # json потрібен агенту
server:
  secret_key: "${secret}"
  limiter: false          # доступ лише з цієї машини — обмежувач запитів не потрібен
  image_proxy: false
  public_instance: false
ui:
  static_use_hash: true
YAML
  echo "Створено ${file}"
}

case "${1:-}" in
  start)
    write_settings
    docker compose -f "${COMPOSE}" up -d
    echo -n "Чекаю на SearXNG"
    for _ in $(seq 1 30); do
      if curl -sf "${URL}/search?q=test&format=json" >/dev/null 2>&1; then
        echo " — готово."
        set_key SEARXNG_URL "${URL}"
        restart_agent
        exit 0
      fi
      echo -n "."
      sleep 2
    done
    echo
    echo "SearXNG не відповів за хвилину. Лог: docker compose -f ${COMPOSE} logs --tail 50"
    exit 1
    ;;
  stop)
    docker compose -f "${COMPOSE}" stop
    set_key SEARXNG_URL ""
    restart_agent
    ;;
  status)
    docker ps --filter name=kru-searxng --format '{{.Names}}: {{.Status}}'
    if out="$(curl -sf "${URL}/search?q=kru&format=json" 2>/dev/null)"; then
      n="$(printf '%s' "${out}" | grep -o '"url"' | wc -l)"
      echo "🔎 SearXNG працює (${URL}), результатів на тестовий запит: ${n}"
    else
      echo "🔎 SearXNG не відповідає — агент шукає через DuckDuckGo"
    fi
    ;;
  *)
    echo "Використання: $0 start | stop | status"
    exit 1
    ;;
esac
