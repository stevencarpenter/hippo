"""MCP search results stay small enough for an MCP client to accept them."""

import asyncio
import json

from hippo_brain.mcp import (
    RESULT_BUDGET_CHARS,
    _bounded,
    _snippet,
    _state,
    search_events,
    search_hybrid,
)


def _client_size(result: list[dict]) -> int:
    """The character count an MCP client reports: compact JSON of ``{"result": [...]}``."""
    return len(json.dumps({"result": result}, separators=(",", ":"), ensure_ascii=False))


def _fat_hit(i: int) -> dict:
    """A hit shaped like the largest ones captured from real overflowing results."""
    return {
        "uuid": f"u{i}",
        "score": 0.5,
        "summary": "s" * 3000,
        "embed_text": "e" * 3000,
        "intent": "",
        "commands_raw": "c" * 3000,
        "key_decisions": ["d" * 2000] * 9,
        "problems_encountered": ["p" * 2000] * 9,
        "outcome": "success",
        "tags": [f"t{n}" for n in range(40)],
        "cwd": "/p",
        "git_branch": "main",
        "captured_at": 1,
        "design_decisions": [{"considered": "x" * 2000, "chosen": "y", "reason": "z"}] * 9,
        "linked_event_ids": list(range(50)),
        "evidence": [
            {
                "ref": f"claude-{n}",
                "source_kind": "claude",
                "timestamp_ms": n,
                "excerpt": "x" * 500,
                "freshness": {"stale": n == 0, "capture_health": {"events_last_1h": 72}},
            }
            for n in range(25)
        ],
        "confidence": {
            "level": "medium",
            "score": 0.58,
            "factors": [{"name": "n", "detail": "d" * 300}] * 6,
            "explanation": "e" * 193,
        },
    }


def test_small_hits_pass_through_unchanged():
    hit = {"uuid": "u1", "score": 0.9, "summary": "short", "tags": ["a"], "captured_at": 0}
    assert _bounded([hit]) == [hit]


def test_compact_hit_keeps_identity_and_bounds_every_field():
    [hit] = _bounded([_fat_hit(1)])
    assert (hit["uuid"], hit["outcome"], hit["cwd"], hit["git_branch"]) == (
        "u1",
        "success",
        "/p",
        "main",
    )
    assert len(hit["summary"]) <= 500
    assert len(hit["embed_text"]) <= 300
    assert len(hit["key_decisions"]) == 5 and all(len(d) <= 300 for d in hit["key_decisions"])
    # Evidence stays citable (ref + staleness) without excerpts or capture telemetry.
    assert hit["evidence"] == [
        {"ref": f"claude-{n}", "source_kind": "claude", "timestamp_ms": n, "stale": n == 0}
        for n in range(3)
    ]
    assert hit["evidence_total"] == 25
    assert hit["confidence"] == {"level": "medium", "score": 0.58}
    assert len(json.dumps(hit)) < 12_000


def test_result_stays_under_budget_and_reports_omitted_hits():
    out = _bounded([_fat_hit(i) for i in range(100)])
    assert _client_size(out) <= RESULT_BUDGET_CHARS
    kept, marker = out[:-1], out[-1]
    assert kept and [h["uuid"] for h in kept] == [f"u{i}" for i in range(len(kept))]
    assert marker["truncated"] is True
    assert marker["omitted_hits"] == 100 - len(kept)


def test_non_ascii_text_is_counted_as_characters_not_escapes():
    hits = [{"uuid": f"u{i}", "summary": "設計の決定" * 60} for i in range(40)]
    out = _bounded(hits)
    assert len(out) == 40  # 40 hits of about 330 characters fit; "\\uXXXX" escapes would not
    assert _client_size(out) <= RESULT_BUDGET_CHARS


def test_snippet_centres_on_the_query_match():
    text = "a" * 4000 + " the NEEDLE is here " + "b" * 4000
    snippet = _snippet(text, "needle", 500)
    assert "NEEDLE" in snippet and len(snippet) <= 500
    assert snippet.startswith("…") and snippet.endswith("…")
    # No match, or no query: the head of the text.
    assert _snippet(text, "absent", 500).startswith("aaa")
    assert _snippet("short", "x", 500) == "short"


def test_search_hybrid_returns_bounded_hits(monkeypatch):
    async def fake_retrieve_filtered(**_kwargs):
        return [_fat_hit(i) for i in range(20)]

    monkeypatch.setattr("hippo_brain.mcp._retrieve_filtered", fake_retrieve_filtered)
    results = asyncio.run(search_hybrid("anything", limit=20))
    assert _client_size(results) <= RESULT_BUDGET_CHARS
    assert "excerpt" not in json.dumps(results)


def test_search_events_shows_the_matching_part_of_a_long_session_summary(tmp_db):
    conn, db_path = tmp_db
    summary = "x" * 6000 + " renamed the frobnicator module " + "y" * 6000
    conn.execute(
        """INSERT INTO agentic_sessions
             (session_id, harness, segment_index, project_dir, cwd, summary_text,
              message_count, start_time, end_time, source_file, content_hash, created_at)
           VALUES ('s1', 'claude-code', 0, '/p', '/p', ?, 3, 1000, 2000, 's1.jsonl', 'h', 1000)""",
        (summary,),
    )
    conn.commit()
    old_db = _state.db_path
    _state.db_path = str(db_path)
    try:
        results = asyncio.run(search_events(query="frobnicator", source="claude"))
    finally:
        _state.db_path = old_db
    assert len(results) == 1
    assert "frobnicator" in results[0]["summary"]
    assert len(results[0]["summary"]) <= 500
