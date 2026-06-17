# Obsidian Vault Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export the hippo knowledge base to a one-way Obsidian-compatible markdown vault where node↔entity and bounded node→node wikilinks are first-class edges.

**Architecture:** A Python renderer/reconciler (`hippo_brain.vault_export`) reads SQLite over one snapshot and writes a self-consistent vault via full reconcile (write-changed + orphan-GC). It is driven by a brain HTTP endpoint `POST /vault/export`. The Rust CLI `hippo export vault` and a launchd `com.hippo.vault-sync` service both POST that endpoint; tuning knobs ride in the request body, resolved from the `[vault]` config section.

**Tech Stack:** Python 3.14 (ruff, pytest, Starlette), Rust edition 2024 (clap, reqwest, rusqlite), SQLite (WAL), launchd.

**Spec:** `docs/superpowers/specs/2026-06-15-obsidian-vault-export-design.md`

---

## File Structure

**Python (brain) — core logic, fully unit-testable:**
- Create `brain/src/hippo_brain/vault_render.py` — slug derivation + markdown rendering (node note, entity page, index notes, derived headline, YAML frontmatter emit).
- Create `brain/src/hippo_brain/vault_edges.py` — rarity-weighted bounded `related[]` + entity member-node lists.
- Create `brain/src/hippo_brain/vault_export.py` — orchestrator `export_vault()`: query layer (probe filter), reconcile (atomic write-changed + orphan GC), safety rails (foreign-vault guard, `.gitignore`, banner, `_vault_meta.json`).
- Modify `brain/src/hippo_brain/server.py` — add `vault_export` handler + route.
- Create `brain/tests/test_vault_export.py` — all Python tests.

**Rust (daemon + CLI):**
- Modify `crates/hippo-core/src/config.rs` — add `VaultConfig` + `HippoConfig.vault` field + `default_*` fns.
- Modify `crates/hippo-daemon/src/cli.rs` — add `Export { action: ExportAction }` + `ExportAction::Vault`.
- Modify `crates/hippo-daemon/src/commands.rs` — add `handle_export_vault()`.
- Modify `crates/hippo-daemon/src/main.rs` — dispatch `Commands::Export`; add vault doctor check call.
- Modify `crates/hippo-daemon/src/install.rs` — render `__VAULT_POLL_INTERVAL_SECS__`, install/remove the new plist.

**Assets / docs:**
- Create `launchd/com.hippo.vault-sync.plist`.
- Modify `config/config.default.toml` — add `[vault]` section.
- Create `docs/vault-export.md`; modify `README.md` + `CLAUDE.md` (brief pointer).

**Format constant:** `VAULT_FORMAT_VERSION = 1` lives in `vault_export.py` and is written into `_vault_meta.json`.

---

## Phase 1 — Python core (rendering & edges)

### Task 1: Slug derivation

**Files:**
- Create: `brain/src/hippo_brain/vault_render.py`
- Test: `brain/tests/test_vault_export.py`

- [ ] **Step 1: Write the failing tests**

```python
# brain/tests/test_vault_export.py
from hippo_brain.vault_render import slugify, entity_slug, node_source_key


def test_slugify_strips_obsidian_reserved_chars():
    # concept canonicals are raw error text with [ ] : / " and #
    assert slugify('error[E0382]: borrow of moved value') == 'error-e0382-borrow-of-moved-value'
    assert slugify('crates/hippo-core/src/storage.rs') == 'crates-hippo-core-src-storage-rs'
    assert slugify('  Multiple   Spaces  ') == 'multiple-spaces'
    assert slugify('') == 'unnamed'


def test_entity_slug_prefers_canonical_falls_back_to_name_then_id():
    assert entity_slug('project', 'hippo', 'hippo', 42) == 'hippo'
    # NULL/empty canonical -> use name
    assert entity_slug('file', '/abs/path.rs', None, 7) == 'abs-path-rs'
    # both empty -> use id
    assert entity_slug('concept', '', '', 9) == 'entity-9'


def test_node_source_key_agentic_min_session_segment():
    # node links two agentic sessions; pick the min (session_id, segment_index)
    links = {
        'agentic': [('codex', 'zzz', 5), ('claude-code', 'aaa', 2), ('claude-code', 'aaa', 0)],
    }
    assert node_source_key(links, node_type='observation', uuid='u1') == 'claude-code-aaa-0'


def test_node_source_key_priority_and_change_outcome_discriminator():
    # workflow + agentic present -> agentic wins by priority; change_outcome gets -co suffix
    links = {'agentic': [('claude-code', 'aaa', 0)], 'workflow': [101]}
    assert node_source_key(links, node_type='change_outcome', uuid='u2') == 'claude-code-aaa-0-co'
    # only workflow
    assert node_source_key({'workflow': [101]}, node_type='change_outcome', uuid='u3') == 'wf-101-co'
    # no links at all -> uuid fallback
    assert node_source_key({}, node_type='observation', uuid='u4') == 'node-u4'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/carpenter/projects/hippo && brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hippo_brain.vault_render'`

> NOTE: run pytest via `brain/.venv/bin/python -m pytest`, **not** `uv run --project brain pytest`. In a worktree the latter resolves the main checkout's code (see project memory `worktree_uv_resolves_main_checkout`).

- [ ] **Step 3: Write minimal implementation**

```python
# brain/src/hippo_brain/vault_render.py
"""Render knowledge nodes and entities into Obsidian-compatible markdown.

This module is pure: it takes already-fetched rows and returns strings. All
SQLite access lives in vault_export.py. Filenames derive from a STABLE source
key, never the node uuid (re-enrichment re-mints uuids — see the design spec).
"""

from __future__ import annotations

import re

# Source priority when a node links several source types. Highest first.
_SOURCE_PRIORITY = ("agentic", "workflow", "browser", "shell", "lesson")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, replace any run of non-alphanumerics with '-', trim dashes."""
    s = _SLUG_STRIP.sub("-", text.strip().lower()).strip("-")
    return s or "unnamed"


def entity_slug(entity_type: str, name: str, canonical: str | None, entity_id: int) -> str:
    """Stable slug for an entity file: canonical, else name, else id."""
    base = (canonical or "").strip() or (name or "").strip()
    if not base:
        return f"entity-{entity_id}"
    return slugify(base)


def node_source_key(links: dict, node_type: str, uuid: str) -> str:
    """Derive a stable, content-independent filename slug for a knowledge node.

    ``links`` maps source kind -> list of stable identifiers:
      agentic  -> list of (harness, session_id, segment_index)
      workflow -> list of run_id (int)
      browser  -> list of browser_event_id (int)
      shell    -> list of event_id (int)
      lesson   -> list of lesson_id (int)
    """
    for kind in _SOURCE_PRIORITY:
        items = links.get(kind)
        if not items:
            continue
        if kind == "agentic":
            harness, session_id, segment = min(items, key=lambda t: (t[1], t[2]))
            base = f"{harness}-{session_id}-{segment}"
        else:
            prefix = {"workflow": "wf", "browser": "web", "shell": "evt", "lesson": "lesson"}[kind]
            base = f"{prefix}-{min(items)}"
        # change_outcome (CI) nodes can co-link the same agentic session as an
        # observation node; discriminate so they never collide on one filename.
        if node_type == "change_outcome":
            base += "-co"
        return base
    return f"node-{uuid}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint + commit**

```bash
cd /Users/carpenter/projects/hippo
uv run --project brain ruff check brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
uv run --project brain ruff format brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
git add brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
git commit -m "feat(vault): stable source-key slug derivation"
```

---

### Task 2: Node note rendering

**Files:**
- Modify: `brain/src/hippo_brain/vault_render.py`
- Test: `brain/tests/test_vault_export.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to brain/tests/test_vault_export.py
import yaml
from hippo_brain.vault_render import render_node_note, NodeRow


