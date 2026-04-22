// Semgrep rule fixture for capture-reliability F-17 (AP-11).
//
// This file intentionally contains patterns that the
// `hippo-capture-silent-result-swallow` rule in .semgrep.yml should flag.
// It is NOT compiled or tested — the file lives under tests/semgrep/ and
// is excluded from cargo (not a bin/lib target) — so the rule can be
// verified locally via:
//
//     semgrep --config .semgrep.yml tests/semgrep/
//
// Expected: 3 findings in this file.
//
// Why we keep the fixture: when the semgrep rule itself is refined
// (stricter pattern, new exclusions), this file is the canary.

#![allow(dead_code, unused)]

use std::fs;

// Finding 1: bare `.filter_map(|r| r.ok())` on a Result iterator.
fn silently_drop_file_errors(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
    fs::read_dir(dir)
        .unwrap()
        .filter_map(|r| r.ok()) // <-- EXPECTED: semgrep flag
        .map(|e| e.path())
        .collect()
}

// Finding 2: `.filter_map(Result::ok)` method reference shape.
fn silently_drop_via_method_ref(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
    fs::read_dir(dir)
        .unwrap()
        .filter_map(Result::ok) // <-- EXPECTED: semgrep flag
        .map(|e| e.path())
        .collect()
}

// Finding 3: same idiom through a binding name that isn't `r`.
fn silently_drop_via_arbitrary_name(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
    fs::read_dir(dir)
        .unwrap()
        .filter_map(|entry| entry.ok()) // <-- EXPECTED: semgrep flag
        .map(|e| e.path())
        .collect()
}

// Non-finding: a `.filter_map` that inspects the error deliberately before
// dropping. This is the acceptable pattern — not a silent swallow.
fn explicit_error_handling(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
    fs::read_dir(dir)
        .unwrap()
        .filter_map(|r| match r {
            Ok(e) => Some(e.path()),
            Err(e) => {
                eprintln!("skipping entry: {e}");
                None
            }
        })
        .collect()
}

// Non-finding: Result-to-Option via `?` — bubbles errors up, doesn't
// silently drop.
fn bubble_errors(dir: &std::path::Path) -> std::io::Result<Vec<std::path::PathBuf>> {
    fs::read_dir(dir)?
        .map(|r| r.map(|e| e.path()))
        .collect::<std::io::Result<Vec<_>>>()
}
