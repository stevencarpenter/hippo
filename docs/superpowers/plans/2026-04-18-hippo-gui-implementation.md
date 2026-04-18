# Hippo Native macOS GUI Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a native SwiftUI macOS application for interacting with the Hippo knowledge base — Query/Ask, Knowledge Browser, Event Browser, and Status views.

**Architecture:** Two-phase implementation:
1. **Phase 1:** Add new HTTP endpoints to brain server (Python) for listing knowledge nodes, events, sessions
2. **Phase 2:** Build SwiftUI application with native networking to query brain server

**Tech Stack:**
- SwiftUI (native macOS views)
- URLSession (networking)
- Python/FastAPI (brain server endpoints)
- SQLite (data source)

---

## Chunk 1: Brain Server Endpoints

Add HTTP endpoints to brain server for GUI data needs.

### Task 1.1: Add GET /knowledge endpoint

**Files:**
- Modify: `brain/src/hippo_brain/server.py`
- Test: `brain/tests/test_server.py`

- [ ] **Step 1: Write test for /knowledge endpoint**

```python
async def test_list_knowledge_nodes(client):
    # Create test node in DB
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 5")
    conn.execute("""
        CREATE TABLE knowledge_nodes (
            id INTEGER PRIMARY KEY,
            uuid TEXT UNIQUE,
            content TEXT,
            embed_text TEXT,
            node_type TEXT DEFAULT 'observation',
            outcome TEXT,
            tags TEXT,
            created_at INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type, created_at) VALUES (?, ?, ?, ?, ?)",
        ("test-uuid", "test content", "test embed", "observation", 1234567890)
    )
    conn.commit()

    # Mock server with knowledge routes
    server = BrainServer(db_path=":memory:")
    response = await client.get("/knowledge")
    assert response.status_code == 200
    data = response.json()
    assert "nodes" in data
    assert len(data["nodes"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Expected: 404 or routing error

- [ ] **Step 3: Implement GET /knowledge endpoint**

Add to `server.py`:
```python
async def list_knowledge(self, request: Request) -> JSONResponse:
    """List knowledge nodes with pagination."""
    limit = int(request.query_params.get("limit", 20))
    offset = int(request.query_params.get("offset", 0))
    node_type = request.query_params.get("node_type")
    since_ms = request.query_params.get("since_ms")

    conn = self._get_conn()
    sql = "SELECT id, uuid, content, node_type, outcome, tags, created_at FROM knowledge_nodes WHERE 1=1"
    params = []
    if node_type:
        sql += " AND node_type = ?"
        params.append(node_type)
    if since_ms:
        sql += " AND created_at >= ?"
        params.append(int(since_ms))
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    # Get total count
    count_sql = "SELECT COUNT(*) FROM knowledge_nodes"
    if node_type:
        count_sql += " WHERE node_type = ?"
    total = conn.execute(count_sql, [node_type] if node_type else []).fetchone()[0]

    nodes = conn.execute(sql, params).fetchall()
    conn.close()

    return JSONResponse({
        "nodes": [
            {
                "id": n[0],
                "uuid": n[1],
                "content": n[2],
                "node_type": n[3],
                "outcome": n[4],
                "tags": n[5],
                "created_at": n[6]
            }
            for n in nodes
        ],
        "total": total
    })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest brain/tests/test_server.py::test_list_knowledge_nodes -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/server.py brain/tests/test_server.py
git commit -m "feat(brain): add GET /knowledge endpoint for GUI"
```

### Task 1.2: Add GET /knowledge/{id} endpoint

**Files:**
- Modify: `brain/src/hippo_brain/server.py`
- Test: `brain/tests/test_server.py`

- [ ] **Step 1: Write test for /knowledge/{id}**

```python
async def test_get_knowledge_node_by_id(client):
    response = await client.get("/knowledge/1")
    assert response.status_code == 200
    data = response.json()
    assert "id" in data
    assert "content" in data
