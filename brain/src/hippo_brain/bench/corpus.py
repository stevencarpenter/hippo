"""Corpus fixture sampling, writing, loading, and verification."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import random
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hippo_brain.enrichment import is_enrichment_eligible
from hippo_brain.redaction import redact


@dataclass
class CorpusEntry:
    event_id: str
    source: str
    redacted_content: str
    reference_enrichment: dict | None = None
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        h = hashlib.sha256()
        h.update(self.source.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.redacted_content.encode("utf-8"))
        self.content_sha256 = h.hexdigest()

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "source": self.source,
                "redacted_content": self.redacted_content,
                "reference_enrichment": self.reference_enrichment,
                "content_sha256": self.content_sha256,
            },
            sort_keys=True,
        )


def compute_corpus_hash(entries: Iterable[CorpusEntry]) -> str:
    h = hashlib.sha256()
    for e in entries:
        h.update(e.content_sha256.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def write_corpus(
    entries: list[CorpusEntry],
    fixture_path: Path,
    manifest_path: Path,
    corpus_version: str,
    seed: int,
) -> None:
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    with fixture_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(e.to_json_line())
            f.write("\n")

    source_counts: dict[str, int] = {}
    for e in entries:
        source_counts[e.source] = source_counts.get(e.source, 0) + 1

    manifest: dict[str, Any] = {
        "corpus_version": corpus_version,
        "created_at_iso": _dt.datetime.now(tz=_dt.UTC).isoformat(),
        "seed": seed,
        "source_counts": source_counts,
        "event_ids_sha256": [{"event_id": e.event_id, "sha256": e.content_sha256} for e in entries],
        "corpus_content_hash": compute_corpus_hash(entries),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def load_corpus(fixture_path: Path) -> Iterable[CorpusEntry]:
    with fixture_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            entry = CorpusEntry(
                event_id=obj["event_id"],
                source=obj["source"],
                redacted_content=obj["redacted_content"],
                reference_enrichment=obj.get("reference_enrichment"),
            )
            # Verify post-load hash still matches what was recorded.
            if entry.content_sha256 != obj["content_sha256"]:
                raise ValueError(
                    f"corpus entry {obj['event_id']!r} content hash mismatch "
                    f"(stored {obj['content_sha256']} vs recomputed {entry.content_sha256})"
                )
            yield entry


def verify_corpus(fixture_path: Path, manifest_path: Path) -> tuple[bool, str]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"manifest not found: {manifest_path}"

    try:
        entries = list(load_corpus(fixture_path))
    except (FileNotFoundError, ValueError) as e:
        return False, f"corpus load failed: {e}"

    recomputed = compute_corpus_hash(entries)
    stored = manifest.get("corpus_content_hash")
    if stored != recomputed:
        return (
            False,
            f"corpus content hash mismatch (manifest {stored} vs recomputed {recomputed})",
        )
    return True, "ok"


def init_corpus(
    db_path: Path,
    fixture_path: Path,
    manifest_path: Path,
    corpus_version: str,
    source_counts: dict[str, int],
    seed: int,
) -> list[CorpusEntry]:
    """Stratified random sample from hippo.db events table.

    NOTE: This function queries a generic (id, source, payload) events table
    for hermetic testing. The real hippo.db adapter (sample_from_hippo_db)
    lives alongside this and reads the per-source tables; see Task 12.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rng = random.Random(seed)
    selected: list[CorpusEntry] = []
    for source, count in source_counts.items():
        if count <= 0:
            continue
        # bench fixtures use a different schema (payload + source columns, no probe_tag)
        rows = conn.execute(  # nosemgrep: unfiltered-event-table-select
            "SELECT id, payload FROM events WHERE source = ? ORDER BY id", (source,)
        ).fetchall()
        if not rows:
            continue
        picked = rng.sample(rows, k=min(count, len(rows)))
        picked.sort(key=lambda r: r["id"])  # stable order in fixture
        for row in picked:
            selected.append(
                CorpusEntry(
                    event_id=f"{source}-{row['id']}",
                    source=source,
                    redacted_content=row["payload"],
                    reference_enrichment=None,
                )
            )
    conn.close()

    write_corpus(selected, fixture_path, manifest_path, corpus_version, seed)
    return selected


