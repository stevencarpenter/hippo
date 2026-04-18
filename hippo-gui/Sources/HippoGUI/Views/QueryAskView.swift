import SwiftUI

struct QueryAskView: View {
    let brainClient: BrainClient

    @State private var question: String = ""
    @State private var answer: String = ""
    @State private var sources: [AskSource] = []
    @State private var isLoading: Bool = false
    @State private var errorMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Ask Hippo")
                .font(.title)
                .fontWeight(.bold)

            HStack {
                TextField("Ask a question...", text: $question)
                    .textFieldStyle(.roundedBorder)
                    .disabled(isLoading)
                    .onSubmit {
                        Task { await askQuestion() }
                    }

                Button("Ask") {
                    Task { await askQuestion() }
                }
                .disabled(question.isEmpty || isLoading)
            }

            if isLoading {
                HStack {
                    ProgressView()
                        .scaleEffect(0.8)
                    Text("Thinking...")
                        .foregroundStyle(.secondary)
                }
            }

            if let error = errorMessage {
                Text(error)
                    .foregroundStyle(.red)
            }

            if !answer.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Answer")
                        .font(.headline)

                    Text(answer)
                        .padding()
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.secondary.opacity(0.1))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }

            if !sources.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Sources")
                        .font(.headline)

                    ForEach(sources) { source in
                        HStack {
                            Text(source.summary)
                                .font(.caption)
                            Spacer()
                            if let score = source.score {
                                Text(String(format: "%.2f", score))
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .padding(8)
                        .background(Color.secondary.opacity(0.05))
                        .clipShape(RoundedRectangle(cornerRadius: 4))
                    }
                }
            }

            Spacer()
        }
        .padding()
    }

    @MainActor
    private func askQuestion() async {
        guard !question.isEmpty, !isLoading else { return }

        isLoading = true
        errorMessage = nil
        answer = ""
        sources = []

        do {
            let response = try await brainClient.ask(question: question)
            if let error = response.error {
                errorMessage = error
            } else {
                answer = response.answer ?? "No answer"
                sources = response.sources ?? []
            }
        } catch {
            errorMessage = error.localizedDescription
        }

        isLoading = false
    }
}
