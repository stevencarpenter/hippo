import copy
import json
import subprocess
from pathlib import Path

import pytest

from hippo_brain.bench.agent_benefit_packets import adjudicate, audit, file_hash, prepare
from hippo_brain.jev import digest


@pytest.fixture
def packet_reviews(tmp_path: Path) -> tuple[Path, list[dict], dict, dict]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.rs").write_text("serve(DenyAllAttestor);\n")
    (repo / "README.md").write_text("pre-implementation\n")
    (repo / "redacted.txt").write_text("password=synthetic-fixture-value\n")
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
    private_input = tmp_path / "private.txt"
    private_input.write_text("password=synthetic-fixture-value\n")
    answers = {}
    for name in ("control", "treatment"):
        p = tmp_path / (name + ".txt")
        p.write_text("Source implements serving; deployment is unverified.\n")
        answers[name] = p
    rubric = {
        "version": "v2",
        "items": [{"id": "implementation"}],
        "source_assertions": [
            {"path": "main.rs", "contains": "DenyAllAttestor"},
            {"path": "redacted.txt", "contains": "[REDACTED]"},
        ],
    }
    out = tmp_path / "packet"
    kwargs = dict(
        inputs={"task.txt": task, "private.txt": private_input},
        expected_hashes={"task.txt": file_hash(task), "private.txt": file_hash(private_input)},
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
    return out, labels, registry, kwargs


def test_packet_and_labels_fail_closed_on_real_evidence_defects(
    packet_reviews: tuple[Path, list[dict], dict, dict], tmp_path: Path
) -> None:
    out, labels, registry, kwargs = packet_reviews
    commit = json.loads((out / "manifest.json").read_text())["source_commit"]
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
    kwargs["inputs"]["task.txt"].write_text("truncated")
    with pytest.raises(ValueError, match="truncated"):
        prepare(tmp_path / "repo", commit, tmp_path / "other", **kwargs)


@pytest.mark.parametrize("rationale", [None, "", " \t\n", True, 1, ["explanation"], {"why": "yes"}])
def test_adjudication_requires_nonempty_string_rationale(
    packet_reviews: tuple[Path, list[dict], dict, dict], rationale: object
) -> None:
    out, labels, registry, _ = packet_reviews
    labels[0]["judgments"]["implementation"]["rationale"] = rationale
    with pytest.raises(ValueError, match="source-backed rationale required"):
        adjudicate(out, labels, registry)


@pytest.mark.parametrize(
    "citations",
    [None, "source/main.rs", {}, {"source/main.rs": 1}, [None], [1], [""], [" \t"]],
)
def test_adjudication_requires_citation_array_of_nonempty_strings(
    packet_reviews: tuple[Path, list[dict], dict, dict], citations: object
) -> None:
    out, labels, registry, _ = packet_reviews
    labels[0]["judgments"]["implementation"]["citations"] = citations
    with pytest.raises(ValueError, match="citations must be an array of nonempty strings"):
        adjudicate(out, labels, registry)


@pytest.mark.parametrize(
    "citations",
    [
        [],
        ["source/redacted.txt:1"],
        ["inputs/private.txt:1"],
        ["source/redacted.txt", "inputs/private.txt", "manifest.json", "answers/A.txt"],
    ],
)
def test_pass_requires_unredacted_source_or_input_support(
    packet_reviews: tuple[Path, list[dict], dict, dict], citations: list[str]
) -> None:
    out, labels, registry, _ = packet_reviews
    manifest = json.loads((out / "manifest.json").read_text())
    entries = {entry["path"]: entry for entry in manifest["files"]}
    assert entries["source/redacted.txt"]["redacted"] is True
    assert entries["inputs/private.txt"]["redacted"] is True
    labels[0]["judgments"]["implementation"]["citations"] = citations
    with pytest.raises(ValueError, match="pass requires unredacted source/input citations"):
        adjudicate(out, labels, registry)


@pytest.mark.parametrize("support", ["source/main.rs:1", "inputs/task.txt:1"])
def test_pass_allows_unredacted_support_alongside_redacted_citations(
    packet_reviews: tuple[Path, list[dict], dict, dict], support: str
) -> None:
    out, labels, registry, _ = packet_reviews
    for label in labels:
        label["judgments"]["implementation"]["citations"] = [
            "source/redacted.txt:1",
            "inputs/private.txt:1",
            "manifest.json",
            support,
        ]
    result = adjudicate(out, labels, registry, acceptance=True)
    assert result["task_success"] is True
    assert result["human_qualified"] is False
    assert result["acceptance_qualified"] is False


@pytest.mark.parametrize("status", ["fail", "missing_evidence"])
def test_nonpassing_judgments_can_explain_redacted_evidence(
    packet_reviews: tuple[Path, list[dict], dict, dict], status: str
) -> None:
    out, labels, registry, _ = packet_reviews
    for label in labels:
        label["judgments"]["implementation"] = {
            "status": status,
            "rationale": "Redaction prevents establishing this claim.",
            "citations": ["source/redacted.txt:1", "inputs/private.txt:1"],
        }
    result = adjudicate(out, labels, registry)
    assert result["task_success"] is (None if status == "missing_evidence" else False)
    assert result["unresolved"] is (status == "missing_evidence")
