"""Своє слово-активатор для режиму «завжди слухаю» — «Чуєш, Кру» (openWakeWord).

Маленька офлайн-модель Vosk на ПК чує не те: вона мусить віднести кожен звук до одного зі слів свого
списку. openWakeWord працює інакше: готова нейромережа Google перетворює звук на «відбитки» (embeddings),
а поверх них ми вчимо крихітний класифікатор, що розпізнає лише одну фразу — незалежно від мови.

Навчання (kru wakeword train) — тут, на сервері, без жодних датасетів:
  1. Фразу озвучують усі голоси HolosTTS (або Piper) з різним темпом та інтонацією.
  2. Кожен запис «псується» по-різному: гучність, шум, луна, трохи інша висота — так модель чує фразу
     в кімнаті, а не лише чистий синтез.
  3. Негативні приклади — звичайні українські фрази й схожі на ім'я («Чуєш, друже», «круто»), шум, тиша;
     якщо вийде, ще й ~11 годин чужого мовлення з набору openWakeWord.
  4. Класифікатор (два шари по 64 нейрони) вчиться на numpy за кілька хвилин; поріг підбирається так,
     щоб на відкладених негативних прикладах хибних спрацювань майже не було.
Робот забирає модель із сервера (повідомлення wake_model) і слухає сам — звук іде на сервер лише після фрази.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("kru.wakeword")

DEFAULT_PHRASE = "Чуєш, Кру"
RATE = 16000
CLIP = 2 * RATE  # 2 с звуку → рівно 16 «відбитків» (стільки бачить класифікатор)
FRAMES = 16
DIM = 96
FEATURE_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/{name}"
FEATURE_FILES = {"melspectrogram": "melspectrogram.onnx", "embedding": "embedding_model.onnx"}
# ~11 годин різного мовлення, музики й шуму (готові відбитки з набору openWakeWord) — негативні приклади
EXTRA_NEGATIVES_URL = ("https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/"
                       "validation_set_features.npy")

# звичайне мовлення, на яке робот не має відгукуватися
NEGATIVE_TEXTS = [
    "Привіт, як справи?", "Котра зараз година?", "Яка сьогодні погода?", "Увімкни, будь ласка, світло.",
    "Я йду на роботу.", "Що ти робиш?", "Подай мені чашку.", "Сьогодні дуже гарний день.",
    "Ходімо гуляти.", "Де мої ключі?", "Мені треба подумати.", "Давай поговоримо пізніше.",
    "Ти мене чуєш?", "Я тебе чую.", "Чую, чую.", "Чудово!", "Круто!", "Кружка на столі.",
    "Кров з носа.", "Крок за кроком.", "Круглий стіл.", "Хто там?", "Добре, домовились.",
    "Зачекай хвилинку.", "Я не знаю.", "Подзвони мамі.", "Купи хліба й молока.", "Що на вечерю?",
    "Вимкни телевізор.", "Музика занадто гучна.", "Кіт спить на дивані.", "Діти пішли до школи.",
    "Почнімо з початку.", "Відкрий вікно.", "Чому так довго?", "Як тебе звати?", "Гарного вечора!",
    "Доброго ранку!", "На добраніч.", "Дякую, все добре.", "Слухай, а ти знаєш?", "Ну що, поїхали?",
    "Агов, ти де?", "Чекай, я зараз.", "Скільки коштує?", "Завтра буде дощ.", "Я втомився.",
    "Це дуже цікаво.", "Переглянь пошту.", "Нагадай мені про зустріч.", "Знайди рецепт борщу.",
    "Котик, іди сюди.", "Юра, ходи вечеряти!", "Христя, ти чуєш?", "Кирило, де ти?",
    "Ти що, не чуєш?", "Чуєш, як гарно співає?", "Чуєш, друже?", "Чуєш, брате?", "Круто, що ти прийшов.",
]


def wake_dir(cfg) -> Path:
    return cfg.data_dir / "wakeword"


def model_path(cfg) -> Path:
    return wake_dir(cfg) / "model.npz"


def info(cfg) -> dict | None:
    """Навчена модель: {"phrase", "version", ...} або None."""
    meta = wake_dir(cfg) / "model.json"
    if not model_path(cfg).is_file() or not meta.is_file():
        return None
    try:
        return json.loads(meta.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def bundle(cfg) -> dict[str, bytes] | None:
    """Усе, що потрібно роботу: класифікатор і дві моделі відбитків."""
    if info(cfg) is None:
        return None
    files = {"model": model_path(cfg)} | {k: wake_dir(cfg) / v for k, v in FEATURE_FILES.items()}
    try:
        return {k: p.read_bytes() for k, p in files.items()}
    except OSError:
        return None


def ensure_features(folder: Path, progress=print) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in FEATURE_FILES.values():
        path = folder / name
        if path.is_file() and path.stat().st_size > 100_000:
            continue
        progress(f"→ Завантажую {name} (openWakeWord)…")
        part = path.with_suffix(".part")
        with urllib.request.urlopen(FEATURE_URL.format(name=name), timeout=60) as r, open(part, "wb") as f:
            f.write(r.read())
        part.replace(path)


class Features:
    """Звук 16 кГц → відбитки (n, 96): мел-спектрограма → нейромережа Google speech_embedding."""

    def __init__(self, folder: Path, threads: int = 0) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = opts.inter_op_num_threads = threads
        self.mel = ort.InferenceSession(str(folder / FEATURE_FILES["melspectrogram"]), sess_options=opts,
                                        providers=["CPUExecutionProvider"])
        self.emb = ort.InferenceSession(str(folder / FEATURE_FILES["embedding"]), sess_options=opts,
                                        providers=["CPUExecutionProvider"])

    def clips(self, audio):
        """(n, 32000) int16 → (n, 16, 96): кожен 2-секундний шматок — вхід класифікатора."""
        import numpy as np

        out = []
        for i in range(0, len(audio), 64):
            batch = np.asarray(audio[i:i + 64], dtype=np.float32)
            spec = self.mel.run(None, {"input": batch})[0]  # (n·кадри, 1, ?, 32) — розгортаємо по кліпах
            spec = spec.reshape(len(batch), -1, 32) / 10 + 2
            # вікна вирівняні по кінцю запису — так само, як їх рахує робот у потоці
            starts = list(range(spec.shape[1] - 76, -1, -8))[:FRAMES][::-1]
            windows = np.stack([spec[:, j:j + 76] for j in starts], axis=1)
            emb = self.emb.run(None, {"input_1": windows.reshape(-1, 76, 32, 1).astype(np.float32)})[0]
            out.append(emb.reshape(len(batch), FRAMES, DIM))
        return np.concatenate(out) if out else np.zeros((0, FRAMES, DIM), np.float32)


# ---------------- синтез і «псування» записів ----------------
def to_16k(wav: bytes):
    import numpy as np

    from .voice import decode_audio

    a = decode_audio(wav, RATE)
    a = a / (np.max(np.abs(a)) + 1e-9)
    keep = np.nonzero(np.abs(a) > 0.03)[0]  # без тиші на початку й у кінці
    return a[keep[0]:keep[-1] + 1].astype(np.float32) if len(keep) else a[:0]


def phrase_variants(phrase: str) -> list[str]:
    base = phrase.strip(" .,!?…")
    plain = base.replace(",", "")
    return [base + "!", base + ".", base + "?", plain + ".", base + "…"]


def near_misses(phrase: str) -> list[str]:
    """Схоже, але не те: окремі слова фрази й інші звертання."""
    words = [w.strip(" ,.!?…") for w in phrase.split() if w.strip(" ,.!?…")]
    out = [w + "." for w in words]
    if len(words) >= 2:
        first, last = words[0], words[-1]
        out += [f"{first}, {x}." for x in ("друже", "Юро", "брате", "Кіро", "мамо")]
        out += [f"{y}, {last}." for y in ("Привіт", "Агов", "Слухай", "Ну", "Гей")]
        out += [f"{first}, {last[:-1] or last}о.", f"{last}то!", f"{last}жка."]
    return out


def colored_noise(rng, n: int, kind: str):
    import numpy as np

    white = rng.standard_normal(n).astype(np.float32)
    if kind == "white":
        return white
    spec = np.fft.rfft(white)
    f = np.arange(len(spec), dtype=np.float32) + 1
    spec /= np.sqrt(f) if kind == "pink" else f  # pink 1/f, brown 1/f²
    out = np.fft.irfft(spec, n).astype(np.float32)
    return out / (np.std(out) + 1e-9)


def augment(clip, rng, positive: bool):
    """Запис → 2-секундне вікно, як його почує мікрофон: темп, гучність, луна, шум; фраза — в кінці вікна."""
    import numpy as np

    x = clip
    if rng.random() < 0.7:  # трохи інший темп/висота
        k = rng.uniform(0.9, 1.1)
        x = np.interp(np.arange(0, len(x) - 1, k), np.arange(len(x)), x).astype(np.float32)
    if rng.random() < 0.35:  # луна кімнати
        n = int(RATE * rng.uniform(0.05, 0.35))
        ir = rng.standard_normal(n).astype(np.float32) * np.exp(-np.linspace(0, rng.uniform(4, 9), n))
        ir[0] = 1.0
        size = len(x) + n
        x = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[: len(x) + n // 2].astype(np.float32)
        x /= np.max(np.abs(x)) + 1e-9
    out = np.zeros(CLIP, np.float32)
    if positive:  # фраза щойно закінчилась (до 0,3 с тому) — тут модель має спрацювати
        end = CLIP - int(rng.uniform(0, 0.3) * RATE)
        start = end - len(x)
        piece = x[max(0, -start):]
        out[max(0, start):max(0, start) + len(piece)] = piece[: CLIP - max(0, start)]
    else:  # будь-де, зокрема частково
        start = int(rng.uniform(-len(x) * 0.5, CLIP - len(x) * 0.5))
        piece = x[max(0, -start):]
        s = max(0, start)
        out[s:s + len(piece)] = piece[: CLIP - s]
    out *= rng.uniform(0.1, 0.9)
    if rng.random() < 0.85:
        level = np.sqrt(np.mean(out ** 2)) + 1e-4
        snr = rng.uniform(3, 30)
        noise = colored_noise(rng, CLIP, rng.choice(["white", "pink", "brown"]))
        out += noise * level / (10 ** (snr / 20))
    return (np.clip(out, -1, 1) * 32767).astype(np.int16)


def noise_windows(rng, n: int):
    import numpy as np

    out = []
    for _ in range(n):
        x = colored_noise(rng, CLIP, rng.choice(["white", "pink", "brown"])) * rng.uniform(0.001, 0.3)
        if rng.random() < 0.3:  # клацання, стук
            for _ in range(rng.integers(1, 5)):
                p = int(rng.integers(0, CLIP - 800))
                x[p:p + 800] += rng.standard_normal(800).astype(np.float32) * np.exp(-np.linspace(0, 8, 800)) * 0.5
        out.append((np.clip(x, -1, 1) * 32767).astype(np.int16))
    return out


# ---------------- класифікатор (numpy) ----------------
def _forward(params: dict, x):
    import numpy as np

    h1 = np.maximum(0, x @ params["w1"] + params["b1"])
    h2 = np.maximum(0, h1 @ params["w2"] + params["b2"])
    z = h2 @ params["w3"] + params["b3"]
    return h1, h2, z


def predict(params: dict, feats):
    """(n, 16, 96) → ймовірність фрази (n,)."""
    import numpy as np

    x = (feats.reshape(len(feats), -1) - params["mean"]) / params["std"]
    z = _forward(params, x)[2][:, 0]
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def train_mlp(pos, neg, rng, epochs: int = 40, hidden: int = 64, progress=lambda s: None) -> dict:
    """Два шари ReLU + сигмоїда, Adam, зважена перехресна ентропія (хибне спрацювання дорожче за пропуск)."""
    import numpy as np

    x = np.concatenate([pos, neg]).reshape(len(pos) + len(neg), -1).astype(np.float32)
    y = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))]).astype(np.float32)
    mean, std = x.mean(axis=0), x.std(axis=0) + 1e-3
    x = (x - mean) / std
    d = x.shape[1]
    p = {"w1": rng.standard_normal((d, hidden)).astype(np.float32) * np.sqrt(2 / d),
         "b1": np.zeros(hidden, np.float32),
         "w2": rng.standard_normal((hidden, hidden)).astype(np.float32) * np.sqrt(2 / hidden),
         "b2": np.zeros(hidden, np.float32),
         "w3": rng.standard_normal((hidden, 1)).astype(np.float32) * np.sqrt(1 / hidden),
         "b3": np.zeros(1, np.float32)}
    m = {k: np.zeros_like(v) for k, v in p.items()}
    v = {k: np.zeros_like(v) for k, v in p.items()}
    w_pos = 0.5 * len(neg) / max(1, len(pos))  # класи врівноважені, але негативи «важать» удвічі більше
    lr, b1, b2, t, decay = 1e-3, 0.9, 0.999, 0, 1e-4
    for epoch in range(epochs):
        order = rng.permutation(len(x))
        for i in range(0, len(x), 256):
            idx = order[i:i + 256]
            xb, yb = x[idx], y[idx]
            h1, h2, z = _forward(p, xb)
            prob = 1 / (1 + np.exp(-np.clip(z[:, 0], -30, 30)))
            wt = np.where(yb == 1, w_pos, 1.0)
            dz = ((prob - yb) * wt / wt.sum())[:, None]
            dh2 = (dz @ p["w3"].T) * (h2 > 0)
            dh1 = (dh2 @ p["w2"].T) * (h1 > 0)
            grads = {"w3": h2.T @ dz, "b3": dz.sum(0), "w2": h1.T @ dh2, "b2": dh2.sum(0),
                     "w1": xb.T @ dh1, "b1": dh1.sum(0)}
            t += 1
            for k in p:
                g = grads[k] + (decay * p[k] if k.startswith("w") else 0)
                m[k] = b1 * m[k] + (1 - b1) * g
                v[k] = b2 * v[k] + (1 - b2) * g * g
                p[k] -= lr * (m[k] / (1 - b1 ** t)) / (np.sqrt(v[k] / (1 - b2 ** t)) + 1e-8)
        if epoch % 10 == 9:
            progress(f"   епоха {epoch + 1}/{epochs}")
    p["mean"], p["std"] = mean.astype(np.float32), std.astype(np.float32)
    return p


def pick_threshold(scores_pos, scores_neg) -> tuple[float, float, float]:
    """Поріг: найнижчий, за якого хибних спрацювань на відкладених негативах ≤ 0,1 %. → (поріг, влучність, хибні)."""
    import numpy as np

    for thr in np.arange(0.5, 0.995, 0.01):
        fa = float(np.mean(scores_neg >= thr)) if len(scores_neg) else 0.0
        if fa <= 0.001:
            return float(round(thr, 2)), float(np.mean(scores_pos >= thr)), fa
    thr = 0.99
    return thr, float(np.mean(scores_pos >= thr)), float(np.mean(scores_neg >= thr))


def save(cfg, params: dict, meta: dict) -> Path:
    import numpy as np

    folder = wake_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    np.savez(buf, threshold=np.float32(meta["threshold"]), **{k: v for k, v in params.items()})
    tmp = folder / "model.npz.part"
    tmp.write_bytes(buf.getvalue())
    tmp.replace(model_path(cfg))
    (folder / "model.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    return model_path(cfg)


# ---------------- усе разом ----------------
async def _synthesize(voice, items: list[tuple[str, str, float]], progress) -> list:
    out = []
    for i, (text, name, speed) in enumerate(items):
        try:
            wav = await voice.speak(text, voice=name or None, effect="natural", speed=speed)
            clip = to_16k(wav)
            if len(clip) > RATE * 0.2:
                out.append(clip)
        except Exception as e:  # noqa: BLE001 — один голос не вдався, решта ні
            log.info("Синтез «%s» (%s): %s", text, name, e)
        if (i + 1) % 25 == 0 or i + 1 == len(items):
            progress(f"   {i + 1}/{len(items)}")
    return out


def train(cfg, phrase: str = DEFAULT_PHRASE, quick: bool = False, extra: bool = True, seed: int = 7,
          progress=print) -> dict:
    """Навчити модель фрази й зберегти в <data_dir>/wakeword. → опис моделі."""
    import numpy as np

    from .voice import Voice

    started = time.monotonic()
    rng = np.random.default_rng(seed)
    voice = Voice(cfg)
    if not voice.engine:
        raise RuntimeError("Немає чим озвучувати фразу: deploy/holos.sh start (або deploy/voice.sh install)")
    folder = wake_dir(cfg)
    ensure_features(folder, progress)

    async def generate():
        await voice.refresh_voices(force=True)
        names = voice.voices() or [""]
        if quick:
            names = names[:4]
        speeds = (0.85, 1.0, 1.2) if not quick else (1.0,)
        pos_items = [(t, n, s) for n in names for s in speeds for t in phrase_variants(phrase)]
        near = near_misses(phrase)
        neg_texts = NEGATIVE_TEXTS[: 20 if quick else None]
        neg_items = [(t, n, s) for t in near for n in names[:6] for s in (0.9, 1.1)]
        neg_items += [(t, names[i % len(names)], float(rng.choice([0.9, 1.0, 1.15])))
                      for i, t in enumerate(neg_texts * (1 if quick else 3))]
        progress(f"→ Озвучую «{phrase}»: {len(pos_items)} записів ({len(names)} голосів)…")
        pos = await _synthesize(voice, pos_items, progress)
        progress(f"→ Озвучую інші фрази (негативні приклади): {len(neg_items)} записів…")
        neg = await _synthesize(voice, neg_items, progress)
        return pos, neg

    pos_clips, neg_clips = asyncio.run(generate())
    if len(pos_clips) < 5:
        raise RuntimeError("Не вдалося озвучити фразу — перевір голос на сервері (deploy/holos.sh status)")

    reps = 4 if quick else 12
    progress(f"→ Готую приклади: {len(pos_clips)} × {reps} варіантів фрази, шум, луна…")
    # відкладаємо частину вихідних записів — на них підбираємо поріг (модель їх не бачила)
    split_p, split_n = max(1, len(pos_clips) // 8), max(1, len(neg_clips) // 8)
    pos_val_src, pos_tr_src = pos_clips[:split_p], pos_clips[split_p:]
    neg_val_src, neg_tr_src = neg_clips[:split_n], neg_clips[split_n:]
    pos_tr = [augment(c, rng, True) for c in pos_tr_src for _ in range(reps)]
    pos_val = [augment(c, rng, True) for c in pos_val_src for _ in range(reps)]
    neg_tr = [augment(c, rng, False) for c in neg_tr_src for _ in range(reps)]
    neg_val = [augment(c, rng, False) for c in neg_val_src for _ in range(reps)]
    neg_tr += noise_windows(rng, 300 if quick else 1500)
    neg_val += noise_windows(rng, 50 if quick else 300)
    # «майже фраза»: лише початок чи лише кінець запису — модель має чекати кінця імені
    for c in pos_tr_src[: len(pos_tr_src) // 2]:
        cut = int(len(c) * rng.uniform(0.3, 0.6))
        neg_tr.append(augment(c[:cut], rng, True))
        neg_tr.append(augment(c[cut:], rng, True))

    progress("→ Рахую відбитки звуку…")
    feats = Features(folder)
    f_pos_tr, f_pos_val = feats.clips(pos_tr), feats.clips(pos_val)
    f_neg_tr, f_neg_val = feats.clips(neg_tr), feats.clips(neg_val)
    extra_used = 0
    if extra:
        more = _extra_negatives(folder, rng, progress)
        if more is not None and len(more):
            k = len(more) * 9 // 10
            f_neg_tr = np.concatenate([f_neg_tr, more[:k]])
            f_neg_val = np.concatenate([f_neg_val, more[k:]])
            extra_used = len(more)

    progress(f"→ Навчаю модель: {len(f_pos_tr)} прикладів фрази, {len(f_neg_tr)} — іншого звуку…")
    params = train_mlp(f_pos_tr, f_neg_tr, rng, epochs=15 if quick else 40, progress=progress)
    thr, recall, fa = pick_threshold(predict(params, f_pos_val), predict(params, f_neg_val))
    meta = {"phrase": phrase, "threshold": thr, "recall": round(recall, 3), "false_alarms": round(fa, 4),
            "version": int(time.time()), "positives": len(pos_clips), "negatives": len(neg_clips),
            "extra_negatives": extra_used, "seconds": int(time.monotonic() - started)}
    save(cfg, params, meta)
    return meta


def _extra_negatives(folder: Path, rng, progress):
    """Відбитки ~11 годин чужого мовлення/музики/шуму з набору openWakeWord (якщо вдасться завантажити)."""
    import numpy as np

    path = folder / "extra_negatives.npy"
    if not path.is_file():
        progress("→ Завантажую додаткові негативні приклади (~180 МБ, один раз)…")
        try:
            part = path.with_suffix(".part")
            with urllib.request.urlopen(EXTRA_NEGATIVES_URL, timeout=120) as r, open(part, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            part.replace(path)
        except OSError as e:
            progress(f"   не вдалося ({e}) — обійдемося без них")
            return None
    try:
        data = np.load(path, mmap_mode="r")
    except (OSError, ValueError) as e:
        progress(f"   файл пошкоджено ({e}) — обійдемося без нього")
        return None
    if data.ndim == 3 and data.shape[1:] == (FRAMES, DIM):
        idx = rng.choice(len(data), size=min(len(data), 40000), replace=False)
        return np.asarray(data[np.sort(idx)], dtype=np.float32)
    if data.ndim == 2 and data.shape[1] == DIM:  # суцільна стрічка відбитків → вікна по 16
        starts = rng.choice(len(data) - FRAMES, size=min(len(data) - FRAMES, 40000), replace=False)
        return np.stack([np.asarray(data[s:s + FRAMES], dtype=np.float32) for s in np.sort(starts)])
    progress(f"   несподіваний формат {data.shape} — пропускаю")
    return None
