from unittest.mock import MagicMock, patch

from hippo_brain.bench.metrics import MetricsSampler, Snapshot


def test_snapshot_shape():
    s = Snapshot(
        monotonic_ns=123,
        lmstudio_rss_mb=100.5,
        lmstudio_cpu_pct=50.0,
        load_avg_1m=2.1,
        mem_free_mb=1024.0,
    )
    assert s.lmstudio_rss_mb == 100.5


def test_sampler_finds_lmstudio_process():
    fake_proc = MagicMock()
    fake_proc.name.return_value = "LM Studio Helper"
    fake_proc.info = {"pid": 42, "name": "LM Studio Helper"}
    fake_proc.memory_info.return_value = MagicMock(rss=100 * 1024 * 1024)
    fake_proc.cpu_percent.return_value = 75.0

    with patch("psutil.process_iter", return_value=[fake_proc]):
        sampler = MetricsSampler(sample_interval_ms=10)
        pid = sampler._discover_lmstudio_pid()
    assert pid == 42


def test_sampler_aggregates_peak():
    sampler = MetricsSampler(sample_interval_ms=1)
    sampler._samples = [
        Snapshot(1, 100.0, 50.0, 1.0, 2000.0),
        Snapshot(2, 200.0, 90.0, 1.5, 1800.0),
        Snapshot(3, 150.0, 70.0, 1.2, 1900.0),
    ]
    peak = sampler.peak()
    assert peak["lmstudio_rss_mb"] == 200.0
    assert peak["lmstudio_cpu_pct"] == 90.0
    assert peak["load_avg_1m"] == 1.5


def test_sampler_latest_returns_most_recent():
    sampler = MetricsSampler(sample_interval_ms=1)
    sampler._samples = [
        Snapshot(1, 100.0, 50.0, 1.0, 2000.0),
        Snapshot(2, 200.0, 90.0, 1.5, 1800.0),
    ]
    latest = sampler.latest()
    assert latest.monotonic_ns == 2
    assert latest.lmstudio_rss_mb == 200.0


def test_sampler_latest_none_when_empty():
    sampler = MetricsSampler(sample_interval_ms=1)
    assert sampler.latest() is None
