"""Tests for the brain HTTP OpenAPI contract."""

from __future__ import annotations

import re

from starlette.applications import Starlette
from starlette.testclient import TestClient

from hippo_brain.openapi import build_openapi_spec
from hippo_brain.server import BrainServer


def _normalize_route_path(path: str) -> str:
    """Strip Starlette path-param type annotations (``{id:int}`` -> ``{id}``)
    so route paths line up with OpenAPI path templates."""
    return re.sub(r":\w+\}", "}", path)


def _documented_methods(route_methods: set[str]) -> set[str]:
    """Drop HEAD, which Starlette auto-registers for GET routes, and lower-case
    to match the OpenAPI verb keys (``get``/``post``)."""
    return {m.lower() for m in route_methods if m.upper() != "HEAD"}


def test_openapi_spec_lists_all_brain_routes():
    spec = build_openapi_spec()

    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "Hippo Brain API"

    paths = spec["paths"]
    expected_paths = {
        "/health",
        "/sessions",
        "/events",
        "/knowledge",
        "/knowledge/{id}",
        "/query",
        "/ask",
        "/control/pause",
        "/control/resume",
        "/openapi.json",
    }
    assert set(paths) == expected_paths

    assert paths["/health"]["get"]["operationId"] == "getHealth"
    assert paths["/sessions"]["get"]["operationId"] == "listSessions"
    assert paths["/events"]["get"]["operationId"] == "listEvents"
    assert paths["/knowledge"]["get"]["operationId"] == "listKnowledge"
    assert paths["/knowledge/{id}"]["get"]["operationId"] == "getKnowledge"
    assert paths["/query"]["post"]["operationId"] == "queryKnowledge"
    assert paths["/ask"]["post"]["operationId"] == "askQuestion"
    assert paths["/control/pause"]["post"]["operationId"] == "pauseEnrichment"
    assert paths["/control/resume"]["post"]["operationId"] == "resumeEnrichment"
    assert paths["/openapi.json"]["get"]["operationId"] == "getOpenApiSpec"


def test_pause_response_schema_documents_quiescence_fields():
    pause_response = build_openapi_spec()["paths"]["/control/pause"]["post"]["responses"]["200"]
    schema = build_openapi_spec()["components"]["schemas"]["PauseResponse"]

    assert pause_response["content"]["application/json"]["schema"]["$ref"].endswith(
        "/PauseResponse"
    )
    assert set(schema["required"]) == {
        "paused_at",
        "in_flight_finished",
        "enrichment_active",
        "query_inflight",
    }
    assert schema["properties"]["in_flight_finished"]["type"] == "boolean"
    assert schema["properties"]["enrichment_active"]["type"] == "boolean"
    assert schema["properties"]["query_inflight"]["type"] == "integer"


def test_openapi_json_route_serves_same_contract(tmp_db):
    _, db_path = tmp_db
    server = BrainServer(db_path=str(db_path))

    client = TestClient(Starlette(routes=server.get_routes()))

    resp = client.get("/openapi.json")

    assert resp.status_code == 200
    assert resp.json() == build_openapi_spec()


def test_openapi_spec_covers_every_server_route(tmp_db):
    """Drift guard: every route registered on the live server must appear in
    the explicit OpenAPI contract with matching documented methods. Catches
    a new route added to ``BrainServer.get_routes`` without a corresponding
    spec entry (or vice versa)."""
    _, db_path = tmp_db
    server = BrainServer(db_path=str(db_path))

    spec = build_openapi_spec()
    spec_paths = {path: {m.lower() for m in ops} for path, ops in spec["paths"].items()}

    route_paths = {
        _normalize_route_path(route.path): _documented_methods(route.methods or set())
        for route in server.get_routes()
    }

    assert set(route_paths) == set(spec_paths), (
        "Server routes and OpenAPI paths drifted. "
        f"Only on server: {set(route_paths) - set(spec_paths)}. "
        f"Only in spec: {set(spec_paths) - set(route_paths)}."
    )

    for path, methods in route_paths.items():
        assert methods == spec_paths[path], (
            f"Method mismatch for {path}: server={methods}, spec={spec_paths[path]}"
        )


def test_knowledge_list_uses_summary_schema_without_heavy_fields():
    """The /knowledge list response must not promise fields the list handler
    never returns. ``list_knowledge`` emits a 7-field projection; only
    ``get_knowledge`` (/knowledge/{id}) hydrates embed_text/related_entities/
    related_events. The list items therefore reference the lighter summary
    schema, while the by-id endpoint keeps the full KnowledgeNode."""
    schemas = build_openapi_spec()["components"]["schemas"]

    list_item_ref = schemas["KnowledgeListResponse"]["properties"]["nodes"]["items"]["$ref"]
    assert list_item_ref.endswith("/KnowledgeNodeSummary")

    heavy = {"embed_text", "related_entities", "related_events"}
    assert heavy.isdisjoint(schemas["KnowledgeNodeSummary"]["properties"])
    assert heavy.issubset(schemas["KnowledgeNode"]["properties"])
