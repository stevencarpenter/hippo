import SwiftUI

struct StatusView: View {
    @Environment(\.brainClient) private var brainClient
    @State private var vm = StatusViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Status")
                .font(.title)
                .fontWeight(.bold)

            if let error = vm.errorMessage {
                ErrorBannerView(message: error) {
                    await vm.refresh()
                }
            }

            HStack(spacing: 16) {
                statusCard(
                    title: "Daemon Socket",
                    isHealthy: vm.daemonResponsive,
                    subtitle: vm.daemonResponsive ? "Responding" : "Not reachable"
                )

                statusCard(
                    title: "Brain HTTP",
                    isHealthy: vm.brainReachable,
                    subtitle: vm.brainReachable ? "Responding" : "Not responding"
                )
            }

            if let health = vm.health {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Queue Summary")
                        .font(.headline)

                    Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 8) {
                        GridRow {
                            Text("Shell")
                            Text("\(health.queueDepth) pending")
                            Text("\(health.queueFailed) failed")
                        }
                        GridRow {
                            Text("Claude")
                            Text("\(health.claudeQueueDepth) pending")
                            Text("\(health.claudeQueueFailed) failed")
                        }
                        GridRow {
                            Text("Browser")
                            Text("\(health.browserQueueDepth) pending")
                            Text("\(health.browserQueueFailed) failed")
                        }
                        GridRow {
                            Text("Workflow")
                            Text("\(health.workflowQueueDepth) pending")
                            Text("\(health.workflowQueueFailed) failed")
                        }
                        GridRow {
                            Text("Total")
                                .fontWeight(.semibold)
                            Text("\(health.totalPendingQueueDepth) pending")
                                .fontWeight(.semibold)
                            Text("\(health.totalFailedQueueDepth) failed")
                                .fontWeight(.semibold)
                        }
                    }
                }
                .padding()
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))

                VStack(alignment: .leading, spacing: 8) {
                    Text("Last checked")
                        .font(.headline)
                    Text(vm.lastCheckedDescription)
                        .foregroundStyle(.secondary)

                    if let version = health.version {
                        LabeledContent("Version") {
                            Text(version)
                        }
                    }

                    if let model = health.enrichmentModel, !model.isEmpty {
                        LabeledContent("Enrichment Model") {
                            Text(model)
                        }
                    }

                    if let lastError = health.lastError, !lastError.isEmpty {
                        LabeledContent("Last Error") {
                            Text(lastError)
                                .multilineTextAlignment(.trailing)
                        }
                    }
                }
                .padding()
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }

            Button {
                Task { await vm.refresh() }
            } label: {
                HStack {
                    if vm.isLoading {
                        ProgressView()
                            .controlSize(.small)
                    }
                    Image(systemName: "arrow.clockwise")
                    Text("Refresh")
                }
            }
            .disabled(vm.isLoading)

            Spacer()
        }
        .padding()
        .task {
            vm.configure(client: brainClient)
            await vm.autoRefresh()
        }
    }

    private func statusCard(title: String, isHealthy: Bool, subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(title)
                    .font(.headline)
                Spacer()
                Circle()
                    .fill(isHealthy ? Color.green : Color.red)
                    .frame(width: 12, height: 12)
            }

            Text(subtitle)
                .foregroundStyle(.secondary)
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

#Preview {
    StatusView()
        .brainClient(MockBrainClient(healthResponse: .success(.preview)))
}