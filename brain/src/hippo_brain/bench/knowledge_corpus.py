"""Frozen corpus authoring, blinded judgments, and paired decision evaluation.

This extends the decision benchmark with offline operations. No command in this
module invokes inference. Generated evidence always lives outside the checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from hippo_brain.bench.decision_sidecar import digest
from hippo_brain.bench.knowledge_probe import open_readonly
from hippo_brain.bench.paths import hippo_bench_root
from hippo_brain.decision_capture import external_path
from hippo_brain.enrichment import is_enrichment_eligible
from hippo_brain.evaluation import graded_retrieval_metrics, paired_family_interval
from hippo_brain.redaction import redact

VERSION = "knowledge-corpus-v2"
RUBRIC = "relevance-0-3-v1"
TOPIC_RUBRIC = "topics-present-absent-v1"
SOURCE_QUOTAS = {
    "shell": 200,
    "claude": 200,
    "codex": 150,
    "browser": 150,
    "workflow": 100,
    "auto-memory": 100,
    "other-agentic": 100,
}
SPLITS = ("development", "calibration", "test")
TOPICS = Path(__file__).parents[1] / "_fixtures/knowledge_topics.json"
PASS_BUDGETS = (1, 2, 4, 8, 16, 100)
CAPTURE_ORIGINS = ("interactive", "captured", "http", "mcp")


class Judgment(TypedDict):
    query_id: str
    node_uuid: str
    question: NotRequired[str]
    query_input_hash: NotRequired[str]
    node_input_hash: NotRequired[str]
    grade: int
    evidence: str
    annotator: str
    annotator_type: str
    rubric_version: str
    adjudication: str


class RankingRecord(TypedDict):
    query_id: str
    arm: str
    order: list[str]
    input_hash: str
    model: str
    recipe_hash: str
    status: str
    elapsed_ms: float


def read_records(path: Path) -> list[dict[str, Any]]:
    """Read existing JSON/JSONL question and decision artifact formats."""
    if path.is_dir():
        return [row for item in sorted(path.glob("*.json")) for row in read_records(item)]
    body = path.read_text()
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in body.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = value.get("questions", value.get("rows", [value]))
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"expected object records: {path.name}")
    return value


def _json(path: Path, value: Any) -> None:
    """Publish private, finite JSON without following an existing file symlink."""
    path = external_path(path)
    body = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(body)


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _source_group(kind: str) -> str:
    return kind if kind in SOURCE_QUOTAS else "other-agentic"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _source_catalog(
    conn: sqlite3.Connection,
) -> tuple[dict[str, dict[str, Any]], dict[int, list[str]]]:
    """Use source identity, not mutable row numbers alone, for grouping and evidence."""
    sources: dict[str, dict[str, Any]] = {}
    links: dict[int, list[str]] = defaultdict(list)
    specs = (
        ("events", "knowledge_node_events", "event_id", "shell"),
        ("browser_events", "knowledge_node_browser_events", "browser_event_id", "browser"),
        ("agentic_sessions", "knowledge_node_agentic_sessions", "agentic_session_id", "agentic"),
        ("workflow_runs", "knowledge_node_workflow_runs", "run_id", "workflow"),
        ("memory_chunks", "knowledge_node_memory_chunks", "memory_chunk_id", "auto-memory"),
    )
    conn.row_factory = sqlite3.Row
    for table, link_table, column, kind in specs:
        if not _table_exists(conn, table):
            continue
        row_keys: dict[int, str] = {}
        ancestry: dict[str, str] = {}
        if kind == "agentic":
            for sid, parent in conn.execute(
                "SELECT session_id,parent_session_id FROM agentic_sessions"
            ):
                if parent:
                    if sid in ancestry and ancestry[sid] != parent:
                        raise ValueError("source session has inconsistent parent identities")
                    ancestry[sid] = parent

        def ancestor(session: str) -> str:
            visited: list[str] = []
            while session in ancestry:
                if session in visited:
                    # A malformed cycle remains one family; audit must resolve it.
                    return min(visited[visited.index(session) :])
                visited.append(session)
                session = ancestry[session]
            return session

        query = f"SELECT * FROM {table}"
        if kind == "auto-memory" and _table_exists(conn, "memory_documents"):
            query = (
                "SELECT c.*, d.uuid document_uuid, d.repository, d.logical_path, "
                "r.content_hash revision_hash FROM memory_chunks c "
                "JOIN memory_revisions r ON r.id=c.revision_id "
                "JOIN memory_documents d ON d.id=r.document_id"
            )
        for record in conn.execute(query):
            row = dict(record)
            source = kind
            if kind == "agentic":
                source = {"claude-code": "claude"}.get(
                    row.get("harness"), row.get("harness", "other-agentic")
                )
            key = f"{source}-{row['id']}"
            row_keys[row["id"]] = key
            if kind == "shell":
                family = f"shell:{row.get('session_id', row['id'])}"
                text = "\n".join(str(row.get(k) or "") for k in ("command", "stdout", "stderr"))
                identity = [
                    row.get("envelope_id"),
                    row.get("session_id"),
                    row.get("timestamp"),
                    row.get("command"),
                ]
            elif kind == "agentic":
                family = f"agentic:{ancestor(str(row.get('session_id', row['id'])))}"
                text = str(row.get("summary_text") or "")
                identity = [row.get("harness"), row.get("session_id"), row.get("segment_index")]
            elif kind == "browser":
                family = "browser:" + digest(row.get("url", key))
                text = "\n".join(str(row.get(k) or "") for k in ("title", "url", "extracted_text"))
                identity = [row.get("envelope_id"), row.get("timestamp"), row.get("url")]
            elif kind == "workflow":
                family = "workflow:" + digest([row.get("repo"), row.get("head_sha")])
                text = str(row.get("raw_json") or "")
                identity = [row.get("repo"), row.get("html_url"), row.get("head_sha")]
            else:
                family = "memory:" + str(
                    row.get("document_uuid", row.get("revision_id", row["id"]))
                )
                text = str(row.get("content") or "")
                identity = [row.get("document_uuid"), row.get("revision_hash"), row.get("ordinal")]
            eligible, reason = is_enrichment_eligible(
                row, "claude" if source == "codex" else source
            )
            if row.get("probe_tag") is not None or (
                kind == "shell" and row.get("source_kind", "shell") != "shell"
            ):
                eligible, reason = False, "diagnostic_or_derived_source"
            timestamp = (
                row.get("timestamp")
                or row.get("end_time")
                or row.get("completed_at")
                or row.get("created_at")
                or 0
            )
            clean = redact(text)
            sources[key] = {
                "key": key,
                "kind": source,
                "identity_hash": digest(identity),
                "content_hash": digest(clean),
                "task_family_id": family,
                "project": redact(
                    str(
                        row.get("project_dir")
                        or row.get("git_repo")
                        or row.get("repository")
                        or row.get("repo")
                        or row.get("cwd")
                        or ""
                    )
                ),
                "timestamp_ms": timestamp,
                "eligible": eligible,
                "eligibility_reason": reason,
                "excerpt": clean[:6000],
                "excerpt_start": 0,
                "excerpt_end": min(len(clean), 6000),
                "truncated": len(clean) > 6000,
            }
        if _table_exists(conn, link_table):
            for node_id, source_id in conn.execute(
                f"SELECT knowledge_node_id, {column} FROM {link_table}"
            ):
                if source_id in row_keys:
                    links[node_id].append(row_keys[source_id])
    return sources, links


def _node_catalog(
    conn: sqlite3.Connection, sources: dict[str, dict[str, Any]], links: dict[int, list[str]]
) -> list[dict[str, Any]]:
    from hippo_brain.classification import node_input

    parents: dict[str, str] = {}

    def find(key: str) -> str:
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def join(keys: list[str]) -> None:
        roots = [find(key) for key in keys]
        for root in roots:
            parents[root] = min(roots)

    nodes = []
    duplicates: dict[str, str] = {}
    for record in conn.execute(
        "SELECT id, uuid, content, embed_text, created_at, updated_at FROM knowledge_nodes ORDER BY uuid"
    ):
        row = dict(record)
        keys = sorted(links.get(row["id"], []))
        family_keys = [sources[key]["task_family_id"] for key in keys] or [
            f"unlinked:{row['uuid']}"
        ]
        try:
            content = json.loads(row["content"])
        except json.JSONDecodeError, TypeError:
            content = None
        valid = isinstance(content, dict) and isinstance(content.get("summary", ""), str)
        identity = node_input(conn, row["id"]) if valid else None
        state = identity[1] if identity else {"summary": "", "detail": ""}
        # Exact duplicate evidence and every source segment remain in one split.
        # Near-duplicate/task grouping still needs the explicit human audit gate.
        content_key = digest(" ".join((state["summary"] + " " + state["detail"]).lower().split()))
        if content_key in duplicates:
            family_keys.append(duplicates[content_key])
        duplicates[content_key] = family_keys[0]
        join(family_keys)
        source_kinds = sorted({sources[key]["kind"] for key in keys})
        nodes.append(
            {
                "node_id": row["id"],
                "node_uuid": row["uuid"],
                "input_hash": identity[2] if identity else digest(state),
                "state": state,
                "source_keys": keys,
                "source_kinds": source_kinds,
                "task_family_id": family_keys[0],
                "created_at_ms": row["created_at"],
                "updated_at_ms": row["updated_at"],
                "content_hash": digest(row["content"]),
                "eligible": valid and any(sources[key]["eligible"] for key in keys),
                "exclusion_reason": None if valid else "non_object_content",
            }
        )
    for source in sources.values():
        source["task_family_id"] = find(source["task_family_id"])
    for node in nodes:
        node["task_family_id"] = find(node["task_family_id"])
    return nodes


def family_split(family: str, *, classification: bool = False) -> str:
    """Stable family hashing; report actual counts instead of breaking families."""
    bucket = int(digest([VERSION, family])[:8], 16) / 2**32
    return (
        "development"
        if bucket < (0.6 if classification else 0.5)
        else "calibration"
        if bucket < (0.8 if classification else 0.7)
        else "test"
    )


def prepare(
    source: Path,
    out: Path,
    *,
    questions: Path | None = None,
    captures: Path | None = None,
    query_target: int = 1000,
    node_target: int = 3000,
) -> dict[str, Any]:
    """Freeze the whole DB and materialize honest authoring/annotation queues."""
    if query_target < 1 or node_target < 1:
        raise ValueError("corpus targets must be positive")
    out = external_path(out)
    if out.exists():
        raise FileExistsError("choose a new corpus directory; frozen inputs are immutable")
    out.mkdir(mode=0o700, parents=True)
    snapshot = out / "baseline.sqlite"
    with (
        closing(open_readonly(source)) as original,
        closing(sqlite3.connect(snapshot)) as destination,
    ):
        original.execute("BEGIN")
        original.execute("SELECT count(*) FROM knowledge_nodes").fetchone()
        original.backup(destination, pages=2048)
    snapshot.chmod(0o600)
    now = time.time_ns() // 1_000_000
    with closing(open_readonly(snapshot)) as conn:
        sources, links = _source_catalog(conn)
        nodes = _node_catalog(conn, sources, links)
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        embedding = (
            dict(conn.execute("SELECT * FROM embed_model_meta LIMIT 1").fetchone())
            if _table_exists(conn, "embed_model_meta")
            else {}
        )
    by_source: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for key in node["source_keys"]:
            by_source[key].append(node["node_uuid"])
    raw_queries = read_records(questions) if questions else []
    if captures:
        raw_queries += [
            row
            for row in read_records(captures)
            if row.get("origin") not in ("benchmark", "probe", "synthetic")
        ]
    # A cross-source question is a grouping constraint, not a reason to leak its
    # sources across splits. Merge transitive task families before assignment.
    family_parents: dict[str, str] = {}

    def family_root(family: str) -> str:
        family_parents.setdefault(family, family)
        while family_parents[family] != family:
            family_parents[family] = family_parents[family_parents[family]]
            family = family_parents[family]
        return family

    for raw in raw_queries:
        keys = list(raw.get("source_keys", [])) or (
            [raw["golden_event_id"]] if raw.get("golden_event_id") else []
        )
        families = [family_root(sources[key]["task_family_id"]) for key in keys if key in sources]
        if raw.get("task_family_id"):
            families.append(family_root("task:" + str(raw["task_family_id"])))
        for family in families:
            family_parents[family] = min(families)
    for item in list(sources.values()) + nodes:
        item["task_family_id"] = family_root(item["task_family_id"])
    queries, seen = [], set()
    for raw in raw_queries:
        qid = str(raw.get("id", raw.get("qa_id", "")))
        if not qid or qid in seen:
            raise ValueError("question IDs must be present and unique")
        seen.add(qid)
        keys = list(raw.get("source_keys", [])) or (
            [raw["golden_event_id"]] if raw.get("golden_event_id") else []
        )
        known = [sources[key] for key in keys if key in sources]
        families = {s["task_family_id"] for s in known}
        family = next(
            iter(families),
            family_root("task:" + str(raw["task_family_id"]))
            if raw.get("task_family_id")
            else f"query:{qid}",
        )
        gold = sorted({uid for key in keys for uid in by_source[key]})
        origin = str(raw.get("origin") or "source_authored")
        as_of = raw.get("as_of_ms", raw.get("captured_at_ms", now))
        historical = as_of < now and origin in CAPTURE_ORIGINS
        queries.append(
            {
                "id": qid,
                "question": redact(str(raw.get("question", ""))),
                "origin": origin,
                "task_family_id": family,
                "split": family_split(family),
                "intent": raw.get("intent", ""),
                "source_bias": raw.get(
                    "source_bias", raw.get("source_filter", known[0]["kind"] if known else "mixed")
                ),
                "filters": raw.get(
                    "filters", {"source": raw["source_filter"]} if raw.get("source_filter") else {}
                ),
                "as_of_ms": as_of,
                "source_keys": keys,
                "source_content_hashes": {s["key"]: s["content_hash"] for s in known},
                "relevant_knowledge_node_uuids": gold,
                "expected_answerability": raw.get(
                    "expected_answerability", "answerable" if keys else "unknown"
                ),
                "coverage_gap_reason": raw.get("coverage_gap_reason")
                or (
                    "source_missing"
                    if len(known) != len(keys)
                    else "node_missing"
                    if keys and not gold
                    else None
                ),
                "source_captured": bool(known),
                "source_eligible": all(s["eligible"] for s in known) if known else None,
                "source_linked": bool(gold),
                "node_available": bool(gold),
                "as_of_reconstructable": not historical,
                "historical_review_reason": "source_revision_review_required"
                if historical
                else None,
                "annotation_status": "pending",
                "slices": list(raw.get("slices", [])),
            }
        )
    # Select source families, not repeated source rows, for authoring shortfalls.
    authoring = []
    used = {q["task_family_id"] for q in queries}
    counts = Counter(_source_group(q["source_bias"]) for q in queries)
    for group, quota in SOURCE_QUOTAS.items():
        needed = max(0, math.ceil(quota * query_target / 1000) - counts[group])
        candidates = sorted(
            (s for s in sources.values() if _source_group(s["kind"]) == group and s["eligible"]),
            key=lambda s: digest([VERSION, s["identity_hash"]]),
        )
        for item in candidates:
            if needed <= 0 or len(authoring) + len(queries) >= query_target:
                break
            family = item["task_family_id"]
            if family in used:
                continue
            used.add(family)
            needed -= 1
            authoring.append(
                {
                    "id": "author-" + item["identity_hash"][:20],
                    "question": "",
                    "origin": "source_authored",
                    "source_bias": item["kind"],
                    "task_family_id": family,
                    "split": family_split(family),
                    "as_of_ms": now,
                    "source_keys": [item["key"]],
                    "source_content_hashes": {item["key"]: item["content_hash"]},
                    "relevant_knowledge_node_uuids": by_source[item["key"]],
                    "source_eligible": item["eligible"],
                    "expected_answerability": "unknown",
                    "coverage_gap_reason": None if by_source[item["key"]] else "node_missing",
                    "annotation_status": "question_authoring_required",
                    "evidence": item["excerpt"],
                    "as_of_reconstructable": True,
                }
            )
    selected = sorted(
        (n for n in nodes if n["eligible"]),
        key=lambda n: digest([VERSION, "classification", n["node_uuid"]]),
    )[:node_target]
    query_families = {q["task_family_id"] for q in queries + authoring}
    for node in nodes:
        node["split"] = family_split(
            node["task_family_id"], classification=node["task_family_id"] not in query_families
        )
    taxonomy = json.loads(TOPICS.read_text())
    classification_queue = [
        {
            **node,
            "rubric_version": TOPIC_RUBRIC,
            "topics": {topic: {"label": None, "evidence": ""} for topic in taxonomy},
            "annotator": "",
            "annotator_type": "",
            "adjudication": "pending",
        }
        for node in selected
    ]
    selected_sources = {key for q in queries + authoring for key in q["source_keys"]} | {
        key for node in selected for key in node["source_keys"]
    }
    artifacts = {
        "questions.json": queries,
        "query-authoring.json": authoring,
        "classification-review.json": classification_queue,
        "nodes.json": [{k: v for k, v in n.items() if k != "state"} for n in nodes],
        "sources.json": [s for key, s in sorted(sources.items()) if key in selected_sources],
        "taxonomy.json": taxonomy,
    }
    for name, value in artifacts.items():
        _json(out / name, value)
    repo = Path(__file__).resolve().parents[4]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=repo, capture_output=True, check=True
    ).stdout
    manifest = {
        "version": VERSION,
        "created_at_ms": now,
        "source_database": str(source.resolve()),
        "snapshot_sha256": _sha(snapshot),
        "schema_version": schema_version,
        "node_count": len(nodes),
        "source_counts": dict(Counter(s["kind"] for s in sources.values())),
        "embedding": embedding,
        "embedding_dimensions": 768,
        "code_revision": revision,
        "dirty_diff_sha256": hashlib.sha256(dirty).hexdigest(),
        "extraction_sha256": _sha(Path(__file__)),
        "query_target": query_target,
        "classification_target": node_target,
        "actual_questions": len(queries),
        "authoring_slots": len(authoring),
        "classification_nodes": len(selected),
        "source_question_counts": dict(counts),
        "question_split_counts": dict(Counter(q["split"] for q in queries)),
        "classification_split_counts": dict(Counter(n["split"] for n in selected)),
        "family_assignment": "source/session families and exact duplicates; human near-duplicate audit required",
        "family_audit_human": False,
        "review_status": "pending_human_annotation",
        "files": {name: _sha(out / name) for name in artifacts},
    }
    _json(out / "manifest.json", manifest)
    return manifest


def finalize(root: Path, questions: Path, out: Path, *, family_auditor: str) -> dict[str, Any]:
    """Publish reviewed questions as a new immutable corpus; retain old evidence.

    This records the caller's named human audit, not an automatic claim of label
    correctness. Family corrections need a fresh prepare with grouped sources.
    """
    manifest, originals, nodes = load_frozen(root)
    if not family_auditor.strip():
        raise ValueError("a named human family auditor is required")
    rows = read_records(questions)
    available = {q["id"]: q for q in originals + read_records(root / "query-authoring.json")}
    sources = {s["key"]: s for s in read_records(root / "sources.json")}
    seen, normalized = set(), []
    for row in rows:
        qid = row.get("id")
        if qid not in available or qid in seen:
            raise ValueError("reviewed questions need unique existing authoring/query IDs")
        seen.add(qid)
        original = available[qid]
        if (
            not str(row.get("question", "")).strip()
            or row.get("annotation_status") != "human_reviewed"
        ):
            raise ValueError("question text and explicit human_reviewed annotation status required")
        if (
            row.get("task_family_id") != original["task_family_id"]
            or row.get("split") != original["split"]
        ):
            raise ValueError("family/split corrections require a new grouped corpus")
        if (
            row.get("source_keys") != original["source_keys"]
            or row.get("source_content_hashes") != original["source_content_hashes"]
        ):
            raise ValueError("source identity corrections require a new frozen corpus")
        answerability = row.get("expected_answerability")
        if answerability not in ("answerable", "unanswerable"):
            raise ValueError("human review must resolve expected answerability")
        if answerability == "unanswerable" and not row.get("unanswerable_evidence_audit"):
            raise ValueError("unanswerable questions require a source evidence audit")
        expected = list(row.get("relevant_knowledge_node_uuids", []))
        if any(uid not in nodes for uid in expected):
            raise ValueError("unknown relevant node UUID")
        keys = row["source_keys"]
        normalized.append(
            {
                **original,
                **row,
                "question": redact(row["question"]),
                "intent": str(row.get("intent", "")),
                "origin": original["origin"],
                "filters": row.get("filters", {}),
                "source_captured": bool(keys) and all(key in sources for key in keys),
                "source_eligible": all(sources[key]["eligible"] for key in keys if key in sources)
                if keys
                else row.get("source_eligible"),
                "source_linked": bool(expected),
                "node_available": bool(expected),
                "as_of_reconstructable": row.get("as_of_reconstructable", False),
                "coverage_gap_reason": row.get("coverage_gap_reason"),
                "slices": row.get("slices", []),
            }
        )
    if set(q["id"] for q in originals) - seen:
        raise ValueError("retain existing questions and their coverage failures")
    out = external_path(out)
    if out.exists():
        raise FileExistsError("reviewed releases use a new external directory")
    out.mkdir(parents=True, mode=0o700)
    for name in ["baseline.sqlite", *manifest["files"]]:
        target = external_path(out / name)
        if name == "questions.json":
            _json(target, normalized)
        else:
            shutil.copyfile(root / name, target)
            target.chmod(0o600)
    updated = {
        **manifest,
        "parent_corpus_hash": digest(manifest),
        "created_at_ms": time.time_ns() // 1_000_000,
        "actual_questions": len(normalized),
        "family_audit_human": True,
        "family_auditor": family_auditor,
        "review_status": "questions_reviewed_labels_pending",
        "question_split_counts": dict(Counter(q["split"] for q in normalized)),
        "source_question_counts": dict(
            Counter(_source_group(q["source_bias"]) for q in normalized)
        ),
        "files": {name: _sha(out / name) for name in manifest["files"]},
    }
    _json(out / "manifest.json", updated)
    return {"questions": len(normalized), "output": str(out), "family_auditor": family_auditor}


def load_frozen(
    root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    root = external_path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("version") != VERSION:
        raise ValueError("unsupported corpus version")
    if _sha(external_path(root / "baseline.sqlite")) != manifest["snapshot_sha256"]:
        raise ValueError("frozen database changed")
    for name, expected in manifest["files"].items():
        target = external_path(root / name)
        if target.parent != root.resolve() or _sha(target) != expected:
            raise ValueError("frozen corpus file changed or escaped its directory")
    questions = read_records(root / "questions.json")
    nodes = {n["node_uuid"]: n for n in read_records(root / "nodes.json")}
    return manifest, questions, nodes


def query_input_hash(question: Mapping[str, Any]) -> str:
    """Bind judgment semantics while allowing review-status metadata to change."""
    return digest(
        {
            "question": question["question"],
            "filters": question.get("filters") or {},
            "as_of_ms": question.get("as_of_ms"),
            "source_keys": sorted(question.get("source_keys", [])),
            "source_content_hashes": question.get("source_content_hashes", {}),
        }
    )


def pool(root: Path, rankings: Path, out: Path, *, cutoff: int = 30) -> dict[str, Any]:
    """Create a blinded union through the largest tested pool, including known answers."""
    if cutoff not in (30, 60):
        raise ValueError("acceptance pools must use cutoff 30 or 60")
    manifest, questions, nodes = load_frozen(root)
    qmap = {q["id"]: q for q in questions}
    candidates = {qid: set(q["relevant_knowledge_node_uuids"]) for qid, q in qmap.items()}
    seen: set[tuple[str, str]] = set()
    for row in read_records(rankings):
        qid, arm = row["query_id"], row["arm"]
        order = row["order"]
        if qid not in qmap or (qid, arm) in seen or len(order) != len(set(order)):
            raise ValueError("invalid or duplicate ranking identity")
        if len(order) > cutoff:
            raise ValueError("pool cutoff must cover the largest evaluated ranking")
        if any(uid not in nodes for uid in order):
            raise ValueError("ranking contains unknown node UUID")
        seen.add((qid, arm))
        candidates[qid].update(order)
    review = []
    with closing(open_readonly(root / "baseline.sqlite")) as conn:
        for qid, uuids in candidates.items():
            for uid in sorted(uuids, key=lambda uid: digest([VERSION, qid, uid])):
                raw = conn.execute(
                    "SELECT content, embed_text FROM knowledge_nodes WHERE uuid=?", (uid,)
                ).fetchone()
                if raw is None:
                    raise ValueError("judgment pool source node is missing")
                review.append(
                    {
                        "query_id": qid,
                        "question": qmap[qid]["question"],
                        "query_input_hash": query_input_hash(qmap[qid]),
                        "node_uuid": uid,
                        "node_input_hash": nodes[uid]["input_hash"],
                        "evidence_input": redact(str(raw[0])[:6000] + "\n" + str(raw[1])[:4000]),
                        "source_keys": nodes[uid]["source_keys"],
                        "grade": None,
                        "evidence": "",
                        "annotator": "",
                        "annotator_type": "",
                        "rubric_version": RUBRIC,
                        "adjudication": "pending",
                    }
                )
    result = {
        "version": VERSION,
        "corpus_hash": digest(manifest),
        "cutoff": cutoff,
        "arms": sorted({a for _, a in seen}),
        "rankings_sha256": _sha(rankings),
        "rows": review,
    }
    _json(out, result)
    return {
        "rows": len(review),
        "queries": len(candidates),
        "arms": result["arms"],
        "cutoff": cutoff,
    }


def review_package(root: Path, out: Path, *, targets: Path | None = None) -> dict[str, Any]:
    """Build human work queues without asserting any annotation is complete."""
    manifest, questions, nodes = load_frozen(root)
    out = external_path(out)
    if out.exists():
        raise FileExistsError("review packages use a new external directory")
    out.mkdir(parents=True, mode=0o700)
    sources = {s["key"]: s for s in read_records(root / "sources.json")}
    _json(
        out / "query-review.json",
        [
            {
                **q,
                "reviewer": "",
                "review_notes": "",
                "annotation_status": "pending",
                "evidence_sources": [sources[key] for key in q["source_keys"] if key in sources],
            }
            for q in questions
        ],
    )
    _json(out / "query-authoring.json", read_records(root / "query-authoring.json"))
    _json(out / "node-review.json", read_records(root / "classification-review.json"))
    _json(out / "taxonomy.json", json.loads((root / "taxonomy.json").read_text()))
    families: dict[str, dict[str, Any]] = {}
    for uid, node in nodes.items():
        family = families.setdefault(
            node["task_family_id"],
            {
                "task_family_id": node["task_family_id"],
                "split": node["split"],
                "node_uuids": [],
                "query_ids": [],
                "source_kinds": set(),
                "reviewer": "",
                "decision": None,
                "notes": "",
            },
        )
        family["node_uuids"].append(uid)
        family["source_kinds"].update(node["source_kinds"])
    for q in questions:
        family = families.setdefault(
            q["task_family_id"],
            {
                "task_family_id": q["task_family_id"],
                "split": q["split"],
                "node_uuids": [],
                "query_ids": [],
                "source_kinds": set(),
                "reviewer": "",
                "decision": None,
                "notes": "",
            },
        )
        family["query_ids"].append(q["id"])
    family_rows = sorted(
        (
            {**f, "source_kinds": sorted(f["source_kinds"]), "node_count": len(f["node_uuids"])}
            for f in families.values()
        ),
        key=lambda f: (-f["node_count"], f["task_family_id"]),
    )
    _json(out / "family-review.json", family_rows)
    qids = {q["id"] for q in questions}
    orders = (
        [
            {
                "query_id": row["qa_id"],
                "arm": "historical-retrieval-pool",
                "order": row["candidate_uuids"],
            }
            for row in read_records(targets)
            if row["qa_id"] in qids
        ]
        if targets
        else []
    )
    _json(out / "historical-pools.json", orders)
    pooled = pool(root, out / "historical-pools.json", out / "retrieval-review.json", cutoff=30)
    rubric = {
        "retrieval_version": RUBRIC,
        "grades": {
            "0": "Irrelevant evidence; explain why.",
            "1": "Related context without materially useful answer evidence.",
            "2": "Materially useful evidence for the question.",
            "3": "Directly answers the question.",
        },
        "node_version": TOPIC_RUBRIC,
        "node_labels": ["present", "absent", "insufficient_evidence"],
        "required_annotation_fields": [
            "annotator",
            "annotator_type",
            "adjudication",
            "rubric_version",
            "evidence",
        ],
        "retrieval_rules": [
            "Judge source evidence, not lexical similarity.",
            "Another answering node receives credit even if not the original reference.",
            "Unknown/unjudged is not grade zero.",
            "Record a supporting span or negative rationale for every grade.",
            "Two independent humans review every candidate on at least 20% of test queries; adjudicate disagreements.",
        ],
        "family_audit": [
            "Check large families for accidentally merged unrelated tasks.",
            "Keep session descendants and near duplicates in one split.",
            "Merge cross-source tasks using shared task_family_id before preparing a new freeze.",
            "Check source revisions before marking historical queries reconstructable.",
            "Do not tune on the locked test split.",
        ],
    }
    _json(out / "rubric.json", rubric)
    summary = {
        "corpus_hash": digest(manifest),
        "actual_questions": len(questions),
        "authoring_slots": len(read_records(root / "query-authoring.json")),
        "classification_nodes": manifest["classification_nodes"],
        "retrieval_judgment_slots": pooled["rows"],
        "retrieval_pool_scope": "historical pools plus known answer nodes; every new arm requires supplemental pooling",
        "question_split_counts": manifest["question_split_counts"],
        "classification_split_counts": manifest["classification_split_counts"],
        "missing_node_questions": sum(
            q["coverage_gap_reason"] == "node_missing" for q in questions
        ),
        "human_annotations": 0,
        "release_gate": "blocked_pending_human_review",
    }
    _json(out / "review-manifest.json", summary)
    body = f"""# Hippo retrieval and classification review

