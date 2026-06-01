import json

from hippo_brain.bench.dashboard_export import build_dashboard_html, export_dashboard
from hippo_brain.bench.results_store import connect, ingest_run

# reuse the JSONL builders shared with the results-store test module
from tests._bench_fixtures import (
    _manifest,
    _model_summary_with_proxy,
    _run_end,
    _write_jsonl,
)


def _seed(tmp_path):
    conn = connect(tmp_path / "bench-results.db")
    ingest_run(
        _write_jsonl(tmp_path / "r.jsonl", [_manifest(), _model_summary_with_proxy(), _run_end()]),
        conn=conn,
    )
    return conn


def test_build_dashboard_html_embeds_data(tmp_path):
    conn = _seed(tmp_path)
    try:
        html = build_dashboard_html(conn)
    finally:
        conn.close()
    assert "<html" in html.lower()
    assert 'id="hippo-bench-data"' in html
    # the embedded JSON blob is parseable and carries the three views
    blob = html.split('id="hippo-bench-data">', 1)[1].split("</script>", 1)[0]
    data = json.loads(blob)
    assert {"leaderboard", "history", "nodes"} <= set(data)
    assert data["leaderboard"][0]["model_id"] == "model-a"
    # per-node view carries the scored corpus node with its retrieval rows
    assert "claude-7" in data["nodes"]
    assert data["nodes"]["claude-7"]["retrieval"][0]["model_id"] == "model-a"


def test_export_dashboard_writes_file(tmp_path):
    conn = _seed(tmp_path)
    conn.close()
    out = tmp_path / "dashboard.html"
    written = export_dashboard(out, db_path=tmp_path / "bench-results.db")
    assert written == out
    assert out.read_text(encoding="utf-8").lower().startswith("<!doctype html")
