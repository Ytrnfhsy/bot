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

    def caps(self) -> dict:
        from .voice_fx import EFFECTS

        return {"stt": self.stt_ok, "tts": self.tts_ok,
                "effects": {k: e.title for k, e in EFFECTS.items()},
                "speakers": ["Лада", "Микита", "Тетяна"] if DEFAULT_VOICE in self.voice_path.name else []}

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
            decode_audio(data), language=self.config.whisper_language or None, vad_filter=True, beam_size=5,
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

    def options(self, speaker=None, effect=None, speed=None) -> tuple[int, str, float]:
        """Голос, звучання й темп: з запиту клієнта, інакше — з налаштувань сервера."""
        from .voice_fx import EFFECTS

        sp = speaker if isinstance(speaker, int) and not isinstance(speaker, bool) and speaker >= 0 \
            else self.config.piper_speaker
        fx = effect if effect in EFFECTS else (self.config.voice_effect if self.config.voice_effect in EFFECTS
                                               else "natural")
        try:
            sd = min(1.6, max(0.6, float(speed))) if speed is not None else 1.0
        except (TypeError, ValueError):
            sd = 1.0
        return sp, fx, sd

    def _speak(self, text: str, speaker: int, effect: str, speed: float) -> bytes:
        from .voice_fx import EFFECTS, apply

        fx = EFFECTS[effect]
        voice = self._load_tts()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            try:
                from piper import SynthesisConfig

                speakers = int(getattr(voice.config, "num_speakers", 1) or 1)
                # вища висота = синтезуємо повільніше на той самий коефіцієнт, а voice_fx «прискорює» назад
                voice.synthesize_wav(text, wav, syn_config=SynthesisConfig(
                    speaker_id=speaker if 0 <= speaker < speakers else None, length_scale=fx.pitch / speed))
            except ImportError:  # старіший piper-tts (1.2)
                voice.synthesize(text, wav, speaker_id=speaker or None, length_scale=fx.pitch / speed)
        audio = buf.getvalue()
        return audio if effect == "natural" else apply(audio, fx)

    async def speak(self, text: str, speaker=None, effect=None, speed=None) -> bytes:
        """Текст відповіді → WAV (голос speaker, звучання effect — див. voice_fx.EFFECTS, темп speed)."""
        if not self.tts_ok:
            raise VoiceError("Озвучення не встановлено на сервері: deploy/voice.sh install")
        speech = speech_text(text)
        if not speech:
            raise VoiceError("Нема чого озвучувати")
        async with self._tts_lock:
            try:
                return await asyncio.to_thread(self._speak, speech, *self.options(speaker, effect, speed))
            except Exception as e:  # noqa: BLE001
                log.exception("Озвучення не вдалося")
                raise VoiceError(f"Не вдалося озвучити: {e}") from e


def decode_audio(data: bytes, rate: int = 16000):
    """Аудіо → numpy float32, моно, 16 кГц (те, що їсть Whisper).

    Декодуємо самі, а не через faster_whisper.decode_audio: та передає в av.open() аргумент
    metadata_errors, якого PyAV 15+ уже не приймає. WAV (з персонажа) читаємо стандартним wave,
    решту (OGG/Opus з Telegram, MP3…) — через PyAV.
    """
    import numpy as np

    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(data)) as w:
                if w.getsampwidth() == 2:
                    ch, src_rate = w.getnchannels(), w.getframerate()
                    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
                    if ch > 1:
                        pcm = pcm[: len(pcm) // ch * ch].reshape(-1, ch).mean(axis=1)
                    if src_rate != rate and len(pcm):
                        n = int(round(len(pcm) * rate / src_rate))
                        pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm)
                    return pcm.astype(np.float32)
        except (wave.Error, EOFError):
            pass  # нестандартний WAV — хай розбирається PyAV
    import av

    chunks = []
    with av.open(io.BytesIO(data), mode="r") as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # залишок у буфері ресемплера
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return (np.concatenate(chunks).astype(np.float32) / 32768).astype(np.float32)


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
