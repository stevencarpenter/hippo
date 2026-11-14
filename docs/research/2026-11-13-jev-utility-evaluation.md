# Jev utility evaluation, 2026-11-13

This record gives the measurements behind one change and two decisions: the `note-v1` rerank recipe, the decision to leave topic retrieval off, and the decision not to adopt a candidate classification recipe. All labels came from model judges, not from people. The human-review criteria of the [approved specification](../superpowers/specs/2026-09-22-jev-retrieval-classification-design.md) remain unmet.

## Data

| Item | Value |
| --- | --- |
| Snapshot | SQLite online backup of the production database, 40,617 nodes |
| Queries | 90 distinct real queries from agent transcripts (2026-10-03 to 2026-11-12), 27 projects |
| Tools | `search_hybrid` 49, `ask` 34, `search_knowledge` 5, `agent_query` 2 |
| Split | 51 tune, 39 holdout, assigned by a hash of the query text |
| Pools | Up to 30 candidates per query from `retrieval.search`; 87 queries had at least 2 candidates |
| Classification sample | 160 nodes, stratified by stored probability for each of the 14 topics |

Each query kept the `limit`, `project`, and `mode` arguments of the original call. Queries with other filters were excluded.

## Method

Nine judges graded every candidate of every pool (2,579 query-candidate pairs) on a 0 to 3 scale. Each judge saw the query, the project scope, and the full note text. Candidate order was shuffled, so no judge saw the order of any arm. A grade of 2 or 3 counts as relevant.

Two judges graded the 14 topics for each of the 160 nodes on a 0 to 2 scale. A grade of 2 means a substantive subject, and 1 means a minor or borderline subject.

| Agreement check | Pairs | Result |
| --- | --- | --- |
| Relevance, double-judged | 270 | Exact 0.92, relevant or not 0.93, kappa 0.78 |
| Topics, double-judged | 224 | Substantive or not 0.996, kappa 0.96 |

Ranking quality is nDCG at each query's own `limit`, with gain `2^grade - 1`. Intervals are 95% paired bootstrap intervals over queries. Quality arms ran with a 30-second timeout, so the 2-second production deadline did not affect them.

## Rerank results

### Primary result: notes written before the query

The snapshot was taken after every query was asked, so a pool can hold notes that did not exist at query time. 1,064 of 2,568 pool candidates (41%) were dated after their query, and they include 353 of the 554 candidates judged relevant. Many of them record the session that issued the query. The table below scores each arm on the candidates dated before the query. It is the result to rely on.

| Arm | Tune nDCG (n=47) | Holdout nDCG (n=35) | All nDCG (n=82) |
| --- | --- | --- | --- |
| Retrieval order | 0.656 | 0.826 | 0.729 |
| `multi-v1` (shared list, 90 Scores) | 0.783 | 0.813 | 0.796 |
| Note, one Noul per candidate | 0.830 | 0.910 | 0.864 |
| Note with identifiers (`note-v1`) | 0.847 | 0.906 | 0.873 |
| Note, one 4-level Score per candidate | 0.862 | 0.890 | 0.874 |
| Note, Noul, `jev-1.14.0` | 0.871 | 0.901 | 0.884 |

| Difference | Tune | Holdout | All |
| --- | --- | --- | --- |
| `multi-v1` minus retrieval order | +0.127 [+0.047, +0.216] | -0.013 [-0.103, +0.069] | +0.067 [+0.009, +0.126] |
| `note-v1` minus retrieval order | +0.191 [+0.101, +0.287] | +0.080 [+0.027, +0.136] | +0.144 [+0.089, +0.204] |
| `note-v1` minus `multi-v1` | +0.064 [-0.005, +0.137] | +0.093 [+0.008, +0.179] | +0.077 [+0.022, +0.132] |

- `note-v1` improves on retrieval order in both halves.
- `multi-v1`, the recipe that ran in production until this change, shows a smaller gain that does not appear in the holdout half.
- The four note variants do not differ from each other beyond noise. The identifier variant was chosen after the holdout numbers were seen, so its margin over `multi-v1` is not a clean held-out result.
- `jev-1.14.0` showed no reliable gain, so the pinned model stays `1.13.0`.
- These pools hold fewer candidates than a production pool at query time (18 on average, against 30), because later notes were removed and not replaced.

### All pool candidates, including later notes

| Arm | Tune nDCG (n=49) | Holdout nDCG (n=38) | All nDCG (n=87) | All top-1 relevant |
| --- | --- | --- | --- | --- |
| Retrieval order | 0.581 | 0.708 | 0.636 | 0.644 |
| `multi-v1` (shared list, 90 Scores) | 0.813 | 0.832 | 0.822 | 0.782 |
| Note, one Noul per candidate | 0.871 | 0.863 | 0.868 | 0.851 |
| Note with identifiers (`note-v1`) | 0.883 | 0.897 | 0.889 | 0.885 |
| Note, one 4-level Score per candidate | 0.891 | 0.867 | 0.880 | 0.816 |
| Note, Noul, `jev-1.14.0` | 0.897 | 0.857 | 0.880 | 0.874 |

- `multi-v1` minus retrieval order, all queries: +0.186 [+0.129, +0.246].
- `note-v1` minus `multi-v1`, all queries: +0.067 [+0.018, +0.120].
- These gains are larger than the primary result because later notes, which are often highly relevant, sat low in retrieval order.

The production code path (`rerank_results` with the configured default recipe, `note-v1`) was then run on the holdout queries with all pool candidates. It scored nDCG 0.895 (n=37), against 0.897 for the experiment arm on the same queries. This run shows that the shipped code reproduces the experiment arm. It is not an independent test of the recipe.