```

- [ ] **Step 2: Run test to verify it fails**

Expected: 404

- [ ] **Step 3: Implement GET /knowledge/{id}**

```python
async def get_knowledge(self, request: Request, id: int) -> JSONResponse:
    """Get single knowledge node by ID."""
    conn = self._get_conn()
    node = conn.execute(
        "SELECT id, uuid, content, embed_text, node_type, outcome, tags, created_at FROM knowledge_nodes WHERE id = ?",
        [id]
    ).fetchone()
    conn.close()

    if not node:
        return JSONResponse({"error": "not found"}, status_code=404)

    return JSONResponse({
        "id": node[0],
        "uuid": node[1],
        "content": node[2],
        "embed_text": node[3],
        "node_type": node[4],
        "outcome": node[5],
        "tags": node[6],
        "created_at": node[7]
    })
```

Add route:
```python
Route("/knowledge/{id:int}", self.get_knowledge, methods=["GET"]),
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

### Task 1.3: Add GET /events endpoint

**Files:**
- Modify: `brain/src/hippo_brain/server.py`
- Test: `brain/tests/test_server.py`

- [ ] **Step 1: Write test for /events**

```python
async def test_list_events(client):
    response = await client.get("/events?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "events" in data
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement GET /events**

```python
async def list_events(self, request: Request) -> JSONResponse:
    """List events with pagination."""
    limit = int(request.query_params.get("limit", 20))
    offset = int(request.query_params.get("offset", 0))
    session_id = request.query_params.get("session_id")
    since_ms = request.query_params.get("since_ms")
    project = request.query_params.get("project")

    conn = self._get_conn()
    sql = "SELECT id, session_id, timestamp, command, exit_code, duration_ms, cwd, git_branch FROM events WHERE 1=1"
    params = []
    if session_id:
        sql += " AND session_id = ?"
        params.append(int(session_id))
    if since_ms:
        sql += " AND timestamp >= ?"
        params.append(int(since_ms))
    if project:
        sql += " AND cwd LIKE ?"
        params.append(f"%{project}%")
    sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    events = conn.execute(sql, params).fetchall()
    conn.close()

    return JSONResponse({
        "events": [
            {
                "id": e[0],
                "session_id": e[1],
                "timestamp": e[2],
                "command": e[3],
                "exit_code": e[4],
                "duration_ms": e[5],
                "cwd": e[6],
                "git_branch": e[7]
            }
            for e in events
        ],
        "total": total
    })
```

Add route:
```python
Route("/events", self.list_events, methods=["GET"]),
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

### Task 1.4: Add GET /sessions endpoint

**Files:**
- Modify: `brain/src/hippo_brain/server.py`
- Test: `brain/tests/test_server.py`

- [ ] **Step 1: Write test for /sessions**

```python
async def test_list_sessions(client):
    response = await client.get("/sessions?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "sessions" in data
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement GET /sessions**

```python
async def list_sessions(self, request: Request) -> JSONResponse:
    """List sessions with pagination."""
    limit = int(request.query_params.get("limit", 20))
    offset = int(request.query_params.get("offset", 0))
    since_ms = request.query_params.get("since_ms")

    conn = self._get_conn()
    sql = "SELECT s.id, s.start_time, s.hostname, s.shell FROM sessions s WHERE 1=1"
    params = []
    if since_ms:
        sql += " AND s.start_time >= ?"
        params.append(int(since_ms))
    sql += " ORDER BY s.start_time DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    sessions = conn.execute(sql, params).fetchall()

    # Get event counts per session
    sql_counts = "SELECT session_id, COUNT(*) FROM events WHERE session_id IN (" + ",".join(["?"] * len(sessions)) + ") GROUP BY session_id"
    counts = {row[0]: row[1] for row in conn.execute(sql_counts, [s[0] for s in sessions]).fetchall()}
    conn.close()

    return JSONResponse({
        "sessions": [
            {
                "id": s[0],
                "start_time": s[1],
                "hostname": s[2],
                "shell": s[3],
                "event_count": counts.get(s[0], 0)
            }
            for s in sessions
        ],
        "total": total
    })
```

Add route:
```python
Route("/sessions", self.list_sessions, methods=["GET"]),
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

### Task 1.5: Update Routes Array

**Files:**
- Modify: `brain/src/hippo_brain/server.py:829-831`

- [ ] **Step 1: Update route registration**

