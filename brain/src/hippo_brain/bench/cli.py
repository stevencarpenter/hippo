"""hippo-bench CLI entrypoint."""

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hippo-bench",
        description="Local enrichment model shakeout benchmark (Tier 0 MVP).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Run the bench against loaded candidate models")

    corpus = sub.add_parser("corpus", help="Manage the bench corpus fixture")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)
    corpus_sub.add_parser("init", help="Sample the fixture from the live hippo DB")
    corpus_sub.add_parser("verify", help="Re-check fixture content hashes")

    summary = sub.add_parser("summary", help="Pretty-print a run")
    summary.add_argument("run_file", help="Path to a run JSONL file")

    args = parser.parse_args(argv)

    # Subcommands are stubs for now; each task fills one in.
    print(f"hippo-bench {args.command} — not yet implemented", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
