use hippo_core::storage::{open_db, watchlist};
use tempfile::TempDir;

#[test]
fn upsert_then_resolve() {
    let tmp = TempDir::new().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();

    watchlist::upsert(
        &conn,
        "abc123",
        "me/repo",
        /*created_at=*/ 1_700_000_000_000,
        /*expires_at=*/ 1_700_000_600_000,
    )
    .unwrap();

    let active = watchlist::list_active(&conn, 1_700_000_000_000).unwrap();
    assert_eq!(active.len(), 1);
    assert_eq!(active[0].sha, "abc123");

    let hit = watchlist::mark_terminal(&conn, "abc123", "me/repo", "failure").unwrap();
    assert!(hit, "mark_terminal should return true when row exists");
    let pending = watchlist::pending_notifications(&conn, 1_700_000_000_000).unwrap();
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].terminal_status.as_deref(), Some("failure"));
}

#[test]
fn mark_terminal_unknown_sha_returns_false() {
    let tmp = TempDir::new().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();
    let hit = watchlist::mark_terminal(&conn, "nope", "me/repo", "failure").unwrap();
    assert!(!hit);
}

#[test]
fn expired_entry_not_active() {
    let tmp = TempDir::new().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();

    watchlist::upsert(&conn, "abc", "me/repo", 0, 1_000).unwrap();
    let active = watchlist::list_active(&conn, 2_000).unwrap();
    assert!(active.is_empty());
}
