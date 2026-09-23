# Jev conflict judgments in Hippo

Jev is a candidate for advisory semantic conflict analysis. The measured outcome
misses do not support using it to clear Hippo's existing warnings. Compared with
the deterministic detector, fewer false warnings came with more missed genuine
outcome conflicts. Jev adds remote inference latency to this production path.

The comparison executes `analyze_conflicts` unchanged as the `current` arm.
The semantic policy treats unresolved incompatible assertions about the same
subject, project, environment and time or attempt as conflicts. Explicit recovery
or replacement is compatible chronology; capture timestamps alone establish
neither. This refines the current behavior, so changed labels do not establish
that existing unit tests are incorrect.

[`cases.json`](cases.json) contains 44 reviewed synthetic cases fixed before
inference. [`pool-cases.json`](pool-cases.json) contains eight ten-hit follow-ups
authored after inspecting initial results. Neither is a representative production
sample or an independent holdout. Models see normalized enriched summaries,
not original source records. Repeats are dependent observations.

Raw requests/responses, summaries, measured protocols, analyses, source snapshots
and the full research report are archived outside the repository:

```sh
archive="${XDG_DATA_HOME:-$HOME/.local/share}/hippo-bench/decisions/archive/typesafe-2026-09-20-conflicts"
```

The archive contains `run/` (528 records), `pool-run/` (64 records),
`analysis.json`, `pool-analysis.json`, `protocol.json`, `pool-protocol.json`, and
`README.md`. The archived report preserves the recorded misses and the post-hoc
advisory-policy simulation that retains existing warnings. That simulation was
not an executed arm; its call savings were simulated and its latency unmeasured.
The parent archive's `relocation-2026-09-22.json` records SHA256 checksums,
including the full report with its uncommitted advisory-policy update.

Run deterministic comparisons and replay recorded responses without inference:

```sh
mise run bench:conflicts
mise run bench:conflicts:replay
mise run bench:conflicts:replay -- "$archive/pool-run"
```

[`audit.py`](audit.py) defaults to the external `run/` directory and never writes
artifacts. It verifies corpus, labels, rules, source and request hashes, matching
input states, unique record identities, response verdicts and summary metrics.
An unset or empty `XDG_DATA_HOME` uses `~/.local/share`.

Use the [decision sidecar instructions](../../decision-sidecars.md) for new live
comparisons. Generated runs remain outside Git. No production database,
inference selection, warning policy, capture service or model configuration was
changed by this experiment.