def _node(**kw):
    base = dict(
        uuid="8f3a", source_key="claude-code-aaa-0", node_type="observation",
        outcome="success", content_json='{"summary":"Fixed FTS trigger backfill in storage.rs. It works now.","intent":"debugging","key_decisions":["Chose porter tokenizer"],"problems_encountered":["FTS rows missing"],"design_decisions":[{"considered":"vec0 only","chosen":"FTS5 | hybrid","reason":"recall\\nmatters"}]}',
        embed_text="storage.rs knowledge_fts porter", tags=["hippo", "rust"],
        created_ms=1781530200000, updated_ms=1781530302000,
        entities=[("project", "hippo", "entities/project/hippo"),
                  ("file", "storage.rs", "entities/file/crates-hippo-core-src-storage-rs")],
        related=[("claude-code-bbb-1", "FTS trigger backfill")],
        sources=["agentic-session: claude-code/aaa#0"],
    )
    base.update(kw)
    return NodeRow(**base)


def test_render_node_note_frontmatter_round_trips_and_quotes_links():
    md = render_node_note(_node())
    assert md.startswith("<!-- GENERATED BY hippo export vault")
    fm = md.split("---\n")[1]
    data = yaml.safe_load(fm)  # must not raise
    assert data["uuid"] == "8f3a"
    assert data["node_type"] == "observation"
    assert data["related"] == ["[[claude-code-bbb-1|FTS trigger backfill]]"]
    assert data["tags"] == ["hippo", "rust"]
    # ISO8601 with Z
    assert data["created"] == "2026-06-15T13:30:00Z"


def test_render_node_note_derived_headline_truncated_at_sentence():
    md = render_node_note(_node())
    assert "# Fixed FTS trigger backfill in storage.rs" in md
    # full summary still present in the body
    assert "It works now." in md.split("## Summary")[1]


def test_render_node_note_design_decisions_as_list_escapes_pipes_and_newlines():
    md = render_node_note(_node())
    dd = md.split("## Design Decisions")[1]
    # rendered as a list, not a table; literal | preserved, newlines collapsed
    assert "- **Considered** vec0 only" in dd
    assert "FTS5 | hybrid" in dd
    assert "recall matters" in dd  # newline collapsed to space


def test_render_node_note_non_json_content_falls_back():
    md = render_node_note(_node(content_json="not json at all", embed_text="raw tokens here"))
    assert "## Summary" in md
    assert "raw tokens here" in md  # falls back to embed_text
    # frontmatter still valid
    yaml.safe_load(md.split("---\n")[1])


def test_render_node_note_full_outcome_vocab_passthrough():
    for oc in ("success", "partial", "failure", "unknown", "cancelled", "action_required", "skipped"):
        md = render_node_note(_node(outcome=oc))
        assert yaml.safe_load(md.split("---\n")[1])["outcome"] == oc
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k render_node_note -v`
Expected: FAIL — `ImportError: cannot import name 'render_node_note'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to brain/src/hippo_brain/vault_render.py
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml

GENERATED_BANNER = "<!-- GENERATED BY hippo export vault — edits are overwritten on next sync -->"


@dataclass
class NodeRow:
    uuid: str
    source_key: str
    node_type: str
    outcome: str | None
    content_json: str
    embed_text: str
    tags: list[str]
    created_ms: int
    updated_ms: int
    entities: list[tuple[str, str, str]]   # (type, display, link_target_path)
    related: list[tuple[str, str]]         # (target_source_key, alias)
    sources: list[str] = field(default_factory=list)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _headline(summary: str, cap: int = 80) -> str:
    s = " ".join(summary.split())
    if not s:
        return "Untitled knowledge node"
    first = re.split(r"(?<=[.!?])\s", s, maxsplit=1)[0]
    if len(first) > cap:
        first = first[: cap - 1].rstrip() + "…"
    return first


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _emit_frontmatter(data: dict) -> str:
    # sort_keys=False preserves our order; default_flow_style=False -> block lists;
    # allow_unicode keeps identifiers intact. PyYAML force-quotes values needing it.
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return f"---\n{body}---\n"


def render_node_note(node: NodeRow) -> str:
    try:
        content = json.loads(node.content_json)
        if not isinstance(content, dict):
            raise ValueError
    except (ValueError, TypeError):
        content = {}

    summary = content.get("summary") or node.embed_text or "(no content)"
    headline = _headline(summary)

    fm = {
        "uuid": node.uuid,
        "type": "knowledge",
        "node_type": node.node_type,
        "outcome": node.outcome or "unknown",
        "intent": content.get("intent", ""),
        "created": _iso(node.created_ms),
        "updated": _iso(node.updated_ms),
        "tags": list(node.tags),
        "related": [f"[[{key}|{alias}]]" for key, alias in node.related],
        "aliases": [headline],
    }

    lines = [GENERATED_BANNER, _emit_frontmatter(fm), f"# {headline}\n"]
    lines.append(f"**Outcome:** {fm['outcome']} · **Intent:** {fm['intent'] or 'n/a'}\n")

    lines.append("## Summary\n")
    lines.append(summary + "\n")

    if content.get("key_decisions"):
        lines.append("## Key Decisions\n")
        lines += [f"- {_one_line(d)}" for d in content["key_decisions"]]
        lines.append("")

    if content.get("problems_encountered"):
        lines.append("## Problems Encountered\n")
        lines += [f"- {_one_line(p)}" for p in content["problems_encountered"]]
        lines.append("")

    if content.get("design_decisions"):
        lines.append("## Design Decisions\n")
        for dd in content["design_decisions"]:
            considered = _one_line(dd.get("considered", ""))
            chosen = _one_line(dd.get("chosen", ""))
            reason = _one_line(dd.get("reason", ""))
            lines.append(f"- **Considered** {considered} — **Chose** {chosen} — **Reason** {reason}")
        lines.append("")

    if node.entities:
        lines.append("## Entities\n")
        by_type: dict[str, list[str]] = {}
        for etype, display, target in node.entities:
            by_type.setdefault(etype, []).append(f"[[{target}|{display}]]")
        for etype in sorted(by_type):
            lines.append(f"- {etype}: " + ", ".join(by_type[etype]))
        lines.append("")

    if node.related:
        lines.append("## Related\n")
        lines += [f"- [[{key}|{alias}]]" for key, alias in node.related]
        lines.append("")

    if node.sources:
        lines.append("## Sources\n")
        lines += [f"- {s}" for s in node.sources]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k render_node_note -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Lint + commit**

```bash
cd /Users/carpenter/projects/hippo
uv run --project brain ruff check brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
uv run --project brain ruff format brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
git add brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
git commit -m "feat(vault): render knowledge node notes (non-JSON fallback, list design-decisions, YAML round-trip)"
```

---

### Task 3: Entity page rendering

**Files:**
- Modify: `brain/src/hippo_brain/vault_render.py`
- Test: `brain/tests/test_vault_export.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to brain/tests/test_vault_export.py
from hippo_brain.vault_render import render_entity_page, EntityRow


def test_render_entity_page_lists_members_and_omits_last_seen():
    row = EntityRow(
        entity_type="project", canonical="hippo", first_seen_ms=1704067200000,
        members=[("claude-code-aaa-0", "Fixed FTS"), ("evt-12", "ran cargo test")],
        total_members=2, cap=200,
    )
    md = render_entity_page(row)
    fm = yaml.safe_load(md.split("---\n")[1])
    assert fm["type"] == "entity"
    assert fm["entity_type"] == "project"
    assert "last_seen" not in fm  # churn-free: last_seen deliberately omitted
    assert "[[claude-code-aaa-0|Fixed FTS]]" in md
    assert "showing" not in md.lower()  # no truncation note when under cap


def test_render_entity_page_caps_hub_members_with_explicit_note():
    members = [(f"evt-{i}", f"node {i}") for i in range(3)]
    row = EntityRow(entity_type="tool", canonical="git", first_seen_ms=0,
                    members=members, total_members=5699, cap=3)
    md = render_entity_page(row)
    assert "showing 3 of 5699" in md  # no silent truncation
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k render_entity_page -v`
Expected: FAIL — `ImportError: cannot import name 'render_entity_page'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to brain/src/hippo_brain/vault_render.py
@dataclass
class EntityRow:
    entity_type: str
    canonical: str
    first_seen_ms: int
    members: list[tuple[str, str]]   # (node_source_key, headline) — already capped by caller
    total_members: int
    cap: int


def render_entity_page(entity: EntityRow) -> str:
    fm = {
        "type": "entity",
        "entity_type": entity.entity_type,
        "canonical": entity.canonical,
        "first_seen": _iso(entity.first_seen_ms),
        "aliases": [f"{entity.entity_type}: {entity.canonical}"],
    }
    # NOTE: last_seen is intentionally omitted — it is rewritten on every
    # (re-)enrichment and would churn this file's content/mtime every sync.
    lines = [GENERATED_BANNER, _emit_frontmatter(fm), f"# {entity.canonical}\n"]
    lines.append(f"**Type:** {entity.entity_type}\n")
    lines.append("## Nodes\n")
    lines += [f"- [[{key}|{headline}]]" for key, headline in entity.members]
    if entity.total_members > len(entity.members):
        lines.append(f"\n_(showing {len(entity.members)} of {entity.total_members} nodes)_")
    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k render_entity_page -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Lint + commit**

```bash
cd /Users/carpenter/projects/hippo
uv run --project brain ruff check brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
uv run --project brain ruff format brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
git add brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
git commit -m "feat(vault): render entity hub pages (member lists, capped, churn-free)"
```

---

### Task 4: Index / MOC rendering

**Files:**
- Modify: `brain/src/hippo_brain/vault_render.py`
- Test: `brain/tests/test_vault_export.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to brain/tests/test_vault_export.py
from hippo_brain.vault_render import render_root_index, shard_for


