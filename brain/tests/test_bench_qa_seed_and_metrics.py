"""Tests for bench.qa_seed (fixture seeding) and bench.metrics (system sampler)."""

from __future__ import annotations

import time

from hippo_brain.bench.metrics import MetricsSampler, Snapshot
from hippo_brain.bench.qa_seed import seed_qa_fixture


class TestSeedQaFixture:
    def test_seeds_template_into_fixtures_dir(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        count = seed_qa_fixture()

        dest = tmp_path / "hippo-bench" / "fixtures" / "eval-qa-v1.jsonl"
        assert dest.exists()
        lines = [line for line in dest.read_text().splitlines() if line.strip()]
        assert count == len(lines) > 0
        assert f"Seeded {count} " in capsys.readouterr().out

    def test_reseed_overwrites_existing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        dest_dir = tmp_path / "hippo-bench" / "fixtures"
        dest_dir.mkdir(parents=True)
        (dest_dir / "eval-qa-v1.jsonl").write_text("stale content\n")

        count = seed_qa_fixture()
        content = (dest_dir / "eval-qa-v1.jsonl").read_text()
        assert "stale content" not in content
        assert count > 1


class TestMetricsSampler:
    def test_sample_once_without_process_populates_host_metrics(self):
        sampler = MetricsSampler()
        snap = sampler._sample_once(None)
        assert isinstance(snap, Snapshot)
        assert snap.inference_rss_mb == 0.0
        assert snap.inference_cpu_pct == 0.0
        assert snap.mem_free_mb > 0.0
        assert snap.monotonic_ns > 0

    def test_latest_and_peak_empty(self):
        sampler = MetricsSampler()
        assert sampler.latest() is None
        peak = sampler.peak()
        assert peak == {
            "inference_rss_mb": 0.0,
            "inference_cpu_pct": 0.0,
            "load_avg_1m": 0.0,
            "mem_free_mb": 0.0,
        }

    def test_peak_aggregates_max_and_min_free(self):
        sampler = MetricsSampler()
        sampler._samples = [
            Snapshot(
                1,
                inference_rss_mb=100.0,
                inference_cpu_pct=10.0,
                load_avg_1m=1.0,
                mem_free_mb=8000.0,
            ),
            Snapshot(
                2,
                inference_rss_mb=300.0,
                inference_cpu_pct=5.0,
                load_avg_1m=2.5,
                mem_free_mb=4000.0,
            ),
        ]
        peak = sampler.peak()
        assert peak["inference_rss_mb"] == 300.0
        assert peak["inference_cpu_pct"] == 10.0
        assert peak["load_avg_1m"] == 2.5
        # mem_free is a min — the low-water mark is what matters.
        assert peak["mem_free_mb"] == 4000.0

    def test_start_stop_collects_samples(self):
        sampler = MetricsSampler(sample_interval_ms=10)
        sampler.start()
        try:
            deadline = time.monotonic() + 2.0
            while sampler.latest() is None and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            sampler.stop()
        assert sampler.latest() is not None
        assert sampler._thread is None  # stop() joined and cleared the thread

    def test_discover_inference_pid_returns_int_or_none(self):
        pid = MetricsSampler._discover_inference_pid()
        assert pid is None or isinstance(pid, int)
