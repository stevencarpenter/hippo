# Q/A Fixture Annotation Pipeline (BT-21 audit)

This document records how `golden_event_id` values get into the
hippo-bench v2 Q/A fixture, identifies the leakage risk surface, and
gives a guideline for keeping future annotation provenance clean.

## Pipeline (as of v0.21.1, branch `feat/bench-trust`)

```
brain/src/hippo_brain/bench/qa_template.jsonl    (100 items, golden_event_id=null)
        │
        │  qa_seed.seed_qa_fixture()  — verbatim copy, no transformation
        ▼
~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl
        │
        │  *** OPERATOR ANNOTATES HERE ***
        │  This is the unmanaged step in the current pipeline.
        ▼
~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl   (with populated golden_event_ids)
        │
        │  load_qa_items(qa_path, corpus_event_ids)
        │   ├─ filters items whose golden_event_id ∉ corpus_event_ids
        │   └─ returns (included_items, filtered_count)
        ▼
run_downstream_proxy_pass(conn, included_items, embedding_fn, search_fn)
```

## What ships in the template (audited 2026-05-03)

- 100 Q/A items, all with `golden_event_id: null`
- All 100 items have **non-empty** `acceptable_answer_keywords` (3+ keywords each)
- Stratification by `source_filter`: shell 40 / claude 30 / browser 20 / workflow 10
- Tag distribution (top 5): `lookup+single-event` (55), `how-it-works` (18),
  `why-decision` (12), `state-lookup` (8), `diagnostic+lookup` (3)
- Schema fields: `qa_id`, `question`, `golden_event_id`, `source_filter`,
  `acceptable_answer_keywords`, `tags`

The template is **deliberately unannotated**. There is no committed
`golden_event_id` to risk leaking — the field is null. Operator
annotation is the only path to populated goldens.

## Leakage risk analysis

The methodology panel's concern was that **golden_event_ids might be
drawn from a prior retrieval run**, baking the retrieval's biases into
the metric the bench is supposed to validate. Three possible leakage
modes:

### Mode A: operator runs retrieval, picks top result as golden

Risk: **HIGH** if the operator does this. The bench then measures
"how well does the retriever match itself" rather than "how well does
retrieval find the right event." This is the panel's documented
concern.

Mitigation: documented below. Code cannot prevent this — it's a
workflow discipline issue.

### Mode B: corpus event chosen first, question written to match

Risk: **LOW** when done well. This is the recommended workflow:
operator finds an interesting event in the live hippo.db, picks it as
the golden, *then* writes a question whose plain-language wording
doesn't lift directly from the event content. Provided the question
isn't a paraphrase of the event content, retrieval has to actually
embed-match or keyword-match correctly to find it.

Caveat: a question that uses the same nouns as the event content can
inadvertently leak. "What command did I run for `git status`" with a
golden whose content is `git status` is leakage-by-mention. Prefer
"what did I check the working tree status with."

### Mode C: synthetic question, no golden in the corpus

Risk: **LOW for retrieval, but creates filtered-out items**.
`load_qa_items` drops items whose `golden_event_id` isn't in the
sampled corpus, returning them as `filtered`. These don't pollute the
score but reduce statistical power.

## Provenance: how were template golden_event_ids produced?

**They were not produced.** Every item in `qa_template.jsonl` has
`golden_event_id: null` (verified 2026-05-03 — `grep -c
'"golden_event_id":null' qa_template.jsonl` returns 100/100). The
template is a question scaffold, not an annotated fixture. The leakage
risk lives entirely on the operator side at annotation time.

## Recommended annotation guidelines

When populating `golden_event_id` for the Q/A fixture:

1. **Find the event first, write the question second.** Look at
   knowledge nodes / events in the corpus, identify ones that exemplify
   each tag dimension, write a plain-language question for each.
2. **Don't paraphrase the event content into the question.** If the
   event is "ran `cargo build --release`," ask "how do I build the
   release binary," not "what was that cargo build release command."
3. **Do not run retrieval to find the golden.** If you need a hint,
   browse the corpus by source/timestamp, not by query.
4. **Cross-reviewer label a 10% sample.** Have a second person read 10
   randomly-chosen Q/A items and confirm the chosen golden is the most
   appropriate event in the corpus. Disagreements → re-annotate.
5. **Record annotation provenance.** When committing a populated
   fixture, include a note in the commit message: "annotated by <person>,
   <date>, against corpus sha256=<hash>". This lets future readers tell
   v2.1's annotations apart from v3's.

## Statistical power note

The methodology panel reported "~13 scoreable items, MRR SE ~0.07–0.09"
based on the **v1 fixture** at `brain/tests/eval_questions.json` (40
items with adversarial overlay reducing scoreable count). The v2
template ships with **100 items** — once annotated, statistical power
should be substantially higher. Concrete numbers depend on how many
items have non-null `golden_event_id` after annotation and how many
golden events are present in any given run's corpus sample.

Per the methodology panel's recommendation, the target is **≥150
scoreable items** for reliable model ranking at p<0.05. The current
template is 100; expansion to 150+ is tracked under BT-23 (Phase 2
sketches, currently blocked pending design review).

## What this audit does NOT cover

- BT-22 (populating `acceptable_answer_keywords`) — out of scope here;
  audited separately. **Note:** the v2 template already has those
  populated (verified 100/100). The methodology panel's
  `keyword_hit_rate=0.000` finding was against the **v1** fixture at
  `eval_questions.json`, not against this v2 template.
- Corpus sampling / stratification — covered by `corpus_v2.py` tests.
- Adversarial overlay annotation — different schema, separate file.

## References

- Template: `brain/src/hippo_brain/bench/qa_template.jsonl`
- Seed code: `brain/src/hippo_brain/bench/qa_seed.py`
- Filter logic: `brain/src/hippo_brain/bench/downstream_proxy.py::load_qa_items`
- Methodology panel report: PR #127 review (Copilot + Codex, 2026-05-03)
- Tracking: `docs/superpowers/plans/2026-05-03-hippo-bench-trust-tracking.md` BT-21
