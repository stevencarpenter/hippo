#!/usr/bin/env python3
"""One-shot re-enrichment of existing knowledge nodes.

Re-runs enrichment for knowledge nodes that pre-date the current prompt /
model setup, using the same code paths the live brain uses, and updates
each node in place. Preserves ``id`` / ``uuid`` / ``created_at`` / link
rows; only the derived content (summary, embed_text, entities, tags,
design_decisions, etc.) is replaced.

Why this exists
---------------
Across PRs #100, #105, #107 the enrichment system prompts were upgraded
(verbatim preservation, identifier-dense embed_text, design_decisions
schema, worktree-stripped entity names). The configured enrichment model
also moved from ``gpt-oss-120b`` to ``qwen3.6-35b-a3b-ud-mlx``. None of
that retroactively benefits the existing corpus. Running this script
brings every previously-enriched node up to the current standard.

Sources handled
---------------
- **shell** — link table ``knowledge_node_events``, prompt via
  ``build_enrichment_prompt`` against rows from ``events``.
- **claude** — link table ``knowledge_node_claude_sessions``, prompt is
  the concatenation of ``claude_sessions.summary_text`` rows joined by
  ``\\n---\\n``, mirroring ``Server._enrich_claude_batches``.

Sources NOT handled by this version
-----------------------------------
- **workflow** — ``workflow_enrichment.py`` writes free-text markdown
  into ``knowledge_nodes.content`` rather than a structured JSON
  EnrichmentResult; needs a separate code path. 187 nodes, low priority.
- **browser** — only 6 nodes in the live corpus; not worth a third
  branch in this script.

Both stay at their existing ``enrichment_version`` and will be skipped
by future runs of this script. Add explicit handling later if needed.

Usage
-----
    uv run --project brain python brain/scripts/re-enrich-knowledge-nodes.py \\
        [--db PATH] [--source shell|claude|all] [--limit N] [--dry-run] \\
        [--throttle-ms MS] [--newest-first]

Resume semantics
----------------
The script bumps each successfully-processed node's
``enrichment_version`` to ``TARGET_ENRICHMENT_VERSION``. The default
WHERE clause filters ``enrichment_version < TARGET_VERSION``, so a
re-run picks up where a prior run left off. Failed nodes stay at the
old version and will be retried on the next run. Each bump of the
target invalidates the entire corpus — it ratchets up when an
enrichment-prompt or entity-taxonomy change makes older outputs
structurally stale (most recent: v3 added the ``env_vars`` bucket).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hippo_brain import vector_store  # noqa: E402
from hippo_brain.claude_sessions import CLAUDE_SYSTEM_PROMPT  # noqa: E402
from hippo_brain.client import LMStudioClient  # noqa: E402
from hippo_brain.embeddings import embed_knowledge_node  # noqa: E402
from hippo_brain.enrichment import (  # noqa: E402
    CURRENT_ENRICHMENT_VERSION,
    SHELL_ENTITY_TYPE_MAP,
    SYSTEM_PROMPT,
    build_enrichment_prompt,
    parse_enrichment_response,
    upsert_entities,
)

# The script targets the current daemon write version. Bumping
# CURRENT_ENRICHMENT_VERSION in enrichment.py automatically invalidates the
# whole corpus for re-enrichment.
TARGET_ENRICHMENT_VERSION = CURRENT_ENRICHMENT_VERSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("re-enrich")


def _default_db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "hippo" / "hippo.db"


def _load_settings() -> dict:
    """Pull lmstudio + model settings from the canonical brain config."""
    from hippo_brain import _load_runtime_settings

    return _load_runtime_settings()


def _select_candidate_nodes(
    conn: sqlite3.Connection, source: str, limit: int | None, newest_first: bool
) -> list[dict]:
    """Return knowledge_nodes rows that need re-enrichment, joined with source.

    ``source`` is one of ``shell`` / ``claude`` / ``all``. The returned dicts
    carry an explicit ``_source`` key naming the link table the row hit.
    """
    order_clause = "DESC" if newest_first else "ASC"
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""

    queries = []
    if source in ("shell", "all"):
        queries.append(
            f"""
            SELECT n.id, n.uuid, n.created_at, 'shell' AS _source
            FROM knowledge_nodes n
            JOIN knowledge_node_events kne ON kne.knowledge_node_id = n.id
            WHERE n.enrichment_version < {TARGET_ENRICHMENT_VERSION}
            GROUP BY n.id
            """
        )
    if source in ("claude", "all"):
        queries.append(
            f"""
            SELECT n.id, n.uuid, n.created_at, 'claude' AS _source
            FROM knowledge_nodes n
            JOIN knowledge_node_claude_sessions kncs ON kncs.knowledge_node_id = n.id
            WHERE n.enrichment_version < {TARGET_ENRICHMENT_VERSION}
            GROUP BY n.id
            """
        )

    union_sql = " UNION ".join(queries)
    final_sql = f"SELECT * FROM ({union_sql}) ORDER BY created_at {order_clause} {limit_clause}"
    rows = conn.execute(final_sql).fetchall()
    return [dict(r) for r in rows]


def _fetch_shell_events(conn: sqlite3.Connection, node_id: int) -> list[dict]:
    """Return event rows linked to the given knowledge node, ordered by timestamp."""
    rows = conn.execute(
        """
        SELECT e.id, e.session_id, e.timestamp, e.command, e.exit_code, e.duration_ms,
               e.cwd, e.git_branch, e.git_commit, e.git_repo, e.stdout, e.stderr,
               e.hostname, e.shell
        FROM events e
        JOIN knowledge_node_events kne ON kne.event_id = e.id
        WHERE kne.knowledge_node_id = ?
        ORDER BY e.timestamp ASC
        """,
        (node_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_claude_segments(conn: sqlite3.Connection, node_id: int) -> list[dict]:
    """Return claude_sessions rows linked to the given knowledge node."""
    rows = conn.execute(
        """
        SELECT cs.id, cs.session_id, cs.cwd, cs.git_branch, cs.summary_text,
               cs.tool_calls_json, cs.user_prompts_json, cs.message_count,
               cs.start_time, cs.end_time, cs.content_hash
        FROM claude_sessions cs
        JOIN knowledge_node_claude_sessions kncs ON kncs.claude_session_id = cs.id
        WHERE kncs.knowledge_node_id = ?
        ORDER BY cs.start_time ASC
        """,
        (node_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _build_prompt_for(source: str, payload: list[dict]) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for the given source + payload."""
    if source == "shell":
        return SYSTEM_PROMPT, build_enrichment_prompt(payload)
    if source == "claude":
        # Mirror Server._enrich_claude_batches: concatenate summary_text rows.
        return CLAUDE_SYSTEM_PROMPT, "\n---\n\n".join(s.get("summary_text", "") for s in payload)
    raise ValueError(f"unsupported source: {source!r}")


