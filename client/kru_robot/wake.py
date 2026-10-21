"""Режим «завжди слухаю»: робот чекає на своє ім'я — «Кру» — і тоді слухає питання.

Як це влаштовано:
  1. Мікрофон працює постійно. Ім'я шукає або сервер (за замовчуванням: кожна коротка фраза, що прозвучала
     в кімнаті, іде до Whisper — той самий, що розпізнає утримання робота, тож чує точно), або маленька
     офлайн-модель Vosk тут, на ПК (звук нікуди не йде, але маленька модель часто чує не те).
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

from PySide6.QtCore import QObject, QTimer, Signal

from .audio import Resampler, make_wav, to_mono16

RATE = 16000  # частота, на якій працюють Vosk і Whisper
FRAME_MS = 30  # аналізуємо звук шматками по 30 мс
PRE_ROLL = 0.35  # с звуку перед початком мовлення, що додається до запису (не обрізати перший склад)
BUFFER_SEC = 20  # стільки останніх секунд тримаємо в пам'яті
END_SILENCE = 1.2  # пауза, після якої фраза вважається завершеною (паузи між словами коротші)
MAX_COMMAND = 15.0  # найдовша фраза після імені
MIN_PHRASE = 0.25  # с — коротші звуки (клацання, стук) на сервер не надсилаємо
MIN_FOLLOWUP = 0.5  # с — уточнення без імені коротше за це вважаємо шумом і слухаємо далі
WAKE_WORDS = ("кру", "крю", "кроу", "крук", "круг")  # як модель може «почути» ім'я (сервер перевіряє точніше)
SENSITIVITY = ("low", "medium", "high")
# Часті слова — «куди падати» звичайному мовленню в граматиці, якщо модель не має [unk]
FILLER_WORDS = (
    "так ні що це як і в на не я ти ми він вона вони добре привіт дякую будь ласка де коли чому скільки "
    "котра година погода сьогодні завтра зараз може треба хочу слухай там тут вже ще дуже ну а о один два "
    "три чотири п'ять дім робота мама тато кіт вода хліб машина телефон комп'ютер гра музика книга день ніч "
    "ранок вечір рік час люди слово справи світло кружка круто"
).split()


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
        self.whole_phrases = recognizer is None  # без Vosk: кожна фраза йде на сервер, ім'я шукає Whisper
        self.detector = None  # своя фраза («Чуєш, Кру», wakeword.Detector) — шукає її сам, без сервера
        self.loose = False  # модель не знає слова «кру» — шукаємо схожі слова у вільному розпізнаванні
        self.debug = None  # функція для діагностики (KRU_WAKE_DEBUG=1)
        self._last_partial = ""

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
        if self.detector is not None:
            self.detector.reset()  # інакше щойно почута фраза спрацює ще раз
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
        # уточнення без імені — суворіше: інакше шум у кімнаті стає «питанням»
        strict = self.state == "await" or (self.state == "capture" and not self.need_wake)
        start = max(self.floor * (3.5 if strict else 2.5), 600.0 if strict else 350.0)
        # почалось мовлення — далі досить і тихішого звуку: тихі склади й кінці слів не рвуть фразу
        keep = max(self.floor * 1.6, 250.0)
        loud = rms > (keep if self.speaking else start)
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
        if self.state == "idle" and self.whole_phrases and self.detector is None:
            too_long = self.speaking and (self.n - self.speech_start) * FRAME_MS / 1000 >= MAX_COMMAND
            if (was_speaking and not self.speaking) or too_long:
                spoken = (self.n - self.speech_start - self.silence) * FRAME_MS / 1000
                if spoken >= MIN_PHRASE:
                    self.state, self.need_wake = "busy", True
                    if self.debug:
                        self.debug(f"фраза {spoken:.1f} с → на сервер (шукаю ім'я)")
                    events.append(("utterance", self._audio(self._preroll(self.speech_start)), True))
            return events
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
                spoken = (self.n - self.capture_started - self.silence) * FRAME_MS / 1000
                if not self.need_wake and spoken < MIN_FOLLOWUP and not long_enough:
                    if self.debug:
                        self.debug(f"уточнення {spoken:.1f} с — схоже на шум, пропускаю")
                    if self.n <= self.deadline:
                        self.state = "await"  # ще чекаємо справжнього уточнення
                    else:
                        self.state = "busy"
                        events.append(("timeout",))
                    return events
                events.append(("utterance", self._audio(self.capture_from), self.need_wake))
                self.state = "busy"
        return events

    def _recognize(self, frame: bytes) -> bool:
        if self.detector is not None:
            score = self.detector.feed(frame)
            if self.debug and score >= 0.2:
                self.debug(f"схоже на фразу: {score:.2f} (поріг {self.detector.threshold:.2f})")
            return score >= self.detector.threshold
        if self.rec is None:
            return False
        is_wake = _is_wake_loose if self.loose else _is_wake
        final = self.rec.AcceptWaveform(frame)
        if final:
            res = _json(self.rec.Result())
            # кілька варіантів почутого (SetMaxAlternatives): ім'я хоч в одному — рахується
            alts = res.get("alternatives") or [res]
            if self.debug:
                shown = [(a.get("text", ""), round(float(a.get("confidence", 0)), 1)) for a in alts if a.get("text")]
                if shown:
                    self.debug(f"почуто (фраза): {shown}")
            ok = False
            for a in alts[:3]:
                words = [w for w in a.get("result", []) if isinstance(w, dict)]
                if words:
                    ok = ok or any(is_wake(w.get("word", "")) and
                                   (self.sensitivity != "low" or float(w.get("conf", 1.0)) >= 0.6) for w in words)
                else:
                    ok = ok or any(is_wake(w) for w in a.get("text", "").split())
                if self.sensitivity == "low":
                    break  # «низька» — лише найімовірніший варіант
            self._hits = 0
            return ok
        if self.sensitivity == "low":
            return False
        partial = _json(self.rec.PartialResult()).get("partial", "")
        if self.debug and partial and partial != self._last_partial:
            self.debug(f"чую: {partial!r}")
        self._last_partial = partial
        if any(is_wake(w) for w in partial.split()):
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


def _is_wake_loose(word: str) -> bool:
    """Вільне розпізнавання (модель не знає «кру»): ім'я зазвичай чується як коротке слово на «кру-»/«крю-»."""
    w = word.strip(" ,.!?").lower()
    return w in WAKE_WORDS or (w.startswith(("кру", "крю")) and len(w) <= 5)


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


