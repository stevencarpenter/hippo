"""hippo-bench CLI entrypoint.

Subcommands:
  run                    — run the bench against one or more candidate models
  corpus init            — sample a fixture from the live hippo DB
  corpus verify          — re-check a fixture against its manifest
  corpus add-adversarial — add an adversarial event to the v2 overlay
  summary                — pretty-print a run JSONL file as a text table

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

from hippo_brain.bench.corpus import (
    sample_from_hippo_db,
    verify_corpus,
    write_corpus,
)
from hippo_brain.bench.orchestrate import orchestrate_run
from hippo_brain.bench.paths import (
    bench_runs_dir,
    corpus_manifest_path,
    corpus_path,
    corpus_v2_jsonl_path,
    corpus_v2_manifest_path,
    corpus_v2_overlay_path,
    corpus_v2_sqlite_path,
    runs_dir,
)

# Default corpus stratification — mirrors the spec's "shakeout" sizing.
# Override via --shell, --claude, --browser, --workflow on `corpus init`.
_DEFAULT_SOURCE_COUNTS = {"shell": 15, "claude": 12, "browser": 10, "workflow": 3}
_OVERLAY_CAP = 50


def _cmd_corpus_init(args: argparse.Namespace) -> int:
    if args.corpus_version == "corpus-v2":
        from hippo_brain.bench.corpus_v2 import init_corpus_v2

        corpus_version = args.bump_version if args.bump_version else args.corpus_version
        force = bool(args.bump_version)
        dest_sqlite = corpus_v2_sqlite_path()
        dest_jsonl = corpus_v2_jsonl_path()
        manifest = corpus_v2_manifest_path()
        try:
            entries = init_corpus_v2(
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

    # v1 path
    fixture = corpus_path(args.corpus_version)
    manifest = corpus_manifest_path(args.corpus_version)
    counts = {
        "shell": args.shell,
        "claude": args.claude,
        "browser": args.browser,
        "workflow": args.workflow,
    }
    entries = sample_from_hippo_db(
        db_path=Path(args.db_path),
        source_counts=counts,
        seed=args.seed,
        filter_trivial=not args.no_filter_trivial,
    )
    write_corpus(entries, fixture, manifest, args.corpus_version, args.seed)
    print(f"wrote {len(entries)} entries to {fixture}")
    print(f"manifest: {manifest}")
    return 0


def _cmd_corpus_verify(args: argparse.Namespace) -> int:
    if args.corpus_version == "corpus-v2":
        from hippo_brain.bench.corpus_v2 import verify_corpus_v2

        ok, detail = verify_corpus_v2(
            corpus_v2_sqlite_path(),
            corpus_v2_jsonl_path(),
            corpus_v2_manifest_path(),
        )
        print(detail)
        return 0 if ok else 1

    fixture = corpus_path(args.corpus_version)
    manifest = corpus_manifest_path(args.corpus_version)
    ok, detail = verify_corpus(fixture, manifest)
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

    overlay_path = corpus_v2_overlay_path()
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

        source_table_map = {
            "shell": ("events", "id"),
            "claude": ("claude_sessions", "id"),
            "browser": ("browser_events", "id"),
            "workflow": ("workflow_runs", "id"),
        }
        table, id_col = source_table_map[source]

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
                f"SELECT * FROM {table} WHERE {id_col} = ?", (raw_id,)
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

    if args.corpus_version == "corpus-v2":
        from hippo_brain.bench.orchestrate_v2 import orchestrate_run_v2

        out = (
            Path(args.out)
            if args.out
            else bench_runs_dir(create=True) / f"run-{ts}-{platform.node()}.jsonl"
        )
        result = orchestrate_run_v2(
            candidate_models=models,
            corpus_version=args.corpus_version,
            out_path=out,
            brain_url=args.brain_url,
            lmstudio_url=args.base_url,
            embedding_model=args.embedding_model,
            skip_prod_pause=args.skip_prod_pause,
            dry_run=args.dry_run,
            skip_checks=args.skip_checks,
        )
        print(f"run_id={result.run_id} out={result.out_path}")
        if result.models_completed:
            print(f"completed: {result.models_completed}")
        if result.models_errored:
            print(f"errored:   {result.models_errored}")
        if result.preflight_aborted:
            return 2
        if result.models_errored and not result.models_completed:
            return 3
        return 0

    # v1 path
    fixture = corpus_path(args.corpus_version)
    manifest = corpus_manifest_path(args.corpus_version)
    out = (
        Path(args.out) if args.out else runs_dir(create=True) / f"run-{ts}-{platform.node()}.jsonl"
    )
    result = orchestrate_run(
        candidate_models=models,
        corpus_version=args.corpus_version,
        fixture_path=fixture,
        manifest_path=manifest,
        base_url=args.base_url,
        embedding_model=args.embedding_model,
        out_path=out,
        timeout_sec=args.latency_ceiling_sec,
        self_consistency_events=args.self_consistency_events,
        self_consistency_runs=args.self_consistency_runs,
        skip_checks=args.skip_checks,
        dry_run=args.dry_run,
        temperature=args.temperature,
    )
    print(f"run_id={result.run_id} out={result.out_path}")
    if result.models_completed:
        print(f"completed: {result.models_completed}")
    if result.models_errored:
        print(f"errored:   {result.models_errored}")
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hippo-bench",
        description="Local enrichment-model shakeout benchmark (Tier 0).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the bench against candidate models")
    run.add_argument("--models", required=True, help="Comma-separated model identifiers")
    # BT-18: default to v2 — bench v2 is the production path. v1 still
    # selectable explicitly for legacy comparisons.
    run.add_argument("--corpus-version", default="corpus-v2")
    run.add_argument("--base-url", default="http://localhost:1234/v1")
    run.add_argument(
        "--brain-url", default="http://localhost:8000", help="Prod brain base URL (v2)"
    )
    run.add_argument("--embedding-model", default="text-embedding-nomic-embed-text-v2-moe")
    run.add_argument(
        "--latency-ceiling-sec",
        type=int,
        default=60,
        help="Per-call timeout. Calls exceeding this are recorded as timeout.",
    )
    run.add_argument(
        "--self-consistency-events",
        type=int,
        default=5,
        help="How many corpus events to re-run for self-consistency.",
    )
    run.add_argument(
        "--self-consistency-runs",
        type=int,
        default=5,
        help="How many times to re-run each self-consistency event.",
    )
    run.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="Sampling temperature. T<0.3 makes self-consistency a vacuous signal.",
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
        help="Skip pausing the prod brain before the run (v2 only).",
    )
    run.set_defaults(func=_cmd_run)

    corpus = sub.add_parser("corpus", help="Manage the bench fixture")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)

    ci = corpus_sub.add_parser("init", help="Sample a fresh fixture from hippo.db")
    ci.add_argument("--corpus-version", default="corpus-v2")
    ci.add_argument("--seed", type=int, default=42)
    ci.add_argument(
        "--db-path",
        default=str(Path.home() / ".local" / "share" / "hippo" / "hippo.db"),
    )
    # v1 per-source counts
    ci.add_argument("--shell", type=int, default=_DEFAULT_SOURCE_COUNTS["shell"])
    ci.add_argument("--claude", type=int, default=_DEFAULT_SOURCE_COUNTS["claude"])
    ci.add_argument("--browser", type=int, default=_DEFAULT_SOURCE_COUNTS["browser"])
    ci.add_argument("--workflow", type=int, default=_DEFAULT_SOURCE_COUNTS["workflow"])
    ci.add_argument(
        "--no-filter-trivial",
        action="store_true",
        help=(
            "Disable production-eligibility filter (default: filter trivial events "
            "via hippo_brain.enrichment.is_enrichment_eligible)."
        ),
    )
    # v2 per-source minimums and time-bucketing
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
        help="Override corpus version string and force-overwrite existing corpus (v2 only).",
    )
    ci.set_defaults(func=_cmd_corpus_init)

    cv = corpus_sub.add_parser("verify", help="Re-check a fixture's content hash")
    cv.add_argument("--corpus-version", default="corpus-v2")
    cv.set_defaults(func=_cmd_corpus_verify)

    caa = corpus_sub.add_parser(
        "add-adversarial", help="Append an adversarial event to the v2 overlay"
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

    # BT-06: recovery subcommand. Idempotent — clears stale pause lockfile
    # and resumes prod brain if a prior bench was SIGKILL'd.
    recover = sub.add_parser(
        "recover",
        help="Detect a stale pause lockfile from a crashed bench and resume prod brain",
    )
    recover.add_argument(
        "--brain-url",
        default="http://localhost:8000",
        help="Prod brain base URL to resume if lockfile doesn't carry one",
    )
    recover.set_defaults(func=_cmd_recover)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
