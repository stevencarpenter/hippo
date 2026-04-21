import SwiftUI

struct KnowledgeView: View {
    @Environment(\.brainClient) private var brainClient
    @State private var viewModel = KnowledgeViewModel()

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Knowledge Nodes")
                        .font(.title)
                        .fontWeight(.bold)

                    Spacer()

                    Picker("Type", selection: $viewModel.nodeType) {
                        Text("All").tag("")
                        ForEach(KnowledgeViewModel.nodeTypes.dropFirst(), id: \.self) { type in
                            Text(type).tag(type)
                        }
                    }
                    .pickerStyle(.menu)
                    .frame(width: 120)

                    Picker("Since", selection: $viewModel.sincePreset) {
                        ForEach(TimeFilterPreset.allCases) { preset in
                            Text(preset.title).tag(preset)
                        }
                    }
                    .pickerStyle(.menu)
                    .frame(width: 120)

                    Button {
                        Task { await viewModel.refresh() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(viewModel.isLoadingList)
                }

                if viewModel.isLoadingList {
                    HStack {
                        ProgressView()
                            .scaleEffect(0.8)
                        Text("Loading...")
                            .foregroundStyle(.secondary)
                    }
                }

                if let error = viewModel.errorMessage {
                    ErrorBannerView(message: error) {
                        await viewModel.refresh()
                    }
                }

                List(
                    selection: Binding(
                        get: { viewModel.selectedNodeID },
                        set: { newValue in
                            Task { await viewModel.selectNode(id: newValue) }
                        })
                ) {
                    ForEach(viewModel.filteredNodes) { node in
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
                        .tag(Optional(node.id))
                    }

                    if viewModel.canLoadMore {
                        Button {
                            Task { await viewModel.loadMore() }
                        } label: {
                            HStack {
                                Spacer()
                                if viewModel.isLoadingList {
                                    ProgressView()
                                        .controlSize(.small)
                                } else {
                                    Text("Load More")
                                }
                                Spacer()
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
                .listStyle(.inset)
                .searchable(text: $viewModel.searchText, prompt: "Search summaries or tags")
            }
            .frame(minWidth: 250)

            if let node = viewModel.selectedNode {
                let parsed = ParsedKnowledgeContent(raw: node.content)
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
                            if viewModel.isLoadingDetail {
                                ProgressView("Loading details…")
                            }

                            LabeledContent("Type") {
                                Text(node.nodeType)
                            }

                            LabeledContent("UUID") {
                                Text(node.uuid)
                                    .font(.caption)
                                    .textSelection(.enabled)
                            }

                            LabeledContent("Created") {
                                Text(formattedDate(node.createdAt))
                            }

                            if let summary = parsed.summary {
                                LabeledContent("Summary") {
                                    Text(summary)
                                }
                            }

                            if let outcome = parsed.outcome ?? node.outcome, !outcome.isEmpty {
                                LabeledContent("Outcome") {
                                    Text(outcome)
                                }
                            }

                            let renderedTags = parsed.tags.isEmpty ? node.tags : parsed.tags
                            if !renderedTags.isEmpty {
                                LabeledContent("Tags") {
                                    Text(renderedTags.joined(separator: ", "))
                                }
                            }

                            ForEach(parsed.additionalRows, id: \.title) { row in
                                LabeledContent(row.title) {
                                    Text(row.value)
                                        .multilineTextAlignment(.trailing)
                                }
                            }

                            if let embedText = node.embedText, !embedText.isEmpty {
                                LabeledContent("Embed Text") {
                                    Text(embedText)
                                        .textSelection(.enabled)
                                }
                            }

                            if !node.relatedEntities.isEmpty {
                                Divider()
                                Text("Related Entities")
                                    .font(.headline)

                                ForEach(node.relatedEntities) { entity in
                                    LabeledContent(entity.name) {
                                        Text(entity.type)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                            }

                            if !node.relatedEvents.isEmpty {
                                Divider()
                                Text("Related Events")
                                    .font(.headline)

                                ForEach(node.relatedEvents) { event in
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text("#\(event.id)")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                        Text(event.command)
                                            .font(.system(.body, design: .monospaced))
                                            .textSelection(.enabled)
                                    }
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(10)
                                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                                }
                            }

                            Divider()

                            Text("Raw Content")
                                .font(.headline)

                            DisclosureGroup("Show JSON") {
                                Text(parsed.prettyPrintedRaw)
                                    .font(.system(.body, design: .monospaced))
                                    .textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.top, 4)
                            }
                        }
                    }

                    Spacer()
                }
                .padding()
                .frame(minWidth: 300)
            } else {
                ContentUnavailableView(
                    "Select a Knowledge Node", systemImage: "brain.head.profile",
                    description: Text("Choose an item from the list to inspect its details."))
            }
        }
        .onChange(of: viewModel.nodeType) { _, _ in
            Task { await viewModel.refresh() }
        }
        .onChange(of: viewModel.sincePreset) { _, _ in
            Task { await viewModel.refresh() }
        }
        .task {
            viewModel.configure(client: brainClient)
            await viewModel.refresh()
        }
    }

    private func formattedDate(_ timestamp: Int) -> String {
        Date(timeIntervalSince1970: Double(timestamp) / 1000)
            .formatted(date: .abbreviated, time: .shortened)
    }
}