Replace the routes section:
```python
app = Starlette(
    routes=[
        Route("/health", self.health, methods=["GET"]),
        Route("/query", self.query, methods=["POST"]),
        Route("/ask", self.ask, methods=["POST"]),
        Route("/knowledge", self.list_knowledge, methods=["GET"]),
        Route("/knowledge/{id:int}", self.get_knowledge, methods=["GET"]),
        Route("/events", self.list_events, methods=["GET"]),
        Route("/sessions", self.list_sessions, methods=["GET"]),
    ],
    lifespan=lifespan,
)
```

- [ ] **Step 2: Run full test suite**

Run: `pytest brain/tests/test_server.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add brain/src/hippo_brain/server.py
git commit -m "feat(brain): add /events and /sessions endpoints for GUI"
```

---

## Chunk 2: SwiftUI Application Scaffold

Create the SwiftUI app structure.

### Task 2.1: Create gui/ directory structure

**Files:**
- Create: `gui/Package.swift`
- Create: `gui/Sources/HippoGUI/App/HippoGUIApp.swift`
- Create: `gui/Sources/HippoGUI/Views/ContentView.swift`
- Create: `gui/Resources/Assets.xcassets/`

- [ ] **Step 1: Create Package.swift**

```swift
// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "HippoGUI",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(
            name: "HippoGUI",
            targets: ["HippoGUI"]
        )
    ],
    targets: [
        .executableTarget(
            name: "HippoGUI",
            path: "Sources/HippoGUI",
            resources: ["Resources"]
        )
    ]
)
```

- [ ] **Step 2: Create minimal SwiftUI app**

```swift
import SwiftUI

@main
struct HippoGUIApp: App {
    var body: some Scene {
        Window {
            ContentView()
        }
    }
}
```

```swift
import SwiftUI

struct ContentView: View {
    var body: some View {
        Text("Hippo GUI")
            .padding()
    }
}
```

- [ ] **Step 3: Initialize with swift build**

Run: `cd gui && swift build`
Expected: Builds successfully

- [ ] **Step 4: Commit**

```bash
git add gui/
git commit -m "feat(gui): scaffold SwiftUI application"
```

### Task 2.2: Create BrainClient service

**Files:**
- Create: `gui/Sources/HippoGUI/Services/BrainClient.swift`
- Create: `gui/Sources/HippoGUI/Models/`

- [ ] **Step 1: Create Models**

```swift
import Foundation

struct KnowledgeNode: Identifiable, Codable {
    let id: Int
    let uuid: String
    let content: String
    let nodeType: String
    let outcome: String?
    let tags: String?
    let createdAt: Int

    enum CodingKeys: String, CodingKey {
        case id, uuid, content
        case nodeType = "node_type"
        case outcome, tags
        case createdAt = "created_at"
    }
}

struct KnowledgeListResponse: Codable {
    let nodes: [KnowledgeNode]
    let total: Int
}
```

```swift
import Foundation

struct Event: Identifiable, Codable {
    let id: Int
    let sessionId: Int
    let timestamp: Int
    let command: String
    let exitCode: Int?
    let durationMs: Int
    let cwd: String
    let gitBranch: String?

    enum CodingKeys: String, CodingKey {
        case id, sessionId = "session_id", timestamp, command
        case exitCode = "exit_code"
        case durationMs = "duration_ms"
        case cwd, gitBranch = "git_branch"
    }
}

struct EventListResponse: Codable {
    let events: [Event]
    let total: Int
}
```

```swift
import Foundation

struct Session: Identifiable, Codable {
    let id: Int
    let startTime: Int
    let hostname: String
    let shell: String
    let eventCount: Int

    enum CodingKeys: String, CodingKey {
        case id, startTime = "start_time"
        case hostname, shell, eventCount = "event_count"
    }
}

struct SessionListResponse: Codable {
    let sessions: [Session]
    let total: Int
}
```

```swift
import Foundation

struct AskResponse: Codable {
    let answer: String?
    let sources: [AskSource]?
    let model: String?
    let error: String?
}

struct AskSource: Codable, Identifiable {
    let id: Int
    let summary: String
    let score: Double?
    let nodeType: String?
}
```

