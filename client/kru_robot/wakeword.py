"""Своя фраза-активатор («Чуєш, Кру») прямо на ПК: модель openWakeWord, навчена на сервері (kru wakeword train).

Звук 16 кГц шматками по 80 мс → мел-спектрограма → «відбитки» нейромережі Google speech_embedding →
крихітний класифікатор, що впізнає лише одну фразу. ≈3 % одного ядра; звук нікуди не йде, доки фраза
не прозвучала. Модель і дві мережі відбитків робот один раз забирає з сервера (повідомлення wake_model).
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path

CHUNK = 1280  # 80 мс — один новий «відбиток»
FRAMES = 16  # стільки відбитків (~1,3 с + вікно 0,8 с) бачить класифікатор
FILES = {"model": "model.npz", "melspectrogram": "melspectrogram.onnx", "embedding": "embedding_model.onnx"}
SENSITIVITY_SHIFT = {"low": 0.1, "medium": 0.0, "high": -0.15}  # зсув порогу моделі


def folder() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "kru-robot" / "wakeword"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "kru-robot" / "wakeword"


def available() -> str:
    """"" — можна; інакше — чого бракує."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception as e:  # noqa: BLE001 — нема пакета чи він не вантажиться на цій системі
        return f"немає numpy/onnxruntime ({e}) — онови робота: cd ~/bot && ./update.sh"
    return ""


def cached_version() -> int | None:
    try:
        meta = json.loads((folder() / "meta.json").read_text("utf-8"))
        if all((folder() / f).is_file() for f in FILES.values()):
            return int(meta.get("version"))
    except (OSError, ValueError, TypeError):
        pass
    return None


def save_bundle(ev: dict) -> Path:
    """Відповідь сервера wake_model → файли в теці робота."""
    files = ev.get("files") or {}
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    for key, name in FILES.items():
        data = base64.b64decode(str(files.get(key, "")), validate=True)
        if not data:
            raise ValueError(f"сервер не надіслав {name}")
        tmp = d / (name + ".part")
        tmp.write_bytes(data)
        tmp.replace(d / name)
    meta = {"phrase": ev.get("phrase", ""), "version": ev.get("version"), "threshold": ev.get("threshold")}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    return d


class Detector:
    """feed(pcm16 моно 16 кГц) → найбільша ймовірність фрази серед нових шматків по 80 мс (0, якщо їх ще нема)."""

    def __init__(self, path: Path | None = None, sensitivity: str = "medium", *, mel=None, emb=None,
                 params: dict | None = None) -> None:
        import numpy as np

        self.np = np
        path = path or folder()
        if mel is None or emb is None:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = opts.inter_op_num_threads = 1  # не заважати решті ПК
            m = ort.InferenceSession(str(path / FILES["melspectrogram"]), sess_options=opts,
                                     providers=["CPUExecutionProvider"])
            e = ort.InferenceSession(str(path / FILES["embedding"]), sess_options=opts,
                                     providers=["CPUExecutionProvider"])
            mel = mel or (lambda x: m.run(None, {"input": x})[0])
            emb = emb or (lambda x: e.run(None, {"input_1": x})[0])
        self._mel, self._emb = mel, emb
        if params is None:
            with np.load(io.BytesIO((path / FILES["model"]).read_bytes())) as z:
                params = {k: z[k] for k in z.files}
        self.p = params
        base = float(params.get("threshold", 0.5))
        self.threshold = min(0.99, max(0.2, base + SENSITIVITY_SHIFT.get(sensitivity, 0.0)))
        self.score = 0.0
        self.reset()

    def reset(self) -> None:
        """Забути почуте (після відповіді — щоб давня фраза не спрацювала вдруге)."""
        np = self.np
        self._pending = np.zeros(0, np.int16)
        self._tail = np.zeros(480, np.int16)
        self._mel_buf = np.ones((76, 32), np.float32)
        self._feats = np.zeros((0, 96), np.float32)
        rng = np.random.default_rng(0)
        quiet = (rng.standard_normal(CHUNK * (FRAMES + 2)) * 3).astype(np.int16)  # «тиша» з ледь чутним шумом
        for i in range(0, len(quiet), CHUNK):
            self._step(quiet[i:i + CHUNK])
        self.score = 0.0

    def _step(self, chunk) -> None:
        np = self.np
        audio = np.concatenate([self._tail, chunk])
        self._tail = audio[-480:]
        spec = np.asarray(self._mel(audio[None].astype(np.float32))).reshape(-1, 32) / 10 + 2
        self._mel_buf = np.vstack([self._mel_buf, spec])[-76:]
        e = np.asarray(self._emb(self._mel_buf[None, :, :, None].astype(np.float32))).reshape(1, 96)
        self._feats = np.vstack([self._feats, e])[-FRAMES:]

    def _predict(self) -> float:
        np, p = self.np, self.p
        x = (self._feats.reshape(1, -1) - p["mean"]) / p["std"]
        h = np.maximum(0, x @ p["w1"] + p["b1"])
        h = np.maximum(0, h @ p["w2"] + p["b2"])
        z = float((h @ p["w3"] + p["b3"])[0, 0])
        return 1 / (1 + np.exp(-max(-30.0, min(30.0, z))))

    def feed(self, pcm: bytes) -> float:
        np = self.np
        data = np.concatenate([self._pending, np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")])
        best = 0.0
        n = len(data) // CHUNK * CHUNK
        for i in range(0, n, CHUNK):
            self._step(data[i:i + CHUNK])
            if len(self._feats) == FRAMES:
                best = max(best, self._predict())
        self._pending = data[n:]
        if n:
            self.score = best
        return best
