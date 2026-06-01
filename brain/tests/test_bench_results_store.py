from hippo_brain.bench.paths import bench_results_db_path


def test_bench_results_db_path_under_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    p = bench_results_db_path()
    assert p == tmp_path / "hippo-bench" / "bench-results.db"
