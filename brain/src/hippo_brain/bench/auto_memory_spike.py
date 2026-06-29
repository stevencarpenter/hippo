"""Reproducible SNUG-131 auto-memory chunking and mutation spike.

This module deliberately uses a scratch in-memory SQLite database. It evaluates
document/chunk identity and FTS5 behavior without coupling the experiment to the
production Hippo schema or inference service.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hippo_brain.markdown_chunking import markdown_heading_chunks

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "be",
    "how",
    "is",
    "should",
    "the",
    "to",
    "what",
    "where",
    "which",
}


_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class Chunk:
    path: str
    ordinal: int
    heading: str
    text: str

    @property
    def chunk_id(self) -> str:
        return f"{self.path}#{self.ordinal}"


def whole_file(path: str, text: str) -> list[Chunk]:
    return [Chunk(path=path, ordinal=0, heading="", text=text.strip())]


def markdown_headings(path: str, text: str) -> list[Chunk]:
    """Split at Markdown headings using the production chunker."""
    chunks: list[Chunk] = []
    for chunk in markdown_heading_chunks(text):
        heading = chunk.heading_path.split(" > ")[-1] if chunk.heading_path else ""
        chunks.append(Chunk(path, chunk.ordinal, heading, chunk.content))
    return chunks or whole_file(path, text)


def token_windows(path: str, text: str, *, max_tokens: int = 48, overlap: int = 12) -> list[Chunk]:
    """Create deterministic word-token windows for comparison, not production tokenization."""
    if max_tokens <= 0 or overlap < 0 or overlap >= max_tokens:
        raise ValueError("require max_tokens > overlap >= 0")
    tokens = _WORD_RE.findall(text)
    if not tokens:
        return [Chunk(path, 0, "", "")]
    step = max_tokens - overlap
    return [
        Chunk(path, ordinal, "", " ".join(tokens[start : start + max_tokens]))
        for ordinal, start in enumerate(range(0, len(tokens), step))
    ]


STRATEGIES = {
    "whole-file": whole_file,
    "markdown-heading": markdown_headings,
    "token-window": token_windows,
}


class ScratchIndex:
    """Small transactional index used to characterize mutable-file semantics."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE documents (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                indexed INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                heading TEXT NOT NULL,
                text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                chunk_id UNINDEXED, path UNINDEXED, heading, text,
                tokenize='porter unicode61 remove_diacritics 2'
            );
            CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
              INSERT INTO chunks_fts(rowid, chunk_id, path, heading, text)
              VALUES (new.rowid, new.chunk_id, new.path, new.heading, new.text);
            END;
            CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
              DELETE FROM chunks_fts WHERE rowid=old.rowid;
            END;
            """
        )

    def replace(
        self, path: str, content_hash: str, chunks: list[Chunk], *, fail: bool = False
    ) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM documents WHERE path=?", (path,))
            self.conn.execute(
                "INSERT INTO documents(path, content_hash, indexed) VALUES (?, ?, 1)",
                (path, content_hash),
            )
            self.conn.executemany(
                "INSERT INTO chunks(chunk_id,path,ordinal,heading,text) VALUES (?,?,?,?,?)",
                [(c.chunk_id, c.path, c.ordinal, c.heading, c.text) for c in chunks],
            )
            if fail:
                raise RuntimeError("injected replacement failure")

    def defer(self, path: str, content_hash: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO documents(path,content_hash,indexed) VALUES (?,?,0)",
                (path, content_hash),
            )

    def delete(self, path: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM documents WHERE path=?", (path,))

    def rename(self, old_path: str, new_path: str) -> None:
        with self.conn:
            row = self.conn.execute(
                "SELECT content_hash, indexed FROM documents WHERE path=?", (old_path,)
            ).fetchone()
            if row is None:
                raise KeyError(old_path)
            chunks = self.conn.execute(
                "SELECT ordinal,heading,text FROM chunks WHERE path=? ORDER BY ordinal", (old_path,)
            ).fetchall()
            self.conn.execute("DELETE FROM documents WHERE path=?", (old_path,))
            self.conn.execute(
                "INSERT INTO documents(path,content_hash,indexed) VALUES (?,?,?)",
                (new_path, row[0], row[1]),
            )
            self.conn.executemany(
                "INSERT INTO chunks(chunk_id,path,ordinal,heading,text) VALUES (?,?,?,?,?)",
                [
                    (f"{new_path}#{ordinal}", new_path, ordinal, heading, text)
                    for ordinal, heading, text in chunks
                ],
            )

    def search(self, query: str) -> list[str]:
        terms = [t.lower() for t in _WORD_RE.findall(query) if t.lower() not in _STOP_WORDS]
        expression = " OR ".join(f'"{term}"' for term in terms)
        if not expression:
            return []
        rows = self.conn.execute(
            """
            SELECT path, MIN(rank) AS best_rank
            FROM chunks_fts
            WHERE chunks_fts MATCH ?
            GROUP BY path
            ORDER BY best_rank, path
            """,
            (expression,),
        ).fetchall()
        return [row[0] for row in rows]


def load_fixture(fixture_dir: Path) -> tuple[dict, dict[str, str]]:
    manifest = json.loads((fixture_dir / "manifest.json").read_text())
    source = fixture_dir / "source"
    documents = {
        item["path"]: (source / item["path"]).read_text() for item in manifest["documents"]
    }
    return manifest, documents


def reciprocal_rank(results: list[str], expected: str) -> float:
    try:
        return 1.0 / (results.index(expected) + 1)
    except ValueError:
        return 0.0


def evaluate(fixture_dir: Path) -> dict:
    manifest, documents = load_fixture(fixture_dir)
    report: dict[str, dict] = {}
    for name, strategy in STRATEGIES.items():
        index = ScratchIndex()
        chunk_count = 0
        for path, text in documents.items():
            chunks = strategy(path, text)
            chunk_count += len(chunks)
            index.replace(path, f"initial:{path}", chunks)
        query_rows = []
        for item in manifest["queries"]:
            results = index.search(item["query"])
            query_rows.append(
                {
                    "id": item["id"],
                    "expected": item["expected_path"],
                    "top": results[0] if results else None,
                    "rr": reciprocal_rank(results, item["expected_path"]),
                }
            )
        report[name] = {
            "chunks": chunk_count,
            "hit_at_1": sum(r["top"] == r["expected"] for r in query_rows) / len(query_rows),
            "mrr": sum(r["rr"] for r in query_rows) / len(query_rows),
            "queries": query_rows,
        }
    return report


def render_markdown(report: dict) -> str:
    lines = [
        "# Claude auto-memory chunking spike results",
        "",
        "Generated from the synthetic corpus by `mise run bench:auto-memory-spike`.",
        "This measures deterministic FTS5 behavior; semantic/vector quality requires the later live-model benchmark.",
        "",
        "| Strategy | Chunks | Hit@1 | MRR |",
        "|---|---:|---:|---:|",
    ]
    for name, data in report.items():
        lines.append(f"| {name} | {data['chunks']} | {data['hit_at_1']:.3f} | {data['mrr']:.3f} |")
    lines.extend(["", "## Per-query results", ""])
    for name, data in report.items():
        lines.append(f"### {name}")
        lines.append("")
        for row in data["queries"]:
            lines.append(
                f"- `{row['id']}`: expected `{row['expected']}`, top `{row['top']}`, reciprocal rank {row['rr']:.3f}"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.fixture)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown(report) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
