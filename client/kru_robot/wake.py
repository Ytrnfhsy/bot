"""Режим «завжди слухаю»: робот чекає на фразу «Чуєш, Кру» і тоді слухає питання.

Як це влаштовано:
  1. Мікрофон працює постійно. Фразу «Чуєш, Кру» шукає маленька модель тут, на ПК (openWakeWord,
     wakeword.py) — звук нікуди не йде, поки фразу не почуто.
  2. Почувши фразу, робот записує її до кінця (до паузи). Останні секунди звуку весь час
     тримаються в пам'яті, тож «Чуєш, Кру, яка сьогодні погода?» можна сказати одним духом.
  3. Запис іде на сервер — Whisper розпізнає, звертання прибирається, решта — задача.
  4. Після відповіді робот ще кілька секунд слухає без фрази — для уточнення («а завтра?»).

WakeLogic — сама логіка (без Qt, перевіряється в самоперевірці), WakeListener — мікрофон + робочий потік.
"""

from __future__ import annotations

import array
import math
import os
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from .audio import Resampler, make_wav, to_mono16

RATE = 16000  # частота, на якій працюють модель фрази і Whisper
FRAME_MS = 30  # аналізуємо звук шматками по 30 мс
PRE_ROLL = 0.35  # с звуку перед початком мовлення, що додається до запису (не обрізати перший склад)
BUFFER_SEC = 20  # стільки останніх секунд тримаємо в пам'яті
END_SILENCE = 1.2  # пауза, після якої фраза вважається завершеною (паузи між словами коротші)
MAX_COMMAND = 15.0  # найдовша фраза після імені
MIN_FOLLOWUP = 0.5  # с — уточнення без імені коротше за це вважаємо шумом і слухаємо далі
WAKE_WAIT = 5.0  # с після «Чуєш, Кру» чекаємо, поки почнеться саме питання (людина часто робить паузу)
MIN_COMMAND = 0.5  # с мовлення після фрази — це вже питання, а не хвіст «…Кру»
FLOOR_WINDOW = 1.2  # с: найтихіше за цей час — рівень фону (вентилятор, телевізор), навіть посеред фрази


MIN_COMMAND_FRAMES = int(MIN_COMMAND * 1000 / FRAME_MS)


class WakeLogic:
    """Стан слухання. feed(pcm16) → список подій:
    ("wake",)                 — почуто фразу, почався запис фрази;
    ("speech",)               — у режимі очікування уточнення почалося мовлення;
    ("utterance", wav, need)  — фраза записана (need — у ній є звертання, сервер його прибере);
    ("timeout",)              — уточнення (чи питання після фрази) так і не прозвучало.
    Після "utterance" логіка чекає, поки застосунок викличе listen() чи await_command()."""

    def __init__(self, rate: int, detector=None) -> None:
        self.rate = rate
        self.detector = detector  # своя фраза («Чуєш, Кру», wakeword.Detector)
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
        self.voiced = 0  # кадрів мовлення після фрази-активатора
        self._quiet: deque[float] = deque(maxlen=int(FLOOR_WINDOW * 1000 / FRAME_MS))
        self.deadline = 0  # кадр, до якого чекаємо на уточнення
        self.level = 0.0
        self.debug = None  # функція для діагностики (KRU_WAKE_DEBUG=1)

    # ---------------- керування ----------------
    def listen(self) -> None:
        """Чекати на фразу."""
        self.state = "idle"
        self._pending = b""
        self._reset_rec()

    def await_command(self, seconds: float) -> None:
        """Слухати без фрази (уточнення після відповіді або «Чуєш, Кру…» з паузою)."""
        self.state = "await"
        self.deadline = self.n + int(seconds * 1000 / FRAME_MS)
        self.speaking, self.silence = False, 0  # чекаємо нового мовлення, а не хвіст попереднього
        self._pending = b""
        self._reset_rec()

    def pause(self) -> None:
        self.state = "paused"

    def _reset_rec(self) -> None:
        if self.detector is not None:
            self.detector.reset()  # інакше щойно почута фраза спрацює ще раз

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
        # фон, що з'явився чи посилився посеред фрази (вентилятор, телевізор), інакше «тягне» запис до 15 с:
        # найтихіший кадр за останню секунду — і є фон (у мовленні між складами завжди є тихіші місця)
        self._quiet.append(rms)
        if len(self._quiet) == self._quiet.maxlen and (low := min(self._quiet)) > self.floor * 1.3:
            self.floor = 0.95 * self.floor + 0.05 * low
        events: list[tuple] = []
        was_speaking = self.speaking
        if loud:
            self.silence = 0
            if self.state == "capture":
                self.voiced += 1
            if not self.speaking:
                self.speaking = True
                self.speech_start = self.n
                if self.state == "await":
                    self.state = "capture"
                    self.need_wake = False
                    self.capture_from = self._preroll(self.n)
                    self.capture_started = self.n
                    self.voiced = MIN_COMMAND_FRAMES  # уточнення — уже саме питання
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
                self.voiced = 0
                events.append(("wake",))
        elif self.state == "paused" or self.state == "busy":
            return events

        if self.state == "capture":
            elapsed = (self.n - self.capture_started) * FRAME_MS / 1000
            long_enough = elapsed >= MAX_COMMAND
            if self.need_wake and self.voiced < MIN_COMMAND_FRAMES and not long_enough:
                # «Чуєш, Кру» і пауза: питання ще попереду — не відправляємо саму фразу, а чекаємо на нього
                if elapsed < WAKE_WAIT:
                    return events
                if self.debug:
                    self.debug(f"після фрази {WAKE_WAIT:.0f} с тиші — питання так і не прозвучало")
                self.state = "busy"
                events.append(("timeout",))
                return events
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
                if self.debug:
                    why = "задовга фраза" if long_enough else f"пауза {END_SILENCE} с"
                    self.debug(f"фраза записана: {elapsed:.1f} с ({why}); фон {self.floor:.0f}, "
                               f"тиша — нижче {max(self.floor * 1.6, 250.0):.0f}")
                events.append(("utterance", self._audio(self.capture_from), self.need_wake))
                self.state = "busy"
        return events

    def _recognize(self, frame: bytes) -> bool:
        if self.detector is not None:
            score = self.detector.feed(frame)
            if self.debug and score >= 0.2:
                self.debug(f"схоже на фразу: {score:.2f} (поріг {self.detector.threshold:.2f})")
            return score >= self.detector.threshold
        return False

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


