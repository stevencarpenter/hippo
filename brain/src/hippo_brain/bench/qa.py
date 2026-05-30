from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class QaValidationReport:
    total: int
    scoreable: int
    unscoreable: int
    min_scoreable: int
    missing_by_qa_id: dict[str, str | None]

    @property
    def passes(self) -> bool:
        return self.scoreable >= self.min_scoreable

    @property
    def detail(self) -> str:
        if self.passes:
            return (
                f"scoreable Q/A items: {self.scoreable}/{self.total} (minimum {self.min_scoreable})"
            )
        return (
            f"need at least {self.min_scoreable} scoreable Q/A items; "
            f"found {self.scoreable}/{self.total}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "scoreable": self.scoreable,
            "unscoreable": self.unscoreable,
            "min_scoreable": self.min_scoreable,
            "missing_by_qa_id": dict(self.missing_by_qa_id),
            "passes": self.passes,
            "detail": self.detail,
        }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError(f"{path}: expected JSON object per line")
                items.append(obj)
    return items


def collect_corpus_event_ids(corpus_sqlite: Path) -> set[str]:
    specs = [
        ("shell", "events", "id"),
        ("claude", "claude_sessions", "id"),
        ("browser", "browser_events", "id"),
        ("workflow", "workflow_runs", "id"),
    ]
    ids: set[str] = set()
    with contextlib.closing(sqlite3.connect(f"file:{corpus_sqlite}?mode=ro", uri=True)) as conn:
        for prefix, table, id_col in specs:
            try:
                rows = conn.execute(f"SELECT {id_col} FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            ids.update(f"{prefix}-{row[0]}" for row in rows if row[0] is not None)
    return ids


def validate_qa_fixture(
    qa_path: Path,
    corpus_sqlite: Path,
    *,
    min_scoreable: int,
) -> QaValidationReport:
    items = _load_jsonl(qa_path)
    corpus_ids = collect_corpus_event_ids(corpus_sqlite)
    missing: dict[str, str | None] = {}
    scoreable = 0
    for idx, item in enumerate(items, start=1):
        qa_id = str(item.get("qa_id") or item.get("id") or f"line-{idx}")
        golden = item.get("golden_event_id")
        if isinstance(golden, str) and golden in corpus_ids:
            scoreable += 1
        else:
            missing[qa_id] = golden if isinstance(golden, str) else None
    return QaValidationReport(
        total=len(items),
        scoreable=scoreable,
        unscoreable=len(items) - scoreable,
        min_scoreable=min_scoreable,
        missing_by_qa_id=missing,
    )


def export_label_worklist(qa_path: Path, corpus_sqlite: Path, out_path: Path) -> int:
    items = _load_jsonl(qa_path)
    corpus_ids = sorted(collect_corpus_event_ids(corpus_sqlite))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as f:
        for idx, item in enumerate(items, start=1):
            golden = item.get("golden_event_id")
            if isinstance(golden, str) and golden in corpus_ids:
                continue
            source_filter = item.get("source_filter")
            candidates = [
                event_id
                for event_id in corpus_ids
                if not isinstance(source_filter, str) or event_id.startswith(f"{source_filter}-")
            ]
            f.write(
                json.dumps(
                    {
                        "qa_id": item.get("qa_id") or item.get("id") or f"line-{idx}",
                        "question": item.get("question"),
                        "source_filter": source_filter,
                        "current_golden_event_id": golden,
                        "candidate_event_ids": candidates,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            written += 1
    return written
