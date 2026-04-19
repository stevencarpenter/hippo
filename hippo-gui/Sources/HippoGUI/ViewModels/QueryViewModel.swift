import Observation
import Foundation

@MainActor
@Observable
final class QueryViewModel {
    var mode: QueryMode = .ask
    var queryText = ""
    var limit = 10
    var answerText = ""
    var askSources: [AskSource] = []
    var searchResponse: QueryResponse?
    var errorMessage: String?
    var isLoading = false

    @ObservationIgnored private var client: (any BrainClientProtocol)?

    init(client: (any BrainClientProtocol)? = nil) {
        self.client = client
    }

    func configure(client: any BrainClientProtocol) {
        self.client = client
    }

    var buttonTitle: String {
        mode.isSearchMode ? "Search" : "Ask"
    }

    var promptTitle: String {
        mode.isSearchMode ? "Search Hippo" : "Ask Hippo"
    }

    var promptPlaceholder: String {
        mode.isSearchMode ? "Search your knowledge..." : "Ask a question..."
    }

    var hasResults: Bool {
        !answerText.isEmpty || !askSources.isEmpty || searchResponse?.isEmpty == false
    }

    func retry() async {
        await submit()
    }

    func clearResults() {
        answerText = ""
        askSources = []
        searchResponse = nil
        errorMessage = nil
    }

    func submit() async {
        guard !queryText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return
        }
        guard !isLoading else {
            return
        }
        guard let client else {
            errorMessage = BrainClientError.notConfigured.localizedDescription
            return
        }

        isLoading = true
        errorMessage = nil
        answerText = ""
        askSources = []
        searchResponse = nil

        defer { isLoading = false }

        do {
            if mode.isSearchMode {
                searchResponse = try await client.query(queryText, limit: max(limit, 1))
            } else {
                let response = try await client.ask(question: queryText, limit: max(limit, 1))
                if let error = response.error, !error.isEmpty {
                    errorMessage = error
                } else {
                    answerText = response.answer ?? "No answer available."
                    askSources = response.sources ?? []
                }
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
