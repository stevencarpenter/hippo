# Local-LLM enrichment (accuracy/verbatim) lens summary

Mean scores: accuracy 4.74 / succinctness 4.60 / usefulness 4.59 / ask 3.79 / mcp 4.42
Nodes scored: 100

## Top strengths (top 3, concrete)

1. **Verbatim path preservation on tight sessions**: `18a0a269-eed1-48df-82b0-1a5cba60a578` (chezmoi formatter fix) keeps the full repo-relative paths (`.chezmoiscripts/run_after_sync-mcp.sh.tmpl`, `.editorconfig`, `dot_config/dot_copilot/config.json.tmpl`) exactly as the source segments emitted them, and the embed_text reads as identifier tag-soup with literal Go-template syntax (`{{- if hasPrefix "work" .machine }}`) preserved verbatim — exactly what hybrid FTS+vec wants.
2. **Structured `design_decisions` with {considered, chosen, reason}**: `fe5d66df-c6f0-43bd-ad00-f6f18bbd7f0f` (rusqlite tests) and `b564e2bf-22af-4ea5-bf2f-238c3a56cbd7` (per-cwd git owner cache) translate narrative trade-offs into the canonical structured shape with concrete reasons — making them directly renderable in `hippo ask` output. 39/100 nodes populate this field.
3. **Faithful shell-only summaries**: `b9a5e0cd-a994-4c78-8921-64c2a23dc757` and other git-push nodes quote the exact command, branch, and commit SHA from the event row without inventing surrounding context — short, precise, and zero hallucination.

## Top weaknesses (top 3, concrete)

1. **Fabricated env_vars / versions** (8/100 nodes with severe hallucinations): `87976564-3ace-4903-b8a4-0e666720d4f1` adds `CARGO_HOME` and `PATH` to env_vars even though neither token appears anywhere in the source segments; `ed2c982a-7e40-4571-a836-8329b8dfb16d` and `1cf61560-d46d-4fed-b85c-5f79e1fd37f8` invent semver strings (`1.93.1`, `0.2.0`, `0.149.0`) that are wholly absent from the source. These are the single most damaging failures — a future agent cannot un-believe a wrong version.
2. **Path entities not present in source text** (8/100 nodes): `ed2c982a-7e40-4571-a836-8329b8dfb16d` lists 12 file paths whose tail filenames never appear in `claude_segments_text`; `7f81d66e-ce29-4ffc-96a0-741943b2d2df` invents `docs/capture-reliability/09-test-matrix.md` (only the directory `capture-reliability` is in the source). Worktree-prefixed paths (`87976564`, `25b32204`) also leak through instead of being stripped per the v3 prompt rule.
3. **Duplicated enrichments on same-content sessions**: `25b32204-7111-4d8d-8f9b-e0a2d5e4610a` and `6dd039b8-5525-4539-87ed-782f59ed6ad5` produce essentially identical summary, files, env_vars, and embed_text — both also fabricate `--with` and `--all` flags and the `PATH` env_var. This suggests the model amplifies a single hallucination into multiple retrieval rows, doubling its impact on `search_knowledge` and `get_entities`.

## Worst 5 nodes

| uuid | reason |
| --- | --- |
| `ed2c982a-7e40-4571-a836-8329b8dfb16d` | halluc:2ver; 12path-not-in-src |
| `25b32204-7111-4d8d-8f9b-e0a2d5e4610a` | halluc:1env,2flag; 5path-not-in-src; case-drift(PATH) |
| `6dd039b8-5525-4539-87ed-782f59ed6ad5` | halluc:1env,2flag; 5path-not-in-src; case-drift(PATH) |
| `87976564-3ace-4903-b8a4-0e666720d4f1` | halluc:2env; 1path-not-in-src; case-drift(PATH) |
| `b63c3f9b-d9b5-4510-ad13-afe41d5a3435` | verbatim clean but trivial content (low usefulness) |

## Cross-cutting observation

Through the verbatim-preservation lens, qwen3.6-35b-a3b-ud-mlx is **strong on the easy wins** (mean accuracy 4.74, mean mcp_suitability 4.42) — tool names, command names, file paths that appear plainly in shell `command` fields or Claude `summary_text` are reproduced exactly, and 39/100 nodes populate `design_decisions` in the correct {considered, chosen, reason} shape. The failure mode is **contextual confabulation on env_vars and versions**: when the source describes Rust or Cargo work, the model adds `CARGO_HOME` / `PATH` to env_vars; when it describes a release, it invents plausible-but-absent semver strings. These are the v3 prompt's stated worst case ("hallucinated identifier worse than missing one") and they go undetected by downstream retrieval. The two duplicated-enrichment cases (`25b32204` / `6dd039b8`) amplify single hallucinations into multiple knowledge rows — a corpus-level issue worth fixing upstream of the LLM. Coverage on long multi-segment Claude sessions also slips: embed_text stays short while content_len blows past 4000 chars, dropping later-segment identifiers from the FTS+vec index. Net: the model is reliable enough for production but would benefit from (a) a post-LLM verbatim-validator that strips entities not present in source, (b) per-segment chunking before enrichment for long sessions, and (c) a deduplication pass on near-identical enrichment outputs.
