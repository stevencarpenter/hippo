# Enrichment fidelity review

Run `mise run bench:claims -- --help` for the local review workflow. It evaluates
whether a summary follows from captured evidence. No command changes production
trust, knowledge, or retrieval. The first unit is a complete existing summary;
yes requires every proposition to be supported.

## Prepare and assess

Choose a new directory outside Git and Hippo's production directory. Commands
refuse existing output files/directories, except resuming a review. The following paths are examples:

```sh
mise run bench:claims -- prepare \
  --database "$HOME/.local/share/hippo/hippo.db" \
  --out "$HOME/.local/share/hippo-bench/claims-01" --limit 50
mise run bench:claims -- assess \
  --corpus "$HOME/.local/share/hippo-bench/claims-01" \
  --run "$HOME/.local/share/hippo-bench/claims-run-01" --max-requests 50
```

Prepare reads one SQLite transaction. It includes the most recent nodes, including
nodes with missing evidence. This is a descriptive cohort, not a representative
holdout. Evidence comes from eligible linked capture rows. Up to eight sources,
6,000 characters per field, and 4,000 summary characters are retained. Workflow
evidence includes up to 100 annotations with job attribution; corrections change
packet identity. JSON fields are decoded before redaction. Known capture truncation,
export truncation, missing source, malformed JSON, or oversized input blocks automatic assessment. The
export cannot prove the original capture was complete.

Assess requires `TYPESAFE_API_KEY` and explicitly sends bounded, redacted packet
state to TypeSafe. Human labels never enter the request. It pins `jev-1.13.0`,
makes at most `--max-requests` assessment attempts, and defaults to a ten-second
timeout per attempt. Credentials are checked before reserving the run directory.
Interrupted runs retain completed records; missing records
remain visible. A rerun uses a new output directory.

## Review a bounded queue

```sh
mise run bench:claims -- queue \
  --corpus "$HOME/.local/share/hippo-bench/claims-01" \
  --run "$HOME/.local/share/hippo-bench/claims-run-01" \
  --confidence 0.9 --limit 20 --audit 5 --seed review-01 \
  --out "$HOME/.local/share/hippo-bench/claims-queue-01.json"
mise run bench:claims -- review \
  --corpus "$HOME/.local/share/hippo-bench/claims-01" \
  --run "$HOME/.local/share/hippo-bench/claims-run-01" \
  --queue "$HOME/.local/share/hippo-bench/claims-queue-01.json" \
  --reviewer YOUR_NAME --out "$HOME/.local/share/hippo-bench/claims-labels-01"
```

The confidence value is an exploratory policy, not a calibrated recommendation.
The queue reserves five seeded random audit cases from the full cohort and fills
remaining places with unresolved cases. It can contain fewer than 20 items.
Select the seed before inspecting outcomes. Reseeding or stopping after particular
answers undermines unbiased audit interpretation. New queues may overlap; this
initial workflow reports each frozen queue separately.

The selected items use a separate seeded presentation order that does not group
audits or sort by confidence. The terminal shows the summary, captured fields, and coverage gaps. Jev's verdict
and sampling stratum are hidden. Use `y`, `n`, or `u` for yes, no, or unsure. Add
optional text after the decision; `s` skips and `q` exits. Each completed review
is saved immediately. EOF and interruption preserve previous annotations.
Repeat the same review command to resume; already annotated packets are skipped.
Missing or truncated summary text cannot receive a yes label. Use no or unsure,
add notes, or skip until the complete summary is available. Reports also reject
previously saved positive labels for incomplete summaries.

No includes unsupported or contradicted claims. Unsure remains unresolved. A
claim that tests passed needs supporting observations; a transcript saying so
supports an attributed report, not independent execution success. Record outside
knowledge in notes without treating it as evidence present in the packet.

## Inspect the report

```sh
mise run bench:claims -- report \
  --corpus "$HOME/.local/share/hippo-bench/claims-01" \
  --run "$HOME/.local/share/hippo-bench/claims-run-01" \
  --queue "$HOME/.local/share/hippo-bench/claims-queue-01.json" \
  --labels "$HOME/.local/share/hippo-bench/claims-labels-01" \
  --out "$HOME/.local/share/hippo-bench/claims-report-01.json"
```

Reports retain missing/error/budget states and separate random audits from
targeted exceptions. False approvals and rejections include their reviewed
denominators; unsure and unreviewed rows remain explicit. The output never claims
population accuracy or release acceptance. Unknown token usage is not zero.

Artifacts bind decisions to source/summary/rubric identities. Re-export after
evidence changes, and reassess after rubric/model changes. Corrections to labels
use a new directory; do not mix competing annotations in one report. Private
artifacts use 0600 files and 0700 leaf directories. Complete files are published
atomically without overwriting existing artifacts. Keep them outside Git.

The accepted [specification](superpowers/specs/2026-09-25-claim-review-design.md)
defines the evaluation boundary and independent acceptance requirements.
