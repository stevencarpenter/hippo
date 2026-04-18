# Migration Script Safety Audit — v5→v6

**Reviewer:** migration-reviewer (adversarial)
**Date:** 2026-04-18
**Script:** `brain/scripts/migrate-v5-to-v6.py` (commit 6ef3e65)
**Corpus at risk:** ~2,000 knowledge nodes, 8,000+ events

---

## TL;DR Verdict

**SAFE TO DRY-RUN — but three HIGH issues must be fixed before real-DB cutover.**

The `--dry-run` path is correctly gated: every mutating phase short-circuits on the flag, subprocesses are either suppressed (phase 5) or called with their own `--dry-run` (phases 6–7), and `_synthetic_round_trip` uses a savepoint that always rolls back. Run the dry-run.

**Do not run without `--dry-run`** until H1, H2, and H3 are remediated. H1 (missing FTS backfill) will cause a hard failure at phase 8 verify on every real run. H2 (no backup integrity check) means you could proceed without a valid fallback. H3 (the 50% deletion gate logs a warning and continues) means a misconfigured eligibility filter could silently delete over 1,000 nodes.

---

## Adversarial Scenario Results

### S-01 — Crash between phases

**Severity: MED | Confidence: 95%**

If the process crashes (SIGKILL, OOM) between phases, the DB state depends on where:

- **Crash during any `with conn:` phase (3, 4):** SQLite WAL rolls back uncommitted changes on next open. Safe.
- **Crash after phase 2 (schema-forward):** `user_version` is at 6 (PRAGMA runs in autocommit via `executescript` side-effect). A rerun hits the phase 1 preflight check `version != 5` → `RuntimeError: expected schema_version=5, got 6`. **The script stops with no documented recovery path.**

The user can recover by rerunning with `--skip-phase preflight --skip-phase schema-forward`, but the error message doesn't say this. Phases 2–8 are all idempotent (IF NOT EXISTS DDL, WHERE-NULL re-embed logic, savepoint-protected verify).

**Remediation:** Improve the `version == 6` error message to include the recovery command.

```python
# migrate-v5-to-v6.py:202
if version != 5:
    raise RuntimeError(
        f"preflight: expected schema_version=5, got {version}. "
        "Already migrated, or wrong DB? "
        "If schema was already bumped, resume with: "
        "--skip-phase preflight --skip-phase schema-forward"
    )
```

---

### S-02 — LM Studio garbage mid-re-embed

**Severity: LOW | Confidence: 90%**

`migrate-vectors.py` wraps each `embed_knowledge_node` call in try/except; failures increment `failed` and return exit code 1. The outer `_run_subprocess` raises `RuntimeError` on non-zero exit, halting migration before verify.

On rerun, `migrate-vectors.py` queries `WHERE v.knowledge_node_id IS NULL` — so successfully-embedded nodes are skipped. Resume is clean.

The one gap: if LM Studio silently returns a wrong-dimension vector that `embed_knowledge_node` accepts without raising, that bad vector lands in vec0 and passes count-parity at phase 8. The count check does not validate UUID alignment or vector dimensionality.

See S-08 for the verify-phase gap this creates.

---

### S-03 — Daemon starts during phase 3+

**Severity: LOW | Confidence: 85%**

Phase 1 checks `_daemon_is_running` once at startup. No mechanism prevents the daemon from being started by launchd or the user during phases 3–7.

If the daemon starts and writes new events → brain enriches → new `knowledge_nodes` rows appear between phase 4 (noise-cleanup) and phase 8 (verify), the new nodes have no corresponding vec rows, causing verify to fail. In practice the daemon writes to `events` not `knowledge_nodes`; the brain (also stopped) is the writer. The risk is low unless the user has both stopped.

Not a hard block: the daemon socket check at startup is sufficient given the documented requirement to stop services before running.

---

### S-04 — Corrupt backup

**Severity: HIGH | Confidence: 95%**

`brain/scripts/migrate-v5-to-v6.py:229–236`:

