"""Голос: запис → текст (faster-whisper), відповідь → WAV (Piper); WS-протокол і голосові в Telegram.

Справжні моделі тут не завантажуються — їх замінюють заглушки з тим самим API."""

from __future__ import annotations

import base64
import json
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
        self.length_scales: list[float] = []

    def synthesize_wav(self, text, wav, syn_config=None):
        self.said.append((text, syn_config.speaker_id))
        self.length_scales.append(syn_config.length_scale)
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\0\0" * 100)


@pytest.fixture
def voice_ready(monkeypatch):
    piper = types.ModuleType("piper")
    piper.SynthesisConfig = lambda **kw: types.SimpleNamespace(**{"speaker_id": None, "length_scale": None, **kw})
    monkeypatch.setitem(sys.modules, "piper", piper)
    monkeypatch.setattr(voice_mod, "decode_audio", lambda data: data)  # декодер перевіряє окремий тест
    monkeypatch.setattr(Voice, "stt_ok", property(lambda self: self.enabled))
    monkeypatch.setattr(Voice, "piper_ok", property(lambda self: True))
    whisper, tts = FakeWhisper(), FakePiper()

    def attach(agent: Agent):
        agent.voice._stt, agent.voice._stt_fast, agent.voice._tts = whisper, whisper, tts
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
    caps = Voice(cfg).caps()
    assert (caps["stt"], caps["tts"]) == (False, False) and caps["effects"]["robot"] == "М'який робот"
    cfg.voice = "auto"
    v = Voice(cfg)
    assert v.voice_path == cfg.data_dir / "voice" / "uk_UA-ukrainian_tts-medium.onnx"
    assert v.tts_ok is False  # голосу на диску немає


async def test_transcribe_and_speak(cfg, voice_ready):
    cfg.piper_speaker = 2
    cfg.voice_effect = "natural"  # без обробки — тут перевіряємо сам синтез
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
        assert hello["type"] == "hello" and (hello["voice"]["stt"], hello["voice"]["tts"]) == (True, True)
        assert "droid" in hello["voice"]["effects"]
        ws.send_json({"type": "voice_in", "id": "v1", "data": base64.b64encode(b"RIFF").decode()})
        heard, _ = _until(ws, "voice_text")
        assert heard == {"type": "voice_text", "id": "v1", "text": "яка погода в Києві"}
        kw = agent.voice._stt.calls[-1]  # швидкі налаштування CPU: один варіант пошуку, без часових міток
        assert kw["beam_size"] == 1 and kw["without_timestamps"] and not kw["condition_on_previous_text"]
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
        caps = ws.receive_json()["voice"]
        assert (caps["stt"], caps["tts"]) == (False, False)
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