def test_shard_for_uses_created_month():
    assert shard_for(1781530200000) == "2026-06"


def test_render_root_index_links_to_sub_indexes_not_all_nodes():
    md = render_root_index(projects=["hippo", "whistlepost"], months=["2026-06", "2026-05"])
    assert "[[indexes/project-hippo|hippo]]" in md
    assert "[[indexes/month-2026-06|2026-06]]" in md
    # the root index must NOT enumerate individual nodes (unbounded growth guard)
    assert "knowledge/" not in md
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k "shard_for or root_index" -v`
Expected: FAIL — `ImportError: cannot import name 'render_root_index'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to brain/src/hippo_brain/vault_render.py
def shard_for(created_ms: int) -> str:
    """knowledge/ sub-folder for a node, derived from immutable created_at month."""
    return datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def render_root_index(projects: list[str], months: list[str]) -> str:
    lines = [GENERATED_BANNER, "# hippo knowledge vault\n", "## Projects\n"]
    lines += [f"- [[indexes/project-{slugify(p)}|{p}]]" for p in projects]
    lines.append("\n## Months\n")
    lines += [f"- [[indexes/month-{m}|{m}]]" for m in months]
    return "\n".join(lines).rstrip() + "\n"


def render_sub_index(title: str, members: list[tuple[str, str]]) -> str:
    """A per-project or per-month MOC listing node links (bounded by caller)."""
    lines = [GENERATED_BANNER, f"# {title}\n"]
    lines += [f"- [[{key}|{headline}]]" for key, headline in members]
    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k "shard_for or root_index" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Lint + commit**

```bash
cd /Users/carpenter/projects/hippo
uv run --project brain ruff check brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
uv run --project brain ruff format brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
git add brain/src/hippo_brain/vault_render.py brain/tests/test_vault_export.py
git commit -m "feat(vault): render bounded root + sub-index MOC notes, month sharding"
```

---

### Task 5: Rarity-weighted bounded related edges

**Files:**
- Create: `brain/src/hippo_brain/vault_edges.py`
- Test: `brain/tests/test_vault_export.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to brain/tests/test_vault_export.py
from hippo_brain.vault_edges import compute_related


def test_compute_related_excludes_hub_entities_and_bounds_topk():
    # node 1 shares hub entity 'git'(deg 9999) with everyone, and rare entity
    # 'storage.rs'(deg 2) with node 2 only. Only the rare link should survive.
    node_entities = {
        1: {"git", "storage.rs"},
        2: {"git", "storage.rs"},
        3: {"git"},
    }
    entity_degree = {"git": 9999, "storage.rs": 2}
    related = compute_related(
        node_entities, entity_degree, hub_degree_cap=200, top_k=8
    )
    assert related[1] == [2]   # node 3 shares only the excluded hub -> no edge
    assert related[3] == []


def test_compute_related_respects_top_k_ordering_by_rarity_weight():
    node_entities = {
        1: {"a", "b", "c"},
        2: {"a"},          # shares rarest
        3: {"b"},
        4: {"c"},
    }
    entity_degree = {"a": 2, "b": 3, "c": 4}
    related = compute_related(node_entities, entity_degree, hub_degree_cap=200, top_k=2)
    assert related[1] == [2, 3]   # top-2 by inverse-degree weight: a > b > c
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k compute_related -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hippo_brain.vault_edges'`

- [ ] **Step 3: Write minimal implementation**

```python
# brain/src/hippo_brain/vault_edges.py
"""Bounded, rarity-weighted node->node related edges.

Raw shared-entity co-occurrence is unusable (56M pairs live, dominated by hub
entities like 'git' that link ~42% of the KB). We exclude entities above a
degree cap and weight the rest by inverse degree so rare shared entities (a
specific file, a specific error) dominate, then keep top-K neighbours.
"""

from __future__ import annotations

import math
from collections import defaultdict


def compute_related(
    node_entities: dict[int, set],
    entity_degree: dict,
    hub_degree_cap: int,
    top_k: int,
) -> dict[int, list[int]]:
    """Return {node_id: [neighbour_node_id, ...]} bounded to top_k by rarity weight."""
    # Invert: rare (non-hub) entity -> nodes carrying it.
    entity_nodes: dict = defaultdict(list)
    for node_id, ents in node_entities.items():
        for e in ents:
            if entity_degree.get(e, 0) <= hub_degree_cap:
                entity_nodes[e].append(node_id)

    scores: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for e, nodes in entity_nodes.items():
        weight = 1.0 / math.log(1 + entity_degree[e])
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                scores[a][b] += weight
                scores[b][a] += weight

    related: dict[int, list[int]] = {}
    for node_id in node_entities:
        neighbours = scores.get(node_id, {})
        ranked = sorted(neighbours.items(), key=lambda kv: (-kv[1], kv[0]))
        related[node_id] = [nid for nid, _ in ranked[:top_k]]
    return related
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k compute_related -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Lint + commit**

```bash
cd /Users/carpenter/projects/hippo
uv run --project brain ruff check brain/src/hippo_brain/vault_edges.py brain/tests/test_vault_export.py
uv run --project brain ruff format brain/src/hippo_brain/vault_edges.py brain/tests/test_vault_export.py
git add brain/src/hippo_brain/vault_edges.py brain/tests/test_vault_export.py
git commit -m "feat(vault): rarity-weighted hub-excluded top-K related edges"
```

---

## Phase 2 — Reconcile & orchestration

### Task 6: Probe-safe node query layer

**Files:**
- Create: `brain/src/hippo_brain/vault_export.py`
- Test: `brain/tests/test_vault_export.py`

Build the query that selects exportable nodes. A node is exportable only if it
has **at least one non-probe source row** — `knowledge_nodes` has no `probe_tag`
column, so this restores the AP-6 guarantee at the export surface.

- [ ] **Step 1: Write the failing test**

The brain test suite already ships a `tmp_db` fixture in `brain/tests/conftest.py`
that applies the real `crates/hippo-core/src/schema.sql` to a temp SQLite file and
yields `(conn, db_path)`. Use it — do **not** shell out to the Rust binary.

```python
# append to brain/tests/test_vault_export.py
from hippo_brain.vault_export import fetch_exportable_node_ids


def test_fetch_exportable_excludes_probe_only_nodes(tmp_db):
    conn, _db_path = tmp_db
    conn.execute("INSERT INTO knowledge_nodes (id, uuid, content, embed_text) VALUES (1,'real','{}','x'),(2,'probe','{}','y')")
    conn.execute("INSERT INTO agentic_sessions (id, session_id, harness, segment_index, project_dir, cwd, summary_text, start_time, end_time, probe_tag) VALUES (10,'s','codex',0,'/p','/c','sum',0,0,NULL),(11,'sp','codex',1,'/p','/c','sum',0,0,'PROBE')")
    conn.execute("INSERT INTO knowledge_node_agentic_sessions VALUES (1,10),(2,11)")
    conn.commit()
    ids = fetch_exportable_node_ids(conn)
    assert ids == [1]   # node 2 is sourced only by a probe-tagged session
