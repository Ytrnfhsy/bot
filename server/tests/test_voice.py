"""Голос: запис → текст (faster-whisper), відповідь → WAV (Piper); WS-протокол і голосові в Telegram.

Справжні моделі тут не завантажуються — їх замінюють заглушки з тим самим API."""

from __future__ import annotations

import base64
import io
import sys
import types
import wave

import pytest
from fastapi.testclient import TestClient

from kru_agent.agent import Agent
from kru_agent.server import create_app
from kru_agent import voice as voice_mod
from kru_agent.voice import Voice, VoiceError, decode_audio, speech_text

from .conftest import FakeLLM, call, text
from .test_server import _until
from .test_telegram_bot import make, msg


class FakeWhisper:
    def __init__(self, words: str = "яка погода в Києві") -> None:
        self.words = words
        self.calls: list[dict] = []

    def transcribe(self, audio, **kw):
        self.calls.append({"bytes": audio, **kw})
        seg = types.SimpleNamespace(text=f" {self.words} ")
        return iter([seg] if self.words else []), None


class FakePiper:
    config = types.SimpleNamespace(num_speakers=3)

    def __init__(self) -> None:
        self.said: list[tuple[str, int | None]] = []

    def synthesize_wav(self, text, wav, syn_config=None):
        self.said.append((text, syn_config.speaker_id))
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\0\0" * 100)


@pytest.fixture
def voice_ready(monkeypatch):
    piper = types.ModuleType("piper")
    piper.SynthesisConfig = lambda speaker_id=None: types.SimpleNamespace(speaker_id=speaker_id)
    monkeypatch.setitem(sys.modules, "piper", piper)
    monkeypatch.setattr(voice_mod, "decode_audio", lambda data: data)  # декодер перевіряє окремий тест
    monkeypatch.setattr(Voice, "stt_ok", property(lambda self: self.enabled))
    monkeypatch.setattr(Voice, "tts_ok", property(lambda self: self.enabled))
    whisper, tts = FakeWhisper(), FakePiper()

    def attach(agent: Agent):
        agent.voice._stt, agent.voice._tts = whisper, tts
        return whisper, tts

    return attach


def test_speech_text_reads_like_a_human():
    md = ("## Огляд\n\n| Рішення | Мови |\n|---|---|\n| **Whisper** | 200 |\n\n"
          "- Модель [Hy-MT2](https://x.org) (Hunyuan) 🚀\n\nКод: `pip install x` і сайт https://a.b\n\n"
          "```bash\nrm -rf /\n```\nГотово!")
    out = speech_text(md)
    assert out == ("Огляд. Рішення, Мови. Whisper, 200. Модель Hy-MT2 (Hunyuan). Код: pip install x і сайт. "
                   "(код показано в чаті). Готово!")
    long = speech_text("Речення номер один. " * 200)
    assert long.endswith("Решта — у чаті.") and len(long) < 1600
    assert speech_text("```\nлише код\n```").startswith("(код")
    assert speech_text("🚀") == ""


def test_caps_without_libraries(cfg):
    cfg.voice = "off"
    assert Voice(cfg).caps() == {"stt": False, "tts": False}
    cfg.voice = "auto"
    v = Voice(cfg)
    assert v.voice_path == cfg.data_dir / "voice" / "uk_UA-ukrainian_tts-medium.onnx"
    assert v.tts_ok is False  # голосу на диску немає


async def test_transcribe_and_speak(cfg, voice_ready):
    cfg.piper_speaker = 2
    agent = Agent(cfg, llm=FakeLLM([]))
    whisper, tts = voice_ready(agent)
    assert await agent.voice.transcribe(b"RIFF....") == "яка погода в Києві"
    assert whisper.calls[0]["language"] == "uk" and whisper.calls[0]["vad_filter"] is True
    audio = await agent.voice.speak("**Сонячно**, +18 °C ☀️")
    with wave.open(io.BytesIO(audio)) as w:
        assert w.getframerate() == 22050 and w.getnframes() == 100
    assert tts.said == [("Сонячно, +18 °C.", 2)]
    with pytest.raises(VoiceError, match="Нема чого"):
        await agent.voice.speak("🚀\n\n---")
    with pytest.raises(VoiceError, match="задовгий"):
        await agent.voice.transcribe(b"0" * (11 * 1024 * 1024))


