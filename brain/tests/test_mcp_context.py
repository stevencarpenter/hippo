import json

from hippo_brain.bench.mcp_context import response_hits, score_response


def test_wire_formats_and_source_bound_fact_scoring():
    case = {
        "id": "pins",
        "family": "policy",
        "tool": "agent_query",
        "arguments": {"query": "retain exact npm pins"},
        "judgments": {"support": 3, "workflow": 0},
        "facts": [{"id": "pins", "node_ids": ["support"], "any_text": ["retain exact npm pins"]}],
    }
    hits = [
        {"uuid": "workflow", "summary": "retain exact npm pins"},
        {"uuid": "support", "summary": "fixed preferences removed"},
    ]
    for wire in (
        {"structuredContent": {"hits": hits}},
        {"structuredContent": {"result": hits}},
        {"content": [{"type": "text", "text": json.dumps(hit)} for hit in hits]},
    ):
        assert response_hits(wire)[0] == hits
        score = score_response(case, wire, 5)
        assert score["known_support_recall"] == 1
        assert score["known_support_mrr"] == 0.5
        assert score["literal_fact_availability"] == {"pins": False}
    hits[1]["key_decisions"] = ["retain exact npm pins"]
    assert score_response(case, {"structuredContent": {"hits": hits}}, 5)[
        "literal_fact_availability"
    ] == {"pins": True}


def test_unknown_candidates_and_errors_remain_visible():
    case = {"id": "case", "family": "family", "tool": "agent_query", "judgments": {"support": 3}}
    score = score_response(case, {"structuredContent": {"hits": [{"uuid": "unjudged"}]}}, 1)
    assert score["graded"]["unjudged_returned"] == 1
    assert score["graded"]["ndcg_at_5"] is None
    for wire in (
        {"isError": True},
        {"structuredContent": {"error": "failed"}},
        {"content": [{"type": "text", "text": "not JSON context"}]},
        {"structuredContent": {"hits": [{"uuid": "support"}, {"uuid": "support"}]}},
    ):
        assert not score_response(case, wire, 1)["success"]
    duplicates = {"structuredContent": {"hits": [{"uuid": "support"}, {"uuid": "support"}]}}
    assert score_response(case, duplicates, 1)["known_support_recall"] == 1
