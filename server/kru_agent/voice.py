"""Голос: розпізнавання мовлення (faster-whisper) і озвучення відповідей (Piper).

Обидві моделі працюють на CPU (відеопам'ять зайнята моделлю агента) і завантажуються при першому
зверненні. Встановлення бібліотек і українського голосу — deploy/voice.sh install.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import wave
from pathlib import Path
from typing import Any

log = logging.getLogger("kru.voice")

MAX_AUDIO = 10 * 1024 * 1024  # ~5 хв мовлення у WAV 16 кГц
MAX_SPEECH = 1500  # стільки символів відповіді озвучуємо (довше — до кінця речення)
DEFAULT_VOICE = "uk_UA-ukrainian_tts-medium.onnx"


class VoiceError(Exception):
    pass


def _importable(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


class Voice:
    def __init__(self, config) -> None:
        self.config = config
        self._stt: Any = None
        self._tts: Any = None
        self._stt_lock = asyncio.Lock()
        self._tts_lock = asyncio.Lock()

    # ---------------- що доступно ----------------
    @property
    def enabled(self) -> bool:
        return str(self.config.voice).lower() not in {"off", "false", "0", "no"}

    @property
    def voice_path(self) -> Path:
        return Path(self.config.piper_voice).expanduser() if self.config.piper_voice else \
            self.config.data_dir / "voice" / DEFAULT_VOICE

    @property
    def stt_ok(self) -> bool:
        return self.enabled and _importable("faster_whisper")

    @property
    def tts_ok(self) -> bool:
        return self.enabled and _importable("piper") and self.voice_path.is_file()

    def caps(self) -> dict[str, bool]:
        return {"stt": self.stt_ok, "tts": self.tts_ok}

    # ---------------- розпізнавання ----------------
    def _load_stt(self):
        if self._stt is None:
            from faster_whisper import WhisperModel

            log.info("Завантажую модель розпізнавання мовлення %s…", self.config.whisper_model)
            self._stt = WhisperModel(self.config.whisper_model, device=self.config.whisper_device,
                                     compute_type="int8")
        return self._stt

    def _transcribe(self, data: bytes) -> str:
        model = self._load_stt()
        segments, _info = model.transcribe(
            io.BytesIO(data), language=self.config.whisper_language or None, vad_filter=True, beam_size=5,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, data: bytes) -> str:
        """Аудіо (WAV, OGG/Opus з Telegram, MP3…) → текст."""
        if not self.stt_ok:
            raise VoiceError("Розпізнавання мовлення не встановлено на сервері: deploy/voice.sh install")
        if len(data) > MAX_AUDIO:
            raise VoiceError("Запис задовгий")
        async with self._stt_lock:
            try:
                return await asyncio.to_thread(self._transcribe, data)
            except VoiceError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("Розпізнавання не вдалося")
                raise VoiceError(f"Не вдалося розпізнати: {e}") from e

    # ---------------- озвучення ----------------
    def _load_tts(self):
        if self._tts is None:
            from piper import PiperVoice

            log.info("Завантажую голос %s…", self.voice_path.name)
            self._tts = PiperVoice.load(str(self.voice_path))
        return self._tts

    def _speak(self, text: str) -> bytes:
        voice = self._load_tts()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            try:
                from piper import SynthesisConfig

                speakers = int(getattr(voice.config, "num_speakers", 1) or 1)
                speaker = self.config.piper_speaker if 0 <= self.config.piper_speaker < speakers else None
                voice.synthesize_wav(text, wav, syn_config=SynthesisConfig(speaker_id=speaker))
            except ImportError:  # старіший piper-tts (1.2)
                voice.synthesize(text, wav, speaker_id=self.config.piper_speaker or None)
        return buf.getvalue()

    async def speak(self, text: str) -> bytes:
        """Текст відповіді → WAV."""
        if not self.tts_ok:
            raise VoiceError("Озвучення не встановлено на сервері: deploy/voice.sh install")
        speech = speech_text(text)
        if not speech:
            raise VoiceError("Нема чого озвучувати")
        async with self._tts_lock:
            try:
                return await asyncio.to_thread(self._speak, speech)
            except Exception as e:  # noqa: BLE001
                log.exception("Озвучення не вдалося")
                raise VoiceError(f"Не вдалося озвучити: {e}") from e


_CODE_BLOCK = re.compile(r"```.*?(```|$)", re.DOTALL)
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")


def speech_text(markdown: str, limit: int = MAX_SPEECH) -> str:
    """Відповідь у Markdown → текст для читання вголос: без коду, посилань, таблиць-розмітки й емодзі."""
    text = _CODE_BLOCK.sub(" (код показано в чаті) ", markdown)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # картинки
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # посилання — лише підпис
    text = _URL.sub("", text)
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if re.fullmatch(r"\|?\s*:?-{2,}.*", s) or re.fullmatch(r"[-*_]{3,}", s):
            continue  # роздільники таблиць і горизонтальні лінії
        s = re.sub(r"^#{1,6}\s*", "", s)  # заголовки
        s = re.sub(r"^(\s*[-*+]|\s*\d+[.)])\s+", "", s)  # маркери списків
        s = s.lstrip("> ")
        if "|" in s:  # рядок таблиці — клітинки через кому
            s = ", ".join(c.strip() for c in s.strip("|").split("|") if c.strip())
        s = _EMOJI.sub("", re.sub(r"[*_~]{1,3}", "", s)).strip()
        if s and re.search(r"\w", s):
            lines.append(s if s[-1] in ".!?:;…" else s + ".")
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,!?;:…])", r"\1", text)
    text = re.sub(r"([.!?])\.", r"\1", text)
    if len(text) > limit:
        cut = max(text.rfind(m, 0, limit) for m in (". ", "! ", "? "))
        text = text[: cut + 1 if cut > limit // 2 else limit].rstrip() + " … Решта — у чаті."
    return text
