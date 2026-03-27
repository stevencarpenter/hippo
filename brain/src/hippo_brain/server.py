import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from hippo_brain.client import LMStudioClient
from hippo_brain.enrichment import (
    SYSTEM_PROMPT,
    build_enrichment_prompt,
    claim_pending_events,
    mark_queue_failed,
    parse_enrichment_response,
    write_knowledge_node,
)

logger = logging.getLogger("hippo_brain")


class BrainServer:
    def __init__(
        self,
        db_path: str = "",
        lmstudio_base_url: str = "http://localhost:1234/v1",
        enrichment_model: str = "",
        poll_interval_secs: int = 5,
        enrichment_batch_size: int = 10,
    ):
        if not db_path:
            db_path = str(
                Path.home() / ".local" / "share" / "hippo" / "hippo.db"
            )
        self.db_path = db_path
        self.client = LMStudioClient(base_url=lmstudio_base_url)
        self.enrichment_model = enrichment_model
        self.poll_interval_secs = poll_interval_secs
        self.enrichment_batch_size = enrichment_batch_size
        self.enrichment_running = False
        self._enrichment_task = None

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def health(self, request: Request) -> JSONResponse:
        reachable = await self.client.is_reachable()
        return JSONResponse({
            "status": "ok",
            "lmstudio_reachable": reachable,
            "enrichment_running": self.enrichment_running,
        })

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
                """SELECT id, command, cwd, timestamp FROM events
                   WHERE command LIKE ? ORDER BY timestamp DESC LIMIT 10""",
                (pattern,),
            )
            events = [
                {"event_id": r[0], "command": r[1], "cwd": r[2], "timestamp": r[3]}
                for r in cursor.fetchall()
            ]

            # Search knowledge nodes
            cursor = conn.execute(
                """SELECT id, uuid, content, embed_text FROM knowledge_nodes
                   WHERE content LIKE ? OR embed_text LIKE ?
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
        while True:
            try:
                await asyncio.sleep(self.poll_interval_secs)
                conn = self._get_conn()
                events = claim_pending_events(conn, self.enrichment_batch_size, worker_id)
                if not events:
                    conn.close()
                    continue

                event_ids = [e["id"] for e in events]
                prompt = build_enrichment_prompt(events)

                try:
                    raw = await self.client.chat(
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        model=self.enrichment_model,
                    )
                    result = parse_enrichment_response(raw)
                    write_knowledge_node(conn, result, event_ids, self.enrichment_model)
                    logger.info("enriched %d events -> node created", len(event_ids))
                except Exception as e:
                    logger.error("enrichment failed: %s", e)
                    mark_queue_failed(conn, event_ids, str(e))

                conn.close()
            except Exception as e:
                logger.error("enrichment loop error: %s", e)
                await asyncio.sleep(self.poll_interval_secs)

    def start_enrichment(self):
        self._enrichment_task = asyncio.create_task(self._enrichment_loop())

    def get_routes(self) -> list[Route]:
        return [
            Route("/health", self.health, methods=["GET"]),
            Route("/query", self.query, methods=["POST"]),
        ]


def create_app(
    db_path: str = "",
    lmstudio_base_url: str = "http://localhost:1234/v1",
    enrichment_model: str = "",
    poll_interval_secs: int = 5,
    enrichment_batch_size: int = 10,
) -> Starlette:
    server = BrainServer(
        db_path=db_path,
        lmstudio_base_url=lmstudio_base_url,
        enrichment_model=enrichment_model,
        poll_interval_secs=poll_interval_secs,
        enrichment_batch_size=enrichment_batch_size,
    )

    async def on_startup():
        server.start_enrichment()

    app = Starlette(
        routes=server.get_routes(),
        on_startup=[on_startup],
    )
    return app
