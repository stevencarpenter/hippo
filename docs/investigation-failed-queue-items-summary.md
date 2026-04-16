# Failed Queue Items Investigation Summary

**Date:** 2026-04-16
**Status:** ✅ Investigation Complete

## TL;DR

The failed queue items reported by the Grafana dashboard are **expected transient failures** that automatically recover through the retry mechanism (default: 5 retries per item). The current alert is too sensitive because it doesn't distinguish between:

- **Transient failures** (mid-retry, will recover automatically)
- **Permanent failures** (exhausted retries, need investigation)

## Root Cause

The `/health` endpoint returns a single `queue_failed` count that includes ALL items with `status='failed'`, regardless of retry status:

```python
queue_failed = conn.execute(
    "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'failed'"
).fetchone()[0]
```

This count includes:
- Items that have `retry_count < max_retries` (will retry on next poll cycle)
- Items that have `retry_count >= max_retries` (exhausted, need manual investigation)

## Common Transient Failure Scenarios

1. **LM Studio restart** (while switching models) → Connection errors, recovers in < 1 minute
2. **LLM parse failures** (malformed JSON) → Retries with prompt guidance, recovers in ~10-30s
3. **Database lock contention** (concurrent writes) → SQLite busy timeout handles this, recovers in < 5s
4. **Stale locks** (brain server crash) → Auto-reclaimed after 5 minutes

All of these scenarios trigger `status='failed'` temporarily, causing the dashboard alert to fire during normal operation.

## Recommended Fix

### Update Dashboard Alerts

Replace the current alert:
```promql
sum(hippo_queue_failed) > 0  # Too sensitive
```

With this:
```sql
-- Alert only on permanently failed items (exhausted retries)
SELECT COUNT(*) FROM enrichment_queue
WHERE status = 'failed' AND retry_count >= max_retries;
```

Or add a time threshold:
```sql
-- Alert only on items failed for > 30 minutes
SELECT COUNT(*) FROM enrichment_queue
WHERE status = 'failed'
  AND updated_at < (unixepoch('now', 'subsec') * 1000 - 1800000);
```

### Add Granular Metrics

Enhance the `/health` endpoint to expose:

```json
{
  "queue_failed": 10,                    // Total (current)
  "queue_failed_exhausted": 2,           // NEW: retry_count >= max_retries
  "queue_failed_retrying": 8,            // NEW: retry_count < max_retries
  "queue_oldest_failed_age_ms": 45000    // NEW: Time since oldest failure
}
```

## Verification Queries

### Check for permanently failed items:

```sql
SELECT
    COUNT(*) as permanent_failures,
    error_message
FROM enrichment_queue
WHERE status = 'failed' AND retry_count >= max_retries
GROUP BY error_message;
```

### Check retry distribution:

```sql
SELECT
    retry_count,
    status,
    COUNT(*) as count
FROM enrichment_queue
GROUP BY retry_count, status
ORDER BY retry_count, status;
```

Expected healthy output:
```
retry_count | status  | count
------------|---------|------
0           | pending | 150    # Fresh items
0           | done    | 8500   # Success on first try
1           | pending | 5      # First retry in progress
1           | done    | 120    # Recovered after 1 retry
2           | done    | 10     # Recovered after 2 retries
5           | failed  | 2      # INVESTIGATE THESE
```

## Next Steps

1. ✅ Investigation complete - no code bug found
2. ⏳ Update Grafana dashboard alerts (see recommendations above)
3. ⏳ Add granular failed metrics to `/health` endpoint (short-term)
4. ⏳ Implement exponential backoff for failed items (long-term optimization)

## Full Report

See [docs/investigation-failed-queue-items.md](investigation-failed-queue-items.md) for complete analysis including:
- Detailed retry logic walkthrough
- All failure scenarios with code paths
- Future enhancement recommendations (backoff, circuit breaker)
- Diagnostic SQL queries
- Test coverage analysis
