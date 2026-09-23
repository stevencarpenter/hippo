"""Migration must honor an explicit database filename."""

import importlib.util
from pathlib import Path

from hippo_brain.vector_store import open_conn, insert_vectors


async def test_explicit_db_does_not_modify_sibling(tmp_path, monkeypatch):
    script = Path(__file__).parents[1] / "scripts" / "migrate-vectors.py"
    spec = importlib.util.spec_from_file_location("migrate_vectors", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "clone.db"
    sibling = tmp_path / "hippo.db"
    schema = (Path(__file__).parents[2] / "crates/hippo-core/src/schema.sql").read_text()
    for path in (target, sibling):
        conn = open_conn(path)
        conn.executescript(schema)
        with conn:
            conn.execute(
                "INSERT INTO knowledge_nodes(uuid, content, embed_text) VALUES (?, '{}', ?)",
                (path.name, path.name),
            )
        conn.close()

    async def embed(client, conn, node, **kwargs):
        with conn:
            insert_vectors(conn, node["id"], [0.1] * 768, [0.1] * 768)

    monkeypatch.setattr(module, "embed_knowledge_node", embed)
    assert await module.run(target, tmp_path, tmp_path / "missing-config") == 0
    for path, expected in ((target, 1), (sibling, 0)):
        conn = open_conn(path)
        try:
            assert conn.execute("SELECT count(*) FROM knowledge_vectors").fetchone()[0] == expected
        finally:
            conn.close()
