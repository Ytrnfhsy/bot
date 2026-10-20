"""Режим «завжди слухаю»: робот чекає на своє ім'я — «Кру» — і тоді слухає питання.

Як це влаштовано:
  1. Мікрофон працює постійно, але звук нікуди не йде: маленька офлайн-модель Vosk тут, на ПК,
     шукає лише ім'я (≈2–5 % одного ядра). Модель завантажується один раз при першому ввімкненні.
  2. Почувши «Кру», робот записує фразу до кінця (до паузи). Останні секунди звуку весь час
     тримаються в пам'яті, тож «Кру, яка сьогодні погода?» можна сказати одним духом.
  3. Запис іде на сервер — Whisper розпізнає точно й перевіряє, що фраза справді починається з імені
     (хибні спрацювання на «круто», «кружка» відкидаються мовчки), ім'я прибирається, решта — задача.
  4. Після відповіді робот ще кілька секунд слухає без імені — для уточнення («а завтра?»).

WakeLogic — сама логіка (без Qt, перевіряється в самоперевірці), WakeListener — мікрофон + потік Vosk.
"""

from __future__ import annotations

import array
import json
import math
import os
import queue
import sys
import threading
import time
import urllib.request
import zipfile
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .audio import make_wav, to_mono16

FRAME_MS = 30  # аналізуємо звук шматками по 30 мс
PRE_ROLL = 0.35  # с звуку перед початком мовлення, що додається до запису (не обрізати перший склад)
BUFFER_SEC = 20  # стільки останніх секунд тримаємо в пам'яті
END_SILENCE = 0.9  # пауза, після якої фраза вважається завершеною
MAX_COMMAND = 15.0  # найдовша фраза після імені
WAKE_WORDS = ("кру", "крю", "кроу", "крук", "круг")  # як модель може «почути» ім'я (сервер перевіряє точніше)
SENSITIVITY = ("low", "medium", "high")


