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
