#!/usr/bin/env bash
# Оновлення агента Kru на цій машині: git pull + перевстановлення встановлених частин + перезапуск.
#   ./update.sh
# Сервер і персонаж встановлені у власні venv, тому самого `git pull` недостатньо — цей скрипт робить решту.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source deploy/venv-lib.sh

echo "==> git pull"
git pull --ff-only

updated=0
if [[ -x "${HOME}/.local/share/kru/venv/bin/python" ]]; then
  echo "==> Сервер агента"
  kru_venv_install "${HOME}/.local/share/kru/venv" ./server kru_agent kru kru_agent.cli:main
  if systemctl --user cat kru.service >/dev/null 2>&1; then
    systemctl --user restart kru.service
    sleep 2
    systemctl --user --no-pager --lines=0 status kru.service | head -3 || true
  else
    # пакет є, а сервісу немає: тут лише CLI (сервер працює на іншій машині або під іншим користувачем)
    echo "Сервісу kru у цього користувача немає — оновлено лише CLI kru."
  fi
  updated=1
fi

if [[ -x "${HOME}/.local/share/kru-robot/venv/bin/python" ]]; then
  echo "==> Персонаж"
  kru_venv_install "${HOME}/.local/share/kru-robot/venv" ./client kru_robot kru-robot kru_robot.app:main
  ./client/install_fedora.sh --refresh  # лаунчер, ярлики, іконка, Ctrl+Alt+B
  updated=1
fi
if [[ ${updated} == 1 && -x "${HOME}/.local/bin/kru-robot" && -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  # запущена стара копія побачить нову версію й поступиться їй
  ("${HOME}/.local/bin/kru-robot" >/dev/null 2>&1 &)
  echo "Персонажа перезапущено"
fi

if [[ ${updated} == 0 ]]; then
  echo "Не знайшов встановлених сервера чи персонажа. Спершу: server/install_server.sh або client/install_fedora.sh"
  exit 1
fi
echo "Готово ✅"