async def test_voice_disabled_errors(cfg):
    cfg.voice = "off"
    agent = Agent(cfg, llm=FakeLLM([]))
    with pytest.raises(VoiceError, match="voice.sh install"):
        await agent.voice.transcribe(b"x")
    with pytest.raises(VoiceError, match="voice.sh install"):
        await agent.voice.speak("привіт")


def test_ws_voice_roundtrip(cfg, voice_ready):
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM([text("У Києві сонячно, **+18**.")]))
    voice_ready(agent)
    client = TestClient(create_app(cfg, agent=agent))
    with client, client.websocket_connect("/ws?token=secret") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["voice"] == {"stt": True, "tts": True}
        ws.send_json({"type": "voice_in", "id": "v1", "data": base64.b64encode(b"RIFF").decode()})
        heard, _ = _until(ws, "voice_text")
        assert heard == {"type": "voice_text", "id": "v1", "text": "яка погода в Києві"}
        ws.send_json({"type": "task", "text": heard["text"], "session_id": "v"})
        final, _ = _until(ws, "final")
        ws.send_json({"type": "tts", "id": "t1", "text": final["text"]})
        audio, _ = _until(ws, "tts_audio")
        assert audio["id"] == "t1" and audio["format"] == "wav"
        assert base64.b64decode(audio["data"]).startswith(b"RIFF")
        ws.send_json({"type": "voice_in", "id": "v2", "data": "не base64!"})
        assert _until(ws, "voice_error")[0]["message"] == "пошкоджений запис"


def test_ws_voice_not_installed(cfg):
    cfg.token = "secret"
    cfg.voice = "off"
    client = TestClient(create_app(cfg, agent=Agent(cfg, llm=FakeLLM([]))))
    with client, client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["voice"] == {"stt": False, "tts": False}
        ws.send_json({"type": "tts", "id": "t", "text": "привіт"})
        assert "voice.sh install" in _until(ws, "tts_error")[0]["message"]


async def test_telegram_voice_message(cfg, voice_ready):
    gw, agent, bot = make(cfg, [call("remember", content="x"), text("Сонячно")])
    whisper, _ = voice_ready(agent)
    bot.files["vf"] = (b"OggS-opus", "voice/file_1.oga")
    await gw.handle(msg(voice={"file_id": "vf", "duration": 2}))
    assert whisper.calls[0]["bytes"] == b"OggS-opus"
    assert bot.texts()[0] == "🎤 «яка погода в Києві»" and bot.texts()[-1] == "Сонячно"
    assert agent.llm.calls[0]["messages"][-1]["content"].endswith("яка погода в Києві")
    whisper.words = ""
    await gw.handle(msg(voice={"file_id": "vf"}))
    assert "Не почув" in bot.texts()[-1] and len(agent.llm.calls) == 2


async def test_telegram_voice_not_installed(cfg):
    cfg.voice = "off"
    gw, agent, bot = make(cfg, [])
    await gw.handle(msg(voice={"file_id": "vf"}))
    assert "ще не підключено" in bot.texts()[0] and agent.llm.calls == []


def test_decode_wav_without_pyav():
    """WAV з персонажа декодуємо самі: faster_whisper.decode_audio падає з PyAV 15+ (metadata_errors)."""
    np = pytest.importorskip("numpy")

    def wav(rate, channels, frames):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(np.asarray(frames, dtype="<i2").tobytes())
        return buf.getvalue()

    mono = decode_audio(wav(16000, 1, [0, 16384, -16384, 32767]))
    assert mono.dtype == np.float32 and np.allclose(mono, [0, 0.5, -0.5, 32767 / 32768])
    stereo = decode_audio(wav(16000, 2, [16384, 0, -16384, -16384]))  # канали усереднюються
    assert np.allclose(stereo, [0.25, -0.5])
    resampled = decode_audio(wav(48000, 1, [8192] * 4800))  # 0,1 с з 48 кГц → 1600 відліків 16 кГц
    assert len(resampled) == 1600 and np.allclose(resampled, 0.25)
