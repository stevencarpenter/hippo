# hippo-daemon

Rust binary providing the always-on daemon and CLI for Hippo. The daemon listens on a Unix socket for shell events, and
the CLI provides tools for querying, managing, and diagnosing the system.

## Running

```bash
# Foreground (for development)
cargo run --bin hippo -- daemon run

# Via launchd
cargo run --bin hippo -- daemon install
cargo run --bin hippo -- daemon start
```

## CLI Commands

| Command                                    | Description                |
|--------------------------------------------|----------------------------|
| `hippo daemon run`                         | Run daemon in foreground   |
| `hippo daemon start/stop/restart`          | Manage via launchd         |
| `hippo daemon install`                     | Install LaunchAgent        |
| `hippo status`                             | Show daemon status         |
| `hippo sessions [--today] [--since 7d]`    | List sessions              |
| `hippo events [--session ID] [--since 2h]` | List events                |
| `hippo ask <question>`                     | RAG query via brain server |
| `hippo query <text> [--raw]`               | Lexical search             |
| `hippo entities [--type TYPE]`             | List known entities        |
| `hippo export-training [--since 30d]`      | Export JSONL training data |
| `hippo config edit`                        | Open config in editor      |
| `hippo config set KEY VALUE`               | Set a config value         |
| `hippo redact test <string>`               | Test redaction patterns    |
| `hippo ingest claude-session`              | Ingest Claude session logs |
| `hippo native-messaging-host`              | Firefox native messaging bridge |
| `hippo brain stop`                         | Stop the brain server      |
| `hippo doctor`                             | Run diagnostic checks      |

## Modules

| Module        | Purpose                                                 |
|---------------|---------------------------------------------------------|
| `main.rs`     | Entry point, argument routing                           |
| `cli.rs`      | clap command definitions                                |
| `daemon.rs`   | Daemon loop, Unix socket listener                       |
| `commands.rs` | CLI command handlers                                    |
| `framing.rs`  | Length-prefixed message framing for the socket protocol |
| `claude_session.rs` | Claude session JSONL ingestion                  |
| `install.rs`  | LaunchAgent installation                                |
| `native_messaging.rs` | Firefox Native Messaging host               |
| `telemetry.rs` | OpenTelemetry integration                              |

## Testing

```bash
cargo test -p hippo-daemon
```
