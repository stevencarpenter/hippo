"""Deterministic and model-derived categories plus MEMORY.md index links."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

INDEX_LOGICAL_PATH = "MEMORY.md"
KNOWN_CATEGORIES = frozenset({"user", "feedback", "project", "reference", "index"})
FILENAME_PREFIX_CATEGORIES = (
    ("feedback_", "feedback"),
    ("project_", "project"),
    ("reference_", "reference"),
    ("user_", "user"),
)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


@dataclass(frozen=True)
class MarkdownLink:
    anchor_text: str
    href: str


@dataclass(frozen=True)
class CategoryRecord:
    category: str
    source: str
    confidence: float | None
    model: str | None
    enrichment_version: int | None
    created_at: int
    updated_at: int


def category_from_filename(logical_path: str) -> str | None:
    """Return a deterministic category from the memory filename, if any."""
    name = PurePosixPath(logical_path).name
    if name == INDEX_LOGICAL_PATH:
        return "index"
    stem = PurePosixPath(name).stem.lower()
    if stem == "user":
        return "user"
    lowered = name.lower()
    for prefix, category in FILENAME_PREFIX_CATEGORIES:
        if lowered.startswith(prefix):
            return category
    return None


def validate_memory_category_filter(category: str) -> None:
    """Reject unknown memory category filters (mirrors source filter validation)."""
    if category not in KNOWN_CATEGORIES:
        raise ValueError(f"unknown memory category filter: {category!r}")


def extract_markdown_links(markdown: str) -> list[MarkdownLink]:
    """Extract inline Markdown links from redacted content."""
    links: list[MarkdownLink] = []
    for match in _MARKDOWN_LINK_RE.finditer(markdown):
        href = match.group(2).strip()
        if href:
            links.append(MarkdownLink(anchor_text=match.group(1).strip(), href=href))
    return links


def _memory_directory(source_path: Path) -> Path:
    return source_path.expanduser().resolve().parent


def _normalize_href(href: str) -> str:
    return href.strip().split("#", 1)[0].strip()


def classify_link_resolution(
    href: str,
    *,
    memory_dir: Path,
    source_document_id: int,
    repository: str,
    conn: sqlite3.Connection,
) -> tuple[str, str, int | None]:
    """Return (target_logical_path, resolution, target_document_id)."""
    normalized = _normalize_href(href)
    if not normalized or normalized.startswith("#"):
        return normalized or href, "external", None
    lowered = normalized.lower()
    if lowered.startswith(("http://", "https://", "mailto:")):
        return normalized, "external", None

    target_path = (memory_dir / normalized).resolve()
    try:
        target_path.relative_to(memory_dir.resolve())
    except ValueError:
        return PurePosixPath(normalized).name or normalized, "external", None

    logical_path = PurePosixPath(normalized).name
    if not logical_path:
        return normalized, "external", None

    rows = conn.execute(
        "SELECT id FROM memory_documents WHERE repository = ? AND logical_path = ? "
        "AND state = 'active'",
        (repository, logical_path),
    ).fetchall()
    if len(rows) > 1:
        return logical_path, "ambiguous", None
    if len(rows) == 1:
        target_id = int(rows[0][0])
        if target_id == source_document_id:
            return logical_path, "circular", None
        if _would_create_cycle(conn, source_document_id, target_id):
            return logical_path, "circular", target_id
        return logical_path, "resolved", target_id
    if target_path.is_file():
        return logical_path, "unresolved", None
    return logical_path, "unresolved", None


def _would_create_cycle(
    conn: sqlite3.Connection, source_document_id: int, target_document_id: int
) -> bool:
    """True when following resolved links from target reaches source."""
    seen = {source_document_id}
    frontier = [target_document_id]
    while frontier:
        current = frontier.pop()
        if current in seen:
            return True
        seen.add(current)
        next_ids = [
            int(row[0])
            for row in conn.execute(
                "SELECT DISTINCT target_document_id FROM memory_document_links "
                "WHERE source_document_id = ? AND resolution = 'resolved' "
                "AND target_document_id IS NOT NULL",
                (current,),
            ).fetchall()
        ]
        frontier.extend(next_ids)
    return False


def upsert_filename_category(
    conn: sqlite3.Connection,
    document_id: int,
    logical_path: str,
    *,
    now_ms: int,
) -> str | None:
    """Upsert the deterministic filename category; returns category or None."""
    category = category_from_filename(logical_path)
    if category is None:
        conn.execute(
            "DELETE FROM memory_document_categories WHERE document_id = ? AND source = 'filename'",
            (document_id,),
        )
        return None
    conn.execute(
        "INSERT INTO memory_document_categories "
        "(document_id, category, source, confidence, model, enrichment_version, "
        " created_at, updated_at) VALUES (?, ?, 'filename', 1.0, NULL, NULL, ?, ?) "
        "ON CONFLICT(document_id, category, source) DO UPDATE SET "
        "updated_at = excluded.updated_at",
        (document_id, category, now_ms, now_ms),
    )
    return category


def replace_model_categories(
    conn: sqlite3.Connection,
    document_id: int,
    categories: list[str],
    *,
    model_name: str,
    enrichment_version: int = 1,
    now_ms: int,
    default_confidence: float = 0.8,
) -> None:
    """Replace model-derived categories for a document."""
    conn.execute(
        "DELETE FROM memory_document_categories WHERE document_id = ? AND source = 'model'",
        (document_id,),
    )
    for raw in categories:
        category = raw.strip().lower()
        if not category or category not in KNOWN_CATEGORIES or category == "index":
            continue
        conn.execute(
            "INSERT INTO memory_document_categories "
            "(document_id, category, source, confidence, model, enrichment_version, "
            " created_at, updated_at) VALUES (?, ?, 'model', ?, ?, ?, ?, ?) "
            "ON CONFLICT(document_id, category, source) DO UPDATE SET "
            "confidence = excluded.confidence, model = excluded.model, "
            "enrichment_version = excluded.enrichment_version, updated_at = excluded.updated_at",
            (
                document_id,
                category,
                default_confidence,
                model_name,
                enrichment_version,
                now_ms,
                now_ms,
            ),
        )


def reconcile_index_links(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    revision_id: int,
    logical_path: str,
    repository: str,
    source_path: Path,
    redacted_content: str,
    now_ms: int,
) -> int:
    """Rebuild MEMORY.md index links for one revision. Returns link count."""
    if logical_path != INDEX_LOGICAL_PATH:
        return 0
    memory_dir = _memory_directory(source_path)
    conn.execute(
        "DELETE FROM memory_document_links WHERE source_document_id = ?",
        (document_id,),
    )
    inserted = 0
    for link in extract_markdown_links(redacted_content):
        target_logical_path, resolution, target_document_id = classify_link_resolution(
            link.href,
            memory_dir=memory_dir,
            source_document_id=document_id,
            repository=repository,
            conn=conn,
        )
        conn.execute(
            "INSERT INTO memory_document_links "
            "(source_document_id, source_revision_id, target_document_id, "
            " target_logical_path, anchor_text, resolution, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                revision_id,
                target_document_id,
                target_logical_path,
                link.anchor_text,
                resolution,
                now_ms,
                now_ms,
            ),
        )
        inserted += 1
    return inserted


def retry_unresolved_links(
    conn: sqlite3.Connection,
    *,
    repository: str,
    memory_dir: Path,
    now_ms: int,
) -> int:
    """Re-resolve pending index links after a new topic file lands."""
    rows = conn.execute(
        "SELECT mdl.id, mdl.source_document_id, mdl.target_logical_path "
        "FROM memory_document_links mdl "
        "JOIN memory_documents md ON md.id = mdl.source_document_id "
        "WHERE md.repository = ? AND mdl.resolution = 'unresolved'",
        (repository,),
    ).fetchall()
    updated = 0
    for link_id, source_document_id, target_logical_path in rows:
        target_path = memory_dir / target_logical_path
        if not target_path.is_file():
            continue
        target_rows = conn.execute(
            "SELECT id FROM memory_documents WHERE repository = ? AND logical_path = ? "
            "AND state = 'active'",
            (repository, target_logical_path),
        ).fetchall()
        if len(target_rows) != 1:
            resolution = "ambiguous" if len(target_rows) > 1 else "unresolved"
            conn.execute(
                "UPDATE memory_document_links SET resolution = ?, updated_at = ? WHERE id = ?",
                (resolution, now_ms, link_id),
            )
            continue
        target_document_id = int(target_rows[0][0])
        resolution = "circular"
        if target_document_id != source_document_id and not _would_create_cycle(
            conn, source_document_id, target_document_id
        ):
            resolution = "resolved"
        conn.execute(
            "UPDATE memory_document_links SET target_document_id = ?, resolution = ?, "
            "updated_at = ? WHERE id = ?",
            (target_document_id, resolution, now_ms, link_id),
        )
        updated += 1
    return updated


def reconcile_document_taxonomy(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    revision_id: int,
    repository: str,
    logical_path: str,
    source_path: Path,
    redacted_content: str,
    now_ms: int,
    content_changed: bool,
) -> None:
    """Refresh filename categories and index links after ingest."""
    upsert_filename_category(conn, document_id, logical_path, now_ms=now_ms)
    missing_index_links = False
    if logical_path == INDEX_LOGICAL_PATH:
        missing_index_links = (
            conn.execute(
                "SELECT COUNT(*) FROM memory_document_links WHERE source_document_id = ?",
                (document_id,),
            ).fetchone()[0]
            == 0
        )
    should_reconcile_links = content_changed or missing_index_links
    if should_reconcile_links:
        body = redacted_content
        if not body:
            row = conn.execute(
                "SELECT redacted_content FROM memory_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            body = row[0] if row is not None and row[0] else ""
        reconcile_index_links(
            conn,
            document_id=document_id,
            revision_id=revision_id,
            logical_path=logical_path,
            repository=repository,
            source_path=source_path,
            redacted_content=body,
            now_ms=now_ms,
        )
    if content_changed or missing_index_links:
        retry_unresolved_links(
            conn,
            repository=repository,
            memory_dir=_memory_directory(source_path),
            now_ms=now_ms,
        )


def list_document_categories(conn: sqlite3.Connection, document_id: int) -> list[CategoryRecord]:
    rows = conn.execute(
        "SELECT category, source, confidence, model, enrichment_version, created_at, updated_at "
        "FROM memory_document_categories WHERE document_id = ? ORDER BY category, source",
        (document_id,),
    ).fetchall()
    return [
        CategoryRecord(
            category=row[0],
            source=row[1],
            confidence=row[2],
            model=row[3],
            enrichment_version=row[4],
            created_at=int(row[5]),
            updated_at=int(row[6]),
        )
        for row in rows
    ]


def list_document_links(conn: sqlite3.Connection, document_id: int) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT target_logical_path, anchor_text, resolution, target_document_id, "
        "source_revision_id, created_at, updated_at "
        "FROM memory_document_links WHERE source_document_id = ? "
        "ORDER BY target_logical_path",
        (document_id,),
    ).fetchall()
    return [
        {
            "target_logical_path": row[0],
            "anchor_text": row[1],
            "resolution": row[2],
            "target_document_id": row[3],
            "source_revision_id": row[4],
            "created_at": row[5],
            "updated_at": row[6],
        }
        for row in rows
    ]
