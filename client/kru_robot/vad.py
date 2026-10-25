"""Детектор голосу: чи звучить зараз людська мова, а не гул, музика, стук чи клацання клавіатури.

Silero VAD (https://github.com/snakers4/silero-vad, MIT) — нейромережа ~2 МБ на тому ж onnxruntime, що й модель
фрази «Чуєш, Кру»; <1 % одного ядра. Звук 16 кГц шматками по 512 відліків (32 мс) → ймовірність мови 0…1.
Модель лежить поруч (models/silero_vad.onnx), тож нічого не завантажується.
"""

from __future__ import annotations

import sys
from pathlib import Path

MODEL = Path(__file__).with_name("models") / "silero_vad.onnx"
CHUNK = 512  # відліків на крок моделі (16 кГц)
CONTEXT = 64  # стільки останніх відліків попереднього шматка модель бачить разом із новим


class SileroVad:
    def __init__(self, path: Path = MODEL) -> None:
        import numpy as np
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = opts.inter_op_num_threads = 1  # маленька модель — одного потоку досить
        opts.log_severity_level = 3
        self._np = np
        self._session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        self._sr = np.array(16000, dtype=np.int64)
        self.prob = 0.0
        self.reset()

    def reset(self) -> None:
        np = self._np
        self._state = np.zeros((2, 1, 128), np.float32)
        self._context = np.zeros(CONTEXT, np.float32)
        self._buf = np.zeros(0, np.float32)
        self.prob = 0.0

    def feed(self, pcm16: bytes) -> float:
        """Новий звук (16 кГц, моно, 16 біт) → ймовірність мови в останньому повному шматку."""
        np = self._np
        x = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, x]) if self._buf.size else x
        while self._buf.size >= CHUNK:
            chunk, self._buf = self._buf[:CHUNK], self._buf[CHUNK:]
            inp = np.concatenate([self._context, chunk])[None, :]
            out, self._state = self._session.run(None, {"input": inp, "state": self._state, "sr": self._sr})
            self._context = chunk[-CONTEXT:]
            self.prob = float(out[0][0])
        return self.prob


def load() -> SileroVad | None:
    """Детектор голосу або None (немає numpy/onnxruntime чи моделі) — тоді слухаємо за гучністю, як раніше."""
    try:
        return SileroVad()
    except Exception as e:  # noqa: BLE001
        print(f"[kru-robot] детектор голосу недоступний ({e}) — визначаю мовлення за гучністю", file=sys.stderr)
        return None