```python
backup_conn = sqlite3.connect(str(backup_path))
try:
    conn.backup(backup_conn)
finally:
    backup_conn.close()
log.info("[preflight] backup complete: %s (%.1f MB)", ..., backup_path.stat().st_size / 1e6)
```

The backup's size is logged, but the backup file is **never opened and verified**. If `conn.backup()` silently produces a truncated or corrupt file (e.g., out-of-disk-space condition that SQLite doesn't propagate as an exception), the script proceeds with an invalid fallback. The user has no warning until they attempt to restore.

**Remediation:** Open the backup and run `PRAGMA integrity_check` before proceeding:

```python
# After backup_conn.close():
verify_conn = sqlite3.connect(str(backup_path))
try:
    result = verify_conn.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise RuntimeError(
            f"[preflight] backup integrity check FAILED ({result}). "
            "Do not proceed. Check disk space and retry."
        )
finally:
    verify_conn.close()
```

---

### S-05 — Subprocess non-zero but partial state

**Severity: LOW | Confidence: 90%**

`_run_subprocess` streams subprocess stderr into the migration log and raises `RuntimeError` on non-zero exit code. Subprocess stderr is captured. Partial state (e.g., 30% of nodes re-embedded) is recoverable because phases 5–7 are resume-safe (re-embed uses LEFT JOIN WHERE NULL; backfill and dedup are idempotent). The migration log provides enough context for diagnosis.

No action required.

---

### S-06 — Ctrl-C mid-phase

**Severity: LOW | Confidence: 90%**

`KeyboardInterrupt` is a `BaseException`, not caught by `except Exception`. It propagates through `with conn:` context managers, which call `conn.rollback()` on exception exit. The `finally: conn.close()` in `main()` also runs. SQLite rolls back uncommitted transactions on close.

For subprocess phases (5–7): the subprocess is in the same process group; SIGINT is delivered to it too. Python raises `KeyboardInterrupt` in the subprocess, which aborts. For phases 5 (re-embed), resume is clean (LEFT JOIN WHERE NULL). For phases 6–7, both use single-transaction commits, so SIGINT causes a rollback.

No action required.

---

### S-07 — `--dry-run` correctness

**Severity: NA | Confidence: 95%**

All eight phases correctly gate mutations on `dry_run`:

- Phases 1–5: short-circuit via `if dry_run: ...; return`
- Phase 5 subprocess: `_run_subprocess(..., dry_run=True)` returns immediately without launching
- Phases 6–7: intentionally pass `dry_run=False` to `_run_subprocess` but append `--dry-run` to the subprocess command. The subprocesses run (to report what they would do) but make no DB changes. This is correct and commented.
- Phase 8: count checks gated `if not dry_run:`; `_synthetic_round_trip` always runs but uses SAVEPOINT → rolled back regardless.

Dry-run is safe. No action required.

---

### S-08 — Verify phase logical gap

**Severity: HIGH | Confidence: 100%** ← **BLOCKING**

`brain/scripts/migrate-v5-to-v6.py:679–688`:

```python
if kn_count != fts_count:
    raise RuntimeError(
        f"[verify] row-count mismatch: knowledge_nodes={kn_count} "
        f"!= knowledge_fts={fts_count}"
    )
```

**FTS triggers do not backfill pre-existing rows.** SQLite `AFTER INSERT` triggers only fire for future inserts. The ~2,000 existing `knowledge_nodes` rows have no corresponding `knowledge_fts` rows after schema-forward.

Empirically verified:
```
nodes=2, fts=0   ← trigger does NOT backfill pre-existing rows
FTS is backfilled for pre-existing rows? False
```

The migration pipeline has no FTS backfill step. After a full real run:
- `kn_count` ≈ 2000 (minus noise-deleted nodes)
- `kv_count` ≈ 2000 (re-embedded by phase 5)
- `fts_count` = 0

Phase 8 **always fails** on a real run: `RuntimeError: kn_count=N != fts_count=0`.

