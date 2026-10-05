"""Exercise resource consumers from real wheels, including an sdist rebuild."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest


BRAIN = Path(__file__).parents[1]
TEMPLATE = Path("src/hippo_brain/bench/qa_template.jsonl")
GOLDEN_QA = Path("tests/fixtures/golden_corpus_v1/qa.jsonl")


@pytest.fixture(scope="module")
def distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    root = tmp_path_factory.mktemp("package-resources")
    uv = shutil.which("uv")
    assert uv is not None, "package regression requires the project's uv toolchain"
    subprocess.run(
        [
            uv,
            "build",
            str(BRAIN),
            "--wheel",
            "--sdist",
            "--python",
            sys.executable,
            "--build-constraints",
            str(BRAIN / "build-constraints.txt"),
            "--require-hashes",
            "--out-dir",
            str(root / "direct"),
        ],
        check=True,
        timeout=120,
    )
    (wheel,) = (root / "direct").glob("*.whl")
    (sdist,) = (root / "direct").glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        archive.extractall(root / "source", filter="data")
    (source,) = (root / "source").iterdir()
    subprocess.run(
        [
            uv,
            "build",
            str(source),
            "--wheel",
            "--python",
            sys.executable,
            "--build-constraints",
            str(source / "build-constraints.txt"),
            "--require-hashes",
            "--out-dir",
            str(root / "rebuilt"),
        ],
        check=True,
        timeout=120,
    )
    (rebuilt,) = (root / "rebuilt").glob("*.whl")
    return wheel, rebuilt, source


@pytest.mark.parametrize("origin", ["direct", "rebuilt"])
def test_installed_wheel_resource_consumers(
    distributions: tuple[Path, Path, Path], tmp_path: Path, origin: str
) -> None:
    wheel = distributions[origin == "rebuilt"]
    installed = tmp_path / "installed"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        check=True,
        timeout=60,
    )
    # Ignore editable installs, PYTHONPATH, user site, and the checkout's cwd.
    probe = """import pathlib, sys
sys.path.insert(0, sys.argv[1])
from hippo_brain.bench import qa_seed
assert pathlib.Path(qa_seed.__file__).is_relative_to(sys.argv[1])
assert qa_seed.seed_qa_fixture() > 0
"""
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", probe, str(installed)],
        cwd=tmp_path,
        env={**os.environ, "XDG_DATA_HOME": str(tmp_path / "data")},
        check=True,
        timeout=30,
    )
    seeded = tmp_path / "data/hippo-bench/fixtures/eval-qa-v1.jsonl"
    assert seeded.read_bytes() == (BRAIN / TEMPLATE).read_bytes()
    # Reuse installed dependencies, while the target wheel supplies all Hippo modules.
    questions_probe = """import pathlib, sys
sys.path.insert(0, sys.argv[1])
from hippo_brain import evaluation
assert pathlib.Path(evaluation.__file__).is_relative_to(sys.argv[1])
questions = pathlib.Path(evaluation._parse_args([]).questions)
loaded = evaluation.load_questions(questions)
assert questions.is_relative_to(sys.argv[1])
assert questions.read_bytes() == pathlib.Path(sys.argv[2]).read_bytes()
assert loaded == evaluation.load_questions(sys.argv[2])
"""
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            questions_probe,
            str(installed),
            str(BRAIN / "tests/eval_questions.json"),
        ],
        cwd=tmp_path,
        check=True,
        timeout=30,
    )
    with zipfile.ZipFile(wheel) as archive:
        assert {name for name in archive.namelist() if name.endswith(".jsonl")} == {
            TEMPLATE.relative_to("src").as_posix()
        }
        resources = list((BRAIN / "src/hippo_brain/_fixtures").rglob("*"))
        resources.append(BRAIN / "src/hippo_brain/bench/qa_template.provenance.json")
        for resource in resources:
            if resource.is_file():
                assert archive.read(resource.relative_to(BRAIN / "src").as_posix()) == (
                    resource.read_bytes()
                )


def test_sdist_preserves_required_fixtures(distributions: tuple[Path, Path, Path]) -> None:
    source = distributions[2]
    assert {path.relative_to(source) for path in source.rglob("*.jsonl")} == {
        TEMPLATE,
        GOLDEN_QA,
    }
    for relative in (TEMPLATE, GOLDEN_QA):
        assert (source / relative).read_bytes() == (BRAIN / relative).read_bytes()


def test_source_default_questions_preserves_current_corpus() -> None:
    from hippo_brain import evaluation

    questions = Path(evaluation._parse_args([]).questions)
    expected = BRAIN / "tests/eval_questions.json"
    assert questions.read_bytes() == expected.read_bytes()
    assert evaluation.load_questions(questions) == evaluation.load_questions(expected)
