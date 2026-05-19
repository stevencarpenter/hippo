"""Tests for the brain-side opencode session enrichment path.

The daemon writes opencode sessions to `agentic_sessions` and enqueues
`agentic_enrichment_queue` rows in one transaction; these tests start from
that state and exercise the brain's claim → eligibility → write → close-out
flow plus the failure-retry path.

Acts as the regression bed for F-26..F-28 in `docs/capture/test-matrix.md`.
"""

from hippo_brain.enrichment import is_enrichment_eligible
from hippo_brain.models import EnrichmentResult
from hippo_brain.opencode_sessions import (
    OPENCODE_ENRICHMENT_PROMPT,
    build_opencode_enrichment_prompt,
    claim_pending_opencode_segments,
    mark_opencode_queue_failed,
    write_opencode_knowledge_node,
)


def _insert_session(
    conn,
    *,
    session_id: str,
    cwd: str = "/proj",
    title: str = "Session title",
    summary_text: str = "Opencode session (project: /proj, slug: x)",
    message_count: int = 5,
    snapshot_diffs_json: str = '{"additions": 12, "deletions": 4, "files": 3}',
    commit_messages_json: str = "[]",
    probe_tag: str | None = None,
    enqueue: bool = True,
) -> int:
    """Insert one row into agentic_sessions (+ queue) mimicking the daemon."""
    cursor = conn.execute(
        """
        INSERT INTO agentic_sessions
            (session_id, harness, model, agent, project_dir, cwd, slug, title,
             summary_text, source_file, snapshot_diffs_json, commit_messages_json,
             message_count, token_count, start_time, end_time, probe_tag)
        VALUES (?, 'opencode', '', '', ?, ?, '', ?, ?, '', ?, ?, ?, 0, ?, ?, ?)
        """,
        (
            session_id,
            cwd,
            cwd,
            title,
            summary_text,
            snapshot_diffs_json,
            commit_messages_json,
            message_count,
            1_700_000_000_000,
            1_700_000_001_000,
            probe_tag,
        ),
    )
    seg_id = cursor.lastrowid
    if enqueue:
        conn.execute(
            """
            INSERT INTO agentic_enrichment_queue
              (session_id, status, enqueued_at, updated_at)
            VALUES (?, 'pending', 0, 0)
            """,
            (seg_id,),
        )
    conn.commit()
    return seg_id


class TestEligibility:
    def test_skip_session_with_no_messages_no_diffs_no_commits(self):
        seg = {"message_count": 0, "snapshot_diffs": None, "commit_messages": []}
        ok, reason = is_enrichment_eligible(seg, "opencode")
        assert not ok
        assert "message_count=0" in reason

    def test_eligible_when_messages_high(self):
        seg = {"message_count": 10, "snapshot_diffs": None, "commit_messages": []}
        ok, _ = is_enrichment_eligible(seg, "opencode")
        assert ok

    def test_eligible_when_has_diffs(self):
        seg = {
            "message_count": 0,
            "snapshot_diffs": {"additions": 5, "deletions": 0, "files": 1},
            "commit_messages": [],
        }
        ok, _ = is_enrichment_eligible(seg, "opencode")
        assert ok

    def test_eligible_when_has_commits(self):
        seg = {
            "message_count": 0,
            "snapshot_diffs": None,
            "commit_messages": ["fix(x): something"],
        }
        ok, _ = is_enrichment_eligible(seg, "opencode")
        assert ok

    def test_diff_with_all_zeros_is_not_eligibility_signal(self):
        seg = {
            "message_count": 0,
            "snapshot_diffs": {"additions": 0, "deletions": 0, "files": 0},
            "commit_messages": [],
        }
        ok, _ = is_enrichment_eligible(seg, "opencode")
        assert not ok


