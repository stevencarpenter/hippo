import SwiftUI

struct EventBrowserView: View {
    let brainClient: BrainClient

    @State private var sessions: [Session] = []
    @State private var events: [Event] = []
    @State private var selectedSession: Session?
    @State private var selectedEvent: Event?
    @State private var isLoading: Bool = false
    @State private var errorMessage: String?

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Sessions")
                        .font(.title)
                        .fontWeight(.bold)

                    Spacer()

                    Button {
                        Task { await loadSessions() }
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

                List(selection: $selectedSession) {
                    ForEach(sessions) { session in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(formatSessionDate(session.startTime))
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
                        .tag(session)
                    }
                }
                .listStyle(.inset)
                .onChange(of: selectedSession) { _, newSession in
                    if let session = newSession {
                        Task { await loadEvents(sessionId: session.id) }
                    }
                }
            }
            .frame(minWidth: 200)

            if let session = selectedSession {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("Events")
                            .font(.headline)
                        Spacer()
                        Text("Shell: \(session.shell)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    List(selection: $selectedEvent) {
                        ForEach(events) { event in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(event.command.prefix(80))
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
                                    Text(formatTimestamp(event.timestamp))
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            .tag(event)
                        }
                    }
                    .listStyle(.inset)
                }
                .frame(minWidth: 300)

                if let event = selectedEvent {
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
                                    Text(formatTimestamp(event.timestamp))
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
                                    .background(Color.secondary.opacity(0.1))
                                    .clipShape(RoundedRectangle(cornerRadius: 4))
                            }
                        }

                        Spacer()
                    }
                    .padding()
                    .frame(minWidth: 300)
                }
            }
        }
        .task {
            await loadSessions()
        }
    }

    @MainActor
    private func loadSessions() async {
        isLoading = true
        errorMessage = nil

        do {
            let response = try await brainClient.listSessions()
            sessions = response.sessions
            selectedSession = sessions.first
            // selectedSession change triggers loadEvents via .onChange
        } catch {
            errorMessage = error.localizedDescription
        }

        isLoading = false
    }

    @MainActor
    private func loadEvents(sessionId: Int) async {
        selectedEvent = nil
        do {
            let response = try await brainClient.listEvents(sessionId: sessionId)
            events = response.events
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private static let sessionDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        return f
    }()

    private static let timestampFormatter: DateFormatter = {
        let f = DateFormatter()
        f.timeStyle = .medium
        return f
    }()

    private func formatSessionDate(_ timestamp: Int) -> String {
        Self.sessionDateFormatter.string(from: Date(timeIntervalSince1970: Double(timestamp) / 1000))
    }

    private func formatTimestamp(_ timestamp: Int) -> String {
        Self.timestampFormatter.string(from: Date(timeIntervalSince1970: Double(timestamp) / 1000))
    }
}
