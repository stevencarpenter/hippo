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
                .success(.init(nodes: [second], total: 2)),
            ],
            knowledgeDetails: [
                1: .success(first),
                2: .success(second),
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
}