- [ ] **Step 2: Create BrainClient**

```swift
import Foundation

actor BrainClient {
    private let baseURL: URL
    private let session: URLSession

    init(port: Int = 8765) {
        self.baseURL = URL(string: "http://localhost:\(port)")!
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        self.session = URLSession(configuration: config)
    }

    func listKnowledge(limit: Int = 20, offset: Int = 0, nodeType: String? = nil) async throws -> KnowledgeListResponse {
        var components = URLComponents(url: baseURL.appendingPathComponent("knowledge"), resolvingAgainstBaseURL: false)!
        var queryItems = [URLQueryItem(name: "limit", value: String(limit)), URLQueryItem(name: "offset", value: String(offset))]
        if let nodeType = nodeType {
            queryItems.append(URLQueryItem(name: "node_type", value: nodeType))
        }
        components.queryItems = queryItems

        let (data, response) = try await session.data(from: components.url!)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw BrainClientError.requestFailed
        }
        return try JSONDecoder().decode(KnowledgeListResponse.self, from: data)
    }

    func getKnowledge(id: Int) async throws -> KnowledgeNode {
        let url = baseURL.appendingPathComponent("knowledge/\(id)")
        let (data, response) = try await session.data(from: url)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw BrainClientError.notFound
        }
        return try JSONDecoder().decode(KnowledgeNode.self, from: data)
    }

    func ask(question: String, limit: Int = 10) async throws -> AskResponse {
        var request = URLRequest(url: baseURL.appendingPathComponent("ask"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(["question": question, "limit": limit])

        let (data, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw BrainClientError.requestFailed
        }
        return try JSONDecoder().decode(AskResponse.self, from: data)
    }

    func listEvents(limit: Int = 20, offset: Int = 0, sessionId: Int? = nil) async throws -> EventListResponse {
        var components = URLComponents(url: baseURL.appendingPathComponent("events"), resolvingAgainstBaseURL: false)!
        var queryItems = [URLQueryItem(name: "limit", value: String(limit)), URLQueryItem(name: "offset", value: String(offset))]
        if let sessionId = sessionId {
            queryItems.append(URLQueryItem(name: "session_id", value: String(sessionId)))
        }
        components.queryItems = queryItems

        let (data, response) = try await session.data(from: components.url!)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw BrainClientError.requestFailed
        }
        return try JSONDecoder().decode(EventListResponse.self, from: data)
    }

    func listSessions(limit: Int = 20, offset: Int = 0) async throws -> SessionListResponse {
        var components = URLComponents(url: baseURL.appendingPathComponent("sessions"), resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "limit", value: String(limit)), URLQueryItem(name: "offset", value: String(offset))]

        let (data, response) = try await session.data(from: components.url!)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw BrainClientError.requestFailed
        }
        return try JSONDecoder().decode(SessionListResponse.self, from: data)
    }

    func health() async throws -> Bool {
        let (data, response) = try await session.data(from: baseURL.appendingPathComponent("health"))
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            return false
        }
        struct HealthResponse: Codable { let status: String }
        let health = try JSONDecoder().decode(HealthResponse.self, from: data)
        return health.status == "ok"
    }
}

enum BrainClientError: Error {
    case requestFailed
    case notFound
}
```

- [ ] **Step 3: Commit**

```bash
git add gui/
git commit -m "feat(gui): add BrainClient and models"
```

### Task 2.3: Create Views

**Files:**
- Modify: `gui/Sources/HippoGUI/Views/ContentView.swift`
- Create: `gui/Sources/HippoGUI/Views/QueryAskView.swift`
- Create: `gui/Sources/HippoGUI/Views/KnowledgeView.swift`
- Create: `gui/Sources/HippoGUI/Views/EventBrowserView.swift`
- Create: `gui/Sources/HippoGUI/Views/StatusView.swift`

- [ ] **Step 1: Create QueryAskView**

