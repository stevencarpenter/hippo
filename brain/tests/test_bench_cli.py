"""CLI surface tests. Each subcommand is invoked with mocked downstream calls
to verify argument parsing, routing, and exit codes. The actual orchestration
work is covered by the dedicated module tests; here we just check the wiring.
"""

import json

import pytest

from hippo_brain.bench.cli import main


def test_cli_help_smoke(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "run" in out
    assert "corpus" in out
    assert "summary" in out
    assert "determinism" in out


def test_cli_run_help_lists_core_args(capsys):
    with pytest.raises(SystemExit):
        main(["run", "--help"])
    out = capsys.readouterr().out
    assert "--models" in out
    assert "--corpus-version" in out
    assert "--brain-url" in out
    assert "--embedding-model" in out
    assert "--skip-prod-pause" in out


def test_cli_corpus_help_lists_v2_args(capsys):
    with pytest.raises(SystemExit):
        main(["corpus", "init", "--help"])
    out = capsys.readouterr().out
    assert "--corpus-days" in out
    assert "--corpus-buckets" in out
    assert "--shell-min" in out
    assert "--bump-version" in out


def test_cli_determinism_help_lists_mode_and_budgets(capsys):
    """BT-29 / post-review M1: `--mode` is the only way for an operator on a
    non-hybrid retrieval deployment to verify determinism. A refactor that
    drops the arg would silently fall back to comparing `hybrid` even when
    the operator passed `--mode semantic` (argparse would `error: unrecognized
    argument`, which the operator might not notice in a script). Pin both
    `--mode` and the two budget flags so the parser surface is regression-
    protected.
    """
    with pytest.raises(SystemExit):
        main(["determinism", "--help"])
    out = capsys.readouterr().out
    assert "--mode" in out
    assert "--mrr-budget" in out
    assert "--hit-at-1-budget" in out


def test_cli_determinism_returns_0_on_passing_runs(tmp_path):
    """End-to-end CLI dispatch: write two JSONLs whose hybrid-mode metrics
    differ by < 0.02, invoke `hippo-bench determinism r1 r2`, expect exit 0."""
    rows_r1 = [
        {
            "record_type": "model_summary",
            "run_id": "t",
            "model": {"id": "model-A"},
            "downstream_proxy": {
                "modes": {"hybrid": {"mrr": 0.40, "hit_at_1": 0.50}},
                "qa_count": 8,
                "k": 10,
                "per_item": [],
            },
        }
    ]
    rows_r2 = [
        {
            "record_type": "model_summary",
            "run_id": "t",
            "model": {"id": "model-A"},
            "downstream_proxy": {
                "modes": {"hybrid": {"mrr": 0.405, "hit_at_1": 0.50}},
                "qa_count": 8,
                "k": 10,
                "per_item": [],
            },
        }
    ]
    p1 = tmp_path / "r1.jsonl"
    p2 = tmp_path / "r2.jsonl"
    p1.write_text("\n".join(json.dumps(r) for r in rows_r1))
    p2.write_text("\n".join(json.dumps(r) for r in rows_r2))

    rc = main(["determinism", str(p1), str(p2)])
    assert rc == 0


def test_cli_determinism_returns_1_on_regression(tmp_path):
    """Operator's CI gate: exit code 1 when any model exceeds budget. Pinned so
    a refactor that swapped 0/1 returns can't silently flip the gate's polarity.
    """
    rows = lambda mrr: [  # noqa: E731 — closure-style helper inside test
        {
            "record_type": "model_summary",
            "run_id": "t",
            "model": {"id": "model-A"},
            "downstream_proxy": {
                "modes": {"hybrid": {"mrr": mrr, "hit_at_1": 0.50}},
                "qa_count": 8,
                "k": 10,
                "per_item": [],
            },
        }
    ]
    p1 = tmp_path / "r1.jsonl"
    p2 = tmp_path / "r2.jsonl"
    p1.write_text(json.dumps(rows(0.40)[0]))
    p2.write_text(json.dumps(rows(0.50)[0]))  # 0.10 spread, well over 0.02

    rc = main(["determinism", str(p1), str(p2)])
    assert rc == 1


def test_cli_run_dry_run_invokes_orchestrate(monkeypatch, tmp_path, capsys):
    """`hippo-bench run --dry-run` plumbs args through to orchestrate_run."""
    captured = {}

    def fake_orchestrate(**kwargs):
        captured.update(kwargs)
        from hippo_brain.bench.orchestrate import OrchestrationResult

        return OrchestrationResult(
            run_id="run-x",
            out_path=kwargs["out_path"],
            models_completed=[],
            models_errored=[],
            preflight_aborted=False,
        )

    monkeypatch.setattr("hippo_brain.bench.cli.orchestrate_run", fake_orchestrate)
    monkeypatch.setattr("hippo_brain.bench.pause_rpc.recover_stale_pause", lambda _u: False)
    out_path = tmp_path / "run.jsonl"
    rc = main(
        [
            "run",
            "--dry-run",
            "--skip-checks",
            "--out",
            str(out_path),
            "--models",
            "m1,m2",
        ]
    )
    assert rc == 0
    assert captured["candidate_models"] == ["m1", "m2"]
    assert captured["dry_run"] is True
    assert captured["skip_checks"] is True
    captured_out = capsys.readouterr().out
    assert "run-x" in captured_out


def test_cli_run_returns_3_when_all_models_errored(monkeypatch, tmp_path):
    """Exit code 3 signals: ran but every model failed."""
    from hippo_brain.bench.orchestrate import OrchestrationResult

    def fake_orchestrate(**kwargs):
        return OrchestrationResult(
            run_id="r",
            out_path=kwargs["out_path"],
            models_completed=[],
            models_errored=["bad-model"],
            preflight_aborted=False,
        )

    monkeypatch.setattr("hippo_brain.bench.cli.orchestrate_run", fake_orchestrate)
    monkeypatch.setattr("hippo_brain.bench.pause_rpc.recover_stale_pause", lambda _u: False)
    rc = main(
        [
            "run",
            "--skip-checks",
            "--models",
            "bad-model",
            "--out",
            str(tmp_path / "r.jsonl"),
        ]
    )
    assert rc == 3


def test_cli_run_returns_2_when_preflight_aborts(monkeypatch, tmp_path):
    """Exit code 2 signals: pre-flight blocked the run."""
    from hippo_brain.bench.orchestrate import OrchestrationResult

    def fake_orchestrate(**kwargs):
        return OrchestrationResult(
            run_id="r",
            out_path=kwargs["out_path"],
            models_completed=[],
            models_errored=[],
            preflight_aborted=True,
        )

    monkeypatch.setattr("hippo_brain.bench.cli.orchestrate_run", fake_orchestrate)
    monkeypatch.setattr("hippo_brain.bench.pause_rpc.recover_stale_pause", lambda _u: False)
    rc = main(
        [
            "run",
            "--models",
            "m1",
            "--out",
            str(tmp_path / "r.jsonl"),
        ]
    )
    assert rc == 2


def test_cli_run_surfaces_preflight_warnings(monkeypatch, tmp_path, capsys):
    """A non-fatal preflight warning (e.g. QA scoring skipped) is printed in a
    visible [WW] banner and does NOT change the exit code (run still succeeded)."""
    from hippo_brain.bench.orchestrate import OrchestrationResult

    def fake_orchestrate(**kwargs):
        return OrchestrationResult(
            run_id="r",
            out_path=kwargs["out_path"],
            models_completed=["m1"],
            models_errored=[],
            preflight_aborted=False,
            preflight_warnings=["qa_scoreable: Q/A fixture missing: /x/eval-qa-v1.jsonl"],
        )

    monkeypatch.setattr("hippo_brain.bench.cli.orchestrate_run", fake_orchestrate)
    monkeypatch.setattr("hippo_brain.bench.pause_rpc.recover_stale_pause", lambda _u: False)
    rc = main(
        [
            "run",
            "--skip-checks",
            "--models",
            "m1",
            "--out",
            str(tmp_path / "r.jsonl"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[WW]" in out
    assert "qa_scoreable" in out
    assert "Q/A fixture missing" in out


def test_cli_run_requires_models():
    with pytest.raises(SystemExit) as exc:
        main(["run"])
    assert exc.value.code == 2


def test_cli_summary_renders_run_file(tmp_path, capsys):
    """summary subcommand reads JSONL and prints a table."""
    f = tmp_path / "run.jsonl"
    lines = [
        json.dumps(
            {
                "record_type": "run_manifest",
                "run_id": "r",
                "candidate_models": ["m"],
                "corpus_version": "v",
            }
        ),
        json.dumps(
            {
                "record_type": "model_summary",
                "run_id": "r",
                "model": {"id": "m"},
                "events_attempted": 1,
                "attempts_total": 1,
                "gates": {
                    "schema_validity_rate": 1.0,
                    "refusal_rate": 0.0,
                    "latency_p95_ms": 100,
                    "self_consistency_mean": 0.9,
                    "entity_sanity_mean": 0.95,
                },
                "system_peak": {"wall_clock_sec": 1},
                "tier0_verdict": {"passed": True, "failed_gates": [], "notes": []},
            }
        ),
    ]
    f.write_text("\n".join(lines) + "\n")

    rc = main(["summary", str(f)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "m" in out
    assert "pass" in out.lower()


def test_cli_qa_validate_prints_report(monkeypatch, tmp_path, capsys):
    from hippo_brain.bench import cli

    qa = tmp_path / "qa.jsonl"
    corpus = tmp_path / "corpus.sqlite"
    qa.write_text("")
    corpus.write_bytes(b"")

    class Report:
        passes = True
        detail = "scoreable Q/A items: 3/3 (minimum 1)"

        def to_dict(self):
            return {"scoreable": 3, "total": 3, "passes": True}

    monkeypatch.setattr(cli, "validate_qa_fixture", lambda *_a, **_k: Report())

    code = cli.main(
        [
            "qa",
            "validate",
            "--qa-path",
            str(qa),
            "--corpus-sqlite",
            str(corpus),
            "--min-scoreable",
            "1",
        ]
    )

    assert code == 0
    assert "scoreable Q/A items" in capsys.readouterr().out


def test_cli_qa_export_worklist(monkeypatch, tmp_path):
    from hippo_brain.bench import cli

    qa = tmp_path / "qa.jsonl"
    corpus = tmp_path / "corpus.sqlite"
    out = tmp_path / "worklist.jsonl"
    qa.write_text("")
    corpus.write_bytes(b"")
    monkeypatch.setattr(cli, "export_label_worklist", lambda *_a: 7)

    code = cli.main(
        [
            "qa",
            "export-worklist",
            "--qa-path",
            str(qa),
            "--corpus-sqlite",
            str(corpus),
            "--out",
            str(out),
        ]
    )

    assert code == 0


def test_cli_ingest_single_and_all(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from hippo_brain.bench import cli
    from hippo_brain.bench.paths import bench_runs_dir
    from hippo_brain.bench.results_store import connect

    # build a minimal valid run JSONL inside the runs dir
    runs = bench_runs_dir(create=True)
    jsonl = runs / "run-x.jsonl"
    import json

    with jsonl.open("w") as f:
        f.write(
            json.dumps(
                {
                    "record_type": "run_manifest",
                    "run_id": "run-x",
                    "started_at_iso": "2026-05-31T00:00:00+00:00",
                    "host": {},
                    "candidate_models": [],
                    "corpus_content_hash": "h",
                },
                sort_keys=True,
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "record_type": "run_end",
                    "run_id": "run-x",
                    "finished_at_iso": "2026-05-31T01:00:00+00:00",
                    "models_completed": [],
                    "models_errored": [],
                },
                sort_keys=True,
            )
            + "\n"
        )

    assert cli.main(["ingest", str(jsonl)]) == 0
    capsys.readouterr()  # drop first-ingest output

    conn = connect()
    try:
        first_ingested_at = conn.execute(
            "SELECT ingested_at_ms FROM bench_runs WHERE run_id='run-x'"
        ).fetchone()[0]
    finally:
        conn.close()

    # --all is idempotent: run-x already present → skipped, still exit 0.
    assert cli.main(["ingest", "--all"]) == 0
    # Verify the skip branch was actually taken — not a silent re-ingest. Because
    # ingest_run is idempotent via DELETE-then-INSERT, COUNT(*) alone can't tell
    # a skip from a force re-ingest, so assert the printed status and that the
    # original ingested_at_ms is untouched (a re-ingest would overwrite it).
    out = capsys.readouterr().out
    assert "skipped (already ingested)" in out

    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM bench_runs").fetchone()[0] == 1
        second_ingested_at = conn.execute(
            "SELECT ingested_at_ms FROM bench_runs WHERE run_id='run-x'"
        ).fetchone()[0]
        assert second_ingested_at == first_ingested_at
    finally:
        conn.close()


def test_cli_ingest_no_target_errors(capsys):
    """`ingest` with neither a run_file nor --all must fail cleanly with an
    error message and exit 1 — not raise an uncaught TypeError stack trace."""
    from hippo_brain.bench import cli

    assert cli.main(["ingest"]) == 1
    err = capsys.readouterr().out
    assert "error:" in err
    assert "run_file" in err or "--all" in err


def test_cli_add_adversarial_claude_reads_agentic_sessions(monkeypatch, tmp_path):
    """A claude-<id> adversarial id must resolve against agentic_sessions
    (harness='claude-code'), NOT the frozen claude_sessions table — matching the
    id space used by the corpus builder, qa validator, and retrieval. Regression
    for the fifth frozen-claude_* read (cli.py corpus add-adversarial)."""
    import sqlite3

    from hippo_brain.bench import cli

    # Fake $HOME so the hardcoded prod DB path resolves under tmp_path, and a
    # separate XDG_DATA_HOME so the bench overlay lands in tmp too.
    home = tmp_path / "home"
    (home / ".local" / "share" / "hippo").mkdir(parents=True)
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    prod_db = home / ".local" / "share" / "hippo" / "hippo.db"
    conn = sqlite3.connect(prod_db)
    # agentic_sessions has the real claude row at id=7; the frozen claude_sessions
    # has a DIFFERENT row at id=7 that must NOT be used.
    conn.execute(
        "CREATE TABLE agentic_sessions (id INTEGER PRIMARY KEY, harness TEXT, summary_text TEXT)"
    )
    conn.execute("CREATE TABLE claude_sessions (id INTEGER PRIMARY KEY, summary_text TEXT)")
    conn.execute(
        "INSERT INTO agentic_sessions (id, harness, summary_text) "
        "VALUES (7, 'claude-code', 'CORRECT agentic row')"
    )
    conn.execute("INSERT INTO claude_sessions (id, summary_text) VALUES (7, 'WRONG frozen row')")
    conn.commit()
    conn.close()

    code = cli.main(["corpus", "add-adversarial", "claude-7", "--reason", "test adversarial"])
    assert code == 0

    # Verify the overlay stored the agentic row content, not the frozen one.
    from hippo_brain.bench.paths import corpus_overlay_path

    overlay = sqlite3.connect(corpus_overlay_path())
    stored = overlay.execute(
        "SELECT redacted_content FROM adversarial_events WHERE event_id = ?", ("claude-7",)
    ).fetchone()
    overlay.close()
    assert stored is not None
    assert "CORRECT agentic row" in stored[0]
    assert "WRONG frozen row" not in stored[0]
