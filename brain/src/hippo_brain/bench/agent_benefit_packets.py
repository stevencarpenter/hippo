"""Complete frozen task packets and evidence-bound diagnostic adjudication."""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from hippo_brain.bench.agent_benefit_study import require
from hippo_brain.bench.claim_packets import create_directory, write_artifact
from hippo_brain.decision_capture import external_path
from hippo_brain.jev import digest
from hippo_brain.redaction import redact

VERSION = "agent-task-packets-v2"
LEAK = re.compile(
    r'/(?:control|treatment)(?:/|\b)|/Users/|/private/var/|hippo-agent-trials|"arm"\s*:\s*"(?:control|treatment)"'
)
CITATION = re.compile(r"(?:crates|docs|scripts|src|home)/[\w./-]+\.[\w-]+")


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def _write(root: Path, name: str, raw: bytes, *, hide_paths: bool = False) -> dict:
    path = root / name
    require(
        path.resolve().is_relative_to(root.resolve()) and ".git" not in path.parts,
        "unsafe packet path",
    )
    text = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    changed = False
    if text is not None:
        clean = redact(text)
        if hide_paths:
            clean = re.sub(r"/Users/[^\s`\"'<>),]+", "<private-path>", clean)
            clean = re.sub(r"/private/var/[^\s`\"'<>),]+", "<private-path>", clean)
            clean = re.sub(r"/(control|treatment)/", "/submission/", clean)
            clean = re.sub(r'"arm"\s*:\s*"(?:control|treatment)"', '"arm": "hidden"', clean)
        changed = clean != text
        raw_out = clean.encode()
    else:
        raw_out = raw
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    for parent in [path.parent, *path.parent.parents]:
        if parent == root.parent:
            break
        parent.chmod(0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(raw_out)
    return {
        "path": name,
        "original_sha256": hashlib.sha256(raw).hexdigest(),
        "sha256": file_hash(path),
        "original_bytes": len(raw),
        "bytes": len(raw_out),
        "redacted": changed,
    }


def prepare(
    repo: Path,
    commit: str,
    out: Path,
    *,
    inputs: dict[str, Path],
    expected_hashes: dict[str, str],
    submissions: dict[str, Path],
    rubric: dict,
    required_files: list[str],
    missing: list[str],
    key_path: Path,
    seed: str,
) -> dict:
    """Export Git objects, never a mutable checkout or its history. Originals survive."""
    out, key_path = external_path(out), external_path(key_path)
    require(not key_path.is_relative_to(out), "arm key must be outside reviewer packet")
    require(
        set(inputs) == set(expected_hashes) and "task.txt" in inputs,
        "complete hashed task inputs required",
    )
    for name, path in inputs.items():
        require(file_hash(path) == expected_hashes[name], f"changed or truncated input: {name}")
    require(len(submissions) == 2, "two submissions required")
    require(
        git(repo, "rev-parse", commit).decode().strip() == commit, "full frozen commit required"
    )
    entries = git(repo, "ls-tree", "-r", "-z", commit).split(b"\0")
    files, tree, unavailable = [], [], list(missing)
    create_directory(out)
    for entry in filter(None, entries):
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.decode().split()
        name = raw_name.decode()
        tree.append({"path": name, "mode": mode, "object": object_id})
        if kind != "blob":
            unavailable.append(f"dependency_not_exported:{name}")
            continue
        files.append(_write(out, "source/" + name, git(repo, "cat-file", "blob", object_id)))
    order = sorted(submissions, key=lambda name: digest([seed, name]))
    aliases = dict(zip(order, ("A", "B"), strict=True))
    for name, path in inputs.items():
        display = name
        for original, alias in aliases.items():
            if name == "changes/" + original + ".diff":
                display = "changes/" + alias + ".diff"
            if name.startswith("candidate/" + original + "/"):
                display = (
                    "candidate/" + alias + "/" + name.removeprefix("candidate/" + original + "/")
                )
        files.append(_write(out, "inputs/" + display, path.read_bytes(), hide_paths=True))
    arm_key = {}
    for alias, name in zip(("A", "B"), order, strict=True):
        files.append(
            _write(out, f"answers/{alias}.txt", submissions[name].read_bytes(), hide_paths=True)
        )
        arm_key[alias] = name
    source_names = {item["path"] for item in tree}
    citations = set(required_files)
    for alias in ("A", "B"):
        citations.update(CITATION.findall((out / f"answers/{alias}.txt").read_text()))
    for citation in sorted(citations):
        if citation not in source_names and not any(
            f["path"].startswith("inputs/candidate/") and f["path"].endswith("/" + citation)
            for f in files
        ):
            unavailable.append("citation_missing:" + citation)
    definition = {
        "version": VERSION,
        "source_commit": commit,
        "source_tree": tree,
        "rubric": rubric,
        "rubric_hash": digest(rubric),
        "files": files,
        "required_files": required_files,
        "citations": sorted(citations),
        "missing_evidence": sorted(set(unavailable)),
        "acceptance_eligible": False,
    }
    manifest = {**definition, "packet_hash": digest(definition)}
    write_artifact(out / "manifest.json", manifest)
    write_artifact(key_path, {"packet_hash": manifest["packet_hash"], "aliases": arm_key})
    return audit(out)


def audit(root: Path) -> dict:
    root = external_path(root)
    require(
        not root.is_symlink() and root.stat().st_mode & 0o777 == 0o700,
        "packet directory must be private",
    )
    manifest = json.loads((root / "manifest.json").read_text())
    require(
        manifest["version"] == VERSION
        and manifest["packet_hash"]
        == digest({k: v for k, v in manifest.items() if k != "packet_hash"}),
        "modified packet manifest",
    )
    require(manifest["rubric_hash"] == digest(manifest["rubric"]), "modified rubric")
    listed = {entry["path"] for entry in manifest["files"]}
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    require(actual == listed | {"manifest.json"}, "omitted or unexpected packet files")
    tree_blobs = {
        "source/" + entry["path"] for entry in manifest["source_tree"] if entry["mode"] != "160000"
    }
    require(tree_blobs <= listed, "incomplete tracked source")
    for path in root.rglob("*"):
        require(not path.is_symlink(), "packet symlink forbidden")
        require(
            path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600),
            "unsafe packet permissions",
        )
    for entry in manifest["files"]:
        path = root / entry["path"]
        require(path.resolve().is_relative_to(root), "unsafe evidence path")
        require(
            file_hash(path) == entry["sha256"] and path.stat().st_size == entry["bytes"],
            "modified evidence",
        )
        if entry["path"].startswith(("answers/", "inputs/")):
            require(not LEAK.search(path.read_text()), "arm/path leak in reviewer evidence")
    missing = list(manifest["missing_evidence"])
    for assertion in manifest["rubric"].get("source_assertions", []):
        source = root / "source" / assertion["path"]
        require(source.resolve().is_relative_to(root / "source"), "unsafe rubric source")
        if not source.is_file() or assertion["contains"] not in source.read_text():
            missing.append("rubric_source_contradiction:" + assertion["path"])
    for citation in manifest["required_files"]:
        entry = next((f for f in manifest["files"] if f["path"] == "source/" + citation), None)
        if entry is None or entry["redacted"]:
            missing.append("required_source_missing_or_redacted:" + citation)
    return {
        "packet_hash": manifest["packet_hash"],
        "rubric_hash": manifest["rubric_hash"],
        "files": len(listed),
        "tracked_rust_files": sum(p.endswith(".rs") for p in tree_blobs),
        "missing_evidence": sorted(set(missing)),
        "complete_for_adjudication": not missing,
        "acceptance_eligible": False,
    }