def test_voice_options_and_effects(cfg, voice_ready):
    """Звучання: голос/ефект/темп із запиту клієнта; ефект зберігає тривалість і не перевантажує звук."""
    np = pytest.importorskip("numpy")
    from kru_agent.voice_fx import EFFECTS, apply

    v = Voice(cfg)
    assert v.options() == ("Лада", "natural", 1.0)  # типово — без обробки
    assert v.options("Тетяна", "droid", 1.2) == ("Тетяна", "droid", 1.2)
    assert v.options(None, "невідомий", "швидко") == ("Лада", "natural", 1.0)  # сміття з клієнта — до типових
    assert v.options(None, None, 9, speaker=1) == ("Микита", "natural", 1.6)  # старі клієнти: номер диктора

    rate = 22050
    t = np.arange(rate) / rate  # 1 с «голосу»: 220 Гц з обертонами
    sig = (0.6 * np.sin(2 * np.pi * 220 * t) + 0.3 * np.sin(2 * np.pi * 440 * t)) * np.hanning(rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((sig * 32767).astype("<i2").tobytes())
    for name, fx in EFFECTS.items():
        out = apply(buf.getvalue(), fx)
        with wave.open(io.BytesIO(out)) as w:
            y = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
            assert w.getframerate() == rate
        assert abs(len(y) - rate / fx.pitch) <= 1, name  # pitch: Piper синтезує довше рівно на стільки ж
        assert 0.8 < np.max(np.abs(y)) <= 0.9 and np.all(np.isfinite(y)), name
        assert abs(y[0]) < 1e-3 and abs(y[-1]) < 1e-3, name  # без клацань на краях


async def test_speak_uses_requested_voice(cfg, voice_ready):
    agent = Agent(cfg, llm=FakeLLM([]))
    _, tts = voice_ready(agent)
    await agent.voice.speak("Привіт", voice="Микита", effect="droid", speed=1.25)
    assert tts.said[-1] == ("Привіт.", 1)
    assert tts.length_scales[-1] == pytest.approx(1.14 / 1.25)  # вища висота й швидший темп


def _holos(monkeypatch, handler):
    import httpx

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def _wav(frames: int = 2400, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * frames)
    return buf.getvalue()


async def test_holos_tts(cfg, monkeypatch):
    """HolosTTS як окремий сервіс: голоси за назвами, WAV через OpenAI-сумісний API, зрозумілі помилки."""
    import httpx

    cfg.tts_url = "http://127.0.0.1:8890"
    cfg.tts_voice = "Speaker_67"
    requests = []

    state = {"down": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            raise httpx.ConnectError("refused")
        if request.url.path == "/v1/audio/voices":
            return httpx.Response(200, json={"voices": ["Speaker_67", "Гаська Шиян", "Speaker_0"]})
        body = json.loads(request.content)
        requests.append(body)
        if body["input"].startswith("Зламай"):
            return httpx.Response(400, json={"detail": "input is empty"})
        return httpx.Response(200, content=_wav(), headers={"content-type": "audio/wav"})

    _holos(monkeypatch, handler)
    v = Voice(cfg)
    assert v.engine == "holos" and v.tts_ok  # Piper не потрібен
    await v.refresh_voices(force=True)
    caps = v.caps()
    assert caps["engine"] == "holos" and caps["voices"] == ["Speaker_67", "Гаська Шиян", "Speaker_0"]

    audio = await v.speak("**Привіт**, о 7:45 зустріч! ```код```", voice="Гаська Шиян", speed=1.2)
    assert audio.startswith(b"RIFF")
    assert requests[-1] == {"input": "Привіт, о 7:45 зустріч! (код показано в чаті).", "response_format": "wav",
                            "speed": 1.2, "voice": "Гаська Шиян"}  # числа сервіс озвучує сам
    await v.speak("Як справи?")
    assert requests[-1]["voice"] == "Speaker_67" and requests[-1]["speed"] == 1.0  # голос сервера за замовчуванням
    try:
        import numpy  # noqa: F401 — обробка голосу без numpy вимикається
        has_numpy = True
    except ImportError:
        has_numpy = False
    if has_numpy:
        out = await v.speak("Я робот.", effect="droid")
        assert requests[-1]["speed"] == pytest.approx(1 / 1.14, abs=1e-3)  # повільніше — й стиснути назад
        with wave.open(io.BytesIO(out)) as w:
            assert abs(w.getnframes() - 2400 / 1.14) <= 1
    with pytest.raises(VoiceError, match="HolosTTS: input is empty"):
        await v.speak("Зламай")

    state["down"] = True
    with pytest.raises(VoiceError, match="deploy/holos.sh status"):
        await v.speak("Привіт")
    await v.refresh_voices(force=True)  # сервіс ще не запустився — список лишається, без помилки
    assert v.voices() == ["Speaker_67", "Гаська Шиян", "Speaker_0"]


def test_strip_wake():
    from kru_agent.voice import strip_wake

    assert strip_wake("Кру, яка сьогодні погода?") == (True, "Яка сьогодні погода?")
    assert strip_wake("Агов, Кру! увімкни світло.") == (True, "Увімкни світло.")
    assert strip_wake("Ну, крю, допоможи") == (True, "Допоможи")
    assert strip_wake("Кру.") == (True, "")
    assert strip_wake("Привет, Кру!") == (True, "")  # Whisper на коротких фразах пише й по-російськи
    for miss in ("Це круто, правда?", "Кружка на столі", "Скажи, кру, котра година", ""):
        assert strip_wake(miss)[0] is False, miss


def test_ws_wake_mode(cfg, voice_ready):
    """«Завжди слухаю»: сервер перевіряє, що фраза починається з імені, і прибирає його."""
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM([]))
    whisper, _ = voice_ready(agent)
    client = TestClient(create_app(cfg, agent=agent))
    data = base64.b64encode(b"RIFF").decode()
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        cases = [("Кру, яка сьогодні погода?", {"text": "Яка сьогодні погода?"}),
                 ("Це було круто", {"text": "", "ignored": True, "heard": "Це було круто"}),
                 ("Кру", {"text": "", "wake_only": True})]
        for i, (heard, expected) in enumerate(cases):
            whisper.words = heard
            ws.send_json({"type": "voice_in", "id": f"w{i}", "data": data, "wake": "require"})
            got, _ = _until(ws, "voice_text")
            assert {k: v for k, v in got.items() if k not in {"type", "id"}} == expected, heard
        assert "Кру" in whisper.calls[-1]["initial_prompt"]  # Whisper знає, як пишеться ім'я
        whisper.words = "Кру, без режиму"  # звичайний запис (утримання робота) — текст як є
        ws.send_json({"type": "voice_in", "id": "plain", "data": data})
        assert _until(ws, "voice_text")[0]["text"] == "Кру, без режиму"
        assert whisper.calls[-1]["initial_prompt"] is None


def test_ws_wake_two_models(cfg, voice_ready):
    """Ім'я перевіряє легка модель (швидко, на кожну фразу), а задачу після імені — точна."""
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM([]))
    full, _ = voice_ready(agent)
    fast = FakeWhisper()
    agent.voice._stt_fast = fast
    assert agent.voice.two_pass  # типово small для імені, turbo для задачі
    client = TestClient(create_app(cfg, agent=agent))
    data = base64.b64encode(b"RIFF").decode()
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        fast.words, full.words = "Ми говорили про кіно", "не має викликатись"
        ws.send_json({"type": "voice_in", "id": "a", "data": data, "wake": "require"})
        assert _until(ws, "voice_text")[0].get("ignored") is True
        assert full.calls == [] and fast.calls[-1]["beam_size"] == 1  # без імені точна модель не працює
        fast.words, full.words = "Кру, яка погода в кеві", "Кру, яка погода в Києві?"
        ws.send_json({"type": "voice_in", "id": "b", "data": data, "wake": "require"})
        assert _until(ws, "voice_text")[0]["text"] == "Яка погода в Києві?"
        fast.words, full.words = "Привет, Кру!", "Привіт."
        ws.send_json({"type": "voice_in", "id": "c", "data": data, "wake": "require"})
        assert _until(ws, "voice_text")[0].get("wake_only") is True
        assert len(full.calls) == 1  # лише ім'я — друге розпізнавання не потрібне


