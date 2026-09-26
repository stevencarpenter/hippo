"""Source fidelity packets preserve missing evidence and invalidate revisions."""

import json
import sqlite3
import errno
from contextlib import contextmanager

import pytest
import httpx

from hippo_brain.bench.claim_packets import (
    load_packets,
    make_packet,
    prepare,
    validate_packet,
    write_artifact,
)
from tests.retrieval_fixtures import TRUST_EVAL_SCHEMA


def database(path):
    conn = sqlite3.connect(path)
    conn.executescript(TRUST_EVAL_SCHEMA)
    conn.execute("ALTER TABLE events ADD COLUMN stdout TEXT")
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at) VALUES(1,'node',?,'',1)",
        (json.dumps({"summary": "The test passed."}),),
    )
    conn.execute(
        "INSERT INTO events(id,timestamp,command,cwd,stdout) VALUES(1,1,'test','/project','PASS')"
    )
    conn.execute("INSERT INTO knowledge_node_events VALUES(1,1)")
    conn.commit()
    return conn


def test_export_is_readonly_private_frozen_and_keeps_original_evidence(tmp_path):
    source = tmp_path / "source.sqlite"
    conn = database(source)
    conn.close()
    before = source.read_bytes()
    out = tmp_path / "packets"
    manifest = prepare(source, out)
    packets = load_packets(out)
    assert manifest["count"] == 1 and manifest["blocked"] == 0
    assert packets[0]["state"]["sources"][0]["fields"]["stdout"] == "PASS"
    assert source.read_bytes() == before
    assert out.stat().st_mode & 0o777 == 0o700
    assert (out / "packets.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        prepare(source, out)
    packets[0]["state"]["claim"] = "changed"
    (out / "packets.json").write_text(json.dumps(packets))
    with pytest.raises(ValueError, match="manifest mismatch"):
        load_packets(out)


def test_corrections_and_unavailable_sources_change_identity(tmp_path, monkeypatch):
    with database(tmp_path / "source.sqlite") as conn:
        first = make_packet(conn, 1)
        conn.execute("UPDATE events SET stdout='Correction: FAIL'")
        assert make_packet(conn, 1)["packet_hash"] != first["packet_hash"]
        conn.execute("UPDATE events SET probe_tag='probe'")
        monkeypatch.setenv("HIPPO_RETRIEVAL_INCLUDE_EXCLUDED", "1")
        packet = make_packet(conn, 1)
        assert packet["state"]["sources"] == []
        assert packet["problems"] == ["unavailable_source:shell-1"]
        conn.execute("DELETE FROM knowledge_node_events")
        assert make_packet(conn, 1)["problems"] == ["no_linked_sources"]


@pytest.mark.parametrize("legacy_version", ["claim-packets-v1", "claim-packets-v2"])
def test_legacy_packets_require_reexport(tmp_path, legacy_version):
    from hippo_brain.jev import digest

    with database(tmp_path / "source.sqlite") as conn:
        packet = make_packet(conn, 1)
    packet["version"] = legacy_version
    packet["packet_hash"] = digest({k: v for k, v in packet.items() if k != "packet_hash"})
    with pytest.raises(ValueError, match="unsupported claim packet"):
        validate_packet(packet)
    validate_packet(packet, allow_legacy=True)


def test_legacy_corpus_is_readable_only_for_historical_reporting(tmp_path):
    from hippo_brain.jev import digest

    source = tmp_path / "source.sqlite"
    with database(source):
        pass
    corpus = tmp_path / "corpus"
    prepare(source, corpus)
    packets = load_packets(corpus)
    packets[0]["version"] = "claim-packets-v2"
    packets[0]["packet_hash"] = digest(
        {key: value for key, value in packets[0].items() if key != "packet_hash"}
    )
    manifest = json.loads((corpus / "manifest.json").read_text())
    manifest["version"] = "claim-packets-v2"
    manifest["packets_hash"] = digest(packets)
    (corpus / "packets.json").write_text(json.dumps(packets))
    (corpus / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest mismatch"):
        load_packets(corpus)
    assert load_packets(corpus, allow_legacy=True) == packets


@pytest.mark.parametrize("raw", ['{"n": NaN}', '{"n": Infinity}', "{broken"])
def test_invalid_json_evidence_blocks_without_aborting_export(tmp_path, raw):
    with database(tmp_path / "source.sqlite") as conn:
        conn.execute("ALTER TABLE events ADD COLUMN raw_json TEXT")
        conn.execute("UPDATE events SET raw_json=?", (raw,))
        packet = make_packet(conn, 1)
    assert "invalid_json_field:shell-1:raw_json" in packet["problems"]


def test_truncation_and_redaction_are_visible(tmp_path):
    with database(tmp_path / "source.sqlite") as conn:
        conn.execute("UPDATE events SET stdout=?", ("x" * 6001,))
        packet = make_packet(conn, 1)
        assert packet["problems"] == ["truncated_field:shell-1:stdout"]
        conn.execute(
            "UPDATE events SET stdout='Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456'"
        )
        packet = make_packet(conn, 1)
        assert "abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(packet)


def test_agentic_packet_retains_attribution_and_correction(tmp_path):
    with database(tmp_path / "source.sqlite") as conn:
        conn.execute("DELETE FROM knowledge_node_events")
        conn.execute(
            "INSERT INTO agentic_sessions(id,start_time,end_time,cwd,summary_text) "
            "VALUES(2,1,2,'/project','Assistant: tests passed. User: correction, they failed.')"
        )
        conn.execute("INSERT INTO knowledge_node_agentic_sessions VALUES(1,2)")
        packet = make_packet(conn, 1)
        assert packet["problems"] == []
        evidence = packet["state"]["sources"][0]
        assert evidence["ref"] == "claude-2"
        assert "User: correction" in evidence["fields"]["summary_text"]


def test_output_rejects_repository_and_overwrite(tmp_path):
    from pathlib import Path

    with pytest.raises(ValueError, match="repository"):
        write_artifact(Path(__file__).parent / "claim-output.json", {})
    target = tmp_path / "once.json"
    write_artifact(target, {})
    with pytest.raises(FileExistsError):
        write_artifact(target, {})


def test_output_rejects_another_git_checkout(tmp_path):
    import subprocess

    other_repo = tmp_path / "other-repo"
    subprocess.run(["git", "init", "-q", str(other_repo)], check=True)
    with pytest.raises(ValueError, match="repository"):
        write_artifact(other_repo / "private-review.json", {"synthetic": "evidence"})
    assert not (other_repo / "private-review.json").exists()


@pytest.mark.parametrize("stdout,stderr", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_capture_truncation_is_preserved_and_blocks_assessment(tmp_path, stdout, stderr):
    with database(tmp_path / "source.sqlite") as conn:
        conn.executescript(
            "ALTER TABLE events ADD COLUMN stdout_truncated INTEGER;"
            "ALTER TABLE events ADD COLUMN stderr_truncated INTEGER;"
        )
        conn.execute("UPDATE events SET stdout_truncated=?, stderr_truncated=?", (stdout, stderr))
        packet = make_packet(conn, 1)
        fields = packet["state"]["sources"][0]["fields"]
        assert fields["stdout_truncated"] == stdout
        assert fields["stderr_truncated"] == stderr
        assert bool(packet["problems"]) == bool(stdout or stderr)


def test_workflow_annotations_are_evidence_and_invalidate_revisions(tmp_db):
    conn, _ = tmp_db
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at) VALUES(1,'workflow',?,'',1)",
        (json.dumps({"summary": "Ruff reported F821 in app.py at line 4."}),),
    )
    conn.execute(
        "INSERT INTO workflow_runs(id,repo,head_sha,event,status,html_url,raw_json,first_seen_at,last_seen_at) "
        "VALUES(1,'org/repo','abc','push','completed','https://example.test/run','{}',1,1)"
    )
    conn.execute("INSERT INTO knowledge_node_workflow_runs VALUES(1,1)")
    conn.execute(
        "INSERT INTO workflow_jobs(id,run_id,name,status,raw_json) VALUES(1,1,'lint','completed','{}')"
    )
    conn.execute(
        "INSERT INTO workflow_annotations(job_id,level,tool,rule_id,path,start_line,message) "
        "VALUES(1,'failure','ruff','F821','app.py',4,'undefined name x')"
    )
    packet = make_packet(conn, 1)
    assert not packet["problems"]
    fields = packet["state"]["sources"][0]["fields"]
    annotation = fields["annotations_json"][0]
    assert (annotation["rule_id"], annotation["path"], annotation["start_line"]) == (
        "F821",
        "app.py",
        4,
    )
    conn.execute("UPDATE workflow_annotations SET rule_id='F822', message='corrected diagnostic'")
    assert make_packet(conn, 1)["packet_hash"] != packet["packet_hash"]
    conn.execute("UPDATE workflow_annotations SET message=?", ("x" * 6001,))
    assert make_packet(conn, 1)["problems"]
    conn.execute("UPDATE workflow_annotations SET message='diagnostic'")
    conn.executemany(
        "INSERT INTO workflow_annotations(job_id,level,message) VALUES(1,'failure',?)",
        [(f"diagnostic {i}",) for i in range(100)],
    )
    assert "truncated_annotations:workflow-1" in make_packet(conn, 1)["problems"]
    conn.execute("DROP TABLE workflow_annotations")
    assert "missing_workflow_annotations:workflow-1" in make_packet(conn, 1)["problems"]


def test_shell_enrichment_qualifiers_survive_export(tmp_db):
    conn, _ = tmp_db
    conn.execute(
        "INSERT INTO sessions(id,start_time,shell,hostname,username) VALUES(1,1,'zsh','test','test')"
    )
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at) VALUES(1,'shell',?,'',1)",
        (
            json.dumps(
                {"summary": "Claude ran cargo test on commit abc1234 in org/repo in 250 ms."}
            ),
        ),
    )
    conn.execute(
        "INSERT INTO events(id,session_id,timestamp,command,stdout,exit_code,duration_ms,cwd,hostname,shell,git_repo,git_commit,source_kind,tool_name) "
        "VALUES(1,1,1,'cargo test','ok',0,250,'/work','test','claude','org/repo','abc1234','claude-tool','Bash')"
    )
    conn.execute("INSERT INTO knowledge_node_events VALUES(1,1)")
    packet = make_packet(conn, 1)
    fields = packet["state"]["sources"][0]["fields"]
    assert packet["problems"] == []
    assert {
        key: fields[key]
        for key in ("shell", "source_kind", "tool_name", "duration_ms", "git_commit", "git_repo")
    } == {
        "shell": "claude",
        "source_kind": "claude-tool",
        "tool_name": "Bash",
        "duration_ms": 250,
        "git_commit": "abc1234",
        "git_repo": "org/repo",
    }


