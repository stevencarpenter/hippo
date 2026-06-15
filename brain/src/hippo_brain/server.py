import asyncio
import datetime as _dt
import logging
import sqlite3
import time

import sqlite_vec  # type: ignore[import-untyped]
from contextlib import asynccontextmanager, nullcontext, suppress
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from hippo_brain.client import InferenceClient
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION, ACCEPTED_READ_VERSIONS
from hippo_brain.version import get_version
from hippo_brain.vault_export import export_vault
from hippo_brain._version import __version__ as _hippo_version
from hippo_brain.embeddings import (
    embed_knowledge_node,
    get_or_create_table,
    open_vector_db,
    search_similar,
)
from hippo_brain.vector_store import get_stored_embed_model
from hippo_brain.rag import ask as rag_ask
from hippo_brain.enrichment import (
    SYSTEM_PROMPT,
    build_enrichment_prompt,
    claim_pending_events_by_session,
    mark_queue_failed,
    parse_enrichment_response,
    write_knowledge_node,
)
from hippo_brain.browser_enrichment import (
    BROWSER_SYSTEM_PROMPT,
    build_browser_enrichment_prompt,
    claim_pending_browser_events,
    format_browser_context_for_shell_prompt,
    get_correlated_browser_events,
    mark_browser_queue_failed,
    write_browser_knowledge_node,
)
from hippo_brain.claude_sessions import (
    CLAUDE_SYSTEM_PROMPT,
    claim_pending_claude_segments,
    mark_claude_queue_failed,
    write_claude_knowledge_node,
)
from hippo_brain.opencode_sessions import (
    OPENCODE_ENRICHMENT_PROMPT,
    build_opencode_enrichment_prompt,
    claim_pending_opencode_segments,
    mark_opencode_queue_failed,
    write_opencode_knowledge_node,
)
from hippo_brain.workflow_enrichment import (
    claim_pending_workflow_runs,
    enrich_one_async,
    mark_workflow_queue_failed,
)
from hippo_brain.watchdog import (
    DEFAULT_LOCK_TIMEOUT_MS,
    DEFAULT_MAX_CLAIM_BATCH,
    preflight_inference,
    reap_stale_locks,
)
from hippo_brain.telemetry import (
    add as _add,
    get_meter,
    get_tracer as _get_tracer,
    hist as _hist,
    is_telemetry_active,
    is_telemetry_enabled,
)

_meter = get_meter()
_events_claimed = (
    _meter.create_counter(
        "hippo.brain.enrichment.events_claimed", description="Events pulled from enrichment queue"
    )
    if _meter
    else None
)
_nodes_created = (
    _meter.create_counter(
        "hippo.brain.enrichment.nodes_created", description="Knowledge nodes written"
    )
    if _meter
    else None
)
_enrichment_failures = (
    _meter.create_counter(
        "hippo.brain.enrichment.failures", description="Enrichment batch failures"
    )
    if _meter
    else None
)
_loop_duration = (
    _meter.create_histogram(
        "hippo.brain.enrichment.loop_duration",
        description="Enrichment cycle wall clock",
        unit="ms",
    )
    if _meter
    else None
)


logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("hippo_brain")

# Upper bound for `limit` on LM/embedding-bound endpoints (/ask, /query). Caps
# pathological requests that would force expensive embedding lookups or
# enormous result sets through the LM. Lower than MAX_LIST_LIMIT because the
# downstream cost (embedding + LLM context) is super-linear in result count.
MAX_QUERY_LIMIT = 100

# Upper bound for `limit` on plain SQL list endpoints (/knowledge, /events,
# /sessions). These are just bounded SELECTs, so the cap is mainly to avoid
# accidentally serializing tens of thousands of rows into one response.
MAX_LIST_LIMIT = 500

QUEUE_DEPTH_STATUSES = ("pending", "processing", "failed")
CODEX_SOURCE_SQL = (
    "s.source_file LIKE '%/.codex/%' OR s.source_file LIKE '%/CodingAssistant/codex/%'"
)
CURSOR_SOURCE_SQL = "s.source_file LIKE '%/.cursor/%'"


def _is_codex_source_file(source_file: str | None) -> bool:
    return bool(
        source_file and ("/.codex/" in source_file or "/CodingAssistant/codex/" in source_file)
    )


def _is_cursor_source_file(source_file: str | None) -> bool:
    return bool(source_file and "/.cursor/" in source_file)


def _source_label_for_claude_segments(segments: list[dict]) -> str:
    harnesses = {seg.get("harness") for seg in segments if seg.get("harness")}
    if len(harnesses) == 1:
        harness = next(iter(harnesses))
        return {
            "claude-code": "claude",
            "codex": "codex",
            "cursor": "cursor",
            "opencode": "opencode",
        }.get(harness, "claude")
    if segments and all(_is_codex_source_file(seg.get("source_file")) for seg in segments):
        return "codex"
    if segments and all(_is_cursor_source_file(seg.get("source_file")) for seg in segments):
        return "cursor"
    return "claude"