```

> The Task 9 integration test uses the same `tmp_db` fixture (unpack `conn, _ = tmp_db`).

- [ ] **Step 2: Run test to verify it fails**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k fetch_exportable -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_exportable_node_ids'`

- [ ] **Step 3: Write minimal implementation**

```python
# brain/src/hippo_brain/vault_export.py
"""Export the knowledge base into an Obsidian vault (one-way projection).

Orchestrates query -> render -> full reconcile over a single read snapshot.
"""

from __future__ import annotations

import sqlite3

VAULT_FORMAT_VERSION = 1


def fetch_exportable_node_ids(conn: sqlite3.Connection) -> list[int]:
    """Node ids that have >=1 non-probe source row (AP-6 at the export surface)."""
    rows = conn.execute(
        """
        SELECT DISTINCT kn.id
        FROM knowledge_nodes kn
        WHERE EXISTS (SELECT 1 FROM knowledge_node_agentic_sessions l
                        JOIN agentic_sessions s ON s.id = l.agentic_session_id
                       WHERE l.knowledge_node_id = kn.id AND s.probe_tag IS NULL)
           OR EXISTS (SELECT 1 FROM knowledge_node_events l
                        JOIN events e ON e.id = l.event_id
                       WHERE l.knowledge_node_id = kn.id AND e.probe_tag IS NULL)
           OR EXISTS (SELECT 1 FROM knowledge_node_browser_events l
                        JOIN browser_events b ON b.id = l.browser_event_id
                       WHERE l.knowledge_node_id = kn.id AND b.probe_tag IS NULL)
           OR EXISTS (SELECT 1 FROM knowledge_node_workflow_runs l
                       WHERE l.knowledge_node_id = kn.id)
        ORDER BY kn.id
        """
    ).fetchall()
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k fetch_exportable -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/carpenter/projects/hippo
uv run --project brain ruff check brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
uv run --project brain ruff format brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
git add brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
git commit -m "feat(vault): probe-safe exportable-node query (AP-6 at export surface)"
```

---

### Task 7: Reconcile (atomic write-changed + orphan GC)

**Files:**
- Modify: `brain/src/hippo_brain/vault_export.py`
- Test: `brain/tests/test_vault_export.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to brain/tests/test_vault_export.py
from hippo_brain.vault_export import reconcile_files


def test_reconcile_writes_changed_skips_unchanged_and_gcs_orphans(tmp_path):
    root = tmp_path / "vault"
    (root / "knowledge").mkdir(parents=True)
    stale = root / "knowledge" / "old.md"
    stale.write_text("stale\n")
    keep = root / "knowledge" / "keep.md"
    keep.write_text("v1\n")

    desired = {
        root / "knowledge" / "keep.md": "v1\n",      # unchanged
        root / "knowledge" / "new.md": "fresh\n",    # new
    }
    mtime_before = keep.stat().st_mtime_ns
    summary = reconcile_files(root, desired, managed_subdirs=["knowledge"])

    assert (root / "knowledge" / "new.md").read_text() == "fresh\n"
    assert not stale.exists()                         # orphan GC'd
    assert keep.stat().st_mtime_ns == mtime_before    # unchanged not rewritten
    assert summary["written"] == 1 and summary["deleted"] == 1 and summary["unchanged"] == 1


def test_reconcile_gc_scoped_to_managed_subdirs_only(tmp_path):
    root = tmp_path / "vault"
    (root / "knowledge").mkdir(parents=True)
    foreign = root / "user-note.md"   # outside managed subdirs
    foreign.write_text("mine\n")
    reconcile_files(root, {}, managed_subdirs=["knowledge"])
    assert foreign.exists()           # never touched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k reconcile -v`
Expected: FAIL — `ImportError: cannot import name 'reconcile_files'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to brain/src/hippo_brain/vault_export.py
import os
from pathlib import Path


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)   # atomic on POSIX


def reconcile_files(root: Path, desired: dict, managed_subdirs: list[str]) -> dict:
    """Write changed files, skip unchanged, delete orphans within managed subdirs."""
    written = unchanged = deleted = 0
    desired = {Path(p): c for p, c in desired.items()}

    for path, content in desired.items():
        if path.exists() and path.read_text(encoding="utf-8") == content:
            unchanged += 1
            continue
        _atomic_write(path, content)
        written += 1

    desired_paths = set(desired)
    for sub in managed_subdirs:
        base = root / sub
        if not base.exists():
            continue
        for existing in base.rglob("*.md"):
            if existing not in desired_paths:
                existing.unlink()
                deleted += 1

    return {"written": written, "unchanged": unchanged, "deleted": deleted}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k reconcile -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/carpenter/projects/hippo
uv run --project brain ruff check brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
uv run --project brain ruff format brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
git add brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
git commit -m "feat(vault): atomic write-changed + orphan-GC reconcile scoped to managed subdirs"
```

---

### Task 8: Safety rails (foreign-vault guard, .gitignore, meta + format guard)

**Files:**
- Modify: `brain/src/hippo_brain/vault_export.py`
- Test: `brain/tests/test_vault_export.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to brain/tests/test_vault_export.py
import json as _json
import pytest
from hippo_brain.vault_export import (
    assert_safe_target, write_vault_meta, check_format_version, VAULT_FORMAT_VERSION,
)


def test_assert_safe_target_rejects_foreign_obsidian_vault(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    with pytest.raises(RuntimeError, match="foreign Obsidian vault"):
        assert_safe_target(tmp_path)


def test_assert_safe_target_allows_empty_or_hippo_owned(tmp_path):
    assert_safe_target(tmp_path)                       # empty: ok
    write_vault_meta(tmp_path, hippo_version="0.28.7", schema_version=18, config_hash="abc")
    assert_safe_target(tmp_path)                       # has our meta: ok


def test_check_format_version_flags_drift(tmp_path):
    write_vault_meta(tmp_path, hippo_version="0.28.7", schema_version=18, config_hash="abc")
    meta = _json.loads((tmp_path / "_vault_meta.json").read_text())
    assert meta["vault_format_version"] == VAULT_FORMAT_VERSION
    assert check_format_version(tmp_path) is True       # matches
    meta["vault_format_version"] = 999
    (tmp_path / "_vault_meta.json").write_text(_json.dumps(meta))
    assert check_format_version(tmp_path) is False      # drift detected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k "safe_target or format_version" -v`
Expected: FAIL — `ImportError: cannot import name 'assert_safe_target'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to brain/src/hippo_brain/vault_export.py
import json

_META_NAME = "_vault_meta.json"


def assert_safe_target(root: Path) -> None:
    """Refuse to write into a directory that is a foreign Obsidian vault."""
    root = Path(root)
    if (root / ".obsidian").exists() and not (root / _META_NAME).exists():
        raise RuntimeError(
            f"{root} looks like a foreign Obsidian vault (.obsidian present, no hippo "
            f"{_META_NAME}). Refusing to write. Use a dedicated hippo vault dir."
        )


def write_vault_meta(root: Path, hippo_version: str, schema_version: int, config_hash: str) -> None:
    Path(root).mkdir(parents=True, exist_ok=True)
    (Path(root) / _META_NAME).write_text(
        json.dumps(
            {
                "vault_format_version": VAULT_FORMAT_VERSION,
                "hippo_version": hippo_version,
                "schema_version": schema_version,
                "config_hash": config_hash,
            },
            indent=2,
        )
    )


def write_gitignore(root: Path) -> None:
    gi = Path(root) / ".gitignore"
    if not gi.exists():
        gi.write_text("# hippo vault is a regenerated projection; do not commit\n*\n")


def check_format_version(root: Path) -> bool:
    """True if the on-disk vault matches our format version (or is fresh)."""
    meta_path = Path(root) / _META_NAME
    if not meta_path.exists():
        return True
    try:
        meta = json.loads(meta_path.read_text())
    except (ValueError, OSError):
        return False
    return meta.get("vault_format_version") == VAULT_FORMAT_VERSION
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k "safe_target or format_version" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/carpenter/projects/hippo
uv run --project brain ruff check brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
uv run --project brain ruff format brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
git add brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
git commit -m "feat(vault): safety rails — foreign-vault guard, .gitignore, meta + format-version guard"
```

