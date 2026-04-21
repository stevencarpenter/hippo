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
        rows = conn.execute(
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
