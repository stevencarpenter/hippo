import Foundation
import Testing
@testable import HippoGUIKit

@MainActor
struct ViewModelTests {
    @Test
    func queryViewModelAskSuccess() async throws {
        let mock = MockBrainClient(
            askResponse: .success(
                AskResponse(
                    answer: "You updated the GUI.",
                    sources: [AskSource(summary: "Edited `ContentView.swift`", score: 0.92)],
                    model: "preview",
                    error: nil,
                    degraded: false,
                    stage: nil
                )
            )
        )
        let vm = QueryViewModel(client: mock)
        vm.queryText = "What changed?"

        await vm.submit()

        #expect(vm.answerText == "You updated the GUI.")
        #expect(vm.askSources.count == 1)
        #expect(vm.errorMessage == nil)
        let lastRequest = await mock.lastAskRequest
        #expect(lastRequest?.question == "What changed?")
        #expect(lastRequest?.limit == 10)
    }

    @Test
    func knowledgeViewModelPaginationAndFiltering() async throws {
        let first = KnowledgeNode(
            id: 1,
            uuid: "node-1",
            content: "{\"summary\":\"First node\",\"tags\":[\"swift\"]}",
            nodeType: "observation",
            outcome: "success",
            tags: ["swift"],
            createdAt: 1_713_404_800_000
        )
        let second = KnowledgeNode(
            id: 2,
            uuid: "node-2",
            content: "{\"summary\":\"Second node\",\"tags\":[\"rust\"]}",
            nodeType: "concept",
            outcome: "success",
            tags: ["rust"],
            createdAt: 1_713_404_900_000
        )
        let mock = MockBrainClient(
            knowledgeResponsesSequence: [
                .success(.init(nodes: [first], total: 2)),
                .success(.init(nodes: [second], total: 2))
            ],
            knowledgeDetails: [
                1: .success(first),
                2: .success(second)
            ]
        )
        let vm = KnowledgeViewModel(client: mock)

        await vm.loadKnowledge(reset: true)
        #expect(vm.nodes.count == 1)
        #expect(vm.canLoadMore)

        await vm.loadMore()
        #expect(vm.nodes.count == 2)
        #expect(vm.offset == 2)
        let lastRequest = await mock.lastKnowledgeRequest
        #expect(lastRequest?.offset == 1)

        vm.searchText = "rust"
        #expect(vm.filteredNodes.map(\.id) == [2])
    }

    @Test
    func eventBrowserViewModelUsesSinceAndProjectFilters() async throws {
        let session = Session(id: 1, startTime: 1_713_404_800_000, hostname: "laptop", shell: "zsh", eventCount: 1)
        let event = Event(id: 11, sessionId: 1, timestamp: 1_713_404_800_000, command: "swift test", exitCode: 0, durationMs: 400, cwd: "/Users/carpenter/projects/hippo", gitBranch: "main")
        let mock = MockBrainClient(
            eventResponse: .success(.init(events: [event], total: 1)),
            sessionResponse: .success(.init(sessions: [session], total: 1))
        )
        let vm = EventBrowserViewModel(client: mock)
        vm.sincePreset = .last24Hours
        vm.project = "hippo"

        await vm.loadSessions(reset: true)
        await vm.loadEvents(reset: true)

        #expect(vm.sessions.count == 1)
        #expect(vm.filteredEvents.count == 1)
        let sessionRequest = await mock.lastSessionRequest
        #expect(sessionRequest?.sinceMs != nil)
        let eventRequest = await mock.lastEventRequest
        #expect(eventRequest?.sessionId == 1)
        #expect(eventRequest?.project == "hippo")
        #expect(eventRequest?.sinceMs != nil)
    }

    // MARK: - QueryViewModel additional coverage

    @Test
    func queryViewModelIgnoresWhitespaceOnlyQuery() async throws {
        let mock = MockBrainClient()
        let vm = QueryViewModel(client: mock)
        vm.queryText = "   "

        await vm.submit()

        let lastRequest = await mock.lastAskRequest
        #expect(lastRequest == nil)
        #expect(vm.answerText.isEmpty)
        #expect(vm.errorMessage == nil)
    }