```swift
import SwiftUI

struct QueryAskView: View {
    @State private var question = ""
    @State private var isLoading = false
    @State private var answer = ""
    @State private var sources: [AskSource] = []

    let client: BrainClient

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Ask Hippo")
                .font(.headline)

            HStack {
                TextField("Ask a question...", text: $question)
                    .textFieldStyle(.roundedBorder)

                Button("Ask") {
                    Task {
                        isLoading = true
                        defer { isLoading = false }
                        do {
                            let response = try await client.ask(question: question)
                            answer = response.answer ?? response.error ?? "No answer"
                            sources = response.sources ?? []
                        } catch {
                            answer = "Error: \(error.localizedDescription)"
                        }
                    }
                }
                .disabled(question.isEmpty || isLoading)
            }

            if isLoading {
                ProgressView()
            }

            if !answer.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Answer")
                        .font(.subheadline)
                        .foregroundColor(.secondary)
                    Text(answer)
                        .textSelection(.enabled)

                    if !sources.isEmpty {
                        Divider()
                        Text("Sources")
                            .font(.subheadline)
                            .foregroundColor(.secondary)
                        ForEach(sources) { source in
                            Text("\(source.id): \(source.summary)")
                                .font(.caption)
                        }
                    }
                }
            }
        }
        .padding()
    }
}
```

- [ ] **Step 2: Create KnowledgeView**

```swift
import SwiftUI

struct KnowledgeView: View {
    @State private var nodes: [KnowledgeNode] = []
    @State private var isLoading = false
    @State private var selectedNode: KnowledgeNode?

    let client: BrainClient

    var body: some View {
        SplitView {
            List(nodes) { node in
                VStack(alignment: .leading) {
                    Text(node.content.prefix(100))
                        .font(.body)
                    Text(node.nodeType)
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .tag(node)
            }
            .listStyle(.inset)
            .onAppear {
                Task {
                    isLoading = true
                    defer { isLoading = false }
                    do {
                        let response = try await client.listKnowledge()
                        nodes = response.nodes
                    } catch {
                        print("Error: \(error)")
                    }
                }
            }

            if isLoading {
                ProgressView()
            } else if let selected = selectedNode {
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(selected.content)
                            .textSelection(.enabled)
                        if let outcome = selected.outcome {
                            Text("Outcome: \(outcome)")
                                .foregroundColor(.secondary)
                        }
                        Text("Created: \(selected.createdAt)")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    .padding()
                }
            }
        }
    }
}
```

- [ ] **Step 3: Create EventBrowserView**

```swift
import SwiftUI

struct EventBrowserView: View {
    @State private var sessions: [Session] = []
    @State private var events: [Event] = []
    @State private var selectedSession: Session?
    @State private var selectedEvent: Event?
    @State private var isLoading = false

    let client: BrainClient

    var body: some View {
        NavigationSplitView {
            List(sessions, id: \.id, selection: $selectedSession) { session in
                VStack(alignment: .leading) {
                    Text("Session \(session.id)")
                    Text("\(session.hostname) • \(session.shell)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    Text("\(session.eventCount) events")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .tag(session)
            }
            .listStyle(.inset)
            .onAppear {
                Task {
                    isLoading = true
                    defer { isLoading = false }
                    do {
                        let response = try await client.listSessions()
                        sessions = response.sessions
                    } catch {
                        print("Error: \(error)")
                    }
                }
            }
            .onChange(of: selectedSession) { _, newSession in
                guard let session = newSession else { return }
                Task {
                    do {
                        let response = try await client.listEvents(sessionId: session.id)
                        events = response.events
                    } catch {
                        print("Error: \(error)")
                    }
                }
            }

            List(events) { event in
                VStack(alignment: .leading) {
                    Text(event.command.prefix(50))
                        .font(.body)
                    Text("\(event.cwd) [exit: \(event.exitCode ?? -1)]")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .tag(event)
            }
            .listStyle(.inset)

            if let event = selectedEvent {
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(event.command)
                            .font(.system(.body, design: .monospaced))
                        Text("CWD: \(event.cwd)")
                        Text("Duration: \(event.durationMs)ms")
                        Text("Exit: \(event.exitCode ?? -1)")
                    }
                    .padding()
                }
            }
        }
    }
}
```

- [ ] **Step 4: Create StatusView**

