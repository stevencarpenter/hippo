"""HIPO-10 synthetic identity acceptance. No installed DB, model, or source probe."""

import json
import runpy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hippo_brain.claude_sessions import write_claude_knowledge_node
from hippo_brain.enrichment import write_knowledge_node
from hippo_brain.mcp_queries import search_knowledge_lexical
from hippo_brain.models import EnrichmentResult
from hippo_brain.opencode_sessions import write_opencode_knowledge_node
from hippo_brain.projects import (
    attribute_node,
    backfill,
    canonical_name,
    infer_project,
    load_aliases,
    normalize,
    project_clause,
    query_terms,
)
from hippo_brain.retrieval import Filters, Tuning, _apply_filters, search

SNUG_ALIASES = {
    alias: "snugmarina"
    for alias in ("SNUG", "SNUG project", "SNUG-23", "snug-31", "snugmarina-base")
}


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HIPPO_PROJECT_ROOTS", "/projects/foo:/projects/bar")
    return tmp_path


def set_aliases(path, aliases):
    config = path / "hippo" / "config.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        "[projects.aliases]\n"
        + "\n".join(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in aliases.items())
    )


def session(conn, directory, harness="claude-code", start=1000, end=2000):
    sid = conn.execute(
        "INSERT INTO agentic_sessions(session_id,harness,project_dir,cwd,start_time,end_time,"
        "summary_text,message_count,source_file) VALUES(?,?,?,?,?,?,?,10,?) RETURNING id",
        (
            directory,
            harness,
            directory,
            directory,
            start,
            end,
            "Project work",
            "/synthetic/session.jsonl",
        ),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO agentic_enrichment_queue(session_id,enqueued_at,updated_at) VALUES(?,1000,1000)",
        (sid,),
    )
    return sid


def event(conn, repo, cwd="/synthetic", timestamp=1500):
    conn.execute(
        "INSERT OR IGNORE INTO sessions(id,start_time,shell,hostname,username) VALUES(1,1,'zsh','test','test')"
    )
    eid = conn.execute(
        "INSERT INTO events(session_id,timestamp,command,duration_ms,cwd,hostname,shell,git_repo) "
        "VALUES(1,?,'cargo test',100,?,'test','zsh',?) RETURNING id",
        (timestamp, cwd, repo),
    ).fetchone()[0]
    conn.execute("INSERT INTO enrichment_queue(event_id) VALUES(?)", (eid,))
    return eid


def result(text="Stopped lumin project", entities=None):
    return EnrichmentResult(
        summary=text,
        intent="development",
        outcome="success",
        entities=entities or {},
        tags=[],
        embed_text=text,
    )


def existing_node(conn, sid):
    nid = conn.execute(
        "INSERT INTO knowledge_nodes(uuid,content,embed_text) VALUES(?, '{}', 'source') RETURNING id",
        (f"node-{sid}",),
    ).fetchone()[0]
    conn.execute("INSERT INTO knowledge_node_agentic_sessions VALUES(?,?)", (nid, sid))
    return nid


def project_name(conn, nid):
    row = conn.execute(
        "SELECT p.canonical_name FROM knowledge_nodes n JOIN projects p ON p.id=n.project_id WHERE n.id=?",
        (nid,),
    ).fetchone()
    return row[0] if row else None


@pytest.mark.parametrize(
    "value",
    [
        "dotfiles",
        ".dotfiles",
        "stevencarpenter/dotfiles",
        "sjcarpenter/.dotfiles",
        "https://github.com/owner/.dotfiles.git",
        "git@github.com:owner/dotfiles.git",
    ],
)
def test_owner_dot_and_remote_normalization(value):
    assert normalize(value) == "dotfiles"


def test_directory_basename_and_config_validation(isolated_config):
    assert normalize("/projects/foo/brain", directory=True) == "brain"
    assert normalize("/projects/bar/brain", directory=True) == "brain"
    assert normalize("/projects/foo/.claude/worktrees/agent-a", directory=True) == "foo"
    assert normalize("/synthetic/.dotfiles", directory=True) == "dotfiles"
    with pytest.raises(ValueError, match="cycle"):
        canonical_name("one", {"one": "two", "two": "one"})
    set_aliases(isolated_config, {"snug": ""})
    with pytest.raises(ValueError, match="nonempty"):
        load_aliases()


