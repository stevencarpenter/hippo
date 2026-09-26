# Jev-assisted enrichment fidelity review

Status: accepted for implementation on September 25, 2026.

## Contract

Evaluate whether a knowledge node's summary is supported by its linked captured
source. Jev supplies a typed judgment. Human reviews establish calibration and
record corrections. All decisions are advisory and stored outside the production
database. No automatic trust publication, answer suppression, or source rewriting
is enabled by this workflow.

The first review unit is the complete existing summary. Every proposition must be
supported for a yes decision. This avoids introducing an unvalidated claim
extraction model. Compound summaries may need more human review. A transcript
reporting success supports the attributed report, not independently verified
success. Historical support does not establish present truth.

## Evidence and decisions

1. Export a bounded cohort of recent knowledge nodes through a read-only SQLite
   transaction. Load linked shell, agentic, browser, workflow, and memory evidence
   through existing eligibility checks. Preserve missing and excluded sources as
   coverage problems rather than silently dropping the node.
2. Freeze the redacted summary, source fields, source identities, revision hashes,
   capture metadata, and coverage problems in a content-addressed packet. Source
   fields remain distinct from the derived summary. Keep attribution and temporal
   qualifiers in the evidence. Bound source and payload sizes; truncation prevents
   automatic disposition. Captured fields are not proof of complete original logs.
3. Ask a pinned Jev Choice whether the source supports, contradicts, or leaves the
   summary unsupported. Reuse the existing client, response validation, redaction,
   timeout, and cooldown. Code owns hashes, sizes, ordering, and admission checks.
   The checked-in rubric defines the semantic contract.
4. Record exact request identity, full typed response, model, rubric, elapsed time,
   and errors. Missing evidence and transport failures remain unresolved. A
   threshold supplied explicitly by the operator produces a hypothetical
   automatic disposition, never a production approval. Confidence is not an
   empirical error probability.
5. Select a bounded review queue with a seeded random audit reservation plus
   unresolved exceptions. Audit selection is independent of the model verdict
   and includes automatic approvals and rejections. Preserve sampling provenance.
   Hide predictions during annotation. Accept yes, no, unsure, and optional notes.

## Human labels and learning

An annotation records packet hash, named reviewer, explicit human/model origin,
decision, notes, and Unix millisecond timestamp. Yes means the entire summary is
supported by the displayed evidence. No means at least one proposition is not
supported, including contradiction. Unsure is unresolved, not a negative label.
Notes are retained verbatim as review evidence and are never instructions to the
verifier. An attestation based on outside knowledge belongs in notes and does not
silently convert source fidelity into real-world verification.

Labels bind to the frozen packet. A changed source, summary, or rubric cannot
reuse a previous assessment as current. Re-export creates new packet identities.
Run and label files are immutable; corrections use a new annotation file. Reports
reject mismatched identities and duplicate/conflicting labels instead of choosing
one silently. Human assertions of reviewer identity are recorded, not authenticated.

Reviewed failures inform evidence selection and rubric changes. Development and
acceptance cohorts must be separately frozen and reviewed. No automated training
or promotion occurs. Jev does not offer customer-specific fine-tuning. A changed
rubric or model requires fresh qualification. Related sessions and duplicate
claims need a family audit before any independent-holdout claim.

## Measurements

Report planned packets, completed decisions, coverage failures, inference errors,
hypothetical automation coverage, reviewed approvals, false approvals, reviewed
rejections, false rejections, unsure labels, and unreviewed decisions. Keep the
random audit stratum separate from targeted exceptions. Report observed error
counts and denominators; incomplete or biased review is not population accuracy.
Zero observed errors is not a zero-error guarantee. Threshold selection and formal
acceptance are separate from these descriptive reports.

Select a tolerated false-approval rate and minimum useful automation coverage
before acceptance evaluation. Assess confidence bins and claim/source types on
independent labels. Set acceptance sample size from that error tolerance and
family dependence. Human review time and Jev usage determine cost per useful
decision. Service failures remain in operational denominators.

## Persistence and execution

Use private, exclusive-create JSON artifacts under an explicit external directory.
Directories use mode 0700 and files 0600. Refuse repository and production output
paths. Captured data, predictions, human notes, and generated reports never enter
Git. Commit the specification, rubric, implementation, and synthetic tests only.

The CLI provides prepare, assess, queue, review, and report operations through a
mise task. Preparation and review are local. Only explicit assess execution sends
bounded redacted evidence to TypeSafe. Request count and timeout are bounded.
Runs retain partial/error results and refuse overwrite. A terminal review loop
provides immediate human participation; the swipe interface is outside this
implementation. No new dependency, production migration, or background service is
required.

## Stack and verification

1. Specification and versioned rubric.
2. Frozen evidence packet export and integrity checks.
3. Jev shadow assessment, bounded review queue, annotations, and descriptive report.

Tests use synthetic SQLite and mocked HTTP responses. Verify source corrections
change packet identity, missing/truncated evidence cannot auto-resolve, labels
never enter model state, invalid responses fail closed, random audit selection is
reproducible, unknown annotations fail, and unsure/unreviewed cases remain visible.
Run the canonical Python suite and lint before publishing the implementation.

## Evidence for this decision

The September 18 synthetic experiment classified 30/32 verification cases
correctly, with no false supports among 23 negative cases. One wrong three-way
verdict had confidence 0.86. A post-hoc threshold of 0.9 retained 25/32 correct
verdicts; it is diagnostic evidence, not a production threshold. The follow-up
cases were authored after initial results and are not an independent holdout.
See [decision sidecars](../../decision-sidecars.md) and the external archive
`~/.local/share/hippo-bench/decisions/archive/typesafe-2026-09-18/`.

Current TypeSafe contracts: [citation checks](https://docs.typesafe.ai/cookbooks/citation_check),
[confidence](https://docs.typesafe.ai/confidence),
[model versions and customization](https://docs.typesafe.ai/models), and
[known model limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