After code review, the shipped recipe gained two things: a statement in each question that the note is untrusted text, and the note's branch. The shipped recipe was run again on the holdout queries. On notes dated before the query it scored nDCG 0.911 (n=35), against 0.906 for the experiment arm: a difference of +0.005 [-0.004, +0.016]. Its median request was 14,405 input tokens.

| Request cost | `multi-v1` | Note | `note-v1` |
| --- | --- | --- | --- |
| Input tokens, median | 23,324 | 10,937 | 13,458 |
| Payload bytes, median | 88,296 | 40,454 | 49,301 |
| HTTP time, median / 95th percentile (ms) | 280 / 376 | 216 / 336 | 193 / 311 |

No request in any arm took more than 2,000 ms. The production deadline misses therefore came from the client side, not from Jev latency.

Retrieval order leaves room for a reranker. Of 554 relevant candidates, 316 sat below the query's `limit` in retrieval order. Four of 87 pools held no relevant candidate.

### Calibration and a relevance floor

The `note-v1` Noul tracks judged relevance:

| Noul | 0.0 to 0.1 | 0.2 to 0.3 | 0.4 to 0.5 | 0.5 to 0.6 | 0.7 to 0.8 | 0.9 to 1.0 |
| --- | --- | --- | --- | --- | --- | --- |
| Share judged relevant | 0.03 | 0.28 | 0.49 | 0.67 | 0.79 | 0.87 |

A floor would drop delivered results below a Noul value. No floor is implemented; the `relevance` field lets a consumer apply one.

| Floor | Results per query | Precision | Relevant kept | Queries left empty |
| --- | --- | --- | --- | --- |
| None | 6.21 | 0.628 | 1.000 | 0 |
| 0.2 | 4.97 | 0.736 | 0.938 | 3 |
| 0.3 | 4.09 | 0.809 | 0.850 | 5 |
| 0.5 | 3.01 | 0.870 | 0.673 | 10 |

## Topic retrieval results

All tests used the snapshot with the classification rows of 2026-10-12 restored, so 39,840 nodes had labels.

| Test | Result |
| --- | --- |
| Alias channel (`topic_retrieval = true`) | A topic matched 5 of 90 queries. The pool changed for 2. nDCG change -0.005 [-0.014, 0.000]. |
| Channel fed by Jev query topics | 53 of 87 queries had a topic at 0.7 or above. The pool changed for 29. Of 103 admitted candidates, 8 had a Noul of 0.5 or above. The channel displaced 10 candidates judged relevant. |
| Topic agreement as a signal | AUC 0.61 for separating relevant from other candidates in a pool. The relevance Noul scored 0.94. |
| Topic agreement added to the Noul | Holdout nDCG change between -0.006 and -0.018 for weights 0.05 to 0.4. |

No tested use of the 14 topics improved retrieval.

## Classification results

Accepted labels on the 160-node sample:

| Arm | Threshold | Accepted | Substantive precision | Lenient precision | Recall of substantive |
| --- | --- | --- | --- | --- | --- |
| Stored production labels | 0.8 | 176 | 0.557 | 0.858 | 0.748 |
| Note state, earlier wording | 0.8 | 182 | 0.588 | 0.907 | 0.817 |
| Earlier state, per-topic questions | 0.8 | 123 | 0.715 | 0.959 | 0.672 |
| Note state, per-topic questions | 0.8 | 128 | 0.711 | 0.961 | 0.695 |
| Note state, per-topic questions (candidate recipe) | 0.7 | 164 | 0.634 | 0.939 | 0.794 |

- Candidate recipe at 0.7 minus stored labels at 0.8: substantive precision +0.077 [+0.008, +0.147], lenient precision +0.082 [+0.027, +0.138], recall +0.045 [-0.042, +0.138].
- The mean `capture-ingestion` probability on nodes that judges graded 0 fell from 0.57 to 0.07.
- `testing-validation` recall fell (13 of 24 substantive nodes accepted, against 19 of 24).

The candidate recipe was not adopted, because no reader gains from the labels. It asks one plain question per topic, with a concrete description of what counts and what does not, and it sends only the note's summary, intent, decisions, problems, and details. The built-in recipe also sends the note's source and node type, which is why ordinary shell notes scored high for `capture-ingestion`. The candidate wording is in `class_new_wording.json`, and the code change is in `classification-v2-and-durability.patch`, both in the artifact directory below.

A second defect is recorded and not fixed. A writer process that never calls `classification.configure` is treated as a disabled writer. When it changes the input of a classified node, it marks the row `skipped` with `classification disabled` and clears the result. A re-embed run did this to 14,635 rows on 2026-10-12. The same patch holds a fix.

## Limits

- Model judges produced every label. A person has reviewed none of them.
- The node sample was drawn by the stored probabilities, so it favors the stored labels on recall.
- The queries came from agent transcripts of one user. Shell and browser use is not represented.
- The pools came from one snapshot. Pool quality at other times was not measured.
- No test measured whether a better ranking changes what an agent does next.
- The calibration, floor, and topic tables use all pool candidates, including notes dated after the query.

## Cost and artifacts

The experiment sent 11.8 million input tokens to TypeSafe, about $0.50 at $0.042 per million. Data sent: redacted query text and redacted note text, the same classes that production already sends.

All inputs, raw answers, judge packets, and labels are in `~/.local/share/hippo-bench/decisions/retro-20261113/`, outside the repository and the production data directory. `eval.py`, `class_eval.py`, and `analysis2.py` in that directory reproduce every table from the stored answers without a Jev call.
