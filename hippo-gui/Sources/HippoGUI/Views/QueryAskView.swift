import SwiftUI

struct QueryAskView: View {
    @Environment(\.brainClient) private var brainClient
    @State private var viewModel = QueryViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(viewModel.promptTitle)
                .font(.title)
                .fontWeight(.bold)

            Picker("Mode", selection: $viewModel.mode) {
                Text("Ask").tag(QueryMode.ask)
                Text("Search").tag(QueryMode.search)
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 240)

            HStack {
                TextField(viewModel.promptPlaceholder, text: $viewModel.queryText)
                    .textFieldStyle(.roundedBorder)
                    .disabled(viewModel.isLoading)
                    .onSubmit {
                        Task { await viewModel.submit() }
                    }

                Stepper(value: $viewModel.limit, in: 1...25) {
                    Text("Top \(viewModel.limit)")
                }
                .frame(maxWidth: 120)

                Button(viewModel.buttonTitle) {
                    Task { await viewModel.submit() }
                }
                .disabled(
                    viewModel.queryText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || viewModel.isLoading
                )
                .keyboardShortcut(.defaultAction)
            }

            if viewModel.isLoading {
                HStack {
                    ProgressView()
                        .scaleEffect(0.8)
                    Text(viewModel.mode.isSearchMode ? "Searching..." : "Thinking...")
                        .foregroundStyle(.secondary)
                }
            }

            if let error = viewModel.errorMessage {
                ErrorBannerView(message: error) {
                    await viewModel.retry()
                }
            }

            if !viewModel.answerText.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Answer")
                        .font(.headline)

                    Text(viewModel.answerText)
                        .textSelection(.enabled)
                        .padding()
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
            }

            if !viewModel.askSources.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Sources")
                        .font(.headline)

                    ForEach(viewModel.askSources) { source in
                        sourceCard(summary: source.summary, score: source.score, detail: source.cwd)
                    }
                }
            }

            if let searchResponse = viewModel.searchResponse {
                if let warning = searchResponse.warning, !warning.isEmpty {
                    Text(warning)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if !searchResponse.results.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(searchResponse.mode == .lexical ? "Results" : "Search Results")
                            .font(.headline)

                        ForEach(searchResponse.results) { result in
                            sourceCard(
                                summary: result.summary,
                                score: result.score,
                                detail: [result.cwd, result.gitBranch].compactMap { $0 }.joined(separator: " • ")
                            ) {
                                if !result.tags.isEmpty {
                                    Text(result.tags.joined(separator: ", "))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                if let embedText = result.embedText, !embedText.isEmpty {
                                    Text(embedText)
                                        .font(.subheadline)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(3)
                                }
                            }
                        }
                    }
                }

                if !searchResponse.nodes.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Knowledge Matches")
                            .font(.headline)

                        ForEach(searchResponse.nodes) { node in
                            sourceCard(summary: node.content, score: nil, detail: node.uuid)
                        }
                    }
                }

                if !searchResponse.events.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Event Matches")
                            .font(.headline)

                        ForEach(searchResponse.events) { event in
                            sourceCard(summary: event.command, score: nil, detail: event.cwd)
                        }
                    }
                }

                if searchResponse.isEmpty {
                    ContentUnavailableView(
                        "No Results", systemImage: "magnifyingglass",
                        description: Text("Try broadening the search terms."))
                }
            }

            Spacer()
        }
        .padding()
        .task {
            viewModel.configure(client: brainClient)
        }
    }

    @ViewBuilder
    private func sourceCard<ExtraContent: View>(
        summary: String,
        score: Double?,
        detail: String?,
        @ViewBuilder extraContent: () -> ExtraContent = { EmptyView() }
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                Text(summary)
                    .font(.body)
                    .multilineTextAlignment(.leading)
                    .textSelection(.enabled)
                Spacer(minLength: 8)
                if let score {
                    Text(score.formatted(.number.precision(.fractionLength(2))))
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }

            if let detail, !detail.isEmpty {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .textSelection(.enabled)
            }

            extraContent()
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

#if DEBUG
#Preview {
    QueryAskView()
        .brainClient(
            PreviewBrainClient(
                askResponse: .success(
                    AskResponse(
                        answer: "You ran `cargo test` and resolved a failing snapshot.",
                        sources: [
                            AskSource(
                                summary: "Ran cargo test for hippo-core", score: 0.96,
                                cwd: "/Users/carpenter/projects/hippo")
                        ],
                        model: "preview-model",
                        error: nil,
                        degraded: false,
                        stage: nil
                    )
                ),
                queryResponse: .success(
                    QueryResponse(
                        mode: .semantic,
                        results: [
                            SemanticQueryResult(
                                score: 0.88,
                                summary: "Updated the GUI plan and added Swift 6 view models.",
                                tags: ["swift", "mvviewModel"],
                                cwd: "/Users/carpenter/projects/hippo",
                                gitBranch: "main",
                                embedText: "Refactored the macOS app to use Observation and NavigationSplitView."
                            )
                        ]
                    )
                )
            )
        )
}
#endif
