import Observation
import Foundation

@MainActor
@Observable
final class StatusViewModel {
    var health: HealthResponse?
    var daemonResponsive = false
    var isLoading = false
    var errorMessage: String?
    var lastCheckedAt: Date?

    @ObservationIgnored private var client: (any BrainClientProtocol)?
    @ObservationIgnored private let daemonClient: DaemonSocketClient

    init(
        client: (any BrainClientProtocol)? = nil,
        daemonClient: DaemonSocketClient = DaemonSocketClient()
    ) {
        self.client = client
        self.daemonClient = daemonClient
    }

    func configure(client: any BrainClientProtocol) {
        self.client = client
    }

    var brainReachable: Bool {
        health?.brainReachable ?? false
    }

    var lastCheckedDescription: String {
        guard let lastCheckedAt else {
            return "Not checked yet"
        }

        let seconds = max(Int(Date().timeIntervalSince(lastCheckedAt)), 0)
        switch seconds {
        case 0..<60:
            return "Checked \(seconds) seconds ago"
        case 60..<3600:
            return "Checked \(seconds / 60) minutes ago"
        default:
            return "Checked \(seconds / 3600) hours ago"
        }
    }

    func refresh() async {
        guard let client else {
            errorMessage = BrainClientError.notConfigured.localizedDescription
            return
        }
        guard !isLoading else { return }

        isLoading = true
        errorMessage = nil

        defer {
            isLoading = false
            lastCheckedAt = Date()
        }

        let daemonClient = self.daemonClient
        let daemonTask = Task.detached(priority: .userInitiated) {
            daemonClient.isResponsive()
        }

        do {
            async let healthResponse = client.health()
            daemonResponsive = await daemonTask.value
            health = try await healthResponse
        } catch {
            daemonResponsive = await daemonTask.value
            health = nil
            errorMessage = error.localizedDescription
        }
    }

    func autoRefresh() async {
        while !Task.isCancelled {
            await refresh()
            do {
                try await Task.sleep(for: .seconds(30))
            } catch {
                break
            }
        }
    }
}