Open `query-review.json` and review the first question against its `evidence_sources` (about 2 minutes).

This package contains {len(questions)} actual questions, {summary["authoring_slots"]} empty source-grounded authoring slots, {manifest["classification_nodes"]} independently sampled nodes, and {pooled["rows"]} blinded retrieval judgment slots. Blank fields are unfinished human work. No human labels have been supplied. The release gate is blocked.

1. Read `rubric.json`. Use integer retrieval grades 0 through 3. For node topics use present, absent, or insufficient_evidence. Preserve UUIDs, input hashes, source keys, and rubric versions.
2. Inspect `family-review.json`, largest families first. Record reviewer and decision. Shared reviewed `task_family_id` values in source question inputs merge those families during a new prepare. Do not edit frozen split fields. Keep descendants, duplicates, and all cross-source parts of one task together.
3. Review actual question text and source evidence in `query-review.json`. Preserve the {summary["missing_node_questions"]} historical source-link gaps. Resolve expected answerability, historical reconstructability, source eligibility, intent and slices. Mark reviewed questions `annotation_status: human_reviewed`. Empty authoring slots are not questions. Source-answerable missing nodes are coverage failures, not unanswerable queries.
4. Label `retrieval-review.json` and `node-review.json`. Supply actual annotator identity, `annotator_type: human`, evidence spans, and reviewed/adjudicated status. Two independent humans must review all candidates for at least 20% of test queries and at least 20% of classification test nodes. Resolve conflicting labels explicitly.
5. Finalize reviewed questions as a new corpus, then pool every evaluated arm through the largest cutoff (30 or 60). Import labels against the finalized corpus hash. This package uses historical pools only and cannot establish completeness for a new arm. Changed question text, filters, as-of time, source identities, or node evidence requires re-pooling and re-grading. Changing only annotation status preserves the semantic fingerprints. Legacy labels without query and node fingerprints cannot qualify a release.

