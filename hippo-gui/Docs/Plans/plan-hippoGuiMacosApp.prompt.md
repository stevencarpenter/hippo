## Plan: Build Out HippoGUI macOS SwiftUI App (Implemented)

**Target platform:** macOS 26+ · **Swift:** 6.x · **SwiftUI:** latest (WWDC 2025 design system)

A phased plan to evolve HippoGUI from a basic functional prototype into a spec-compliant, read-only macOS app using the latest language and framework features. Phases cover: foundation (Swift 6 concurrency + MVVM + data model fixes), core UX (navigation, pagination, search, filters), feature enrichment (status, related entities/events, auto-refresh), and testing.

**Status:** Implemented in the current Swift package-based app as of April 2026.

**Implementation notes:**

- `Package.swift` currently uses `// swift-tools-version: 6.3`, `.macOS(.v26)`, and `swiftLanguageModes: [.v6]`.
- Dependency injection is implemented with a custom SwiftUI environment key via `.brainClient(...)` and `@Environment(\.brainClient)` rather than `.environment(brainClient)` / `@Environment(BrainClient.self)`.
- ViewModels are `@Observable @MainActor`, previews use `MockBrainClient`, and tests live in `Tests/HippoGUITests/` using Swift Testing.

---

### Phase 0 — Toolchain Upgrade
*Goal: Set the project up for Swift 6 strict concurrency and macOS 26 before any other changes.*

- [x] **0.1 Bump `Package.swift` to Swift 6 tooling** — The package now uses `// swift-tools-version: 6.3`, targets `.macOS(.v26)`, and opts into Swift 6 via `swiftLanguageModes: [.v6]`.

- [x] **0.2 Make all models `Sendable`** — `KnowledgeNode`, `Event`, `Session`, `AskResponse`, `AskSource`, and new response/detail types were updated for Swift 6 concurrency.

- [x] **0.3 Apply typed throws to `BrainClient`** — `BrainClient` and `BrainClientProtocol` now use `throws(BrainClientError)` throughout the public API.

- [x] **0.4 Annotate ViewModels with `@MainActor`** — All view models are now `@Observable @MainActor`.

---

### Phase 1 — Foundation: MVVM + Architecture Cleanup + Data Model Fixes
*Goal: Correct the data model and separate concerns before adding features. All future work builds on this.*

- [x] **1.1 Update `KnowledgeNode` model** — `KnowledgeNode` now decodes `related_entities` and `related_events`, and the related value types conform to `Sendable`.

- [x] **1.2 Add `POST /query` to `BrainClient`** — `BrainClient` now supports `POST /query`, backed by a `Sendable & Codable` `QueryResponse` model.

- [x] **1.3 Create `@Observable @MainActor` ViewModels** — `QueryViewModel`, `KnowledgeViewModel`, `EventBrowserViewModel`, and `StatusViewModel` now own the async loading and presentation state.

- [x] **1.4 Inject `BrainClient` via Environment** — `HippoGUIApp.swift` now stores a synchronous `BrainClient()` and injects it through a dedicated environment key consumed with `@Environment(\.brainClient)`.

- [x] **1.5 Wire Views to ViewModels** — Each primary view now owns a view model and binds its controls to `vm.*` state while configuring the injected client.

- [x] **1.6 Enforce minimum window size** — The app scene and `ContentView` now enforce the planned minimum window sizes.

---

### Phase 2 — Core UX: Navigation + Pagination + Search + Filters + macOS 26 Design
*Goal: Make lists usable, add spec-required filters, and adopt the macOS 26 Liquid Glass design language.*

- [x] **2.1 Replace `TabView` with `NavigationSplitView`** — `ContentView` now uses `NavigationSplitView` with a balanced sidebar/detail layout.

- [x] **2.2 Add Ask / Search segmented control to `QueryAskView`** — `QueryAskView` now switches between `POST /ask` and `POST /query` with shared result rendering.

- [x] **2.3 Add `since_ms` filter to Knowledge, Events, and Sessions** — Preset time filters are wired through the view models, `BrainClient`, and the corresponding list endpoints.

- [x] **2.4 Add `project` filter to Events** — `EventBrowserViewModel` and `EventBrowserView` now support project filtering for events.

- [x] **2.5 Add `node_type` filter to Knowledge list** — Knowledge type filtering is now bound through `KnowledgeViewModel` into `GET /knowledge`.