def test_precedence_backfill_and_reruns_preserve_sources(tmp_db):
    conn, _ = tmp_db
    sid = session(conn, "/synthetic/local-directory")
    nid = existing_node(conn, sid)
    local = event(conn, "local-repo", cwd="/synthetic/local-directory")
    remote = event(
        conn, "https://github.com/owner/.remote-repo.git", cwd="/synthetic/local-directory"
    )
    conn.execute("INSERT INTO knowledge_node_events VALUES(?,?)", (nid, local))
    conn.execute("INSERT INTO knowledge_node_events VALUES(?,?)", (nid, remote))
    fallback = existing_node(conn, session(conn, "/synthetic/.fallback"))
    repo_only = existing_node(conn, session(conn, "/synthetic/directory", start=3000, end=4000))
    event(conn, "sjcarpenter/preferred", cwd="/synthetic/directory", timestamp=3500)
    sources = (
        conn.execute("SELECT * FROM events").fetchall(),
        conn.execute("SELECT * FROM agentic_sessions").fetchall(),
    )
    conn.commit()
    assert backfill(conn, {}) == 3
    assert project_name(conn, nid) == "remote-repo"
    assert project_name(conn, fallback) == "fallback"
    assert project_name(conn, repo_only) == "preferred"
    snapshot = (
        conn.execute("SELECT * FROM projects ORDER BY id").fetchall(),
        conn.execute("SELECT * FROM project_aliases ORDER BY alias").fetchall(),
    )
    backfill(conn, {})
    assert snapshot == (
        conn.execute("SELECT * FROM projects ORDER BY id").fetchall(),
        conn.execute("SELECT * FROM project_aliases ORDER BY alias").fetchall(),
    )
    assert sources == (
        conn.execute("SELECT * FROM events").fetchall(),
        conn.execute("SELECT * FROM agentic_sessions").fetchall(),
    )
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_configured_snug_ingest_query_parity(tmp_db, isolated_config):
    conn, _ = tmp_db
    set_aliases(isolated_config, SNUG_ALIASES)
    nodes = []
    for index, alias in enumerate(SNUG_ALIASES):
        writer = write_opencode_knowledge_node if index % 2 else write_claude_knowledge_node
        sid = session(
            conn, f"/synthetic/{alias}", harness="opencode" if index % 2 else "claude-code"
        )
        conn.commit()
        nodes.append(writer(conn, result("snugmarina implementation"), [sid], "synthetic-model"))
    assert {project_name(conn, nid) for nid in nodes} == {"snugmarina"}
    assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
    for alias in [*SNUG_ALIASES, "snugmarina"]:
        assert _apply_filters(conn, nodes, Filters(project=alias)) == set(nodes)
        assert len(search_knowledge_lexical(conn, "", project=alias)) == 5
        assert search_knowledge_lexical(conn, "absent-term", project=alias) == []
        hits = search(conn, alias, None, mode="lexical", tuning=Tuning())
        assert {hit.uuid for hit in hits} == {
            row[0] for row in conn.execute("SELECT uuid FROM knowledge_nodes")
        }


def test_config_edit_resolves_existing_ids_and_next_ingest(tmp_db, isolated_config):
    conn, _ = tmp_db
    sid = session(conn, "/synthetic/SNUG-23")
    conn.commit()
    first = write_claude_knowledge_node(conn, result("snug-23 work"), [sid], "synthetic")
    set_aliases(isolated_config, SNUG_ALIASES)
    assert _apply_filters(conn, [first], Filters(project="snugmarina")) == {first}
    sid2 = session(conn, "/synthetic/snug-31")
    conn.commit()
    second = write_claude_knowledge_node(conn, result("snug-31 work"), [sid2], "synthetic")
    assert _apply_filters(conn, [first, second], Filters(project="SNUG")) == {first, second}
    backfill(conn)
    assert project_name(conn, first) == project_name(conn, second) == "snugmarina"


@pytest.mark.parametrize(
    "aliases",
    [
        {"snug-23": "snugmarina"},
        {"owner/.snug-23.git": "owner/.snugmarina.git"},
        {"snug-23": "intermediate", "intermediate": "snugmarina"},
    ],
)
def test_canonical_target_inference_after_config_edit(tmp_db, isolated_config, aliases):
    conn, _ = tmp_db
    sid = session(conn, "/synthetic/snug-23")
    conn.commit()
    node = write_claude_knowledge_node(conn, result("snug-23 work"), [sid], "synthetic")
    set_aliases(isolated_config, aliases)
    conn.execute("PRAGMA query_only=ON")
    query = "explain snugmarina"
    assert infer_project(conn, query) == "snugmarina"
    assert [hit["summary"] for hit in search_knowledge_lexical(conn, query)] == ["snug-23 work"]
    assert [hit.summary for hit in search(conn, query, None, mode="lexical", tuning=Tuning())] == [
        "snug-23 work"
    ]
    assert project_name(conn, node) == "snug-23"
    assert [row[0] for row in conn.execute("SELECT canonical_name FROM projects")] == ["snug-23"]


