# hippo-core

Shared Rust library for the Hippo system. Provides types, configuration loading, SQLite storage, secret redaction, and
the protocol definition used between the shell hook and daemon.

## Modules

| Module         | Purpose                                                          |
|----------------|------------------------------------------------------------------|
| `config.rs`    | Loads and validates `config.toml` / `redact.toml`                |
| `storage.rs`   | SQLite operations (events, sessions, enrichment queue, entities) |
| `events.rs`    | Event type definitions                                           |
| `protocol.rs`  | Message type definitions for daemon request/response protocol    |
| `redaction.rs` | Regex-based secret scrubbing applied before storage              |

## Testing

Run from the repository root:

```bash
mise run test:core
```
