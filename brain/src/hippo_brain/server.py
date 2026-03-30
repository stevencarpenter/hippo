import asyncio
import logging
import sqlite3
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from hippo_brain.client import LMStudioClient
from hippo_brain.embeddings import embed_knowledge_node, get_or_create_table, open_vector_db
from hippo_brain.enrichment import (
    SYSTEM_PROMPT,
    build_enrichment_prompt,
    claim_pending_events,
    mark_queue_failed,
    parse_enrichment_response,
    write_knowledge_node,
)

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
        poll_interval_secs: int = 5,
        enrichment_batch_size: int = 10,
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
        EXPECTED_VERSION = 2
        if version != EXPECTED_VERSION:
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
            finally:
                conn.close()
        except Exception:
            db_reachable = False

        return JSONResponse(
            {
                "status": "ok" if db_reachable else "degraded",
                "lmstudio_reachable": reachable,
                "enrichment_running": self.enrichment_running,
                "db_reachable": db_reachable,
                "queue_depth": queue_depth,
                "queue_failed": queue_failed,
                "last_success_at_ms": self.last_success_at_ms,
                "last_error": self.last_error,
                "last_error_at_ms": self.last_error_at_ms,
            }
        )

    # Current implementation: lexical substring search over events.command and
    # knowledge_nodes.content/embed_text. Semantic (vector) retrieval is available
    # via the embeddings module but is not yet wired into this endpoint.
    async def query(self, request: Request) -> JSONResponse:
        body = await request.json()
        text = body.get("text", "")
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)

        try:
            conn = self._get_conn()
            pattern = f"%{text}%"

            # Search events
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

            # Search knowledge nodes
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

            conn.close()
            return JSONResponse({"events": events, "nodes": nodes})
        except Exception as e:
            logger.error("query error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _enrichment_loop(self):
        """Background enrichment polling loop."""
        self.enrichment_running = True
        worker_id = "brain-enrichment"
        try:
            while True:
                try:
                    await asyncio.sleep(self.poll_interval_secs)
                    conn = self._get_conn()
                    events = claim_pending_events(conn, self.enrichment_batch_size, worker_id)
                    if not events:
                        conn.close()
                        continue

                    event_ids = [e["id"] for e in events]
                    logger.info("claimed %d events: %s", len(event_ids), event_ids)
                    prompt = build_enrichment_prompt(events)
                    logger.info("calling LM Studio (prompt len: %d chars)", len(prompt))

                    try:
                        raw = await self.client.chat(
                            messages=[
                                {"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": prompt},
                            ],
                            model=self.enrichment_model,
                        )
                        result = parse_enrichment_response(raw)
                        node_id = write_knowledge_node(
                            conn, result, event_ids, self.enrichment_model
                        )
                        self.last_success_at_ms = int(time.time() * 1000)
                        self.last_error = None
                        self.last_error_at_ms = None
                        logger.info("enriched %d events -> node %d", len(event_ids), node_id)

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
                                logger.info("embedded node %d into vector store", node_id)
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
    enrichment_batch_size: int = 10,
) -> Starlette:
    server = BrainServer(
        db_path=db_path,
        data_dir=data_dir,
        lmstudio_base_url=lmstudio_base_url,
        enrichment_model=enrichment_model,
        embedding_model=embedding_model,
        poll_interval_secs=poll_interval_secs,
        enrichment_batch_size=enrichment_batch_size,
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