class Agc:
    """Автопідсилення: тихий мікрофон (мовлення ледь вище шуму) підтягуємо до нормальної гучності —
    і Vosk, і Whisper розпізнають помітно краще. Підсилення змінюється плавно й не «качає» тишу."""

    TARGET = 4000.0  # бажаний рівень мовлення (RMS, 16 біт)
    MAX_GAIN = 12.0

    def __init__(self) -> None:
        self.peak = 1500.0  # «пік» мовлення, що повільно згасає
        self.gain = 1.0

    def __call__(self, pcm16: bytes) -> bytes:
        a = array.array("h", pcm16[: len(pcm16) // 2 * 2])
        if not a:
            return pcm16
        if sys.byteorder == "big":
            a.byteswap()
        rms = math.sqrt(sum(x * x for x in a[::4]) / max(1, len(a[::4])))
        self.peak = max(rms, self.peak * 0.997, 1000.0)  # згасає за кілька секунд; тишу більш ніж ×4 не підсилюємо
        want = min(self.MAX_GAIN, max(1.0, self.TARGET / self.peak))
        self.gain += (want - self.gain) * (0.5 if want < self.gain else 0.05)  # вниз — швидко, вгору — плавно
        if self.gain <= 1.05:
            return pcm16
        g = self.gain
        out = array.array("h", (max(-32768, min(32767, int(x * g))) for x in a))
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes()


DEBUG = os.environ.get("KRU_WAKE_DEBUG", "").lower() not in {"", "0", "false", "no"}


def debug(msg: str) -> None:
    if DEBUG:
        print(f"[kru-wake {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def make_recognizer(model_path: Path, rate: int):
    """Vosk-розпізнавач, обмежений лише ім'ям (найточніше й найлегше); якщо модель не знає жодного
    варіанта імені — вільне розпізнавання, а ім'я шукаємо в тексті. → (model, recognizer, обмежений?)"""
    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(0 if DEBUG else -1)
    model = Model(str(model_path))
    known = [w for w in WAKE_WORDS if model.vosk_model_find_word(w) >= 0]  # слова, які модель знає
    debug(f"модель {model_path.name}, частота {rate} Гц, слова імені в словнику: {known or 'жодного'}")
    if known:
        # решта мовлення має кудись «падати», інакше модель підганяє будь-який звук під ім'я:
        # [unk] («щось інше»), а якщо маленька модель його не має — звичайні часті слова
        others = ["[unk]"] if model.vosk_model_find_word("[unk]") >= 0 else \
            [w for w in FILLER_WORDS if model.vosk_model_find_word(w) >= 0]
        debug(f"інше мовлення: {'[unk]' if others == ['[unk]'] else f'{len(others)} частих слів'}")
        # звертання перед ім'ям дають моделі контекст: «Привіт, Кру» чується надійніше за саме «Кру»
        phrases = [f"{a} {w}" for a in ("привіт", "агов", "гей", "слухай") for w in known[:2]
                   if model.vosk_model_find_word(a) >= 0]
        rec = KaldiRecognizer(model, rate, json.dumps(known + phrases + others, ensure_ascii=False))
    else:
        rec = KaldiRecognizer(model, rate)
    rec.SetWords(True)
    rec.SetMaxAlternatives(3)
    return model, rec, bool(known)


class WakeListener(QObject):
    """Мікрофон (потік інтерфейсу) + Vosk (робочий потік). Робочий потік не чіпає Qt: події складає
    в чергу, а інтерфейс забирає їх таймером і вже тоді випускає сигнали."""

    woke = Signal()
    speech = Signal()
    utterance = Signal(bytes, bool)
    timeout = Signal()
    failed = Signal(str)
    ready = Signal()  # модель завантажено — слухає

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source = None
        self._io = None
        self._fmt = ("Int16", 1, 16000)
        self._queue: queue.Queue = queue.Queue(maxsize=400)  # звук і команди → робочий потік
        self._events: queue.SimpleQueue = queue.SimpleQueue()  # події → потік інтерфейсу
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.logic: WakeLogic | None = None
        self._poll = QTimer(self, interval=40)
        self._poll.timeout.connect(self._drain)

    @property
    def active(self) -> bool:
        return self._source is not None

    @property
    def level(self) -> float:
        logic = self.logic
        return logic.level if logic else 0.0

    def start(self, model_path: Path | None, sensitivity: str = "medium", phrase_model: Path | None = None) -> bool:
        """Відкриває мікрофон одразу, а модель вантажить у робочому потоці (інтерфейс не завмирає);
        коли все готово — сигнал ready, якщо ні — failed. model_path=None — без Vosk, ім'я шукає сервер;
        phrase_model — тека своєї фрази («Чуєш, Кру», wakeword.py): шукає її сам ПК."""
        from .audio import MicError, open_input

        if self._source is not None:
            return True
        try:
            source, fmt = open_input(self)
        except MicError as e:
            self.failed.emit(str(e))
            return False
        io = source.start()
        if io is None:
            self.failed.emit("мікрофон зайнятий або доступ до нього заборонено")
            return False
        self._source, self._io, self._fmt = source, io, fmt
        self._resample = Resampler(fmt[2], RATE)  # Vosk і Whisper — 16 кГц
        debug(f"мікрофон: {fmt[0]}, каналів {fmt[1]}, {fmt[2]} Гц → {RATE} Гц")
        io.readyRead.connect(self._read)
        self._stop.clear()
        self._thread = threading.Thread(target=self._work, args=(model_path, sensitivity, phrase_model), name="kru-wake",
                                        daemon=True)
        self._thread.start()
        self._poll.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._poll.stop()
        if self._source is not None:
            self._source.stop()
            self._source.deleteLater()
        self._source = self._io = None
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        self.logic = None
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    # керування — з потоку інтерфейсу; логіку змінює лише робочий потік, тож передаємо командою
    def listen(self) -> None:
        self._command(("listen",))

    def await_command(self, seconds: float) -> None:
        self._command(("await", seconds))

    def pause(self) -> None:
        self._command(("pause",))

    def _command(self, cmd: tuple) -> None:
        if self._source is not None:
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
                self._queue.put_nowait(data)  # перетворення — у робочому потоці, не тут
            except queue.Full:  # робочий потік не встигає (чи ще вантажить модель) — пропускаємо шматок
                pass

    def _drain(self) -> None:
        """Потік інтерфейсу: події з робочого потоку → сигнали."""
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "failed":
                self.stop()
                self.failed.emit(ev[1])
                return
            if kind == "ready":
                self.ready.emit()
            elif kind == "wake":
                self.woke.emit()
            elif kind == "speech":
                self.speech.emit()
            elif kind == "utterance":
                self.utterance.emit(ev[1], ev[2])
            elif kind == "timeout":
                self.timeout.emit()

    def _work(self, model_path: Path | None, sensitivity: str, phrase_model: Path | None = None) -> None:
        rec, limited, detector = None, True, None
        if phrase_model is not None:
            try:
                from .wakeword import Detector

                detector = Detector(phrase_model, sensitivity)
            except Exception as e:  # noqa: BLE001 — немає onnxruntime, пошкоджена модель тощо
                self._events.put(("failed", f"не вдалося запустити розпізнавання фрази: {e}"))
                return
            debug(f"фразу шукає ПК (своя модель), поріг {detector.threshold:.2f}")
        elif model_path is not None:
            try:
                self._model, rec, limited = make_recognizer(model_path, RATE)
            except Exception as e:  # noqa: BLE001 — немає vosk, пошкоджена модель тощо
                self._events.put(("failed", f"не вдалося запустити розпізнавання імені: {e}"))
                return
        else:
            debug("ім'я шукає сервер (Whisper): кожна фраза — на сервер")
        logic = WakeLogic(RATE, rec, sensitivity)
        logic.detector = detector
        logic.loose = not limited
        logic.debug = debug if DEBUG else None
        self.logic = logic
        self._events.put(("ready",))
        last_report = time.monotonic()
        agc = Agc() if rec is not None else (lambda pcm: pcm)  # Whisper і своя модель тихий звук чують і так
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                item = b""
            if isinstance(item, tuple):
                if item[0] == "listen":
                    logic.listen()
                elif item[0] == "pause":
                    logic.pause()
                elif item[0] == "await":
                    logic.await_command(item[1])
                debug(f"команда {item[0]} → стан {logic.state}")
                continue
            try:
                if item:
                    item = agc(self._resample(to_mono16(item, self._fmt[0], self._fmt[1])))
                events = logic.feed(item)
            except Exception as e:  # noqa: BLE001
                self._events.put(("failed", f"розпізнавання імені: {e}"))
                return
            for ev in events:
                debug(f"подія {ev[0]}" + (f" ({len(ev[1])} байт, потрібне ім'я: {ev[2]})" if ev[0] == "utterance" else ""))
                self._events.put(ev)
            if DEBUG and time.monotonic() - last_report > 2:
                last_report = time.monotonic()
                debug(f"стан {logic.state}, рівень {logic.level:.2f}, шум {logic.floor:.0f}, мовлення {logic.speaking}, "
                      f"підсилення ×{getattr(agc, 'gain', 1.0):.1f}")
        self._model = None
