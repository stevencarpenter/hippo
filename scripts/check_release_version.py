"""Reject release tags that disagree with the packages being shipped."""

import argparse
import re
import tomllib
from pathlib import Path


def check_release_version(root: Path, tag: str) -> str:
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ValueError(f"expected a stable release tag vX.Y.Z, got {tag!r}")
    version = tag[1:]
    manifests = (
        ("Cargo.toml", ("workspace", "package", "version")),
        ("brain/pyproject.toml", ("project", "version")),
    )
    for filename, keys in manifests:
        value = tomllib.loads((root / filename).read_text())
        for key in keys:
            value = value[key]
        if value != version:
            raise ValueError(f"{filename}: version {value!r} does not match {tag}")
    for filename, names in (
        ("Cargo.lock", ("hippo-core", "hippo-daemon")),
        ("brain/uv.lock", ("hippo-brain",)),
    ):
        packages = tomllib.loads((root / filename).read_text())["package"]
        for name in names:
            versions = [
                package["version"] for package in packages if package["name"] == name
            ]
            if versions != [version]:
                raise ValueError(
                    f"{filename}: {name} versions {versions!r} do not match {tag}"
                )
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="stable release tag, for example v1.0.0")
    args = parser.parse_args()
    try:
        version = check_release_version(Path(__file__).resolve().parents[1], args.tag)
    except (OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"Release version validation failed: {exc}\n")
    print(f"Release {version}: manifests and lockfiles match")


if __name__ == "__main__":
    main()