---

### Task 9: Orchestrator `export_vault()` + integration test

**Files:**
- Modify: `brain/src/hippo_brain/vault_export.py`
- Test: `brain/tests/test_vault_export.py`

This wires query → fetch rows → render → edges → reconcile + meta, applying the
export-time redaction pass to all rendered text. Returns a summary dict.

- [ ] **Step 1: Write the failing integration test**

```python
# append to brain/tests/test_vault_export.py
from hippo_brain.vault_export import export_vault


def test_export_vault_end_to_end_redacts_and_reconciles(tmp_db, tmp_path):
    conn, _db_path = tmp_db
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, outcome, created_at, updated_at) "
        "VALUES (1,'u1','{\"summary\":\"used token sk-ABC123DEF456GHI789JKL to call api\",\"intent\":\"debug\"}','tok',"
        "'observation','success',1781530200000,1781530200000)"
    )
    conn.execute(
        "INSERT INTO agentic_sessions (id, session_id, harness, segment_index, project_dir, cwd, summary_text, start_time, end_time) "
        "VALUES (10,'sess','codex',0,'/p','/c','s',0,0)"
    )
    conn.execute("INSERT INTO knowledge_node_agentic_sessions VALUES (1,10)")
    conn.commit()

    out = tmp_path / "vault"
    summary = export_vault(
        conn, str(out), hippo_version="0.0.0",
        related_top_k=8, hub_degree_cap=200, hub_node_list_cap=200, shard_by="month",
    )

    note = out / "knowledge" / "2026-06" / "codex-sess-0.md"
    assert note.exists()
    text = note.read_text()
    assert "sk-ABC123DEF456GHI789JKL" not in text     # export-time redaction applied
    assert (out / "_vault_meta.json").exists()
    assert (out / ".gitignore").exists()
    assert summary["nodes"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -k export_vault_end_to_end -v`
Expected: FAIL — `ImportError: cannot import name 'export_vault'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to brain/src/hippo_brain/vault_export.py
import hashlib

from hippo_brain import redaction
from hippo_brain.vault_edges import compute_related
from hippo_brain.vault_render import (
    EntityRow, NodeRow, entity_slug, node_source_key, render_entity_page,
    render_node_note, render_root_index, render_sub_index, shard_for, slugify,
)


def _load_node_links(conn: sqlite3.Connection, node_id: int) -> dict:
    links: dict = {}
    rows = conn.execute(
        "SELECT s.harness, s.session_id, s.segment_index FROM knowledge_node_agentic_sessions l "
        "JOIN agentic_sessions s ON s.id = l.agentic_session_id WHERE l.knowledge_node_id = ?",
        (node_id,),
    ).fetchall()
    if rows:
        links["agentic"] = [(r[0], r[1], r[2]) for r in rows]
    for kind, table, col in (
        ("workflow", "knowledge_node_workflow_runs", "run_id"),
        ("browser", "knowledge_node_browser_events", "browser_event_id"),
        ("shell", "knowledge_node_events", "event_id"),
    ):
        ids = [r[0] for r in conn.execute(
            f"SELECT {col} FROM {table} WHERE knowledge_node_id = ?", (node_id,)
        ).fetchall()]
        if ids:
            links[kind] = ids
    return links


def export_vault(
    conn: sqlite3.Connection, out_dir: str, hippo_version: str,
    related_top_k: int, hub_degree_cap: int, hub_node_list_cap: int, shard_by: str,
) -> dict:
    root = Path(out_dir).expanduser()
    if not check_format_version(root):
        raise RuntimeError(
            f"{root} was written by a different vault_format_version; run a full export "
            "into a clean directory."
        )
    assert_safe_target(root)

    node_ids = fetch_exportable_node_ids(conn)

    # Pull node rows + per-node entity sets (typed, from the JOIN — authoritative).
    node_meta: dict[int, dict] = {}
    node_entity_sets: dict[int, set] = {}
    for nid in node_ids:
        r = conn.execute(
            "SELECT uuid, content, embed_text, node_type, outcome, tags, created_at, updated_at "
            "FROM knowledge_nodes WHERE id = ?", (nid,)
        ).fetchone()
        ents = conn.execute(
            "SELECT e.id, e.type, e.name, e.canonical FROM knowledge_node_entities kne "
            "JOIN entities e ON e.id = kne.entity_id WHERE kne.knowledge_node_id = ?", (nid,)
        ).fetchall()
        node_meta[nid] = {"row": r, "links": _load_node_links(conn, nid), "ents": ents}
        node_entity_sets[nid] = {e[0] for e in ents}   # entity ids

    # Entity degrees over the exported node set only.
    entity_degree: dict = {}
    for ents in node_entity_sets.values():
        for eid in ents:
            entity_degree[eid] = entity_degree.get(eid, 0) + 1

    related_ids = compute_related(node_entity_sets, entity_degree, hub_degree_cap, related_top_k)

    # Stable source-key slug per node id (needed to render related/entity links).
    import json as _json

    def _headline_of(nid: int) -> str:
        try:
            c = _json.loads(node_meta[nid]["row"][1])
            return (c.get("summary") or node_meta[nid]["row"][2] or "")[:80]
        except (ValueError, TypeError):
            return node_meta[nid]["row"][2] or ""

    slug_of: dict[int, str] = {}
    for nid in node_ids:
        row = node_meta[nid]["row"]
        slug_of[nid] = node_source_key(node_meta[nid]["links"], node_type=row[3], uuid=row[0])

    desired: dict[Path, str] = {}
    projects: dict[str, list[tuple[str, str]]] = {}   # project name -> node (slug, headline)
    months: dict[str, list[tuple[str, str]]] = {}     # month shard -> node (slug, headline)
    entity_members: dict[int, list[tuple[str, str]]] = {}

    for nid in node_ids:
        uuid, content_json, embed_text, node_type, outcome, tags_json, created, updated = node_meta[nid]["row"]
        ents = node_meta[nid]["ents"]
        entity_links = []
        for eid, etype, ename, ecanon in ents:
            target = f"entities/{etype}/{entity_slug(etype, ename, ecanon, eid)}"
            entity_links.append((etype, ename, target))
            entity_members.setdefault(eid, []).append((slug_of[nid], _headline_of(nid)))
            if etype == "project":
                projects.setdefault(ecanon or ename, []).append((slug_of[nid], _headline_of(nid)))
        related = [(slug_of[t], _headline_of(t)) for t in related_ids.get(nid, [])]
        try:
            tags = _json.loads(tags_json) if tags_json else []
        except (ValueError, TypeError):
            tags = []
        node = NodeRow(
            uuid=uuid, source_key=slug_of[nid], node_type=node_type, outcome=outcome,
            content_json=content_json, embed_text=embed_text,
            tags=[slugify(str(t)) for t in tags],
            created_ms=created, updated_ms=updated,
            entities=entity_links, related=related,
            sources=[f"{k}: {v}" for k, v in node_meta[nid]["links"].items()],
        )
        md = redaction.redact(render_node_note(node))   # export-time redaction pass
        shard = shard_for(created) if shard_by == "month" else "all"
        months.setdefault(shard, []).append((slug_of[nid], _headline_of(nid)))
        desired[root / "knowledge" / shard / f"{slug_of[nid]}.md"] = md

    # Entity pages (capped member lists).
    for eid, etype, ecanon, ename, first_seen in conn.execute(
        "SELECT id, type, canonical, name, first_seen FROM entities"
    ).fetchall():
        members = entity_members.get(eid)
        if not members:
            continue
        capped = members[:hub_node_list_cap]
        page = render_entity_page(EntityRow(
            entity_type=etype, canonical=ecanon or ename, first_seen_ms=first_seen,
            members=capped, total_members=len(members), cap=hub_node_list_cap,
        ))
        slug = entity_slug(etype, ename, ecanon, eid)
        desired[root / "entities" / etype / f"{slug}.md"] = redaction.redact(page)

    # Index notes: a small root MOC + one sub-index per project and per month.
    desired[root / "_index.md"] = render_root_index(
        sorted(projects), sorted(months, reverse=True)
    )
    for proj, members in projects.items():
        desired[root / "indexes" / f"project-{slugify(proj)}.md"] = render_sub_index(proj, members)
    for month, members in months.items():
        desired[root / "indexes" / f"month-{month}.md"] = render_sub_index(month, members)

    write_gitignore(root)
    schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
    config_hash = hashlib.sha256(
        f"{related_top_k}-{hub_degree_cap}-{hub_node_list_cap}-{shard_by}".encode()
    ).hexdigest()[:12]
    write_vault_meta(root, hippo_version, schema_version, config_hash)

    recon = reconcile_files(root, desired, managed_subdirs=["knowledge", "entities", "indexes"])
    return {"nodes": len(node_ids), **recon}
```

