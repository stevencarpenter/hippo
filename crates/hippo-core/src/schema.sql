CREATE TABLE IF NOT EXISTS sessions
(
    id
    INTEGER
    PRIMARY
    KEY,
    start_time
    INTEGER
    NOT
    NULL,
    end_time
    INTEGER,
    terminal
    TEXT,
    shell
    TEXT
    NOT
    NULL,
    hostname
    TEXT
    NOT
    NULL,
    username
    TEXT
    NOT
    NULL,
    summary
    TEXT,
    created_at
    INTEGER
    NOT
    NULL
    DEFAULT (
    unixepoch
(
    'now',
    'subsec'
) * 1000)
    );

CREATE TABLE IF NOT EXISTS env_snapshots
(
    id
    INTEGER
    PRIMARY
    KEY,
    content_hash
    TEXT
    NOT
    NULL
    UNIQUE,
    env_json
    TEXT
    NOT
    NULL,
    created_at
    INTEGER
    NOT
    NULL
    DEFAULT (
    unixepoch
(
    'now',
    'subsec'
) * 1000)
    );

CREATE TABLE IF NOT EXISTS events
(
    id
    INTEGER
    PRIMARY
    KEY,
    session_id
    INTEGER
    NOT
    NULL
    REFERENCES
    sessions
(
    id
),
    timestamp INTEGER NOT NULL,
    command TEXT NOT NULL,
    stdout TEXT,
    stderr TEXT,
    stdout_truncated INTEGER DEFAULT 0,
    stderr_truncated INTEGER DEFAULT 0,
    exit_code INTEGER,
    duration_ms INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    hostname TEXT NOT NULL,
    shell TEXT NOT NULL,
    git_repo TEXT,
    git_branch TEXT,
    git_commit TEXT,
    git_dirty INTEGER,
    env_snapshot_id INTEGER REFERENCES env_snapshots
(
    id
),
    enriched INTEGER NOT NULL DEFAULT 0,
    redaction_count INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT
(
    unixepoch
(
    'now',
    'subsec'
) * 1000)
    );

CREATE TABLE IF NOT EXISTS entities
(
    id
    INTEGER
    PRIMARY
    KEY,
    type
    TEXT
    NOT
    NULL
    CHECK (
    type
    IN
(
    'project',
    'file',
    'tool',
    'service',
    'repo',
    'host',
    'person',
    'concept'
)),
    name TEXT NOT NULL,
    canonical TEXT,
    metadata TEXT,
    first_seen INTEGER NOT NULL DEFAULT
(
    unixepoch
(
    'now',
    'subsec'
) * 1000),
    last_seen INTEGER NOT NULL DEFAULT
(
    unixepoch
(
    'now',
    'subsec'
) * 1000),
    created_at INTEGER NOT NULL DEFAULT
(
    unixepoch
(
    'now',
    'subsec'
) * 1000),
    UNIQUE
(
    type,
    canonical
)
    );

CREATE TABLE IF NOT EXISTS relationships
(
    id
    INTEGER
    PRIMARY
    KEY,
    from_entity_id
    INTEGER
    NOT
    NULL
    REFERENCES
    entities
(
    id
),
    to_entity_id INTEGER NOT NULL REFERENCES entities
(
    id
),
    relationship TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    first_seen INTEGER NOT NULL DEFAULT
(
    unixepoch
(
    'now',
    'subsec'
) * 1000),
    last_seen INTEGER NOT NULL DEFAULT
(
    unixepoch
(
    'now',
    'subsec'
) * 1000),
    UNIQUE
(
    from_entity_id,
    to_entity_id,
    relationship
)
    );

CREATE TABLE IF NOT EXISTS event_entities
(
    id
    INTEGER
    PRIMARY
    KEY,
    event_id
    INTEGER
    NOT
    NULL
    REFERENCES
    events
(
    id
),
    entity_id INTEGER NOT NULL REFERENCES entities
(
    id
),
    role TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT
(
    unixepoch
(
    'now',
    'subsec'
) * 1000),
    UNIQUE
(
    event_id,
    entity_id,
    role
)
    );

CREATE TABLE IF NOT EXISTS knowledge_nodes
(
    id
    INTEGER
    PRIMARY
    KEY,
    uuid
    TEXT
    NOT
    NULL
    UNIQUE,
    content
    TEXT
    NOT
    NULL,
    embed_text
    TEXT
    NOT
    NULL,
    node_type
    TEXT
    NOT
    NULL
    DEFAULT
    'observation',
    outcome
    TEXT,
    tags
    TEXT,
    enrichment_model
    TEXT,
    enrichment_version
    INTEGER
    NOT
    NULL
    DEFAULT
    1,
    created_at
    INTEGER
    NOT
    NULL
    DEFAULT (
    unixepoch
(
    'now',
    'subsec'
) * 1000),
    updated_at INTEGER NOT NULL DEFAULT
(
    unixepoch
(
    'now',
    'subsec'
) * 1000)
    );

CREATE TABLE IF NOT EXISTS knowledge_node_entities
(
    knowledge_node_id
    INTEGER
    NOT
    NULL
    REFERENCES
    knowledge_nodes
(
    id
),
    entity_id INTEGER NOT NULL REFERENCES entities
(
    id
),
    PRIMARY KEY
(
    knowledge_node_id,
    entity_id
)
    );

CREATE TABLE IF NOT EXISTS knowledge_node_events
(
    knowledge_node_id
    INTEGER
    NOT
    NULL
    REFERENCES
    knowledge_nodes
(
    id
),
    event_id INTEGER NOT NULL REFERENCES events
(
    id
),
    PRIMARY KEY
(
    knowledge_node_id,
    event_id
)
    );

CREATE TABLE IF NOT EXISTS enrichment_queue
(
    id
    INTEGER
    PRIMARY
    KEY,
    event_id
    INTEGER
    NOT
    NULL
    UNIQUE
    REFERENCES
    events
(
    id
),
    status TEXT NOT NULL DEFAULT 'pending'
    CHECK
(
    status
    IN
(
    'pending',
    'processing',
    'done',
    'failed',
    'skipped'
)),
    priority INTEGER NOT NULL DEFAULT 5,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    error_message TEXT,
    locked_at INTEGER,
    locked_by TEXT,
    created_at INTEGER NOT NULL DEFAULT
(
    unixepoch
(
    'now',
    'subsec'
) * 1000),
    updated_at INTEGER NOT NULL DEFAULT
(
    unixepoch
(
    'now',
    'subsec'
) * 1000)
    );

CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_git_repo ON events (git_repo) WHERE git_repo IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_enriched ON events (enriched) WHERE enriched = 0;
CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities (type, name);
CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities (canonical) WHERE canonical IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_relationships_from ON relationships (from_entity_id, relationship);
CREATE INDEX IF NOT EXISTS idx_relationships_to ON relationships (to_entity_id, relationship);
CREATE INDEX IF NOT EXISTS idx_event_entities_entity ON event_entities (entity_id, event_id);
CREATE INDEX IF NOT EXISTS idx_kn_entities_entity ON knowledge_node_entities (entity_id);
CREATE INDEX IF NOT EXISTS idx_queue_pending ON enrichment_queue (status, priority) WHERE status = 'pending';
