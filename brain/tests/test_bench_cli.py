"""CLI surface tests. Each subcommand is invoked with mocked downstream calls
to verify argument parsing, routing, and exit codes. The actual orchestration
work is covered by the dedicated module tests; here we just check the wiring.
"""

import json
import sqlite3

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


def test_cli_run_help_lists_temperature_and_filters(capsys):
    with pytest.raises(SystemExit):
        main(["run", "--help"])
    out = capsys.readouterr().out
    assert "--models" in out
    assert "--temperature" in out
    assert "--self-consistency-events" in out
    assert "--self-consistency-runs" in out
    assert "--latency-ceiling-sec" in out


def test_cli_corpus_help_lists_filter_flag(capsys):
    with pytest.raises(SystemExit):
        main(["corpus", "init", "--help"])
    out = capsys.readouterr().out
    assert "--no-filter-trivial" in out
    assert "--shell" in out
    assert "--claude" in out


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
    out_path = tmp_path / "run.jsonl"
    rc = main(
        [
            "run",
            "--corpus-version",  # BT-18: explicit v1 since test patches v1 orchestrator
            "corpus-v1",
            "--dry-run",
            "--skip-checks",
            "--out",
            str(out_path),
            "--models",
            "m1,m2",
            "--temperature",
            "0.3",
        ]
    )
    assert rc == 0
    assert captured["candidate_models"] == ["m1", "m2"]
    assert captured["dry_run"] is True
    assert captured["skip_checks"] is True
    assert captured["temperature"] == 0.3
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
    rc = main(
        [
            "run",
            "--corpus-version",
            "corpus-v1",
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
    rc = main(
        [
            "run",
            "--corpus-version",
            "corpus-v1",
            "--models",
            "m1",
            "--out",
            str(tmp_path / "r.jsonl"),
        ]
    )
    assert rc == 2


def test_cli_run_requires_models():
    import pytest

    with pytest.raises(SystemExit) as exc:
        main(["run"])
    assert exc.value.code == 2


def _seed_minimal_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE shell_events (
            id INTEGER PRIMARY KEY, command TEXT, stdout TEXT, stderr TEXT,
            duration_ms INTEGER, exit_code INTEGER, cwd TEXT, ts INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO shell_events (command, stdout, stderr, duration_ms, exit_code, cwd, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("cargo test", "12 passed", "", 4200, 0, "/r", 0),
    )
    conn.commit()
    conn.close()


def test_cli_corpus_init_writes_fixture(monkeypatch, tmp_path, capsys):
    db = tmp_path / "hippo.db"
    _seed_minimal_db(db)

    fixture = tmp_path / "fixtures" / "corpus-test.jsonl"
    manifest = tmp_path / "fixtures" / "corpus-test.manifest.json"
    monkeypatch.setattr("hippo_brain.bench.cli.corpus_path", lambda v: fixture)
    monkeypatch.setattr("hippo_brain.bench.cli.corpus_manifest_path", lambda v: manifest)

    rc = main(
        [
            "corpus",
            "init",
            "--corpus-version",
            "corpus-test",
            "--db-path",
            str(db),
            "--shell",
            "1",
            "--claude",
            "0",
            "--browser",
            "0",
            "--workflow",
            "0",
            "--seed",
            "1",
        ]
    )
    assert rc == 0
    assert fixture.exists()
    assert manifest.exists()
    out = capsys.readouterr().out
    assert "wrote" in out


def test_cli_corpus_verify_passes_on_clean_fixture(monkeypatch, tmp_path, capsys):
    """corpus verify returns 0 when content hash matches manifest."""
    db = tmp_path / "hippo.db"
    _seed_minimal_db(db)

    fixture = tmp_path / "corpus-test.jsonl"
    manifest = tmp_path / "corpus-test.manifest.json"
    monkeypatch.setattr("hippo_brain.bench.cli.corpus_path", lambda v: fixture)
    monkeypatch.setattr("hippo_brain.bench.cli.corpus_manifest_path", lambda v: manifest)
    main(
        [
            "corpus",
            "init",
            "--corpus-version",
            "v",
            "--db-path",
            str(db),
            "--shell",
            "1",
            "--claude",
            "0",
            "--browser",
            "0",
            "--workflow",
            "0",
        ]
    )
    capsys.readouterr()  # discard init output

    rc = main(["corpus", "verify", "--corpus-version", "v"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok" in out


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
