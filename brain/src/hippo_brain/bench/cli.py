"""hippo-bench CLI entrypoint."""

from __future__ import annotations

import argparse
import datetime as _dt
import platform
import sys
from pathlib import Path

from hippo_brain.bench.corpus import (
    sample_from_hippo_db,
    verify_corpus,
    write_corpus,
)
from hippo_brain.bench.orchestrate import orchestrate_run
from hippo_brain.bench.paths import corpus_manifest_path, corpus_path, runs_dir


def _cmd_corpus_init(args: argparse.Namespace) -> int:
    fixture = corpus_path(args.corpus_version)
    manifest = corpus_manifest_path(args.corpus_version)
    counts = {"shell": 15, "claude": 12, "browser": 10, "workflow": 3}
    entries = sample_from_hippo_db(db_path=Path(args.db_path), source_counts=counts, seed=args.seed)
    write_corpus(entries, fixture, manifest, args.corpus_version, args.seed)
    print(f"wrote {len(entries)} entries to {fixture}")
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
    models = args.models.split(",") if args.models else []
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
    )
    print(f"run_id={result.run_id} out={result.out_path}")
    return 0 if not result.preflight_aborted else 2


def _cmd_summary(args: argparse.Namespace) -> int:
    # Implemented in Task 19.
    print("hippo-bench summary — implemented in Task 19", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hippo-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--models", default="")
    run.add_argument("--corpus-version", default="corpus-v1")
    run.add_argument("--base-url", default="http://localhost:1234/v1")
    run.add_argument("--embedding-model", default="text-embedding-nomic-embed-text-v2-moe")
    run.add_argument("--latency-ceiling-sec", type=int, default=60)
    run.add_argument("--self-consistency-events", type=int, default=5)
    run.add_argument("--self-consistency-runs", type=int, default=5)
    run.add_argument("--skip-checks", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--out")
    run.set_defaults(func=_cmd_run)

    corpus = sub.add_parser("corpus")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)
    ci = corpus_sub.add_parser("init")
    ci.add_argument("--corpus-version", default="corpus-v1")
    ci.add_argument("--seed", type=int, default=42)
    ci.add_argument(
        "--db-path",
        default=str(Path.home() / ".local" / "share" / "hippo" / "hippo.db"),
    )
    ci.set_defaults(func=_cmd_corpus_init)
    cv = corpus_sub.add_parser("verify")
    cv.add_argument("--corpus-version", default="corpus-v1")
    cv.set_defaults(func=_cmd_corpus_verify)

    summary = sub.add_parser("summary")
    summary.add_argument("run_file")
    summary.set_defaults(func=_cmd_summary)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
