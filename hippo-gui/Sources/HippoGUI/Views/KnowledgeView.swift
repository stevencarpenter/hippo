import SwiftUI

struct KnowledgeView: View {
    let brainClient: BrainClient

    @State private var nodes: [KnowledgeNode] = []
    @State private var selectedNode: KnowledgeNode?
    @State private var isLoading: Bool = false
    @State private var errorMessage: String?
    @State private var selectedType: String = ""

    private let nodeTypes = ["", "lesson", "pattern", "context", "command"]

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Knowledge Nodes")
                        .font(.title)
                        .fontWeight(.bold)

                    Spacer()

                    Picker("Type", selection: $selectedType) {
                        Text("All").tag("")
                        ForEach(nodeTypes.dropFirst(), id: \.self) { type in
                            Text(type).tag(type)
                        }
                    }
                    .pickerStyle(.menu)
                    .frame(width: 100)
                    .onChange(of: selectedType) { _, _ in
                        Task { await loadKnowledge() }
                    }

                    Button {
                        Task { await loadKnowledge() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(isLoading)
                }

                if isLoading {
                    HStack {
                        ProgressView()
                            .scaleEffect(0.8)
                        Text("Loading...")
                            .foregroundStyle(.secondary)
                    }
                }

                if let error = errorMessage {
                    Text(error)
                        .foregroundStyle(.red)
                }

                List(selection: $selectedNode) {
                    ForEach(nodes) { node in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(node.displaySummary)
                                .lineLimit(2)
                                .font(.body)
                            HStack {
                                Text(node.nodeType)
                                    .font(.caption)
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 2)
                                    .background(Color.accentColor.opacity(0.2))
                                    .clipShape(RoundedRectangle(cornerRadius: 4))
                                Spacer()
                                if !node.tags.isEmpty {
                                    Text(node.tags.joined(separator: ", "))
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                        .tag(node)
                    }
                }
                .listStyle(.inset)
            }
            .frame(minWidth: 250)

            if let node = selectedNode {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("Node Details")
                            .font(.headline)
                        Spacer()
                        Text("ID: \(node.id)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    ScrollView {
                        VStack(alignment: .leading, spacing: 12) {
                            LabeledContent("Type") {
                                Text(node.nodeType)
                            }

                            LabeledContent("UUID") {
                                Text(node.uuid)
                                    .font(.caption)
                                    .textSelection(.enabled)
                            }

                            LabeledContent("Created") {
                                Text(formatDate(node.createdAt))
                            }

                            if let outcome = node.outcome, !outcome.isEmpty {
                                LabeledContent("Outcome") {
                                    Text(outcome)
                                }
                            }

                            if !node.tags.isEmpty {
                                LabeledContent("Tags") {
                                    Text(node.tags.joined(separator: ", "))
                                }
                            }

                            Divider()

                            Text("Content")
                                .font(.headline)

                            Text(node.content)
                                .font(.body)
                                .textSelection(.enabled)
                        }
                    }

                    Spacer()
                }
                .padding()
                .frame(minWidth: 300)
            }
        }
        .task {
            await loadKnowledge()
        }
    }

    @MainActor
    private func loadKnowledge() async {
        isLoading = true
        errorMessage = nil

        do {
            let nodeType = selectedType.isEmpty ? nil : selectedType
            let response = try await brainClient.listKnowledge(nodeType: nodeType)
            nodes = response.nodes
        } catch {
            errorMessage = error.localizedDescription
        }

        isLoading = false
    }

    private static let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        return f
    }()

    private func formatDate(_ timestamp: Int) -> String {
        Self.dateFormatter.string(from: Date(timeIntervalSince1970: Double(timestamp) / 1000))
    }
}
