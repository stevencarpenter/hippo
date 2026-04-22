"""hippo-bench CLI entrypoint.

Subcommands:
  run            — run the bench against one or more candidate models
  corpus init    — sample a fixture from the live hippo DB
  corpus verify  — re-check a fixture against its manifest
  summary        — pretty-print a run JSONL file as a text table

See `docs/superpowers/specs/2026-04-21-hippo-bench-design.md` for the
full design and `brain/src/hippo_brain/bench/README.md` for an
operator-facing onboarding guide.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import platform
from pathlib import Path

from hippo_brain.bench.corpus import (
    sample_from_hippo_db,
    verify_corpus,
    write_corpus,
)
from hippo_brain.bench.orchestrate import orchestrate_run
from hippo_brain.bench.paths import corpus_manifest_path, corpus_path, runs_dir

# Default corpus stratification — mirrors the spec's "shakeout" sizing.
# Override via --shell, --claude, --browser, --workflow on `corpus init`.
_DEFAULT_SOURCE_COUNTS = {"shell": 15, "claude": 12, "browser": 10, "workflow": 3}


def _cmd_corpus_init(args: argparse.Namespace) -> int:
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
    fixture = corpus_path(args.corpus_version)
    manifest = corpus_manifest_path(args.corpus_version)
    ok, detail = verify_corpus(fixture, manifest)
    print(detail)
    return 0 if ok else 1


def _cmd_run(args: argparse.Namespace) -> int:
    fixture = corpus_path(args.corpus_version)
    manifest = corpus_manifest_path(args.corpus_version)
    ts = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%S")
    out = (
        Path(args.out) if args.out else runs_dir(create=True) / f"run-{ts}-{platform.node()}.jsonl"
    )
    models = [m.strip() for m in args.models.split(",") if m.strip()]
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
    run.add_argument("--corpus-version", default="corpus-v1")
    run.add_argument("--base-url", default="http://localhost:1234/v1")
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
    run.set_defaults(func=_cmd_run)

    corpus = sub.add_parser("corpus", help="Manage the bench fixture")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)
    ci = corpus_sub.add_parser("init", help="Sample a fresh fixture from hippo.db")
    ci.add_argument("--corpus-version", default="corpus-v1")
    ci.add_argument("--seed", type=int, default=42)
    ci.add_argument(
        "--db-path",
        default=str(Path.home() / ".local" / "share" / "hippo" / "hippo.db"),
    )
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
    ci.set_defaults(func=_cmd_corpus_init)
    cv = corpus_sub.add_parser("verify", help="Re-check a fixture's content hash")
    cv.add_argument("--corpus-version", default="corpus-v1")
    cv.set_defaults(func=_cmd_corpus_verify)

    summary = sub.add_parser("summary", help="Pretty-print a run JSONL file")
    summary.add_argument("run_file")
    summary.set_defaults(func=_cmd_summary)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
