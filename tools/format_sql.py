#!/usr/bin/env -S uv run --project brain python
## /// script
## requires-python = ">=3.14"
## dependencies = [
##     "sqlfluff",
## ]
## ///

"""Format SQL files using sqlfluff with sensible defaults for SQLite.

Usage:
  python tools/format_sql.py path/to/schema.sql [other files...]

This script is intentionally small and depends on `sqlfluff` which should be
installed in the project's Python environment (add to dev deps).
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import sqlfluff  # noqa: F401
except Exception:
    sqlfluff = None


def run_sqlfluff_fix(path: Path) -> int:
    # Prefer using sqlfluff if available on PATH (installed in the project's env)
    sqlfluff_exe = shutil.which("sqlfluff")
    if not sqlfluff_exe:
        return 2

    # Run `sqlfluff fix --force <file>`; --force overwrites the file
    cmd = [sqlfluff_exe, "fix", "--dialect", "sqlite", "--force", str(path)]
    try:
        proc = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode == 0:
            print(f"sqlfluff fixed: {path}")
            return 0
        else:
            print(f"sqlfluff failed ({proc.returncode}) for {path}: {proc.stderr.strip()}")
            return 2
    except Exception as e:
        print(f"Failed to run sqlfluff: {e}")
        return 2


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Format SQL files for the project (sqlfluff preferred)")
    parser.add_argument("files", nargs="+", help="SQL files to format")
    args = parser.parse_args(argv[1:])

    exit_code = 0
    for f in args.files:
        p = Path(f)
        if not p.exists():
            print(f"File not found: {p}")
            exit_code = 2
            continue

        # Try sqlfluff first (preferred for consistent team formatting)
        rc = run_sqlfluff_fix(p)
        if rc == 0:
            continue

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