def _collect_queue_depths(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    queries = {
        "shell": "SELECT COUNT(*) FROM enrichment_queue WHERE status = ?",
        "claude": """
            SELECT COUNT(*)
            FROM agentic_enrichment_queue q
            JOIN agentic_sessions s ON q.session_id = s.id
            WHERE q.status = ?
              AND s.probe_tag IS NULL
              AND s.harness = 'claude-code'
        """,
        "cursor": """
            SELECT COUNT(*)
            FROM agentic_enrichment_queue q
            JOIN agentic_sessions s ON q.session_id = s.id
            WHERE q.status = ?
              AND s.probe_tag IS NULL
              AND s.harness = 'cursor'
        """,
        "codex": """
            SELECT COUNT(*)
            FROM agentic_enrichment_queue q
            JOIN agentic_sessions s ON q.session_id = s.id
            WHERE q.status = ?
              AND s.probe_tag IS NULL
              AND s.harness = 'codex'
        """,
        "browser": "SELECT COUNT(*) FROM browser_enrichment_queue WHERE status = ?",
        "workflow": "SELECT COUNT(*) FROM workflow_enrichment_queue WHERE status = ?",
        "opencode": """
            SELECT COUNT(*)
            FROM agentic_enrichment_queue q
            JOIN agentic_sessions s ON q.session_id = s.id
            WHERE q.status = ?
              AND s.harness = 'opencode'
              AND s.probe_tag IS NULL
        """,
    }
    depths: list[tuple[str, str, int]] = []
    for source, sql in queries.items():
        for status in QUEUE_DEPTH_STATUSES:
            try:
                count = conn.execute(sql, (status,)).fetchone()[0]
                depths.append((source, status, int(count)))
            except sqlite3.OperationalError:
                # Table does not exist on this DB schema version; skip this
                # source rather than blanking the entire metric.
                break
    return depths


class BrainServer:
    def __init__(
        self,
        db_path: str = "",
        data_dir: str = "",
        inference_base_url: str = "http://127.0.0.1:42069/v1",
        inference_timeout_secs: float = 300.0,
        enrichment_model: str = "",
        embedding_model: str = "",
        query_model: str = "",
        poll_interval_secs: int = 5,
        enrichment_batch_size: int = 30,
        session_stale_secs: int = 120,
        max_claim_batch: int = DEFAULT_MAX_CLAIM_BATCH,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
        long_dwell_bypass_ms: int = 120_000,
        embed_reaper_interval_secs: int = 300,
        embed_reaper_batch_size: int = 50,
        embed_orphan_stale_secs: int = 900,
    ):
        if not db_path:
            db_path = str(Path.home() / ".local" / "share" / "hippo" / "hippo.db")
        if not data_dir:
            data_dir = str(Path.home() / ".local" / "share" / "hippo")
        self.db_path = db_path
        self.data_dir = data_dir
        self.client = InferenceClient(base_url=inference_base_url, timeout=inference_timeout_secs)
        self._preferred_model = enrichment_model
        self.enrichment_model = enrichment_model
        self.embedding_model = embedding_model
        self.query_model = query_model
        self.poll_interval_secs = poll_interval_secs
        self.enrichment_batch_size = enrichment_batch_size
        self.session_stale_secs = session_stale_secs
        self.max_claim_batch = max_claim_batch
        self.lock_timeout_ms = lock_timeout_ms
        self.long_dwell_bypass_ms = long_dwell_bypass_ms
        self.embed_reaper_interval_secs = embed_reaper_interval_secs
        self.embed_reaper_batch_size = embed_reaper_batch_size
        self.embed_orphan_stale_secs = embed_orphan_stale_secs
        self.enrichment_running = False
        self._paused: bool = False
        self._paused_at_iso: str | None = None
        self._query_inflight: int = 0  # incremented while /ask is executing
        # True while the enrichment loop body is running an LM-bound batch
        # (preflight + claim + gather). Used by /control/pause so callers like
        # hippo-bench can confirm prod is quiescent before claiming the LM slot.
        self._enrichment_active: bool = False
        # Set whenever a query is in flight; the enrichment loop sleeps on this
        # event so it wakes immediately rather than waiting out the full poll
        # interval before yielding to the LM.
        self._query_arrived = asyncio.Event()
        # Set by /control/resume so the loop wakes immediately from its paused
        # sleep instead of waiting out the full poll_interval_secs.
        self._resume_event = asyncio.Event()
        self._enrichment_task = None
        self._reaper_task = None
        self._embed_reaper_task = None
        self._vector_db = None
        self._vector_table = None
        if self.embedding_model:
            try:
                self._vector_db = open_vector_db(self.data_dir)
                self._vector_table = get_or_create_table(self._vector_db)
                logger.info("vector store initialized: %s", self._vector_table)
            except Exception as e:
                logger.error("failed to initialize vector store: %s", e)
        self.last_success_at_ms: int | None = None
        self.last_error: str | None = None
        self.last_error_at_ms: int | None = None

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        # Load sqlite-vec so the enrichment write path can delete a replaced
        # node's vector in the SAME transaction as the node row (see
        # replace_prior_agentic_nodes). Loading the extension only registers the
        # vec0 module; it does not create tables and is a no-op for read paths.
        # Close the just-opened connection if the load fails so a packaging
        # regression doesn't leak fds AND re-fault the error handlers that call
        # _get_conn() again to mark the queue row failed.
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            conn.close()
            raise
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in ACCEPTED_READ_VERSIONS:
            conn.close()
            raise RuntimeError(
                f"DB schema version mismatch: expected {EXPECTED_SCHEMA_VERSION}, found {version}. "
                "Please run migrations or delete the database."
            )
        return conn

    async def health(self, request: Request) -> JSONResponse:
        reachable = await self.client.is_reachable()

        queue_depth = 0
        queue_failed = 0
        claude_queue_depth = 0
        claude_queue_failed = 0
        browser_queue_depth = 0
        browser_queue_failed = 0
        workflow_queue_depth = 0
        workflow_queue_failed = 0
        db_reachable = True
        try:
            conn = self._get_conn()
            try:
                queue_depth = conn.execute(
                    "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'pending'"
                ).fetchone()[0]
                queue_failed = conn.execute(
                    "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'failed'"
                ).fetchone()[0]
                claude_queue_depth = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM agentic_enrichment_queue q
                    JOIN agentic_sessions s ON q.session_id = s.id
                    WHERE q.status = 'pending' AND s.harness = 'claude-code'
                    """
                ).fetchone()[0]
                claude_queue_failed = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM agentic_enrichment_queue q
                    JOIN agentic_sessions s ON q.session_id = s.id
                    WHERE q.status = 'failed' AND s.harness = 'claude-code'
                    """
                ).fetchone()[0]
                try:
                    browser_queue_depth = conn.execute(
                        "SELECT COUNT(*) FROM browser_enrichment_queue WHERE status = 'pending'"
                    ).fetchone()[0]
                    browser_queue_failed = conn.execute(
                        "SELECT COUNT(*) FROM browser_enrichment_queue WHERE status = 'failed'"
                    ).fetchone()[0]
                except Exception:
                    browser_queue_depth = 0
                    browser_queue_failed = 0
                try:
                    workflow_queue_depth = conn.execute(
                        "SELECT COUNT(*) FROM workflow_enrichment_queue WHERE status = 'pending'"
                    ).fetchone()[0]
                    workflow_queue_failed = conn.execute(
                        "SELECT COUNT(*) FROM workflow_enrichment_queue WHERE status = 'failed'"
                    ).fetchone()[0]
                except Exception:
                    workflow_queue_depth = 0
                    workflow_queue_failed = 0
            finally:
                conn.close()
        except Exception:
            db_reachable = False

        embed_model_drift: str | None = None
        if self._vector_db is not None and self.embedding_model:
            try:
                stored = get_stored_embed_model(self._vector_db)
                if stored is not None and stored != self.embedding_model:
                    embed_model_drift = f"stored={stored!r} live={self.embedding_model!r}"
            except Exception:
                pass

        status = "ok" if db_reachable else "degraded"
        if embed_model_drift:
            status = "degraded"

        return JSONResponse(
            {
                "status": status,
                "version": get_version(),
                "expected_schema_version": EXPECTED_SCHEMA_VERSION,
                "accepted_read_versions": sorted(ACCEPTED_READ_VERSIONS),
                "inference_reachable": reachable,
                "enrichment_running": self.enrichment_running,
                "paused": self._paused,
                "paused_at": self._paused_at_iso,
                "db_reachable": db_reachable,
                "queue_depth": queue_depth,
                "queue_failed": queue_failed,
                "claude_queue_depth": claude_queue_depth,
                "claude_queue_failed": claude_queue_failed,
                "browser_queue_depth": browser_queue_depth,
                "browser_queue_failed": browser_queue_failed,
                "workflow_queue_depth": workflow_queue_depth,
                "workflow_queue_failed": workflow_queue_failed,
                "enrichment_model": self.enrichment_model,
                "enrichment_model_preferred": self._preferred_model,
                "query_inflight": self._query_inflight,
                "embed_model_drift": embed_model_drift,
                "last_success_at_ms": self.last_success_at_ms,
                "last_error": self.last_error,
                "last_error_at_ms": self.last_error_at_ms,
                # Distinguishes "telemetry configured-on AND running" from
                # "telemetry configured-on but dead" — the failure mode that
                # caused dashboards to silently go dark when the deployed brain
                # venv was out of sync with pyproject.toml.
                "telemetry_enabled": is_telemetry_enabled(),
                "telemetry_active": is_telemetry_active(),
            }
        )

    def _query_lexical(self, text: str, limit: int = 10) -> dict:
        """Lexical substring search over events and knowledge nodes."""
        conn = self._get_conn()
        try:
            pattern = f"%{text}%"
            cursor = conn.execute(
                """SELECT id, command, cwd, timestamp
                   FROM events
                   WHERE command LIKE ?
                     AND probe_tag IS NULL
                   ORDER BY timestamp DESC LIMIT ?""",
                (pattern, limit),
            )
            events = [
                {"event_id": r[0], "command": r[1], "cwd": r[2], "timestamp": r[3]}
                for r in cursor.fetchall()
            ]

            cursor = conn.execute(
                """SELECT id, uuid, content, embed_text
                   FROM knowledge_nodes
                   WHERE content LIKE ?
                       OR embed_text LIKE ?
                   ORDER BY created_at DESC LIMIT ?""",
                (pattern, pattern, limit),
            )
            nodes = [
                {"id": r[0], "uuid": r[1], "content": r[2], "embed_text": r[3]}
                for r in cursor.fetchall()
            ]
            return {"mode": "lexical", "events": events, "nodes": nodes}
        finally:
            conn.close()

    async def query(self, request: Request) -> JSONResponse:
        body = await request.json()
        text = body.get("text", "")
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)

        mode = body.get("mode", "semantic")
        limit = body.get("limit", 10)

        try:
            limit = int(limit)
        except (TypeError, ValueError):  # fmt: skip
            return JSONResponse({"error": "limit must be an integer"}, status_code=400)

        if limit <= 0:
            return JSONResponse({"error": "limit must be greater than 0"}, status_code=400)
        if limit > MAX_QUERY_LIMIT:
            return JSONResponse(
                {"error": f"limit must be <= {MAX_QUERY_LIMIT}"},
                status_code=400,
            )

        if mode == "lexical":
            try:
                return JSONResponse(self._query_lexical(text, limit=limit))
            except Exception as e:
                logger.error("query error: %s", e)
                return JSONResponse({"error": str(e)}, status_code=500)

        # Semantic mode — fall back to lexical if unavailable
        if not self.embedding_model or self._vector_table is None:
            try:
                result = self._query_lexical(text, limit=limit)
                result["warning"] = "semantic search unavailable, fell back to lexical"
                return JSONResponse(result)
            except Exception as e:
                logger.error("query error: %s", e)
                return JSONResponse({"error": str(e)}, status_code=500)

        try:
            vecs = await self.client.embed([text], model=self.embedding_model)
            hits = search_similar(self._vector_table, vecs[0], limit=limit)
            results = [
                {
                    "score": round(1.0 - hit.get("_distance", 0.0), 4),
                    "summary": hit.get("summary", ""),
                    "tags": hit.get("tags", ""),
                    "key_decisions": hit.get("key_decisions", ""),
                    "problems_encountered": hit.get("problems_encountered", ""),
                    "cwd": hit.get("cwd", ""),
                    "git_branch": hit.get("git_branch", ""),
                    "session_id": hit.get("session_id", 0),
                    "commands_raw": hit.get("commands_raw", ""),
                    "embed_text": hit.get("embed_text", ""),
                }
                for hit in hits
            ]
            return JSONResponse({"mode": "semantic", "results": results})
        except Exception as e:
            logger.warning("semantic search failed, falling back to lexical: %s", e)
            try:
                result = self._query_lexical(text, limit=limit)
                result["warning"] = f"semantic search failed: {e}"
                return JSONResponse(result)
            except Exception as e2:
                logger.error("query error: %s", e2)
                return JSONResponse({"error": str(e2)}, status_code=500)

    async def list_knowledge(self, request: Request) -> JSONResponse:
        """List knowledge nodes with pagination and filtering."""
        import json

        limit = request.query_params.get("limit", "20")
        offset = request.query_params.get("offset", "0")
        node_type = request.query_params.get("node_type")
        since_ms = request.query_params.get("since_ms")

        try:
            limit = int(limit)
            offset = int(offset)
        except ValueError:
            return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)
        if limit <= 0 or limit > MAX_LIST_LIMIT:
            return JSONResponse(
                {"error": f"limit must be in 1..{MAX_LIST_LIMIT}"},
                status_code=400,
            )
        if offset < 0:
            return JSONResponse({"error": "offset must be >= 0"}, status_code=400)

        if since_ms:
            try:
                since_ms = int(since_ms)
            except ValueError:
                return JSONResponse({"error": "since_ms must be an integer"}, status_code=400)

        conn = self._get_conn()
        try:
            sql = (
                "SELECT id, uuid, content, node_type, outcome, tags, created_at "
                "FROM knowledge_nodes"
            )
            params = []
            conditions = []

            if node_type:
                conditions.append("node_type = ?")
                params.append(node_type)

            if since_ms:
                conditions.append("created_at > ?")
                params.append(since_ms)

            if conditions:
                sql += " WHERE " + " AND ".join(conditions)

            sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor = conn.execute(sql, params)
            nodes = []
            for r in cursor.fetchall():
                try:
                    tags = json.loads(r[5]) if r[5] else []
                except (json.JSONDecodeError, TypeError):  # fmt: skip
                    tags = []
                nodes.append(
                    {
                        "id": r[0],
                        "uuid": r[1],
                        "content": r[2] or "",
                        "node_type": r[3],
                        "outcome": r[4],
                        "tags": tags,
                        "created_at": r[6],
                    }
                )

            count_sql = "SELECT COUNT(*) FROM knowledge_nodes"
            count_params = params[:-2]
            if conditions:
                count_sql += " WHERE " + " AND ".join(conditions)
            total = conn.execute(count_sql, count_params).fetchone()[0]

            return JSONResponse({"nodes": nodes, "total": total})
        except Exception as e:
            logger.error("list_knowledge error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            conn.close()

    async def get_knowledge(self, request: Request) -> JSONResponse:
        """Get a single knowledge node by ID."""
        import json

        node_id = request.path_params["id"]

        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT id, uuid, content, embed_text, node_type, outcome, tags, created_at "
                "FROM knowledge_nodes WHERE id = ?",
                (node_id,),
            )
            row = cursor.fetchone()
            if not row:
                return JSONResponse({"error": "Knowledge node not found"}, status_code=404)

            try:
                tags = json.loads(row[6]) if row[6] else []
            except (json.JSONDecodeError, TypeError):  # fmt: skip
                tags = []

            related_entities = [
                {"id": entity_id, "name": name, "type": entity_type}
                for entity_id, name, entity_type in conn.execute(
                    """
                    SELECT e.id, e.name, e.type
                    FROM entities e
                    JOIN knowledge_node_entities kne ON kne.entity_id = e.id
                    WHERE kne.knowledge_node_id = ?
                    ORDER BY e.type, e.name
                    """,
                    (node_id,),
                ).fetchall()
            ]
            related_events = [
                {"id": event_id, "command": command}
                for event_id, command in conn.execute(
                    """
                    SELECT ev.id, ev.command
                    FROM events ev
                    JOIN knowledge_node_events kne ON kne.event_id = ev.id
                    WHERE kne.knowledge_node_id = ?
                      AND ev.probe_tag IS NULL
                    ORDER BY ev.timestamp DESC
                    """,
                    (node_id,),
                ).fetchall()
            ]

            return JSONResponse(
                {
                    "id": row[0],
                    "uuid": row[1],
                    "content": row[2] or "",
                    "embed_text": row[3],
                    "node_type": row[4],
                    "outcome": row[5],
                    "tags": tags,
                    "created_at": row[7],
                    "related_entities": related_entities,
                    "related_events": related_events,
                }
            )
        except Exception as e:
            logger.error("get_knowledge error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            conn.close()

    async def list_events(self, request: Request) -> JSONResponse:
        """List shell events with pagination and filtering."""
        limit = request.query_params.get("limit", "20")
        offset = request.query_params.get("offset", "0")
        session_id = request.query_params.get("session_id")
        since_ms = request.query_params.get("since_ms")
        project = request.query_params.get("project")

        try:
            limit = int(limit)
            offset = int(offset)
        except ValueError:
            return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)
        if limit <= 0 or limit > MAX_LIST_LIMIT:
            return JSONResponse(
                {"error": f"limit must be in 1..{MAX_LIST_LIMIT}"},
                status_code=400,
            )
        if offset < 0:
            return JSONResponse({"error": "offset must be >= 0"}, status_code=400)

        if since_ms:
            try:
                since_ms = int(since_ms)
            except ValueError:
                return JSONResponse({"error": "since_ms must be an integer"}, status_code=400)

        conn = self._get_conn()
        try:
            sql = (
                "SELECT id, session_id, timestamp, command, exit_code, duration_ms, cwd, git_branch "
                "FROM events"
            )
            params = []
            conditions = ["probe_tag IS NULL"]

            if session_id:
                try:
                    session_id = int(session_id)
                except ValueError:
                    return JSONResponse({"error": "session_id must be an integer"}, status_code=400)
                conditions.append("session_id = ?")
                params.append(session_id)

            if since_ms:
                conditions.append("timestamp > ?")
                params.append(since_ms)

            if project:
                conditions.append("cwd LIKE ?")
                params.append(f"%{project}%")

            if conditions:
                sql += " WHERE " + " AND ".join(conditions)

            sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor = conn.execute(sql, params)
            events = [
                {
                    "id": r[0],
                    "session_id": r[1],
                    "timestamp": r[2],
                    "command": r[3],
                    "exit_code": r[4],
                    "duration_ms": r[5],
                    "cwd": r[6],
                    "git_branch": r[7],
                }
                for r in cursor.fetchall()
            ]

            count_sql = "SELECT COUNT(*) FROM events"
            count_params = params[:-2]
            if conditions:
                count_sql += " WHERE " + " AND ".join(conditions)
            total = conn.execute(count_sql, count_params).fetchone()[0]

            return JSONResponse({"events": events, "total": total})
        except Exception as e:
            logger.error("list_events error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            conn.close()

    async def list_sessions(self, request: Request) -> JSONResponse:
        """List sessions with event counts, pagination, and filtering."""
        limit = request.query_params.get("limit", "20")
        offset = request.query_params.get("offset", "0")
        since_ms = request.query_params.get("since_ms")

        try:
            limit = int(limit)
            offset = int(offset)
        except ValueError:
            return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)
        if limit <= 0 or limit > MAX_LIST_LIMIT:
            return JSONResponse(
                {"error": f"limit must be in 1..{MAX_LIST_LIMIT}"},
                status_code=400,
            )
        if offset < 0:
            return JSONResponse({"error": "offset must be >= 0"}, status_code=400)

        if since_ms:
            try:
                since_ms = int(since_ms)
            except ValueError:
                return JSONResponse({"error": "since_ms must be an integer"}, status_code=400)

        conn = self._get_conn()
        try:
            sql = """
                SELECT s.id, s.start_time, s.hostname, s.shell,
                       (SELECT COUNT(*) FROM events e
                        WHERE e.session_id = s.id AND e.probe_tag IS NULL) as event_count
                FROM sessions s
            """
            params = []
            conditions = []

            if since_ms:
                conditions.append("s.start_time > ?")
                params.append(since_ms)

            if conditions:
                sql += " WHERE " + " AND ".join(conditions)

            sql += " ORDER BY s.start_time DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor = conn.execute(sql, params)
            sessions = [
                {
                    "id": r[0],
                    "start_time": r[1],
                    "hostname": r[2],
                    "shell": r[3],
                    "event_count": r[4],
                }
                for r in cursor.fetchall()
            ]

            count_sql = "SELECT COUNT(*) FROM sessions s"
            count_params = []
            if conditions:
                count_sql += " WHERE " + " AND ".join(conditions)
                count_params = params[:-2]
            total = conn.execute(count_sql, count_params).fetchone()[0]

            return JSONResponse({"sessions": sessions, "total": total})
        except Exception as e:
            logger.error("list_sessions error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            conn.close()

    async def ask(self, request: Request) -> JSONResponse:
        """RAG endpoint: retrieve relevant knowledge and synthesize an answer."""
        body = await request.json()
        question = body.get("question", "")
        if not question:
            return JSONResponse({"error": "question is required"}, status_code=400)

        limit = body.get("limit", 10)
        try:
            limit = int(limit)
        except (TypeError, ValueError):  # fmt: skip
            return JSONResponse({"error": "limit must be an integer"}, status_code=400)
        if limit <= 0:
            return JSONResponse({"error": "limit must be greater than 0"}, status_code=400)
        if limit > MAX_QUERY_LIMIT:
            return JSONResponse(
                {"error": f"limit must be <= {MAX_QUERY_LIMIT}"},
                status_code=400,
            )

        if not self.embedding_model or self._vector_table is None:
            return JSONResponse(
                {"error": "Semantic search unavailable (no embedding model or vector store)"},
                status_code=503,
            )

        # Use query_model, fall back to dynamically-resolved enrichment model
        model = self.query_model or self.enrichment_model
        if not model:
            return JSONResponse(
                {"error": "No query model configured (set models.query in config.toml)"},
                status_code=503,
            )

        self._query_inflight += 1
        self._query_arrived.set()
        try:
            result = await rag_ask(
                question=question,
                inference_client=self.client,
                vector_table=self._vector_table,
                query_model=model,
                embedding_model=self.embedding_model,
                limit=limit,
            )
        finally:
            self._query_inflight = max(0, self._query_inflight - 1)
            if self._query_inflight == 0:
                self._query_arrived.clear()

        status = 200 if "answer" in result else 502
        return JSONResponse(result, status_code=status)

    async def control_pause(self, request: Request) -> JSONResponse:
        """Pause the enrichment loop. Idempotent.

        in_flight_finished reflects both /ask queries and the enrichment
        loop body — bench callers need both quiescent before they own the
        inference server slot.
        """
        if not self._paused:
            self._paused = True
            self._paused_at_iso = _dt.datetime.now(tz=_dt.UTC).isoformat()
        in_flight_finished = self._query_inflight == 0 and not self._enrichment_active
        return JSONResponse(
            {
                "paused_at": self._paused_at_iso,
                "in_flight_finished": in_flight_finished,
                "enrichment_active": self._enrichment_active,
                "query_inflight": self._query_inflight,
            }
        )

    async def control_resume(self, request: Request) -> JSONResponse:
        """Resume the enrichment loop. Idempotent.

        Sets _resume_event so the loop wakes immediately rather than
        waiting out the full poll_interval_secs sleep.
        """
        self._paused = False
        self._paused_at_iso = None
        self._resume_event.set()
        return JSONResponse({"resumed_at": _dt.datetime.now(tz=_dt.UTC).isoformat()})

    async def vault_export(self, request: Request) -> JSONResponse:
        body = await request.json()
        out = body.get("out")
        if not out:
            return JSONResponse({"error": "out is required"}, status_code=400)
        conn = self._get_conn()
        try:
            summary = export_vault(
                conn,
                out_dir=out,
                hippo_version=_hippo_version,
                related_top_k=int(body.get("related_top_k", 8)),
                hub_degree_cap=int(body.get("hub_degree_cap", 200)),
                hub_node_list_cap=int(body.get("hub_node_list_cap", 200)),
                shard_by=str(body.get("shard_by", "month")),
                full=bool(body.get("full", False)),
            )
            return JSONResponse(summary)
        except (RuntimeError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()

    def _resolve_model_from_preflight(self, loaded: list[str]) -> bool:
        """Sync model selection from preflight's already-fetched model list."""
        return self._pick_enrichment_model(loaded)

    def _record_preflight_to_source_health(self, decision) -> None:
        """Mirror the preflight decision into source_health['brain-preflight'].

        The watchdog reads source_health to evaluate invariants. I-12 alarms
        when `consecutive_failures > 12`, so we need to bump the counter on
        every failed preflight and reset it on every success. Schema-table
        absence (test DBs / pre-migration installs) is swallowed silently
        per the existing source_health convention.
        """
        now_ms = int(time.time() * 1000)
        try:
            conn = self._get_conn()
        except Exception as e:
            logger.debug("brain-preflight source_health: get_conn failed: %s", e)
            return
        try:
            # Upsert rather than UPDATE-only so a v13→v14-migrated DB whose
            # daemon has not yet run the post-migration idempotent seed (see
            # storage.rs:754) still gets a row on the first preflight cycle.
            # The Rust side uses INSERT OR IGNORE + UPDATE; we collapse that
            # into a single ON CONFLICT upsert here.
            if decision.proceed:
                conn.execute(
                    """
                    INSERT INTO source_health (
                        source, last_event_ts, last_success_ts,
                        consecutive_failures, updated_at
                    )
                    VALUES ('brain-preflight', ?1, ?1, 0, ?1)
                    ON CONFLICT(source) DO UPDATE SET
                        last_event_ts        = ?1,
                        last_success_ts      = ?1,
                        consecutive_failures = 0,
                        last_error_msg       = NULL,
                        updated_at           = ?1
                    """,
                    (now_ms,),
                )
            else:
                err_msg = (decision.error or decision.reason or "preflight_failed")[:500]
                conn.execute(
                    """
                    INSERT INTO source_health (
                        source, last_error_ts, last_error_msg,
                        consecutive_failures, updated_at
                    )
                    VALUES ('brain-preflight', ?1, ?2, 1, ?1)
                    ON CONFLICT(source) DO UPDATE SET
                        last_error_ts        = ?1,
                        last_error_msg       = ?2,
                        consecutive_failures = source_health.consecutive_failures + 1,
                        updated_at           = ?1
                    """,
                    (now_ms, err_msg),
                )
            conn.commit()
        except sqlite3.OperationalError as e:
            # source_health absent or schema older than v8 — swallow per
            # existing daemon-side convention.
            logger.debug("brain-preflight source_health write skipped: %s", e)
        finally:
            conn.close()

    def _pick_enrichment_model(self, loaded: list[str]) -> bool:
        """Filter chat models from `loaded` and set self.enrichment_model.

        Returns False when no chat models are available.
        """
        embedding_hints = ("embed", "nomic", "modernbert")
        chat_models = [m for m in loaded if not any(h in m.lower() for h in embedding_hints)]

        if not chat_models:
            return False

        if self._preferred_model and self._preferred_model in chat_models:
            if self.enrichment_model != self._preferred_model:
                logger.info("enrichment model restored: %s", self._preferred_model)
                self.enrichment_model = self._preferred_model
            return True

        fallback = chat_models[0]
        if self.enrichment_model != fallback:
            logger.info(
                "enrichment model fallback: %s -> %s (preferred %s not loaded)",
                self.enrichment_model,
                fallback,
                self._preferred_model,
            )
            self.enrichment_model = fallback
        return True

    async def _enrichment_loop(self):
        """Background enrichment polling loop.

        Claims work from all three sources sequentially (fast SQLite ops), then
        processes them concurrently via asyncio.gather. LLM calls still serialize
        at the inference-server level, but DB writes and embeddings from one batch
        overlap with the next LLM call.
        """
        self.enrichment_running = True
        worker_id = "brain-enrichment"
        try:
            while True:
                try:
                    if self._paused:
                        # Wake immediately when /control/resume sets the event;
                        # otherwise re-poll _paused after poll_interval_secs.
                        try:
                            await asyncio.wait_for(
                                self._resume_event.wait(),
                                timeout=self.poll_interval_secs,
                            )
                        except TimeoutError:
                            pass
                        self._resume_event.clear()
                        continue
                    # Sleep up to poll_interval_secs, waking early if a query
                    # arrives mid-sleep. The event-based wake means we yield
                    # to the inference-server slot instead of waiting out the
                    # interval and starting a competing enrichment batch.
                    try:
                        await asyncio.wait_for(
                            self._query_arrived.wait(),
                            timeout=self.poll_interval_secs,
                        )
                    except TimeoutError:
                        pass

                    # If queries are running (or one just arrived), drain them
                    # before claiming work. Polling at 100 ms granularity is
                    # cheap and prevents a tight wait_for spin while the event
                    # stays set across multiple in-flight queries.
                    while self._query_inflight > 0:
                        logger.debug(
                            "enrichment waiting for %d in-flight queries to drain",
                            self._query_inflight,
                        )
                        await asyncio.sleep(0.1)

                    self._enrichment_active = True
                    try:
                        decision = await preflight_inference(
                            self.client, self._preferred_model or None, allow_fallback=True
                        )
                        # Mirror the preflight outcome into source_health so
                        # watchdog I-12 can alarm on sustained failures
                        # (motivating incident: silent [lmstudio]->[inference]
                        # drift made preflight fail for hours with no alarm).
                        self._record_preflight_to_source_health(decision)
                        if not decision.proceed:
                            continue

                        # Sync resolved enrichment_model from preflight result; falls
                        # back to first chat model when preferred isn't loaded.
                        if not self._resolve_model_from_preflight(decision.loaded_models):
                            continue

                        conn = self._get_conn()
                        try:
                            # Claim all work upfront (sequential — avoids SQLite write contention)
                            shell_chunks = claim_pending_events_by_session(
                                conn,
                                self.enrichment_batch_size,
                                worker_id,
                                self.session_stale_secs,
                                max_claim_batch=self.max_claim_batch,
                                stale_lock_timeout_ms=self.lock_timeout_ms,
                            )
                            try:
                                claude_batches = claim_pending_claude_segments(
                                    conn,
                                    worker_id,
                                    max_claim_batch=self.max_claim_batch,
                                    stale_lock_timeout_ms=self.lock_timeout_ms,
                                )
                            except Exception as e:
                                logger.debug("no claude segments to process: %s", e)
                                claude_batches = []
                            try:
                                browser_batches = claim_pending_browser_events(
                                    conn,
                                    worker_id,
                                    stale_secs=60,
                                    max_claim_batch=self.max_claim_batch,
                                    stale_lock_timeout_ms=self.lock_timeout_ms,
                                    long_dwell_bypass_ms=self.long_dwell_bypass_ms,
                                )
                            except Exception as e:
                                logger.warning("browser claim error: %s", e, exc_info=True)
                                browser_batches = []
                            try:
                                workflow_run_ids = claim_pending_workflow_runs(
                                    conn,
                                    worker_id,
                                    stale_lock_timeout_ms=self.lock_timeout_ms,
                                    max_claim_batch=self.max_claim_batch,
                                )
                            except Exception as e:
                                logger.warning("workflow claim error: %s", e, exc_info=True)
                                workflow_run_ids = []
                            try:
                                opencode_batches = claim_pending_opencode_segments(
                                    conn,
                                    worker_id,
                                    max_claim_batch=self.max_claim_batch,
                                    stale_lock_timeout_ms=self.lock_timeout_ms,
                                )
                            except Exception as e:
                                # AP-11: a real exception here (SQL error,
                                # schema mismatch) is a structural failure and
                                # must not be downgraded to debug. Mirrors the
                                # browser/workflow claim paths above.
                                logger.warning("opencode claim error: %s", e, exc_info=True)
                                opencode_batches = []

                            if (
                                not shell_chunks
                                and not claude_batches
                                and not browser_batches
                                and not workflow_run_ids
                                and not opencode_batches
                            ):
                                continue

                            # Process all sources concurrently — each method uses its own
                            # DB connection for writes so they don't block each other.
                            # Shell receives the claim conn for read-only browser correlation.
                            t0 = time.monotonic()
                            await asyncio.gather(
                                self._enrich_shell_batches(shell_chunks, conn),
                                self._enrich_claude_batches(claude_batches),
                                self._enrich_browser_batches(browser_batches),
                                self._enrich_workflow_runs(workflow_run_ids),
                                self._enrich_opencode_batches(opencode_batches),
                            )
                            _hist(_loop_duration, (time.monotonic() - t0) * 1000)
                        finally:
                            conn.close()
                    finally:
                        self._enrichment_active = False
                except Exception as e:
                    self.last_error = str(e) or type(e).__name__
                    self.last_error_at_ms = int(time.time() * 1000)
                    logger.error("enrichment loop error: %s", e, exc_info=True)
                    await asyncio.sleep(self.poll_interval_secs)
        finally:
            self.enrichment_running = False

    async def _call_llm_with_retries(self, system_prompt, prompt, source_label):
        """Call the inference server with up to 3 retries on parse failure."""
        last_err: Exception = RuntimeError("no attempts made")
        for attempt in range(3):
            try:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]
                if attempt > 0:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was not valid JSON. "
                                "Output ONLY a JSON object, no explanation or markdown."
                            ),
                        }
                    )
                raw = await self.client.chat(
                    messages=messages,
                    model=self.enrichment_model,
                )
                logger.debug(
                    "LLM raw response (%d chars): %s",
                    len(raw or ""),
                    repr(raw)[:200],
                )
                result = parse_enrichment_response(raw)
                return result
            except Exception as e:
                last_err = e
                logger.warning(
                    "%s enrichment attempt %d failed: %s",
                    source_label,
                    attempt + 1,
                    e,
                    exc_info=True,
                )
        raise last_err

    def _record_success(self):
        self.last_success_at_ms = int(time.time() * 1000)
        self.last_error = None
        self.last_error_at_ms = None

    def _record_error(self, e):
        # last-writer-wins: concurrent shell/claude/browser batches may overwrite
        # each other's error state. The health endpoint reflects the most recent
        # writer, not necessarily the most recent error chronologically.
        err_msg = str(e) or type(e).__name__
        self.last_error = err_msg
        self.last_error_at_ms = int(time.time() * 1000)
        return err_msg

    def _log_enrichment_failure(self, queue_name: str, stage: str, e: Exception, **fields) -> None:
        """Emit a structured failure log so wedges are greppable in minutes.

        Includes queue_name, claim_count, claim_age_ms, exception_type,
        enrichment_model, stage — the fields the R-22 spec calls for.
        """
        exc_type = type(e).__name__
        msg_fields = {
            "queue_name": queue_name,
            "stage": stage,
            "exception_type": exc_type,
            "enrichment_model": self.enrichment_model,
            **fields,
        }
        logger.error(
            "enrichment failed %s",
            " ".join(f"{k}={v!r}" for k, v in msg_fields.items()),
            exc_info=True,
            extra=msg_fields,
        )

    async def _enrich_shell_batches(self, chunks, claim_conn):
        """Process shell event batches with background embeddings."""
        if not chunks:
            return
        embed_tasks = []
        for events in chunks:
            event_ids = [e["id"] for e in events]
            logger.info("claimed %d events: %s", len(event_ids), event_ids)
            _add(_events_claimed, len(event_ids), source="shell")

            browser_context = ""
            try:
                start_ts = min(e["timestamp"] for e in events)
                end_ts = max(e["timestamp"] for e in events)
                correlated = get_correlated_browser_events(claim_conn, start_ts, end_ts)
                browser_context = format_browser_context_for_shell_prompt(correlated)
            except Exception as e:
                logger.debug("browser correlation skipped: %s", e)

            prompt = build_enrichment_prompt(events, browser_context=browser_context)
            logger.info("calling inference server (prompt len: %d chars)", len(prompt))

            tracer = _get_tracer()
            span = (
                tracer.start_as_current_span(
                    "enrichment.shell",
                    attributes={
                        "hippo.event_count": len(event_ids),
                        "hippo.model": self.enrichment_model,
                    },
                )
                if tracer
                else nullcontext()
            )
            batch_start_ms = int(time.time() * 1000)
            with span:
                try:
                    result = await self._call_llm_with_retries(SYSTEM_PROMPT, prompt, "shell")
                    conn = self._get_conn()
                    try:
                        node_id = write_knowledge_node(
                            conn, result, event_ids, self.enrichment_model
                        )
                    finally:
                        conn.close()
                    _add(_nodes_created, source="shell")
                    self._record_success()
                    logger.info("enriched %d events -> node %d", len(event_ids), node_id)

                    if self.embedding_model:
                        node_dict = {
                            "id": node_id,
                            "session_id": events[0].get("session_id", 0),
                            "captured_at": int(time.time() * 1000),
                            "commands_raw": " ; ".join(e.get("command", "") for e in events),
                            "cwd": events[0].get("cwd", ""),
                            "git_branch": events[0].get("git_branch", ""),
                            "git_repo": "",
                            "outcome": result.outcome,
                            "tags": result.tags,
                            "key_decisions": result.key_decisions,
                            "problems_encountered": result.problems_encountered,
                            "entities": result.entities
                            if isinstance(result.entities, dict)
                            else {},
                            "embed_text": result.embed_text,
                            "summary": result.summary,
                            "enrichment_model": self.enrichment_model,
                        }
                        embed_tasks.append(
                            asyncio.create_task(self._embed_node(node_id, node_dict, "shell"))
                        )
                except Exception as e:
                    _add(_enrichment_failures, source="shell")
                    err_msg = self._record_error(e)
                    self._log_enrichment_failure(
                        "enrichment_queue",
                        "shell.chat",
                        e,
                        claim_count=len(event_ids),
                        claim_age_ms=int(time.time() * 1000) - batch_start_ms,
                    )
                    retry_conn = self._get_conn()
                    try:
                        mark_queue_failed(retry_conn, event_ids, err_msg)
                    finally:
                        retry_conn.close()

        if embed_tasks:
            await asyncio.gather(*embed_tasks, return_exceptions=True)

    async def _enrich_claude_batches(self, batches):
        """Process Claude session segment batches with background embeddings."""
        if not batches:
            return
        embed_tasks = []
        for segments in batches:
            segment_ids = [s["id"] for s in segments]
            source_label = _source_label_for_claude_segments(segments)
            prompt = "\n---\n\n".join(s["summary_text"] for s in segments)
            logger.info("claimed %d %s segments: %s", len(segment_ids), source_label, segment_ids)
            _add(_events_claimed, len(segment_ids), source=source_label)

            tracer = _get_tracer()
            span = (
                tracer.start_as_current_span(
                    f"enrichment.{source_label}",
                    attributes={
                        "hippo.event_count": len(segment_ids),
                        "hippo.model": self.enrichment_model,
                    },
                )
                if tracer
                else nullcontext()
            )
            batch_start_ms = int(time.time() * 1000)
            with span:
                try:
                    result = await self._call_llm_with_retries(
                        CLAUDE_SYSTEM_PROMPT, prompt, source_label
                    )
                    conn = self._get_conn()
                    try:
                        node_id = write_claude_knowledge_node(
                            conn,
                            result,
                            segment_ids,
                            self.enrichment_model,
                            content_hashes=[s.get("content_hash") for s in segments],
                        )
                    finally:
                        conn.close()
                    _add(_nodes_created, source=source_label)
                    self._record_success()
                    logger.info(
                        "enriched %d %s segments -> node %d",
                        len(segment_ids),
                        source_label,
                        node_id,
                    )

                    if self.embedding_model:
                        import json as _json

                        all_tools = []
                        for s in segments:
                            try:
                                tools = _json.loads(s.get("tool_calls_json", "[]"))
                                all_tools.extend(f"{t['name']}: {t['summary']}" for t in tools)
                            except (_json.JSONDecodeError, KeyError):  # fmt: skip
                                pass
                        node_dict = {
                            "id": node_id,
                            "session_id": 0,
                            "captured_at": int(time.time() * 1000),
                            "commands_raw": " ; ".join(all_tools[:50]),
                            "cwd": segments[0].get("cwd", ""),
                            "git_branch": segments[0].get("git_branch", ""),
                            "git_repo": "",
                            "outcome": result.outcome,
                            "tags": result.tags,
                            "key_decisions": result.key_decisions,
                            "problems_encountered": result.problems_encountered,
                            "entities": result.entities
                            if isinstance(result.entities, dict)
                            else {},
                            "embed_text": result.embed_text,
                            "summary": result.summary,
                            "enrichment_model": self.enrichment_model,
                        }
                        embed_tasks.append(
                            asyncio.create_task(self._embed_node(node_id, node_dict, source_label))
                        )
                except Exception as e:
                    _add(_enrichment_failures, source=source_label)
                    err_msg = self._record_error(e)
                    self._log_enrichment_failure(
                        "agentic_enrichment_queue",
                        f"{source_label}.chat",
                        e,
                        claim_count=len(segment_ids),
                        claim_age_ms=int(time.time() * 1000) - batch_start_ms,
                    )
                    retry_conn = self._get_conn()
                    try:
                        mark_claude_queue_failed(
                            retry_conn,
                            segment_ids,
                            err_msg,
                            content_hashes=[s.get("content_hash") for s in segments],
                        )
                    finally:
                        retry_conn.close()

        if embed_tasks:
            await asyncio.gather(*embed_tasks, return_exceptions=True)

    async def _enrich_browser_batches(self, batches):
        """Process browser event batches with background embeddings."""
        if not batches:
            return
        embed_tasks = []
        for events in batches:
            event_ids = [e["id"] for e in events]
            logger.info("claimed %d browser events: %s", len(event_ids), event_ids)
            _add(_events_claimed, len(event_ids), source="browser")
            prompt = build_browser_enrichment_prompt(events)

            tracer = _get_tracer()
            span = (
                tracer.start_as_current_span(
                    "enrichment.browser",
                    attributes={
                        "hippo.event_count": len(event_ids),
                        "hippo.model": self.enrichment_model,
                    },
                )
                if tracer
                else nullcontext()
            )
            batch_start_ms = int(time.time() * 1000)
            with span:
                try:
                    result = await self._call_llm_with_retries(
                        BROWSER_SYSTEM_PROMPT, prompt, "browser"
                    )
                    conn = self._get_conn()
                    try:
                        node_id = write_browser_knowledge_node(
                            conn, result, event_ids, self.enrichment_model
                        )
                    finally:
                        conn.close()
                    _add(_nodes_created, source="browser")
                    self._record_success()
                    if node_id is None:
                        logger.info(
                            "browser events (%d) linked to existing node via content dedup",
                            len(event_ids),
                        )
                    else:
                        logger.info(
                            "enriched %d browser events -> node %d",
                            len(event_ids),
                            node_id,
                        )

                    if node_id is not None and self.embedding_model:
                        node_dict = {
                            "id": node_id,
                            "session_id": 0,
                            "captured_at": int(time.time() * 1000),
                            "commands_raw": " ; ".join(e.get("url", "") for e in events),
                            "cwd": "",
                            "git_branch": "",
                            "git_repo": "",
                            "outcome": result.outcome,
                            "tags": result.tags,
                            "key_decisions": result.key_decisions,
                            "problems_encountered": result.problems_encountered,
                            "entities": result.entities
                            if isinstance(result.entities, dict)
                            else {},
                            "embed_text": result.embed_text,
                            "summary": result.summary,
                            "enrichment_model": self.enrichment_model,
                        }
                        embed_tasks.append(
                            asyncio.create_task(self._embed_node(node_id, node_dict, "browser"))
                        )
                except Exception as e:
                    _add(_enrichment_failures, source="browser")
                    err_msg = self._record_error(e)
                    self._log_enrichment_failure(
                        "browser_enrichment_queue",
                        "browser.chat",
                        e,
                        claim_count=len(event_ids),
                        claim_age_ms=int(time.time() * 1000) - batch_start_ms,
                    )
                    retry_conn = self._get_conn()
                    try:
                        mark_browser_queue_failed(retry_conn, event_ids, err_msg)
                    finally:
                        retry_conn.close()

        if embed_tasks:
            await asyncio.gather(*embed_tasks, return_exceptions=True)

    async def _enrich_workflow_runs(self, run_ids: list[int]):
        """Process workflow run enrichment — create change-outcome knowledge nodes."""
        if not run_ids:
            return
        query_model = self.query_model or self.enrichment_model
        embed_tasks = []
        for run_id in run_ids:
            logger.info("enriching workflow run %d", run_id)
            _add(_events_claimed, 1, source="workflow")

            tracer = _get_tracer()
            span = (
                tracer.start_as_current_span(
                    "enrichment.workflow",
                    attributes={
                        "hippo.run_id": run_id,
                        "hippo.model": query_model,
                    },
                )
                if tracer
                else nullcontext()
            )
            batch_start_ms = int(time.time() * 1000)
            with span:
                try:
                    result = await enrich_one_async(
                        self.db_path,
                        run_id=run_id,
                        inference=self.client,
                        query_model=query_model,
                    )
                    _add(_nodes_created, source="workflow")
                    self._record_success()
                    logger.info("enriched workflow run %d -> knowledge node", run_id)
                    if result is not None and self.embedding_model:
                        node_id, node_dict = result
                        embed_tasks.append(
                            asyncio.create_task(self._embed_node(node_id, node_dict, "workflow"))
                        )
                except Exception as e:
                    _add(_enrichment_failures, source="workflow")
                    err_msg = self._record_error(e)
                    self._log_enrichment_failure(
                        "workflow_enrichment_queue",
                        "workflow.enrich",
                        e,
                        claim_count=1,
                        run_id=run_id,
                        claim_age_ms=int(time.time() * 1000) - batch_start_ms,
                    )
                    retry_conn = self._get_conn()
                    try:
                        mark_workflow_queue_failed(retry_conn, run_id, err_msg)
                    finally:
                        retry_conn.close()

        if embed_tasks:
            await asyncio.gather(*embed_tasks, return_exceptions=True)

    async def _enrich_opencode_batches(self, batches):
        """Process opencode session segment batches via the agentic queue."""
        if not batches:
            return
        embed_tasks = []
        for segments in batches:
            segment_ids = [s["id"] for s in segments]
            prompt = build_opencode_enrichment_prompt(segments)
            logger.info("claimed %d opencode segments: %s", len(segment_ids), segment_ids)
            _add(_events_claimed, len(segment_ids), source="opencode")

            tracer = _get_tracer()
            span = (
                tracer.start_as_current_span(
                    "enrichment.opencode",
                    attributes={
                        "hippo.event_count": len(segment_ids),
                        "hippo.model": self.enrichment_model,
                    },
                )
                if tracer
                else nullcontext()
            )
            batch_start_ms = int(time.time() * 1000)
            with span:
                try:
                    result = await self._call_llm_with_retries(
                        OPENCODE_ENRICHMENT_PROMPT, prompt, "opencode"
                    )
                    conn = self._get_conn()
                    try:
                        node_id = write_opencode_knowledge_node(
                            conn,
                            result,
                            segment_ids,
                            self.enrichment_model,
                            content_hashes=[s.get("content_hash") for s in segments],
                        )
                    finally:
                        conn.close()
                    _add(_nodes_created, source="opencode")
                    self._record_success()
                    logger.info(
                        "enriched %d opencode segments -> node %d",
                        len(segment_ids),
                        node_id,
                    )

                    if self.embedding_model:
                        node_dict = {
                            "id": node_id,
                            "session_id": 0,
                            "captured_at": int(time.time() * 1000),
                            "commands_raw": "",
                            "cwd": segments[0].get("cwd", ""),
                            "git_branch": "",
                            "git_repo": "",
                            "outcome": result.outcome,
                            "tags": result.tags,
                            "key_decisions": result.key_decisions,
                            "problems_encountered": result.problems_encountered,
                            "entities": result.entities
                            if isinstance(result.entities, dict)
                            else {},
                            "embed_text": result.embed_text,
                            "summary": result.summary,
                            "enrichment_model": self.enrichment_model,
                        }
                        embed_tasks.append(
                            asyncio.create_task(self._embed_node(node_id, node_dict, "opencode"))
                        )
                except Exception as e:
                    _add(_enrichment_failures, source="opencode")
                    err_msg = self._record_error(e)
                    self._log_enrichment_failure(
                        "agentic_enrichment_queue",
                        "opencode.chat",
                        e,
                        claim_count=len(segment_ids),
                        claim_age_ms=int(time.time() * 1000) - batch_start_ms,
                    )
                    retry_conn = self._get_conn()
                    try:
                        mark_opencode_queue_failed(retry_conn, segment_ids, err_msg)
                    finally:
                        retry_conn.close()

        if embed_tasks:
            await asyncio.gather(*embed_tasks, return_exceptions=True)

    async def _embed_node(self, node_id, node_dict, source_label):
        """Embed a knowledge node into the vector store.

        Exceptions are caught so a single failure does not cancel sibling
        embed tasks running under ``asyncio.gather(..., return_exceptions=True)``.
        Failures are surfaced via ``logger.error`` and (when OTel telemetry is
        configured) the ``_embed_failures`` counter incremented by
        ``embed_knowledge_node`` before reraising.
        """
        try:
            await embed_knowledge_node(
                self.client,
                self._vector_table,
                node_dict,
                embed_model=self.embedding_model,
            )
            logger.info("embedded %s node %d into vector store", source_label, node_id)
        except Exception as e:
            logger.error(
                "%s embedding failed for node %d: %s",
                source_label,
                node_id,
                e,
                exc_info=True,
            )

    async def _reaper_loop(self):
        """Independent reaper task — fires on its own timer regardless of gather state.

        Decoupled from _enrichment_loop so stale locks are released even while
        asyncio.gather() is blocked inside _call_llm_with_retries.
        """
        while True:
            await asyncio.sleep(self.poll_interval_secs)
            try:
                conn = self._get_conn()
                try:
                    reap_stale_locks(conn, lock_timeout_ms=self.lock_timeout_ms)
                finally:
                    conn.close()
            except Exception as e:
                logger.warning("reaper loop error: %s", e, exc_info=True)

    async def _embed_reaper_tick(self):
        """One reaper sweep: re-embed knowledge_nodes that have no vector row.

        Source-agnostic — finds orphans by anti-join, not queue membership, so
        any source that fails to embed is healed regardless of which one.
        """
        if self._paused:
            # Quiescent during a /control/pause window (hippo-bench isolation);
            # firing embeds would issue inference calls and corrupt the run.
            return
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - self.embed_orphan_stale_secs * 1000
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, embed_text FROM knowledge_nodes "
                "WHERE created_at < ? "
                "AND id NOT IN (SELECT rowid FROM knowledge_vectors_rowids) "
                "ORDER BY created_at LIMIT ?",
                (cutoff, self.embed_reaper_batch_size),
            ).fetchall()
        except sqlite3.OperationalError as e:
            # Tolerate only the missing knowledge_vectors_rowids shadow table
            # (fresh install, nothing embedded yet). Any other operational
            # error — locked DB, I/O failure, bad SQL — must surface via the
            # loop's logging rather than masquerade as a healthy idle reaper.
            if "no such table" not in str(e):
                raise
            return
        finally:
            conn.close()
        if not rows:
            return
        for node_id, embed_text in rows:
            try:
                await self._embed_node(
                    node_id,
                    {"id": node_id, "embed_text": embed_text, "commands_raw": ""},
                    "reaper",
                )
            except Exception:
                # One bad node must not abandon the rest of the batch; it stays
                # an orphan and is retried next tick.
                logger.warning("embed reaper: re-embed failed for node %d", node_id, exc_info=True)
        logger.info("embed reaper: swept %d orphaned node(s)", len(rows))

    async def _embed_reaper_loop(self):
        """Independent loop that periodically runs _embed_reaper_tick."""
        while True:
            await asyncio.sleep(self.embed_reaper_interval_secs)
            try:
                await self._embed_reaper_tick()
            except Exception as e:
                logger.warning("embed reaper loop error: %s", e, exc_info=True)

    def start_enrichment(self):
        self._enrichment_task = asyncio.create_task(self._enrichment_loop())
        self._reaper_task = asyncio.create_task(self._reaper_loop())
        self._embed_reaper_task = asyncio.create_task(self._embed_reaper_loop())

    async def stop_enrichment(self):
        tasks = [
            t
            for t in (self._enrichment_task, self._reaper_task, self._embed_reaper_task)
            if t is not None
        ]
        if not tasks:
            return
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError):
                await t
        self._enrichment_task = None
        self._reaper_task = None
        self._embed_reaper_task = None

    def get_routes(self) -> list[Route]:
        return [
            Route("/health", self.health, methods=["GET"]),
            Route("/sessions", self.list_sessions, methods=["GET"]),
            Route("/events", self.list_events, methods=["GET"]),
            Route("/knowledge", self.list_knowledge, methods=["GET"]),
            Route("/knowledge/{id:int}", self.get_knowledge, methods=["GET"]),
            Route("/query", self.query, methods=["POST"]),
            Route("/ask", self.ask, methods=["POST"]),
            Route("/control/pause", self.control_pause, methods=["POST"]),
            Route("/control/resume", self.control_resume, methods=["POST"]),
            Route("/vault/export", self.vault_export, methods=["POST"]),
        ]