- [ ] **Step 4: Run the full Python suite**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_vault_export.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/carpenter/projects/hippo
uv run --project brain ruff check brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
uv run --project brain ruff format brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
git add brain/src/hippo_brain/vault_export.py brain/tests/test_vault_export.py
git commit -m "feat(vault): export_vault orchestrator with export-time redaction + integration test"
```

---

## Phase 3 — Brain HTTP endpoint

### Task 10: `POST /vault/export` handler

**Files:**
- Modify: `brain/src/hippo_brain/server.py:1801-1812` (get_routes) and add handler near `query`
- Test: `brain/tests/test_server_extended.py`

- [ ] **Step 1: Write the failing test**

```python
# append to brain/tests/test_server_extended.py
from starlette.testclient import TestClient
from hippo_brain.server import create_app


def test_vault_export_endpoint_invokes_export(tmp_path, monkeypatch):
    captured = {}

    def fake_export(conn, out_dir, hippo_version, related_top_k, hub_degree_cap, hub_node_list_cap, shard_by):
        captured.update(out_dir=out_dir, top_k=related_top_k, cap=hub_degree_cap)
        return {"nodes": 3, "written": 3, "unchanged": 0, "deleted": 1}

    monkeypatch.setattr("hippo_brain.server.export_vault", fake_export)
    app = create_app(db_path=str(tmp_path / "x.db"))
    with TestClient(app) as client:
        resp = client.post("/vault/export", json={"out": str(tmp_path / "v"), "related_top_k": 5})
    assert resp.status_code == 200
    assert resp.json()["nodes"] == 3
    assert captured["out_dir"].endswith("/v") and captured["top_k"] == 5


