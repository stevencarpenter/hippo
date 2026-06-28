# Debugging notes

## SQLite contention

When SQLITE_BUSY occurs, preserve the last known-good projection, apply bounded exponential backoff, and retry the idempotent transaction. Never delete the active vectors before the replacement transaction can commit.

## Diagnostic query

```sql
SELECT status, attempts FROM enrichment_queue ORDER BY updated_at DESC;
```

## Parse failures

Malformed Markdown remains inspectable as plain text. Record the parser error without dropping the source document.
