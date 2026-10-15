#!/usr/bin/env bash
# Пісочниця для команд агента Kru (run_shell / run_python у Docker).
#   deploy/sandbox.sh build    — зібрати образ kru-sandbox, увімкнути пісочницю й перезапустити агента
#   deploy/sandbox.sh off      — вимкнути (команди знову виконуються на сервері з підтвердженням)
#   deploy/sandbox.sh status   — чи увімкнено й чи працює
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${HOME}/.config/kru.env"
IMAGE="kru-sandbox"

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

check_docker() {
  if ! docker info >/dev/null 2>&1; then
    echo "✗ Docker недоступний для користувача $(id -un)."
    echo "  Додай його в групу docker і перелогінься: sudo usermod -aG docker $(id -un)"
    exit 1
  fi
}

case "${1:-}" in
  build)
    check_docker
    echo "Збираю образ ${IMAGE} (перший раз — кілька хвилин)…"
    docker build -t "${IMAGE}" "${HERE}/sandbox"
    tmp="$(mktemp -d)"
    if docker run --rm --user "$(id -u):$(id -g)" -v "${tmp}:/workspace" -w /workspace --read-only \
         --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges "${IMAGE}" \
         python -c "import pandas, matplotlib; open('ok.txt','w').write('ok'); print('пісочниця працює')" \
       && [[ -f "${tmp}/ok.txt" ]]; then
      rm -rf "${tmp}"
      set_key SANDBOX docker
      restart_agent
      echo "✓ Пісочницю увімкнено: shell і Python агента виконуються в Docker без запитів на дозвіл."
    else
      rm -rf "${tmp}"
      echo "✗ Тестовий запуск не вдався — пісочницю не ввімкнено."
      exit 1
    fi
    ;;
  off)
    set_key SANDBOX off
    restart_agent
    echo "Пісочницю вимкнено: команди знову виконуються на сервері з підтвердженням."
    ;;
  status)
    mode="$(grep -s '^KRU_SANDBOX=' "${ENV_FILE}" | tail -1 | cut -d= -f2 || true)"
    echo "Режим: ${mode:-off}"
    if docker image inspect "${IMAGE}" >/dev/null 2>&1; then
      echo "Образ ${IMAGE}: є ($(docker image inspect -f '{{.Created}}' "${IMAGE}" | cut -c1-10))"
    else
      echo "Образ ${IMAGE}: немає — deploy/sandbox.sh build"
    fi
    ;;
  *)
    echo "Використання: $0 build | off | status"
    exit 1
    ;;
esac