class WakeLogic:
    """Стан слухання. feed(pcm16) → список подій:
    ("wake",)                 — почуто ім'я, почався запис фрази;
    ("speech",)               — у режимі очікування уточнення почалося мовлення;
    ("utterance", wav, need)  — фраза записана (need — сервер має знайти в ній ім'я);
    ("timeout",)              — уточнення так і не прозвучало.
    Після "utterance" логіка чекає, поки застосунок викличе listen() чи await_command()."""

    def __init__(self, rate: int, recognizer=None, sensitivity: str = "medium") -> None:
        self.rate = rate
        self.rec = recognizer
        self.sensitivity = sensitivity if sensitivity in SENSITIVITY else "medium"
        self.frame_bytes = int(rate * FRAME_MS / 1000) * 2
        self.frames: deque[bytes] = deque(maxlen=int(BUFFER_SEC * 1000 / FRAME_MS))
        self.n = 0  # номер останнього кадру
        self._pending = b""
        self.state = "idle"  # idle | capture | await | busy | paused
        self.need_wake = True
        self.floor = 300.0  # рівень фонового шуму (адаптивний)
        self.speaking = False
        self.speech_start = 0  # кадр, з якого почалося поточне мовлення
        self.silence = 0  # кадрів тиші поспіль
        self.capture_from = 0
        self.capture_started = 0
        self.deadline = 0  # кадр, до якого чекаємо на уточнення
        self._hits = 0  # «середня» чутливість: ім'я в кількох проміжних результатах поспіль
        self.level = 0.0

    # ---------------- керування ----------------
    def listen(self) -> None:
        """Чекати на ім'я."""
        self.state = "idle"
        self._pending = b""
        self._reset_rec()

    def await_command(self, seconds: float) -> None:
        """Слухати без імені (уточнення після відповіді або «Кру…» з паузою)."""
        self.state = "await"
        self.deadline = self.n + int(seconds * 1000 / FRAME_MS)
        self.speaking, self.silence = False, 0  # чекаємо нового мовлення, а не хвіст попереднього
        self._pending = b""
        self._reset_rec()

    def pause(self) -> None:
        self.state = "paused"

    def _reset_rec(self) -> None:
        self._hits = 0
        if self.rec is not None:
            try:
                self.rec.Reset()
            except Exception:  # noqa: BLE001
                pass

    # ---------------- звук ----------------
    def feed(self, pcm: bytes) -> list[tuple]:
        events: list[tuple] = []
        data = self._pending + pcm
        fb = self.frame_bytes
        whole = len(data) // fb * fb
        self._pending = data[whole:]
        for i in range(0, whole, fb):
            events += self._frame(data[i:i + fb])
        return events

    def _frame(self, frame: bytes) -> list[tuple]:
        self.frames.append(frame)
        self.n += 1
        rms = _rms(frame)
        self.level = min(1.0, math.sqrt(rms / 32768) * 1.5)
        threshold = max(self.floor * 2.5, 350.0)
        loud = rms > threshold
        if not loud:
            self.floor = 0.97 * self.floor + 0.03 * max(rms, 50.0)
        events: list[tuple] = []
        was_speaking = self.speaking
        if loud:
            self.silence = 0
            if not self.speaking:
                self.speaking = True
                self.speech_start = self.n
                if self.state == "await":
                    self.state = "capture"
                    self.need_wake = False
                    self.capture_from = self._preroll(self.n)
                    self.capture_started = self.n
                    events.append(("speech",))
        else:
            self.silence += 1
            if self.speaking and self.silence * FRAME_MS / 1000 >= END_SILENCE:
                self.speaking = False

        if self.state == "await" and not self.speaking and self.n > self.deadline:
            self.state = "busy"
            return [("timeout",)]
        if self.state == "idle":
            hit = self._recognize(frame)
            if hit:
                self.state = "capture"
                self.need_wake = True
                # фраза — від початку поточного (чи щойно завершеного) мовлення, але не давніше 10 с
                recent = self.speaking or was_speaking or self.n - self.speech_start < 10 * 1000 / FRAME_MS
                start = self.speech_start if recent else self.n
                self.capture_from = self._preroll(max(start, self.n - int(10 * 1000 / FRAME_MS)))
                self.capture_started = self.n
                events.append(("wake",))
        elif self.state == "paused" or self.state == "busy":
            return events

        if self.state == "capture":
            long_enough = (self.n - self.capture_started) * FRAME_MS / 1000 >= MAX_COMMAND
            if not self.speaking or long_enough:
                events.append(("utterance", self._audio(self.capture_from), self.need_wake))
                self.state = "busy"
        return events

    def _recognize(self, frame: bytes) -> bool:
        if self.rec is None:
            return False
        final = self.rec.AcceptWaveform(frame)
        if final:
            res = _json(self.rec.Result())
            words = [w for w in res.get("result", []) if isinstance(w, dict)]
            if words:
                ok = any(_is_wake(w.get("word", "")) and
                         (self.sensitivity != "low" or float(w.get("conf", 1.0)) >= 0.6) for w in words)
            else:
                ok = _has_wake(res.get("text", ""))
            self._hits = 0
            return ok
        if self.sensitivity == "low":
            return False
        if _has_wake(_json(self.rec.PartialResult()).get("partial", "")):
            self._hits += 1
        else:
            self._hits = 0
        return self._hits >= (1 if self.sensitivity == "high" else 3)

    def _preroll(self, frame_no: int) -> int:
        return max(frame_no - int(PRE_ROLL * 1000 / FRAME_MS), self.n - len(self.frames) + 1)

    def _audio(self, from_frame: int) -> bytes:
        first = self.n - len(self.frames) + 1  # номер найстарішого кадру в буфері
        frames = list(self.frames)[max(0, from_frame - first):]
        return make_wav(b"".join(frames), self.rate)


def _rms(frame: bytes) -> float:
    a = array.array("h", frame)
    if sys.byteorder == "big":
        a.byteswap()
    if not a:
        return 0.0
    step = 3
    s = sum(x * x for x in a[::step])
    return math.sqrt(s / max(1, len(a[::step])))


def _json(text: str) -> dict:
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def _is_wake(word: str) -> bool:
    return word.strip(" ,.!?").lower() in WAKE_WORDS


def _has_wake(text: str) -> bool:
    return any(_is_wake(w) for w in text.split())


# ---------------- модель Vosk ----------------
MODEL_LIST_URL = "https://alphacephei.com/vosk/models/model-list.json"
MODEL_URL = "https://alphacephei.com/vosk/models/{name}.zip"
FALLBACK_MODEL = "vosk-model-small-uk-v3-nano"


def models_dir() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "kru-robot" / "vosk"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "kru-robot" / "vosk"


def installed_model() -> Path | None:
    d = models_dir()
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.is_dir() and ((p / "am").exists() or (p / "conf").exists()):
                return p
    return None