The verify's synthetic round-trip test (`_synthetic_round_trip`) only confirms the trigger fires for *new* inserts — it does not expose the backfill gap.

**Remediation:** Add FTS backfill to phase 2 (after the DDL is applied):

```python
# migrate-v5-to-v6.py — add to phase_schema_forward after _V6_DDL + vec0 creation:

_SQL_FTS_BACKFILL = """
INSERT INTO knowledge_fts (rowid, summary, embed_text, content)
SELECT
    id,
    COALESCE(
        CASE WHEN json_valid(content) THEN json_extract(content, '$.summary') END,
        ''
    ),
    embed_text,
    content
FROM knowledge_nodes
"""

def phase_schema_forward(conn, dry_run, log):
    ...
    if dry_run:
        log.info(
            "[schema-forward] --dry-run: would apply v6 DDL, backfill FTS, "
            "and bump user_version to 6"
        )
        return

    with conn:
        conn.executescript(_V6_DDL)
        conn.execute(_SQL_CREATE_VEC_TABLE)
        conn.execute("PRAGMA user_version = 6")

    # Backfill FTS for all pre-existing knowledge_nodes.
    # Triggers only fire for future inserts; existing rows must be backfilled explicitly.
    node_count = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
    log.info("[schema-forward] backfilling FTS for %d existing knowledge_nodes", node_count)
    with conn:
        conn.execute(_SQL_FTS_BACKFILL)
    backfilled = conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0]
    log.info("[schema-forward] FTS backfill complete: %d rows", backfilled)
    if backfilled != node_count:
        raise RuntimeError(
            f"[schema-forward] FTS backfill incomplete: "
            f"expected {node_count} rows, got {backfilled}"
        )
```

Also add a test:
```python
def test_schema_forward_backfills_fts(tmp_db):
    conn = _open_v5(tmp_db)
    with patch.object(_mod, "_SQL_CREATE_VEC_TABLE", "SELECT 1"):
        _mod.phase_schema_forward(conn, dry_run=False, log=_dummy_log())
    kn_count = conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
    fts_count = conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0]
    assert kn_count == fts_count  # backfill happened
    assert kn_count > 0  # at least the fixture rows
```

---

### S-09 — Idempotency of phase 2

**Severity: NA | Confidence: 95%**

All DDL uses `IF NOT EXISTS`:
- `CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts`
- All three triggers: `CREATE TRIGGER IF NOT EXISTS ...`
- `CREATE TABLE IF NOT EXISTS embed_model_meta`
- `CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vectors`
- `PRAGMA user_version = 6` is a no-op if already 6.

Phase 2 can be run twice safely. The FTS backfill added by H1 remediation should also be idempotent — using `INSERT OR IGNORE` if the backfill query is run post-creation of the FTS table:

Actually, the backfill must guard against duplicate inserts if phase 2 is rerun after partial completion. Since the FTS table was empty before backfill, a second `INSERT INTO knowledge_fts SELECT FROM knowledge_nodes` would create duplicate FTS entries. The fix is to run backfill only when `COUNT(knowledge_fts) = 0`:

```python
fts_count = conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0]
if fts_count == 0 and node_count > 0:
    conn.execute(_SQL_FTS_BACKFILL)
    log.info("[schema-forward] FTS backfill complete")
else:
    log.info("[schema-forward] FTS already populated (%d rows), skipping backfill", fts_count)
```

---

### S-10 — Savepoint round-trip in verify

**Severity: NA | Confidence: 95%**

`brain/scripts/migrate-v5-to-v6.py:762–767`:

```python
finally:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("ROLLBACK TO SAVEPOINT synth_verify")
    conn.execute("RELEASE SAVEPOINT synth_verify")
```

Correct. `ROLLBACK TO SAVEPOINT` undoes all changes since the savepoint (but keeps the savepoint). `RELEASE SAVEPOINT` then pops it. Synthetic rows do not survive. `PRAGMA foreign_keys` is NOT transactional (takes effect immediately) but is correctly restored in the `finally` block regardless of exceptions.

