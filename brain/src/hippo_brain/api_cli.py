"""Structured CLI for the Hippo brain HTTP API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

import httpx

from hippo_brain.bench.prod_config import default_prod_brain_url
from hippo_brain.openapi import build_openapi_spec

DEFAULT_TIMEOUT = 10.0


def _base_url(explicit_url: str | None) -> str:
    return (
        explicit_url
        or os.environ.get("HIPPO_BRAIN_URL")
        or os.environ.get("BRAIN_URL")
        or default_prod_brain_url()
    ).rstrip("/")


def _optional_params(values: dict[str, Any]) -> dict[str, Any] | None:
    params = {key: value for key, value in values.items() if value is not None}
    return params or None


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _request(args: argparse.Namespace, method: str, path: str, params=None, body=None) -> int:
    url = f"{_base_url(args.url)}{path}"
    try:
        response = httpx.request(
            method,
            url,
            params=params,
            json=body,
            timeout=args.timeout,
        )
    except httpx.RequestError as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 1

    if response.status_code >= 400:
        print(f"HTTP {response.status_code}: {response.text}", file=sys.stderr)
        return 1

    try:
        _emit_json(response.json())
    except ValueError:
        print(response.text)
    return 0


def _handle_health(args: argparse.Namespace) -> int:
    return _request(args, "GET", "/health")


def _handle_sessions(args: argparse.Namespace) -> int:
    return _request(
        args,
        "GET",
        "/sessions",
        params=_optional_params(
            {"limit": args.limit, "offset": args.offset, "since_ms": args.since_ms}
        ),
    )


def _handle_events(args: argparse.Namespace) -> int:
    return _request(
        args,
        "GET",
        "/events",
        params=_optional_params(
            {
                "limit": args.limit,
                "offset": args.offset,
                "session_id": args.session_id,
                "since_ms": args.since_ms,
                "project": args.project,
            }
        ),
    )


def _handle_knowledge(args: argparse.Namespace) -> int:
    return _request(
        args,
        "GET",
        "/knowledge",
        params=_optional_params(
            {
                "limit": args.limit,
                "offset": args.offset,
                "node_type": args.node_type,
                "since_ms": args.since_ms,
            }
        ),
    )


def _handle_knowledge_get(args: argparse.Namespace) -> int:
    return _request(args, "GET", f"/knowledge/{args.id}")


def _handle_query(args: argparse.Namespace) -> int:
    return _request(
        args,
        "POST",
        "/query",
        body={"text": args.text, "mode": args.mode, "limit": args.limit},
    )


def _handle_ask(args: argparse.Namespace) -> int:
    return _request(
        args,
        "POST",
        "/ask",
        body={"question": args.question, "limit": args.limit},
    )


def _handle_pause(args: argparse.Namespace) -> int:
    return _request(args, "POST", "/control/pause")


def _handle_resume(args: argparse.Namespace) -> int:
    return _request(args, "POST", "/control/resume")


def _handle_openapi(args: argparse.Namespace) -> int:
    if args.offline:
        _emit_json(build_openapi_spec())
        return 0
    return _request(args, "GET", "/openapi.json")


def _add_pagination(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hippo-brain-api",
        description="Call the local Hippo brain HTTP API.",
    )
    parser.add_argument(
        "--url",
        help=(
            "Brain base URL. Defaults to HIPPO_BRAIN_URL, BRAIN_URL, or "
            "http://127.0.0.1:<[brain].port>."
        ),
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)

    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="GET /health")
    health.set_defaults(handler=_handle_health)

    sessions = sub.add_parser("sessions", help="GET /sessions")
    _add_pagination(sessions)
    sessions.add_argument("--since-ms", type=int)
    sessions.set_defaults(handler=_handle_sessions)

    events = sub.add_parser("events", help="GET /events")
    _add_pagination(events)
    events.add_argument("--session-id", type=int)
    events.add_argument("--since-ms", type=int)
    events.add_argument("--project")
    events.set_defaults(handler=_handle_events)

    knowledge = sub.add_parser("knowledge", help="GET /knowledge")
    _add_pagination(knowledge)
    knowledge.add_argument("--node-type")
    knowledge.add_argument("--since-ms", type=int)
    knowledge.set_defaults(handler=_handle_knowledge)

    knowledge_get = sub.add_parser("knowledge-get", help="GET /knowledge/{id}")
    knowledge_get.add_argument("id", type=int)
    knowledge_get.set_defaults(handler=_handle_knowledge_get)

    query = sub.add_parser("query", help="POST /query")
    query.add_argument("text")
    query.add_argument("--mode", choices=("semantic", "lexical"), default="semantic")
    query.add_argument("--limit", type=int, default=10)
    query.set_defaults(handler=_handle_query)

    ask = sub.add_parser("ask", help="POST /ask")
    ask.add_argument("question")
    ask.add_argument("--limit", type=int, default=10)
    ask.set_defaults(handler=_handle_ask)

    pause = sub.add_parser("pause", help="POST /control/pause")
    pause.set_defaults(handler=_handle_pause)

    resume = sub.add_parser("resume", help="POST /control/resume")
    resume.set_defaults(handler=_handle_resume)

    openapi = sub.add_parser("openapi", help="GET /openapi.json")
    openapi.add_argument(
        "--offline",
        action="store_true",
        help="Print the built-in OpenAPI contract instead of calling the live server.",
    )
    openapi.set_defaults(handler=_handle_openapi)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)
