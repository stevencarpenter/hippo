import json
import random
from pathlib import Path

SYSTEM_PROMPT = (
    "You are a personal developer assistant with deep knowledge of the user's projects, "
    "tools, and workflows. You have observed their shell activity and can provide contextual "
    "help, recall past commands and their outcomes, suggest solutions based on prior experience, "
    "and help with debugging, deployment, and development tasks."
)


def _write_jsonl(path: Path, examples: list[dict]):
    """Write a list of dicts as JSONL."""
    with open(path, "w") as f:
        for ex in examples:
            f.write(json.dumps(ex) + "\n")


def export_training_data(
    conn,
    output_dir: str | Path,
    since_ms: int | None = None,
    min_events: int = 1,
) -> dict:
    """Export knowledge nodes as JSONL conversation pairs for fine-tuning.

    Returns stats dict with total, train, valid, test counts.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Query knowledge nodes with success/partial outcome
    sql = """
          SELECT kn.id, kn.content, kn.embed_text, kn.outcome, kn.tags
          FROM knowledge_nodes kn
          WHERE kn.outcome IN ('success', 'partial') \
          """
    params = []
    if since_ms is not None:
        sql += " AND kn.created_at >= ?"
        params.append(since_ms)

    cursor = conn.execute(sql, params)
    nodes = cursor.fetchall()

    examples = []
    for node_id, content, embed_text, outcome, tags in nodes:
        # Get linked events
        event_cursor = conn.execute(
            """
            SELECT e.command, e.exit_code, e.duration_ms, e.cwd, e.git_branch
            FROM events e
                     JOIN knowledge_node_events kne ON kne.event_id = e.id
            WHERE kne.knowledge_node_id = ?
            """,
            (node_id,),
        )
        events = event_cursor.fetchall()

        if len(events) < min_events:
            continue

        # Build user message from events
        user_parts = []
        for cmd, exit_code, duration_ms, cwd, git_branch in events:
            parts = [f"$ {cmd}"]
            if exit_code is not None:
                parts.append(f"  exit: {exit_code}")
            if duration_ms:
                parts.append(f"  duration: {duration_ms}ms")
            if cwd:
                parts.append(f"  cwd: {cwd}")
            if git_branch:
                parts.append(f"  branch: {git_branch}")
            user_parts.append("\n".join(parts))

        user_message = "What was I doing here?\n\n" + "\n\n".join(user_parts)

        # Build assistant message from knowledge node
        try:
            content_data = json.loads(content)
            summary = content_data.get("summary", embed_text)
        except json.JSONDecodeError, TypeError:
            summary = embed_text

        assistant_message = summary

        example = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ]
        }
        examples.append(example)

    if not examples:
        return {"total": 0, "train": 0, "valid": 0, "test": 0}

    # Shuffle and split 80/10/10
    random.shuffle(examples)
    n = len(examples)
    train_end = max(1, int(n * 0.8))
    valid_end = max(train_end + 1, train_end + int(n * 0.1))

    train = examples[:train_end]
    valid = examples[train_end:valid_end]
    test = examples[valid_end:]

    _write_jsonl(output_dir / "train.jsonl", train)
    _write_jsonl(output_dir / "valid.jsonl", valid)
    _write_jsonl(output_dir / "test.jsonl", test)

    return {
        "total": n,
        "train": len(train),
        "valid": len(valid),
        "test": len(test),
    }
