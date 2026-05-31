# hippo-bench Operator Runbook

This runbook covers the operator-driven gates that the autonomous bench loop
deliberately doesn't run because the blast radius is too high. The current
critical entry is **BT-29: deterministic-rerun verification.**

> **mise shortcut:** the everyday flow is wrapped in `bench:*` mise tasks —
> `mise run bench:status`, `mise run bench:run <model-id>`, etc. See
> [the bench README](../../brain/src/hippo_brain/bench/README.md#running-via-mise-recommended).
> This runbook remains authoritative for the operator-gated BT-29 procedure.

## Required pre-BT-29 corpus/Q/A gate

Before running the three BT-29 model passes, confirm the corpus is schema-current
and the Q/A fixture is fully scoreable against it. A mislabeled or stale golden
shifts MRR by 1/N — larger than the BT-29 budget — so this gate protects the
trust claim at its root.

```bash
uv run --project brain hippo-bench corpus verify --corpus-version corpus-v2
uv run --project brain hippo-bench qa validate \
  --qa-path ~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl \
  --corpus-sqlite ~/.local/share/hippo-bench/fixtures/corpus-v2.sqlite \
  --min-scoreable 100
```

Both commands must exit 0. The Q/A `golden_event_id`s are corpus-grounded and
bound to a specific `corpus_content_hash` (recorded in
`brain/src/hippo_brain/bench/qa_template.provenance.json`). **If you rebuild the
corpus from a different live DB, the sampled events change and the goldens stop
resolving** — `qa validate` will report the shortfall. Re-annotate against the
new corpus (corpus-derived authoring, Mode B in
[`docs/baselines/QA-ANNOTATION.md`](../baselines/QA-ANNOTATION.md)) before
trusting any retrieval metric.

## BT-29: deterministic-rerun verification

### Why this exists

The bench's "trust" claim — "if it says model A > model B, that's true" —
falls apart if the same model produces materially different verdicts across
identical reruns. Per the tracking doc's Definition of Done #1:

> Three consecutive runs of the same model against the frozen reference
> corpus produce identical verdicts (Hit@1 ± 0.02, MRR ± 0.02, judge-mean ± 0.1).

Until BT-29 fires green at least once, the trust foundation is unverified.

### Why the autonomous loop doesn't run it

Each run pauses prod brain for ~30 min and consumes the local inference server
(oMLX) exclusively. Three consecutive runs is ~90 min of blocked prod
observability. That's unsafe to trigger from a multi-iteration ralph loop where
a hung model can extend the pause indefinitely.

### Procedure

**Prerequisites:**
- The local inference server (oMLX, default `http://localhost:8000/v1`) is
  running and idle (no other consumers). Pass `--base-url` if it differs.
- Prod brain is running and healthy (`hippo doctor` is green).
- The frozen corpus snapshot is present at the path you'll pass to `--corpus-version`.
- You have ~90 min where prod observability gaps are acceptable.

**Run:**

`hippo-bench run` builds an internal `run_id` per invocation (timestamp +
short hash) and writes a JSONL there. The `--out` flag forces a specific
output path; that's what we use here so all three runs land in known
locations the harness can compare.

```bash
# Pick a model your inference server (oMLX) can serve. Use the SAME model +
# temperature across all three runs; BT-29 measures bench-verdict
# reproducibility at the settings you actually deploy with, not at
# temperature=0 (which would make self-consistency a vacuous signal).
MODEL="Qwen3.6-35B-A3B-UD-MLX-4bit"

for i in 1 2 3; do
  uv run --project brain hippo-bench run \
    --models "$MODEL" \
    --corpus-version corpus-v2 \
    --out "/tmp/bt29-r$i.jsonl"
done

# Compare. Exits 1 if any model exceeds the 0.02 budget.
uv run --project brain hippo-bench determinism \
  /tmp/bt29-r1.jsonl /tmp/bt29-r2.jsonl /tmp/bt29-r3.jsonl
```

The harness prints a determinism report with one row per compared model
listing the mrr range, mrr delta, hit@1 range, hit@1 delta, and verdict.
It exits 0 when every model's MRR delta and Hit@1 delta are within budget
— at which point trust foundation is verified for this model. No reference
metrics are published in this runbook on purpose: real numbers belong in
the trust ledger (see below), not in copy-paste templates.

The harness defaults to comparing the `hybrid` retrieval mode (production
path). To verify a different mode (e.g. semantic-only deployment), pass
`--mode semantic`. To loosen or tighten the budget, use `--mrr-budget` and
`--hit-at-1-budget`.

If any compared run is missing `downstream_proxy.modes[<mode>].mrr` or
`hit_at_1` (e.g. the proxy step raised and was captured into `errors[]`),
the model gets a `FAIL (missing: ...)` verdict rather than a silent PASS —
determinism cannot be assessed when one of the data points is absent.

**Expected output (FAIL path):**

If any model's MRR delta or Hit@1 delta crosses 0.02, the verdict is FAIL
and the harness exits 1. This means the model is **not deterministic enough
for the bench to rank it reliably** — the verdict is dominated by run-to-run
noise rather than actual ranking signal.

Possible causes (ordered by likelihood):
1. **Model quantization mismatch** — if the inference server unloaded and
   reloaded the model between runs you may have hit a different quantization.
   Confirm the served model card stayed identical before each run (e.g.
   `curl -s http://localhost:8000/v1/models`).
2. **Corpus drift** — if `corpus.sqlite` was rebuilt mid-experiment, the
   inputs differ. Check `sha256sum` of the corpus file across runs.
3. **Real model nondeterminism above the budget** — sampling at default
   temperature (0.7) means MoE routing or stochastic decoding *can* produce
   spread; the budget is tuned to accept production-realistic noise. If the
   delta is consistently >0.02, the model is too noisy for the bench to rank
   reliably and should be excluded or flagged in the trust ledger.
4. **Temperature drift** — if you bumped `--temperature` between runs, the
   spread is expected. Confirm all three invocations used the same flag.

### Recording the result

Once a model passes BT-29, append a line to the trust ledger:

```bash
# (proposal — table doesn't exist yet, see Phase 3 work)
echo "$(date -u +%Y-%m-%dT%H:%MZ) | $MODEL | $VERDICT | mrr_delta=$MRR_DELTA | hit_at_1_delta=$HIT_AT_1_DELTA" \
  >> docs/baselines/bt29-trust-ledger.tsv
```

This gives you "is this new model better than the last passing model on the
ledger?" without re-running BT-29 on every challenger.

### Skipping BT-29 (and being honest about it)

If you ship a bench result without BT-29 having run, the verdict is
**unverified empirically** — the model passed lint + golden retrieval test
(BT-19) but nothing has confirmed run-to-run stability. Document this on
the run summary as "BT-29 deferred." Do NOT claim "trust foundation
complete" without it.

## Other operator-gated procedures

(Empty for now. Add new entries here as Phase 2/3 work surfaces gates that
shouldn't run autonomously — judge-LLM rubric calibration, frozen-corpus
re-freeze cadence, etc.)