def _pick_model() -> tuple[str, str]:
    """Найменша актуальна українська модель зі списку Vosk → (назва, URL)."""
    try:
        with urllib.request.urlopen(MODEL_LIST_URL, timeout=15) as r:
            models = json.load(r)
        uk = [m for m in models if str(m.get("lang")) in {"uk", "ua"} and str(m.get("obsolete")) == "false"
              and m.get("type") == "small"]
        if uk:
            m = min(uk, key=lambda m: int(m.get("size") or 10**12))
            return m["name"], m.get("url") or MODEL_URL.format(name=m["name"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return FALLBACK_MODEL, MODEL_URL.format(name=FALLBACK_MODEL)


def download_model(progress=lambda text: None) -> Path:
    """Завантажити й розпакувати модель (один раз; ~50–130 МБ). Викликати не з потоку інтерфейсу."""
    if (found := installed_model()) is not None:
        return found
    d = models_dir()
    d.mkdir(parents=True, exist_ok=True)
    name, url = _pick_model()
    part = d / f"{name}.zip.part"
    progress(f"Завантажую модель розпізнавання імені ({name})…")
    with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0.0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            done += len(chunk)
            if total and time.monotonic() - last > 0.5:
                last = time.monotonic()
                progress(f"Завантажую модель розпізнавання імені: {done * 100 // total}%")
    progress("Розпаковую модель…")
    with zipfile.ZipFile(part) as z:
        root = d.resolve()
        for member in z.namelist():  # без виходу за межі теки
            if not (d / member).resolve().is_relative_to(root):
                raise OSError(f"дивний шлях в архіві: {member}")
        z.extractall(d)
    part.unlink(missing_ok=True)
    found = installed_model()
    if found is None:
        raise OSError("у завантаженому архіві немає моделі Vosk")
    return found


def make_recognizer(model_path: Path, rate: int):
    """Vosk-розпізнавач, обмежений лише ім'ям (найточніше й найлегше); якщо модель не знає жодного
    варіанта імені — вільне розпізнавання, а ім'я шукаємо в тексті."""
    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(-1)
    model = Model(str(model_path))
    known = [w for w in WAKE_WORDS if model.vosk_model_find_word(w) >= 0]  # слова, які модель знає
    rec = KaldiRecognizer(model, rate, json.dumps(known + ["[unk]"], ensure_ascii=False)) if known else \
        KaldiRecognizer(model, rate)
    rec.SetWords(True)
    return model, rec


class WakeListener(QObject):
    """Мікрофон + Vosk в окремому потоці. Сигнали — у потоці інтерфейсу."""

    woke = Signal()
    speech = Signal()
    utterance = Signal(bytes, bool)
    timeout = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source = None
        self._io = None
        self._fmt = ("Int16", 1, 16000)
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.logic: WakeLogic | None = None
        self._model = None

    @property
    def active(self) -> bool:
        return self._source is not None

    @property
    def level(self) -> float:
        return self.logic.level if self.logic else 0.0

    def start(self, model_path: Path, sensitivity: str = "medium") -> bool:
        from .audio import MicError, open_input

        if self._source is not None:
            return True
        try:
            source, fmt = open_input(self)
            model, rec = make_recognizer(model_path, fmt[2])
        except MicError as e:
            self.failed.emit(str(e))
            return False
        except Exception as e:  # noqa: BLE001 — немає vosk, пошкоджена модель тощо
            self.failed.emit(f"не вдалося запустити розпізнавання імені: {e}")
            return False
        self._model, self._fmt = model, fmt
        self.logic = WakeLogic(fmt[2], rec, sensitivity)
        self._io = source.start()
        if self._io is None:
            self.failed.emit("мікрофон зайнятий або доступ до нього заборонено")
            return False
        self._source = source
        self._io.readyRead.connect(self._read)
        self._stop.clear()
        self._thread = threading.Thread(target=self._work, name="kru-wake", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._source is not None:
            self._source.stop()
            self._source.deleteLater()
        self._source = self._io = None
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self.logic = None
        self._model = None

    # керування — з потоку інтерфейсу; логіку змінює лише робочий потік, тож передаємо командою
    def listen(self) -> None:
        self._command(("listen",))

    def await_command(self, seconds: float) -> None:
        self._command(("await", seconds))

    def pause(self) -> None:
        self._command(("pause",))

    def _command(self, cmd: tuple) -> None:
        if self.logic is not None:
            try:
                self._queue.put_nowait(cmd)
            except queue.Full:
                pass

    def _read(self) -> None:
        if self._io is None:
            return
        data = bytes(self._io.readAll().data())
        if data:
            try:
                self._queue.put_nowait(to_mono16(data, self._fmt[0], self._fmt[1]))
            except queue.Full:  # робочий потік не встигає — пропускаємо шматок, а не гальмуємо інтерфейс
                pass

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                item = b""
            logic = self.logic
            if logic is None:
                continue
            if isinstance(item, tuple):
                {"listen": lambda: logic.listen(), "pause": logic.pause,
                 "await": lambda: logic.await_command(item[1])}[item[0]]()
                continue
            try:
                events = logic.feed(item)
            except Exception as e:  # noqa: BLE001
                self.failed.emit(f"розпізнавання імені: {e}")
                return
            for ev in events:
                if ev[0] == "wake":
                    self.woke.emit()
                elif ev[0] == "speech":
                    self.speech.emit()
                elif ev[0] == "utterance":
                    self.utterance.emit(ev[1], ev[2])
                elif ev[0] == "timeout":
                    self.timeout.emit()