    @Test
    func queryViewModelSearchModeCallsQueryNotAsk() async throws {
        let result = SemanticQueryResult(summary: "Found something relevant")
        let mock = MockBrainClient(
            queryResponse: .success(.init(mode: .semantic, results: [result]))
        )
        let vm = QueryViewModel(client: mock)
        vm.mode = .search
        vm.queryText = "What did I work on?"

        await vm.submit()

        let queryRequest = await mock.lastQueryRequest
        let askRequest = await mock.lastAskRequest
        #expect(queryRequest?.text == "What did I work on?")
        #expect(askRequest == nil)
        #expect(vm.searchResponse?.results.count == 1)
        #expect(vm.errorMessage == nil)
    }

    @Test
    func queryViewModelAskResponseWithErrorFieldSetsErrorMessage() async throws {
        let mock = MockBrainClient(
            askResponse: .success(AskResponse(
                answer: nil,
                sources: nil,
                model: nil,
                error: "LLM unavailable",
                degraded: true,
                stage: nil
            ))
        )
        let vm = QueryViewModel(client: mock)
        vm.queryText = "test question"

        await vm.submit()

        #expect(vm.errorMessage == "LLM unavailable")
        #expect(vm.answerText.isEmpty)
        #expect(vm.askSources.isEmpty)
    }

    @Test
    func queryViewModelNetworkErrorSetsErrorMessage() async throws {
        let mock = MockBrainClient(askResponse: .failure(.networkError("Connection refused")))
        let vm = QueryViewModel(client: mock)
        vm.queryText = "test question"

        await vm.submit()

        #expect(vm.errorMessage != nil)
        #expect(vm.answerText.isEmpty)
    }

    @Test
    func queryViewModelWithoutClientSetsErrorMessage() async throws {
        let vm = QueryViewModel(client: nil)
        vm.queryText = "test question"

        await vm.submit()

        #expect(vm.errorMessage != nil)
    }

    @Test
    func queryViewModelClearResultsResetsAllState() {
        let vm = QueryViewModel()
        vm.answerText = "some answer"
        vm.askSources = [AskSource(summary: "source")]
        vm.errorMessage = "some error"

        vm.clearResults()

        #expect(vm.answerText.isEmpty)
        #expect(vm.askSources.isEmpty)
        #expect(vm.searchResponse == nil)
        #expect(vm.errorMessage == nil)
    }

    @Test
    func queryViewModelHasResultsWithAnswer() {
        let vm = QueryViewModel()
        #expect(!vm.hasResults)

        vm.answerText = "something"
        #expect(vm.hasResults)
    }

    @Test
    func queryViewModelHasResultsWithNonEmptySearchResponse() {
        let vm = QueryViewModel()
        vm.searchResponse = QueryResponse(mode: .semantic, results: [SemanticQueryResult(summary: "found")])
        #expect(vm.hasResults)
    }

    @Test
    func queryViewModelHasResultsIsFalseForEmptySearchResponse() {
        let vm = QueryViewModel()
        vm.searchResponse = QueryResponse(mode: .search)
        #expect(!vm.hasResults)
    }

    @Test
    func queryViewModelComputedLabels() {
        let vm = QueryViewModel()

        vm.mode = .ask
        #expect(vm.buttonTitle == "Ask")
        #expect(vm.promptTitle == "Ask Hippo")
        #expect(vm.promptPlaceholder == "Ask a question...")

        vm.mode = .search
        #expect(vm.buttonTitle == "Search")
        #expect(vm.promptTitle == "Search Hippo")
        #expect(vm.promptPlaceholder == "Search your knowledge...")
    }

    // MARK: - KnowledgeViewModel additional coverage

