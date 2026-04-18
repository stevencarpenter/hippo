"""Enrichment queue watchdog: reaper + preflight.

Guards against R-22: a worker claims a batch, LM Studio wedges (e.g. returns
HTTP 400 or goes unreachable), and the lock is held indefinitely while the
`pending` queue grows behind it. Observed on the live corpus as 417 rows all
sharing one `locked_at` for 30+ minutes.

Mitigations here:

- `reap_stale_locks` runs every loop iteration across all four enrichment
  queues. Rows whose `locked_at` is older than `lock_timeout_ms` are flipped
  back to `pending` (or `failed` when `retry_count + 1 >= max_retries`), with
  `retry_count` incremented so a permanently bad row doesn't loop forever.

- `preflight_lm_studio` is called before claiming. On unreachable LM Studio or
  a completely empty model list it returns a decision object the loop uses to
  skip the cycle and WARN, rather than claiming a batch the LLM can't process.

- Claim functions accept `max_claim_batch` to cap rows claimed per UPDATE per
  cycle, so one bad batch can't poison 400+ rows.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from hippo_brain.client import LMStudioClient
from hippo_brain.telemetry import add as _add
from hippo_brain.telemetry import get_meter

logger = logging.getLogger("hippo_brain.watchdog")

DEFAULT_LOCK_TIMEOUT_MS = 10 * 60 * 1000
DEFAULT_MAX_CLAIM_BATCH = 10

_meter = get_meter()
_reaped_counter = (
    _meter.create_counter(
        "hippo.brain.enrichment.reaped",
        description="Stale locks swept by the watchdog reaper",
    )
    if _meter
    else None
)
_preflight_skipped = (
    _meter.create_counter(
        "hippo.brain.enrichment.preflight_skipped",
        description="Enrichment cycles skipped by LM Studio preflight",
    )
    if _meter
    else None
)


@dataclass(frozen=True)
class QueueSpec:
    """Hardcoded spec for one enrichment queue table.

    `table` and `pk_col` are never user-controlled — they come from this
    frozen tuple — so it is safe to interpolate them into SQL.
    """

    name: str
    table: str
    pk_col: str


QUEUES: tuple[QueueSpec, ...] = (
    QueueSpec("shell", "enrichment_queue", "id"),
    QueueSpec("claude", "claude_enrichment_queue", "id"),
    QueueSpec("browser", "browser_enrichment_queue", "id"),
    QueueSpec("workflow", "workflow_enrichment_queue", "run_id"),
)


def reap_stale_locks(
    conn: sqlite3.Connection,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    now_ms: int | None = None,
) -> dict[str, int]:
    """Sweep all enrichment queues, releasing stale `processing` locks.

    A lock is stale when `locked_at <= now_ms - lock_timeout_ms`. Reaped rows
    have `retry_count` incremented and get an `error_message` explaining the
    reap; rows that hit `max_retries` are marked `failed` instead of `pending`
    so a permanently bad payload doesn't loop forever.

    Returns `{queue_name: reaped_count}`. Missing tables (older schemas) are
    logged at debug and counted as 0.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    threshold_ms = now_ms - lock_timeout_ms
    reason = f"stale lock reaped (>={lock_timeout_ms // 1000}s)"
    reaped: dict[str, int] = {}

    for spec in QUEUES:
        # table + pk_col are from the frozen QUEUES tuple — safe to interpolate.
        sql = (
            f"UPDATE {spec.table} "
            f"SET status = CASE WHEN retry_count + 1 >= max_retries THEN 'failed' ELSE 'pending' END, "
            f"    retry_count = retry_count + 1, "
            f"    error_message = ?, "
            f"    locked_at = NULL, "
            f"    locked_by = NULL, "
            f"    updated_at = ? "
            f"WHERE status = 'processing' AND COALESCE(locked_at, 0) <= ? "
            f"RETURNING {spec.pk_col}"
        )
        try:
            cursor = conn.execute(sql, (reason, now_ms, threshold_ms))
            rows = cursor.fetchall()
            conn.commit()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                logger.debug("reaper skipped %s (%s): %s", spec.name, spec.table, e)
                reaped[spec.name] = 0
                continue
            logger.exception(
                "reaper failed for %s (%s) due to sqlite operational error",
                spec.name,
                spec.table,
            )
            raise

        count = len(rows)
        reaped[spec.name] = count
        if count:
            logger.warning(
                "reaped stale locks queue_name=%r reaped_count=%d lock_timeout_ms=%d",
                spec.name,
                count,
                lock_timeout_ms,
                extra={
                    "queue_name": spec.name,
                    "reaped_count": count,
                    "lock_timeout_ms": lock_timeout_ms,
                    "stage": "reaper",
                },
            )
            _add(_reaped_counter, count, queue_name=spec.name)

    return reaped


@dataclass(frozen=True)
class PreflightDecision:
    """Outcome of `preflight_lm_studio`.

    `proceed` is True when the loop may safely claim work. `reason` is a short
    tag suitable for logs/metrics (`ok`, `unreachable`, `no_models`,
    `model_missing`). `loaded_models` carries the API response so a caller
    doesn't need a second `list_models` round-trip.
    """

    proceed: bool
    reason: str
    loaded_models: list[str]
    error: str | None = None


async def preflight_lm_studio(
    client: LMStudioClient,
    preferred_model: str | None,
    allow_fallback: bool = True,
) -> PreflightDecision:
    """Verify LM Studio is reachable and a chat model is available.

    Returns `PreflightDecision(proceed=False, ...)` when:
      - `list_models` raises (unreachable / TLS / auth): `reason="unreachable"`.
      - LM Studio responds with no chat models at all: `reason="no_models"`.
      - `allow_fallback=False` and `preferred_model` isn't loaded:
        `reason="model_missing"`.
    Otherwise `proceed=True` with `reason` in {`ok`, `fallback`}.
    """
    try:
        loaded = await client.list_models()
    except Exception as e:
        err = str(e) or type(e).__name__
        logger.warning(
            "LM Studio preflight: unreachable error=%r preferred_model=%r",
            err,
            preferred_model,
            extra={"stage": "preflight", "reason": "unreachable", "error": err},
        )
        _add(_preflight_skipped, reason="unreachable")
        return PreflightDecision(proceed=False, reason="unreachable", loaded_models=[], error=err)

    embedding_hints = ("embed", "nomic", "modernbert")
    chat_models = [m for m in loaded if not any(h in m.lower() for h in embedding_hints)]

    if not chat_models:
        logger.warning(
            "LM Studio preflight: no chat models loaded loaded_models=%r preferred_model=%r",
            loaded,
            preferred_model,
            extra={"stage": "preflight", "reason": "no_models"},
        )
        _add(_preflight_skipped, reason="no_models")
        return PreflightDecision(proceed=False, reason="no_models", loaded_models=loaded)

    if preferred_model and preferred_model in chat_models:
        return PreflightDecision(proceed=True, reason="ok", loaded_models=loaded)

    if not allow_fallback and preferred_model:
        logger.warning(
            "LM Studio preflight: preferred model not loaded preferred_model=%r loaded_models=%r chat_models=%r",
            preferred_model,
            loaded,
            chat_models,
            extra={"stage": "preflight", "reason": "model_missing"},
        )
        _add(_preflight_skipped, reason="model_missing")
        return PreflightDecision(proceed=False, reason="model_missing", loaded_models=loaded)

    return PreflightDecision(proceed=True, reason="fallback", loaded_models=loaded)