def create_app(
    db_path: str = "",
    data_dir: str = "",
    inference_base_url: str = "http://127.0.0.1:42069/v1",
    inference_timeout_secs: float = 300.0,
    enrichment_model: str = "",
    embedding_model: str = "",
    query_model: str = "",
    poll_interval_secs: int = 5,
    enrichment_batch_size: int = 30,
    session_stale_secs: int = 120,
    max_claim_batch: int = DEFAULT_MAX_CLAIM_BATCH,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    long_dwell_bypass_ms: int = 120_000,
    embed_reaper_interval_secs: int = 300,
    embed_reaper_batch_size: int = 50,
    embed_orphan_stale_secs: int = 900,
) -> Starlette:
    server = BrainServer(
        db_path=db_path,
        data_dir=data_dir,
        inference_base_url=inference_base_url,
        inference_timeout_secs=inference_timeout_secs,
        enrichment_model=enrichment_model,
        embedding_model=embedding_model,
        query_model=query_model,
        poll_interval_secs=poll_interval_secs,
        enrichment_batch_size=enrichment_batch_size,
        session_stale_secs=session_stale_secs,
        max_claim_batch=max_claim_batch,
        lock_timeout_ms=lock_timeout_ms,
        long_dwell_bypass_ms=long_dwell_bypass_ms,
        embed_reaper_interval_secs=embed_reaper_interval_secs,
        embed_reaper_batch_size=embed_reaper_batch_size,
        embed_orphan_stale_secs=embed_orphan_stale_secs,
    )

    if _meter:
        _resolved_db_path = server.db_path

        def _observe_queue_depths(callback_options):
            try:
                conn = sqlite3.connect(f"file:{_resolved_db_path}?mode=ro", uri=True)
                try:
                    for source, status, count in _collect_queue_depths(conn):
                        yield otel_metrics.Observation(count, {"source": source, "status": status})
                finally:
                    conn.close()
            except Exception:
                pass

        import opentelemetry.metrics as otel_metrics

        _meter.create_observable_gauge(
            "hippo.brain.enrichment.queue_depth",
            callbacks=[_observe_queue_depths],
            description="Enrichment queue sizes",
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        server.start_enrichment()
        try:
            yield
        finally:
            await server.stop_enrichment()

    app = Starlette(
        routes=server.get_routes(),
        lifespan=lifespan,
    )
    return app