Run these commands from the Hippo checkout. Replace `<reviewer-name>` with the actual human auditor. Use a new output filename for each immutable import or report.

```sh
mise run bench:knowledge:corpus -- validate '{root}'
mise run bench:knowledge:corpus -- finalize '{root}' --questions '{out / "query-review.json"}' --family-auditor '<reviewer-name>' --out '{root.parent / "corpus-reviewed"}'
mise run bench:knowledge:corpus -- pool '{root.parent / "corpus-reviewed"}' --rankings '<all-arm-rankings.json>' --cutoff 30 --out '<new-pooled-review.json>'
mise run bench:knowledge:corpus -- import-labels '{root.parent / "corpus-reviewed"}' --labels '<completed-retrieval-review.json>' --out '<imported-retrieval-labels.json>'
mise run bench:knowledge:corpus -- import-node-labels '{root.parent / "corpus-reviewed"}' --labels '{out / "node-review.json"}' --out '<imported-node-labels.json>'
```

After generating frozen classifier predictions, sample connection pairs before labeling. Preserve the separate plan and review every selected pair, including incorrect links. Development samples cannot qualify the test gate.

```sh
mise run bench:knowledge:corpus -- connection-review '{root.parent / "corpus-reviewed"}' --predictions '<classifier-predictions.json>' --split test --out '<connection-review.json>' --plan '<connection-review.plan.json>'
mise run bench:knowledge:corpus -- connection-report '{root.parent / "corpus-reviewed"}' --predictions '<classifier-predictions.json>' --pairs '<completed-connection-review.json>' --plan '<connection-review.plan.json>' --out '<connection-report.json>'
```

