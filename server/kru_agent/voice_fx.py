"""Звучання голосу Kru: обробка того, що синтезував HolosTTS (numpy).

Кожне звучання — набір простих ефектів:
  pitch — вища/нижча висота без зміни темпу (HolosTTS синтезує повільніше, а ми «прискорюємо» запис назад);
  ring  — кільцева модуляція (класичний «металевий» голос робота), м'яко підмішана до чистого;
  comb  — гребінчастий фільтр із коротким відлунням: «резонанс корпусу», трохи бляшаний;
  chorus — друга копія з плаваючою затримкою: об'ємний «цифровий» відтінок.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass


@dataclass(frozen=True)
class Effect:
    title: str
    pitch: float = 1.0  # 1.08 — на 8 % вище
    ring_hz: float = 0.0
    ring_mix: float = 0.0  # 0..1 — скільки модульованого сигналу
    comb_ms: float = 0.0
    comb_feedback: float = 0.0
    comb_mix: float = 0.0
    chorus_mix: float = 0.0


EFFECTS: dict[str, Effect] = {
    "natural": Effect("Звичайний"),
    # приємний, ледь металевий: голос лишається теплим і розбірливим
    "robot": Effect("М'який робот", pitch=1.06, ring_hz=50, ring_mix=0.22, comb_ms=6.0, comb_feedback=0.35,
                    comb_mix=0.35, chorus_mix=0.15),
    # чистий «цифровий помічник»: об'ємний, без дзижчання
    "digital": Effect("Цифровий помічник", pitch=1.03, comb_ms=3.5, comb_feedback=0.25, comb_mix=0.25,
                      chorus_mix=0.4),
    # маленький дроїд: вищий, дзвінкіший, помітно «залізний»
    "droid": Effect("Маленький дроїд", pitch=1.14, ring_hz=85, ring_mix=0.4, comb_ms=4.0, comb_feedback=0.5,
                    comb_mix=0.45),
    # великий робот: нижчий, з гулким корпусом
    "mech": Effect("Великий робот", pitch=0.9, ring_hz=35, ring_mix=0.3, comb_ms=9.0, comb_feedback=0.45,
                   comb_mix=0.45),
}
DEFAULT_EFFECT = "robot"


def apply(wav_bytes: bytes, effect: Effect) -> bytes:
    """WAV (16 біт, моно) → WAV з ефектом. pitch тут — лише перерахунок довжини (HolosTTS уже синтезував
    повільніше на той самий коефіцієнт, тож темп повертається до звичайного, а висота зростає)."""
    import numpy as np

    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2 or ch != 1 or not raw:
        return wav_bytes
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768

    if effect.pitch != 1.0 and len(x) > 1:
        n = max(1, int(round(len(x) / effect.pitch)))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    t = np.arange(len(x), dtype=np.float32) / rate
    y = x
    if effect.ring_mix:
        y = (1 - effect.ring_mix) * y + effect.ring_mix * y * np.sin(2 * np.pi * effect.ring_hz * t)
    if effect.chorus_mix:
        delay = (0.011 + 0.004 * np.sin(2 * np.pi * 0.7 * t)) * rate  # 7–15 мс, плаває
        idx = np.arange(len(y), dtype=np.float32) - delay
        wet = np.interp(idx, np.arange(len(y)), y, left=0.0)
        y = (1 - effect.chorus_mix * 0.5) * y + effect.chorus_mix * 0.5 * wet
    if effect.comb_mix:
        y = (1 - effect.comb_mix) * y + effect.comb_mix * _comb(y, int(rate * effect.comb_ms / 1000), effect.comb_feedback)
    # плавний початок і кінець (без клацань) і запас до максимуму
    fade = min(len(y) // 2, int(rate * 0.01))
    if fade:
        ramp = np.linspace(0, 1, fade, dtype=np.float32)
        y[:fade] *= ramp
        y[-fade:] *= ramp[::-1]
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak > 0:
        y = y * (0.89 / peak)  # ~ −1 дБ
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    return out.getvalue()


def _comb(x, delay: int, feedback: float):
    """y[n] = x[n] + g·y[n−D], поблоково по D відліків (векторно — швидко й на хвилинних записах)."""
    import numpy as np

    if delay <= 0:
        return x
    y = np.array(x, dtype=np.float32, copy=True)
    for start in range(delay, len(y), delay):
        end = min(start + delay, len(y))
        y[start:end] += feedback * y[start - delay:end - delay]
    return y