def test_shell_writer_uses_captured_git_identity_not_model_entities(tmp_db):
    conn, _ = tmp_db
    eid = event(conn, "stevencarpenter/.dotfiles")
    conn.commit()
    nid = write_knowledge_node(
        conn, result("dotfiles change", {"projects": ["Lumin Digital"]}), [eid], "synthetic"
    )
    assert project_name(conn, nid) == "dotfiles"
    assert (
        conn.execute("SELECT git_repo FROM events WHERE id=?", (eid,)).fetchone()[0]
        == "stevencarpenter/.dotfiles"
    )


def test_cross_project_node_has_no_arbitrary_key(tmp_db):
    conn, _ = tmp_db
    first = session(conn, "/projects/foo")
    second = session(conn, "/projects/bar")
    nid = existing_node(conn, first)
    conn.execute("INSERT INTO knowledge_node_agentic_sessions VALUES(?,?)", (nid, second))
    assert attribute_node(conn, nid, {}) is None
    assert project_name(conn, nid) is None


def lumin_nodes(conn):
    project_sid = session(conn, "/synthetic/lumin")
    employer_sid = session(conn, "/synthetic/workplace")
    conn.commit()
    project = write_claude_knowledge_node(
        conn,
        result("Stopped lumin project because maintenance exceeded value", {"projects": ["lumin"]}),
        [project_sid],
        "synthetic",
    )
    employer = write_claude_knowledge_node(
        conn,
        result("Lumin Digital employer audit", {"services": ["Lumin Digital"]}),
        [employer_sid],
        "synthetic",
    )
    return project, employer


def test_lumin_project_and_employer_are_distinct_referents(tmp_db):
    conn, _ = tmp_db
    project, employer = lumin_nodes(conn)
    assert project_name(conn, project) == "lumin"
    assert project_name(conn, employer) == "workplace"
    assert infer_project(conn, "why did I stop lumin") == "lumin"
    assert infer_project(conn, "Lumin Digital employer audit") is None
    assert [
        hit.summary
        for hit in search(conn, "why did I stop lumin", None, mode="lexical", tuning=Tuning())
    ] == ["Stopped lumin project because maintenance exceeded value"]
    assert [hit["summary"] for hit in search_knowledge_lexical(conn, "why did I stop lumin")] == [
        "Stopped lumin project because maintenance exceeded value"
    ]
    assert [
        hit.summary for hit in search(conn, "Lumin Digital", None, mode="lexical", tuning=Tuning())
    ][0] == "Lumin Digital employer audit"


@pytest.mark.asyncio
async def test_ask_lumin_receives_only_project_evidence(tmp_db, monkeypatch):
    from hippo_brain import rag

    conn, _ = tmp_db
    lumin_nodes(conn)
    client = AsyncMock()
    client.base_url = "http://synthetic.invalid"
    client.health_check.return_value = {"ok": True}
    client.embed.return_value = [[0.0] * 768]
    client.chat.return_value = "Maintenance exceeded value."
    monkeypatch.setattr(rag, "capture_query", lambda *args, **kwargs: None)
    monkeypatch.setattr(rag, "retrieval_get_tuning", lambda: Tuning())
    response = await rag.ask(
        "why did I stop lumin", client, conn, "synthetic", "synthetic", conn=conn, mode="lexical"
    )
    assert [source["summary"] for source in response["sources"]] == [
        "Stopped lumin project because maintenance exceeded value"
    ]
    messages = client.chat.call_args.args[0]
    assert "Lumin Digital employer audit" not in json.dumps(messages)


def test_fragmentation_ratio_remains_informational(tmp_db, isolated_config):
    conn, _ = tmp_db
    exporter = runpy.run_path(str(Path(__file__).parents[2] / "scripts/hippo-metrics-exporter.py"))
    first = event(conn, "foo-bar", cwd="/synthetic/foo-bar")
    second = event(conn, "foobar", cwd="/synthetic/foobar")
    conn.commit()
    set_aliases(isolated_config, {"foo-bar": "foobar"})

    def metric():
        registry = exporter["Registry"]()
        exporter["_f_identity"](registry, exporter["_project_stats"](conn, set()))
        return exporter["render_prometheus"](registry)

    before = metric()
    node = write_knowledge_node(conn, result("foobar work"), [first, second], "synthetic")
    backfill(conn)
    assert project_name(conn, node) == "foobar"
    assert metric() == before
    value = next(
        line.split()[-1]
        for line in before.decode().splitlines()
        if line.startswith("hippo_kb_project_fragmentation_ratio ")
    )
    assert float(value) == 2.0


