# Hippo 1.0 acceptance status, updated 2026-10-05

**Decision: hold.** The package version remains 0.37.0. The existing
[acceptance contract](../../config/agent-benefit-v2.json) is unchanged.
Software regression results and the selected historical diagnostic case do
not establish population benefit or human-reviewed answer accuracy.

## Human review evidence

Two independently ordered packets are ready for one historical diagnostic
case. Each contains 419 manifest-bound evidence files, ten blank judgments,
a blank reviewer identity form, and instructions. Five source files and
seven task/submission artifacts remain readable. The other 407 source paths
contain explicit privacy placeholders. Both packets pass the exporter's
completeness audit. Their `acceptance_eligible` value is false. Two other
historical cases remain incomplete because required source or historical
execution evidence is unavailable.

The private review entry point is:

```text
~/.local/share/hippo-bench/v1-readiness/2026-10-04-followup/packets/handoff-v2/START-HERE.txt
```

The adjacent `human-reviewer-1.zip` and `human-reviewer-2.zip` contain opposite
A/B presentation orders. Assign each to a different independent human.
Starting takes under two minutes. Allow 45 to 60 minutes per review as a
planning estimate, not a measured duration. `handoff-status.json` records
artifact hashes and completeness. The original full-source ZIPs are
superseded for sharing and remain private historical evidence. Two diagnostic
human returns are now available; they do not supply the missing prospective
study cohorts or qualify this historical case for acceptance.

Six automated pre-handoff reviews completed 117 integrity/provenance/archive
checks and 29 synthetic return-contract tests. The owner importer validates
returned forms against separately verified reviewer identities. These checks
establish packet integrity and processing behavior, not candidate success.
The packets contain no historical execution logs or measured generated PR
metadata. Human reviewers must mark conclusions that require absent facts
as missing evidence. The shared adjudicator rejects whitespace-only
rationales and pass judgments supported only by redacted evidence.

The existing evidence audit verified 125 original review-file hashes,
65 archived hashes, and nine mechanism-evidence hashes. The registered
reviewers are AI reviewers, and neither historical 100-row query queue has
completed human labels. AI reviews do not satisfy the human-review contract.

## Consecutive-window evidence

The read-only census at 2026-10-05 04:51:40 UTC found 673 captured segments
across 184 readable source files. Explicit automation excludes 127 sources;
57 sources have unresolved origin. No qualified task families were established.

A bounded private export preserves 59 Codex execution batches containing
80 syntactic invocation sites and 14 Pi calls, including initial responses
and observed asynchronous waits. All 57 source prefixes match the frozen
inventory. Two files subsequently grew; extraction used their verified
prefixes. These are candidate records, not an adjudicated accuracy sample.

All ten inspected Pi evidence references resolve to database rows. Six
transcript paths exist, one original transcript is missing, and three shell
events correctly have no transcript path. Human origin, independent task
families, batch attribution, factual eligibility, and source support remain
unqualified. `packets/call-frame-disposition.json` records the exact disposition.

The contract requires at least 100 primary task pairs, its specified power
and benefit thresholds, 300 natural factual calls, 100 answerable families,
and 100 audited absent controls. It also requires independent human review
and the stated accuracy confidence bounds. Zero acceptance-eligible cases
are established by this audit. No additional model trials were launched.

## Operational and dependency evidence

The read-only production snapshot at approximately 2026-10-05 04:51 UTC
reported schema 25, WAL mode, zero foreign-key violations, zero active
capture alarms, and successful SQLite quick and full integrity checks.
Settled source cursors were current for 166 Codex, eight Cursor, and
105 Pi files modified during the preceding seven days.

The preceding 24 hours contained 60 real shell events, 186 Codex segments,
one Cursor segment, and 61 Pi segments. Browser and Claude-tool capture each
had zero real events and 162 probes. The observed daemon counted probes in
production health fields. The proposed fix separates real activity from
probe liveness and repairs those fields during the periodic refresh.
Successful probes are evidence of the tested capture path, not user activity
or Firefox extension connectivity.

Current lockfile scans reported no known advisories: cargo-audit 0.22.1
checked 323 Rust packages; OSV Scanner 2.3.3 checked 69 Python packages.
The shell secret-pattern guard passed. The October 5 follow-up separately
audited the six pinned Python build dependencies and added recurring CI
checks. `security/report.md` retains the original core scope, scanner
versions, advisory database identity, and lockfile hashes.

The [optional dependency disposition](2026-10-05-dependency-disposition.md)
records the subsequent Firefox/site fixes, digest-pinned observability
images, and remaining upstream advisories. This broader audit does not
establish a general security certification. The Grafana panel description
and seconds unit were visually checked in an isolated Grafana 12.4.4 fixture.
That check used no production data and does not establish current production
dashboard behavior or numeric time-series rendering.

## Release verification scope

The integrated `mise run test` gate passed: 1,978 Python tests passed with
one expected failure and 85% coverage; Rust tests, clippy, formatting, and
installer checks passed. A separate Rust run with default features disabled
passed 762 tests. Release-version preflight and workflow actionlint passed.

The [release process](../release.md) validates matching stable versions,
runs reusable Rust/Python/installer checks, and supports manual candidate
builds without publishing a release. The artifact smoke installs supplied
packages under isolated HOME/XDG directories and exercises configuration,
reinstall preservation, daemon startup, brain health, lexical retrieval,
HTTP input errors, and packaged shell capture. It uses no live model service
and does not register LaunchAgents.

Directory and archive smokes passed with distinct XDG directories. Cancellation
and resistant child/grandchild cleanup checks also passed. Compact proofs are
retained in `installation/summary.json` and the referenced files.

The initial local artifact rehearsal passed the runtime checks. Its
release build wrote `target/release/hippo`, which is the target of this
machine's installed `~/.local/bin/hippo` symlink. That changed the installed
executable on disk. Existing services were not restarted. Subsequent release
builds used an isolated target directory. These results establish fixture
behavior; they do not assert that the running production services contain
the proposed fixes.

Review, intake, capture, dependency, and installation evidence is under the
dated directory above, with directories
restricted to mode 0700 and files to mode 0600. Captured transcripts,
reviewer packets, and raw call frames are excluded from the repository.
