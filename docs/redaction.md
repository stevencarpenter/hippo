# Redaction Reference

How hippo's secret-redaction layer works, what it catches by default, and what it explicitly doesn't. Companion to the [README's Privacy and Security section](../README.md#privacy-and-security).

For the bigger threat model (data flow, encryption, MCP trust boundary), read the README section first. This doc is the regex-pattern deep-dive.

## What redaction is

A regex-based filter that runs over event text **before storage**. Implemented in `crates/hippo-core/src/redaction.rs::RedactionEngine`. Applied to:

- Shell command strings, stdout, stderr (in `daemon.rs::flush_events` before `insert_event_at`).
- Browser titles and Readability-extracted page content (same path).
- Claude session segment text (same path).

Redaction is **best-effort**. It catches known secret formats; it cannot catch secrets in arbitrary positions. Treat it as a noise filter, not a security guarantee. If you need stronger guarantees, don't paste secrets into your terminal in front of a tool that captures stdout.

## What redaction isn't

- **Not a network filter.** Redaction runs at storage time, not at LLM-call time. The LM Studio prompt is built from already-redacted events, so secrets that *did* slip past redaction will reach the LLM. That LLM is local (LM Studio at `localhost:1234`); there's no third-party LLM in the path. But if you point LM Studio at a remote backend, see the README's Privacy section.
- **Not a database scrubber.** Once a non-redacted secret has been stored in `events`, hippo doesn't re-process old rows when you add a pattern. Add a pattern → only future captures benefit.
- **Not a substitute for FileVault.** The DB is unencrypted at rest.

## Pattern format

`~/.config/hippo/redact.toml` is a TOML file with an array of pattern tables:

```toml
[[patterns]]
name = "aws_access_key"
regex = 'AKIA[0-9A-Z]{16}'
replacement = "[REDACTED]"

[[patterns]]
name = "github_pat"
regex = 'ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82}'
replacement = "[REDACTED]"
```

| Field | Required | Behavior |
|---|---|---|
| `name` | yes | Rule identifier. Surfaces in metrics (`hippo.daemon.redaction_hits{rule=<name>}`) and in `hippo redact test` output. Must be unique. |
| `regex` | yes | Rust [`regex` crate](https://docs.rs/regex/) syntax. **No PCRE backreferences or lookaround.** Compiled once at daemon startup (and `RegexSet`-bundled for fast match dispatch). Use `(?i)` for case-insensitivity. |
| `replacement` | yes | The replacement string for matched substrings. Conventionally `"[REDACTED]"`; can include capture-group references (`$1`, `$2`) per the regex crate's `Replacer`. |

The whole `redact.toml` is the rule set; patterns are loaded in file order. See `crates/hippo-core/src/config.rs::RedactConfig` for the deserialization shape.

## Evaluation model

- **All patterns apply, not first-match.** `RedactionEngine::redact` iterates over `RegexSet::matches`, then calls `replace_all` for each matching rule. A single command can fire multiple rules.
- **Order is deterministic.** Patterns evaluate in the order they appear in `redact.toml`. After each pattern's `replace_all`, subsequent patterns operate on the *already-redacted* text. This matters when patterns can overlap: an earlier pattern that replaces a substring with `[REDACTED]` prevents a later pattern from matching what was there.
- **Per-rule hit attribution.** Counting happens before replacement (counting after `replace_all` would return zero, since `[REDACTED]` doesn't match the original pattern). Hit counts feed the OTel counter `hippo.daemon.redaction_hits{rule=<name>}`.
- **No event dropping.** When the entire command matches a pattern, the substring is replaced with `[REDACTED]` in-place; the event row is still stored. Hippo doesn't delete events even when redaction renders them empty. (See [issue #52](https://github.com/stevencarpenter/hippo/issues/52) for the open discussion of "over-redaction silently producing empty events" — the current behavior is "store the redacted row," which is auditable but means a power user might see `[REDACTED]` lines in `hippo events`.)

## Default patterns

Shipped in [`config/redact.default.toml`](../config/redact.default.toml). All replacements are `"[REDACTED]"`.

| Rule | Regex | Catches |
|---|---|---|
| `aws_access_key` | `AKIA[0-9A-Z]{16}` | Long-lived AWS access key IDs (the `AKIA*` prefix). |
| `github_pat` | `ghp_[a-zA-Z0-9]{36}\|github_pat_[a-zA-Z0-9_]{82}` | GitHub classic personal access tokens (`ghp_`) and fine-grained tokens (`github_pat_`). |
| `generic_secret_assignment` | `(?i)(api[_-]?key\|api[_-]?token\|access[_-]?token\|auth[_-]?token\|secret[_-]?key\|private[_-]?key\|password)\s*[=:]\s*\S{8,}` | `key=value` and `key: value` assignments where the key matches a known secret-y name and the value is ≥ 8 non-whitespace characters. |
| `jwt` | `eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+` | Three-segment JWTs starting with the standard base64 `{` prefix. |
| `bearer_header` | `(?i)authorization:\s*bearer\s+\S+` | HTTP `Authorization: Bearer <token>` headers. |
| `private_key_pem` | `-----BEGIN [A-Z ]*PRIVATE KEY-----` | The leading line of any PEM-encoded private key. (The body and trailing `-----END` line aren't matched, so a key body in stdout would have its header redacted but the body would persist. This is a known gap.) |

### Known false-negatives

The default rules **do not** catch:

- **AWS temporary keys** (`ASIA*` prefix, used by STS).
- **Secrets in positional arguments**: `./deploy prod my-secret-token` won't match anything — there's no `key=` prefix.
- **Secrets in env-var names not on the keyword list**: `STRIPE_KEY=sk_live_...`, `SENDGRID_APIKEY=...` won't fire `generic_secret_assignment` because the keyword regex doesn't include `STRIPE` or `SENDGRID`.
- **Secrets renamed locally**: `x=ghp_...` only fires `github_pat` (because the value matches), not `generic_secret_assignment` (because `x` isn't a recognized keyword). If the user pastes a non-`ghp_` token under a non-keyword name, neither rule fires.
- **Multi-line secrets**: a key body across multiple stdout lines isn't matched by `private_key_pem` (which only matches the header).
- **Secrets in JSON payloads not matching `keyword=value`**: e.g. `{"apiKey": "..."}`.  The keyword regex requires `=` or `:` adjacent to the keyword name; embedded JSON shapes pass through.

If your workflow involves any of the above, write custom patterns. (See [Writing custom patterns](#writing-custom-patterns).)

## Writing custom patterns

The `hippo redact test "<input>"` CLI compiles your live `redact.toml` and reports which rules fire on the given input. Use it iteratively while writing a new pattern.

### Example session

You want to catch your team's internal API tokens, which look like `xtok_<32-hex>`:

```bash
# Step 1: confirm the default rules don't catch it
hippo redact test "deploy --token xtok_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"
# rules fired: (none)

# Step 2: add the pattern
$EDITOR ~/.config/hippo/redact.toml
```

```toml
[[patterns]]
name = "internal_xtok"
regex = 'xtok_[a-f0-9]{32}'
replacement = "[REDACTED]"
```

```bash
# Step 3: verify
hippo redact test "deploy --token xtok_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"
# rules fired: internal_xtok

# Step 4: confirm it doesn't false-positive on benign text
hippo redact test "git checkout xtok-feature-branch"
# rules fired: (none)
```

### More example sessions

```bash
# Stripe live keys
hippo redact test "STRIPE_KEY=sk_live_AbCdEfGhIjKlMnOp"
# default rules fire: (none) — STRIPE_KEY isn't on the keyword list

# After adding a pattern named `stripe_secret_key` with regex `sk_live_[a-zA-Z0-9]{24,}`:
hippo redact test "STRIPE_KEY=sk_live_AbCdEfGhIjKlMnOp"
# rules fired: stripe_secret_key
```

```bash
# Slack incoming webhooks (placeholder shown — replace with your team's URL when testing)
hippo redact test "curl -X POST https://hooks.slack.com/services/<TEAM>/<CHANNEL>/<TOKEN>"
# default rules fire: (none)

# After adding a pattern: regex = 'https://hooks\.slack\.com/services/[A-Z0-9/]+'
# the same call would report: rules fired: slack_webhook_url
```

```bash
# Database connection strings
hippo redact test "DATABASE_URL=postgres://admin:hunter2@db.example.com:5432/prod"
# default rules fire: (none) — DATABASE_URL isn't on the keyword list

# Add: regex = '(?i)(database_url|postgres|mysql)://[^@\s]+:[^@\s]+@'
hippo redact test "DATABASE_URL=postgres://admin:hunter2@db.example.com:5432/prod"
# rules fired: db_connection_string
```

After adding any new pattern, restart the daemon: `mise run restart`. The engine compiles patterns at startup and doesn't reload on `redact.toml` change.

## Browser URL redaction

Separate from `redact.toml`. Configured in `[browser.url_redaction]` in `~/.config/hippo/config.toml`:

```toml
[browser.url_redaction]
strip_params = ["session_id", "auth_token", "access_token", "api_key", "token"]
```

Implemented in `crates/hippo-daemon/src/native_messaging.rs::strip_sensitive_params`. For each URL passed by the Firefox extension, query parameters whose names match `strip_params` are removed before storage. Path components are preserved.

What it catches:

- `https://example.com/api?api_key=abc&user=joe` → `https://example.com/api?user=joe`
- `https://example.com/login?token=xyz` → `https://example.com/login`

What it doesn't catch:

- Secrets in path segments: `https://example.com/api/v1/abc-token-xyz/users` keeps the path as-is.
- Secrets in fragment: `https://example.com/page#auth=abc` keeps the fragment.
- Encoded params: `https://example.com/api?secret%3Dabc` won't be parsed as a `secret=abc` query.

## Threat model

| Hippo defends against | Hippo does NOT defend against |
|---|---|
| Accidental capture of common token formats (AWS, GitHub PAT, JWT, PEM private keys) into the LLM context | Secrets in positional arguments, non-standard env-var names, or arbitrary file content the user pastes |
| Common URL-borne tokens in browser visit URLs | Tokens in URL path segments or page content |
| Secrets in `Authorization: Bearer …` HTTP headers logged to stdout | Secrets in custom auth schemes |
| Storing structured PEM private-key headers | The body of a multi-line private key (only the header line matches) |
| Replay of `[REDACTED]` strings across the LLM/MCP path (since the secret has been replaced before storage) | Secrets that already-stored prior to a pattern being added |

For threats outside this list, the answer is "don't paste secrets into your terminal." Hippo's job is to catch the common case.

## See also

- [README Privacy and Security](../README.md#privacy-and-security) — the wider data-flow story.
- [`config/redact.default.toml`](../config/redact.default.toml) — the default ruleset.
- [`crates/hippo-core/src/redaction.rs`](../crates/hippo-core/src/redaction.rs) — `RedactionEngine` implementation.
- [`crates/hippo-daemon/src/native_messaging.rs`](../crates/hippo-daemon/src/native_messaging.rs) — `strip_sensitive_params` for browser URL redaction.
- [Issue #52](https://github.com/stevencarpenter/hippo/issues/52) — open discussion on over-redaction behavior.
