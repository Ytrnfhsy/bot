"""Голос: розпізнавання мовлення (faster-whisper) і озвучення відповідей.

Озвучення — HolosTTS (окремий Docker-сервіс, deploy/holos.sh start; природний український голос, 27 дикторів)
або, якщо його немає, Piper. Усе працює на CPU (відеопам'ять зайнята моделлю агента).
Whisper і Piper — deploy/voice.sh install; моделі завантажуються при першому зверненні.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
import wave
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("kru.voice")

MAX_AUDIO = 10 * 1024 * 1024  # ~5 хв мовлення у WAV 16 кГц
MAX_SPEECH = 1500  # стільки символів відповіді озвучуємо (довше — до кінця речення)
DEFAULT_VOICE = "uk_UA-ukrainian_tts-medium.onnx"
PIPER_SPEAKERS = ["Лада", "Микита", "Тетяна"]  # дикторки/диктори голосу ukrainian_tts
VOICES_TTL = 300.0  # як часто перепитувати HolosTTS про список голосів


class VoiceError(Exception):
    pass


def _importable(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


class Voice:
    def __init__(self, config) -> None:
        self.config = config
        self._stt: Any = None
        self._stt_fast: Any = None  # легка модель для перевірки імені (whisper_wake_model)
        self._tts: Any = None
        self._stt_lock = asyncio.Lock()
        self._tts_lock = asyncio.Lock()
        self._holos_voices: list[str] = []
        self._holos_checked = 0.0

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
    def piper_ok(self) -> bool:
        return _importable("piper") and self.voice_path.is_file()

    @property
    def engine(self) -> str:
        """Чим озвучувати: holos (сервіс HolosTTS), piper або "" — нічим."""
        if not self.enabled:
            return ""
        if self.config.tts_url:
            return "holos"
        return "piper" if self.piper_ok else ""

    @property
    def tts_ok(self) -> bool:
        return bool(self.engine)

    def voices(self) -> list[str]:
        if self.engine == "holos":
            return list(self._holos_voices)
        return PIPER_SPEAKERS[:] if self.engine == "piper" and DEFAULT_VOICE in self.voice_path.name else []

    async def refresh_voices(self, force: bool = False) -> None:
        """Список голосів HolosTTS (кешується на кілька хвилин; сервіс міг ще не запуститися — тоді пізніше)."""
        if self.engine != "holos" or (not force and time.monotonic() - self._holos_checked < VOICES_TTL):
            return
        self._holos_checked = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{self.config.tts_url}/v1/audio/voices")
                r.raise_for_status()
                voices = r.json().get("voices")
            if isinstance(voices, list):
                self._holos_voices = [str(v) for v in voices if isinstance(v, str)]
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.info("HolosTTS ще не відповідає (%s) — голоси спитаю пізніше", e)
            self._holos_checked = time.monotonic() - VOICES_TTL + 15

    def caps(self) -> dict:
        from .voice_fx import EFFECTS

        from . import wakeword

        ww = wakeword.info(self.config)
        return {"stt": self.stt_ok, "tts": self.tts_ok, "engine": self.engine,
                "wakeword": {"phrase": ww.get("phrase"), "version": ww.get("version")} if ww else None,
                "effects": {k: e.title for k, e in EFFECTS.items()}, "voices": self.voices(),
                "speakers": self.voices() if self.engine == "piper" else []}  # speakers — для старих клієнтів

    # ---------------- розпізнавання ----------------
    @property
    def two_pass(self) -> bool:
        """Ім'я перевіряє окрема легка модель, а задачу — основна."""
        wake = self.config.whisper_wake_model.strip()
        return bool(wake) and wake != self.config.whisper_model

    def _load_stt(self, fast: bool = False):
        fast = fast and self.two_pass
        if (self._stt_fast if fast else self._stt) is None:
            from faster_whisper import WhisperModel

            name = self.config.whisper_wake_model.strip() if fast else self.config.whisper_model
            log.info("Завантажую модель розпізнавання мовлення %s…", name)
            model = WhisperModel(name, device=self.config.whisper_device, compute_type="int8")
            if fast:
                self._stt_fast = model
            else:
                self._stt = model
        return self._stt_fast if fast else self._stt

    def _transcribe(self, data: bytes, prompt: str | None = None, fast: bool = False, strict: bool = False) -> str:
        model = self._load_stt(fast)
        segments, _info = model.transcribe(
            decode_audio(data), language=self.config.whisper_language or None, vad_filter=True,
            beam_size=1 if fast else 5, initial_prompt=prompt,
        )
        if strict:  # відкинути шматки, де модель сама не певна, що це мовлення
            segments = [s for s in segments if getattr(s, "no_speech_prob", 0.0) < 0.6
                        and getattr(s, "avg_logprob", 0.0) > -1.0]
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, data: bytes, prompt: str | None = None, fast: bool = False,
                         strict: bool = False) -> str:
        """Аудіо (WAV, OGG/Opus з Telegram, MP3…) → текст. fast — легкою моделлю (перевірка імені)."""
        if not self.stt_ok:
            raise VoiceError("Розпізнавання мовлення не встановлено на сервері: deploy/voice.sh install")
        if len(data) > MAX_AUDIO:
            raise VoiceError("Запис задовгий")
        async with self._stt_lock:
            try:
                return await asyncio.to_thread(self._transcribe, data, prompt, fast, strict)
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

    def options(self, voice=None, effect=None, speed=None, speaker=None) -> tuple[str, str, float]:
        """Голос (назва), обробка й темп: з запиту клієнта, інакше — з налаштувань сервера."""
        from .voice_fx import EFFECTS

        name = voice.strip() if isinstance(voice, str) else ""
        if not name and isinstance(speaker, int) and not isinstance(speaker, bool) and 0 <= speaker < len(PIPER_SPEAKERS):
            name = PIPER_SPEAKERS[speaker]  # старі клієнти надсилали номер диктора Piper
        if not name:
            name = self.config.tts_voice if self.engine == "holos" else (
                PIPER_SPEAKERS[self.config.piper_speaker] if 0 <= self.config.piper_speaker < len(PIPER_SPEAKERS)
                else "")
        fx = effect if effect in EFFECTS else (self.config.voice_effect if self.config.voice_effect in EFFECTS
                                               else "natural")
        try:
            sd = min(1.6, max(0.6, float(speed))) if speed is not None else 1.0
        except (TypeError, ValueError):
            sd = 1.0
        return name, fx, sd

    def _effect(self, audio: bytes, effect: str) -> bytes:
        if effect == "natural":
            return audio
        from .voice_fx import EFFECTS, apply

        try:
            return apply(audio, EFFECTS[effect])
        except ImportError:  # без numpy (Piper зазвичай тягне його сам) — звичайний голос, а не помилка
            log.warning("numpy не встановлено — обробка «%s» недоступна, голос без неї", effect)
            return audio

    async def _speak_holos(self, text: str, voice: str, effect: str, speed: float) -> bytes:
        """HolosTTS через його OpenAI-сумісний API. Числа, наголоси й довгий текст сервіс обробляє сам."""
        from .voice_fx import EFFECTS

        pitch = EFFECTS[effect].pitch
        body = {"input": text, "response_format": "wav",
                # вища висота: синтезуємо повільніше, а voice_fx стискає запис назад до звичайного темпу
                "speed": round(min(2.0, max(0.5, speed / pitch)), 3)}
        if voice:
            body["voice"] = voice
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
                r = await client.post(f"{self.config.tts_url}/v1/audio/speech", json=body)
        except httpx.ConnectError as e:
            raise VoiceError("Сервіс голосу HolosTTS не відповідає — перевір: deploy/holos.sh status") from e
        except httpx.TimeoutException as e:
            raise VoiceError("HolosTTS не встиг озвучити (тайм-аут)") from e
        if r.status_code != 200:
            try:
                detail = r.json().get("detail") or r.text
            except ValueError:
                detail = r.text
            raise VoiceError(f"HolosTTS: {str(detail)[:200]}")
        return await asyncio.to_thread(self._effect, r.content, effect)

    def _speak_piper(self, text: str, voice: str, effect: str, speed: float) -> bytes:
        from .voice_fx import EFFECTS

        fx = EFFECTS[effect]
        speaker = PIPER_SPEAKERS.index(voice) if voice in PIPER_SPEAKERS else self.config.piper_speaker
        model = self._load_tts()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            try:
                from piper import SynthesisConfig

                speakers = int(getattr(model.config, "num_speakers", 1) or 1)
                # вища висота = синтезуємо повільніше на той самий коефіцієнт, а voice_fx «прискорює» назад
                model.synthesize_wav(text, wav, syn_config=SynthesisConfig(
                    speaker_id=speaker if 0 <= speaker < speakers else None, length_scale=fx.pitch / speed))
            except ImportError:  # старіший piper-tts (1.2)
                model.synthesize(text, wav, speaker_id=speaker or None, length_scale=fx.pitch / speed)
        return self._effect(buf.getvalue(), effect)

    async def speak(self, text: str, voice=None, effect=None, speed=None, speaker=None) -> bytes:
        """Текст відповіді → WAV (голос voice — назва, обробка effect — див. voice_fx.EFFECTS, темп speed)."""
        engine = self.engine
        if not engine:
            raise VoiceError("Озвучення не налаштовано на сервері: deploy/holos.sh start (або deploy/voice.sh install)")
        speech = speech_text(text)
        if not speech:
            raise VoiceError("Нема чого озвучувати")
        return await self._synth(speech, self.options(voice, effect, speed, speaker))

    async def speak_parts(self, text: str, voice=None, effect=None, speed=None, speaker=None):
        """Те саме, що speak, але частинами: (WAV, остання?). Перше речення звучить, поки синтезується решта, —
        довга відповідь не змушує чекати, доки озвучиться вся."""
        if not self.engine:
            raise VoiceError("Озвучення не налаштовано на сервері: deploy/holos.sh start (або deploy/voice.sh install)")
        chunks = speech_chunks(speech_text(text))
        if not chunks:
            raise VoiceError("Нема чого озвучувати")
        opts = self.options(voice, effect, speed, speaker)
        for i, chunk in enumerate(chunks):
            yield await self._synth(chunk, opts), i == len(chunks) - 1

    async def _synth(self, speech: str, opts: tuple[str, str, float]) -> bytes:
        engine = self.engine
        async with self._tts_lock:
            try:
                if engine == "holos":
                    return await self._speak_holos(speech, *opts)
                return await asyncio.to_thread(self._speak_piper, speech, *opts)
            except VoiceError:
                raise
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


