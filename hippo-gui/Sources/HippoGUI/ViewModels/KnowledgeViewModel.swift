import Observation
import Foundation

@MainActor
@Observable
final class KnowledgeViewModel {
    static let pageSize = 20
    static let nodeTypes = ["", "lesson", "pattern", "context", "command", "observation", "concept"]

    var nodes: [KnowledgeNode] = []
    var selectedNodeID: Int?
    var selectedNodeDetail: KnowledgeNode?
    var isLoadingList = false
    var isLoadingDetail = false
    var errorMessage: String?
    var nodeType = ""
    var sincePreset: TimeFilterPreset = .all
    var searchText = ""
    var offset = 0
    var total = 0

    @ObservationIgnored private var client: (any BrainClientProtocol)?

    init(client: (any BrainClientProtocol)? = nil) {
        self.client = client
    }

    func configure(client: any BrainClientProtocol) {
        self.client = client
    }

    var canLoadMore: Bool {
        nodes.count < total
    }

    var filteredNodes: [KnowledgeNode] {
        guard !searchText.isEmpty else {
            return nodes
        }

        let needle = searchText.localizedLowercase
        return nodes.filter { node in
            node.displaySummary.localizedLowercase.contains(needle)
                || node.tags.contains(where: { $0.localizedLowercase.contains(needle) })
        }
    }

    var selectedNode: KnowledgeNode? {
        selectedNodeDetail ?? nodes.first(where: { $0.id == selectedNodeID })
    }

    func refresh() async {
        await loadKnowledge(reset: true)
    }

    func loadMore() async {
        guard canLoadMore else { return }
        await loadKnowledge(reset: false)
    }

    func loadKnowledge(reset: Bool) async {
        guard let client else {
            errorMessage = BrainClientError.notConfigured.localizedDescription
            return
        }
        guard !isLoadingList else { return }

        isLoadingList = true
        errorMessage = nil
        if reset {
            offset = 0
        }

        defer { isLoadingList = false }

        do {
            let response = try await client.listKnowledge(
                limit: Self.pageSize,
                offset: reset ? 0 : offset,
                nodeType: nodeType.isEmpty ? nil : nodeType,
                sinceMs: sincePreset.sinceMs
            )

            if reset {
                nodes = response.nodes
            } else {
                nodes.append(contentsOf: response.nodes.filter { incoming in
                    !nodes.contains(where: { $0.id == incoming.id })
                })
            }

            total = response.total
            offset = nodes.count

            if let selectedNodeID, nodes.contains(where: { $0.id == selectedNodeID }) {
                await loadDetail(for: selectedNodeID)
            } else if let first = nodes.first {
                selectedNodeID = first.id
                await loadDetail(for: first.id)
            } else {
                selectedNodeID = nil
                selectedNodeDetail = nil
            }
        } catch {
            errorMessage = error.localizedDescription
            if reset {
                nodes = []
                total = 0
                offset = 0
                selectedNodeID = nil
                selectedNodeDetail = nil
            }
        }
    }

    func selectNode(id: Int?) async {
        selectedNodeID = id
        guard let id else {
            selectedNodeDetail = nil
            return
        }
        await loadDetail(for: id)
    }

    private func loadDetail(for id: Int) async {
        guard let client else {
            errorMessage = BrainClientError.notConfigured.localizedDescription
            return
        }
        guard !isLoadingDetail else { return }

        isLoadingDetail = true
        defer { isLoadingDetail = false }

        do {
            selectedNodeDetail = try await client.getKnowledge(id: id)
        } catch {
            errorMessage = error.localizedDescription
            selectedNodeDetail = nodes.first(where: { $0.id == id })
        }
    }
}