def test_browser_enrichment_qualifiers_survive_export(tmp_db):
    conn, _ = tmp_db
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at) VALUES(1,'browser',?,'',1)",
        (json.dumps({"summary": "Searched for checkpoint recovery and read the page."}),),
    )
    conn.execute(
        "INSERT INTO browser_events(id,timestamp,url,title,domain,dwell_ms,scroll_depth,search_query) "
        "VALUES(1,1,'https://example.test','Reference','example.test',45000,0.8,'wal checkpoint recovery')"
    )
    conn.execute("INSERT INTO knowledge_node_browser_events VALUES(1,1)")
    packet = make_packet(conn, 1)
    fields = packet["state"]["sources"][0]["fields"]
    assert packet["problems"] == []
    assert (
        fields["domain"],
        fields["dwell_ms"],
        fields["scroll_depth"],
        fields["search_query"],
    ) == ("example.test", 45000, 0.8, "wal checkpoint recovery")


def test_opencode_enrichment_qualifiers_survive_export(tmp_db):
    conn, _ = tmp_db
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at) VALUES(1,'opencode',?,'',1)",
        (json.dumps({"summary": "The fix changed two files and was committed."}),),
    )
    conn.execute(
        "INSERT INTO agentic_sessions(id,session_id,harness,project_dir,cwd,start_time,end_time,summary_text,message_count,snapshot_diffs_json,commit_messages_json,agent,model,slug) "
        "VALUES(1,'s','opencode','project','/work',1,2,'Worked on a fix',3,?,?,?,?,?)",
        (
            json.dumps({"additions": 12, "deletions": 3, "files": 2}),
            json.dumps(["fix: repair checkpoint replay"]),
            "builder",
            "local",
            "fix",
        ),
    )
    conn.execute("INSERT INTO knowledge_node_agentic_sessions VALUES(1,1)")
    packet = make_packet(conn, 1)
    fields = packet["state"]["sources"][0]["fields"]
    assert packet["problems"] == []
    assert fields["snapshot_diffs_json"] == {"additions": 12, "deletions": 3, "files": 2}
    assert fields["commit_messages_json"] == ["fix: repair checkpoint replay"]
    assert (fields["agent"], fields["model"], fields["slug"], fields["message_count"]) == (
        "builder",
        "local",
        "fix",
        3,
    )


