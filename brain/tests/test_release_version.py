"""Release metadata must agree before artifacts can be built or published."""

import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/check_release_version.py"
check_release_version = runpy.run_path(str(SCRIPT))["check_release_version"]


@pytest.fixture
def release_tree(tmp_path):
    (tmp_path / "brain").mkdir()
    files = {
        "Cargo.toml": '[workspace.package]\nversion = "1.2.3"\n',
        "brain/pyproject.toml": '[project]\nversion = "1.2.3"\n',
        "Cargo.lock": (
            '[[package]]\nname = "hippo-core"\nversion = "1.2.3"\n'
            '[[package]]\nname = "hippo-daemon"\nversion = "1.2.3"\n'
            '[[package]]\nname = "third-party"\nversion = "9.9.9"\n'
        ),
        "brain/uv.lock": '[[package]]\nname = "hippo-brain"\nversion = "1.2.3"\n',
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    return tmp_path


def test_matching_release_versions(release_tree):
    assert check_release_version(release_tree, "v1.2.3") == "1.2.3"


@pytest.mark.parametrize(
    "tag", ["1.2.3", "v1.2", "v01.2.3", "v1.2.3-test", "v1.2.3+build", "v1.2.3\n"]
)
def test_rejects_unsupported_tag(release_tree, tag):
    with pytest.raises(ValueError, match="stable release tag"):
        check_release_version(release_tree, tag)


@pytest.mark.parametrize(
    "filename, package",
    [
        ("Cargo.toml", None),
        ("brain/pyproject.toml", None),
        ("Cargo.lock", "hippo-core"),
        ("Cargo.lock", "hippo-daemon"),
        ("brain/uv.lock", "hippo-brain"),
    ],
)
def test_rejects_stale_package_version(release_tree, filename, package):
    path = release_tree / filename
    prefix = f'name = "{package}"\n' if package else ""
    path.write_text(
        path.read_text().replace(prefix + 'version = "1.2.3"', prefix + 'version = "1.2.2"')
    )
    with pytest.raises(ValueError, match="(do|does) not match"):
        check_release_version(release_tree, "v1.2.3")


@pytest.mark.parametrize("duplicate", [False, True])
def test_rejects_missing_or_duplicate_lock_package(release_tree, duplicate):
    path = release_tree / "Cargo.lock"
    entry = '[[package]]\nname = "hippo-daemon"\nversion = "1.2.3"\n'
    content = path.read_text()
    path.write_text(content + entry if duplicate else content.replace(entry, ""))
    with pytest.raises(ValueError, match="hippo-daemon"):
        check_release_version(release_tree, "v1.2.3")