def test_speech_chunks():
    from kru_agent.voice import speech_chunks

    text = "Привіт! Сьогодні сонячно. " + "Це довге речення про погоду на завтра і післязавтра. " * 12
    parts = speech_chunks(text.strip())
    assert parts[0] == "Привіт! Сьогодні сонячно."  # перша частина коротка — звучить майже одразу
    assert len(parts) > 2 and all(len(p) <= 450 for p in parts[1:])
    assert " ".join(parts) == " ".join(text.split())  # нічого не загубилось
    assert speech_chunks("") == [] and speech_chunks("Одне.") == ["Одне."]


def test_ws_tts_stream(cfg, voice_ready):
    """stream=true: відповідь озвучується частинами; tts_stop зупиняє решту."""
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM([]))
    _, tts = voice_ready(agent)
    client = TestClient(create_app(cfg, agent=agent))
    long = "Перше речення відповіді. " + "Далі йде доволі довге речення, щоб частин було кілька. " * 20
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "tts", "id": "s1", "text": long, "stream": True})
        got = []
        while not got or not got[-1]["last"]:
            got.append(_until(ws, "tts_audio")[0])
        assert [g["part"] for g in got] == list(range(len(got))) and len(got) >= 3
        assert all(g["id"] == "s1" and base64.b64decode(g["data"]).startswith(b"RIFF") for g in got)
        assert tts.said[-len(got)][0] == "Перше речення відповіді."
        ws.send_json({"type": "tts", "id": "s2", "text": "Коротко.", "stream": True})
        one, _ = _until(ws, "tts_audio")
        assert (one["id"], one["part"], one["last"]) == ("s2", 0, True)


def test_followup_noise(cfg, voice_ready):
    """Уточнення без імені: типові «вигадки» Whisper на шумі відкидаються, справжнє питання — ні."""
    from kru_agent.voice import is_noise_text

    assert is_noise_text("Дякую за перегляд!") and is_noise_text("Субтитри зробив DimaTorzok")
    assert is_noise_text("Угу.") and is_noise_text("...") and is_noise_text("")
    assert not is_noise_text("А завтра?") and not is_noise_text("Так") and not is_noise_text("Дякую, а що з погодою?")
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM([]))
    whisper, _ = voice_ready(agent)
    client = TestClient(create_app(cfg, agent=agent))
    data = base64.b64encode(b"RIFF").decode()
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        for heard, expected in (("Дякую за перегляд!", ""), ("А завтра?", "А завтра?")):
            whisper.words = heard
            ws.send_json({"type": "voice_in", "id": heard, "data": data, "wake": "followup"})
            got, _ = _until(ws, "voice_text")
            assert got["text"] == expected and bool(got.get("ignored")) == (not expected), got


def test_preload_loads_models_with_threads(cfg, monkeypatch):
    """При старті сервера моделі завантажуються заздалегідь, з потоками CPU (типово — ~фізичні ядра)."""
    made = []
    fw = types.ModuleType("faster_whisper")
    fw.WhisperModel = lambda name, **kw: made.append((name, kw)) or object()
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    monkeypatch.setattr(Voice, "stt_ok", property(lambda self: True))
    monkeypatch.setattr(voice_mod.os, "cpu_count", lambda: 16)
    v = Voice(cfg)
    v.preload()
    assert [(n, kw["cpu_threads"], kw["compute_type"]) for n, kw in made] == [("turbo", 8, "int8"), ("small", 8, "int8")]
    v.preload()  # уже завантажено — вдруге не вантажить
    assert len(made) == 2
    cfg.whisper_threads = 3
    v2 = Voice(cfg)
    v2.preload()
    assert made[-1][1]["cpu_threads"] == 3