# Per-source SELECT + payload-shape lambdas. The lambda receives a sqlite3.Row
# and returns the serialized payload that goes into CorpusEntry.redacted_content.
# A second `eligibility_dict` lambda extracts the fields needed by
# is_enrichment_eligible(). They're separate because eligibility wants raw fields
# (command, dwell_ms, ...) but the corpus stores the serialized JSON payload.
_SOURCE_QUERIES: dict[str, dict] = {
    "shell": {
        "select": (
            "SELECT id, command, stdout, stderr, duration_ms, exit_code, cwd FROM shell_events"
        ),
        "shape": lambda row: json.dumps(
            {
                "command": row["command"],
                "stdout": row["stdout"],
                "stderr": row["stderr"],
                "duration_ms": row["duration_ms"],
                "exit_code": row["exit_code"],
                "cwd": row["cwd"],
            },
            sort_keys=True,
        ),
        "eligibility_dict": lambda row: {
            "command": row["command"],
            "stdout": row["stdout"],
            "stderr": row["stderr"],
            "duration_ms": row["duration_ms"],
        },
    },
    "claude": {
        "select": (
            "SELECT id, session_id, transcript, message_count, tool_calls_json FROM claude_sessions"
        ),
        "shape": lambda row: json.dumps(
            {
                "session_id": row["session_id"],
                "transcript": row["transcript"],
                "message_count": row["message_count"],
                "tool_calls_json": row["tool_calls_json"],
            },
            sort_keys=True,
        ),
        "eligibility_dict": lambda row: {
            "message_count": row["message_count"],
            "tool_calls_json": row["tool_calls_json"],
        },
    },
    "browser": {
        "select": "SELECT id, url, title, dwell_ms, scroll_depth FROM browser_events",
        "shape": lambda row: json.dumps(
            {
                "url": row["url"],
                "title": row["title"],
                "dwell_ms": row["dwell_ms"],
                "scroll_depth": row["scroll_depth"],
            },
            sort_keys=True,
        ),
        "eligibility_dict": lambda row: {"dwell_ms": row["dwell_ms"]},
    },
    "workflow": {
        "select": (
            "SELECT id, repo, workflow_name, conclusion, annotations_json FROM workflow_runs"
        ),
        "shape": lambda row: json.dumps(
            {
                "repo": row["repo"],
                "workflow_name": row["workflow_name"],
                "conclusion": row["conclusion"],
                "annotations_json": row["annotations_json"],
            },
            sort_keys=True,
        ),
        # Workflow runs have no eligibility heuristic in production; always-eligible.
        "eligibility_dict": lambda row: {},
    },
}


def sample_from_hippo_db(
    db_path: Path,
    source_counts: dict[str, int],
    seed: int,
    filter_trivial: bool = True,
) -> list[CorpusEntry]:
    """Stratified random sample from the real hippo.db schema.

    If filter_trivial is True (default), events that the production
    enrichment pipeline would skip via `is_enrichment_eligible` are
    excluded. This mirrors what real enrichment sees and prevents the
    bench from flagging models for correctly emitting terse summaries on
    trivial inputs (which the gates would call "trivial_summary").

    If a source's table is missing (e.g., older schema), that source is
    silently skipped — a cross-version corpus shouldn't crash on a fresh
    schema migration.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rng = random.Random(seed)
    selected: list[CorpusEntry] = []
    try:
        for source, count in source_counts.items():
            if count <= 0:
                continue
            spec = _SOURCE_QUERIES[source]
            try:
                rows = conn.execute(spec["select"]).fetchall()
            except sqlite3.OperationalError:
                # Table missing — schema mismatch. Skip this source.
                continue
            if not rows:
                continue
            if filter_trivial:
                rows = [
                    r
                    for r in rows
                    if is_enrichment_eligible(spec["eligibility_dict"](r), source)[0]
                ]
                if not rows:
                    continue
            picked = rng.sample(rows, k=min(count, len(rows)))
            picked.sort(key=lambda r: r["id"])
            for row in picked:
                raw_payload = spec["shape"](row)
                redacted = redact(raw_payload)
                selected.append(
                    CorpusEntry(
                        event_id=f"{source}-{row['id']}",
                        source=source,
                        redacted_content=redacted,
                        reference_enrichment=None,
                    )
                )
    finally:
        conn.close()
    return selected
