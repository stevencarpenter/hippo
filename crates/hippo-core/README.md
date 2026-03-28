# hippo-core

Shared Rust library for the Hippo system. Provides types, configuration loading, SQLite storage, secret redaction, and
the protocol definition used between the shell hook and daemon.

## Modules

| Module         | Purpose                                                          |
|----------------|------------------------------------------------------------------|
| `config.rs`    | Loads and validates `config.toml` / `redact.toml`                |
| `storage.rs`   | SQLite operations (events, sessions, enrichment queue, entities) |
| `events.rs`    | Event type definitions                                           |
| `protocol.rs`  | Length-prefixed JSON protocol for Unix socket communication      |
| `redaction.rs` | Regex-based secret scrubbing applied before storage              |

## Testing

```bash
cargo test -p hippo-core
```
