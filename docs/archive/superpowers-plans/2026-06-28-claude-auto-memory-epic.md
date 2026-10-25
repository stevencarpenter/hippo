# Claude Auto-Memory Epic Implementation Plan

> Linear parent: SNUG-130. Execute child issues SNUG-132 through SNUG-138 as independently testable vertical slices.

## Constraints

- Claude Code's files are strictly read-only inputs; Hippo never mutates another program's datastore.
- Redaction happens before durable storage, enrichment, embeddings, logs, or diagnostics.
- The current source document is distinct from derived projections so failed enrichment leaves the last-known-good projection queryable.
- Existing local inference, sqlite-vec, FTS5, source-health, watchdog, doctor, HTTP, CLI, and MCP paths are extended rather than duplicated.

## SNUG-132: Single-file vertical slice

1. Add schema v19 with document, revision, chunk, queue, category, link, and knowledge-node link tables.
2. Add Python ingest tests first: explicit file configuration, stable identity, redaction-before-persist, deterministic Markdown chunks, one pending queue row, and hash-based no-op re-ingest.
3. Implement the ingest transaction and wire a single-file operator command.
4. Add enrichment tests first, then claim the queue through the existing local model/embedding path and atomically publish knowledge-node, FTS, vector, and chunk links.
5. Add source-filtered retrieval tests and expose `claude-auto-memory` through current query interfaces.
6. Document setup and rollback; run focused Rust/Python verification.

## SNUG-133: Revision lifecycle

1. Add failing create/update/rename/delete/history-retention tests.
2. Implement immutable revisions, tombstones, rename identity handling, configurable bounded history, and cleanup of superseded projections.
3. Verify rollback retains the current source and last-known-good projection.

## SNUG-134: Continuous reconciliation

1. Add failing tests for debounce, partial writes, hash no-op, retry/backoff, deletion, and recovery.
2. Add event-driven watching with periodic reconciliation fallback.
3. Publish derived nodes/FTS/vectors as one projection swap and expose lag/error status.

## SNUG-135: Fleet discovery

1. Add failing discovery tests for normal repos, worktrees, custom `autoMemoryDirectory`, duplicates, missing roots, and dry-run output.
2. Implement deterministic discovery with explicit privacy boundaries and per-source lifecycle/health.
3. Add configuration and operator documentation.

## SNUG-136: Categories and links

1. Add failing tests for filename/model category provenance and Markdown/wiki-link extraction.
2. Implement category and link reconciliation, including unresolved targets that resolve later.
3. Add category/link retrieval filters and provenance display.

## SNUG-137: Agent and human interfaces

1. Add failing MCP, HTTP, and CLI tests for current-by-default results and explicit history.
2. Extend existing interfaces with source/repository/category/path filters and linked-memory context.
3. Verify no raw pre-redaction content or deleted projections leak through any interface.

## SNUG-138: Reliability and operations

1. Add failing probe, watchdog, doctor, stale-lock, and crash-recovery tests.
2. Extend source health, synthetic probes, alarms, and diagnostics for discovery, ingest, queue, enrichment, and projection lag.
3. Run `mise run test`, recovery exercises, schema/rollback checks, and update the acceptance matrix and operator runbook.

