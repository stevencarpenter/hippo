"""Ingest path for Claude Code auto-memory Markdown files."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from hippo_brain.auto_memory_constants import (
    CHUNKER_NAME,
    CHUNKER_VERSION,
    PROBE_REPOSITORY,
    SOURCE_KIND,
    _IDENTITY_NAMESPACE,
)
from hippo_brain.auto_memory_lifecycle import (
    RevisionRetention,
    finalize_superseded_revision,
    prune_document_revisions,
    try_resolve_rename,
)
from hippo_brain.auto_memory_categories import reconcile_document_taxonomy
from hippo_brain.markdown_chunking import MarkdownChunk, markdown_heading_chunks
from hippo_brain.redaction import redact


@dataclass(frozen=True)
class IngestResult:
    document_id: int
    document_uuid: str
    revision_id: int
    revision_number: int
    changed: bool
    chunk_count: int


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_repository_identity(source_path: Path | str, explicit: str | None = None) -> str:
    """Return an explicit identity, a sanitized Git remote, or a documented local fallback."""
    if explicit and explicit.strip():
        return explicit.strip()
    directory = Path(source_path).expanduser().resolve().parent
    try:
        root_result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        root = Path(root_result.stdout.strip()).resolve()
        remote_result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        remote = remote_result.stdout.strip()
        if remote:
            if ":" in remote and "://" not in remote:
                host, path = remote.split(":", 1)
                host = host.rsplit("@", 1)[-1]
            else:
                parsed = urlsplit(remote)
                host = parsed.hostname or "git"
                path = parsed.path
            clean_path = path.strip("/")
            if clean_path.endswith(".git"):
                clean_path = clean_path[:-4]
            if clean_path:
                return f"{host}/{clean_path}"
        return f"local-git:{root}"
    except FileNotFoundError, subprocess.SubprocessError, OSError, ValueError:
        return f"local:{directory}"


def _chunks(markdown: str) -> list[MarkdownChunk]:
    return markdown_heading_chunks(markdown)


def _apply_taxonomy(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    revision_id: int,
    repository: str,
    logical_path: str,
    path: Path,
    redacted: str | None,
    changed: bool,
    now_ms: int,
) -> None:
    reconcile_document_taxonomy(
        conn,
        document_id=document_id,
        revision_id=revision_id,
        repository=repository,
        logical_path=logical_path,
        source_path=path,
        redacted_content=redacted or "",
        now_ms=now_ms,
        content_changed=changed,
    )


def ingest_memory_file(
    conn: sqlite3.Connection,
    source_path: Path | str,
    *,
    repository: str | None = None,
    logical_path: str | None = None,
    now_ms: int | None = None,
    retention: RevisionRetention | None = None,
) -> IngestResult:
    """Read, redact, version, chunk, and enqueue one explicit memory file.

    The source is never opened for writing. Only redacted text and hashes of
    redacted text cross the SQLite durability boundary.
    """
    path = Path(source_path).expanduser()
    if not path.is_file():
        raise ValueError(f"auto-memory source must be an explicit regular file: {path}")

    stat = path.stat()
    resolved = str(path.resolve())
    identity_path = logical_path or path.name

    # Fast path: a poll fires every 60s, but memory files rarely change. When the
    # on-disk mtime(ms)+size still match the document's current revision, the
    # content is unchanged, so skip the Git identity derivation (two subprocesses),
    # the full read, the regex redact, and the double hash entirely. mtime+size is
    # the same cheap proxy the codex/cursor pollers use; logical_path is resolved
    # without Git so this lookup stays cheap. Any real edit changes mtime or size
    # and falls through to the authoritative content-hash path below.
    #
    # The authoritative document identity is (source_kind, repository, logical_path),
    # but the fast path keys on source_path to avoid the Git derivation. When an
    # explicit repository is configured we additionally scope by it — cheaply, no
    # Git — so two sources sharing one path but declaring different repositories
    # don't alias onto the first document and silently suppress the second. When
    # the repository is auto-derived (None), the same path yields the same identity,
    # so the path-keyed match is already unambiguous.
    explicit_repo = repository.strip() if repository and repository.strip() else None
    unchanged = conn.execute(
        "SELECT d.id, d.uuid, r.id, r.revision_number, r.source_hash, d.repository "
        "FROM memory_documents d JOIN memory_revisions r ON r.id = d.current_revision_id "
        "WHERE d.source_kind = ? AND d.source_path = ? AND d.logical_path = ? "
        "AND (? IS NULL OR d.repository = ?) "
        "AND d.state = 'active' AND r.source_mtime_ms = ? AND r.source_size = ?",
        (
            SOURCE_KIND,
            resolved,
            identity_path,
            explicit_repo,
            explicit_repo,
            int(stat.st_mtime * 1000),
            stat.st_size,
        ),
    ).fetchone()
    if unchanged is not None:
        doc_id, doc_uuid, rev_id, rev_num, stored_source_hash, stored_repository = unchanged
        source_text = path.read_text(encoding="utf-8")
        if _sha256(source_text) == stored_source_hash:
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE revision_id = ?", (rev_id,)
            ).fetchone()[0]
            _apply_taxonomy(
                conn,
                document_id=int(doc_id),
                revision_id=int(rev_id),
                repository=stored_repository,
                logical_path=identity_path,
                path=path,
                redacted=None,
                changed=False,
                now_ms=now_ms if now_ms is not None else int(time.time() * 1000),
            )
            conn.commit()
            return IngestResult(
                int(doc_id), doc_uuid, int(rev_id), int(rev_num), False, int(chunk_count)
            )

    repository_identity = derive_repository_identity(path, repository)

    source = path.read_text(encoding="utf-8")
    source_hash = _sha256(source)
    redacted = redact(source)
    content_hash = _sha256(redacted)
    observed_at = now_ms if now_ms is not None else int(time.time() * 1000)
    document_uuid = str(
        uuid.uuid5(_IDENTITY_NAMESPACE, f"{SOURCE_KIND}\0{repository_identity}\0{identity_path}")
    )
    retention_policy = retention or RevisionRetention()

    renamed_document_id = try_resolve_rename(
        conn,
        repository=repository_identity,
        logical_path=identity_path,
        content_hash=content_hash,
        source_path=resolved,
        observed_at=observed_at,
    )
    if renamed_document_id is not None:
        row = conn.execute(
            "SELECT d.uuid, d.current_revision_id, r.revision_number FROM memory_documents d "
            "JOIN memory_revisions r ON r.id = d.current_revision_id WHERE d.id = ?",
            (renamed_document_id,),
        ).fetchone()
        if row is not None:
            doc_uuid, revision_id, revision_number = row[0], int(row[1]), int(row[2])
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE revision_id = ?", (revision_id,)
            ).fetchone()[0]
            prune_document_revisions(
                conn, renamed_document_id, retention_policy, now_ms=observed_at
            )
            _apply_taxonomy(
                conn,
                document_id=renamed_document_id,
                revision_id=revision_id,
                repository=repository_identity,
                logical_path=identity_path,
                path=path,
                redacted=None,
                changed=False,
                now_ms=observed_at,
            )
            conn.commit()
            return IngestResult(
                renamed_document_id,
                doc_uuid,
                revision_id,
                revision_number,
                False,
                int(chunk_count),
            )

    with conn:
        row = conn.execute(
            "SELECT id, current_revision_id FROM memory_documents "
            "WHERE source_kind = ? AND repository = ? AND logical_path = ?",
            (SOURCE_KIND, repository_identity, identity_path),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT INTO memory_documents "
                "(uuid, source_kind, repository, logical_path, source_path, state, "
                " projection_status, observed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', 'pending', ?, ?, ?)",
                (
                    document_uuid,
                    SOURCE_KIND,
                    repository_identity,
                    identity_path,
                    resolved,
                    observed_at,
                    observed_at,
                    observed_at,
                ),
            )
            document_id = int(cursor.lastrowid)
            current_revision_id = None
        else:
            document_id, current_revision_id = int(row[0]), row[1]
            conn.execute(
                "UPDATE memory_documents SET source_path = ?, state = 'active', "
                "observed_at = ?, updated_at = ?, tombstoned_at = NULL WHERE id = ?",
                (resolved, observed_at, observed_at, document_id),
            )

        if current_revision_id is not None:
            current = conn.execute(
                "SELECT id, revision_number, content_hash, redacted_content FROM memory_revisions "
                "WHERE id = ?",
                (current_revision_id,),
            ).fetchone()
            if current is not None and current[2] == content_hash:
                chunk_count = conn.execute(
                    "SELECT COUNT(*) FROM memory_chunks WHERE revision_id = ?", (current[0],)
                ).fetchone()[0]
                _apply_taxonomy(
                    conn,
                    document_id=document_id,
                    revision_id=int(current[0]),
                    repository=repository_identity,
                    logical_path=identity_path,
                    path=path,
                    redacted=current[3],
                    changed=False,
                    now_ms=observed_at,
                )
                return IngestResult(
                    document_id,
                    document_uuid,
                    int(current[0]),
                    int(current[1]),
                    False,
                    int(chunk_count),
                )

        previous_redacted: str | None = None
        if current_revision_id is not None:
            previous = conn.execute(
                "SELECT redacted_content FROM memory_revisions WHERE id = ?",
                (current_revision_id,),
            ).fetchone()
            if previous is not None and previous[0]:
                previous_redacted = previous[0]

        revision_number = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM memory_revisions "
                "WHERE document_id = ?",
                (document_id,),
            ).fetchone()[0]
        )
        cursor = conn.execute(
            "INSERT INTO memory_revisions "
            "(document_id, revision_number, content_hash, source_hash, redacted_content, "
            " source_mtime_ms, source_size, change_kind, chunker_name, chunker_version, "
            " chunker_config_json, enrichment_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                document_id,
                revision_number,
                content_hash,
                source_hash,
                redacted,
                int(stat.st_mtime * 1000),
                stat.st_size,
                "create" if revision_number == 1 else "update",
                CHUNKER_NAME,
                CHUNKER_VERSION,
                json.dumps({"boundary": "heading", "retain_heading": True}, sort_keys=True),
                observed_at,
            ),
        )
        revision_id = int(cursor.lastrowid)
        chunks = _chunks(redacted)
        conn.executemany(
            "INSERT INTO memory_chunks "
            "(revision_id, ordinal, heading_path, start_offset, end_offset, content, "
            " content_hash, token_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    revision_id,
                    chunk.ordinal,
                    chunk.heading_path,
                    chunk.start_offset,
                    chunk.end_offset,
                    chunk.content,
                    _sha256(chunk.content),
                    len(chunk.content.split()),
                    observed_at,
                )
                for chunk in chunks
            ],
        )
        is_probe = repository_identity == PROBE_REPOSITORY
        if not is_probe:
            conn.execute(
                "INSERT INTO memory_enrichment_queue "
                "(revision_id, status, priority, retry_count, max_retries, enqueued_at, updated_at) "
                "VALUES (?, 'pending', 5, 0, 5, ?, ?)",
                (revision_id, observed_at, observed_at),
            )
            projection_status = "pending"
        else:
            projection_status = "ready"
        conn.execute(
            "UPDATE memory_documents SET current_revision_id = ?, projection_status = ?, "
            "last_error = NULL, updated_at = ? WHERE id = ?",
            (revision_id, projection_status, observed_at, document_id),
        )
        if not is_probe:
            conn.execute(
                "UPDATE source_health SET last_event_ts = ?, last_success_ts = ?, "
                "last_error_msg = NULL, last_error_ts = NULL, "
                "consecutive_failures = 0, updated_at = ? WHERE source = ?",
                (observed_at, observed_at, observed_at, SOURCE_KIND),
            )
        if previous_redacted is not None and current_revision_id is not None:
            finalize_superseded_revision(
                conn,
                int(current_revision_id),
                old_redacted=previous_redacted,
                new_redacted=redacted,
            )
        _apply_taxonomy(
            conn,
            document_id=document_id,
            revision_id=revision_id,
            repository=repository_identity,
            logical_path=identity_path,
            path=path,
            redacted=redacted,
            changed=True,
            now_ms=observed_at,
        )

    prune_document_revisions(conn, document_id, retention_policy, now_ms=observed_at)
    conn.commit()

    return IngestResult(
        document_id,
        document_uuid,
        revision_id,
        revision_number,
        True,
        len(chunks),
    )
