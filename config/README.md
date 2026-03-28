# Configuration

Default configuration templates. These are copied to `~/.config/hippo/` on first run.

## Files

| File | Purpose |
|---|---|
| `config.default.toml` | Main configuration — LM Studio endpoint, model IDs, daemon tuning, brain server port, storage paths |
| `redact.default.toml` | Secret redaction patterns — regex rules applied to all events before storage |

## Redaction Patterns

Out of the box, Hippo redacts:

- AWS access keys (`AKIA...`)
- GitHub personal access tokens (`ghp_*`, `github_pat_*`)
- Generic secret assignments (`api_key=...`, `password=...`, etc.)
- JWT tokens
- Bearer authorization headers
- PEM private key headers

Add custom patterns by editing `~/.config/hippo/redact.toml`.
