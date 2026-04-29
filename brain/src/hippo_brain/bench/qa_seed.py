"""Seed eval-qa-v1.jsonl into the bench fixtures directory from the committed template."""

from pathlib import Path

from hippo_brain.bench.paths import bench_fixtures_dir


def seed_qa_fixture() -> int:
    template = Path(__file__).parent / "qa_template.jsonl"
    dest = bench_fixtures_dir(create=True) / "eval-qa-v1.jsonl"
    content = template.read_text()
    dest.write_text(content)
    count = sum(1 for line in content.splitlines() if line.strip())
    print(f"Seeded {count} Q/A items to {dest}")
    return count


if __name__ == "__main__":
    seed_qa_fixture()
