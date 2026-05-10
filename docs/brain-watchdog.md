# Enrichment queue watchdog

Mitigates the enrichment-queue wedge risk: on 2026-04-17 the live corpus had
**417 queue rows all locked by one worker at one timestamp** and held for 30+
minutes because the inference server returned HTTP 400 and the claim loop got
stuck, while pending work grew behind it. The watchdog removes the three
failure-mode legs that made that possible.

## What it does

1. **Reaper** (`hippo_brain.watchdog.reap_stale_locks`) runs at the top of every
   enrichment loop iteration, across all four queues
   (`enrichment_queue`, `claude_enrichment_queue`, `browser_enrichment_queue`,
   `workflow_enrichment_queue`). Rows where
   `status='processing' AND locked_at <= now - lock_timeout` are swept back to
   `pending`, `retry_count` is incremented, and rows that hit `max_retries` are
   promoted to `failed` so a permanently bad payload can't loop forever.

2. **Inference-server preflight** (`preflight_inference`) runs before each
   claim. It calls `/v1/models` on the configured inference server and returns
   one of `ok | fallback | unreachable | no_models | model_missing`. On any
   non-ok reason the cycle skips claiming and WARNs — the claim queue stays
   intact instead of being poisoned by a wedge.

3. **Claim-batch cap** — each `claim_pending_*` function accepts
   `max_claim_batch`. The cap is enforced as `LIMIT ?` on the UPDATE's inner
   SELECT, so one bad batch can't claim (and then hold) an entire backlog.

Structured failure logs include `queue_name`, `stage`, `exception_type`,
`enrichment_model`, `claim_count`, and `claim_age_ms` — the fields a human or
alert rule needs to diagnose a wedge without tailing the full log.

## Config knobs

`[brain]` section of `~/.config/hippo/config.toml`:

| Key                 | Default | Effect                                                              |
| ------------------- | ------- | ------------------------------------------------------------------- |
| `max_claim_batch`   | `10`    | Max rows claimed per queue per cycle. Per-queue, not global.        |
| `lock_timeout_secs` | `600`   | A `processing` lock older than this is considered stale and reaped. |

Metrics: `hippo.brain.enrichment.reaped{queue_name=...}` and
`hippo.brain.enrichment.preflight_skipped{reason=...}` — watch these to see
the watchdog actually firing.

## Tracking inference-server model worker crashes

Inference servers can die mid-inference: with LM Studio it's the qwen-MoE
worker process (JIT eviction when another client requests a different model,
or Metal allocator failures under context-cache pressure). Until the worker
reloads, the API returns HTTP 4xx with body `{"error": "The model has crashed
without additional information. (Exit code: null)"}`. The brain's queue-level
retry transparently re-runs the work against a freshly-restarted model, so
end-to-end enrichment succeeds — but the underlying instability was previously
visible only in the workflow path's logs (which lack the in-process retry
wrapper that claude/shell/browser have).

Metric: `hippo.brain.inference.crashes` — incremented by the inference HTTP
client's `_raise_with_body` helper whenever a 4xx body matches `"model has
crashed"` (case-insensitive). Path-agnostic: chat, embed, and list_models
all route through `_raise_with_body`, so a crash on any of them counts. Crashes are a strict subset of `hippo.brain.inference.errors`, so a
single crash increments both counters; dashboards graphing `crashes / errors`
see the share of failures attributable to model-worker crashes over time. The
substring match is currently calibrated to LM Studio's crash wording — extend
the pattern in `client.py` when an oMLX (or other backend) crash signature is
observed in production.

LM Studio mitigations live in LM Studio settings, not hippo: set
`unloadPreviousJITModelOnLoad: false` to stop competing-client evictions, and
cap `defaultContextLength` to a fixed value (e.g. 32768) instead of `"max"`
to reduce Metal allocator pressure.
