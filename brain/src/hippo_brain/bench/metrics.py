"""Background sampler for per-model system metrics.

Tracks RSS / CPU of the inference-backend process. Discovery is currently
LM-Studio-specific (process-name match) — see `_LMSTUDIO_NAME_HINTS`. For
other OpenAI-compatible backends (oMLX, ollama, vLLM) the discovery returns
None and the per-process metrics stay at 0; host-wide metrics (load average,
free memory) still populate. Adding probes for other backends is a follow-up.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import psutil


@dataclass
class Snapshot:
    monotonic_ns: int
    inference_rss_mb: float
    inference_cpu_pct: float
    load_avg_1m: float  # os.getloadavg()[0] — 1-minute kernel load average
    mem_free_mb: float  # vm.available, not vm.free — kernel's user-accessible memory


# Process-name substrings that identify the LM Studio inference server.
# This is the only LM-Studio-specific bit left in the module; other backends
# (oMLX, ollama, vLLM) would need their own hints to be discovered by name.
_LMSTUDIO_NAME_HINTS = ("lm studio", "lmstudio", "lms")


class MetricsSampler:
    def __init__(self, sample_interval_ms: int = 250):
        self.sample_interval_ms = sample_interval_ms
        self._samples: list[Snapshot] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pid: int | None = None

    @staticmethod
    def _discover_inference_pid() -> int | None:
        """Find the PID of the inference backend.

        Currently LM-Studio-specific — matches `_LMSTUDIO_NAME_HINTS` against
        the process name list. Returns None on non-LM-Studio backends, which
        leaves the per-process Snapshot fields at 0.
        """
        best: int | None = None
        best_rss = 0
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info.get("name") or "").lower()
            except psutil.Error:
                continue
            if any(hint in name for hint in _LMSTUDIO_NAME_HINTS):
                try:
                    rss = proc.memory_info().rss
                except psutil.Error:
                    continue
                if rss > best_rss:
                    best_rss = rss
                    best = proc.info["pid"]
        return best

    def _sample_once(self, proc: psutil.Process | None) -> Snapshot:
        rss_mb = 0.0
        cpu_pct = 0.0
        if proc is not None:
            try:
                rss_mb = proc.memory_info().rss / (1024 * 1024)
                cpu_pct = proc.cpu_percent(interval=None)
            except psutil.Error:
                pass
        load_1, _, _ = os.getloadavg()
        vm = psutil.virtual_memory()
        return Snapshot(
            monotonic_ns=time.monotonic_ns(),
            inference_rss_mb=rss_mb,
            inference_cpu_pct=cpu_pct,
            load_avg_1m=load_1,
            mem_free_mb=vm.available / (1024 * 1024),
        )

    def _run_loop(self) -> None:
        proc: psutil.Process | None = None
        if self._pid is not None:
            try:
                proc = psutil.Process(self._pid)
                proc.cpu_percent(interval=None)  # prime
            except psutil.Error:
                proc = None
        interval = self.sample_interval_ms / 1000.0
        while not self._stop.is_set():
            self._samples.append(self._sample_once(proc))
            self._stop.wait(interval)

    def start(self) -> None:
        self._pid = self._discover_inference_pid()
        self._stop.clear()
        self._samples = []
        self._thread = threading.Thread(target=self._run_loop, name="bench-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def latest(self) -> Snapshot | None:
        return self._samples[-1] if self._samples else None

    def peak(self) -> dict[str, float]:
        if not self._samples:
            return {
                "inference_rss_mb": 0.0,
                "inference_cpu_pct": 0.0,
                "load_avg_1m": 0.0,
                "mem_free_mb": 0.0,
            }
        return {
            "inference_rss_mb": max(s.inference_rss_mb for s in self._samples),
            "inference_cpu_pct": max(s.inference_cpu_pct for s in self._samples),
            "load_avg_1m": max(s.load_avg_1m for s in self._samples),
            "mem_free_mb": min(s.mem_free_mb for s in self._samples),
        }