The test `TestVerifySyntheticRoundTrip.test_synthetic_roundtrip_rolls_back` exercises this path and confirms zero row leak.

---

### S-11 — Backup filename collisions

**Severity: LOW | Confidence: 90%**

Timestamp format is `%Y%m%dT%H%M%SZ` (second-precision). Two runs within the same second would produce the same filename. `sqlite3.connect(str(backup_path))` opens an existing file if present; `conn.backup(backup_conn)` overwrites it.

In practice this is harmless: the preflight version check gates on `user_version == 5`, so the second run within the same second would be the first real run trying to proceed. The original backup (from what was probably a dry-run or test run) would be overwritten with an identical DB state. Not catastrophic.

Adding microseconds to the timestamp would eliminate the risk entirely:

```python
ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
```

No action required; LOW risk.

---

### S-12 — Schema.sql drift

**Severity: NA | Confidence: 95%**

The `_V6_DDL` in the migration script is not read from `schema.sql` at runtime — it is a hardcoded string. Comparison:

- `knowledge_fts` definition and all three FTS triggers: **match** schema.sql exactly.
- `knowledge_vectors` (vec0): the migration's `_SQL_CREATE_VEC_TABLE` matches `vector_store.py:_SQL_CREATE_VEC_TABLE` character-for-character.
- `embed_model_meta`: present in migration DDL but NOT in `schema.sql`. However, `vector_store.py:ensure_vec_table()` creates `embed_model_meta` idempotently on every brain boot via `IF NOT EXISTS`. Fresh v6 installs get it from the brain; migrated installs get it from phase 2. No functional gap.

The schema.sql comment explicitly notes that `knowledge_vectors` is not created there (the brain creates it on boot). The same rationale applies to `embed_model_meta`. No drift issue.

---

### S-13 — Permission / existence checks

**Severity: LOW | Confidence: 85%**

`main()` checks `db_path.exists()` before opening a connection and returns 1 if missing. A fresh install without a DB would fail cleanly.

Backup write permission: `sqlite3.connect(str(backup_path))` raises `OperationalError` if the parent directory doesn't exist or is not writable; this is caught by `except Exception` in `main()` and logged appropriately.

The `_setup_logging` call uses `log_path.parent.mkdir(parents=True, exist_ok=True)`, which handles missing log directories. No issues.

---

### S-14 — Noise cleanup policy vs live corpus

**Severity: HIGH | Confidence: 95%**

`brain/scripts/migrate-v5-to-v6.py:511–518`:

```python
if len(noise_ids) > 0.5 * len(node_ids):
    log.warning(
        "[noise-cleanup] WARNING: deleting >50%% of nodes (%d/%d). "
        "Inspect before proceeding. Continuing anyway.",
        len(noise_ids),
        len(node_ids),
    )
```

**The 50% safety gate logs a warning and continues.** A user who passed `--yes-drop-noise` acknowledged "irreversible deletion" but not "deletion of over half your knowledge base." The flag's help text says "Acknowledge irreversible noise-cleanup deletion" — not "I understand >1,000 nodes may be deleted."

If the eligibility filter is more aggressive against the live corpus than expected (e.g., many browser events under 1-second dwell or Claude sessions under 3 messages), this could silently delete the majority of the corpus.

The noise classification logic (`_node_is_noise`) is conservative: a node is noise only when ALL its source events fail eligibility. A node linked to one trivial command AND one substantial command is kept. The current risk is low for the 2,000-node corpus, but the safety gate design is incorrect regardless.

**Remediation:** Change the warning to a hard stop:

```python
# migrate-v5-to-v6.py:511
if len(noise_ids) > 0.5 * len(node_ids):
    raise RuntimeError(
        f"[noise-cleanup] ABORTED: would delete {len(noise_ids)}/{len(node_ids)} nodes "
        f"({100 * len(noise_ids) // len(node_ids)}% of corpus). "
        "This exceeds the 50% safety threshold. "
        "Inspect the noise list via --dry-run, then rerun with "
        "--yes-drop-noise --yes-drop-half-corpus to override."
    )
```

