# Configuration

Default configuration templates. These are copied to `~/.config/hippo/` on first run.

## Files

| File                  | Purpose                                                                                             |
|-----------------------|-----------------------------------------------------------------------------------------------------|
| `config.default.toml` | Main configuration — inference-server endpoint (oMLX or LM Studio), model IDs, daemon tuning, brain server port, storage paths |
| `redact.default.toml` | Secret redaction patterns for supported capture paths                        |

## Redaction Patterns

See [the redaction reference](../docs/redaction.md) for capture coverage,
limitations, default rules, and custom-pattern configuration.
