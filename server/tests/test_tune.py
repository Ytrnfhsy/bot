"""deploy/tune.py — підбір налаштувань моделі (логіка пошуку перевіряється на «фейковій відеокарті»)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
spec = importlib.util.spec_from_file_location("kru_tune", DEPLOY / "tune.py")
tune = importlib.util.module_from_spec(spec)
sys.modules["kru_tune"] = tune  # dataclasses шукають модуль за іменем
spec.loader.exec_module(tune)


class FakeGPU:
    """Модель вміщається, якщо на CPU лишилось достатньо експертів; більший контекст/ubatch — більше VRAM."""

    def __init__(self) -> None:
        self.runs: list[tuple[int, int, int]] = []

    @staticmethod
    def need(ctx: int, ub: int) -> int:
        return 20 + (ctx // 65536 - 1) * 2 + (ub // 512 - 1) * 2

    def run(self, t):
        self.runs.append((t.ctx, t.ubatch, t.ncm))
        spare = t.ncm - self.need(t.ctx, t.ubatch)
        if spare < 0:
            t.ok, t.reason = False, "не вмістилась у VRAM"
            return t
        t.ok = True
        t.free_mb = 300 + spare * 400
        t.tg = 40 - t.ncm * 0.5  # більше експертів на GPU — швидше
        t.pp = 300 + t.ubatch * 0.5 - t.ncm * 3
        return t


def test_find_min_ncm_goes_down_until_headroom_runs_out():
    gpu = FakeGPU()
    best, trials = tune.find_min_ncm(gpu, 65536, 512, start=26, headroom_mb=700, log=lambda _: None)
    # need=20: 22 → вільно 1100, 20 → 300 (< 700, не підходить)
    assert best.ncm == 22 and [t.ncm for t in trials] == [26, 24, 22, 20]
    assert "запас VRAM" in trials[-1].reason


def test_find_min_ncm_goes_up_when_start_does_not_fit():
    gpu = FakeGPU()
    best, trials = tune.find_min_ncm(gpu, 131072, 1024, start=20, headroom_mb=700, log=lambda _: None)
    # need=24: 20 ✗, 22 ✗, 24 → запас 300 ✗, 26 ✓
    assert best.ncm == 26 and [t.ncm for t in trials] == [20, 22, 24, 26]


def test_tune_prefers_bigger_context_if_almost_as_fast():
    gpu = FakeGPU()
    best, trials = tune.tune(gpu, [65536, 131072], [512, 1024], start_ncm=26, headroom_mb=700, log=lambda _: None)
    assert best is not None and best.ok
    fastest = min(t.step_s for t in trials if t.ok)
    assert best.step_s <= fastest * 1.10
    assert best.ctx == 131072  # більший контекст майже не повільніший — беремо його


def test_choose_takes_fastest_when_big_context_is_much_slower():
    small = tune.Trial(65536, 512, 20, ok=True, pp=800, tg=40)
    big = tune.Trial(131072, 512, 30, ok=True, pp=300, tg=20)
    assert tune.choose([small, big]) is small
    assert tune.choose([tune.Trial(65536, 512, 20, ok=False)]) is None


def test_write_env_keeps_other_settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MODEL_DIR=/data\nN_CPU_MOE=30\n")
    tune.write_env(tune.Trial(131072, 1024, 24, ok=True, pp=700, tg=30, free_mb=900), env)
    text = env.read_text()
    assert "MODEL_DIR=/data" in text and "N_CPU_MOE=24" in text and "N_CPU_MOE=30" not in text
    assert "CTX_SIZE=131072" in text and "UBATCH=1024" in text


def test_runner_detects_out_of_memory(monkeypatch):
    r = tune.Runner(timeout=5)
    monkeypatch.setattr(r, "_compose", lambda *a, **k: None)
    monkeypatch.setattr(r, "_inspect", lambda: ("restarting", 1))
    monkeypatch.setattr(r, "_logs", lambda: "ggml_backend_cuda_buffer_type_alloc_buffer: allocating ... cudaMalloc failed: out of memory")
    t = r.run(tune.Trial(131072, 2048, 18))
    assert not t.ok and t.reason == "не вмістилась у VRAM"


def test_runner_bench_reads_llama_timings(monkeypatch):
    r = tune.Runner(timeout=5)
    monkeypatch.setattr(r, "_compose", lambda *a, **k: None)
    monkeypatch.setattr(r, "_inspect", lambda: ("running", 0))
    calls = []

    def http(path, payload=None, timeout=600):
        calls.append(path)
        if path == "/health":
            return {"status": "ok"}
        return {"timings": {"prompt_per_second": 812.5, "predicted_per_second": 31.2}}

    monkeypatch.setattr(r, "_http", http)
    monkeypatch.setattr(r, "free_vram_mb", lambda: 1234)
    t = r.run(tune.Trial(65536, 512, 24))
    assert t.ok and (t.pp, t.tg, t.free_mb) == (812.5, 31.2, 1234)
    assert calls == ["/health", "/completion", "/completion"]
    assert round(t.step_s, 1) == round(4000 / 812.5 + 300 / 31.2, 1)
