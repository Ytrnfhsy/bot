#!/usr/bin/env bash
# Оновлення Bonsai Agent на цій машині: git pull + перевстановлення встановлених частин + перезапуск.
#   ./update.sh
# Сервер і робот встановлені у власні venv, тому самого `git pull` недостатньо — цей скрипт робить решту.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "==> git pull"
git pull --ff-only

reinstall() {  # $1 — venv, $2 — тека проєкту, $3 — назва Python-пакета
  local pip=("$1/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir)
  if "${pip[@]}" "$2" && "${pip[@]}" --force-reinstall --no-deps "$2"; then
    return 0
  fi
  # pypi.org недоступний (немає інтернету, NetBird, IPv6…): оновлюємо сам код, копіюючи його у встановлений пакет
  echo "⚠ pip не зміг завантажити залежності — копіюю новий код напряму (залежності лишаються попередні)"
  local dest
  dest="$("$1/bin/python" -c "import importlib.util, os; print(os.path.dirname(importlib.util.find_spec('$3').origin))")"
  find "${dest}" -name '__pycache__' -type d -prune -exec rm -rf {} +
  cp -r "$2/$3/." "${dest}/"
  echo "Код оновлено у ${dest}"
}

updated=0
SERVER_VENV="${HOME}/.local/share/bonsai-agent/venv"
if [[ -x "${SERVER_VENV}/bin/pip" ]]; then
  echo "==> Сервер агента"
  reinstall "${SERVER_VENV}" ./server bonsai_agent
  if systemctl --user cat bonsai-agent.service >/dev/null 2>&1; then
    systemctl --user restart bonsai-agent.service
    sleep 2
    systemctl --user --no-pager --lines=0 status bonsai-agent.service | head -3 || true
  else
    # пакет є, а сервісу немає: тут лише CLI `bonsai` (сервер працює на іншій машині або під іншим користувачем)
    echo "Сервісу bonsai-agent у цього користувача немає — оновлено лише CLI bonsai."
  fi
  updated=1
fi

ROBOT_VENV="${HOME}/.local/share/bonsai-robot/venv"
if [[ -x "${ROBOT_VENV}/bin/pip" ]]; then
  echo "==> Робот"
  reinstall "${ROBOT_VENV}" ./client bonsai_robot
  if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v bonsai-robot >/dev/null; then
    # запущена стара копія побачить нову версію й поступиться їй
    (bonsai-robot >/dev/null 2>&1 &)
    echo "Робота перезапущено"
  fi
  updated=1
fi

if [[ ${updated} == 0 ]]; then
  echo "Не знайшов встановлених сервера чи робота. Спершу: server/install_server.sh або client/install_fedora.sh"
  exit 1
fi
echo "Готово ✅"
