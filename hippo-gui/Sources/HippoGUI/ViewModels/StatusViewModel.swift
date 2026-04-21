import Foundation
import Observation

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
    @ObservationIgnored private var autoRefreshTask: Task<Void, Never>?

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

        async let daemonResponse = daemonClient.isResponsive()
        async let healthResponse = client.health()

        do {
            health = try await healthResponse
            daemonResponsive = await daemonResponse
        } catch {
            daemonResponsive = await daemonResponse
            health = nil
            errorMessage = error.localizedDescription
        }
    }

    /// Start a polling loop that refreshes every 30 seconds.
    ///
    /// Safe to call multiple times — the prior loop is cancelled before a new
    /// one starts. Call `stopAutoRefresh()` (or let the view model deinit) to
    /// stop the loop.
    func startAutoRefresh() {
        autoRefreshTask?.cancel()
        autoRefreshTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                do {
                    try await Task.sleep(for: .seconds(30))
                } catch {
                    return
                }
            }
        }
    }

    func stopAutoRefresh() {
        autoRefreshTask?.cancel()
        autoRefreshTask = nil
    }

    /// Legacy entry point — prefer `startAutoRefresh()` plus `stopAutoRefresh()`.
    /// Kept for callers that drive the lifecycle via `.task {}` (SwiftUI will
    /// cancel the enclosing Task on view disappear).
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
