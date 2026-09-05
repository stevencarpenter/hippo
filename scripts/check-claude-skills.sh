#!/usr/bin/env bash
# Validate the Claude Code skills shipped in extension/claude-skill/.
#
# Exists because PR #169 deleted both SKILL.md files as collateral in an
# unrelated bugfix while leaving the `install:skill` mise task pointing at
# them. The task's `ln -s` happily creates a dangling symlink and `mise run
# install` swallows the failure with `|| echo WARN`, so every user's install
# silently produced a broken ~/.claude/skills entry for three months. Nothing
# failed loudly. This check makes that class of drift fail in CI.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

skill_root="extension/claude-skill"
status=0

fail() {
    echo "FAIL: $*" >&2
    status=1
}

# 1. Every skill directory is well-formed: a SKILL.md whose frontmatter
#    `name:` matches the directory name (Claude Code resolves skills by that
#    field, so a mismatch installs a skill under an unexpected name).
shopt -s nullglob
skill_dirs=("$skill_root"/*/)
if [ ${#skill_dirs[@]} -eq 0 ]; then
    fail "no skills found under $skill_root/"
fi

for dir in "${skill_dirs[@]}"; do
    name="$(basename "$dir")"
    manifest="$dir/SKILL.md"
    if [ ! -f "$manifest" ]; then
        fail "$name: missing SKILL.md"
        continue
    fi
    declared="$(awk '/^---$/{n++; next} n==1 && /^name:/{sub(/^name:[[:space:]]*/, ""); print; exit}' "$manifest")"
    if [ -z "$declared" ]; then
        fail "$name: SKILL.md has no frontmatter 'name:' field"
    elif [ "$declared" != "$name" ]; then
        fail "$name: SKILL.md declares name '$declared', expected '$name'"
    fi
done

# 2. The skills that are supposed to ship still ship. `install:skill` now
#    discovers skills by glob, so a deletion no longer breaks a hard-coded
#    path — it would just silently install one fewer skill. Listing them here
#    means dropping one is a deliberate edit to this file, not collateral.
required_skills=(
    monitoring-hippo
    using-hippo-brain
)

for name in "${required_skills[@]}"; do
    if [ ! -d "$skill_root/$name" ]; then
        fail "required skill '$name' is missing from $skill_root/ (remove it from required_skills here if that is intended)"
    fi
done

# 3. mise.toml still installs from the expected root. Catches the install task
#    being deleted, renamed, or pointed somewhere else.
if ! grep -q "$skill_root" mise.toml; then
    fail "mise.toml no longer references $skill_root/ — the install task was removed or repointed"
fi

# Any literal per-skill path mise.toml names must also resolve.
while IFS= read -r path; do
    [ -d "$path" ] || fail "mise.toml references $path, which does not exist"
done < <(grep -oE "$skill_root/[A-Za-z0-9_-]+" mise.toml | sort -u)

if [ "$status" -eq 0 ]; then
    echo "OK: ${#skill_dirs[@]} skill(s) well-formed, ${#required_skills[@]} required skill(s) present"
fi

exit "$status"
