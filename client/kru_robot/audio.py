"""Голос на боці персонажа: запис мікрофона у WAV і відтворення озвученої відповіді.

Розпізнавання й озвучення робить сервер (faster-whisper / Piper) — тут лише звук туди й назад.
QtMultimedia імпортується ліниво: без звукової підсистеми (чи з урізаним Qt) решта працює як раніше.
"""

from __future__ import annotations

import array
import io
import math
import os
import sys
import tempfile
import wave

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

MAX_SECONDS = 60  # довше не слухаємо — запис зупиняється сам
MIN_SECONDS = 0.35  # коротше — випадкове натискання, не надсилаємо


def to_mono16(raw: bytes, sample_format: str, channels: int) -> bytes:
    """Сирі відліки з мікрофона → 16-біт моно (Whisper однаково перетворить у моно 16 кГц)."""
    if sample_format == "Int16":
        a = array.array("h", raw[: len(raw) // 2 * 2])
    elif sample_format == "Float":
        f = array.array("f", raw[: len(raw) // 4 * 4])
        a = array.array("h", (max(-32768, min(32767, int(x * 32767))) for x in f))
    elif sample_format == "Int32":
        i = array.array("i", raw[: len(raw) // 4 * 4])
        a = array.array("h", (x >> 16 for x in i))
    elif sample_format == "UInt8":
        a = array.array("h", ((b - 128) << 8 for b in raw))
    else:
        raise ValueError(f"формат звуку {sample_format} не підтримується")
    if sys.byteorder == "big":
        a.byteswap()
    if channels > 1:
        a = a[::channels]  # перший канал — для мовлення досить
    return a.tobytes()


def make_wav(pcm16: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    return buf.getvalue()


def peak(pcm16: bytes) -> float:
    """Гучність шматка запису 0..1 (для анімації «слухаю»)."""
    a = array.array("h", pcm16[: len(pcm16) // 2 * 2])
    if not a:
        return 0.0
    step = max(1, len(a) // 400)
    top = max(abs(x) for x in a[::step])
    return min(1.0, math.sqrt(top / 32768) * 1.25)


def envelope(wav_bytes: bytes, frame_ms: int = 40) -> tuple[list[float], int]:
    """Гучність WAV по кадрах (0..1) — щоб рот і вуха рухались у такт голосу. → (рівні, мс на кадр)."""
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            if w.getsampwidth() != 2:
                return [], frame_ms
            rate, ch = w.getframerate(), w.getnchannels()
            a = array.array("h", w.readframes(w.getnframes()))
    except (wave.Error, EOFError):
        return [], frame_ms
    if sys.byteorder == "big":
        a.byteswap()
    if ch > 1:
        a = a[::ch]
    n = max(1, rate * frame_ms // 1000)
    levels = []
    for i in range(0, len(a), n):
        chunk = a[i:i + n:4]
        rms = math.sqrt(sum(x * x for x in chunk) / len(chunk)) if chunk else 0.0
        levels.append(rms)
    top = max(levels, default=0.0) or 1.0
    return [min(1.0, (v / top) ** 0.7) for v in levels], frame_ms


class MicError(Exception):
    pass


def open_input(parent: QObject | None = None, rate: int = 16000):
    """QAudioSource для мікрофона за замовчуванням: 16 кГц моно 16 біт, якщо пристрій уміє, інакше — його рідний
    формат (відліки потім зводяться до to_mono16). → (source, (формат, канали, частота)); MicError — якщо ніяк."""
    try:
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
    except ImportError as e:
        raise MicError("у цій збірці Qt немає QtMultimedia") from e
    dev = QMediaDevices.defaultAudioInput()
    if dev.isNull():
        raise MicError("мікрофон не знайдено")
    fmt = QAudioFormat()
    fmt.setSampleRate(rate)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    if not dev.isFormatSupported(fmt):
        fmt = dev.preferredFormat()
    name = fmt.sampleFormat().name
    if name not in {"Int16", "Float", "Int32", "UInt8"}:
        raise MicError(f"мікрофон дає звук у форматі {name}, який не підтримується")
    return QAudioSource(dev, fmt, parent), (name, max(1, fmt.channelCount()), fmt.sampleRate())


class Recorder(QObject):
    """Запис з мікрофона за замовчуванням: start() … stop() → finished(wav) або failed(причина)."""

    finished = Signal(bytes)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source = None
        self._io = None
        self._chunks: list[bytes] = []
        self._fmt = ("Int16", 1, 16000)
        self.level = 0.0
        self._limit = QTimer(self, singleShot=True, interval=MAX_SECONDS * 1000)
        self._limit.timeout.connect(self.stop)

    @property
    def active(self) -> bool:
        return self._source is not None

    def start(self) -> bool:
        if self._source is not None:
            return True
        try:
            self._source, self._fmt = open_input(self)
        except MicError as e:
            self.failed.emit(str(e))
            return False
        self._chunks = []
        self.level = 0.0
        self._io = self._source.start()
        if self._io is None:
            self._source = None
            self.failed.emit("мікрофон зайнятий або доступ до нього заборонено")
            return False
        self._io.readyRead.connect(self._read)
        self._limit.start()
        return True

    def _read(self) -> None:
        if self._io is None:
            return
        data = bytes(self._io.readAll().data())
        if data:
            pcm = to_mono16(data, self._fmt[0], self._fmt[1])
            self._chunks.append(pcm)
            self.level = peak(pcm)

    def stop(self, send: bool = True) -> None:
        if self._source is None:
            return
        self._limit.stop()
        self._read()
        self._source.stop()
        self._source.deleteLater()
        self._source = self._io = None
        self.level = 0.0
        pcm = b"".join(self._chunks)
        self._chunks = []
        if not send:
            return
        rate = self._fmt[2]
        if len(pcm) < MIN_SECONDS * rate * 2:
            self.failed.emit("закороткий запис — утримуй персонажа, поки говориш")
            return
        self.finished.emit(make_wav(pcm, rate))


class Player(QObject):
    """Відтворення WAV відповіді; level — поточна гучність (для рота й вух персонажа)."""

    started = Signal()
    stopped = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player = None
        self._output = None
        self._levels: list[float] = []
        self._frame_ms = 40
        self._file = os.path.join(tempfile.gettempdir(), f"kru-speech-{os.getpid()}-{id(self)}.wav")
        self.playing = False

    @property
    def level(self) -> float:
        if not self.playing or self._player is None or not self._levels:
            return 0.0
        i = int(self._player.position() // self._frame_ms)
        return self._levels[i] if 0 <= i < len(self._levels) else 0.0

    def play(self, wav_bytes: bytes, volume: float = 1.0) -> bool:
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except ImportError:
            return False
        self.stop()
        try:
            with open(self._file, "wb") as f:
                f.write(wav_bytes)
        except OSError:
            return False
        self._levels, self._frame_ms = envelope(wav_bytes)
        if self._player is None:
            self._player = QMediaPlayer(self)
            self._output = QAudioOutput(self)
            self._player.setAudioOutput(self._output)
            self._player.playbackStateChanged.connect(self._on_state)
            self._player.errorOccurred.connect(lambda *_: self._finish())
        self._output.setVolume(volume)
        self._player.setSource(QUrl())  # той самий файл з новим вмістом — перечитати
        self._player.setSource(QUrl.fromLocalFile(self._file))
        self._player.play()
        self.playing = True
        self.started.emit()
        return True

    def _on_state(self, state) -> None:
        if state.name == "StoppedState" and self.playing:
            self._finish()

    def _finish(self) -> None:
        if self.playing:
            self.playing = False
            self.stopped.emit()

    def stop(self) -> None:
        if self._player is not None and self.playing:
            self._player.stop()
        self._finish()