private struct ParsedKnowledgeContent {
    struct Row: Hashable {
        let title: String
        let value: String
    }

    let summary: String?
    let outcome: String?
    let tags: [String]
    let additionalRows: [Row]
    let prettyPrintedRaw: String

    init(raw: String) {
        guard let data = raw.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data),
            let dictionary = object as? [String: Any]
        else {
            summary = nil
            outcome = nil
            tags = []
            additionalRows = []
            prettyPrintedRaw = raw
            return
        }

        summary = dictionary["summary"] as? String
        outcome = dictionary["outcome"] as? String
        tags = (dictionary["tags"] as? [String]) ?? []

        additionalRows =
            dictionary
            .filter { key, _ in !["summary", "outcome", "tags"].contains(key) }
            .sorted { $0.key < $1.key }
            .map { key, value in
                Row(title: key.replacingOccurrences(of: "_", with: " ").capitalized, value: Self.render(value))
            }

        if let prettyData = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted]),
            let prettyString = String(data: prettyData, encoding: .utf8) {
            prettyPrintedRaw = prettyString
        } else {
            prettyPrintedRaw = raw
        }
    1s1s0vi

    private static func render(_ value: Any) -> String {
        switch value {
        case let string as String:
            return string
        case let number as NSNumber:
            return number.stringValue
        case let strings as [String]:
            return strings.joined(separator: ", ")
        case let array as [Any]:
            return array.map(render).joined(separator: ", ")
        case let dict as [String: Any]:
            return
                dict
                .sorted { $0.key < $1.key }
                .map { "\($0.key): \(render($0.value))" }
                .joined(separator: "; ")
        default:
            return String(describing: value)
        }
    }
}

#if DEBUG
private func previewKnowledgeNode() -> KnowledgeNode {
    let json = """
        {
            "id": 1,
            "uuid": "preview-node",
            "content": "{\\"summary\\":\\"Preview summary\\",\\"tags\\":[\\"swift\\",\\"preview\\"],\\"key_decisions\\":[\\"Used NavigationSplitView\\"]}",
            "embed_text": "Preview embed text",
            "node_type": "observation",
            "outcome": "success",
            "tags": ["swift"],
            "created_at": 1713404800000,
            "related_entities": [{"id": 1, "name": "swift", "type": "tool"}],
            "related_events": [{"id": 42, "command": "swift test"}]
        }
        """
    do {
        return try JSONDecoder().decode(KnowledgeNode.self, from: Data(json.utf8))
    } catch {
        // SwiftUI previews don't render anything if this fails — surface the
        // decoding error visibly rather than silently rendering an empty list.
        fatalError("preview knowledge node failed to decode: \(error)")
    }
}

#Preview {
    let nodeList = KnowledgeListResponse(
        nodes: [previewKnowledgeNode()],
        total: 1
    )

    KnowledgeView()
        .brainClient(
            PreviewBrainClient(
                knowledgeResponse: .success(nodeList),
                knowledgeDetails: [1: .success(nodeList.nodes[0])]
            )
        )
}
#endif
