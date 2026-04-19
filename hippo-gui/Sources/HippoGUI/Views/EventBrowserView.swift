import SwiftUI

struct EventBrowserView: View {
    @Environment(\.brainClient) private var brainClient
    @State private var vm = EventBrowserViewModel()

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Sessions")
                        .font(.title)
                        .fontWeight(.bold)

                    Spacer()

                    Picker("Since", selection: $vm.sincePreset) {
                        ForEach(TimeFilterPreset.allCases) { preset in
                            Text(preset.title).tag(preset)
                        }
                    }
                    .pickerStyle(.menu)
                    .frame(width: 120)

                    Button {
                        Task { await vm.refresh() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(vm.isLoadingSessions)
                }

                if vm.isLoadingSessions {
                    HStack {
                        ProgressView()
                            .scaleEffect(0.8)
                        Text("Loading sessions...")
                            .foregroundStyle(.secondary)
                    }
                }

                if let error = vm.errorMessage {
                    ErrorBannerView(message: error) {
                        await vm.refresh()
                    }
                }

                List(selection: Binding(get: { vm.selectedSessionID }, set: { newValue in
                    Task { await vm.selectSession(id: newValue) }
                })) {
                    ForEach(vm.sessions) { session in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(formattedDate(session.startTime))
                                .font(.body)
                            HStack {
                                Text(session.hostname)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Text("\(session.eventCount) events")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .tag(Optional(session.id))
                    }

                    if vm.canLoadMoreSessions {
                        Button {
                            Task { await vm.loadMoreSessions() }
                        } label: {
                            HStack {
                                Spacer()
                                if vm.isLoadingSessions {
                                    ProgressView()
                                        .controlSize(.small)
                                } else {
                                    Text("Load More Sessions")
                                }
                                Spacer()
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
                .listStyle(.inset)
            }
            .frame(minWidth: 220)

            if let session = vm.selectedSession {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("Events")
                            .font(.headline)
                        Spacer()
                        Text("Shell: \(session.shell)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    HStack {
                        TextField("Project filter", text: $vm.project)
                            .textFieldStyle(.roundedBorder)
                            .onSubmit {
                                Task { await vm.loadEvents(reset: true) }
                            }

                        Button("Apply") {
                            Task { await vm.loadEvents(reset: true) }
                        }
                        .disabled(vm.isLoadingEvents)
                    }

                    if vm.isLoadingEvents {
                        HStack {
                            ProgressView()
                                .scaleEffect(0.8)
                            Text("Loading events...")
                                .foregroundStyle(.secondary)
                        }
                    }

                    List(selection: Binding(get: { vm.selectedEventID }, set: { vm.selectedEventID = $0 })) {
                        ForEach(vm.filteredEvents) { event in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(event.command)
                                    .lineLimit(1)
                                    .font(.system(.body, design: .monospaced))
                                    .truncationMode(.middle)
                                HStack {
                                    if let exitCode = event.exitCode {
                                        Text("exit: \(exitCode)")
                                            .font(.caption2)
                                            .foregroundStyle(exitCode == 0 ? .green : .red)
                                    }
                                    Text("\(event.durationMs)ms")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                    Spacer()
                                    Text(formattedTime(event.timestamp))
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            .tag(Optional(event.id))
                        }

                        if vm.canLoadMoreEvents {
                            Button {
                                Task { await vm.loadMoreEvents() }
                            } label: {
                                HStack {
                                    Spacer()
                                    if vm.isLoadingEvents {
                                        ProgressView()
                                            .controlSize(.small)
                                    } else {
                                        Text("Load More Events")
                                    }
                                    Spacer()
                                }
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .listStyle(.inset)
                    .searchable(text: $vm.commandFilter, prompt: "Filter commands")
                }
                .frame(minWidth: 320)

                if let event = vm.selectedEvent {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text("Event Details")
                                .font(.headline)
                            Spacer()
                            Text("ID: \(event.id)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }

                        ScrollView {
                            VStack(alignment: .leading, spacing: 12) {
                                LabeledContent("Session") {
                                    Text("\(event.sessionId)")
                                }

                                LabeledContent("Timestamp") {
                                    Text(formattedDate(event.timestamp))
                                }

                                LabeledContent("Working Directory") {
                                    Text(event.cwd)
                                        .font(.caption)
                                        .textSelection(.enabled)
                                }

                                if let branch = event.gitBranch, !branch.isEmpty {
                                    LabeledContent("Git Branch") {
                                        Text(branch)
                                    }
                                }

                                LabeledContent("Duration") {
                                    Text("\(event.durationMs)ms")
                                }

                                if let exitCode = event.exitCode {
                                    LabeledContent("Exit Code") {
                                        Text("\(exitCode)")
                                            .foregroundStyle(exitCode == 0 ? .green : .red)
                                    }
                                }

                                Divider()

                                Text("Command")
                                    .font(.headline)

                                Text(event.command)
                                    .font(.system(.body, design: .monospaced))
                                    .textSelection(.enabled)
                                    .padding(8)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                            }
                        }

                        Spacer()
                    }
                    .padding()
                    .frame(minWidth: 320)
                } else {
                    ContentUnavailableView("Select an Event", systemImage: "terminal", description: Text("Choose an event to inspect the full command details."))
                }
            } else {
                ContentUnavailableView("Select a Session", systemImage: "rectangle.stack.person.crop", description: Text("Choose a captured session to browse its events."))
            }
        }
        .onChange(of: vm.sincePreset) { _, _ in
            Task { await vm.refresh() }
        }
        .task {
            vm.configure(client: brainClient)
            await vm.refresh()
        }
    }

    private func formattedDate(_ timestamp: Int) -> String {
        Date(timeIntervalSince1970: Double(timestamp) / 1000)
            .formatted(date: .abbreviated, time: .shortened)
    }

    private func formattedTime(_ timestamp: Int) -> String {
        Date(timeIntervalSince1970: Double(timestamp) / 1000)
            .formatted(date: .omitted, time: .shortened)
    }
}

#if DEBUG
#Preview {
    let sessions = SessionListResponse(
        sessions: [
            Session(id: 1, startTime: 1_713_404_800_000, hostname: "laptop", shell: "zsh", eventCount: 2)
        ],
        total: 1
    )
    let events = EventListResponse(
        events: [
            Event(id: 1, sessionId: 1, timestamp: 1_713_404_800_000, command: "swift test", exitCode: 0, durationMs: 820, cwd: "/Users/carpenter/projects/hippo", gitBranch: "main"),
            Event(id: 2, sessionId: 1, timestamp: 1_713_404_860_000, command: "swift build", exitCode: 0, durationMs: 420, cwd: "/Users/carpenter/projects/hippo", gitBranch: "main")
        ],
        total: 2
    )

    EventBrowserView()
        .brainClient(
            PreviewBrainClient(
                eventResponse: .success(events),
                sessionResponse: .success(sessions)
            )
        )
}
#endif