- [x] **2.6 Add "Load More" pagination to Knowledge list** — Knowledge pagination tracks `offset` and `total`, and appends results through a "Load More" flow.

- [x] **2.7 Add "Load More" pagination to Sessions and Events lists** — Sessions and events now page independently through `EventBrowserViewModel`.

- [x] **2.8 Add text search to `KnowledgeView`** — Knowledge list search is now implemented client-side over summaries and tags.

- [x] **2.9 Add command filter to `EventBrowserView`** — Event command filtering is now implemented via `.searchable` and `EventBrowserViewModel`.

- [x] **2.10 Improve error UX with retry** — A reusable `ErrorBannerView` now provides consistent retryable error handling across the main views.

---

### Phase 3 — Feature Enrichment: Status, Related Entities/Events, Auto-Refresh
*Goal: Make the app informative, spec-complete on detail views, and live-updating.*

- [x] **3.1 Expand `StatusView`** — `StatusViewModel` and `StatusView` now surface daemon responsiveness, brain reachability, and queue depth summary.

- [x] **3.2 Display `related_entities` and `related_events` in `KnowledgeView` detail** — Knowledge detail now renders related entities and related events below the main content.

- [x] **3.3 Improve `KnowledgeNode` content display** — Knowledge detail now renders structured fields and keeps the raw JSON inside a disclosure section.

- [x] **3.4 Add auto-refresh timer to `StatusView`** — `StatusView` now refreshes on a Swift concurrency-driven timer and updates the last-checked timestamp.

- [x] **3.5 Preserve sidebar selection across launches** — Sidebar selection is now persisted with `@AppStorage("selectedTab")`.

---

### Phase 4 — Testing
*Goal: Meet the spec's explicit testing requirements — previews, ViewModel unit tests, HTTP integration tests — using the modern Swift Testing framework.*

- [x] **4.1 Extract `BrainClientProtocol`** — `BrainClientProtocol` now enables previews and tests to inject `MockBrainClient` without a live server.

- [x] **4.2 Add SwiftUI Previews for all views** — The main views now include SwiftUI previews backed by `MockBrainClient`.

- [x] **4.3 Add a `HippoGUITests` test target using Swift Testing** — `Package.swift` now defines `HippoGUITests`, and the test suite uses the Swift Testing framework.

- [x] **4.4 Unit tests for ViewModels** — View model tests cover success flows, filter logic, and pagination behavior using `MockBrainClient`.

- [x] **4.5 Integration tests for HTTP response parsing** — Decoding tests cover related entities/events, `QueryResponse`, and paginated response models.

---

### Future / Out of Scope (Post-MVP)

These items are **explicitly excluded from the MVP** per the design spec:

- **Menu bar extra** — Future consideration.
- **Keyboard shortcuts** — Future consideration.
- **Graph visualization** — For knowledge node relationships; requires graph layout work.
- **Write operations** — App is strictly read-only for MVP.
- **System notifications** — Future consideration (e.g. enrichment completion alerts).

#### Implemented after spec

- **App icon** — Initially out of scope; subsequently added (`Resources/Assets.xcassets/AppIcon.appiconset` plus the generator at `scripts/generate-app-icon.swift`).

---

### Further Considerations

1. **`@Observable` vs `ObservableObject`** — `@Observable` (macOS 14 / Swift 5.9) is the modern approach and the only correct choice for Swift 6 + macOS 26. Do not fall back to `ObservableObject`/`@Published` — they are legacy and will generate Swift 6 concurrency warnings.
2. **`since_ms` filter UI** — The spec requires the query param but doesn't prescribe UI. A preset segmented control ("Last 24 h / 7 days / All") is simpler than a `DatePicker` for MVP.
3. **Queue depth in StatusView** — The brain `/health` endpoint was extended so the GUI can surface queue summary in `StatusView`.
4. **Liquid Glass** — macOS 26 introduces the new Liquid Glass design language (WWDC 2025). System containers (`NavigationSplitView`, toolbars, sheets) automatically adopt it. Avoid `Color.primary` overlays or custom backgrounds on sidebar/toolbar areas — let the system render the glass material.
5. **Swift 6 concurrency in practice** — `BrainClient` is already an `actor`, which is ideal. ViewModels are `@MainActor`. The only compile-time risk is any `@escaping` closure that captures ViewModel state — annotate such closures with `@MainActor` or use `Task { @MainActor in … }` to silence warnings.