```swift
import SwiftUI

struct StatusView: View {
    @State private var brainHealth = false
    @State private var isLoading = false

    let client: BrainClient

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Status")
                .font(.headline)

            HStack {
                Circle()
                    .fill(brainHealth ? Color.green : Color.red)
                    .frame(width: 10, height: 10)
                Text("Brain Server")
                    .foregroundColor(.secondary)
            }

            Button("Refresh") {
                Task {
                    isLoading = true
                    defer { isLoading = false }
                    do {
                        brainHealth = try await client.health()
                    } catch {
                        brainHealth = false
                    }
                }
            }
        }
        .padding()
        .onAppear {
            Task {
                do {
                    brainHealth = try await client.health()
                } catch {
                    brainHealth = false
                }
            }
        }
    }
}
```

- [ ] **Step 5: Update ContentView with navigation**

```swift
import SwiftUI

struct ContentView: View {
    @State private var selectedTab = 0
    let client = BrainClient()

    var body: some View {
        TabView(selection: $selectedTab) {
            QueryAskView(client: client)
                .tabItem {
                    Label("Query", systemImage: "magnifyingglass")
                }
                .tag(0)

            KnowledgeView(client: client)
                .tabItem {
                    Label("Knowledge", systemImage: "brain")
                }
                .tag(1)

            EventBrowserView(client: client)
                .tabItem {
                    Label("Events", systemImage: "terminal")
                }
                .tag(2)

            StatusView(client: client)
                .tabItem {
                    Label("Status", systemImage: "heart")
                }
                .tag(3)
        }
        .frame(minWidth: 800, minHeight: 600)
    }
}
```

- [ ] **Step 6: Commit**

```bash
git add gui/
git commit -m "feat(gui): add SwiftUI views for Query, Knowledge, Events, Status"
```

---

## Chunk 3: Configuration Loading

Add config file reading for brain port.

### Task 3.1: Create ConfigClient

**Files:**
- Create: `gui/Sources/HippoGUI/Services/ConfigClient.swift`

- [ ] **Step 1: Create ConfigClient**

```swift
import Foundation

struct HippoConfig: Codable {
    let brain: BrainConfig

    struct BrainConfig: Codable {
        let port: Int
    }
}

actor ConfigClient {
    private var cachedPort: Int?

    func getBrainPort() async -> Int {
        if let port = cachedPort {
            return port
        }

        let configPath = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/hippo/config.toml")

        guard FileManager.default.fileExists(atPath: configPath.path) else {
            cachedPort = 8765
            return 8765
        }

        do {
            let content = try String(contentsOf: configPath, encoding: .utf8)
            // Simple TOML parsing for brain.port
            let port = parseBrainPort(from: content)
            cachedPort = port
            return port
        } catch {
            cachedPort = 8765
            return 8765
        }
    }

    private func parseBrainPort(from content: String) -> Int {
        let lines = content.components(separatedBy: .newlines)
        var inBrainSection = false
        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed == "[brain]" {
                inBrainSection = true
                continue
            }
            if trimmed.hasPrefix("[") && trimmed.hasSuffix("]") {
                inBrainSection = false
                continue
            }
            if inBrainSection && trimmed.hasPrefix("port") && trimmed.contains("=") {
                let parts = trimmed.components(separatedBy: "=")
                if parts.count >= 2 {
                    let portStr = parts[1].trimmingCharacters(in: .whitespaces)
                    if let port = Int(portStr) {
                        return port
                    }
                }
            }
        }
        return 8765
    }
}
```

- [ ] **Step 2: Update BrainClient to use ConfigClient**

Modify BrainClient init to accept port parameter, update HippoGUIApp to load config first.

- [ ] **Step 3: Commit**

---

## Testing & Validation

### Final Verification

- [ ] **Swift build compiles**

Run: `cd gui && swift build`
Expected: SUCCESS

- [ ] **App launches**

Run: Open in Xcode, run the app
Expected: Window opens with tab navigation

- [ ] **Health check works**

Click Status tab
Expected: Shows green/red indicator for brain server

---

## Plan Complete

All chunks written to `docs/superpowers/plans/2026-04-18-hippo-gui-implementation.md`.