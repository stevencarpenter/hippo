from hippo_brain.bench.config import BenchConfig, DEFAULT_THRESHOLDS
from hippo_brain.bench.paths import bench_root, corpus_path, runs_dir


def test_bench_root_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    root = bench_root()
    assert root == tmp_path / "hippo" / "bench"


def test_bench_root_default(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = bench_root()
    assert root == tmp_path / ".local" / "share" / "hippo" / "bench"


def test_corpus_path_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    p = corpus_path("corpus-v1")
    assert p == tmp_path / "hippo" / "bench" / "fixtures" / "corpus-v1.jsonl"


def test_runs_dir_created_on_access(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    d = runs_dir(create=True)
    assert d.is_dir()


def test_default_thresholds_shape():
    # Every threshold listed in the design spec must be present and typed.
    assert DEFAULT_THRESHOLDS["schema_validity_min"] == 0.95
    assert DEFAULT_THRESHOLDS["refusal_max"] == 0.0
    assert DEFAULT_THRESHOLDS["latency_p95_max_ms"] == 60_000
    assert DEFAULT_THRESHOLDS["self_consistency_min"] == 0.7
    assert DEFAULT_THRESHOLDS["entity_sanity_min"] == 0.9


def test_bench_config_roundtrip(tmp_path):
    cfg = BenchConfig(
        corpus_version="corpus-v1",
        candidate_models=["qwen3.5-35b-a3b"],
        self_consistency_events=5,
        self_consistency_runs_per_event=5,
        latency_ceiling_sec=60,
        thresholds=dict(DEFAULT_THRESHOLDS),
        fixture_path=tmp_path / "corpus-v1.jsonl",
        out_path=tmp_path / "run.jsonl",
        skip_checks=False,
    )
    d = cfg.to_dict()
    assert d["corpus_version"] == "corpus-v1"
    assert d["thresholds"]["schema_validity_min"] == 0.95
