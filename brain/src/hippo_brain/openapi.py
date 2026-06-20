"""OpenAPI contract for the Hippo brain HTTP API.

The brain server is Starlette, not FastAPI, so there is no automatic OpenAPI
generation. Keep this spec explicit and close to ``BrainServer.get_routes``.
"""

from __future__ import annotations

import json

from hippo_brain.version import get_version


def _json_response(schema_ref: str | dict, description: str = "OK") -> dict:
    schema = {"$ref": schema_ref} if isinstance(schema_ref, str) else schema_ref
    return {
        "description": description,
        "content": {"application/json": {"schema": schema}},
    }


def _error_response(description: str = "Error") -> dict:
    return _json_response("#/components/schemas/ErrorResponse", description)


def _int_query_param(name: str, description: str, required: bool = False) -> dict:
    return {
        "name": name,
        "in": "query",
        "required": required,
        "schema": {"type": "integer"},
        "description": description,
    }


def _string_query_param(name: str, description: str, required: bool = False) -> dict:
    return {
        "name": name,
        "in": "query",
        "required": required,
        "schema": {"type": "string"},
        "description": description,
    }


def _pagination_params() -> list[dict]:
    return [
        _int_query_param("limit", "Maximum number of rows to return."),
        _int_query_param("offset", "Zero-based pagination offset."),
    ]