def adjudicate(
    root: Path,
    labels: list[dict],
    registry: dict,
    *,
    submission: str = "A",
    acceptance: bool = False,
) -> dict:
    """Registry provenance is an owner-verified external trust root, not label text."""
    evidence = audit(root)
    manifest = json.loads((root / "manifest.json").read_text())
    require(submission in {"A", "B"}, "unknown submission")
    expected = digest(
        [manifest["packet_hash"], manifest["rubric_hash"], "task-adjudication-v2", submission]
    )
    items = {item["id"] for item in manifest["rubric"]["items"]}
    evidence_paths = {entry["path"] for entry in manifest["files"]} | {"manifest.json"}
    unredacted_support = {
        entry["path"]
        for entry in manifest["files"]
        if entry["redacted"] is False and entry["path"].startswith(("source/", "inputs/"))
    }
    require(
        len(labels) == 2 and len({label["reviewer_id"] for label in labels}) == 2,
        "two independent reviewers required",
    )
    qualified = True
    for label in labels:
        require(label.get("submission") == submission, "labels belong to a different submission")
        require(label["evaluation_id"] == expected, "changed evidence/rubric/evaluation version")
        reviewer = registry[label["reviewer_id"]]
        require(
            reviewer["independent"] is True
            and reviewer["arm_access"] is False
            and reviewer["prior_exposure"] is False,
            "reviewer independence missing",
        )
        require(
            label["reviewer_kind"] == reviewer["kind"],
            "reviewer kind differs from external registry",
        )
        provenance = Path(reviewer["provenance_file"])
        require(
            file_hash(provenance) == reviewer["provenance_sha256"], "changed reviewer provenance"
        )
        qualified &= reviewer["kind"] == "human" and reviewer.get("verified_by_owner") is True
        require(set(label["judgments"]) == items, "incomplete rubric judgments")
        for judgment in label["judgments"].values():
            require(
                judgment["status"]
                in {
                    "pass",
                    "fail",
                    "missing_evidence",
                    "contradiction",
                    "tool_failure",
                    "task_failure",
                },
                "invalid judgment",
            )
            rationale = judgment.get("rationale")
            require(
                isinstance(rationale, str) and bool(rationale.strip()),
                "source-backed rationale required",
            )
            citations = judgment.get("citations", [])
            require(
                isinstance(citations, list)
                and all(isinstance(ref, str) and ref.strip() for ref in citations),
                "citations must be an array of nonempty strings",
            )
            cited = []
            for ref in citations:
                parts = re.fullmatch(r"(.+?)(?::(\d+)(?:-(\d+))?)?", ref)
                require(parts is not None, "invalid citation")
                name, first, last = parts.groups()
                require(
                    name in evidence_paths,
                    "unresolvable judgment citation",
                )
                if first:
                    lines = len((root / name).read_text().splitlines())
                    require(
                        1 <= int(first) <= int(last or first) <= lines,
                        "citation line is outside frozen evidence",
                    )
                cited.append(name)
            require(
                judgment["status"] != "pass" or any(name in unredacted_support for name in cited),
                "pass requires unredacted source/input citations",
            )
    unresolved = not evidence["complete_for_adjudication"]
    for item in items:
        statuses = [label["judgments"][item]["status"] for label in labels]
        unresolved |= statuses[0] != statuses[1] or "missing_evidence" in statuses
    success = (
        None
        if unresolved
        else all(label["judgments"][item]["status"] == "pass" for label in labels for item in items)
    )
    return {
        "evaluation_id": expected,
        "task_success": success,
        "unresolved": unresolved,
        "human_qualified": qualified,
        "acceptance_qualified": acceptance
        and qualified
        and not unresolved
        and manifest["acceptance_eligible"],
        "reviewer_registry_hash": digest(registry),
        "label_hashes": [digest(label) for label in labels],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "audit", "adjudicate"))
    parser.add_argument(
        "path", type=Path, help="private preparation specification or packet directory"
    )
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--submission", choices=("A", "B"), default="A")
    args = parser.parse_args()
    os.umask(0o077)
    if args.operation == "prepare":
        spec = json.loads(args.path.read_text())
        result = prepare(
            Path(spec["repo"]),
            spec["commit"],
            Path(spec["out"]),
            inputs={k: Path(v) for k, v in spec["inputs"].items()},
            expected_hashes=spec["expected_hashes"],
            submissions={k: Path(v) for k, v in spec["submissions"].items()},
            rubric=spec["rubric"],
            required_files=spec["required_files"],
            missing=spec["missing"],
            key_path=Path(spec["key_path"]),
            seed=spec["seed"],
        )
    elif args.operation == "audit":
        result = audit(args.path)
    else:
        require(
            args.labels is not None and args.registry is not None, "labels and registry required"
        )
        result = adjudicate(
            args.path,
            json.loads(args.labels.read_text()),
            json.loads(args.registry.read_text()),
            submission=args.submission,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
