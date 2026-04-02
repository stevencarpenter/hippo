import asyncio
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from hippo_brain.client import LMStudioClient
from hippo_brain.version import get_version
from hippo_brain.embeddings import (
    embed_knowledge_node,
    get_or_create_table,
    open_vector_db,
    search_similar,
)
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
    mark_browser_queue_failed,
    write_browser_knowledge_node,
)
from hippo_brain.claude_sessions import (
    CLAUDE_SYSTEM_PROMPT,
    claim_pending_claude_segments,
    mark_claude_queue_failed,
    write_claude_knowledge_node,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("hippo_brain")


def _get_tracer():
    """Get OTel tracer if available, else return None."""
    try:
        if os.environ.get("HIPPO_OTEL_ENABLED", "").strip() != "1":
            return None
        from opentelemetry import trace

        return trace.get_tracer("hippo-brain")
    except ImportError:
        return None


class BrainServer:
    def __init__(
        self,
        db_path: str = "",
        data_dir: str = "",
        lmstudio_base_url: str = "http://localhost:1234/v1",
        enrichment_model: str = "",
        embedding_model: str = "",
        poll_interval_secs: int = 5,
        enrichment_batch_size: int = 30,
        session_stale_secs: int = 120,
    ):
        if not db_path:
            db_path = str(Path.home() / ".local" / "share" / "hippo" / "hippo.db")
        if not data_dir:
            data_dir = str(Path.home() / ".local" / "share" / "hippo")
        self.db_path = db_path
        self.data_dir = data_dir
        self.client = LMStudioClient(base_url=lmstudio_base_url)
        self.enrichment_model = enrichment_model
        self.embedding_model = embedding_model
        self.poll_interval_secs = poll_interval_secs
        self.enrichment_batch_size = enrichment_batch_size
        self.session_stale_secs = session_stale_secs
        self.enrichment_running = False
        self._enrichment_task = None
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
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        EXPECTED_VERSION = 4
        if version not in (EXPECTED_VERSION, 3):
            conn.close()
            raise RuntimeError(
                f"DB schema version mismatch: expected {EXPECTED_VERSION}, found {version}. "
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
                    "SELECT COUNT(*) FROM claude_enrichment_queue WHERE status = 'pending'"
                ).fetchone()[0]
                claude_queue_failed = conn.execute(
                    "SELECT COUNT(*) FROM claude_enrichment_queue WHERE status = 'failed'"
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
            finally:
                conn.close()
        except Exception:
            db_reachable = False

        return JSONResponse(
            {
                "status": "ok" if db_reachable else "degraded",
                "version": get_version(),
                "lmstudio_reachable": reachable,
                "enrichment_running": self.enrichment_running,
                "db_reachable": db_reachable,
                "queue_depth": queue_depth,
                "queue_failed": queue_failed,
                "claude_queue_depth": claude_queue_depth,
                "claude_queue_failed": claude_queue_failed,
                "browser_queue_depth": browser_queue_depth,
                "browser_queue_failed": browser_queue_failed,
                "last_success_at_ms": self.last_success_at_ms,
                "last_error": self.last_error,
                "last_error_at_ms": self.last_error_at_ms,
            }
        )

    def _query_lexical(self, text: str) -> dict:
        """Lexical substring search over events and knowledge nodes."""
        conn = self._get_conn()
        try:
            pattern = f"%{text}%"
            cursor = conn.execute(
                """SELECT id, command, cwd, timestamp
                   FROM events
                   WHERE command LIKE ?
                   ORDER BY timestamp DESC LIMIT 10""",
                (pattern,),
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
                   ORDER BY created_at DESC LIMIT 10""",
                (pattern, pattern),
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

        if mode == "lexical":
            try:
                return JSONResponse(self._query_lexical(text))
            except Exception as e:
                logger.error("query error: %s", e)
                return JSONResponse({"error": str(e)}, status_code=500)

        # Semantic mode — fall back to lexical if unavailable
        if not self.embedding_model or self._vector_table is None:
            try:
                result = self._query_lexical(text)
                result["warning"] = "semantic search unavailable, fell back to lexical"
                return JSONResponse(result)
            except Exception as e:
                logger.error("query error: %s", e)
                return JSONResponse({"error": str(e)}, status_code=500)

        try:
            vecs = await self.client.embed([text], model=self.embedding_model)
            hits = search_similar(self._vector_table, vecs[0], limit=10)
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
                result = self._query_lexical(text)
                result["warning"] = f"semantic search failed: {e}"
                return JSONResponse(result)
            except Exception as e2:
                logger.error("query error: %s", e2)
                return JSONResponse({"error": str(e2)}, status_code=500)

    async def _enrichment_loop(self):
        """Background enrichment polling loop."""
        self.enrichment_running = True
        worker_id = "brain-enrichment"
        try:
            while True:
                try:
                    await asyncio.sleep(self.poll_interval_secs)
                    conn = self._get_conn()
                    chunks = claim_pending_events_by_session(
                        conn,
                        self.enrichment_batch_size,
                        worker_id,
                        self.session_stale_secs,
                    )
                    if not chunks:
                        conn.close()
                        continue

                    for events in chunks:
                        event_ids = [e["id"] for e in events]
                        logger.info("claimed %d events: %s", len(event_ids), event_ids)

                        browser_context = ""
                        try:
                            from hippo_brain.browser_enrichment import (
                                get_correlated_browser_events,
                                format_browser_context_for_shell_prompt,
                            )

                            if events:
                                start_ts = min(e["timestamp"] for e in events)
                                end_ts = max(e["timestamp"] for e in events)
                                correlated = get_correlated_browser_events(conn, start_ts, end_ts)
                                browser_context = format_browser_context_for_shell_prompt(
                                    correlated
                                )
                        except Exception as e:
                            logger.debug("browser correlation skipped: %s", e)

                        prompt = build_enrichment_prompt(events, browser_context=browser_context)
                        logger.info("calling LM Studio (prompt len: %d chars)", len(prompt))

                        tracer = _get_tracer()
                        _shell_span = (
                            tracer.start_as_current_span(
                                "enrichment.shell",
                                attributes={
                                    "hippo.event_count": len(event_ids),
                                    "hippo.model": self.enrichment_model,
                                },
                            )
                            if tracer
                            else suppress()
                        )
                        with _shell_span:
                            try:
                                result = None
                                last_err = None
                                for attempt in range(3):
                                    try:
                                        messages = [
                                            {"role": "system", "content": SYSTEM_PROMPT},
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
                                        break
                                    except Exception as e:
                                        last_err = e
                                        logger.warning(
                                            "enrichment parse attempt %d failed: %s",
                                            attempt + 1,
                                            e,
                                        )
                                if result is None:
                                    raise last_err
                                node_id = write_knowledge_node(
                                    conn, result, event_ids, self.enrichment_model
                                )
                                self.last_success_at_ms = int(time.time() * 1000)
                                self.last_error = None
                                self.last_error_at_ms = None
                                logger.info(
                                    "enriched %d events -> node %d", len(event_ids), node_id
                                )

                                if self.embedding_model:
                                    try:
                                        node_dict = {
                                            "id": node_id,
                                            "session_id": events[0].get("session_id", 0),
                                            "captured_at": int(time.time() * 1000),
                                            "commands_raw": " ; ".join(
                                                e.get("command", "") for e in events
                                            ),
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
                                            "enrichment_version": 1,
                                        }
                                        await embed_knowledge_node(
                                            self.client,
                                            self._vector_table,
                                            node_dict,
                                            embed_model=self.embedding_model,
                                        )
                                        logger.info(
                                            "embedded node %d into vector store", node_id
                                        )
                                    except Exception as e:
                                        logger.warning("embedding failed (non-fatal): %s", e)
                            except Exception as e:
                                self.last_error = str(e)
                                self.last_error_at_ms = int(time.time() * 1000)
                                logger.error("enrichment failed: %s", e)
                                retry_conn = self._get_conn()
                                try:
                                    mark_queue_failed(retry_conn, event_ids, str(e))
                                finally:
                                    retry_conn.close()

                    # Process Claude session segments
                    try:
                        claude_batches = claim_pending_claude_segments(conn, worker_id)
                        for segments in claude_batches:
                            segment_ids = [s["id"] for s in segments]
                            prompt = "\n---\n\n".join(s["summary_text"] for s in segments)
                            logger.info(
                                "claimed %d claude segments: %s",
                                len(segment_ids),
                                segment_ids,
                            )

                            tracer = _get_tracer()
                            _claude_span = (
                                tracer.start_as_current_span(
                                    "enrichment.claude",
                                    attributes={
                                        "hippo.event_count": len(segment_ids),
                                        "hippo.model": self.enrichment_model,
                                    },
                                )
                                if tracer
                                else suppress()
                            )
                            with _claude_span:
                                try:
                                    result = None
                                    last_err = None
                                    for attempt in range(3):
                                        try:
                                            messages = [
                                                {
                                                    "role": "system",
                                                    "content": CLAUDE_SYSTEM_PROMPT,
                                                },
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
                                            result = parse_enrichment_response(raw)
                                            break
                                        except Exception as e:
                                            last_err = e
                                            logger.warning(
                                                "claude enrichment parse attempt %d failed: %s",
                                                attempt + 1,
                                                e,
                                            )
                                    if result is None:
                                        raise last_err
                                    node_id = write_claude_knowledge_node(
                                        conn,
                                        result,
                                        segment_ids,
                                        self.enrichment_model,
                                    )
                                    self.last_success_at_ms = int(time.time() * 1000)
                                    self.last_error = None
                                    self.last_error_at_ms = None
                                    logger.info(
                                        "enriched %d claude segments -> node %d",
                                        len(segment_ids),
                                        node_id,
                                    )

                                    if self.embedding_model:
                                        try:
                                            import json as _json

                                            all_tools = []
                                            for s in segments:
                                                try:
                                                    tools = _json.loads(
                                                        s.get("tool_calls_json", "[]")
                                                    )
                                                    all_tools.extend(
                                                        f"{t['name']}: {t['summary']}"
                                                        for t in tools
                                                    )
                                                except (
                                                    _json.JSONDecodeError,
                                                    KeyError,
                                                ):
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
                                            await embed_knowledge_node(
                                                self.client,
                                                self._vector_table,
                                                node_dict,
                                                embed_model=self.embedding_model,
                                            )
                                            logger.info(
                                                "embedded claude node %d into vector store",
                                                node_id,
                                            )
                                        except Exception as e:
                                            logger.warning(
                                                "claude embedding failed (non-fatal): %s",
                                                e,
                                            )
                                except Exception as e:
                                    self.last_error = str(e)
                                    self.last_error_at_ms = int(time.time() * 1000)
                                    logger.error("claude enrichment failed: %s", e)
                                    retry_conn = self._get_conn()
                                    try:
                                        mark_claude_queue_failed(retry_conn, segment_ids, str(e))
                                    finally:
                                        retry_conn.close()
                    except Exception as e:
                        logger.debug("no claude segments to process: %s", e)

                    # Process browser events
                    try:
                        browser_batches = claim_pending_browser_events(
                            conn, worker_id, stale_secs=60
                        )
                        for events in browser_batches:
                            event_ids = [e["id"] for e in events]
                            logger.info(
                                "claimed %d browser events: %s",
                                len(event_ids),
                                event_ids,
                            )
                            prompt = build_browser_enrichment_prompt(events)

                            tracer = _get_tracer()
                            _browser_span = (
                                tracer.start_as_current_span(
                                    "enrichment.browser",
                                    attributes={
                                        "hippo.event_count": len(event_ids),
                                        "hippo.model": self.enrichment_model,
                                    },
                                )
                                if tracer
                                else suppress()
                            )
                            with _browser_span:
                                try:
                                    result = None
                                    last_err = None
                                    for attempt in range(3):
                                        try:
                                            messages = [
                                                {
                                                    "role": "system",
                                                    "content": BROWSER_SYSTEM_PROMPT,
                                                },
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
                                            result = parse_enrichment_response(raw)
                                            break
                                        except Exception as e:
                                            last_err = e
                                            logger.warning(
                                                "browser enrichment attempt %d failed: %s",
                                                attempt + 1,
                                                e,
                                            )
                                    if result is None:
                                        raise last_err
                                    node_id = write_browser_knowledge_node(
                                        conn, result, event_ids, self.enrichment_model
                                    )
                                    self.last_success_at_ms = int(time.time() * 1000)
                                    logger.info(
                                        "enriched %d browser events -> node %d",
                                        len(event_ids),
                                        node_id,
                                    )

                                    if self.embedding_model:
                                        try:
                                            node_dict = {
                                                "id": node_id,
                                                "session_id": 0,
                                                "captured_at": int(time.time() * 1000),
                                                "commands_raw": " ; ".join(
                                                    e.get("url", "") for e in events
                                                ),
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
                                                "enrichment_version": 1,
                                            }
                                            await embed_knowledge_node(
                                                self.client,
                                                self._vector_table,
                                                node_dict,
                                                embed_model=self.embedding_model,
                                            )
                                            logger.info(
                                                "embedded browser node %d into vector store",
                                                node_id,
                                            )
                                        except Exception as e:
                                            logger.warning(
                                                "browser embedding failed (non-fatal): %s",
                                                e,
                                            )
                                except Exception as e:
                                    self.last_error = str(e)
                                    self.last_error_at_ms = int(time.time() * 1000)
                                    logger.error("browser enrichment failed: %s", e)
                                    retry_conn = self._get_conn()
                                    try:
                                        mark_browser_queue_failed(retry_conn, event_ids, str(e))
                                    finally:
                                        retry_conn.close()
                    except Exception as e:
                        logger.warning("browser enrichment polling error: %s", e)

                    conn.close()
                except Exception as e:
                    self.last_error = str(e)
                    self.last_error_at_ms = int(time.time() * 1000)
                    logger.error("enrichment loop error: %s", e)
                    await asyncio.sleep(self.poll_interval_secs)
        finally:
            self.enrichment_running = False

    def start_enrichment(self):
        self._enrichment_task = asyncio.create_task(self._enrichment_loop())

    async def stop_enrichment(self):
        if self._enrichment_task is None:
            return

        self._enrichment_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._enrichment_task
        self._enrichment_task = None

    def get_routes(self) -> list[Route]:
        return [
            Route("/health", self.health, methods=["GET"]),
            Route("/query", self.query, methods=["POST"]),
        ]


def create_app(
    db_path: str = "",
    data_dir: str = "",
    lmstudio_base_url: str = "http://localhost:1234/v1",
    enrichment_model: str = "",
    embedding_model: str = "",
    poll_interval_secs: int = 5,
    enrichment_batch_size: int = 30,
    session_stale_secs: int = 120,
) -> Starlette:
    server = BrainServer(
        db_path=db_path,
        data_dir=data_dir,
        lmstudio_base_url=lmstudio_base_url,
        enrichment_model=enrichment_model,
        embedding_model=embedding_model,
        poll_interval_secs=poll_interval_secs,
        enrichment_batch_size=enrichment_batch_size,
        session_stale_secs=session_stale_secs,
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