Do not alter the frozen manifest, question/node/source records, taxonomy, or database. Validation checks hashes. Draft edits belong in this review directory. Changed grouping needs a new freeze. Preserve this package and its parent snapshot for the engineering record.
"""
    with (out / "README.md").open("x") as stream:
        stream.write(body)
    (out / "README.md").chmod(0o600)
    return summary


def validate_judgments(
    rows: list[dict[str, Any]],
    queries: Mapping[str, dict[str, Any]],
    nodes: Mapping[str, dict[str, Any]],
) -> list[Judgment]:
    """Validate imported decisions without claiming the supplied annotator is human."""
    seen: set[tuple[str, str, str]] = set()
    result = []
    for row in rows:
        qid, uid = row.get("query_id"), row.get("node_uuid")
        if qid not in queries or uid not in nodes:
            raise ValueError("judgment references an unknown query or node")
        if "query_input_hash" in row:
            if row["query_input_hash"] != query_input_hash(queries[qid]):
                raise ValueError("judgment query input hash changed; re-pool and re-grade")
        elif row.get("question") != queries[qid]["question"]:
            raise ValueError("legacy judgment requires the exact frozen question text")
        if "question" in row and row["question"] != queries[qid]["question"]:
            raise ValueError("judgment question text changed; re-pool and re-grade")
        if "node_input_hash" in row and row["node_input_hash"] != nodes[uid]["input_hash"]:
            raise ValueError("judgment node input hash changed; re-pool and re-grade")
        grade = row.get("grade")
        if type(grade) is not int or not 0 <= grade <= 3:
            raise ValueError("judgment grade must be an integer from zero through three")
        if (
            not isinstance(row.get("annotator"), str)
            or not row["annotator"].strip()
            or row.get("annotator_type") not in ("human", "model")
        ):
            raise ValueError("judgment requires named human/model annotator")
        if (
            row.get("adjudication") not in ("reviewed", "adjudicated")
            or row.get("rubric_version") != RUBRIC
        ):
            raise ValueError("judgment requires reviewed status and supported rubric")
        if not isinstance(row.get("evidence"), str) or not row["evidence"].strip():
            raise ValueError("judgment requires an evidence span or explicit negative rationale")
        identity = (qid, uid, row["annotator"])
        if identity in seen:
            raise ValueError("duplicate judgment by the same annotator")
        seen.add(identity)
        if (
            max(nodes[uid]["created_at_ms"], nodes[uid]["updated_at_ms"]) > queries[qid]["as_of_ms"]
            and grade > 0
        ):
            raise ValueError("positive judgment leaks a node created after the query")
        result.append(Judgment(**{key: row[key] for key in Judgment.__annotations__ if key in row}))
    return result


def import_labels(root: Path, labels: Path, out: Path) -> dict[str, Any]:
    manifest, questions, nodes = load_frozen(root)
    rows = validate_judgments(read_records(labels), {q["id"]: q for q in questions}, nodes)
    result = {
        "version": VERSION,
        "corpus_hash": digest(manifest),
        "source_sha256": _sha(labels),
        "rows": rows,
    }
    _json(out, result)
    return {
        "judgments": len(rows),
        "human_judgments": sum(r["annotator_type"] == "human" for r in rows),
        "fingerprinted_judgments": sum(
            bool(r.get("query_input_hash") and r.get("node_input_hash")) for r in rows
        ),
    }


def import_node_labels(root: Path, labels: Path, out: Path) -> dict[str, Any]:
    """Validate complete fixed-taxonomy labels against independently sampled nodes."""
    manifest, _, _ = load_frozen(root)
    rows = validate_node_labels(root, read_records(labels))
    _json(
        out,
        {
            "version": VERSION,
            "corpus_hash": digest(manifest),
            "source_sha256": _sha(labels),
            "rows": rows,
        },
    )
    return {
        "nodes": len({r["node_uuid"] for r in rows}),
        "annotations": len(rows),
        "human_annotations": sum(r["annotator_type"] == "human" for r in rows),
    }


def validate_node_labels(root: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Revalidate complete imported annotation records at every scoring boundary."""
    expected = {n["node_uuid"]: n for n in read_records(root / "classification-review.json")}
    topics = set(json.loads((root / "taxonomy.json").read_text()))
    seen = set()
    for row in rows:
        uid = row.get("node_uuid")
        identity = (uid, row.get("annotator"))
        if uid not in expected or identity in seen:
            raise ValueError("unknown node or duplicate annotation")
        seen.add(identity)
        if row.get("input_hash") != expected[uid]["input_hash"]:
            raise ValueError("classification label input hash changed")
        if (
            not isinstance(row.get("annotator"), str)
            or not row["annotator"].strip()
            or row.get("annotator_type") not in ("human", "model")
            or row.get("adjudication") not in ("reviewed", "adjudicated")
        ):
            raise ValueError("node labels need named reviewed/adjudicated annotators")
        if row.get("rubric_version") != TOPIC_RUBRIC:
            raise ValueError("node labels require the frozen topic rubric")
        if set(row.get("topics", {})) != topics:
            raise ValueError("label every topic, including explicit insufficient evidence")
        for label in row["topics"].values():
            if (
                not isinstance(label, dict)
                or label.get("label") not in ("present", "absent", "insufficient_evidence")
                or not isinstance(label.get("evidence"), str)
                or not label["evidence"].strip()
            ):
                raise ValueError(
                    "topic labels need a valid label and supporting evidence/rationale"
                )
    return rows


