#!/usr/bin/env bash
# Голос для агента Kru: розпізнавання мовлення (faster-whisper) і озвучення відповідей (Piper, український голос).
# Обидві моделі працюють на процесорі — відеопам'ять лишається моделі агента.
#   deploy/voice.sh install [модель]  — встановити (модель Whisper: turbo — типово, small — легша, large-v3 — найточніша)
#   deploy/voice.sh status            — що встановлено
#   deploy/voice.sh off | on          — вимкнути / увімкнути голос, не видаляючи моделей
set -euo pipefail

VENV="${HOME}/.local/share/kru/venv"
ENV_FILE="${HOME}/.config/kru.env"
DATA_DIR="$(grep -s '^KRU_DATA_DIR=' "${ENV_FILE}" | cut -d= -f2- || true)"
DATA_DIR="${DATA_DIR:-${HOME}/.local/share/kru}"
DATA_DIR="${DATA_DIR/#\~/${HOME}}"
VOICE_DIR="${DATA_DIR}/voice"
VOICE="uk_UA-ukrainian_tts-medium"
VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/uk/uk_UA/ukrainian_tts/medium/${VOICE}.onnx"

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
    echo "→ Встановлюю faster-whisper і piper-tts…"
    "${VENV}/bin/pip" install --quiet --disable-pip-version-check "faster-whisper>=1.1" "piper-tts>=1.3"
    echo "→ Український голос Piper (${VOICE}, ~75 МБ)…"
    mkdir -p "${VOICE_DIR}"
    for ext in onnx onnx.json; do
      [[ -s "${VOICE_DIR}/${VOICE}.${ext}" ]] && continue
      curl -fL --progress-bar -o "${VOICE_DIR}/${VOICE}.${ext}.part" "${VOICE_URL%.onnx}.${ext}"
      mv "${VOICE_DIR}/${VOICE}.${ext}.part" "${VOICE_DIR}/${VOICE}.${ext}"
    done
    echo "→ Модель розпізнавання Whisper «${model}» (завантажується один раз)…"
    "${VENV}/bin/python" -c "import sys; from faster_whisper import WhisperModel; WhisperModel(sys.argv[1], device='cpu', compute_type='int8')" "${model}"
    echo "→ Перевіряю озвучення…"
    "${VENV}/bin/python" - "${VOICE_DIR}/${VOICE}.onnx" <<'PY'
import io, sys, wave
from piper import PiperVoice
voice = PiperVoice.load(sys.argv[1])
buf = io.BytesIO()
with wave.open(buf, "wb") as w:
    voice.synthesize_wav("Привіт! Я тебе чую.", w)
print(f"   гаразд: {len(buf.getvalue()) // 1024} КБ звуку")
PY
    set_key VOICE auto
    set_key WHISPER_MODEL "${model}"
    restart_agent
    echo "Готово. Утримуй персонажа (або 🎤 у панелі) і говори; у Telegram — надсилай голосові."
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
    "${VENV}/bin/python" - <<PY
import importlib.util as u, pathlib
ok = lambda m: "так" if u.find_spec(m) else "ні"
print("faster-whisper:", ok("faster_whisper"))
print("piper-tts:     ", ok("piper"))
print("голос:         ", "так" if pathlib.Path("${VOICE_DIR}/${VOICE}.onnx").is_file() else "ні", "(${VOICE_DIR})")
PY
    grep -sE '^KRU_(VOICE|WHISPER_MODEL|PIPER_SPEAKER)=' "${ENV_FILE}" || echo "KRU_VOICE не задано (типово — auto)"
    ;;
  *)
    sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