@pytest.mark.parametrize("source", ["session", "event", "memory", "workflow"])
def test_unassigned_history_survives_first_identity(tmp_db, isolated_config, source):
    from hippo_brain.auto_memory import ingest_memory_file, write_memory_knowledge_node

    conn, _ = tmp_db
    set_aliases(isolated_config, {"shortcut": "lumin"})
    if source == "memory":
        path = isolated_config / "MEMORY.md"
        path.write_text("# Lumin\n\nProject history.\n")
        ingested = ingest_memory_file(conn, path, repository="lumin", now_ms=1000)
        old = write_memory_knowledge_node(conn, result(), ingested.revision_id, "synthetic")
        conn.execute("UPDATE knowledge_nodes SET project_id=NULL WHERE id=?", (old,))
        conn.execute("DELETE FROM project_aliases")
        conn.execute("DELETE FROM projects")
    elif source == "session":
        old = existing_node(conn, session(conn, "/synthetic/lumin"))
    else:
        old = conn.execute(
            "INSERT INTO knowledge_nodes(uuid,content,embed_text) "
            "VALUES('historical', '{}', 'lumin') RETURNING id"
        ).fetchone()[0]
        if source == "event":
            eid = event(conn, "owner/lumin")
            conn.execute("INSERT INTO knowledge_node_events VALUES(?,?)", (old, eid))
        else:
            conn.execute(
                "INSERT INTO workflow_runs "
                "(id,repo,head_sha,head_branch,event,status,conclusion,html_url,raw_json,"
                "first_seen_at,last_seen_at) "
                "VALUES(1,'owner/lumin','abc','main','push','completed','success','x','{}',1,2)"
            )
            conn.execute("INSERT INTO knowledge_node_workflow_runs VALUES(?,1)", (old,))
    conn.commit()
    assert _apply_filters(conn, [old], Filters(project="shortcut")) == {old}
    new_sid = session(conn, "/new/lumin")
    conn.commit()
    new = write_claude_knowledge_node(conn, result(), [new_sid], "synthetic")
    conn.execute("PRAGMA query_only=ON")
    for project in ("lumin", "shortcut", "lum"):
        assert _apply_filters(conn, [old, new], Filters(project=project)) == {old, new}
        assert len(search_knowledge_lexical(conn, "", project=project)) == 2
    assert project_name(conn, old) is None


@pytest.mark.parametrize("writer", [write_claude_knowledge_node, write_opencode_knowledge_node])
def test_encoded_directory_uses_captured_cwd(tmp_db, isolated_config, writer):
    conn, _ = tmp_db
    set_aliases(isolated_config, {"local-lumin": "lumin"})
    old_sid = session(conn, "-Users-carpenter-projects-local-lumin")
    new_sid = session(conn, "-Users-carpenter-other-local-lumin")
    conn.execute("UPDATE agentic_sessions SET cwd='/synthetic/local-lumin'")
    before = conn.execute("SELECT project_dir,cwd FROM agentic_sessions").fetchall()
    old = existing_node(conn, old_sid)
    conn.commit()
    new = writer(conn, result(), [new_sid], "synthetic")
    assert project_name(conn, new) == "lumin"
    backfill(conn)
    assert project_name(conn, old) == "lumin"
    assert [row[0] for row in conn.execute("SELECT canonical_name FROM projects")] == ["lumin"]
    assert _apply_filters(conn, [old, new], Filters(project="local-lumin")) == {old, new}
    assert conn.execute("SELECT project_dir,cwd FROM agentic_sessions").fetchall() == before