def test_failed_write_does_not_publish_partial_json(tmp_path, monkeypatch):
    from hippo_brain.bench import claim_packets
    import tempfile

    first, second = tmp_path / "first.json", tmp_path / "second.json"
    write_artifact(first, {"completed": True})
    original = tempfile.NamedTemporaryFile

    @contextmanager
    def failing_file(*args, **kwargs):
        with original(*args, **kwargs) as stream:

            class InterruptedStream:
                name = stream.name

                def write(self, body):
                    stream.write(body[:3])
                    stream.flush()
                    raise OSError(errno.ENOSPC, "synthetic disk full")

            yield InterruptedStream()

    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "NamedTemporaryFile", failing_file)
        with pytest.raises(OSError, match="synthetic disk full"):
            claim_packets.write_artifact(second, {"completed": False})
    assert json.loads(first.read_text()) == {"completed": True}
    assert not second.exists()
    assert list(tmp_path.iterdir()) == [first]
    write_artifact(second, {"retried": True})
    assert json.loads(second.read_text()) == {"retried": True}


@pytest.mark.parametrize("historical", [False, True])
async def test_ingested_and_historical_credentials_never_reach_http(tmp_db, tmp_path, historical):
    from hippo_brain.claude_sessions import SessionSegment, insert_segment
    from hippo_brain.jev import JevClient, canonical
    from pathlib import Path

    conn, path = tmp_db
    credential = "synthetic_sensitive_credential-927483"
    prompt = json.dumps({"headers": {"Authorization": f"Bearer {credential}"}, "method": "GET"})
    segment = SessionSegment(
        session_id="credential-test",
        project_dir="/project",
        cwd="/project",
        git_branch="main",
        segment_index=0,
        start_time=1,
        end_time=2,
        user_prompts=[prompt],
        tool_calls=[{"name": "http", "summary": json.dumps(prompt)}],
        message_count=5,
        source_file="/project/session.jsonl",
    )
    source_id = insert_segment(conn, segment)
    assert credential not in canonical(
        conn.execute(
            "SELECT summary_text,user_prompts_json,tool_calls_json FROM agentic_sessions WHERE id=?",
            (source_id,),
        ).fetchone()
    )
    if historical:
        conn.execute(
            "UPDATE agentic_sessions SET user_prompts_json=? WHERE id=?",
            (json.dumps([prompt]), source_id),
        )
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at) VALUES(1,'credential',?,'',1)",
        (json.dumps({"summary": "The user configured request headers."}),),
    )
    conn.execute("INSERT INTO knowledge_node_agentic_sessions VALUES(1,?)", (source_id,))
    conn.commit()
    prepare(path, tmp_path / "packets")
    packet = load_packets(tmp_path / "packets")[0]
    assert not packet["problems"]
    assert credential not in canonical(packet)
    assert isinstance(packet["state"]["sources"][0]["fields"]["user_prompts_json"], list)
    seen = []

    def handle(request):
        seen.append(request.content)
        assert credential.encode() not in request.content
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "verdict": {
                        "type": "choice",
                        "choice": "unsupported",
                        "confidence": 1.0,
                        "probabilities": {"supports": 0, "contradicts": 0, "unsupported": 1},
                    }
                },
            },
        )

    questions = json.loads(
        (
            Path(__file__).parents[1] / "src/hippo_brain/_fixtures/claim_review_rubric.json"
        ).read_text()
    )["questions"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        async with JevClient("synthetic", client=http) as client:
            await client.assess(packet["state"], questions)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "key,prefix", [("api_key", ""), ("password", ""), ("Authorization", "Token ")]
)
async def test_nested_credentials_never_reach_assessment(tmp_db, tmp_path, key, prefix):
    from hippo_brain.claude_sessions import SessionSegment, insert_segment
    from hippo_brain.jev import JevClient
    from pathlib import Path

    conn, path = tmp_db
    secret = "synthetic_sensitive_credential-927483"
    nested = json.dumps({key: prefix + secret, "public": "keep"})
    segment = SessionSegment(
        session_id="nested-credential",
        project_dir="/project",
        cwd="/project",
        git_branch="main",
        segment_index=0,
        start_time=1,
        end_time=2,
        user_prompts=[json.dumps({"body": nested})],
        tool_calls=[{"name": "http", "summary": json.dumps(nested)}],
        message_count=5,
        source_file="/project/session.jsonl",
    )
    source_id = insert_segment(conn, segment)
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at) VALUES(1,'credential',?,'',1)",
        (json.dumps({"summary": "The user configured request headers."}),),
    )
    conn.execute("INSERT INTO knowledge_node_agentic_sessions VALUES(1,?)", (source_id,))
    conn.commit()
    prepare(path, tmp_path / "packets")
    packet = load_packets(tmp_path / "packets")[0]
    assert not packet["problems"]
    assert secret not in json.dumps(packet)
    assert "keep" in json.dumps(packet["state"])

    seen = []

    def handle(request):
        seen.append(request.content)
        assert secret.encode() not in request.content
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "verdict": {
                        "type": "choice",
                        "choice": "unsupported",
                        "confidence": 1.0,
                        "probabilities": {"supports": 0, "contradicts": 0, "unsupported": 1},
                    }
                },
            },
        )

    questions = json.loads(
        (
            Path(__file__).parents[1] / "src/hippo_brain/_fixtures/claim_review_rubric.json"
        ).read_text()
    )["questions"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        async with JevClient("synthetic", client=http) as client:
            await client.assess(packet["state"], questions)
    assert len(seen) == 1