    @Test
    func knowledgeViewModelErrorOnResetClearsState() async throws {
        let mock = MockBrainClient(knowledgeResponse: .failure(.networkError("No connection")))
        let vm = KnowledgeViewModel(client: mock)

        await vm.loadKnowledge(reset: true)

        #expect(vm.nodes.isEmpty)
        #expect(vm.total == 0)
        #expect(vm.offset == 0)
        #expect(vm.errorMessage != nil)
        #expect(vm.selectedNodeID == nil)
        #expect(vm.selectedNodeDetail == nil)
    }

    @Test
    func knowledgeViewModelWithoutClientSetsError() async {
        let vm = KnowledgeViewModel(client: nil)
        await vm.loadKnowledge(reset: true)
        #expect(vm.errorMessage != nil)
    }

    @Test
    func knowledgeViewModelFilteredNodesReturnsAllWhenSearchEmpty() async throws {
        let n1 = KnowledgeNode(id: 1, uuid: "n1", content: "{\"summary\":\"Swift stuff\"}", nodeType: "observation", tags: ["swift"], createdAt: 0)
        let n2 = KnowledgeNode(id: 2, uuid: "n2", content: "{\"summary\":\"Rust stuff\"}", nodeType: "observation", tags: ["rust"], createdAt: 0)
        let mock = MockBrainClient(
            knowledgeResponse: .success(.init(nodes: [n1, n2], total: 2)),
            knowledgeDetails: [1: .success(n1), 2: .success(n2)]
        )
        let vm = KnowledgeViewModel(client: mock)
        await vm.loadKnowledge(reset: true)

        vm.searchText = ""
        #expect(vm.filteredNodes.count == 2)
    }

    @Test
    func knowledgeViewModelFilteredNodesByDisplaySummary() async throws {
        let n1 = KnowledgeNode(id: 1, uuid: "n1", content: "{\"summary\":\"Actor isolation fix\"}", nodeType: "observation", tags: [], createdAt: 0)
        let n2 = KnowledgeNode(id: 2, uuid: "n2", content: "{\"summary\":\"Cargo workspace setup\"}", nodeType: "observation", tags: [], createdAt: 0)
        let mock = MockBrainClient(
            knowledgeResponse: .success(.init(nodes: [n1, n2], total: 2)),
            knowledgeDetails: [1: .success(n1), 2: .success(n2)]
        )
        let vm = KnowledgeViewModel(client: mock)
        await vm.loadKnowledge(reset: true)

        vm.searchText = "cargo"
        #expect(vm.filteredNodes.map(\.id) == [2])
    }

    @Test
    func knowledgeViewModelSelectedNodeFallsBackToListNode() async throws {
        let node = KnowledgeNode(id: 1, uuid: "n1", content: "{}", nodeType: "observation", createdAt: 0)
        let mock = MockBrainClient(
            knowledgeResponse: .success(.init(nodes: [node], total: 1)),
            knowledgeDetails: [1: .success(node)]
        )
        let vm = KnowledgeViewModel(client: mock)
        await vm.loadKnowledge(reset: true)

        // Detail was auto-loaded; clear it to exercise the fallback path
        vm.selectedNodeDetail = nil

        #expect(vm.selectedNode?.id == 1)
    }

    @Test
    func knowledgeViewModelSelectNilClearsDetail() async throws {
        let node = KnowledgeNode(id: 1, uuid: "n1", content: "{}", nodeType: "observation", createdAt: 0)
        let mock = MockBrainClient(
            knowledgeResponse: .success(.init(nodes: [node], total: 1)),
            knowledgeDetails: [1: .success(node)]
        )
        let vm = KnowledgeViewModel(client: mock)
        await vm.loadKnowledge(reset: true)
        #expect(vm.selectedNodeDetail != nil)

        await vm.selectNode(id: nil)

        #expect(vm.selectedNodeID == nil)
        #expect(vm.selectedNodeDetail == nil)
    }

