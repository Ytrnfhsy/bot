#!/usr/bin/env bash
# Голос для агента Kru: розпізнавання мовлення (faster-whisper). Озвучення відповідей — HolosTTS: deploy/holos.sh.
# Модель працює на процесорі — відеопам'ять лишається моделі агента.
#   deploy/voice.sh install [модель]  — встановити (модель Whisper: turbo — типово, small — легша, large-v3 — найточніша)
#   deploy/voice.sh status            — що встановлено
#   deploy/voice.sh off | on          — вимкнути / увімкнути голос, не видаляючи моделей
set -euo pipefail

VENV="${HOME}/.local/share/kru/venv"
ENV_FILE="${HOME}/.config/kru.env"

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

need_venv() {
  [[ -x "${VENV}/bin/python" ]] || { echo "Сервер Kru не встановлено (${VENV}). Спершу: server/install_server.sh"; exit 1; }
}

case "${1:-}" in
  install)
    need_venv
    model="${2:-turbo}"
    echo "→ Встановлюю faster-whisper…"
    "${VENV}/bin/pip" install --quiet --disable-pip-version-check "faster-whisper>=1.1"
    echo "→ Модель розпізнавання Whisper «${model}» (завантажується один раз)…"
    "${VENV}/bin/python" -c "import sys; from faster_whisper import WhisperModel; WhisperModel(sys.argv[1], device='cpu', compute_type='int8')" "${model}"
    set_key VOICE auto
    set_key WHISPER_MODEL "${model}"
    restart_agent
    echo "Готово. Утримуй персонажа (або 🎤 у панелі) і говори; у Telegram — надсилай голосові."
    echo "Озвучення відповідей — HolosTTS: deploy/holos.sh start"
    ;;
  off)
    set_key VOICE off
    restart_agent
    ;;
  on)
    set_key VOICE auto
    restart_agent
    ;;
  status)
    need_venv
    "${VENV}/bin/python" -c 'import importlib.util as u; print("faster-whisper:", "так" if u.find_spec("faster_whisper") else "ні")'
    grep -sE '^KRU_(VOICE|WHISPER_MODEL|TTS_URL)=' "${ENV_FILE}" || echo "KRU_VOICE не задано (типово — auto)"
    ;;
  *)
    sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