Add `--yes-drop-half-corpus` as a separate explicit flag.

---

## Additional Finding Not in the Adversarial Scenarios

### AX-01 — Sequential stdout/stderr reading in subprocess phases risks deadlock

**Severity: MED | Confidence: 75%**

`brain/scripts/migrate-v5-to-v6.py:568–583`:

```python
for line in proc.stderr:
    log.info("[%s] %s", phase_label, line)
for line in proc.stdout:
    log.info("[%s] stdout: %s", phase_label, line)
```

stdout and stderr are drained **sequentially**. If `dedup-entities.py` (phase 7) is run in `--dry-run` mode, it emits one `print()` line per duplicate merge group to stdout:

```python
# dedup-entities.py:72-75
print(f"MERGE type={etype!r} canonical={new_canonical!r}: keep id={keep_id}, delete ids={dupe_ids}")
```

If there are enough duplicate groups to fill the stdout pipe buffer (64 KB default), `dedup-entities.py` blocks waiting for the parent to drain stdout. The parent is blocked iterating `proc.stderr`. **Deadlock.**

For the 2,000-node corpus this is unlikely (would require ~640+ merge lines), but it is a latent bug.

**Remediation:** Use `proc.communicate()` or drain both pipes concurrently:

```python
stdout_data, stderr_data = proc.communicate()
for line in stderr_data.splitlines():
    if line.strip():
        log.info("[%s] %s", phase_label, line)
for line in stdout_data.splitlines():
    if line.strip():
        log.debug("[%s] stdout: %s", phase_label, line)
proc.wait()
```

---

## Summary Table

| ID | Scenario | Severity | Confidence | Status |
|---|---|---|---|---|
| S-01 | Crash between phases | MED | 95% | Improve error message |
| S-02 | LM Studio partial failure | LOW | 90% | Acceptable |
| S-03 | Daemon starts during migration | LOW | 85% | Acceptable |
| **S-04** | **Corrupt backup proceeds undetected** | **HIGH** | **95%** | **Must fix** |
| S-05 | Subprocess non-zero + partial state | LOW | 90% | Acceptable |
| S-06 | Ctrl-C mid-phase | LOW | 90% | Acceptable |
| S-07 | `--dry-run` correctness | NA | 95% | ✓ Correct |
| **S-08** | **FTS backfill missing — verify always fails** | **HIGH** | **100%** | **Must fix** |
| S-09 | Phase 2 idempotency | NA | 95% | ✓ Correct (with backfill guard) |
| S-10 | Savepoint round-trip | NA | 95% | ✓ Correct |
| S-11 | Backup filename collisions | LOW | 90% | Acceptable |
| S-12 | Schema.sql drift | NA | 95% | ✓ No drift |
| S-13 | Permission / existence checks | LOW | 85% | Acceptable |
| **S-14** | **50% deletion gate doesn't halt** | **HIGH** | **95%** | **Must fix** |
| AX-01 | Subprocess deadlock (stdout/stderr) | MED | 75% | Fix before real run |

---

## Overall Go/No-Go

| Mode | Verdict |
|---|---|
| `--dry-run` | **GO** — safe, no mutations, all gates correct |
| Real-DB cutover | **NO-GO** — fix H1 (S-08), H2 (S-04), H3 (S-14) first |

**Fix order:**
1. **S-08 (FTS backfill)** — functional blocker; every non-dry-run fails at phase 8
2. **S-04 (backup integrity)** — safety blocker; your only fallback may be silent trash
3. **S-14 (50% gate)** — policy blocker; irreversible mass-deletion is one bad eligibility misconfiguration away
4. **S-01 (error message)** + **AX-01 (subprocess deadlock)** — quality fixes before real run