DEBUG = os.environ.get("KRU_WAKE_DEBUG", "").lower() not in {"", "0", "false", "no"}


def debug(msg: str) -> None:
    if DEBUG:
        print(f"[kru-wake {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class WakeListener(QObject):
    """Мікрофон (потік інтерфейсу) + модель фрази (робочий потік). Робочий потік не чіпає Qt: події складає
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

    def start(self, phrase_model: Path, sensitivity: str = "medium") -> bool:
        """Відкриває мікрофон одразу, а модель фрази (тека з wakeword.py) вантажить у робочому потоці
        (інтерфейс не завмирає); коли все готово — сигнал ready, якщо ні — failed."""
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
        self._resample = Resampler(fmt[2], RATE)  # модель фрази і Whisper — 16 кГц
        debug(f"мікрофон: {fmt[0]}, каналів {fmt[1]}, {fmt[2]} Гц → {RATE} Гц")
        io.readyRead.connect(self._read)
        self._stop.clear()
        self._thread = threading.Thread(target=self._work, args=(phrase_model, sensitivity), name="kru-wake",
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

    def _work(self, phrase_model: Path, sensitivity: str) -> None:
        try:
            from .wakeword import Detector

            detector = Detector(phrase_model, sensitivity)
        except Exception as e:  # noqa: BLE001 — немає onnxruntime, пошкоджена модель тощо
            self._events.put(("failed", f"не вдалося запустити розпізнавання фрази: {e}"))
            return
        debug(f"фразу шукає ПК (своя модель), поріг {detector.threshold:.2f}")
        logic = WakeLogic(RATE, detector)
        logic.debug = debug if DEBUG else None
        self.logic = logic
        self._events.put(("ready",))
        last_report = time.monotonic()
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
                    item = self._resample(to_mono16(item, self._fmt[0], self._fmt[1]))
                events = logic.feed(item)
            except Exception as e:  # noqa: BLE001
                self._events.put(("failed", f"розпізнавання імені: {e}"))
                return
            for ev in events:
                debug(f"подія {ev[0]}" + (f" ({len(ev[1])} байт, з фразою: {ev[2]})" if ev[0] == "utterance" else ""))
                self._events.put(ev)
            if DEBUG and time.monotonic() - last_report > 2:
                last_report = time.monotonic()
                debug(f"стан {logic.state}, рівень {logic.level:.2f}, шум {logic.floor:.0f}, мовлення {logic.speaking}")
