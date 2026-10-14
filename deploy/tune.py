#!/usr/bin/env python3
"""Підбір налаштувань Qwen3.6 під свою відеокарту: --n-cpu-moe, контекст, розмір пакета (ubatch).

Для кожного варіанта модель перезапускається, і вимірюється «крок агента»: прочитати ~4000 токенів
(інструменти повертають багато тексту) і написати ~300. Найкращий варіант, що лишає запас VRAM,
записується в deploy/qwen36/.env, після чого модель перезапускається з ним (deploy/model.sh start).

  deploy/tune.sh                         — типово: контекст 64K і 128K, ubatch 512 і 1024
  deploy/tune.sh --ctx 65536,98304,131072 --ubatch 512,1024,2048
  deploy/tune.sh --dry-run               — лише показати план

Лише стандартна бібліотека Python. Триває 10–30 хв (модель щоразу завантажується заново).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPOSE = HERE / "qwen36" / "docker-compose.yml"
ENV_FILE = HERE / "qwen36" / ".env"
REPORT = HERE / "qwen36" / "tune-report.txt"
URL = "http://127.0.0.1:8085"
CONTAINER = "qwen36"

PROMPT_TOKENS = 4000  # скільки читає агент за крок (результати інструментів, історія)
GEN_TOKENS = 300  # скільки пише
OOM_MARKERS = ("out of memory", "failed to allocate", "cudamalloc failed", "unable to allocate", "error loading model")


@dataclass
class Trial:
    ctx: int
    ubatch: int
    ncm: int
    ok: bool = False
    reason: str = ""
    pp: float = 0.0  # токенів/с — читання промпту
    tg: float = 0.0  # токенів/с — генерація
    free_mb: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def step_s(self) -> float:
        """Час одного кроку агента, с (менше — краще)."""
        if not self.ok or self.pp <= 0 or self.tg <= 0:
            return float("inf")
        return PROMPT_TOKENS / self.pp + GEN_TOKENS / self.tg

    def row(self) -> str:
        if not self.ok:
            return f"{self.ctx // 1024:>5}K {self.ubatch:>6} {self.ncm:>5}   —        —        —       ✗ {self.reason}"
        return (f"{self.ctx // 1024:>5}K {self.ubatch:>6} {self.ncm:>5} {self.pp:>8.0f} {self.tg:>8.1f} "
                f"{self.step_s:>7.1f}с  вільно {self.free_mb} МБ")


HEADER = f"{'ктх':>6} {'ubatch':>6} {'n-cpu':>5} {'читання':>8} {'генер.':>8} {'крок':>8}  VRAM  (т/с — токенів за секунду)"


# ---------------- робота з моделлю ----------------
class Runner:
    """Запускає модель із заданими налаштуваннями й міряє швидкість (реальний docker/GPU)."""

    def __init__(self, timeout: int = 600) -> None:
        self.timeout = timeout

    def _compose(self, *args: str, env: dict[str, str] | None = None) -> None:
        subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], check=True,
                       env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    def _inspect(self) -> tuple[str, int]:
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}} {{.RestartCount}}", CONTAINER],
                             capture_output=True, text=True)
        parts = out.stdout.split()
        return (parts[0], int(parts[1])) if len(parts) == 2 else ("missing", 0)

    def _logs(self) -> str:
        out = subprocess.run(["docker", "logs", "--tail", "80", CONTAINER], capture_output=True, text=True)
        return (out.stdout + out.stderr).lower()

    @staticmethod
    def _http(path: str, payload: dict | None = None, timeout: float = 600) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(URL + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    @staticmethod
    def free_vram_mb() -> int:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True)
        try:
            total, used = (int(x) for x in out.stdout.splitlines()[0].split(","))
            return total - used
        except (ValueError, IndexError):
            return -1

    def start(self, t: Trial) -> str:
        """'' — запустилась; інакше причина невдачі."""
        env = {"N_CPU_MOE": str(t.ncm), "CTX_SIZE": str(t.ctx), "UBATCH": str(t.ubatch)}
        self._compose("up", "-d", "--force-recreate", "qwen36", env=env)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            status, restarts = self._inspect()
            if restarts > 0 or status in {"exited", "dead", "restarting"}:
                logs = self._logs()
                return "не вмістилась у VRAM" if any(m in logs for m in OOM_MARKERS) else f"не запустилась ({status})"
            try:
                if self._http("/health", timeout=5).get("status") == "ok":
                    return ""
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(3)
        return "не запустилась за відведений час"

    def bench(self, t: Trial) -> None:
        filler = ("The agent reads tool results, web pages and files, then writes a clear answer. " * 600)
        self._http("/completion", {"prompt": "Hello", "n_predict": 8, "cache_prompt": False})  # прогрів
        res = self._http("/completion", {
            "prompt": filler[: PROMPT_TOKENS * 4] + "\nSummarize the text above in detail:",
            "n_predict": GEN_TOKENS, "cache_prompt": False, "temperature": 0.6, "ignore_eos": True,
        })
        timings = res.get("timings") or {}
        t.pp = float(timings.get("prompt_per_second") or 0)
        t.tg = float(timings.get("predicted_per_second") or 0)
        t.free_mb = self.free_vram_mb()

    def run(self, t: Trial) -> Trial:
        why = self.start(t)
        if why:
            t.ok, t.reason = False, why
            return t
        try:
            self.bench(t)
            t.ok = True
        except (urllib.error.URLError, OSError, ValueError) as e:
            t.ok, t.reason = False, f"впала під час тесту ({e})"
        return t


# ---------------- пошук ----------------
def find_min_ncm(runner, ctx: int, ubatch: int, start: int, headroom_mb: int, log,
                 lo: int = 0, hi: int = 48, step: int = 2) -> tuple[Trial | None, list[Trial]]:
    """Найменший --n-cpu-moe (найбільше експертів на GPU), з яким модель стартує й лишає запас VRAM.

    Іде від start униз, поки виходить; якщо start не вміщається — угору, поки не запуститься.
    """
    trials: list[Trial] = []

    def attempt(n: int) -> Trial:
        t = runner.run(Trial(ctx, ubatch, n))
        if t.ok and 0 <= t.free_mb < headroom_mb:
            t.ok, t.reason = False, f"запас VRAM лише {t.free_mb} МБ (< {headroom_mb})"
        trials.append(t)
        log(t.row())
        return t

    first = attempt(start)
    best = first if first.ok else None
    n = start
    if best:
        while n - step >= lo:
            t = attempt(n - step)
            if not t.ok:
                break
            best, n = t, n - step
    else:
        while n + step <= hi:
            n += step
            t = attempt(n)
            if t.ok:
                best = t
                break
    return best, trials


def choose(results: list[Trial], prefer_ctx_within: float = 0.10) -> Trial | None:
    """Найшвидший крок; але більший контекст виграє, якщо повільніший не більше ніж на 10%."""
    ok = [t for t in results if t.ok]
    if not ok:
        return None
    fastest = min(t.step_s for t in ok)
    good = [t for t in ok if t.step_s <= fastest * (1 + prefer_ctx_within)]
    return max(good, key=lambda t: (t.ctx, -t.step_s))


def tune(runner, ctxs: list[int], ubatches: list[int], start_ncm: int, headroom_mb: int, log) -> tuple[Trial | None, list[Trial]]:
    all_trials: list[Trial] = []
    winners: list[Trial] = []
    for ctx in ctxs:
        start = start_ncm
        for ub in ubatches:
            log(f"\n▶ контекст {ctx // 1024}K, ubatch {ub}")
            best, trials = find_min_ncm(runner, ctx, ub, start, headroom_mb, log)
            all_trials += trials
            if best:
                winners.append(best)
                start = best.ncm  # більший ubatch потребує не менше VRAM — шукаємо від знайденого
    return choose(winners), all_trials


# ---------------- застосування ----------------
def write_env(best: Trial, path: Path = ENV_FILE) -> None:
    keep = []
    if path.is_file():
        keep = [ln for ln in path.read_text().splitlines()
                if ln.split("=", 1)[0].strip() not in {"N_CPU_MOE", "CTX_SIZE", "UBATCH"}]
    lines = keep + [
        f"# підібрано deploy/tune.sh {time.strftime('%Y-%m-%d %H:%M')}: "
        f"читання {best.pp:.0f} т/с, генерація {best.tg:.1f} т/с, вільно {best.free_mb} МБ VRAM",
        f"N_CPU_MOE={best.ncm}", f"CTX_SIZE={best.ctx}", f"UBATCH={best.ubatch}",
    ]
    path.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Підбір --n-cpu-moe / контексту / ubatch для Qwen3.6")
    ap.add_argument("--ctx", default="65536,131072", help="контексти через кому (токени)")
    ap.add_argument("--ubatch", default="512,1024", help="розміри пакета через кому")
    ap.add_argument("--headroom", type=int, default=700, help="скільки МБ VRAM лишити вільними (картинки, піки)")
    ap.add_argument("--start", type=int, default=0, help="з якого --n-cpu-moe почати (0 — 26, із зором 30)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-apply", action="store_true", help="не записувати результат і не перезапускати модель")
    args = ap.parse_args(argv)

    ctxs = [int(x) for x in args.ctx.split(",") if x.strip()]
    ubatches = [int(x) for x in args.ubatch.split(",") if x.strip()]
    model_dir = Path(os.environ.get("MODEL_DIR", HERE.parent))
    vision = (model_dir / os.environ.get("MMPROJ_FILE", "mmproj-Qwen3.6-35B-A3B-F16.gguf")).is_file()
    start = args.start or (30 if vision else 26)
    print(f"Зір: {'увімкнено' if vision else 'вимкнено'} · контексти: {', '.join(f'{c // 1024}K' for c in ctxs)} · "
          f"ubatch: {', '.join(map(str, ubatches))} · запас VRAM: {args.headroom} МБ · старт n-cpu-moe {start}")
    if args.dry_run:
        return 0
    os.environ.setdefault("MODEL_DIR", str(model_dir))

    lines: list[str] = [HEADER]

    def log(line: str) -> None:
        print(line, flush=True)
        lines.append(line)

    print("Модель перезапускатиметься кілька разів — агент тим часом не відповідатиме.\n" + HEADER)
    best, _ = tune(Runner(), ctxs, ubatches, start, args.headroom, log)
    if best is None:
        print("\n✗ Жоден варіант не вмістився. Спробуй більший --start або менший контекст.")
        return 1
    summary = (f"\n✓ Найкраще: контекст {best.ctx // 1024}K, ubatch {best.ubatch}, --n-cpu-moe {best.ncm} — "
               f"крок агента ~{best.step_s:.1f} с (читання {best.pp:.0f} т/с, генерація {best.tg:.1f} т/с)")
    log(summary)
    REPORT.write_text("\n".join(lines) + "\n")
    if args.no_apply:
        return 0
    write_env(best)
    print(f"Записано в {ENV_FILE}. Перезапускаю модель з цими налаштуваннями…")
    return subprocess.call([str(HERE / "model.sh"), "start"])


if __name__ == "__main__":
    sys.exit(main())
