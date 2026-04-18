# Hippo Native macOS GUI

**Date:** 2026-04-18  
**Status:** Draft  
**Scope:** Native SwiftUI macOS application for interacting with Hippo knowledge base

## Context

Hippo currently exposes a CLI for querying the knowledge base (`hippo query`, `hippo ask`) and daemon status (`hippo status`). Users want a native macOS interface for:

- Querying and asking questions against the knowledge base
- Browsing knowledge nodes and events
- Viewing session information
- Monitoring basic daemon/brain health

This spec covers a native SwiftUI application that targets only local usage on macOS. No network distribution — the app queries local brain server and SQLite database only.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Hippo GUI (SwiftUI)                        │
├─────────────────────────────────────────────────────────────────┤
│  Views                                                         │
│  ├── QueryAskView     (text input → brain /ask, results)       │
│  ├── KnowledgeView   (list nodes, filter, view detail)         │
│  ├── EventsView      (sessions → events → command/output)       │
│  └── StatusView     (health indicators)                         │
├─────────────────────────────────────────────────────────────────┤
│  ViewModels / Services                                          │
│  ├── BrainClient     (HTTP → brain server)                      │
│  └── ConfigClient   (read config from ~/.config/hippo/config.toml)│
├─────────────────────────────────────────────────────────────────┤
│  Networking                                                   │
│  └── URLSession    (native Swift)                              │
└─────────────────────────────────────────────────────────────────┘
           │                                    │
           ▼                                    ▼
┌─────────────────────┐           ┌─────────────────────────┐
│  Brain Server       │           │  SQLite DB             │
│  (FastAPI)          │           │  (events, sessions,    │
│  /ask, /query,     │           │   knowledge_nodes)     │
│  /knowledge, etc.  │           │                       │
└─────────────────────┘           └─────────────────────────┘
```

### Data Flow

1. **Query/Ask →** SwiftUI View → BrainClient → HTTP POST `/ask` → brain server → LanceDB → response
2. **Knowledge Browse →** SwiftUI View → BrainClient → HTTP GET `/knowledge` → brain server → SQLite
3. **Events Browse →** SwiftUI View → BrainClient → HTTP GET `/events` → brain server → SQLite
4. **Status →** SwiftUI View → BrainClient → HTTP GET `/health` → brain server → health response

### Design for Future Graph Feature

Knowledge nodes and entities have relationships (see `schema.sql` relationships table). The service layer should be designed to support future graph traversal:

- Keep data fetching separate from display
- Design views to be data-driven, not hardcoded
- Service layer exposes queryable interfaces for future expansion

## New Brain Server Endpoints

The brain server currently exposes only:
- `GET /health`
- `POST /query`
- `POST /ask`

GUI requires additional endpoints:

| Endpoint | Method | Response |
|----------|--------|----------|
| `/knowledge` | GET | List knowledge nodes (paginated) |
| `/knowledge/{id}` | GET | Single knowledge node |
| `/events` | GET | List events (paginated) |
| `/sessions` | GET | List sessions |

### Endpoint Specs

#### GET /knowledge

Query params:
- `limit` (default 20)
- `offset` (default 0)
- `node_type` (optional filter)
- `since_ms` (optional filter)

Response:
```json
{
  "nodes": [
    {
      "id": 1,
      "uuid": "...",
      "content": "...",
      "node_type": "observation",
      "outcome": "success",
      "tags": ["..."],
      "created_at": 1234567890
    }
  ],
  "total": 100
}
```

#### GET /knowledge/{id}

Response:
```json
{
  "id": 1,
  "uuid": "...",
  "content": "...",
  "embed_text": "...",
  "node_type": "observation",
  "outcome": "success",
  "tags": ["..."],
  "created_at": 1234567890,
  "related_entities": [{"id": 1, "name": "...", "type": "tool"}],
  "related_events": [{"id": 1, "command": "..."}]
}
```

#### GET /events

Query params:
- `limit` (default 20)
- `offset` (default 0)
- `session_id` (optional filter)
- `since_ms` (optional filter)
- `project` (optional filter)

Response:
```json
{
  "events": [
    {
      "id": 1,
      "timestamp": 1234567890,
      "command": "cargo build",
      "exit_code": 0,
      "duration_ms": 1234,
      "cwd": "/home/user/project",
      "git_branch": "main"
    }
  ],
  "total": 100
}
```

#### GET /sessions

Query params:
- `limit` (default 20)
- `offset` (default 0)
- `since_ms` (optional filter)

Response:
```json
{
  "sessions": [
    {
      "id": 1,
      "start_time": 1234567890,
      "hostname": "laptop",
      "shell": "zsh",
      "event_count": 42
    }
  ],
  "total": 10
}
```

## SwiftUI Application Structure

```
gui/
├── Sources/
│   └── HippoGUI/
│       ├── App/
│       │   └── HippoGUIApp.swift
│       ├── Views/
│       │   ├── ContentView.swift
│       │   ├── QueryAskView.swift
│       │   ├── KnowledgeView.swift
│       │   ├── EventBrowserView.swift
│       │   └── StatusView.swift
│       ├── ViewModels/
│       │   ├── QueryAskViewModel.swift
│       │   ├── KnowledgeViewModel.swift
│       │   └── EventsViewModel.swift
│       ├── Services/
│       │   ├── BrainClient.swift
│       │   └── ConfigClient.swift
│       └── Models/
│           ├── KnowledgeNode.swift
│           ├── Event.swift
│           └── Session.swift
├── Resources/
│   └── Assets.xcassets/
├── Package.swift
└── HippoGUI.xcodeproj/
```

### Dependencies

None for MVP — use native frameworks only:
- SwiftUI (views)
- URLSession (networking)
- Foundation (JSON decoding)

### Configuration

Brain server port read from `~/.config/hippo/config.toml`:

```toml
[brain]
port = 8765
```

Swift loads config via:
1. Check `~/.config/hippo/config.toml`
2. Fall back to default port 8765

## Views Specification

### 1. QueryAskView

**Purpose:** Ask questions and search the knowledge base

**UI:**
- Text field for input
- Segmented control: "Ask" / "Search"
- Results list with source attribution
- Loading state

**Flow:**
1. User enters question
2. Tap "Ask" → POST `/ask`
3. Display results with sources
4. Tap result → navigate to detail

### 2. KnowledgeView

**Purpose:** Browse saved knowledge nodes

**UI:**
- List view with node_type filter
- Search field
- Detail panel showing full content
- Tags display

**Flow:**
1. Load nodes from `/knowledge`
2. Display in list
3. Tap node → show detail
4. Filter by type / tags

### 3. EventBrowserView

**Purpose:** Browse shell events and sessions

**UI:**
- Sidebar: sessions list
- Main: events in session
- Detail: command + output

**Flow:**
1. Load sessions from `/sessions`
2. Select session → load events
3. Tap event → show detail

### 4. StatusView

**Purpose:** Basic health indicators (minimal)

**UI:**
- Daemon status (socket responsive)
- Brain server status (HTTP reachable)
- Queue depth summary

**Note:** Detailed metrics — use OTel/Grafana (already running)

## macOS Integration

For MVP:
- Standard window
- Toolbar with navigation
- No menu bar app (future consideration)

## Out of Scope

- Graph visualization (future)
- Write operations (read-only)
- Menu bar app
- System notifications
- Keyboard shortcuts

## Testing

- SwiftUI preview for all views
- Unit tests for ViewModels
- Integration tests: verify HTTP response parsing

## Future Considerations

- Menu bar app for quick access
- Graph visualization for knowledge relationships
- Native notifications for enrichment completion
- Keyboard shortcuts for navigation