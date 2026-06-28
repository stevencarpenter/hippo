# Project architecture

## Process ownership

The Rust capture daemon owns durable source ingestion. The Python brain owns vector embeddings and enrichment. Both use the same SQLite database in WAL mode.

## Retrieval

Current knowledge is searched with FTS5 and sqlite-vec, then fused with reciprocal rank fusion. Search results retain source provenance.

## Unrelated operational inventory

The project also has browser capture, shell capture, session reconciliation, health probes, dashboard panels, release automation, configuration rendering, archive export, dependency updates, and packaging checks. These details deliberately make this document longer so retrieval strategies must isolate the relevant section instead of relying on a tiny-file advantage.

## Constraints

The system remains local and offline. Inference providers are configurable and must not be hardcoded. Every SQLite connection enables foreign keys, WAL-safe timeouts, and schema compatibility checks.
