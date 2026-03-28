# Schema Migration Strategy

## Current Version

The schema version is tracked via SQLite's `PRAGMA user_version`. The current version is **1**.

Both `hippo-daemon` (Rust) and `hippo-brain` (Python) check the version on every connection. If the version does not match, the process refuses to open the database and exits with an error message.

## How It Works

- `crates/hippo-core/src/schema.sql` ends with `PRAGMA user_version = N;`
- `open_db()` in `storage.rs` runs the schema, then reads `PRAGMA user_version` and compares it to `EXPECTED_VERSION`
- `_get_conn()` in `server.py` reads `PRAGMA user_version` after setting connection pragmas and compares it to `EXPECTED_VERSION`

Both sides bail with a clear error if the versions do not match.

## How to Bump the Version

1. Make your schema change in `crates/hippo-core/src/schema.sql`
2. Update the `PRAGMA user_version` at the end of `schema.sql` to `N+1`
3. Update `EXPECTED_VERSION` in `storage.rs` (`open_db`) to `N+1`
4. Update `EXPECTED_VERSION` in `server.py` (`_get_conn`) to `N+1`
5. Run all tests in both Rust and Python to confirm nothing breaks

## Conventions

### Adding a New Table

Use `CREATE TABLE IF NOT EXISTS` so the statement is idempotent on fresh databases. Bump the version.

```sql
-- In schema.sql, before the PRAGMA user_version line:
CREATE TABLE IF NOT EXISTS new_table (...);

-- Update the version:
PRAGMA user_version = 2;
```

### Adding or Renaming a Column

Use `ALTER TABLE` for existing databases. The `CREATE TABLE IF NOT EXISTS` block handles fresh databases. Bump the version.

For a migration from version 1 to version 2:

```sql
-- Add a migration step in open_db (Rust) after execute_batch(SCHEMA):
-- if version == 1 {
--     conn.execute_batch("ALTER TABLE events ADD COLUMN new_col TEXT;")?;
--     conn.execute_batch("PRAGMA user_version = 2;")?;
-- }
```

When the number of migrations grows, move them into a dedicated migration runner.

### Dropping a Column

SQLite does not support `DROP COLUMN` before version 3.35.0. For older SQLite versions, use the recreate-table pattern:

1. Create a new table without the column
2. Copy data from the old table
3. Drop the old table
4. Rename the new table

Always bump the version.

## Testing Migrations

### Rust

```bash
cargo test -p hippo-core test_open_db_version_matches_schema
cargo test -p hippo-core test_open_db_rejects_wrong_version
```

### Python

```bash
uv run --project brain pytest brain/tests/test_server.py::test_brain_server_rejects_wrong_schema_version -v
```

### Manual Verification

To inspect a database's current version:

```bash
sqlite3 ~/.local/share/hippo/hippo.db "PRAGMA user_version"
```

To force-set a version (use with caution):

```bash
sqlite3 ~/.local/share/hippo/hippo.db "PRAGMA user_version = 1"
```