def test_vault_export_requires_out(tmp_path):
    app = create_app(db_path=str(tmp_path / "x.db"))
    with TestClient(app) as client:
        resp = client.post("/vault/export", json={})
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_server_extended.py -k vault_export -v`
Expected: FAIL — 404 (route absent) / `AttributeError`

- [ ] **Step 3: Add the import, handler, and route**

```python
# near the other imports at the top of brain/src/hippo_brain/server.py
from hippo_brain.vault_export import export_vault
from hippo_brain._version import __version__ as _hippo_version  # adjust to the real version symbol
```

```python
# add as a method on BrainServer, beside `query`
async def vault_export(self, request: Request) -> JSONResponse:
    body = await request.json()
    out = body.get("out")
    if not out:
        return JSONResponse({"error": "out is required"}, status_code=400)
    conn = self._get_conn()
    try:
        summary = export_vault(
            conn,
            out_dir=out,
            hippo_version=_hippo_version,
            related_top_k=int(body.get("related_top_k", 8)),
            hub_degree_cap=int(body.get("hub_degree_cap", 200)),
            hub_node_list_cap=int(body.get("hub_node_list_cap", 200)),
            shard_by=str(body.get("shard_by", "month")),
        )
        return JSONResponse(summary)
    except (RuntimeError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()
```

```python
# in get_routes(), add to the list:
            Route("/vault/export", self.vault_export, methods=["POST"]),
```

> If `hippo_brain._version.__version__` is not the right symbol, grep for the existing version constant the server already uses (`rg -n "__version__|VERSION" brain/src/hippo_brain/version.py`) and import that instead.

- [ ] **Step 4: Run test to verify it passes**

Run: `brain/.venv/bin/python -m pytest brain/tests/test_server_extended.py -k vault_export -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/carpenter/projects/hippo
uv run --project brain ruff check brain/src/hippo_brain/server.py brain/tests/test_server_extended.py
uv run --project brain ruff format brain/src/hippo_brain/server.py brain/tests/test_server_extended.py
git add brain/src/hippo_brain/server.py brain/tests/test_server_extended.py
git commit -m "feat(vault): POST /vault/export brain endpoint"
```

---

## Phase 4 — Rust CLI & config

### Task 11: `VaultConfig`

**Files:**
- Modify: `crates/hippo-core/src/config.rs`
- Modify: `config/config.default.toml`

- [ ] **Step 1: Write the failing test**

```rust
// add to the #[cfg(test)] mod in crates/hippo-core/src/config.rs
#[test]
fn vault_config_defaults() {
    let c = HippoConfig::default();
    assert!(!c.vault.enabled);
    assert_eq!(c.vault.poll_interval_secs, 300);
    assert_eq!(c.vault.related_top_k, 8);
    assert_eq!(c.vault.hub_degree_cap, 200);
    assert_eq!(c.vault.hub_node_list_cap, 200);
    assert_eq!(c.vault.shard_by, "month");
}

#[test]
fn vault_config_parses_from_toml() {
    let toml = r#"
[vault]
enabled = true
out = "/tmp/myvault"
poll_interval_secs = 600
related_top_k = 12
"#;
    let c: HippoConfig = toml::from_str(toml).unwrap();
    assert!(c.vault.enabled);
    assert_eq!(c.vault.out.as_deref(), Some("/tmp/myvault"));
    assert_eq!(c.vault.poll_interval_secs, 600);
    assert_eq!(c.vault.related_top_k, 12);
    assert_eq!(c.vault.hub_degree_cap, 200); // default preserved
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/carpenter/projects/hippo && cargo test -p hippo-core vault_config`
Expected: FAIL — no field `vault` on `HippoConfig`

- [ ] **Step 3: Add the struct, defaults, and field**

```rust
// add the field to struct HippoConfig (after `pub cursor: CursorConfig,`)
    #[serde(default)]
    pub vault: VaultConfig,
```

```rust
// add near the other section structs in crates/hippo-core/src/config.rs
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VaultConfig {
    /// Enable Obsidian vault export. When false, `hippo export vault` errors
    /// out and the vault-sync LaunchAgent is a no-op.
    #[serde(default = "default_vault_enabled")]
    pub enabled: bool,
    /// Output directory. None => `<data_dir>/vault`.
    #[serde(default)]
    pub out: Option<String>,
    /// launchd StartInterval for com.hippo.vault-sync, in seconds.
    #[serde(default = "default_vault_poll_interval_secs")]
    pub poll_interval_secs: u64,
    /// Max node->node related edges per note.
    #[serde(default = "default_vault_related_top_k")]
    pub related_top_k: u32,
    /// Entities linking more than this many nodes are excluded from related[].
    #[serde(default = "default_vault_hub_degree_cap")]
    pub hub_degree_cap: u32,
    /// Max member nodes listed on an entity page.
    #[serde(default = "default_vault_hub_node_list_cap")]
    pub hub_node_list_cap: u32,
    /// knowledge/ sharding scheme: "month" or "all".
    #[serde(default = "default_vault_shard_by")]
    pub shard_by: String,
}

fn default_vault_enabled() -> bool { false }
fn default_vault_poll_interval_secs() -> u64 { 300 }
fn default_vault_related_top_k() -> u32 { 8 }
fn default_vault_hub_degree_cap() -> u32 { 200 }
fn default_vault_hub_node_list_cap() -> u32 { 200 }
fn default_vault_shard_by() -> String { "month".to_string() }

impl Default for VaultConfig {
    fn default() -> Self {
        Self {
            enabled: default_vault_enabled(),
            out: None,
            poll_interval_secs: default_vault_poll_interval_secs(),
            related_top_k: default_vault_related_top_k(),
            hub_degree_cap: default_vault_hub_degree_cap(),
            hub_node_list_cap: default_vault_hub_node_list_cap(),
            shard_by: default_vault_shard_by(),
        }
    }
}
```

- [ ] **Step 4: Append the `[vault]` section to `config/config.default.toml`**

```toml
# ─── Obsidian vault export ────────────────────────────────────────────
# When enabled, `hippo export vault` writes a one-way markdown projection of
# the knowledge base, and a com.hippo.vault-sync LaunchAgent refreshes it on
# the poll interval. The vault is regenerated each run; edits made in it are
# discarded. Default location is <data_dir>/vault.
[vault]
enabled = false
# out = "~/.local/share/hippo/vault"
# poll_interval_secs = 300   # changing this requires `hippo daemon install --force`
# related_top_k = 8
# hub_degree_cap = 200       # entities above this are excluded from related[]
# hub_node_list_cap = 200    # max nodes listed on an entity page
# shard_by = "month"
```

- [ ] **Step 5: Run test + commit**

Run: `cargo test -p hippo-core vault_config`
Expected: PASS

```bash
cd /Users/carpenter/projects/hippo
cargo fmt
cargo clippy -p hippo-core --all-targets -- -D warnings
git add crates/hippo-core/src/config.rs config/config.default.toml
git commit -m "feat(vault): [vault] config section"
```

---

### Task 12: `hippo export vault` CLI command

**Files:**
- Modify: `crates/hippo-daemon/src/cli.rs:11` (Commands enum) — add `Export`
- Modify: `crates/hippo-daemon/src/commands.rs` — add `handle_export_vault`
- Modify: `crates/hippo-daemon/src/main.rs` — dispatch

- [ ] **Step 1: Write the failing test**

```rust
// add to the #[cfg(test)] mod in crates/hippo-daemon/src/commands.rs
#[test]
fn resolve_vault_out_prefers_flag_then_config_then_data_dir() {
    use std::path::PathBuf;
    let mut config = HippoConfig::default();
    config.storage.data_dir = PathBuf::from("/data");
    // flag wins
    assert_eq!(resolve_vault_out(&config, Some("/flag".into())), "/flag");
    // config next
    config.vault.out = Some("/cfg".into());
    assert_eq!(resolve_vault_out(&config, None), "/cfg");
    // data_dir fallback
    config.vault.out = None;
    assert_eq!(resolve_vault_out(&config, None), "/data/vault");
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/carpenter/projects/hippo && cargo test -p hippo-daemon resolve_vault_out`
Expected: FAIL — `resolve_vault_out` not found

- [ ] **Step 3: Add the CLI variant, dispatch, and handler**

```rust
// crates/hippo-daemon/src/cli.rs — add to enum Commands (after ExportTraining):
    /// Export the knowledge base to files
    Export {
        #[command(subcommand)]
        action: ExportAction,
    },
```

```rust
// crates/hippo-daemon/src/cli.rs — add a new subcommand enum near the others:
#[derive(clap::Subcommand)]
pub enum ExportAction {
    /// Export an Obsidian-compatible markdown vault
    Vault {
        /// Output directory (overrides [vault] out)
        #[arg(long)]
        out: Option<String>,
        /// Force a full reconcile into a clean directory
        #[arg(long)]
        full: bool,
    },
}
```

```rust
// crates/hippo-daemon/src/commands.rs — add the helper + handler
pub fn resolve_vault_out(config: &HippoConfig, flag: Option<String>) -> String {
    if let Some(f) = flag {
        return f;
    }
    if let Some(c) = &config.vault.out {
        return c.clone();
    }
    config.storage.data_dir.join("vault").to_string_lossy().to_string()
}

pub async fn handle_export_vault(
    config: &HippoConfig,
    out: Option<String>,
    _full: bool,
) -> anyhow::Result<()> {
    if !config.vault.enabled {
        anyhow::bail!("vault export disabled (set vault.enabled = true in config)");
    }
    let out_dir = resolve_vault_out(config, out);
    let url = format!("http://localhost:{}/vault/export", config.brain.port);
    let client = reqwest::Client::new();
    let resp = client
        .post(&url)
        .json(&serde_json::json!({
            "out": out_dir,
            "related_top_k": config.vault.related_top_k,
            "hub_degree_cap": config.vault.hub_degree_cap,
            "hub_node_list_cap": config.vault.hub_node_list_cap,
            "shard_by": config.vault.shard_by,
        }))
        .timeout(std::time::Duration::from_secs(600))
        .send()
        .await?;
    if resp.status().is_success() {
        let body: serde_json::Value = resp.json().await?;
        println!(
            "Vault exported to {out_dir}: {} nodes, {} written, {} unchanged, {} deleted",
            body.get("nodes").and_then(|v| v.as_i64()).unwrap_or(0),
            body.get("written").and_then(|v| v.as_i64()).unwrap_or(0),
            body.get("unchanged").and_then(|v| v.as_i64()).unwrap_or(0),
            body.get("deleted").and_then(|v| v.as_i64()).unwrap_or(0),
        );
        Ok(())
    } else {
        let body: serde_json::Value = resp.json().await.unwrap_or_default();
        anyhow::bail!(
            "vault export failed ({}): {}",
            resp.status(),
            body.get("error").and_then(|e| e.as_str()).unwrap_or("unknown error")
        );
    }
}
```

```rust
// crates/hippo-daemon/src/main.rs — add to the Commands match (and import ExportAction
// alongside the other cli imports at the top):
        Commands::Export { action } => match action {
            ExportAction::Vault { out, full } => {
                commands::handle_export_vault(&config, out, full).await?;
            }
        },
```

- [ ] **Step 4: Run test + clippy**

Run: `cargo test -p hippo-daemon resolve_vault_out`
Expected: PASS

```bash
cd /Users/carpenter/projects/hippo
cargo fmt
cargo clippy -p hippo-daemon --all-targets -- -D warnings
```
Expected: clean

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-daemon/src/cli.rs crates/hippo-daemon/src/commands.rs crates/hippo-daemon/src/main.rs
git commit -m "feat(vault): hippo export vault CLI command (POSTs brain endpoint)"
```

---

## Phase 5 — Service, install, doctor, docs

### Task 13: `com.hippo.vault-sync` LaunchAgent + install wiring

**Files:**
- Create: `launchd/com.hippo.vault-sync.plist`
- Modify: `crates/hippo-daemon/src/install.rs`

- [ ] **Step 1: Write the failing test**

```rust
// add to the #[cfg(test)] mod in crates/hippo-daemon/src/install.rs
#[test]
fn render_plist_substitutes_vault_interval() {
    let vars = test_plist_vars();   // existing helper in this module; if absent, build PlistVars
    let tmpl = "<integer>__VAULT_POLL_INTERVAL_SECS__</integer>";
    let out = render_plist(tmpl, &vars);
    assert!(!out.contains("__VAULT_POLL_INTERVAL_SECS__"));
}
```

> If `test_plist_vars()` does not exist, look at how the existing `render_plist`
> tests build a `PlistVars` and replicate that; add `vault_poll_interval_secs`
> to `PlistVars` mirroring `cursor_poll_interval_secs`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/carpenter/projects/hippo && cargo test -p hippo-daemon render_plist_substitutes_vault_interval`
Expected: FAIL — placeholder not replaced (still present)

- [ ] **Step 3: Create the plist and wire the substitution**

Create `launchd/com.hippo.vault-sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hippo.vault-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>__HIPPO_BIN__</string>
        <string>export</string>
        <string>vault</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>__HOME__</string>
        <key>PATH</key>
        <string>__PATH__</string>
    </dict>
    <key>StartInterval</key><integer>__VAULT_POLL_INTERVAL_SECS__</integer>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key>
    <string>__DATA_DIR__/vault-sync.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>__DATA_DIR__/vault-sync.stderr.log</string>
    <key>WorkingDirectory</key>
    <string>__HOME__</string>
</dict>
</plist>
```

In `crates/hippo-daemon/src/install.rs`:

```rust
// 1. add the field to PlistVars (mirror cursor_poll_interval_secs):
    pub vault_poll_interval_secs: String,

// 2. add the replace in render_plist (beside __CURSOR_POLL_INTERVAL_SECS__):
        .replace("__VAULT_POLL_INTERVAL_SECS__", &vars.vault_poll_interval_secs)

// 3. populate it where PlistVars is constructed from config (mirror cursor):
//    vault_poll_interval_secs: config.vault.poll_interval_secs.to_string(),

// 4. install the plist only when vault.enabled, beside the cursor install call:
    if config.vault.enabled {
        install_plist(
            "com.hippo.vault-sync",
            include_str!("../../../launchd/com.hippo.vault-sync.plist"),
            &vars,
        )?;
    } else {
        remove_plist("com.hippo.vault-sync").ok();
    }
```

> Match the exact construction pattern already used for `com.hippo.cursor-session`
> in this file; the four edits above mirror it. Find that block with
> `rg -n "cursor-session" crates/hippo-daemon/src/install.rs`.

- [ ] **Step 4: Run test + clippy**

Run: `cargo test -p hippo-daemon render_plist_substitutes_vault_interval && cargo clippy -p hippo-daemon --all-targets -- -D warnings`
Expected: PASS + clean

- [ ] **Step 5: Commit**

```bash
cd /Users/carpenter/projects/hippo
cargo fmt
git add launchd/com.hippo.vault-sync.plist crates/hippo-daemon/src/install.rs
git commit -m "feat(vault): com.hippo.vault-sync LaunchAgent + install wiring"
```

---

### Task 14: `hippo doctor` vault check

**Files:**
- Modify: `crates/hippo-daemon/src/commands.rs` (doctor checks)

- [ ] **Step 1: Write the failing test**

```rust
// add to the #[cfg(test)] mod in crates/hippo-daemon/src/commands.rs
#[test]
fn vault_doctor_check_skips_when_disabled() {
    let config = HippoConfig::default();           // vault.enabled = false
    let line = vault_doctor_check(&config);
    assert!(line.contains("[--]"));                // skipped severity
    assert!(line.to_lowercase().contains("vault"));
}

#[test]
fn vault_doctor_check_warns_when_enabled_but_missing() {
    let dir = tempfile::TempDir::new().unwrap();
    let mut config = HippoConfig::default();
    config.vault.enabled = true;
    config.vault.out = Some(dir.path().join("does-not-exist").to_string_lossy().to_string());
    let line = vault_doctor_check(&config);
    assert!(line.contains("[WW]") || line.contains("[!!]"));
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/carpenter/projects/hippo && cargo test -p hippo-daemon vault_doctor_check`
Expected: FAIL — `vault_doctor_check` not found

- [ ] **Step 3: Implement the check + register it in `handle_doctor`**

```rust
// crates/hippo-daemon/src/commands.rs
pub fn vault_doctor_check(config: &HippoConfig) -> String {
    if !config.vault.enabled {
        return "[--] vault export: disabled".to_string();
    }
    let out = resolve_vault_out(config, None);
    let meta = std::path::Path::new(&out).join("_vault_meta.json");
    if !meta.exists() {
        return format!("[WW] vault export: enabled but no vault at {out} (run `hippo export vault`)");
    }
    match std::fs::metadata(&meta).and_then(|m| m.modified()) {
        Ok(modified) => {
            let age = modified.elapsed().map(|d| d.as_secs() / 60).unwrap_or(0);
            format!("[OK] vault export: last synced {age}m ago at {out}")
        }
        Err(e) => format!("[!!] vault export: cannot stat {}: {e}", meta.display()),
    }
}
```

```rust
// in handle_doctor(...), where the other check lines are printed, add:
    println!("{}", vault_doctor_check(config));
```

> Match the existing doctor output style: find how other check lines are
> printed with `rg -n "\\[OK\\]|\\[WW\\]|\\[!!\\]|\\[--\\]" crates/hippo-daemon/src/commands.rs`
> and align the format (and whether checks return a struct vs. print directly).
> If doctor accumulates a fail count, a `[WW]`/`[!!]` here must feed that count.

- [ ] **Step 4: Run test + clippy**

Run: `cargo test -p hippo-daemon vault_doctor_check && cargo clippy -p hippo-daemon --all-targets -- -D warnings`
Expected: PASS + clean

- [ ] **Step 5: Commit**

```bash
cd /Users/carpenter/projects/hippo
cargo fmt
git add crates/hippo-daemon/src/commands.rs
git commit -m "feat(vault): hippo doctor vault-export check"
```

---

### Task 15: Documentation

**Files:**
- Create: `docs/vault-export.md`
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: Write `docs/vault-export.md`**

Cover: what it is (one-way SQLite→markdown projection), `hippo export vault [--out] [--full]`, the `[vault]` config knobs, the `com.hippo.vault-sync` service (and that interval/path changes need `hippo daemon install --force`), the trust boundary (plaintext on disk, default `~/.local/share/hippo/vault`, `.gitignore`, edits discarded), the slug scheme, and the bounded-related semantics (hub_degree_cap, related_top_k). Link the spec.

- [ ] **Step 2: Add a pointer in `README.md`**

Under the feature list / CLI section, add one line: `hippo export vault — project the knowledge base into an Obsidian vault (see docs/vault-export.md)`.

- [ ] **Step 3: Add an architecture note to `CLAUDE.md`**

Add a short `### Obsidian Vault Export` subsection under Architecture mirroring the other source subsections: the brain `POST /vault/export` endpoint, `vault_export.py`/`vault_render.py`/`vault_edges.py`, source-key slug, full-reconcile sync, `com.hippo.vault-sync`, and the spec path.

- [ ] **Step 4: Verify links resolve**

Run: `cd /Users/carpenter/projects/hippo && rg -n "vault-export.md|vault/export" README.md CLAUDE.md docs/vault-export.md`
Expected: references present and consistent.

- [ ] **Step 5: Commit**

```bash
cd /Users/carpenter/projects/hippo
git add docs/vault-export.md README.md CLAUDE.md
git commit -m "docs(vault): vault export reference + README/CLAUDE pointers"
```

---

## Final verification (run before opening the PR)

- [ ] **Full Python suite:** `brain/.venv/bin/python -m pytest brain/tests/ -q` → all pass
- [ ] **Python lint/format:** `uv run --project brain ruff check brain/ && uv run --project brain ruff format --check brain/` → clean
- [ ] **Rust tests:** `cargo test` → all pass
- [ ] **Rust lint/format:** `cargo clippy --all-targets -- -D warnings && cargo fmt --check` → clean
- [ ] **Manual smoke (real DB):**
  ```bash
  cargo build --release
  # enable [vault] in config, ensure brain is running
  ./target/release/hippo export vault --out /tmp/hippo-vault-smoke
  ls /tmp/hippo-vault-smoke/knowledge/*/ | head
  # open /tmp/hippo-vault-smoke in Obsidian; confirm graph edges + backlinks render
  ```
- [ ] **Redaction spot-check:** `rg -n "sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}" /tmp/hippo-vault-smoke` → no hits

---

## Self-Review notes (spec coverage)

- Stable source-key slug → Task 1; multi-session min + change_outcome discriminator covered.
- Non-JSON content / full node_type & outcome vocab / design-decisions `|` → Task 2.
- Entity hub pages with member lists / churn-free (no last_seen) → Task 3.
- Bounded root + sub-index MOCs / sharding → Task 4.
- Rarity-weighted hub-excluded top-K related → Task 5.
- AP-6 probe filter at export surface → Task 6.
- Atomic write-changed + orphan-GC reconcile (handles deletions & re-mint orphans) → Task 7.
- Foreign-vault guard / .gitignore / banner / _vault_meta.json + format guard → Tasks 2,3 (banner), 8.
- Export-time redaction pass → Task 9.
- Brain HTTP endpoint → Task 10.
- `[vault]` config → Task 11.
- `hippo export vault` (brain HTTP, not uv subprocess) → Task 12.
- `com.hippo.vault-sync` LaunchAgent + install → Task 13.
- Doctor visibility → Task 14.
- Docs/discoverability → Task 15.
- Deferred to a follow-up (spec §15): resolve `## Sources` session ids to openable paths; mega-hub pagination beyond a flat cap; optional `graph.json` sidecar.
