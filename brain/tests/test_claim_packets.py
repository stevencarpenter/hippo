"""Source fidelity packets preserve missing evidence and invalidate revisions."""

import json
import sqlite3

import pytest

from hippo_brain.bench.claim_packets import load_packets, make_packet, prepare, write_artifact
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
