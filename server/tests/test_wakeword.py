"""Своя фраза-активатор (openWakeWord): навчання на сервері, видача моделі роботу, wake=detected.

Справжні мережі відбитків і HolosTTS тут не потрібні — їх замінюють заглушки з тим самим API."""

from __future__ import annotations

import base64
import io
import wave

import numpy as np
from fastapi.testclient import TestClient

from kru_agent import voice as voice_mod
from kru_agent import wakeword
from kru_agent.agent import Agent
from kru_agent.server import create_app
from kru_agent.voice import strip_detected

from .conftest import FakeLLM
from .test_server import _until
from .test_voice import voice_ready  # noqa: F401 — фікстура


def _wav(a, rate=24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(a, -1, 1) * 20000).astype("<i2").tobytes())
    return buf.getvalue()


class BandFeatures:
    """Заглушка мереж відбитків: енергія 96 смуг спектра в 16 відрізках кліпу."""

    def __init__(self, folder=None) -> None:
        pass

    def clips(self, audio):
        out = []
        for clip in np.asarray(audio, dtype=np.float32) / 32768:
            segs = clip.reshape(FRAMES, -1)
            spec = np.abs(np.fft.rfft(segs, axis=1))[:, 1:1 + 96 * 8:8]
            out.append(np.log1p(spec * 10))
        return np.asarray(out, dtype=np.float32)


FRAMES = wakeword.FRAMES


def test_mlp_learns_and_threshold():
    rng = np.random.default_rng(0)
    pos = rng.normal(1.0, 1.0, (300, FRAMES, 96)).astype(np.float32)
    neg = rng.normal(-1.0, 1.0, (900, FRAMES, 96)).astype(np.float32)
    params = wakeword.train_mlp(pos, neg, rng, epochs=5)
    sp, sn = wakeword.predict(params, pos), wakeword.predict(params, neg)
    thr, recall, fa = wakeword.pick_threshold(sp, sn)
    assert 0.5 <= thr < 1 and recall > 0.95 and fa <= 0.001


def test_texts_and_augment():
    assert "Чуєш, Кру!" in wakeword.phrase_variants("Чуєш, Кру") and "Чуєш Кру." in wakeword.phrase_variants("Чуєш, Кру")
    near = wakeword.near_misses("Чуєш, Кру")
    assert "Чуєш." in near and "Кру." in near and "Чуєш, друже." in near and "Привіт, Кру." in near
    rng = np.random.default_rng(1)
    clip = np.sin(np.arange(8000) / 5).astype(np.float32)
    out = wakeword.augment(clip, rng, True)
    assert out.dtype == np.int16 and len(out) == wakeword.CLIP
    assert np.abs(out[: wakeword.CLIP // 3]).max() < np.abs(out[-wakeword.CLIP // 2:]).max()  # фраза — у кінці


def test_strip_detected():
    assert strip_detected("Чуєш, Кру, яка погода?") == "Яка погода?"
    assert strip_detected("Чуєш, Крю, яка погода?") == "Яка погода?"
    assert strip_detected("Чуеш крув, увімкни світло") == "Увімкни світло"
    assert strip_detected("Чуєш, Кру.") == ""
    assert strip_detected("Яка погода?") == "Яка погода?"  # ім'я не почулося — лишаємо як є


def test_train_end_to_end(cfg, monkeypatch):
    """Повний цикл: «озвучення» → «псування» → відбитки → класифікатор → файли моделі."""
    cfg.tts_url = "http://holos"

    async def speak(self, text, voice=None, effect=None, speed=None, speaker=None):
        rng = np.random.default_rng(abs(hash((text, voice, speed))) % 2 ** 32)
        t = np.arange(int(24000 * 0.8 / (speed or 1))) / 24000
        if "Чуєш" in text and "Кру" in text and "," in text or text.startswith("Чуєш Кру"):
            a = np.sin(2 * np.pi * (300 + 600 * t) * t)  # «фраза» — висхідний свист
        else:
            a = np.sin(2 * np.pi * rng.uniform(150, 900) * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 5 * t))
        return _wav(a * np.minimum(1, np.sin(np.pi * t / t[-1]) * 4))

    async def refresh(self, force=False):
        pass

    monkeypatch.setattr(voice_mod.Voice, "speak", speak)
    monkeypatch.setattr(voice_mod.Voice, "refresh_voices", refresh)
    monkeypatch.setattr(voice_mod.Voice, "voices", lambda self: ["Голос1", "Голос2", "Голос3"])
    monkeypatch.setattr(wakeword, "ensure_features", lambda folder, progress=print: None)
    monkeypatch.setattr(wakeword, "Features", BandFeatures)
    meta = wakeword.train(cfg, quick=True, extra=False, progress=lambda s: None)
    assert meta["phrase"] == "Чуєш, Кру" and meta["recall"] > 0.8 and meta["false_alarms"] <= 0.01
    assert wakeword.info(cfg)["version"] == meta["version"]
    with np.load(wakeword.model_path(cfg)) as z:
        assert {"w1", "b1", "w2", "b2", "w3", "b3", "mean", "std", "threshold"} <= set(z.files)


def test_ws_wake_model_and_detected(cfg, voice_ready):  # noqa: F811
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM([]))
    whisper, _ = voice_ready(agent)
    client = TestClient(create_app(cfg, agent=agent))
    data = base64.b64encode(b"RIFF").decode()
    with client, client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["voice"]["wakeword"] is None
        ws.send_json({"type": "wake_model"})
        assert "kru wakeword train" in _until(ws, "wake_model")[0]["error"]
        # фразу «Чуєш, Кру» впізнала модель на ПК: ім'я прибирається, навіть якщо Whisper почув «Крю»
        for heard, expected in (("Чуєш, Крю, котра година?", {"text": "Котра година?"}),
                                ("Чуєш, Кру.", {"text": "", "wake_only": True})):
            whisper.words = heard
            ws.send_json({"type": "voice_in", "id": heard, "data": data, "wake": "detected"})
            got, _ = _until(ws, "voice_text")
            assert {k: v for k, v in got.items() if k not in {"type", "id"}} == expected
    # навчена модель — у привітанні й у відповіді на wake_model
    rng = np.random.default_rng(0)
    params = wakeword.train_mlp(rng.normal(1, 1, (20, FRAMES, 96)), rng.normal(-1, 1, (40, FRAMES, 96)), rng, 1)
    wakeword.save(cfg, params, {"phrase": "Чуєш, Кру", "threshold": 0.6, "version": 42})
    for name in wakeword.FEATURE_FILES.values():
        (wakeword.wake_dir(cfg) / name).write_bytes(b"onnx " + name.encode())
    with client, client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["voice"]["wakeword"] == {"phrase": "Чуєш, Кру", "version": 42}
        ws.send_json({"type": "wake_model"})
        got, _ = _until(ws, "wake_model")
        assert (got["phrase"], got["version"], got["threshold"]) == ("Чуєш, Кру", 42, 0.6)
        assert base64.b64decode(got["files"]["embedding"]) == b"onnx embedding_model.onnx"
        with np.load(io.BytesIO(base64.b64decode(got["files"]["model"]))) as z:
            assert float(z["threshold"]) == np.float32(0.6)