def build_openapi_spec() -> dict:
    """Return the OpenAPI document for the current brain HTTP API."""

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Hippo Brain API",
            "version": get_version(),
            "description": "Local HTTP API for Hippo enrichment, retrieval, and brain control.",
        },
        "paths": {
            "/health": {
                "get": {
                    "operationId": "getHealth",
                    "summary": "Return brain health and enrichment status.",
                    "responses": {"200": _json_response("#/components/schemas/HealthResponse")},
                }
            },
            "/sessions": {
                "get": {
                    "operationId": "listSessions",
                    "summary": "List captured shell sessions.",
                    "parameters": [
                        *_pagination_params(),
                        _int_query_param("since_ms", "Only include sessions after this epoch-ms."),
                    ],
                    "responses": {
                        "200": _json_response("#/components/schemas/SessionsResponse"),
                        "400": _error_response("Invalid query parameter."),
                    },
                }
            },
            "/events": {
                "get": {
                    "operationId": "listEvents",
                    "summary": "List captured shell events.",
                    "parameters": [
                        *_pagination_params(),
                        _int_query_param("session_id", "Restrict to one session."),
                        _int_query_param("since_ms", "Only include events after this epoch-ms."),
                        _string_query_param("project", "Substring match against event cwd."),
                    ],
                    "responses": {
                        "200": _json_response("#/components/schemas/EventsResponse"),
                        "400": _error_response("Invalid query parameter."),
                    },
                }
            },
            "/knowledge": {
                "get": {
                    "operationId": "listKnowledge",
                    "summary": "List enriched knowledge nodes.",
                    "parameters": [
                        *_pagination_params(),
                        _string_query_param("node_type", "Restrict to one node type."),
                        _int_query_param("since_ms", "Only include nodes after this epoch-ms."),
                    ],
                    "responses": {
                        "200": _json_response("#/components/schemas/KnowledgeListResponse"),
                        "400": _error_response("Invalid query parameter."),
                    },
                }
            },
            "/knowledge/{id}": {
                "get": {
                    "operationId": "getKnowledge",
                    "summary": "Get one knowledge node by numeric ID.",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                            "description": "Knowledge node ID.",
                        }
                    ],
                    "responses": {
                        "200": _json_response("#/components/schemas/KnowledgeNode"),
                        "404": _error_response("Knowledge node not found."),
                    },
                }
            },
            "/query": {
                "post": {
                    "operationId": "queryKnowledge",
                    "summary": "Search events and knowledge nodes.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/QueryRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": _json_response("#/components/schemas/QueryResponse"),
                        "400": _error_response("Invalid request body."),
                        "500": _error_response("Query failed."),
                    },
                }
            },
            "/ask": {
                "post": {
                    "operationId": "askQuestion",
                    "summary": "Ask a RAG question against enriched knowledge.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/AskRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": _json_response("#/components/schemas/AskResponse"),
                        "400": _error_response("Invalid request body."),
                        "503": _error_response("RAG dependencies unavailable."),
                    },
                }
            },
            "/control/pause": {
                "post": {
                    "operationId": "pauseEnrichment",
                    "summary": "Pause enrichment claims while keeping ingestion running.",
                    "responses": {"200": _json_response("#/components/schemas/PauseResponse")},
                }
            },
            "/control/resume": {
                "post": {
                    "operationId": "resumeEnrichment",
                    "summary": "Resume enrichment claims.",
                    "responses": {"200": _json_response("#/components/schemas/ResumeResponse")},
                }
            },
            "/openapi.json": {
                "get": {
                    "operationId": "getOpenApiSpec",
                    "summary": "Return this OpenAPI document.",
                    "responses": {
                        "200": _json_response({"type": "object", "additionalProperties": True})
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "ErrorResponse": {
                    "type": "object",
                    "required": ["error"],
                    "properties": {"error": {"type": "string"}},
                },
                "HealthResponse": {
                    "type": "object",
                    "required": [
                        "status",
                        "version",
                        "expected_schema_version",
                        "inference_reachable",
                        "enrichment_running",
                        "paused",
                        "db_reachable",
                    ],
                    "properties": {
                        "status": {"type": "string"},
                        "version": {"type": "string"},
                        "expected_schema_version": {"type": "integer"},
                        "accepted_read_versions": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "inference_reachable": {"type": "boolean"},
                        "enrichment_running": {"type": "boolean"},
                        "paused": {"type": "boolean"},
                        "paused_at": {"type": ["string", "null"], "format": "date-time"},
                        "db_reachable": {"type": "boolean"},
                        "queue_depth": {"type": "integer"},
                        "queue_failed": {"type": "integer"},
                        "claude_queue_depth": {"type": "integer"},
                        "claude_queue_failed": {"type": "integer"},
                        "browser_queue_depth": {"type": "integer"},
                        "browser_queue_failed": {"type": "integer"},
                        "workflow_queue_depth": {"type": "integer"},
                        "workflow_queue_failed": {"type": "integer"},
                        "enrichment_model": {"type": "string"},
                        "enrichment_model_preferred": {"type": "string"},
                        "query_inflight": {"type": "integer"},
                        "embed_model_drift": {"type": ["string", "null"]},
                        "last_success_at_ms": {"type": ["integer", "null"]},
                        "last_error": {"type": ["string", "null"]},
                        "last_error_at_ms": {"type": ["integer", "null"]},
                        "telemetry_enabled": {"type": "boolean"},
                        "telemetry_active": {"type": "boolean"},
                    },
                    "additionalProperties": True,
                },
                "Session": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "start_time": {"type": "integer"},
                        "hostname": {"type": "string"},
                        "shell": {"type": "string"},
                        "event_count": {"type": "integer"},
                    },
                    "additionalProperties": True,
                },
                "SessionsResponse": {
                    "type": "object",
                    "required": ["sessions", "total"],
                    "properties": {
                        "sessions": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Session"},
                        },
                        "total": {"type": "integer"},
                    },
                },
                "Event": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "session_id": {"type": ["integer", "null"]},
                        "timestamp": {"type": "integer"},
                        "command": {"type": "string"},
                        "exit_code": {"type": "integer"},
                        "duration_ms": {"type": "integer"},
                        "cwd": {"type": "string"},
                        "git_branch": {"type": ["string", "null"]},
                    },
                    "additionalProperties": True,
                },
                "EventsResponse": {
                    "type": "object",
                    "required": ["events", "total"],
                    "properties": {
                        "events": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Event"},
                        },
                        "total": {"type": "integer"},
                    },
                },
                "KnowledgeNode": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "uuid": {"type": "string"},
                        "content": {"type": "string"},
                        "embed_text": {"type": ["string", "null"]},
                        "node_type": {"type": ["string", "null"]},
                        "outcome": {"type": ["string", "null"]},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "created_at": {"type": "integer"},
                        "related_entities": {
                            "type": "array",
                            "items": {"type": "object", "additionalProperties": True},
                        },
                        "related_events": {
                            "type": "array",
                            "items": {"type": "object", "additionalProperties": True},
                        },
                    },
                    "additionalProperties": True,
                },
                "KnowledgeNodeSummary": {
                    "type": "object",
                    "description": (
                        "Knowledge node as returned by the list endpoint: a subset "
                        "of KnowledgeNode. The list query does not hydrate "
                        "embed_text/related_entities/related_events — only the "
                        "/knowledge/{id} endpoint returns those."
                    ),
                    "properties": {
                        "id": {"type": "integer"},
                        "uuid": {"type": "string"},
                        "content": {"type": "string"},
                        "node_type": {"type": ["string", "null"]},
                        "outcome": {"type": ["string", "null"]},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "created_at": {"type": "integer"},
                    },
                    "additionalProperties": True,
                },
                "KnowledgeListResponse": {
                    "type": "object",
                    "required": ["nodes", "total"],
                    "properties": {
                        "nodes": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/KnowledgeNodeSummary"},
                        },
                        "total": {"type": "integer"},
                    },
                },
                "QueryRequest": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string"},
                        "mode": {"type": "string", "enum": ["semantic", "lexical"]},
                        "limit": {"type": "integer", "minimum": 1},
                    },
                },
                "QueryResponse": {"type": "object", "additionalProperties": True},
                "AskRequest": {
                    "type": "object",
                    "required": ["question"],
                    "properties": {
                        "question": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1},
                    },
                },
                "AskResponse": {"type": "object", "additionalProperties": True},
                "PauseResponse": {
                    "type": "object",
                    "required": [
                        "paused_at",
                        "in_flight_finished",
                        "enrichment_active",
                        "query_inflight",
                    ],
                    "properties": {
                        "paused_at": {"type": "string", "format": "date-time"},
                        "in_flight_finished": {"type": "boolean"},
                        "enrichment_active": {"type": "boolean"},
                        "query_inflight": {"type": "integer"},
                    },
                },
                "ResumeResponse": {
                    "type": "object",
                    "required": ["resumed_at"],
                    "properties": {"resumed_at": {"type": "string", "format": "date-time"}},
                },
            }
        },
    }


def main() -> int:
    print(json.dumps(build_openapi_spec(), indent=2, sort_keys=True))
    return 0