def classification_report(
    root: Path,
    predictions: Path,
    labels: Path,
    out: Path,
    *,
    split: str = "test",
    backend: str = "jev",
    model: str = "",
    recipe_path: Path | None = None,
) -> dict[str, Any]:
    """Score accepted memberships independently from stored free-form tag mapping."""
    from hippo_brain.bench.knowledge_tags import rules
    from hippo_brain.classification import load_recipe

    manifest, _, nodes = load_frozen(root)
    eligible = {
        n["node_uuid"]: n
        for n in read_records(root / "classification-review.json")
        if n["split"] == split
    }
    taxonomy = json.loads((root / "taxonomy.json").read_text())
    recipe = load_recipe(recipe_path)
    if taxonomy != recipe.topics or backend not in ("jev", "local", "rules", "stored"):
        raise ValueError("classification taxonomy or backend differs from the frozen recipe")
    expected_model = (
        recipe.model_id if backend == "jev" else model if backend == "local" else backend
    )
    if not expected_model:
        raise ValueError("local comparator reporting requires its pinned model")
    label_data = json.loads(labels.read_text())
    if label_data.get("corpus_hash") != digest(manifest):
        raise ValueError("node labels belong to another corpus")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in validate_node_labels(root, label_data["rows"]):
        if row["node_uuid"] in eligible:
            grouped[row["node_uuid"]].append(row)
    resolved = {}
    human, dual_nodes = set(), set()
    for uid, votes in grouped.items():
        final = [
            r
            for r in votes
            if r["annotator_type"] == "human" and r["adjudication"] == "adjudicated"
        ] or votes
        for topic in taxonomy:
            if len({r["topics"][topic]["label"] for r in final}) != 1:
                raise ValueError("conflicting node labels require human adjudication")
        resolved[uid] = final[0]["topics"]
        if any(r["annotator_type"] == "human" for r in final):
            human.add(uid)
        if len({r["annotator"] for r in votes if r["annotator_type"] == "human"}) >= 2:
            dual_nodes.add(uid)
    predicted = {}
    for row in read_records(predictions):
        uid = row["node_uuid"]
        if uid not in nodes or uid in predicted:
            raise ValueError("unknown or duplicate classification prediction")
        if row.get("input_hash") != nodes[uid]["input_hash"]:
            raise ValueError("prediction does not match frozen classifier input")
        if (
            row.get("backend") != backend
            or row.get("model") != expected_model
            or row.get("recipe_hash") != recipe.recipe_hash
        ):
            raise ValueError("prediction model/backend/recipe does not match the declared arm")
        if row.get("status") not in ("ok", "error"):
            raise ValueError("classification prediction needs a terminal status")
        predicted[uid] = row
        if row["status"] != "ok":
            continue
        accepted = row.get("accepted_topics", [])
        probabilities = row.get("probabilities", {})
        if (
            len(set(accepted)) != len(accepted)
            or set(accepted) - taxonomy.keys()
            or len(accepted) > 5
        ):
            raise ValueError("invalid accepted-topic membership")
        if set(probabilities) != set(taxonomy) or any(
            type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1
            for p in probabilities.values()
        ):
            raise ValueError("classification needs complete finite probabilities")
        allowed = set(recipe.topics if recipe.publish_topics is None else recipe.publish_topics)
        expected_topics = sorted(
            (
                t
                for t, p in probabilities.items()
                if t in allowed and p >= recipe.thresholds.get(t, 0.8)
            ),
            key=lambda t: (-(probabilities[t] - recipe.thresholds.get(t, 0.8)), t),
        )[: recipe.max_topics]
        if accepted != expected_topics:
            raise ValueError("accepted topics differ from frozen thresholds/allowlist/cap")
    counts = {t: Counter() for t in taxonomy}
    existing_counts = Counter()
    cluster_counts: dict[str, Counter] = defaultdict(Counter)
    covered = 0
    with closing(open_readonly(root / "baseline.sqlite")) as conn:
        for uid, truth in resolved.items():
            if uid not in predicted or predicted[uid]["status"] != "ok":
                continue
            accepted = set(predicted[uid]["accepted_topics"])
            covered += bool(accepted)
            raw = conn.execute("SELECT tags FROM knowledge_nodes WHERE uuid=?", (uid,)).fetchone()[
                0
            ]
            existing = json.loads(raw or "[]")
            existing = {t for t, matched in rules(" ".join(existing), taxonomy).items() if matched}
            for topic, label in truth.items():
                state = label["label"]
                if state == "insufficient_evidence":
                    counts[topic]["unknown"] += 1
                    counts[topic]["accepted_unknown"] += topic in accepted
                    continue
                present = state == "present"
                counts[topic]["positive" if present else "negative"] += 1
                category = (
                    "tp"
                    if present and topic in accepted
                    else "fn"
                    if present
                    else "fp"
                    if topic in accepted
                    else "tn"
                )
                counts[topic][category] += 1
                cluster_counts[nodes[uid]["task_family_id"]][category] += 1
                if present:
                    existing_counts["tp" if topic in existing else "fn"] += 1
    totals = sum(counts.values(), Counter())

    def ratio(num: int, den: int) -> float | None:
        return num / den if den else None

    per_topic = {
        topic: {
            **count,
            "precision": ratio(count["tp"], count["tp"] + count["fp"]),
            "recall": ratio(count["tp"], count["tp"] + count["fn"]),
            "supported": count["positive"] >= 50 and count["negative"] >= 100,
        }
        for topic, count in counts.items()
    }
    precision = ratio(totals["tp"], totals["tp"] + totals["fp"])
    recall = ratio(totals["tp"], totals["tp"] + totals["fn"])
    baseline_recall = ratio(existing_counts["tp"], existing_counts["tp"] + existing_counts["fn"])
    # Ratio bootstrap samples families as clusters rather than treating correlated
    # labels from the same node or task as independent Bernoulli observations.
    import random

    rng = random.Random(20260922)
    clusters = list(cluster_counts.values())
    bootstrap = []
    if len(clusters) >= 2:
        for _ in range(2000):
            sampled = sum(rng.choices(clusters, k=len(clusters)), Counter())
            value = ratio(sampled["tp"], sampled["tp"] + sampled["fp"])
            if value is not None:
                bootstrap.append(value)
    lower = _percentile(bootstrap, 0.025) if len(bootstrap) == 2000 else None
    supported = [t for t in per_topic.values() if t["supported"]]
    coverage = covered / len(eligible) if eligible else 0.0
    checks = {
        "at_least_3000_sampled_nodes": manifest["classification_nodes"] >= 3000,
        "complete_heldout_labels": set(resolved) == set(eligible) and bool(eligible),
        "complete_predictions": set(eligible) <= predicted.keys()
        and all(predicted[uid]["status"] == "ok" for uid in eligible if uid in predicted),
        "human_labels": set(eligible) <= human,
        "double_review_20_percent": bool(eligible)
        and len(dual_nodes) >= math.ceil(len(eligible) * 0.2),
        "no_accepted_unknown": totals["accepted_unknown"] == 0,
        "micro_precision": precision is not None and precision >= 0.9,
        "precision_lower_bound": lower is not None and lower >= 0.85,
        "supported_topic_precision": bool(supported)
        and all(t["precision"] is not None and t["precision"] >= 0.85 for t in supported),
        "recall_gain": recall is not None
        and baseline_recall is not None
        and recall - baseline_recall >= 0.05,
        "node_coverage": coverage >= 0.4,
        "locked_test": split == "test",
        "family_audit": manifest["family_audit_human"] is True,
        "publication_backend": backend == "jev",
    }
    result = {
        "corpus_hash": digest(manifest),
        "predictions_sha256": _sha(predictions),
        "labels_sha256": _sha(labels),
        "split": split,
        "backend": backend,
        "model": expected_model,
        "eligible_nodes": len(eligible),
        "labeled_nodes": len(resolved),
        "node_coverage": coverage,
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_precision_lower_95": lower,
        "stored_tags_mapped_recall": baseline_recall,
        "per_topic": per_topic,
        "publishable_topics": [
            t
            for t, c in per_topic.items()
            if c["supported"] and c["precision"] is not None and c["precision"] >= 0.85
        ]
        if all(checks.values())
        else [],
        "checks": checks,
        "passed": all(checks.values()),
        "scope": "new controlled memberships; stored-tag mapping is not a fresh local-LLM classifier",
        "connection_gate": "requires independent exact-topic pair judgments; not inferred from label precision",
    }
    _json(out, result)
    return result


