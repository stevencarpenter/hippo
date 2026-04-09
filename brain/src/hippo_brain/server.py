import asyncio
import logging
import sqlite3
import time
from contextlib import asynccontextmanager, nullcontext, suppress
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
from hippo_brain.telemetry import get_tracer as _get_tracer
from hippo_brain.telemetry import get_meter

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
        "hippo.brain.enrichment.loop_duration_ms",
        description="Enrichment cycle wall clock",
        unit="ms",
    )
    if _meter
    else None
)


def _add(counter, value=1, **attrs):
    if counter:
        counter.add(value, attrs)


def _hist(histogram, value, **attrs):
    if histogram:
        histogram.record(value, attrs)


logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("hippo_brain")


class BrainServer:
    def __init__(
        self,
        db_path: str = "",
        data_dir: str = "",
        lmstudio_base_url: str = "http://localhost:1234/v1",
        enrichment_model: str = "",
        embedding_model: str = "",
        query_model: str = "",
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
        self._preferred_model = enrichment_model
        self.enrichment_model = enrichment_model
        self.embedding_model = embedding_model
        self.query_model = query_model
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
                "enrichment_model": self.enrichment_model,
                "enrichment_model_preferred": self._preferred_model,
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

    async def ask(self, request: Request) -> JSONResponse:
        """RAG endpoint: retrieve relevant knowledge and synthesize an answer."""
        body = await request.json()
        question = body.get("question", "")
        if not question:
            return JSONResponse({"error": "question is required"}, status_code=400)

        limit = body.get("limit", 10)

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

        result = await rag_ask(
            question=question,
            lm_client=self.client,
            vector_table=self._vector_table,
            query_model=model,
            embedding_model=self.embedding_model,
            limit=limit,
        )

        status = 200 if "answer" in result else 502
        return JSONResponse(result, status_code=status)

    async def _resolve_model(self) -> bool:
        """Pick the best available enrichment model. Returns False if none found."""
        try:
            loaded = await self.client.list_models()
        except Exception:
            return False

        # Filter out embedding models
        embedding_hints = ("embed", "nomic", "modernbert")
        chat_models = [m for m in loaded if not any(h in m.lower() for h in embedding_hints)]

        if not chat_models:
            return False

        # Preferred model available — use it
        if self._preferred_model and self._preferred_model in chat_models:
            if self.enrichment_model != self._preferred_model:
                logger.info("enrichment model restored: %s", self._preferred_model)
                self.enrichment_model = self._preferred_model
            return True

        # Preferred model not loaded — fall back
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
        at the LM Studio level, but DB writes and embeddings from one batch
        overlap with the next LLM call.
        """
        self.enrichment_running = True
        worker_id = "brain-enrichment"
        try:
            while True:
                try:
                    await asyncio.sleep(self.poll_interval_secs)

                    if not await self._resolve_model():
                        continue

                    conn = self._get_conn()
                    try:
                        # Claim all work upfront (sequential — avoids SQLite write contention)
                        shell_chunks = claim_pending_events_by_session(
                            conn,
                            self.enrichment_batch_size,
                            worker_id,
                            self.session_stale_secs,
                        )
                        try:
                            claude_batches = claim_pending_claude_segments(conn, worker_id)
                        except Exception as e:
                            logger.debug("no claude segments to process: %s", e)
                            claude_batches = []
                        try:
                            browser_batches = claim_pending_browser_events(
                                conn, worker_id, stale_secs=60
                            )
                        except Exception as e:
                            logger.warning("browser claim error: %s", e, exc_info=True)
                            browser_batches = []

                        if not shell_chunks and not claude_batches and not browser_batches:
                            continue

                        # Process all sources concurrently — each method uses its own
                        # DB connection for writes so they don't block each other.
                        # Shell receives the claim conn for read-only browser correlation.
                        t0 = time.monotonic()
                        await asyncio.gather(
                            self._enrich_shell_batches(shell_chunks, conn),
                            self._enrich_claude_batches(claude_batches),
                            self._enrich_browser_batches(browser_batches),
                        )
                        _hist(_loop_duration, (time.monotonic() - t0) * 1000)
                    finally:
                        conn.close()
                except Exception as e:
                    self.last_error = str(e) or type(e).__name__
                    self.last_error_at_ms = int(time.time() * 1000)
                    logger.error("enrichment loop error: %s", e, exc_info=True)
                    await asyncio.sleep(self.poll_interval_secs)
        finally:
            self.enrichment_running = False

    async def _call_llm_with_retries(self, system_prompt, prompt, source_label):
        """Call LM Studio with up to 3 retries on parse failure."""
        last_err = None
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
            logger.info("calling LM Studio (prompt len: %d chars)", len(prompt))

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
                    logger.error("enrichment failed: %s", e, exc_info=True)
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
            prompt = "\n---\n\n".join(s["summary_text"] for s in segments)
            logger.info("claimed %d claude segments: %s", len(segment_ids), segment_ids)
            _add(_events_claimed, len(segment_ids), source="claude")

            tracer = _get_tracer()
            span = (
                tracer.start_as_current_span(
                    "enrichment.claude",
                    attributes={
                        "hippo.event_count": len(segment_ids),
                        "hippo.model": self.enrichment_model,
                    },
                )
                if tracer
                else nullcontext()
            )
            with span:
                try:
                    result = await self._call_llm_with_retries(
                        CLAUDE_SYSTEM_PROMPT, prompt, "claude"
                    )
                    conn = self._get_conn()
                    try:
                        node_id = write_claude_knowledge_node(
                            conn, result, segment_ids, self.enrichment_model
                        )
                    finally:
                        conn.close()
                    _add(_nodes_created, source="claude")
                    self._record_success()
                    logger.info(
                        "enriched %d claude segments -> node %d",
                        len(segment_ids),
                        node_id,
                    )

                    if self.embedding_model:
                        import json as _json

                        all_tools = []
                        for s in segments:
                            try:
                                tools = _json.loads(s.get("tool_calls_json", "[]"))
                                all_tools.extend(f"{t['name']}: {t['summary']}" for t in tools)
                            except _json.JSONDecodeError, KeyError:
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
                            asyncio.create_task(self._embed_node(node_id, node_dict, "claude"))
                        )
                except Exception as e:
                    _add(_enrichment_failures, source="claude")
                    err_msg = self._record_error(e)
                    logger.error("claude enrichment failed: %s", e, exc_info=True)
                    retry_conn = self._get_conn()
                    try:
                        mark_claude_queue_failed(retry_conn, segment_ids, err_msg)
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
                    logger.info(
                        "enriched %d browser events -> node %d",
                        len(event_ids),
                        node_id,
                    )

                    if self.embedding_model:
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
                    logger.error("browser enrichment failed: %s", e, exc_info=True)
                    retry_conn = self._get_conn()
                    try:
                        mark_browser_queue_failed(retry_conn, event_ids, err_msg)
                    finally:
                        retry_conn.close()

        if embed_tasks:
            await asyncio.gather(*embed_tasks, return_exceptions=True)

    async def _embed_node(self, node_id, node_dict, source_label):
        """Embed a knowledge node into the vector store (fire-and-forget safe)."""
        try:
            await embed_knowledge_node(
                self.client,
                self._vector_table,
                node_dict,
                embed_model=self.embedding_model,
            )
            logger.info("embedded %s node %d into vector store", source_label, node_id)
        except Exception as e:
            logger.warning("%s embedding failed (non-fatal): %s", source_label, e, exc_info=True)

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
            Route("/ask", self.ask, methods=["POST"]),
        ]


def create_app(
    db_path: str = "",
    data_dir: str = "",
    lmstudio_base_url: str = "http://localhost:1234/v1",
    enrichment_model: str = "",
    embedding_model: str = "",
    query_model: str = "",
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
        query_model=query_model,
        poll_interval_secs=poll_interval_secs,
        enrichment_batch_size=enrichment_batch_size,
        session_stale_secs=session_stale_secs,
    )

    if _meter:
        _QUEUE_TABLES = {
            "shell": "enrichment_queue",
            "claude": "claude_enrichment_queue",
            "browser": "browser_enrichment_queue",
        }

        def _observe_queue_depths(callback_options):
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    for source, table in _QUEUE_TABLES.items():
                        # table is from a hardcoded whitelist — no user input involved.
                        # Use a pre-built mapping to keep the query fixed-form.
                        sql = {
                            "enrichment_queue": "SELECT COUNT(*) FROM enrichment_queue WHERE status = ?",
                            "claude_enrichment_queue": "SELECT COUNT(*) FROM claude_enrichment_queue WHERE status = ?",
                            "browser_enrichment_queue": "SELECT COUNT(*) FROM browser_enrichment_queue WHERE status = ?",
                        }[table]
                        for status in ("pending", "failed"):
                            try:
                                count = conn.execute(sql, (status,)).fetchone()[0]
                                yield otel_metrics.Observation(
                                    count, {"source": source, "status": status}
                                )
                            except Exception:
                                pass
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
