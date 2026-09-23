# TypeSafe experiments for Hippo

The synthetic screen supports comparing Jev reranking and advisory claim
verification with Hippo's current implementation. It does not establish a
production accuracy or latency improvement. The answerability question failed
to distinguish missing requested facts; relevance must not become an
answerability gate.

[`cases.json`](cases.json) contains the initial authored fixtures.
[`holdout.json`](holdout.json) contains follow-up stress cases authored after
inspecting failures, so it is not an independent holdout. Labels are excluded
from inference requests. [`run.py`](run.py) supports offline replay and explicit
live requests using the Python standard library.

Generated outputs and the full research report are stored outside the repository:

```sh
archive="${XDG_DATA_HOME:-$HOME/.local/share}/hippo-bench/decisions/archive/typesafe-2026-09-18"
```

The archive contains `README.md`, exact request/response records in `results.json`
and `holdout-results.json`, their `*-summary.json` files, and original fixtures
and runner snapshots. The parent archive's `relocation-2026-09-22.json` records
SHA256 checksums for the verified relocation. Existing archive files are not
rewritten by replay.

Replay the initial and follow-up results without inference:

```sh
python3 docs/research/typesafe-2026-09-18/run.py
python3 docs/research/typesafe-2026-09-18/run.py \
  --cases docs/research/typesafe-2026-09-18/holdout.json \
  --results "$archive/holdout-results.json"
```

`--self-check` validates metrics, ordering and label exclusion without saved
results. Live requests require an existing `TYPESAFE_API_KEY`, `--live`, and a
new external `--results` path. The runner refuses existing output files and
paths inside a Git repository. The external default respects `XDG_DATA_HOME`;
an unset or empty value uses `~/.local/share`.

Use the [decision sidecar](../../decision-sidecars.md) for comparisons against
Hippo's local model and declarative rules. No production selection changed.
