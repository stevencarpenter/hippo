import sqlite3
from pathlib import Path


def assert_corpus_schema_version(bench_db: Path, expected_schema_version: int) -> None:
    """Read corpus_meta.schema_version from the bench SQLite; raise RuntimeError on mismatch."""
    conn = sqlite3.connect(bench_db)
    try:
        row = conn.execute("SELECT schema_version FROM corpus_meta").fetchone()
        if row is None:
            raise RuntimeError("corpus_meta table missing or empty — rebuild corpus")
        stored = row[0]
        if stored != expected_schema_version:
            raise RuntimeError(
                f"corpus schema version mismatch: bench corpus has schema_version={stored}, "
                f"live hippo has schema_version={expected_schema_version}. "
                "Rebuild corpus with: hippo-bench corpus init --bump-version"
            )
    finally:
        conn.close()