# Як Whisper записує ім'я «Кру» (і як люди звертаються перед ним)
WAKE_PROMPT = "Розмова з голосовим помічником Кру."
_WAKE = re.compile(r"^(кру|крю|кроу|кру-кру|kru|krew|crew|krу)$")
# звертання перед ім'ям; Whisper на коротких фразах часом пише їх по-російськи («Привет, Кру!»)
_BEFORE_WAKE = {"ну", "агов", "гей", "ей", "ой", "о", "слухай", "послухай", "привіт", "привітик", "вітаю", "алло", "а",
                "хей", "хай", "здоров", "привет", "эй", "хэй", "слушай", "послушай", "алё",
                "чуєш", "чуеш", "чуешь", "слышишь", "чуєте"}


# що Whisper «чує» в тиші й шумі (навчений на субтитрах відео)
_NOISE_TEXT = re.compile(
    r"дякую за (перегляд|увагу)|підписуйтес|субтитр|продовження (буде|слідує)|ставте лайк|"
    r"спасибо за просмотр|подписывайтесь|редактор субтитров|продолжение следует|thanks for watching|"
    r"^(дякую|спасибо|угу|ага|ммм*|е+|а+|о+|хм+|ну)[.!?…]*$", re.IGNORECASE)


def is_noise_text(text: str) -> bool:
    """Порожньо, без літер чи типова «вигадка» Whisper на шумі — не уточнення, а шум."""
    t = text.strip()
    return len(re.sub(r"[^\w]", "", t)) < 3 or bool(_NOISE_TEXT.search(t))


