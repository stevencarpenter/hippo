CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS project_aliases (
    canonical_id INTEGER NOT NULL REFERENCES projects (id),
    alias TEXT NOT NULL PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS idx_project_aliases_canonical ON project_aliases (canonical_id);