    @Test
    func knowledgeViewModelSelectNodeLoadsDetail() async throws {
        let n1 = KnowledgeNode(id: 1, uuid: "n1", content: "{\"summary\":\"Node one\"}", nodeType: "observation", createdAt: 0)
        let n2 = KnowledgeNode(id: 2, uuid: "n2", content: "{\"summary\":\"Node two\"}", nodeType: "observation", createdAt: 0)
        let mock = MockBrainClient(
            knowledgeResponse: .success(.init(nodes: [n1, n2], total: 2)),
            knowledgeDetails: [1: .success(n1), 2: .success(n2)]
        )
        let vm = KnowledgeViewModel(client: mock)
        await vm.loadKnowledge(reset: true)
        #expect(vm.selectedNodeID == 1)

        await vm.selectNode(id: 2)

        #expect(vm.selectedNodeID == 2)
        #expect(vm.selectedNodeDetail?.id == 2)
    }

    // MARK: - EventBrowserViewModel additional coverage

    @Test
    func eventBrowserViewModelCommandFilterFiltersEvents() async throws {
        let e1 = Event(id: 1, sessionId: 1, timestamp: 0, command: "swift build", exitCode: 0, durationMs: 100, cwd: "/tmp", gitBranch: nil)
        let e2 = Event(id: 2, sessionId: 1, timestamp: 0, command: "cargo test", exitCode: 0, durationMs: 200, cwd: "/tmp", gitBranch: nil)
        let session = Session(id: 1, startTime: 0, hostname: "laptop", shell: "zsh", eventCount: 2)
        let mock = MockBrainClient(
            eventResponse: .success(.init(events: [e1, e2], total: 2)),
            sessionResponse: .success(.init(sessions: [session], total: 1))
        )
        let vm = EventBrowserViewModel(client: mock)
        await vm.loadSessions(reset: true)

        vm.commandFilter = "cargo"
        #expect(vm.filteredEvents.count == 1)
        #expect(vm.filteredEvents.first?.command == "cargo test")

        vm.commandFilter = ""
        #expect(vm.filteredEvents.count == 2)
    }

    @Test
    func eventBrowserViewModelSessionLoadErrorResetsState() async throws {
        let mock = MockBrainClient(sessionResponse: .failure(.networkError("No connection")))
        let vm = EventBrowserViewModel(client: mock)

        await vm.loadSessions(reset: true)

        #expect(vm.sessions.isEmpty)
        #expect(vm.sessionTotal == 0)
        #expect(vm.errorMessage != nil)
    }

    @Test
    func eventBrowserViewModelLoadMoreSessionsSkipsWhenCannotLoadMore() async throws {
        let session = Session(id: 1, startTime: 0, hostname: "laptop", shell: "zsh", eventCount: 0)
        let mock = MockBrainClient(
            eventResponse: .success(.init(events: [], total: 0)),
            sessionResponse: .success(.init(sessions: [session], total: 1))
        )
        let vm = EventBrowserViewModel(client: mock)
        await vm.loadSessions(reset: true)

        // sessions.count == sessionTotal == 1, so canLoadMoreSessions is false
        #expect(!vm.canLoadMoreSessions)
        let offsetBefore = vm.sessionOffset

        await vm.loadMoreSessions()

        #expect(vm.sessionOffset == offsetBefore)
    }

    @Test
    func eventBrowserViewModelSelectSessionLoadsItsEvents() async throws {
        let s1 = Session(id: 1, startTime: 0, hostname: "laptop", shell: "zsh", eventCount: 0)
        let s2 = Session(id: 2, startTime: 1000, hostname: "laptop", shell: "zsh", eventCount: 1)
        let eventsForS2 = [Event(id: 21, sessionId: 2, timestamp: 0, command: "cargo build", exitCode: 0, durationMs: 100, cwd: "/tmp", gitBranch: nil)]
        let mock = MockBrainClient(
            eventResponsesSequence: [
                .success(.init(events: [], total: 0)),         // auto-load for s1
                .success(.init(events: eventsForS2, total: 1)) // load after selectSession(2)
            ],
            sessionResponse: .success(.init(sessions: [s1, s2], total: 2))
        )
        let vm = EventBrowserViewModel(client: mock)
        await vm.loadSessions(reset: true)
        #expect(vm.selectedSessionID == 1)

        await vm.selectSession(id: 2)

        #expect(vm.selectedSessionID == 2)
        #expect(vm.events.count == 1)
        #expect(vm.events.first?.command == "cargo build")
    }

