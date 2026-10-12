#!/usr/bin/env bash
# Оновлення агента Kru на цій машині: git pull + перевстановлення встановлених частин + перезапуск.
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
  # команда тепер називається kru (без доступу до pypi нової назви у venv може не бути — робимо її з наявної)
  [[ -x "${SERVER_VENV}/bin/kru" ]] || cp "${SERVER_VENV}/bin/bonsai" "${SERVER_VENV}/bin/kru"
  mkdir -p "${HOME}/.local/bin"
  ln -sf "${SERVER_VENV}/bin/kru" "${HOME}/.local/bin/kru"
  rm -f "${HOME}/.local/bin/bonsai"
  ENV_FILE="${HOME}/.config/bonsai-agent.env"
  if [[ -f "${ENV_FILE}" ]]; then
    sed -i 's/^BONSAI_/KRU_/' "${ENV_FILE}"  # старі назви змінних → KRU_*
    if grep -qi '^KRU_MODEL=.*bonsai' "${ENV_FILE}"; then  # Bonsai прибрано — налаштування під Qwen3.6
      sed -i -e 's|^KRU_MODEL=.*|KRU_MODEL=qwen3.6-35b-a3b|' -e 's|^KRU_VISION=.*|KRU_VISION=auto|' \
             -e 's|^KRU_MAX_HISTORY_CHARS=.*|KRU_MAX_HISTORY_CHARS=120000|' "${ENV_FILE}"
      echo "Конфіг агента переведено на Qwen3.6 (модель запускається: deploy/model.sh start)"
    fi
  fi
  if systemctl --user cat bonsai-agent.service >/dev/null 2>&1; then
    UNIT="${HOME}/.config/systemd/user/bonsai-agent.service"
    if [[ -f "${UNIT}" ]]; then
      sed -i -e 's|/bin/bonsai serve|/bin/kru serve|' -e 's|^Description=.*|Description=Kru agent server (Qwen3.6 + tools)|' "${UNIT}"
      systemctl --user daemon-reload
    fi
    systemctl --user restart bonsai-agent.service
    sleep 2
    systemctl --user --no-pager --lines=0 status bonsai-agent.service | head -3 || true
  else
    # пакет є, а сервісу немає: тут лише CLI (сервер працює на іншій машині або під іншим користувачем)
    echo "Сервісу агента в цього користувача немає — оновлено лише CLI kru."
  fi
  updated=1
fi

ROBOT_VENV="${HOME}/.local/share/bonsai-robot/venv"
if [[ -x "${ROBOT_VENV}/bin/pip" ]]; then
  echo "==> Робот"
  reinstall "${ROBOT_VENV}" ./client bonsai_robot
  ./client/install_fedora.sh --refresh  # лаунчер kru-robot, ярлики, іконка, Ctrl+Alt+B
  if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    # запущена стара копія побачить нову версію й поступиться їй
    ("${HOME}/.local/bin/kru-robot" >/dev/null 2>&1 &)
    echo "Персонажа перезапущено"
  fi
  updated=1
fi

if [[ ${updated} == 0 ]]; then
  echo "Не знайшов встановлених сервера чи робота. Спершу: server/install_server.sh або client/install_fedora.sh"
  exit 1
fi
echo "Готово ✅"
