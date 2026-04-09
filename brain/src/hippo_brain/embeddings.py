import json
import time
from pathlib import Path

import lancedb
import pyarrow as pa

from hippo_brain.telemetry import get_meter

_meter = get_meter()
_embed_duration = _meter.create_histogram("hippo.brain.embedding.duration_ms", description="Time to embed a knowledge node", unit="ms") if _meter else None
_embed_failures = _meter.create_counter("hippo.brain.embedding.failures", description="Failed embedding attempts") if _meter else None

EMBED_DIM = 768

KNOWLEDGE_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("session_id", pa.int64()),
        pa.field("captured_at", pa.int64()),
        pa.field("commands_raw", pa.string()),
        pa.field("cwd", pa.string()),
        pa.field("git_branch", pa.string()),
        pa.field("git_repo", pa.string()),
        pa.field("outcome", pa.string()),
        pa.field("tags", pa.string()),
        pa.field("entities_json", pa.string()),
        pa.field("embed_text", pa.string()),
        pa.field("summary", pa.string()),
        pa.field("key_decisions", pa.string()),
        pa.field("problems_encountered", pa.string()),
        pa.field("vec_knowledge", pa.list_(pa.float32(), EMBED_DIM)),
        pa.field("vec_command", pa.list_(pa.float32(), EMBED_DIM)),
        pa.field("enrichment_model", pa.string()),
    ]
)


def open_vector_db(data_dir: str | Path) -> lancedb.DBConnection:
    vectors_path = Path(data_dir) / "vectors"
    vectors_path.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(vectors_path))


def get_or_create_table(db: lancedb.DBConnection) -> lancedb.table.Table:
    existing = db.list_tables()
    table_names = existing.tables if hasattr(existing, "tables") else existing
    if "knowledge" in table_names:
        return db.open_table("knowledge")

    empty = pa.table(
        {field.name: pa.array([], type=field.type) for field in KNOWLEDGE_SCHEMA},
        schema=KNOWLEDGE_SCHEMA,
    )
    return db.create_table("knowledge", data=empty, schema=KNOWLEDGE_SCHEMA)


def _pad_or_truncate(vec: list[float], target_dim: int) -> list[float]:
    if len(vec) >= target_dim:
        return vec[:target_dim]
    return vec + [0.0] * (target_dim - len(vec))


async def embed_knowledge_node(
    client,
    table: lancedb.table.Table,
    node_dict: dict,
    embed_model: str = "",
    command_model: str = "",
):
    t0 = time.monotonic()
    try:
        embed_text = node_dict.get("embed_text", "")
        commands_raw = node_dict.get("commands_raw", "")

        cmd_model = command_model or embed_model
        cmd_text = commands_raw or embed_text

        if cmd_model == embed_model:
            # Single batched call for both vectors
            vecs = await client.embed([embed_text, cmd_text], model=embed_model)
            vec_knowledge = _pad_or_truncate(vecs[0], EMBED_DIM)
            vec_command = _pad_or_truncate(vecs[1], EMBED_DIM)
        else:
            # Different models — two calls required
            knowledge_vecs = await client.embed([embed_text], model=embed_model)
            vec_knowledge = _pad_or_truncate(knowledge_vecs[0], EMBED_DIM)
            command_vecs = await client.embed([cmd_text], model=cmd_model)
            vec_command = _pad_or_truncate(command_vecs[0], EMBED_DIM)

        row = {
            "id": node_dict.get("id", 0),
            "session_id": node_dict.get("session_id", 0),
            "captured_at": node_dict.get("captured_at", 0),
            "commands_raw": commands_raw,
            "cwd": node_dict.get("cwd", ""),
            "git_branch": node_dict.get("git_branch", ""),
            "git_repo": node_dict.get("git_repo", ""),
            "outcome": node_dict.get("outcome", ""),
            "tags": json.dumps(node_dict.get("tags", [])),
            "entities_json": json.dumps(node_dict.get("entities", {})),
            "embed_text": embed_text,
            "summary": node_dict.get("summary", ""),
            "key_decisions": json.dumps(node_dict.get("key_decisions", [])),
            "problems_encountered": json.dumps(node_dict.get("problems_encountered", [])),
            "vec_knowledge": vec_knowledge,
            "vec_command": vec_command,
            "enrichment_model": node_dict.get("enrichment_model", ""),
        }

        table.add([row])
        if _embed_duration:
            _embed_duration.record((time.monotonic() - t0) * 1000)
    except Exception:
        if _embed_failures:
            _embed_failures.add(1)
        raise


def search_similar(
    table: lancedb.table.Table,
    query_vec: list[float],
    column: str = "vec_knowledge",
    limit: int = 10,
) -> list[dict]:
    results = table.search(query_vec, vector_column_name=column).limit(limit).to_list()
    return results