class TestClaimPath:
    def test_claim_pending_groups_by_cwd_and_excludes_probes(self, tmp_db):
        conn, _ = tmp_db
        _insert_session(conn, session_id="s-a-1", cwd="/proj-a")
        _insert_session(conn, session_id="s-a-2", cwd="/proj-a")
        _insert_session(conn, session_id="s-b-1", cwd="/proj-b")
        # Probe row must NOT appear in the claim output (AP-6).
        _insert_session(conn, session_id="s-probe", cwd="/proj-a", probe_tag="probe-canary-1")

        batches = claim_pending_opencode_segments(conn, "test-worker")
        # 1:1 enrichment — 3 real segments, 3 batches.
        flat_ids = [seg["session_id"] for batch in batches for seg in batch]
        assert sorted(flat_ids) == ["s-a-1", "s-a-2", "s-b-1"]

        # All claimed rows must be marked processing.
        statuses = {
            row[0]: row[1]
            for row in conn.execute(
                """
                SELECT s.session_id, q.status
                FROM agentic_enrichment_queue q
                JOIN agentic_sessions s ON q.session_id = s.id
                WHERE s.session_id IN ('s-a-1', 's-a-2', 's-b-1')
                """
            )
        }
        assert all(v == "processing" for v in statuses.values()), statuses

    def test_claim_filters_ineligible_segments_and_marks_skipped(self, tmp_db):
        conn, _ = tmp_db
        _insert_session(
            conn,
            session_id="s-empty",
            message_count=0,
            snapshot_diffs_json='{"additions": 0, "deletions": 0, "files": 0}',
            commit_messages_json="[]",
        )

        batches = claim_pending_opencode_segments(conn, "test-worker")
        # Ineligible → dropped from batches.
        assert all(seg["session_id"] != "s-empty" for batch in batches for seg in batch)

        # Queue row must be marked 'skipped' with the eligibility reason.
        row = conn.execute(
            """
            SELECT q.status, q.error_message
            FROM agentic_enrichment_queue q
            JOIN agentic_sessions s ON q.session_id = s.id
            WHERE s.session_id = 's-empty'
            """
        ).fetchone()
        assert row[0] == "skipped"
        assert "message_count" in row[1]


class TestPromptFormatting:
    def test_prompt_includes_daemon_transcript_summary_text(self):
        prompt = build_opencode_enrichment_prompt(
            [
                {
                    "cwd": "/proj",
                    "slug": "capture",
                    "summary_text": (
                        "User requests:\n"
                        '  1. "Capture opencode message parts"\n'
                        "Work performed:\n"
                        "  - bash: rg opencode brain/src/hippo_brain"
                    ),
                    "snapshot_diffs": None,
                    "commit_messages": [],
                    "message_count": 2,
                    "token_count": 42,
                }
            ]
        )

        assert "Capture opencode message parts" in prompt
        assert "bash: rg opencode brain/src/hippo_brain" in prompt

    def test_system_prompt_requires_structured_entities_object(self):
        assert "entities: An object with lists of extracted entities" in OPENCODE_ENRICHMENT_PROMPT
        assert "env_vars" in OPENCODE_ENRICHMENT_PROMPT


