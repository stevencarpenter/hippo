from hippo_brain.bench.paths import bench_results_db_path
from hippo_brain.bench.results_store import SCHEMA_VERSION, connect


def test_bench_results_db_path_under_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    p = bench_results_db_path()
    assert p == tmp_path / "hippo-bench" / "bench-results.db"


def test_connect_creates_schema(tmp_path):
    db = tmp_path / "bench-results.db"
    conn = connect(db)
    try:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {
            "bench_runs",
            "bench_models",
            "bench_node_enrichment",
            "bench_node_retrieval",
        } <= names
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
