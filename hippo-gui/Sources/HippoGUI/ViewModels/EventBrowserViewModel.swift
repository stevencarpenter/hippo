import Observation
import Foundation

@MainActor
@Observable
final class EventBrowserViewModel {
    static let pageSize = 20

    var sessions: [Session] = []
    var events: [Event] = []
    var selectedSessionID: Int?
    var selectedEventID: Int?
    var isLoadingSessions = false
    var isLoadingEvents = false
    var errorMessage: String?
    var sincePreset: TimeFilterPreset = .all
    var project = ""
    var commandFilter = ""
    var sessionOffset = 0
    var sessionTotal = 0
    var eventOffset = 0
    var eventTotal = 0

    @ObservationIgnored private var client: (any BrainClientProtocol)?

    init(client: (any BrainClientProtocol)? = nil) {
        self.client = client
    }

    func configure(client: any BrainClientProtocol) {
        self.client = client
    }

    var selectedSession: Session? {
        sessions.first(where: { $0.id == selectedSessionID })
    }

    var selectedEvent: Event? {
        filteredEvents.first(where: { $0.id == selectedEventID })
            ?? events.first(where: { $0.id == selectedEventID })
    }

    var filteredEvents: [Event] {
        guard !commandFilter.isEmpty else {
            return events
        }

        let needle = commandFilter.localizedLowercase
        return events.filter { $0.command.localizedLowercase.contains(needle) }
    }

    var canLoadMoreSessions: Bool {
        sessions.count < sessionTotal
    }

    var canLoadMoreEvents: Bool {
        events.count < eventTotal
    }

    func refresh() async {
        await loadSessions(reset: true)
    }

    func loadMoreSessions() async {
        guard canLoadMoreSessions else { return }
        await loadSessions(reset: false)
    }

    func loadMoreEvents() async {
        guard canLoadMoreEvents else { return }
        await loadEvents(reset: false)
    }

    func loadSessions(reset: Bool) async {
        guard let client else {
            errorMessage = BrainClientError.notConfigured.localizedDescription
            return
        }
        guard !isLoadingSessions else { return }

        isLoadingSessions = true
        errorMessage = nil
        if reset {
            sessionOffset = 0
        }

        defer { isLoadingSessions = false }

        do {
            let response = try await client.listSessions(
                limit: Self.pageSize,
                offset: reset ? 0 : sessionOffset,
                sinceMs: sincePreset.sinceMs
            )
            if reset {
                sessions = response.sessions
            } else {
                sessions.append(contentsOf: response.sessions.filter { incoming in
                    !sessions.contains(where: { $0.id == incoming.id })
                })
            }
            sessionTotal = response.total
            sessionOffset = sessions.count

            let sessionToSelect = selectedSessionID.flatMap { id in
                sessions.first(where: { $0.id == id })
            } ?? sessions.first

            if let sessionToSelect {
                selectedSessionID = sessionToSelect.id
                await loadEvents(reset: true)
            } else {
                selectedSessionID = nil
                events = []
                selectedEventID = nil
                eventOffset = 0
                eventTotal = 0
            }
        } catch {
            errorMessage = error.localizedDescription
            if reset {
                sessions = []
                sessionTotal = 0
                sessionOffset = 0
            }
        }
    }

    func selectSession(id: Int?) async {
        selectedSessionID = id
        selectedEventID = nil
        await loadEvents(reset: true)
    }

    func loadEvents(reset: Bool) async {
        guard let client else {
            errorMessage = BrainClientError.notConfigured.localizedDescription
            return
        }
        guard let selectedSessionID else {
            events = []
            eventOffset = 0
            eventTotal = 0
            return
        }
        guard !isLoadingEvents else { return }

        isLoadingEvents = true
        errorMessage = nil
        if reset {
            eventOffset = 0
        }

        defer { isLoadingEvents = false }

        do {
            let response = try await client.listEvents(
                limit: Self.pageSize,
                offset: reset ? 0 : eventOffset,
                sessionId: selectedSessionID,
                sinceMs: sincePreset.sinceMs,
                project: project.isEmpty ? nil : project
            )
            if reset {
                events = response.events
            } else {
                events.append(contentsOf: response.events.filter { incoming in
                    !events.contains(where: { $0.id == incoming.id })
                })
            }
            eventTotal = response.total
            eventOffset = events.count
            if let selectedEventID, events.contains(where: { $0.id == selectedEventID }) {
                self.selectedEventID = selectedEventID
            } else {
                self.selectedEventID = events.first?.id
            }
        } catch {
            errorMessage = error.localizedDescription
            if reset {
                events = []
                eventOffset = 0
                eventTotal = 0
                selectedEventID = nil
            }
        }
    }
}
