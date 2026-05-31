"""hippo-bench CLI entrypoint.

Subcommands:
  run                    — run the bench against one or more candidate models
  corpus init            — sample a fixture from the live hippo DB
  corpus verify          — re-check a fixture against its manifest
  corpus add-adversarial — add an adversarial event to the corpus overlay
  summary                — pretty-print a run JSONL file as a text table
  determinism            — compare N JSONL run files (BT-29 trust gate)
  recover                — clear a stale pause lockfile from a crashed bench

See `docs/superpowers/specs/2026-04-21-hippo-bench-design.md` for the
full design and `brain/src/hippo_brain/bench/README.md` for an
operator-facing onboarding guide.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import platform
import sqlite3
from pathlib import Path

from hippo_brain.bench.corpus import init_corpus, verify_corpus
from hippo_brain.bench.orchestrate import orchestrate_run
from hippo_brain.bench.paths import (
    bench_qa_path,
    bench_runs_dir,
    corpus_jsonl_path,
    corpus_manifest_path,
    corpus_overlay_path,
    corpus_sqlite_path,
)
from hippo_brain.bench.prod_config import (
    default_embedding_model,
    default_inference_base_url,
    default_prod_brain_url,
)
from hippo_brain.bench.qa import export_label_worklist, validate_qa_fixture

_OVERLAY_CAP = 50


def _cmd_corpus_init(args: argparse.Namespace) -> int:
    corpus_version = args.bump_version if args.bump_version else args.corpus_version
    force = bool(args.bump_version)
    dest_sqlite = corpus_sqlite_path(corpus_version)
    dest_jsonl = corpus_jsonl_path(corpus_version)
    manifest = corpus_manifest_path(corpus_version)
    try:
        entries = init_corpus(
            db_path=Path(args.db_path),
            dest_sqlite=dest_sqlite,
            dest_jsonl=dest_jsonl,
            manifest_path=manifest,
            corpus_version=corpus_version,
            corpus_days=args.corpus_days,
            corpus_buckets=args.corpus_buckets,
            shell_min=args.shell_min,
            claude_min=args.claude_min,
            browser_min=args.browser_min,
            workflow_min=args.workflow_min,
            seed=args.seed,
            force=force,
        )
    except FileExistsError as e:
        print(f"error: {e}")
        print("Use --bump-version to overwrite.")
        return 1
    print(f"wrote {len(entries)} entries")
    print(f"sqlite: {dest_sqlite}")
    print(f"jsonl:  {dest_jsonl}")
    print(f"manifest: {manifest}")
    return 0


def _cmd_corpus_verify(args: argparse.Namespace) -> int:
    ok, detail = verify_corpus(
        corpus_sqlite_path(args.corpus_version),
        corpus_jsonl_path(args.corpus_version),
        corpus_manifest_path(args.corpus_version),
    )
    print(detail)
    return 0 if ok else 1


def _init_overlay_db(overlay_path: Path) -> sqlite3.Connection:
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(overlay_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adversarial_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            reason TEXT NOT NULL,
            redacted_content TEXT NOT NULL,
            added_at_iso TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _cmd_corpus_add_adversarial(args: argparse.Namespace) -> int:
    from hippo_brain.redaction import redact

    overlay_path = corpus_overlay_path()
    conn = _init_overlay_db(overlay_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM adversarial_events").fetchone()[0]
        if count >= _OVERLAY_CAP:
            print(f"error: overlay at cap ({_OVERLAY_CAP} items). Remove items before adding.")
            return 1

        event_id: str = args.event_id
        source_override: str | None = args.source

        if source_override:
            source = source_override
        else:
            parts = event_id.split("-", 1)
            if len(parts) < 2:
                print(f"error: cannot determine source from event_id={event_id!r}. Use --source.")
                return 1
            source = parts[0]

        valid_sources = {"shell", "claude", "browser", "workflow"}
        if source not in valid_sources:
            print(f"error: source must be one of {sorted(valid_sources)}, got {source!r}")
            return 1

        # The "claude" source resolves to agentic_sessions (harness='claude-code'),
        # NOT the frozen claude_sessions table — a claude-<id> event_id is an
        # agentic_sessions.id everywhere else (corpus builder, qa validator,
        # retrieval linked_source_ids). Reading claude_sessions here would miss
        # (new sessions only live in agentic_sessions) or mis-resolve a stale row.
        source_table_map = {
            "shell": ("events", "id", ""),
            "claude": ("agentic_sessions", "id", " AND harness = 'claude-code'"),
            "browser": ("browser_events", "id", ""),
            "workflow": ("workflow_runs", "id", ""),
        }
        table, id_col, extra_where = source_table_map[source]

        try:
            _, raw_id_str = event_id.split("-", 1)
            raw_id = int(raw_id_str)
        except (ValueError, IndexError):  # fmt: skip
            print(f"error: could not parse numeric ID from event_id={event_id!r}")
            return 1

        db_path = Path.home() / ".local" / "share" / "hippo" / "hippo.db"
        prod_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        prod_conn.row_factory = sqlite3.Row
        try:
            row = prod_conn.execute(
                f"SELECT * FROM {table} WHERE {id_col} = ?{extra_where}", (raw_id,)
            ).fetchone()
        finally:
            prod_conn.close()

        if row is None:
            print(f"error: event {event_id!r} not found in prod DB ({table})")
            return 1

        raw_payload = json.dumps(dict(row), default=str, sort_keys=True)
        redacted = redact(raw_payload)
        now_iso = _dt.datetime.now(tz=_dt.UTC).isoformat()

        cur = conn.execute(
            "INSERT OR IGNORE INTO adversarial_events "
            "(event_id, source, reason, redacted_content, added_at_iso) VALUES (?, ?, ?, ?, ?)",
            (event_id, source, args.reason, redacted, now_iso),
        )
        conn.commit()
        if cur.rowcount:
            print(f"added adversarial event {event_id!r} to {overlay_path}")
        else:
            print(
                f"already present: adversarial event {event_id!r} in {overlay_path} (not updated)"
            )
        return 0
    finally:
        conn.close()


def _cmd_recover(args: argparse.Namespace) -> int:
    """BT-06: detect a stale pause lockfile and resume prod brain.

    Surfaced both as an explicit subcommand and called automatically at
    the top of `_cmd_run`. Idempotent — exits 0 in both cases.
    """
    from hippo_brain.bench.pause_rpc import PAUSE_LOCKFILE, recover_stale_pause

    recovered = recover_stale_pause(args.brain_url)
    if recovered:
        print(f"recovered: stale pause lockfile cleared ({PAUSE_LOCKFILE})")
    else:
        print(f"no stale lockfile at {PAUSE_LOCKFILE}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    # BT-06: recover from a prior crashed bench run before doing anything
    # else. If the previous bench was SIGKILL'd, prod brain is still paused;
    # this resumes it before we issue our own pause.
    from hippo_brain.bench.pause_rpc import recover_stale_pause

    recover_stale_pause(args.brain_url)

    ts = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%S")
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    out = (
        Path(args.out)
        if args.out
        else bench_runs_dir(create=True) / f"run-{ts}-{platform.node()}.jsonl"
    )
    result = orchestrate_run(
        candidate_models=models,
        corpus_version=args.corpus_version,
        out_path=out,
        brain_url=args.brain_url,
        inference_url=args.base_url,
        embedding_model=args.embedding_model,
        skip_prod_pause=args.skip_prod_pause,
        dry_run=args.dry_run,
        skip_checks=args.skip_checks,
        min_scoreable_qa=args.min_scoreable_qa,
    )
    print(f"run_id={result.run_id} out={result.out_path}")
    if result.models_completed:
        print(f"completed: {result.models_completed}")
    if result.models_errored:
        print(f"errored:   {result.models_errored}")
    if result.preflight_warnings:
        # Surface non-fatal preflight warnings (e.g. QA scoring skipped because
        # the fixture is absent) as the last thing on screen so they are not
        # lost in the run's scrollback. Mirrors `hippo doctor`'s [WW] marker.
        print("=" * 64)
        print(f"[WW] {len(result.preflight_warnings)} preflight warning(s) — run proceeded:")
        for warning in result.preflight_warnings:
            print(f"     - {warning}")
        print("=" * 64)
    if result.preflight_aborted:
        return 2
    if result.models_errored and not result.models_completed:
        return 3
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    from hippo_brain.bench.pretty import render_summary_text

    text = render_summary_text(Path(args.run_file))
    print(text)
    return 0


def _cmd_determinism(args: argparse.Namespace) -> int:
    """BT-29: compare N JSONL run files; exit 1 if any model exceeds budget."""
    from hippo_brain.bench.determinism import compare_runs

    paths = [Path(p) for p in args.run_files]
    report = compare_runs(
        paths,
        mrr_budget=args.mrr_budget,
        hit_at_1_budget=args.hit_at_1_budget,
        mode=args.mode,
    )
    print(report.render())
    return 0 if report.passes() else 1


def _cmd_qa_validate(args: argparse.Namespace) -> int:
    report = validate_qa_fixture(
        Path(args.qa_path),
        Path(args.corpus_sqlite),
        min_scoreable=args.min_scoreable,
    )
    print(report.detail)
    if args.json:
        print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passes else 1


def _cmd_qa_export_worklist(args: argparse.Namespace) -> int:
    count = export_label_worklist(
        Path(args.qa_path),
        Path(args.corpus_sqlite),
        Path(args.out),
    )
    print(f"wrote {count} unscoreable Q/A items to {args.out}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hippo-bench",
        description="Local enrichment-model shakeout benchmark (Tier 0).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the bench against candidate models")
    run.add_argument("--models", required=True, help="Comma-separated model identifiers")
    run.add_argument(
        "--corpus-version",
        default="corpus-v2",
        help="Corpus snapshot identifier (string label only).",
    )
    run.add_argument(
        "--base-url",
        default=default_inference_base_url(),
        help="Inference server base URL. Defaults to [inference].base_url from "
        "$XDG_CONFIG_HOME/hippo/config.toml (or $HOME/.config/hippo/config.toml "
        "if XDG_CONFIG_HOME is unset), falling back to http://localhost:8000/v1.",
    )
    run.add_argument(
        "--brain-url",
        default=default_prod_brain_url(),
        help="Prod brain base URL. Defaults to http://127.0.0.1:<[brain].port> "
        "read from $XDG_CONFIG_HOME/hippo/config.toml (or $HOME/.config/hippo/config.toml "
        "if XDG_CONFIG_HOME is unset), falling back to port 9175.",
    )
    run.add_argument(
        "--embedding-model",
        default=default_embedding_model(),
        help="Embedding model identifier. Defaults to [models].embedding from "
        "$XDG_CONFIG_HOME/hippo/config.toml (or $HOME/.config/hippo/config.toml "
        "if XDG_CONFIG_HOME is unset), falling back to nomicai-modernbert-embed-base-8bit.",
    )
    run.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip pre-flight (lms availability, power, disk, etc). Debug only.",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve everything and emit a manifest, but make no LM Studio calls.",
    )
    run.add_argument("--out", help="Override output JSONL path.")
    run.add_argument(
        "--skip-prod-pause",
        action="store_true",
        help="Skip pausing the prod brain before the run.",
    )
    run.add_argument(
        "--min-scoreable-qa",
        type=int,
        default=1,
        help="Abort the run if the Q/A fixture is present but fewer than this many "
        "goldens resolve against the corpus. Default 1 (any scoreable item). Set to "
        "100 for the publish-grade gate. A missing fixture warns rather than aborts.",
    )
    run.set_defaults(func=_cmd_run)

    corpus = sub.add_parser("corpus", help="Manage the bench fixture")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)

    ci = corpus_sub.add_parser("init", help="Sample a fresh fixture from hippo.db")
    ci.add_argument(
        "--corpus-version",
        default="corpus-v2",
        help="Corpus snapshot identifier (string label only).",
    )
    ci.add_argument("--seed", type=int, default=42)
    ci.add_argument(
        "--db-path",
        default=str(Path.home() / ".local" / "share" / "hippo" / "hippo.db"),
    )
    ci.add_argument("--corpus-days", type=int, default=90)
    ci.add_argument("--corpus-buckets", type=int, default=9)
    ci.add_argument("--shell-min", type=int, default=50)
    ci.add_argument("--claude-min", type=int, default=50)
    ci.add_argument("--browser-min", type=int, default=50)
    ci.add_argument("--workflow-min", type=int, default=50)
    ci.add_argument(
        "--bump-version",
        default=None,
        metavar="VERSION",
        help="Override corpus version string and force-overwrite existing corpus.",
    )
    ci.set_defaults(func=_cmd_corpus_init)

    cv = corpus_sub.add_parser("verify", help="Re-check a fixture's content hash")
    cv.add_argument(
        "--corpus-version",
        default="corpus-v2",
        help="Corpus snapshot identifier (string label only).",
    )
    cv.set_defaults(func=_cmd_corpus_verify)

    caa = corpus_sub.add_parser(
        "add-adversarial", help="Append an adversarial event to the corpus overlay"
    )
    caa.add_argument("event_id", help="Event ID to add (e.g. shell-12345)")
    caa.add_argument("--reason", required=True, help="Why this event is adversarial")
    caa.add_argument(
        "--source",
        choices=["shell", "claude", "browser", "workflow"],
        default=None,
        help="Source override (inferred from event_id prefix if omitted)",
    )
    caa.set_defaults(func=_cmd_corpus_add_adversarial)

    summary = sub.add_parser("summary", help="Pretty-print a run JSONL file")
    summary.add_argument("run_file")
    summary.set_defaults(func=_cmd_summary)

    # BT-29 / post-review: deterministic-rerun verification. Operator runs the
    # bench 3× against the same model + frozen corpus, then compares JSONLs.
    # Exit 1 on any model exceeding the trust budget — wire into CI for an
    # automated regression alarm.
    det = sub.add_parser(
        "determinism",
        help="BT-29: compare N JSONL run files and verify metric stability",
    )
    det.add_argument("run_files", nargs="+", help="Two or more JSONL files from `hippo-bench run`")
    det.add_argument(
        "--mrr-budget",
        type=float,
        default=0.02,
        help="Max permitted spread of MRR across runs (default 0.02 per DoD #1)",
    )
    det.add_argument(
        "--hit-at-1-budget",
        type=float,
        default=0.02,
        help="Max permitted spread of Hit@1 across runs (default 0.02 per DoD #1)",
    )
    det.add_argument(
        "--mode",
        default="hybrid",
        help="Retrieval mode to compare (default hybrid; downstream_proxy.modes key)",
    )
    det.set_defaults(func=_cmd_determinism)

    # BT-06: recovery subcommand. Idempotent — clears stale pause lockfile
    # and resumes prod brain if a prior bench was SIGKILL'd.
    recover = sub.add_parser(
        "recover",
        help="Detect a stale pause lockfile from a crashed bench and resume prod brain",
    )
    recover.add_argument(
        "--brain-url",
        default=default_prod_brain_url(),
        help="Prod brain base URL to resume if lockfile doesn't carry one. "
        "Defaults to http://127.0.0.1:<[brain].port> read from "
        "$XDG_CONFIG_HOME/hippo/config.toml (or $HOME/.config/hippo/config.toml "
        "if XDG_CONFIG_HOME is unset).",
    )
    recover.set_defaults(func=_cmd_recover)

    qa = sub.add_parser("qa", help="Validate and label the bench Q/A fixture")
    qa_sub = qa.add_subparsers(dest="qa_command", required=True)

    qv = qa_sub.add_parser("validate", help="Validate Q/A golden_event_id coverage")
    qv.add_argument("--qa-path", default=str(bench_qa_path()))
    qv.add_argument("--corpus-sqlite", default=str(corpus_sqlite_path("corpus-v2")))
    qv.add_argument("--min-scoreable", type=int, default=1)
    qv.add_argument("--json", action="store_true")
    qv.set_defaults(func=_cmd_qa_validate)

    qw = qa_sub.add_parser("export-worklist", help="Export unlabeled Q/A items for annotation")
    qw.add_argument("--qa-path", default=str(bench_qa_path()))
    qw.add_argument("--corpus-sqlite", default=str(corpus_sqlite_path("corpus-v2")))
    qw.add_argument("--out", required=True)
    qw.set_defaults(func=_cmd_qa_export_worklist)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