def test_retargeted_alias_does_not_transfer_canonical_history(tmp_db, isolated_config):
    conn, _ = tmp_db
    set_aliases(isolated_config, {"shortcut": "alpha"})
    nodes = {}
    for name in ("shortcut", "alpha", "beta"):
        sid = session(conn, f"/synthetic/{name}")
        conn.commit()
        nodes[name] = write_claude_knowledge_node(conn, result(name), [sid], "synthetic")
    set_aliases(isolated_config, {"shortcut": "beta"})
    for project in ("shortcut", "beta"):
        assert _apply_filters(conn, list(nodes.values()), Filters(project=project)) == {
            nodes["beta"]
        }
        assert [hit["summary"] for hit in search_knowledge_lexical(conn, "", project=project)] == [
            "beta"
        ]
    assert _apply_filters(conn, list(nodes.values()), Filters(project="alpha")) == {
        nodes["alpha"],
        nodes["shortcut"],
    }
    sid = session(conn, "/new/shortcut")
    conn.commit()
    new = write_claude_knowledge_node(conn, result("beta"), [sid], "synthetic")
    assert project_name(conn, new) == "beta"
    assert _apply_filters(conn, [*nodes.values(), new], Filters(project="beta")) == {
        nodes["beta"],
        new,
    }
    assert infer_project(conn, "shortcut") == "beta"
    assert "shortcut" not in query_terms(conn, "alpha")
    assert "alpha" not in query_terms(conn, "beta")
    set_aliases(isolated_config, {})
    assert infer_project(conn, "shortcut") is None
    assert "shortcut" not in query_terms(conn, "alpha")
    assert _apply_filters(conn, list(nodes.values()), Filters(project="shortcut")) == {
        nodes["shortcut"]
    }


@pytest.mark.parametrize("alias", ["owner/snug-23", ".snug-23", "owner/.snug-23.git"])
def test_normalized_current_aliases_match_history_and_inference(tmp_db, isolated_config, alias):
    conn, _ = tmp_db
    set_aliases(isolated_config, {alias: "snugmarina"})
    old = existing_node(conn, session(conn, "/projects/snug-23"))
    conn.execute(
        "UPDATE knowledge_nodes SET content=?,embed_text=? WHERE id=?",
        (json.dumps({"summary": "snug-23 history"}), "snug-23 history", old),
    )
    sid = session(conn, "/new/snug-23")
    conn.commit()
    new = write_claude_knowledge_node(conn, result("snugmarina work"), [sid], "synthetic")
    assert project_name(conn, new) == "snugmarina"
    conn.execute("PRAGMA query_only=ON")
    for project in (alias, "snug-23", "snugmarina"):
        assert _apply_filters(conn, [old, new], Filters(project=project)) == {old, new}
        assert len(search_knowledge_lexical(conn, "", project=project)) == 2
    assert infer_project(conn, "explain snug-23") == "snugmarina"
    assert "snug-23" in query_terms(conn, "snugmarina")
    expected = {"snug-23 history", "snugmarina work"}
    assert {hit["summary"] for hit in search_knowledge_lexical(conn, "explain snug-23")} == expected
    assert {
        hit.summary
        for hit in search(conn, "explain snug-23", None, mode="lexical", tuning=Tuning())
    } == expected
    assert project_name(conn, old) is None


@pytest.mark.parametrize("source", ["event", "session"])
def test_unresolved_substrings_match_assigned_and_unassigned_nodes(tmp_db, source):
    conn, _ = tmp_db
    nodes = []
    for index in range(2):
        if source == "event":
            eid = event(conn, "owner/hippo")
            conn.commit()
            node = write_knowledge_node(conn, result("hippo work"), [eid], "synthetic")
        else:
            sid = session(conn, f"/location-{index}/owner/hippo")
            conn.commit()
            node = write_claude_knowledge_node(conn, result("hippo work"), [sid], "synthetic")
        nodes.append(node)
    conn.execute("UPDATE knowledge_nodes SET project_id=NULL WHERE id=?", (nodes[0],))
    conn.commit()
    conn.execute("PRAGMA query_only=ON")
    for project in ("hip", "owner/hip", "missing"):
        expected = set() if project == "missing" else set(nodes)
        assert _apply_filters(conn, nodes, Filters(project=project)) == expected
        assert len(search_knowledge_lexical(conn, "", project=project)) == len(expected)
    assert project_name(conn, nodes[0]) is None
    assert project_name(conn, nodes[1]) == "hippo"


def test_configured_alias_without_identity_does_not_use_unrestricted_substrings(
    tmp_db, isolated_config
):
    conn, _ = tmp_db
    sid = session(conn, "/projects/hippo")
    conn.commit()
    node = write_claude_knowledge_node(conn, result("hippo work"), [sid], "synthetic")
    set_aliases(isolated_config, {"hip": "unseen"})
    assert _apply_filters(conn, [node], Filters(project="hip")) == set()
    assert search_knowledge_lexical(conn, "", project="hip") == []


def test_older_readable_schema_keeps_legacy_behavior(tmp_db):
    conn, _ = tmp_db
    conn.execute("DROP TABLE project_aliases")
    conn.execute("DROP TABLE projects")
    assert attribute_node(conn, 1, {}) is None
    assert infer_project(conn, "lumin") is None
    assert project_clause(conn, "lumin") is None