def strip_detected(text: str) -> str:
    """Фразу-активатор уже впізнала модель на ПК: прибрати звертання й ім'я з початку, навіть якщо Whisper
    записав ім'я інакше («Чуєш, Крю, …», «Чуєш, кру…»)."""
    found, rest = strip_wake(text)
    if found:
        return rest
    words = text.strip().split()
    i = 0
    while i < min(len(words), 3) and re.sub(r"[^\w'-]", "", words[i].lower()) in _BEFORE_WAKE:
        i += 1
    if i and i < len(words) and re.sub(r"[^\w'-]", "", words[i].lower()).startswith(("кр", "kr", "cr")) and \
            len(re.sub(r"[^\w]", "", words[i])) <= 5:
        i += 1  # ім'я, почуте трохи інакше
    rest = " ".join(words[i:]).lstrip(" ,.!?:;—-").strip()
    return rest[:1].upper() + rest[1:]


def strip_wake(text: str) -> tuple[bool, str]:
    """«Кру, яка сьогодні погода?» → (True, "Яка сьогодні погода?"). Ім'я має бути серед перших слів
    (перед ним можна «агов», «привіт»…); інакше (False, text) — режим «завжди слухаю» спрацював хибно."""
    words = text.strip().split()
    for i, raw in enumerate(words[:4]):
        w = re.sub(r"[^\w'-]", "", raw.lower()).strip("-")
        if _WAKE.match(w):
            rest = " ".join(words[i + 1:]).lstrip(" ,.!?:;—-").strip()
            return True, rest[:1].upper() + rest[1:]
        if w not in _BEFORE_WAKE:
            break
    return False, text


_CODE_BLOCK = re.compile(r"```.*?(```|$)", re.DOTALL)
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")


def speech_chunks(speech: str, first: int = 140, rest: int = 450) -> list[str]:
    """Текст для озвучення → частини по реченнях: перша коротка (швидше почне звучати), далі — довші."""
    chunks: list[str] = []
    cur = ""
    for sentence in re.split(r"(?<=[.!?…;:])\s+", speech.strip()):
        if not sentence:
            continue
        # перша частина — щойно набралось хоч трохи (≥ 20 знаків), щоб звук почався якнайшвидше
        if cur and (len(cur) + 1 + len(sentence) > (rest if chunks else first) or (not chunks and len(cur) >= 20)):
            chunks.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}" if cur else sentence
    if cur:
        chunks.append(cur)
    return chunks


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
