from pathlib import Path

import lancedb
import pyarrow as pa

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
        pa.field("vec_knowledge", pa.list_(pa.float32(), 2560)),
        pa.field("vec_command", pa.list_(pa.float32(), 384)),
        pa.field("enrichment_model", pa.string()),
        pa.field("enrichment_version", pa.int32()),
    ]
)


def open_vector_db(data_dir: str | Path) -> lancedb.DBConnection:
    """Open or create a LanceDB at data_dir/vectors/."""
    vectors_path = Path(data_dir) / "vectors"
    vectors_path.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(vectors_path))


def get_or_create_table(db: lancedb.DBConnection) -> lancedb.table.Table:
    """Create the knowledge table if it doesn't exist."""
    existing = db.list_tables()
    if "knowledge" in existing:
        return db.open_table("knowledge")

    empty = pa.table(
        {field.name: pa.array([], type=field.type) for field in KNOWLEDGE_SCHEMA},
        schema=KNOWLEDGE_SCHEMA,
    )
    return db.create_table("knowledge", data=empty, schema=KNOWLEDGE_SCHEMA)


def _pad_or_truncate(vec: list[float], target_dim: int) -> list[float]:
    """Pad with zeros or truncate to target dimensions."""
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
    """Get embeddings from client, pad/truncate, add row to table."""
    embed_text = node_dict.get("embed_text", "")
    commands_raw = node_dict.get("commands_raw", "")

    # Get knowledge embedding (2560d)
    knowledge_vecs = await client.embed([embed_text], model=embed_model)
    vec_knowledge = _pad_or_truncate(knowledge_vecs[0], 2560)

    # Get command embedding (384d)
    cmd_model = command_model or embed_model
    command_vecs = await client.embed([commands_raw or embed_text], model=cmd_model)
    vec_command = _pad_or_truncate(command_vecs[0], 384)

    import json

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
        "vec_knowledge": vec_knowledge,
        "vec_command": vec_command,
        "enrichment_model": node_dict.get("enrichment_model", ""),
        "enrichment_version": node_dict.get("enrichment_version", 1),
    }

    table.add([row])


def search_similar(
    table: lancedb.table.Table,
    query_vec: list[float],
    column: str = "vec_knowledge",
    limit: int = 10,
) -> list[dict]:
    """Vector search on the given column."""
    results = table.search(query_vec, vector_column_name=column).limit(limit).to_list()
    return results
