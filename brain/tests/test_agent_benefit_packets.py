import copy
import json
import subprocess

import pytest

from hippo_brain.bench.agent_benefit_packets import adjudicate, audit, file_hash, prepare
from hippo_brain.jev import digest


def test_packet_and_labels_fail_closed_on_real_evidence_defects(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.rs").write_text("serve(DenyAllAttestor);\n")
    (repo / "README.md").write_text("pre-implementation\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "Fixture",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    task = tmp_path / "task.txt"
    task.write_text("Inspect implementation and distinguish deployment.\n")
    answers = {}
    for name in ("control", "treatment"):
        p = tmp_path / (name + ".txt")
        p.write_text("Source implements serving; deployment is unverified.\n")
        answers[name] = p
    rubric = {
        "version": "v2",
        "items": [{"id": "implementation"}],
        "source_assertions": [{"path": "main.rs", "contains": "DenyAllAttestor"}],
    }
    out = tmp_path / "packet"
    kwargs = dict(
        inputs={"task.txt": task},
        expected_hashes={"task.txt": file_hash(task)},
        submissions=answers,
        rubric=rubric,
        required_files=["main.rs"],
        missing=[],
        key_path=tmp_path / "key.json",
        seed="one",
    )
    evidence = prepare(repo, commit, out, **kwargs)
    assert evidence["complete_for_adjudication"]
    manifest = json.loads((out / "manifest.json").read_text())
    identity = digest(
        [manifest["packet_hash"], manifest["rubric_hash"], "task-adjudication-v2", "A"]
    )
    registry = {
        name: {
            "kind": "AI",
            "independent": True,
            "prior_exposure": False,
            "arm_access": False,
            "provenance_file": str(task),
            "provenance_sha256": file_hash(task),
        }
        for name in ("bob", "carlos")
    }
    labels = [
        {
            "reviewer_id": name,
            "submission": "A",
            "reviewer_kind": "AI",
            "evaluation_id": identity,
            "judgments": {
                "implementation": {
                    "status": "pass",
                    "rationale": "Frozen source implements serving despite README",
                    "citations": ["source/main.rs"],
                }
            },
        }
        for name in registry
    ]
    result = adjudicate(out, labels, registry, acceptance=True)
    assert result["task_success"] is True
    assert result["human_qualified"] is False
    assert result["acceptance_qualified"] is False
    mislabeled = copy.deepcopy(labels)
    mislabeled[0]["reviewer_kind"] = "human"
    with pytest.raises(ValueError, match="external registry"):
        adjudicate(out, mislabeled, registry)
    defective = copy.deepcopy(labels)
    for label in defective:
        label["judgments"]["implementation"]["status"] = "contradiction"
    assert adjudicate(out, defective, registry)["task_success"] is False
    labels[0]["judgments"]["implementation"]["status"] = "missing_evidence"
    assert adjudicate(out, labels, registry)["task_success"] is None
    labels[0]["evaluation_id"] = "old"
    with pytest.raises(ValueError, match="changed evidence"):
        adjudicate(out, labels, registry)
    (out / "source/main.rs").write_text("broken\n")
    with pytest.raises(ValueError, match="modified evidence"):
        audit(out)
    (out / "source/main.rs").unlink()
    with pytest.raises(ValueError, match="omitted"):
        audit(out)
    task.write_text("truncated")
    with pytest.raises(ValueError, match="truncated"):
        prepare(repo, commit, tmp_path / "other", **kwargs)