    @Test
    func eventBrowserViewModelWithoutClientSetsError() async {
        let vm = EventBrowserViewModel(client: nil)
        await vm.loadSessions(reset: true)
        #expect(vm.errorMessage != nil)
    }

    @Test
    func eventBrowserViewModelNoSessionsClearsEvents() async throws {
        let mock = MockBrainClient(
            sessionResponse: .success(.init(sessions: [], total: 0))
        )
        let vm = EventBrowserViewModel(client: mock)
        await vm.loadSessions(reset: true)

        #expect(vm.sessions.isEmpty)
        #expect(vm.events.isEmpty)
        #expect(vm.selectedSessionID == nil)
        #expect(vm.selectedEventID == nil)
    }

    // MARK: - StatusViewModel additional coverage

    @Test
    func statusViewModelRefreshUpdatesHealthAndDaemonState() async throws {
        let mock = MockBrainClient(healthResponse: .success(.preview))
        let vm = StatusViewModel(
            client: mock,
            daemonClient: DaemonSocketClient(socketURL: URL(fileURLWithPath: "/tmp/definitely-missing-hippo.sock"))
        )

        await vm.refresh()

        #expect(vm.health?.status == "ok")
        #expect(vm.brainReachable)
        #expect(vm.daemonResponsive == false)
        #expect(vm.lastCheckedAt != nil)
    }

    @Test
    func statusViewModelRefreshWithHealthErrorSetsErrorAndClearsHealth() async throws {
        let mock = MockBrainClient(healthResponse: .failure(.networkError("Connection refused")))
        let vm = StatusViewModel(
            client: mock,
            daemonClient: DaemonSocketClient(socketURL: URL(fileURLWithPath: "/tmp/missing-hippo.sock"))
        )

        await vm.refresh()

        #expect(vm.health == nil)
        #expect(!vm.brainReachable)
        #expect(vm.errorMessage != nil)
        #expect(vm.lastCheckedAt != nil)
    }

    @Test
    func statusViewModelWithoutClientSetsError() async {
        let vm = StatusViewModel(client: nil)
        await vm.refresh()
        #expect(vm.errorMessage != nil)
    }

    @Test
    func statusViewModelLastCheckedDescriptionWhenNotYetChecked() {
        let vm = StatusViewModel()
        #expect(vm.lastCheckedDescription == "Not checked yet")
    }

    @Test
    func statusViewModelLastCheckedDescriptionJustChecked() async throws {
        let mock = MockBrainClient(healthResponse: .success(.preview))
        let vm = StatusViewModel(
            client: mock,
            daemonClient: DaemonSocketClient(socketURL: URL(fileURLWithPath: "/tmp/missing-hippo.sock"))
        )
        await vm.refresh()

        let description = vm.lastCheckedDescription
        #expect(description.hasPrefix("Checked"))
        #expect(description.hasSuffix("seconds ago"))
    }

    @Test
    func statusViewModelLastCheckedDescriptionMinutesAgo() {
        let vm = StatusViewModel()
        vm.lastCheckedAt = Date(timeIntervalSinceNow: -180) // 3 minutes ago

        let description = vm.lastCheckedDescription
        #expect(description.contains("3 minutes ago"))
    }

    @Test
    func statusViewModelLastCheckedDescriptionHoursAgo() {
        let vm = StatusViewModel()
        vm.lastCheckedAt = Date(timeIntervalSinceNow: -7200) // 2 hours ago

        let description = vm.lastCheckedDescription
        #expect(description.contains("2 hours ago"))
    }

    @Test
    func statusViewModelBrainReachableIsFalseWhenHealthIsNil() {
        let vm = StatusViewModel()
        #expect(!vm.brainReachable)
    }
}