async def _call_llm_with_retries(
    client: LMStudioClient, system_prompt: str, prompt: str, model: str
) -> object:
    """Mirror Server._call_llm_with_retries: 3 attempts, parse on each.

    On retry attempts (≥2), appends a follow-up user message instructing the
    model to output ONLY valid JSON. The live brain does this and it
    materially reduces re-failure rate when a model emits prose around a
    JSON object — without the hint we'd burn 3 full inferences before
    giving up. Behavior matches ``Server._call_llm_with_retries`` exactly.
    """
    last_err: Exception | None = None
    for attempt in range(1, 4):
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        if attempt > 1:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON. "
                        "Output ONLY a JSON object, no explanation or markdown."
                    ),
                }
            )
        try:
            raw = await client.chat(messages=messages, model=model)
            return parse_enrichment_response(raw)
        except Exception as e:
            last_err = e
            log.warning("attempt %d failed: %s", attempt, e)
    assert last_err is not None
    raise last_err


def _update_node_in_place(
    conn: sqlite3.Connection,
    node_id: int,
    result,
    enrichment_model: str,
) -> None:
    """Replace the node's derived content + entity links inside one transaction.

    Does NOT bump ``enrichment_version`` — that's deferred until after the
    embedding has also been refreshed (see ``_finalize_node_version``). If
    embedding fails, the content has already been overwritten with new-model
    output but the version stays at 1, so the next run will re-process and
    overwrite again with another fresh LLM call. Cost is one extra LLM call
    per partially-failed node; benefit is no node ever ends up at TARGET
    with a stale embedding (which would silently degrade retrieval).

    knowledge_node_entities is wiped + re-upserted under the current
    canonicalize / strip_worktree rules. Source-event link tables
    (knowledge_node_events / _claude_sessions / _browser_events /
    _workflow_runs) are untouched.
    """
    now_ms = int(time.time() * 1000)
    content = json.dumps(
        {
            "summary": result.summary,
            "intent": result.intent,
            "outcome": result.outcome,
            "entities": result.entities,
            "tags": result.tags,
            "key_decisions": result.key_decisions,
            "problems_encountered": result.problems_encountered,
            "design_decisions": result.design_decisions,
        }
    )
    tags_json = json.dumps(result.tags)

    conn.execute("BEGIN")
    try:
        conn.execute(
            """
            UPDATE knowledge_nodes
            SET content = ?, embed_text = ?, outcome = ?, tags = ?,
                enrichment_model = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                content,
                result.embed_text,
                result.outcome,
                tags_json,
                enrichment_model,
                now_ms,
                node_id,
            ),
        )
        # Wipe stale entity links, then re-upsert under the new rules.
        conn.execute("DELETE FROM knowledge_node_entities WHERE knowledge_node_id = ?", (node_id,))
        upsert_entities(conn, node_id, result.entities, SHELL_ENTITY_TYPE_MAP, now_ms)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _finalize_node_version(conn: sqlite3.Connection, node_id: int) -> None:
    """Bump enrichment_version to TARGET after content + embedding are both
    refreshed. Separate from ``_update_node_in_place`` so a failed embed
    leaves version at 1, ensuring the next run re-processes the node.
    """
    conn.execute(
        "UPDATE knowledge_nodes SET enrichment_version = ? WHERE id = ?",
        (TARGET_ENRICHMENT_VERSION, node_id),
    )
    conn.commit()


async def _re_embed(
    client: LMStudioClient,
    conn: sqlite3.Connection,
    node_id: int,
    embed_text: str,
    embed_model: str,
) -> None:
    if not embed_model:
        log.debug("no embed_model configured; skipping re-embed for node %d", node_id)
        return
    # vec0 virtual tables don't honor INSERT OR REPLACE the way classical
    # tables do — re-inserting against an existing knowledge_node_id raises
    # `UNIQUE constraint failed on knowledge_vectors primary key`. The live
    # brain never hits this because each enrichment creates a fresh node id.
    # In re-enrichment we update in place, so we must DELETE first.
    conn.execute("DELETE FROM knowledge_vectors WHERE knowledge_node_id = ?", (node_id,))
    await embed_knowledge_node(
        client,
        conn,
        {"id": node_id, "embed_text": embed_text, "commands_raw": ""},
        embed_model=embed_model,
        allow_embed_switch=False,
    )


async def _process_node(
    client: LMStudioClient,
    conn: sqlite3.Connection,
    candidate: dict,
    enrichment_model: str,
    embed_model: str,
    dry_run: bool,
) -> bool:
    """Re-enrich one node. Returns True on success, False on skip/failure."""
    node_id = candidate["id"]
    source = candidate["_source"]
    if source == "shell":
        payload = _fetch_shell_events(conn, node_id)
    elif source == "claude":
        payload = _fetch_claude_segments(conn, node_id)
    else:
        log.warning("node %d: unknown source %r, skipping", node_id, source)
        return False

    if not payload:
        log.warning("node %d: no source rows found via %s link table, skipping", node_id, source)
        return False

    system_prompt, prompt = _build_prompt_for(source, payload)

    if dry_run:
        log.info(
            "[dry-run] node %d (%s, %d source rows, prompt %d chars) — would re-enrich",
            node_id,
            source,
            len(payload),
            len(prompt),
        )
        return True

    try:
        result = await _call_llm_with_retries(client, system_prompt, prompt, enrichment_model)
    except Exception as e:
        log.error("node %d (%s): enrichment failed after retries: %s", node_id, source, e)
        return False

    try:
        _update_node_in_place(conn, node_id, result, enrichment_model)
        await _re_embed(client, conn, node_id, result.embed_text, embed_model)
        _finalize_node_version(conn, node_id)
    except Exception as e:
        log.error("node %d (%s): write/embed failed: %s", node_id, source, e)
        return False

    log.info("node %d (%s) re-enriched", node_id, source)
    return True


async def main_async(args: argparse.Namespace) -> int:
    settings = _load_settings()
    db_path = Path(args.db) if args.db else _default_db_path()
    if not db_path.exists():
        log.error("Database not found: %s", db_path)
        return 1

    enrichment_model = args.model or settings.get("enrichment_model") or ""
    embed_model = settings.get("embedding_model") or ""
    if not enrichment_model and not args.dry_run:
        log.error(
            "No enrichment model configured (config.toml [models].enrichment) and no --model flag"
        )
        return 1

    base_url = settings.get("lmstudio_base_url", "http://localhost:1234/v1")
    timeout = float(settings.get("lmstudio_timeout_secs", 300.0))

    # vector_store.open_conn loads the sqlite-vec extension (vec0) and
    # applies the standard PRAGMAs. Plain sqlite3.connect cannot write to
    # the knowledge_vectors virtual table — embed_knowledge_node would
    # error with "no such module: vec0".
    conn = vector_store.open_conn(db_path)
    conn.row_factory = sqlite3.Row
    # Autocommit mode: the script juggles its own explicit BEGIN/COMMIT
    # blocks plus calls into embed_knowledge_node which manages its own
    # transactions. Python's default DML auto-transaction would leave the
    # connection mid-transaction after a failed INSERT (vec0 UNIQUE), so
    # the next explicit BEGIN trips "cannot start a transaction within a
    # transaction" and every subsequent node fails. Setting isolation_level
    # to None disables the auto-transaction.
    conn.isolation_level = None

    candidates = _select_candidate_nodes(conn, args.source, args.limit, args.newest_first)
    log.info(
        "Selected %d candidate node(s) for re-enrichment "
        "(source=%s limit=%s newest_first=%s target_version=%d)",
        len(candidates),
        args.source,
        args.limit,
        args.newest_first,
        TARGET_ENRICHMENT_VERSION,
    )
    if not candidates:
        log.info("nothing to do")
        conn.close()
        return 0

    client = LMStudioClient(base_url=base_url, timeout=timeout)

    successes = 0
    failures = 0
    throttle = max(0.0, args.throttle_ms / 1000.0)
    try:
        for i, cand in enumerate(candidates, 1):
            log.info("(%d/%d) starting node %d", i, len(candidates), cand["id"])
            ok = await _process_node(
                client, conn, cand, enrichment_model, embed_model, args.dry_run
            )
            if ok:
                successes += 1
            else:
                failures += 1
            if throttle and i < len(candidates):
                await asyncio.sleep(throttle)
    finally:
        conn.close()
        if hasattr(client, "aclose"):
            await client.aclose()

    log.info("Done. successes=%d failures=%d total=%d", successes, failures, len(candidates))
    return 0 if failures == 0 else 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=str, default=None, help="path to hippo.db")
    parser.add_argument(
        "--source",
        choices=("shell", "claude", "all"),
        default="all",
        help="restrict to one source type (default: all)",
    )
    parser.add_argument("--limit", type=int, default=0, help="max nodes to process (0 = unlimited)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print candidates without calling LLM or modifying DB",
    )
    parser.add_argument(
        "--newest-first",
        action="store_true",
        default=True,
        help="(default) process newest nodes first so active queries benefit early",
    )
    parser.add_argument(
        "--oldest-first",
        action="store_false",
        dest="newest_first",
        help="reverse: process oldest nodes first",
    )
    parser.add_argument(
        "--throttle-ms",
        type=int,
        default=200,
        help="ms to sleep between nodes so live brain isn't starved (default: 200)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="override enrichment model (default: read from config.toml)",
    )
    args = parser.parse_args()
    rc = asyncio.run(main_async(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
