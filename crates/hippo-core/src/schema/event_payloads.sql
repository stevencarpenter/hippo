-- HIPO-13: receipts name a removed field, not erasure of every copy.
CREATE TABLE IF NOT EXISTS deletion_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL,
    ts INTEGER NOT NULL,
    reason TEXT NOT NULL,
    what TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS deletion_receipts_no_update
BEFORE UPDATE ON deletion_receipts BEGIN
    SELECT RAISE(ABORT, 'deletion_receipts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS deletion_receipts_no_delete
BEFORE DELETE ON deletion_receipts BEGIN
    SELECT RAISE(ABORT, 'deletion_receipts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS deletion_receipts_no_replace
BEFORE INSERT ON deletion_receipts
WHEN EXISTS (
    SELECT 1 FROM deletion_receipts
    -- NEW is SQLite's trigger row, not a FROM table.
    WHERE id = NEW.id -- noqa: RF01,RF03
) BEGIN
    SELECT RAISE(ABORT, 'deletion_receipts are append-only');
END;

-- Covers every writer, including direct SQL inserts. Old rows stay NULL.
CREATE TRIGGER IF NOT EXISTS events_payload_expiry_on_insert
AFTER INSERT ON events WHEN NEW.payload_expiry IS NULL BEGIN
    UPDATE events SET payload_expiry = NEW.created_at + 7776000000
    WHERE id = NEW.id;
END;
CREATE INDEX IF NOT EXISTS idx_events_env_snapshot
ON events (env_snapshot_id)
WHERE env_snapshot_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_payload_expiry
ON events (COALESCE(payload_expiry, created_at + 7776000000), id)
WHERE stdout IS NOT NULL OR stderr IS NOT NULL OR env_snapshot_id IS NOT NULL;