def _connection_sample(
    nodes: Mapping[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    taxonomy: Mapping[str, Any],
    *,
    limit: int,
    split: str,
    seed: int,
) -> list[dict[str, str]]:
    if type(limit) is not int or limit < 1 or split not in SPLITS or type(seed) is not int:
        raise ValueError(
            "connection sample requires a positive limit, known split, and integer seed"
        )
    topics: dict[str, list[str]] = defaultdict(list)
    seen = set()
    for row in rows:
        uid = row["node_uuid"]
        if uid not in nodes or uid in seen or row.get("input_hash") != nodes[uid]["input_hash"]:
            raise ValueError("connection prediction node identity is invalid")
        seen.add(uid)
        if row.get("status") != "ok" or not nodes[uid]["eligible"] or nodes[uid]["split"] != split:
            continue
        accepted = row.get("accepted_topics")
        if not isinstance(accepted, list) or any(topic not in taxonomy for topic in accepted):
            raise ValueError("connection predictions require known accepted topics")
        for topic in set(accepted):
            topics[topic].append(uid)
    pairs, used = [], set()
    for topic, uuids in sorted(topics.items(), key=lambda item: digest([seed, item[0]])):
        available = sorted(
            (uid for uid in uuids if uid not in used),
            key=lambda uid: digest([seed, "connection", topic, uid]),
        )
        for endpoints in zip(available[::2], available[1::2]):
            if len(pairs) >= limit:
                return pairs
            a, b = sorted(endpoints)
            used.update((a, b))
            pairs.append(
                {
                    "node_a": a,
                    "node_b": b,
                    "topic_id": topic,
                    "input_hash_a": nodes[a]["input_hash"],
                    "input_hash_b": nodes[b]["input_hash"],
                }
            )
    return pairs


def connection_review(
    root: Path,
    predictions: Path,
    out: Path,
    *,
    limit: int = 200,
    split: str = "test",
    seed: int = 20260922,
    plan: Path | None = None,
) -> dict[str, Any]:
    """Freeze disjoint eligible pairs before annotating their exact connecting topic."""
    manifest, _, nodes = load_frozen(root)
    plan = external_path(plan or out.with_suffix(".plan.json"))
    out = external_path(out)
    if plan == out:
        raise ValueError("connection sampling plan and labels must be separate files")
    if plan.exists() or out.exists():
        raise FileExistsError("connection sampling plan or labels already exist")
    pairs = _connection_sample(
        nodes,
        read_records(predictions),
        json.loads((root / "taxonomy.json").read_text()),
        limit=limit,
        split=split,
        seed=seed,
    )
    sampling_plan = {
        "version": VERSION,
        "corpus_hash": digest(manifest),
        "predictions_sha256": _sha(predictions),
        "sampling": "deterministic disjoint eligible endpoint pairs, selected before human labels",
        "split": split,
        "seed": seed,
        "limit": limit,
        "expected_pairs": len(pairs),
        "pairs": pairs,
    }
    labels = []
    with closing(open_readonly(root / "baseline.sqlite")) as conn:
        for pair in pairs:
            excerpts = [
                redact(
                    conn.execute(
                        "SELECT content FROM knowledge_nodes WHERE uuid=?", (pair[key],)
                    ).fetchone()[0]
                )[:6000]
                for key in ("node_a", "node_b")
            ]
            labels.append(
                {
                    **pair,
                    "source_a": excerpts[0],
                    "source_b": excerpts[1],
                    "membership_a": None,
                    "membership_b": None,
                    "evidence_a": "",
                    "evidence_b": "",
                    "useful": None,
                    "discovery_task": "",
                    "annotator": "",
                    "annotator_type": "",
                    "adjudication": "pending",
                }
            )
    _json(plan, sampling_plan)
    _json(
        out,
        {
            "corpus_hash": digest(manifest),
            "predictions_sha256": _sha(predictions),
            "plan_sha256": _sha(plan),
            "rows": labels,
        },
    )
    return {
        "sampled_pairs": len(pairs),
        "unique_endpoints": len(pairs) * 2,
        "human_labels": 0,
        "split": split,
        "plan": str(plan),
    }


def connection_report(
    root: Path, predictions: Path, pairs: Path, out: Path, *, plan: Path | None = None
) -> dict[str, Any]:
    """Score the exact connecting topic and separately report discovery usefulness."""
    import random

    manifest, _, nodes = load_frozen(root)
    evidence = json.loads(pairs.read_text())
    if evidence.get("corpus_hash") != digest(manifest) or evidence.get(
        "predictions_sha256"
    ) != _sha(predictions):
        raise ValueError("connection labels do not match frozen corpus/predictions")
    prediction_rows = read_records(predictions)
    predicted = {r["node_uuid"]: r for r in prediction_rows}
    if len(predicted) != len(prediction_rows):
        raise ValueError("duplicate connection predictions")
    taxonomy = json.loads((root / "taxonomy.json").read_text())
    expected_pairs = None
    if plan is not None:
        sampling_plan = json.loads(plan.read_text())
        if (
            sampling_plan.get("version") != VERSION
            or sampling_plan.get("corpus_hash") != digest(manifest)
            or sampling_plan.get("predictions_sha256") != _sha(predictions)
            or evidence.get("plan_sha256") != _sha(plan)
        ):
            raise ValueError(
                "connection sampling plan does not match corpus, predictions, or labels"
            )
        expected_pairs = _connection_sample(
            nodes,
            prediction_rows,
            taxonomy,
            limit=sampling_plan.get("limit"),
            split=sampling_plan.get("split"),
            seed=sampling_plan.get("seed"),
        )
        if sampling_plan.get("pairs") != expected_pairs or sampling_plan.get(
            "expected_pairs"
        ) != len(expected_pairs):
            raise ValueError("connection sampling plan differs from deterministic frozen selection")
        expected = {
            (p["node_a"], p["node_b"], p["topic_id"], p["input_hash_a"], p["input_hash_b"])
            for p in expected_pairs
        }
        actual = set()
        for row in evidence["rows"]:
            a, b = sorted((row["node_a"], row["node_b"]))
            hashes = dict(
                zip(
                    (row["node_a"], row["node_b"]),
                    (row.get("input_hash_a"), row.get("input_hash_b")),
                )
            )
            actual.add((a, b, row["topic_id"], hashes[a], hashes[b]))
        if actual != expected or len(evidence["rows"]) != len(expected):
            raise ValueError("connection labels must cover every planned pair exactly once")
    parents: dict[str, str] = {}

    def find(key: str) -> str:
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    scored, seen = [], set()
    for row in evidence["rows"]:
        a, b, topic = row["node_a"], row["node_b"], row["topic_id"]
        identity = (*sorted((a, b)), topic)
        if (
            a == b
            or identity in seen
            or a not in nodes
            or b not in nodes
            or not nodes[a]["eligible"]
            or not nodes[b]["eligible"]
            or topic not in taxonomy
        ):
            raise ValueError("invalid or duplicate exact-topic edge")
        seen.add(identity)
        for suffix, uid in (("a", a), ("b", b)):
            prediction = predicted.get(uid, {})
            if (
                prediction.get("status") != "ok"
                or prediction.get("input_hash") != nodes[uid]["input_hash"]
                or topic not in prediction.get("accepted_topics", [])
                or row.get("input_hash_" + suffix) != nodes[uid]["input_hash"]
            ):
                raise ValueError("edge is not supported by current predicted topic membership")
            if (
                row.get("membership_" + suffix)
                not in ("present", "absent", "insufficient_evidence")
                or not isinstance(row.get("evidence_" + suffix), str)
                or not row["evidence_" + suffix].strip()
            ):
                raise ValueError(
                    "each endpoint requires an exact-topic human judgment and evidence"
                )
        if (
            not isinstance(row.get("annotator"), str)
            or not row["annotator"].strip()
            or row.get("annotator_type") not in ("human", "model")
            or row.get("adjudication") not in ("reviewed", "adjudicated")
        ):
            raise ValueError("connection requires explicit reviewed annotation provenance")
        if row.get("useful") is not None and (
            type(row["useful"]) is not bool
            or not isinstance(row.get("discovery_task"), str)
            or not row["discovery_task"].strip()
        ):
            raise ValueError("discovery usefulness requires a boolean and a named task")
        fa, fb = find(nodes[a]["task_family_id"]), find(nodes[b]["task_family_id"])
        parents[max(fa, fb)] = min(fa, fb)
        unknown = "insufficient_evidence" in (row["membership_a"], row["membership_b"])
        scored.append(
            {
                "family": fa,
                "correct": row["membership_a"] == row["membership_b"] == "present",
                "unknown": unknown,
                "human": row["annotator_type"] == "human",
                "useful": row.get("useful"),
                "heldout": nodes[a]["split"] == nodes[b]["split"] == "test",
            }
        )
    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in scored:
        if not row["unknown"]:
            group = groups[find(row["family"])]
            group[0] += row["correct"]
            group[1] += 1
    numerator = sum(g[0] for g in groups.values())
    denominator = sum(g[1] for g in groups.values())
    precision = numerator / denominator if denominator else None
    lower = None
    if len(groups) >= 2 and precision is not None:
        rng = random.Random(20260922)
        values = list(groups.values())
        samples = []
        for _ in range(2000):
            draw = rng.choices(values, k=len(values))
            samples.append(sum(g[0] for g in draw) / sum(g[1] for g in draw))
        # A perfect tiny sample must not produce an unwarranted zero-width CI.
        effective_n = denominator**2 / sum(g[1] ** 2 for g in values)
        z = 1.959963984540054
        wilson = (
            precision
            + z * z / (2 * effective_n)
            - z
            * math.sqrt(precision * (1 - precision) / effective_n + z * z / (4 * effective_n**2))
        ) / (1 + z * z / effective_n)
        lower = min(_percentile(samples, 0.025), wilson)
    useful = [r["useful"] for r in scored if r["useful"] is not None]
    checks = {
        "immutable_sampling_plan": expected_pairs is not None,
        "complete_endpoint_judgments": bool(scored) and not any(r["unknown"] for r in scored),
        "human_judgments": bool(scored) and all(r["human"] for r in scored),
        "heldout_pairs": bool(scored) and all(r["heldout"] for r in scored),
        "family_audit": manifest["family_audit_human"] is True,
        "exact_topic_precision_lower_bound": lower is not None and lower >= 0.85,
    }
    result = {
        "corpus_hash": digest(manifest),
        "predictions_sha256": _sha(predictions),
        "labels_sha256": _sha(pairs),
        "plan_sha256": _sha(plan) if plan is not None else None,
        "planned_pairs": len(expected_pairs) if expected_pairs is not None else None,
        "pairs": len(scored),
        "judged_pairs": denominator,
        "correct_exact_topic_links": numerator,
        "precision": precision,
        "lower_95": lower,
        "independent_family_components": len(groups),
        "interval_method": "task-family component bootstrap with conservative effective-sample Wilson lower safeguard",
        "discovery_task_judgments": len(useful),
        "discovery_task_success": sum(useful) / len(useful) if useful else None,
        "checks": checks,
        "passed": all(checks.values()),
    }
    _json(out, result)
    return result


def _resolved_judgments(
    rows: Iterable[Judgment],
) -> tuple[dict[str, dict[str, int]], set[str], set[str]]:
    grouped: dict[tuple[str, str], list[Judgment]] = defaultdict(list)
    for row in rows:
        grouped[(row["query_id"], row["node_uuid"])].append(row)
    grades: dict[str, dict[str, int]] = defaultdict(dict)
    nonhuman, single_review = set(), set()
    for (qid, uid), votes in grouped.items():
        adjudicated = [
            r
            for r in votes
            if r["adjudication"] == "adjudicated" and r["annotator_type"] == "human"
        ]
        final = adjudicated or votes
        if len({r["grade"] for r in final}) != 1:
            raise ValueError("disagreeing labels require a human adjudication")
        grades[qid][uid] = final[0]["grade"]
        humans = {r["annotator"] for r in votes if r["annotator_type"] == "human"}
        if not humans:
            nonhuman.add(qid)
        if len(humans) < 2:
            single_review.add(qid)
    return dict(grades), nonhuman, set(grades) - single_review


def _percentile(values: list[float], p: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def report(
    root: Path,
    rankings: Path,
    labels: Path,
    out: Path,
    *,
    candidate: str,
    local: str = "local",
    baseline: str = "retrieval",
    cutoff: int = 30,
    split: str = "test",
    adaptive: bool = False,
) -> dict[str, Any]:
    """Validate complete paired evidence, retaining every coverage failure denominator."""
    manifest, all_questions, nodes = load_frozen(root)
    questions = [q for q in all_questions if q["split"] == split]
    qmap = {q["id"]: q for q in questions}
    allmap = {q["id"]: q for q in all_questions}
    labels_data = json.loads(labels.read_text())
    if not isinstance(labels_data, dict) or labels_data.get("corpus_hash") != digest(manifest):
        raise ValueError("labels must be imported against this frozen corpus")
    validated = validate_judgments(labels_data["rows"], allmap, nodes)
    judgments, nonhuman, dual = _resolved_judgments(validated)
    if cutoff not in (30, 60) or len({baseline, local, candidate}) != 3:
        raise ValueError("use three distinct arms and a pool cutoff of 30 or 60")
    arms = {baseline, local, candidate}
    rows: dict[str, dict[str, dict[str, Any]]] = {arm: {} for arm in arms}
    reasons = []
    reliability_rows = []
    seen_rows = set()
    for row in read_records(rankings):
        qid, arm = row["query_id"], row["arm"]
        if qid not in allmap:
            raise ValueError("ranking query is not in frozen corpus")
        if arm not in arms:
            continue
        order = row["order"]
        if (qid, arm) in seen_rows or len(order) != len(set(order)) or len(order) > cutoff:
            raise ValueError("duplicate query or invalid ranking pool")
        seen_rows.add((qid, arm))
        if any(uid not in nodes for uid in order):
            raise ValueError("ranking references an unknown UUID")
        if (
            not row.get("model")
            or not row.get("recipe_hash")
            or row.get("input_hash") != digest(allmap[qid])
        ):
            raise ValueError("ranking must identify its frozen query, model, and recipe")
        if (
            type(row.get("elapsed_ms")) not in (int, float)
            or not math.isfinite(row["elapsed_ms"])
            or row["elapsed_ms"] < 0
        ):
            raise ValueError("ranking latency must be finite and nonnegative")
        if row.get("status") not in ("ok", "fallback", "error", "cancelled"):
            raise ValueError("ranking requires terminal status")
        if any(
            max(nodes[uid]["created_at_ms"], nodes[uid]["updated_at_ms"]) > allmap[qid]["as_of_ms"]
            for uid in order
        ):
            raise ValueError("ranking contains future source evidence")
        if arm == candidate:
            reliability_rows.append(row)
        if qid in qmap:
            rows[arm][qid] = row
    for arm in sorted(arms):
        if set(rows[arm]) != set(qmap):
            reasons.append(f"{arm}: missing {len(set(qmap) - set(rows[arm]))} query decisions")
        if len({(r["model"], r["recipe_hash"]) for r in rows[arm].values()}) > 1:
            reasons.append(f"{arm}: mixed model or recipe identities")
    quality_ids = [
        q["id"]
        for q in questions
        if q["source_eligible"] is not False
        and q["expected_answerability"] == "answerable"
        and q["node_available"]
        and q["as_of_reconstructable"]
    ]
    measured: dict[str, dict[str, dict[str, Any]]] = {arm: {} for arm in arms}
    for qid in quality_ids:
        union = {uid for arm in arms for uid in rows[arm].get(qid, {}).get("order", [])}
        union.update(qmap[qid]["relevant_knowledge_node_uuids"])
        if union - set(judgments.get(qid, {})):
            reasons.append(f"{qid}: pooled candidates lack judgments")
        for arm in arms:
            if qid in rows[arm]:
                measured[arm][qid] = graded_retrieval_metrics(
                    rows[arm][qid]["order"], judgments.get(qid, {}), cutoff=cutoff
                )
    paired = {}
    families = {q["id"]: q["task_family_id"] for q in questions}
    for comparator in (baseline, local):
        for metric in ("ndcg_at_5", "hit_at_1", "hit_at_5", "candidate_recall"):
            available = {
                qid
                for qid in quality_ids
                if measured[candidate].get(qid, {}).get(metric) is not None
                and measured[comparator].get(qid, {}).get(metric) is not None
            }
            paired[f"{candidate}_minus_{comparator}/{metric}"] = paired_family_interval(
                {qid: float(measured[candidate][qid][metric]) for qid in available},
                {qid: float(measured[comparator][qid][metric]) for qid in available},
                families,
            )
            if metric == "ndcg_at_5" and len(available) != len(quality_ids):
                reasons.append(f"{comparator}: undefined/incomplete paired nDCG")
    latencies = [r["elapsed_ms"] for r in rows[candidate].values()]
    performance = {
        "sample_count": len(latencies),
        "p50_ms": _percentile(latencies, 0.5),
        "p95_ms": _percentile(latencies, 0.95),
        "p99_ms": _percentile(latencies, 0.99),
    }
    errors = sum(r["status"] != "ok" for r in reliability_rows)
    slices = {}
    for dimension in ("source_bias", "intent"):
        for value in sorted({str(q[dimension]) for q in questions}):
            ids = [
                q["id"] for q in questions if str(q[dimension]) == value and q["id"] in quality_ids
            ]
            deltas = [
                measured[candidate][qid]["hit_at_5"] - measured[local][qid]["hit_at_5"]
                for qid in ids
                if measured[candidate].get(qid, {}).get("hit_at_5") is not None
                and measured[local].get(qid, {}).get("hit_at_5") is not None
            ]
            slices[f"{dimension}:{value}"] = {
                "questions": len(ids),
                "judged_pairs": len(deltas),
                "qualified": len(ids) >= 30 and len(deltas) == len(ids),
                "hit_at_5_difference": statistics.mean(deltas) if deltas else None,
            }
    total_questions = len(all_questions)
    corpus_checks = {
        "at_least_1000_questions": total_questions >= 1000,
        "at_least_10000_nodes": manifest["node_count"] >= 10000,
        "at_least_300_test_questions": len(questions) >= 300,
        "at_least_400_captured_questions": sum(
            q["origin"] in CAPTURE_ORIGINS for q in all_questions
        )
        >= 400,
        "at_least_100_verified_unanswerable": sum(
            q["expected_answerability"] == "unanswerable"
            and bool(q.get("unanswerable_evidence_audit"))
            for q in all_questions
        )
        >= 100,
        "at_least_200_difficult_questions": sum(
            "difficult" in q.get("slices", []) for q in all_questions
        )
        >= 200,
        "at_least_100_multinode_questions": sum(
            "multi_node" in q.get("slices", []) for q in all_questions
        )
        >= 100,
        "at_least_100_cross_source_questions": sum(
            "cross_source" in q.get("slices", []) for q in all_questions
        )
        >= 100,
        "human_reviewed_questions": all(
            q.get("annotation_status") == "human_reviewed" for q in questions
        ),
        "test_labels_human": not (set(quality_ids) & nonhuman)
        and all(qid in judgments for qid in quality_ids),
        "judgments_bound_to_frozen_inputs": bool(quality_ids)
        and all(
            r.get("query_input_hash") and r.get("node_input_hash")
            for r in validated
            if r["query_id"] in quality_ids
        ),
        "double_review_20_percent": len(set(quality_ids) & dual)
        >= math.ceil(len(quality_ids) * 0.2)
        and bool(quality_ids),
        "family_audit": manifest["family_audit_human"] is True,
        "locked_test": split == "test",
    }

    def lower(comparator: str, metric: str) -> float:
        value = paired[f"{candidate}_minus_{comparator}/{metric}"]["lower"]
        return float(value) if value is not None else -math.inf

    local_hit = paired[f"{candidate}_minus_{local}/hit_at_1"]["mean"]
    checks = {
        **corpus_checks,
        "complete_evidence": not reasons,
        "ndcg_vs_retrieval": lower(baseline, "ndcg_at_5") >= 0,
        "ndcg_vs_local": lower(local, "ndcg_at_5") >= -0.05,
        "hit_at_1_vs_local": local_hit is not None and local_hit >= -0.05,
        "slice_regressions": all(
            s["hit_at_5_difference"] >= -0.1 for s in slices.values() if s["qualified"]
        ),
        "latency": performance["p95_ms"] is not None
        and performance["p95_ms"] <= (1500 if adaptive else 500),
        "decision_deadline": bool(latencies) and max(latencies) <= 2000,
        "reliability": len(reliability_rows) >= 1000 and errors / len(reliability_rows) <= 0.01,
        "source_policy": bool(reliability_rows)
        and all(
            r.get("filter_violations") == 0 and r.get("malformed_accepted") == 0
            for r in reliability_rows
        ),
    }
    eligible_answerable = [
        q["id"]
        for q in questions
        if q["source_eligible"] is True
        and q["expected_answerability"] == "answerable"
        and q["as_of_reconstructable"]
    ]
    end_to_end = {}
    for arm in arms:
        scores = [
            0.0 if not qmap[qid]["node_available"] else measured[arm].get(qid, {}).get("hit_at_5")
            for qid in eligible_answerable
        ]
        end_to_end[arm] = {
            "denominator": len(scores),
            "unknown": sum(v is None for v in scores),
            "hit_at_5": statistics.mean(scores)
            if scores and all(v is not None for v in scores)
            else None,
        }
    result = {
        "version": VERSION,
        "corpus_hash": digest(manifest),
        "rankings_sha256": _sha(rankings),
        "labels_sha256": _sha(labels),
        "scope": "fully judged pooled union, not exhaustive database relevance",
        "candidate": candidate,
        "split": split,
        "cutoff": cutoff,
        "denominators": {
            "questions": len(questions),
            "source_captured": sum(q["source_captured"] for q in questions),
            "eligible": sum(q["source_eligible"] is True for q in questions),
            "source_linked": sum(q["source_linked"] for q in questions),
            "node_available": sum(q["node_available"] for q in questions),
            "quality_questions": len(quality_ids),
            "answerable_missing_node": sum(
                q["expected_answerability"] == "answerable"
                and q["source_eligible"] is True
                and not q["node_available"]
                for q in questions
            ),
            "unanswerable": sum(q["expected_answerability"] == "unanswerable" for q in questions),
            "as_of_unscorable": sum(not q["as_of_reconstructable"] for q in questions),
        },
        "coverage_gaps": dict(
            Counter(q["coverage_gap_reason"] for q in questions if q["coverage_gap_reason"])
        ),
        "metrics": measured,
        "paired": paired,
        "slices": slices,
        "performance": performance,
        "end_to_end": end_to_end,
        "reliability_decisions": len(reliability_rows),
        "errors_or_fallbacks": errors,
        "checks": checks,
        "incomplete_reasons": reasons,
        "passed": all(checks.values()),
        "failed_checks": [name for name, passed in checks.items() if not passed],
    }
    # A widened discovery pool is a diagnostic, not evidence of recall@30 gain.
    discovery_metrics = {
        arm: {
            qid: graded_retrieval_metrics(
                rows[arm][qid]["order"][:30], judgments.get(qid, {}), cutoff=30
            )
            for qid in quality_ids
            if qid in rows[arm]
        }
        for arm in (baseline, candidate)
    }
    discovery_paired = {}
    for metric in ("candidate_recall", "ndcg_at_5"):
        available = {
            qid
            for qid in quality_ids
            if discovery_metrics[candidate].get(qid, {}).get(metric) is not None
            and discovery_metrics[baseline].get(qid, {}).get(metric) is not None
        }
        discovery_paired[metric] = paired_family_interval(
            {qid: float(discovery_metrics[candidate][qid][metric]) for qid in available},
            {qid: float(discovery_metrics[baseline][qid][metric]) for qid in available},
            families,
        )
    unmatched = [
        qid
        for qid in quality_ids
        if qid not in rows[baseline]
        or qid not in rows[candidate]
        or not (len(rows[baseline][qid]["order"]) == len(rows[candidate][qid]["order"]) <= 30)
    ]
    result["candidate_discovery"] = {
        "cutoff": 30,
        "metrics": discovery_metrics,
        "paired": discovery_paired,
        "unmatched_pool_queries": unmatched,
        "widened_pool_diagnostic": {
            "cutoff": cutoff,
            "candidate_recall_difference": paired[f"{candidate}_minus_{baseline}/candidate_recall"],
            "qualifies_for_production_gate": False,
        }
        if cutoff == 60
        else None,
    }
    recall_difference = discovery_paired["candidate_recall"]
    ndcg_lower = discovery_paired["ndcg_at_5"]["lower"]
    result["candidate_discovery_checks"] = {
        "matched_candidate_pools_at_most_30": bool(quality_ids) and not unmatched,
        "paired_recall_gain": recall_difference["mean"] is not None
        and recall_difference["mean"] >= 0.03,
        "positive_recall_lower_bound": recall_difference["lower"] is not None
        and recall_difference["lower"] > 0,
        "matched_size_ndcg_nonregression": ndcg_lower is not None and ndcg_lower >= 0,
        "complete_human_corpus": all(corpus_checks.values()) and not reasons,
    }
    result["candidate_discovery_passed"] = all(result["candidate_discovery_checks"].values())
    _json(out, result)
    _json(
        out.with_suffix(".completion.json"),
        {
            "report_sha256": _sha(out),
            "expected_queries": len(questions),
            "arms": {
                arm: {"rows": len(values), "complete": set(values) == set(qmap)}
                for arm, values in rows.items()
            },
            "passed": result["passed"],
        },
    )
    return result


def sweep(
    signals: Path, labels: Path, out: Path, *, weights: Sequence[Sequence[float]] | None = None
) -> dict[str, Any]:
    """Replay recorded, changed-evidence passes and weights without model calls.

    Input rows identify development query, family, candidate UUIDs and complete
    per-pass normalized relevance/evidence/scope scores. Pass budget exhaustion
    remains visible; repeating an unchanged state is rejected as refinement.
    """
    recipes = list(
        weights or ((0.5, 0.35, 0.15), (0.6, 0.3, 0.1), (0.4, 0.4, 0.2), (1.0, 0.0, 0.0))
    )
    for recipe in recipes:
        if (
            len(recipe) != 3
            or any(not math.isfinite(v) or v < 0 for v in recipe)
            or not math.isclose(sum(recipe), 1)
        ):
            raise ValueError(
                "each weight recipe needs three finite nonnegative weights summing to one"
            )
    grade_rows = read_records(labels)
    grades: dict[str, dict[str, int]] = defaultdict(dict)
    for row in grade_rows:
        if type(row.get("grade")) is not int or not 0 <= row["grade"] <= 3:
            raise ValueError("sweep requires reviewed graded labels")
        if row["node_uuid"] in grades[row["query_id"]]:
            raise ValueError("resolve multiple judgments before weight replay")
        grades[row["query_id"]][row["node_uuid"]] = row["grade"]
    results = []
    identities = set()
    for row in read_records(signals):
        qid = row["query_id"]
        if qid in identities or row.get("split") != "development":
            raise ValueError(
                "sweeps require unique development queries; holdout tuning is prohibited"
            )
        identities.add(qid)
        candidates = row["candidate_uuids"]
        if len(set(candidates)) != len(candidates):
            raise ValueError("duplicate candidate UUIDs")
        seen_evidence = set()
        passes = row["passes"]
        for assessment in passes:
            evidence_hash = assessment.get("evidence_hash")
            if not evidence_hash or evidence_hash in seen_evidence:
                raise ValueError("each refinement must identify changed evidence or questions")
            seen_evidence.add(evidence_hash)
            values = assessment["signals"]
            if set(values) != set(candidates) or any(
                len(v) != 3
                or any(
                    type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1
                    for x in v
                )
                for v in values.values()
            ):
                raise ValueError("passes must have complete finite normalized signals")
            if any(
                type(assessment.get(k)) not in (int, float)
                or not math.isfinite(assessment[k])
                or assessment[k] < 0
                for k in ("elapsed_ms", "requests")
            ):
                raise ValueError("record per-pass elapsed time and request count")
        for recipe in recipes:
            previous = None
            for budget in PASS_BUDGETS:
                count = min(budget, len(passes))
                if not count:
                    continue
                last = passes[count - 1]
                order = sorted(
                    candidates,
                    key=lambda uid: (
                        -sum(a * b for a, b in zip(recipe, last["signals"][uid], strict=True))
                    ),
                )
                metrics = graded_retrieval_metrics(
                    order, grades.get(qid, {}), cutoff=max(30, len(order))
                )
                score = metrics["ndcg_at_5"]
                usage = [p.get("usage") for p in passes[:count]]
                tokens = (
                    sum(u["input_tokens"] + u["output_tokens"] for u in usage)
                    if all(
                        isinstance(u, dict)
                        and type(u.get("input_tokens")) is int
                        and type(u.get("output_tokens")) is int
                        for u in usage
                    )
                    else None
                )
                results.append(
                    {
                        "query_id": qid,
                        "weights": list(recipe),
                        "pass_budget": budget,
                        "passes_available": len(passes),
                        "passes_used": count,
                        "budget_reached": count == budget,
                        "stop_reason": "budget"
                        if count == budget
                        else "recorded_evidence_exhausted",
                        "order": order,
                        "metrics": metrics,
                        "marginal_ndcg": score - previous
                        if score is not None and previous is not None
                        else None,
                        "elapsed_ms": sum(p["elapsed_ms"] for p in passes[:count]),
                        "requests": sum(p["requests"] for p in passes[:count]),
                        "tokens": tokens,
                        "usage_missing_reason": None
                        if tokens is not None
                        else "one_or_more_recorded_passes_missing_usage",
                    }
                )
                previous = score
    result = {
        "version": VERSION,
        "signals_sha256": _sha(signals),
        "labels_sha256": _sha(labels),
        "mode": "quality_only_recorded_signal_replay",
        "fresh_inference_calls": 0,
        "pass_budgets": list(PASS_BUDGETS),
        "rows": results,
    }
    _json(out, result)
    return {"queries": len(identities), "variants": len(results), "fresh_inference_calls": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--source", type=Path, required=True)
    prep.add_argument("--out", type=Path, default=None)
    prep.add_argument("--questions", type=Path)
    prep.add_argument("--captures", type=Path)
    prep.add_argument("--query-target", type=int, default=1000)
    prep.add_argument("--node-target", type=int, default=3000)
    for name in (
        "pool",
        "import-labels",
        "import-node-labels",
        "validate",
        "report",
        "classification-report",
        "finalize",
        "review-package",
        "connection-review",
        "connection-report",
    ):
        cmd = commands.add_parser(name)
        cmd.add_argument("root", type=Path)
        if name != "validate":
            cmd.add_argument("--out", type=Path, required=True)
        if name in ("pool", "report"):
            cmd.add_argument("--rankings", type=Path, required=True)
            cmd.add_argument("--cutoff", type=int, choices=(30, 60), default=30)
        if name in ("import-labels", "import-node-labels", "report", "classification-report"):
            cmd.add_argument("--labels", type=Path, required=True)
        if name == "finalize":
            cmd.add_argument("--questions", type=Path, required=True)
            cmd.add_argument("--family-auditor", required=True)
        if name == "review-package":
            cmd.add_argument("--targets", type=Path)
        if name in ("connection-review", "connection-report"):
            cmd.add_argument("--predictions", type=Path, required=True)
            cmd.add_argument("--plan", type=Path)
        if name == "connection-review":
            cmd.add_argument("--limit", type=int, default=200)
            cmd.add_argument("--split", choices=SPLITS, default="test")
            cmd.add_argument("--seed", type=int, default=20260922)
        if name == "connection-report":
            cmd.add_argument("--pairs", type=Path, required=True)
        if name == "classification-report":
            cmd.add_argument("--recipe", type=Path)
            cmd.add_argument("--predictions", type=Path, required=True)
            cmd.add_argument("--split", choices=SPLITS, default="test")
            cmd.add_argument(
                "--backend", choices=("jev", "local", "rules", "stored"), default="jev"
            )
            cmd.add_argument("--model", default="")
        if name == "report":
            cmd.add_argument("--candidate", required=True)
            cmd.add_argument("--local", default="local")
            cmd.add_argument("--baseline", default="retrieval")
            cmd.add_argument("--split", choices=SPLITS, default="test")
            cmd.add_argument("--adaptive", action="store_true")
    replay = commands.add_parser("sweep")
    replay.add_argument("--signals", type=Path, required=True)
    replay.add_argument("--labels", type=Path, required=True)
    replay.add_argument("--weights", type=Path)
    replay.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        out = args.out or hippo_bench_root() / "decisions" / f"corpus-{time.time_ns() // 1_000_000}"
        result = prepare(
            args.source,
            out,
            questions=args.questions,
            captures=args.captures,
            query_target=args.query_target,
            node_target=args.node_target,
        )
        result = {
            "output": str(out.resolve()),
            "actual_questions": result["actual_questions"],
            "authoring_slots": result["authoring_slots"],
            "classification_nodes": result["classification_nodes"],
            "review_status": result["review_status"],
        }
    elif args.command == "pool":
        result = pool(args.root, args.rankings, args.out, cutoff=args.cutoff)
    elif args.command == "finalize":
        result = finalize(args.root, args.questions, args.out, family_auditor=args.family_auditor)
    elif args.command == "review-package":
        result = review_package(args.root, args.out, targets=args.targets)
    elif args.command == "connection-review":
        result = connection_review(
            args.root,
            args.predictions,
            args.out,
            limit=args.limit,
            split=args.split,
            seed=args.seed,
            plan=args.plan,
        )
    elif args.command == "connection-report":
        result = connection_report(
            args.root, args.predictions, args.pairs, args.out, plan=args.plan
        )
    elif args.command == "import-labels":
        result = import_labels(args.root, args.labels, args.out)
    elif args.command == "import-node-labels":
        result = import_node_labels(args.root, args.labels, args.out)
    elif args.command == "classification-report":
        result = classification_report(
            args.root,
            args.predictions,
            args.labels,
            args.out,
            split=args.split,
            backend=args.backend,
            model=args.model,
            recipe_path=args.recipe,
        )
    elif args.command == "validate":
        manifest, questions, nodes = load_frozen(args.root)
        if _sha(args.root / "baseline.sqlite") != manifest["snapshot_sha256"]:
            raise ValueError("frozen database changed")
        result = {
            "integrity_valid": True,
            "questions": len(questions),
            "nodes": len(nodes),
            "human_review_complete": False,
            "review_status": manifest["review_status"],
        }
    elif args.command == "report":
        result = report(
            args.root,
            args.rankings,
            args.labels,
            args.out,
            candidate=args.candidate,
            local=args.local,
            baseline=args.baseline,
            cutoff=args.cutoff,
            split=args.split,
            adaptive=args.adaptive,
        )
        result = {
            "passed": result["passed"],
            "failed_checks": result["failed_checks"],
            "denominators": result["denominators"],
        }
    else:
        result = sweep(
            args.signals,
            args.labels,
            args.out,
            weights=json.loads(args.weights.read_text()) if args.weights else None,
        )
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