class TestWriteKnowledgeNode:
    """Regression tests for F-28 — the original malformed VALUES clause."""

    def _make_result(self) -> EnrichmentResult:
        return EnrichmentResult(
            summary="Refactored the indexer",
            intent="refactoring",
            outcome="success",
            entities={
                "projects": ["hippo"],
                "tools": ["cargo"],
                "files": ["src/indexer.rs"],
                "services": [],
                "errors": [],
            },
            tags=["rust", "refactor"],
            embed_text="hippo cargo src/indexer.rs refactor",
            key_decisions=["split the indexer into two structs"],
            problems_encountered=[],
        )

    def test_write_links_single_segment(self, tmp_db):
        conn, _ = tmp_db
        seg_id = _insert_session(conn, session_id="single")
        node_id = write_opencode_knowledge_node(conn, self._make_result(), [seg_id], "test-model")

        links = conn.execute(
            "SELECT agentic_session_id FROM knowledge_node_agentic_sessions "
            "WHERE knowledge_node_id = ?",
            (node_id,),
        ).fetchall()
        assert links == [(seg_id,)], (
            "single-segment write must produce exactly one link row; "
            "the original bug produced zero or errored on the malformed VALUES clause"
        )

    def test_write_links_each_segment_in_a_multi_segment_batch(self, tmp_db):
        """The bug that motivated this test: junction-table INSERT used
        `VALUES (?, ?, …, ?)` with `len(segment_ids)` placeholders for a
        2-column table, plus an arity-mismatched params tuple. With 3 segment
        ids the SQL would have 3 placeholders but receive 4 params (node_id +
        3 segments), guaranteeing `ProgrammingError`."""
        conn, _ = tmp_db
        seg_ids = [
            _insert_session(conn, session_id=f"multi-{i}", cwd=f"/proj-{i}") for i in range(3)
        ]
        node_id = write_opencode_knowledge_node(conn, self._make_result(), seg_ids, "test-model")

        link_pairs = conn.execute(
            "SELECT knowledge_node_id, agentic_session_id FROM knowledge_node_agentic_sessions "
            "WHERE knowledge_node_id = ? ORDER BY agentic_session_id",
            (node_id,),
        ).fetchall()
        assert link_pairs == sorted([(node_id, sid) for sid in seg_ids])

    def test_write_flips_enriched_and_closes_queue(self, tmp_db):
        conn, _ = tmp_db
        seg_id = _insert_session(conn, session_id="closeout")
        write_opencode_knowledge_node(conn, self._make_result(), [seg_id], "test-model")

        enriched = conn.execute(
            "SELECT enriched FROM agentic_sessions WHERE id = ?", (seg_id,)
        ).fetchone()[0]
        assert enriched == 1, (
            "enriched=1 must be set as part of the knowledge-node write, "
            "not before — otherwise an LLM failure orphans the row"
        )

        queue_status = conn.execute(
            "SELECT status FROM agentic_enrichment_queue WHERE session_id = ?",
            (seg_id,),
        ).fetchone()[0]
        assert queue_status == "done"


class TestMarkQueueFailed:
    def test_first_failure_keeps_queue_pending(self, tmp_db):
        conn, _ = tmp_db
        seg_id = _insert_session(conn, session_id="retry-1")
        mark_opencode_queue_failed(conn, [seg_id], "LLM timeout")
        status, retry_count, error = conn.execute(
            "SELECT status, retry_count, error_message FROM agentic_enrichment_queue "
            "WHERE session_id = ?",
            (seg_id,),
        ).fetchone()
        assert status == "pending"
        assert retry_count == 1
        assert error == "LLM timeout"

    def test_exhausted_retries_flip_to_failed(self, tmp_db):
        conn, _ = tmp_db
        seg_id = _insert_session(conn, session_id="retry-exhaust")
        # max_retries defaults to 5; exhaust them.
        for _ in range(5):
            mark_opencode_queue_failed(conn, [seg_id], "still failing")
        status, retry_count = conn.execute(
            "SELECT status, retry_count FROM agentic_enrichment_queue WHERE session_id = ?",
            (seg_id,),
        ).fetchone()
        assert status == "failed"
        assert retry_count == 5


class TestBuildPrompt:
    def test_renders_header_and_diff_block(self):
        seg = {
            "cwd": "/projects/hippo",
            "slug": "fix-the-bug",
            "title": "Fix the bug",
            "start_time": 1_700_000_000_000,
            "end_time": 1_700_000_300_000,
            "agent": "plan",
            "model": "claude-3.5",
            "snapshot_diffs": {"additions": 12, "deletions": 4, "files": 3},
            "commit_messages": [],
        }
        prompt = build_opencode_enrichment_prompt([seg])
        assert "/projects/hippo" in prompt
        assert "fix-the-bug" in prompt
        assert "Agent: plan" in prompt
        assert "Model: claude-3.5" in prompt
        assert "+12/-4 lines, 3 files" in prompt

    def test_strips_worktree_from_cwd(self):
        """Mirror Claude-side test_strips_worktree_from_cwd — the prompt
        must not surface the ephemeral worktree path to the LLM."""
        seg = {
            "cwd": "/projects/hippo/.claude/worktrees/agent-ac83d4d3/crates/hippo-core",
            "slug": "x",
            "title": "",
            "start_time": 0,
            "end_time": 0,
            "agent": "",
            "model": "",
            "snapshot_diffs": None,
            "commit_messages": [],
        }
        prompt = build_opencode_enrichment_prompt([seg])
        assert "/projects/hippo/crates/hippo-core" in prompt
        assert ".claude/worktrees" not in prompt
