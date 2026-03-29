#!/usr/bin/env python3
"""Format SQL files using sqlparse with sensible defaults for SQLite.

Usage:
  python tools/format_sql.py path/to/schema.sql [other files...]

This script is intentionally small and depends on `sqlparse` which should be
installed in the project's Python environment (add to dev deps).
"""
import argparse
import sys
from pathlib import Path

try:
    import sqlparse
except Exception as e:
    print("Missing dependency 'sqlparse'. Install it in the brain dev env:")
    print("  uv run --project brain pip install sqlparse")
    raise


def format_file(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    # Format with reindent and uppercase keywords. Do NOT force line wrapping;
    # the user said they don't care about long lines.
    formatted = sqlparse.format(
        text,
        reindent=True,
        keyword_case="upper",
        identifier_case=None,
        strip_comments=False,
        use_space_around_operators=True,
    )

    if formatted != text:
        path.write_text(formatted, encoding="utf-8")
        print(f"Formatted: {path}")
        return 1
    else:
        print(f"Unchanged: {path}")
        return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Format SQL files for the project")
    parser.add_argument("files", nargs="+", help="SQL files to format")
    args = parser.parse_args(argv[1:])

    exit_code = 0
    for f in args.files:
        p = Path(f)
        if not p.exists():
            print(f"File not found: {p}")
            exit_code = 2
            continue
        try:
            changed = format_file(p)
            if changed:
                exit_code = 1
        except Exception as e:
            print(f"Failed to format {p}: {e}")
            exit_code = 2

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

