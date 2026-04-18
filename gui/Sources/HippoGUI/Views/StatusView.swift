import SwiftUI

struct StatusView: View {
    let brainClient: BrainClient

    @State private var isHealthy: Bool = false
    @State private var isLoading: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Status")
                .font(.title)
                .fontWeight(.bold)

            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Brain Server")
                        .font(.headline)

                    Spacer()

                    if isLoading {
                        ProgressView()
                            .scaleEffect(0.6)
                    } else {
                        Circle()
                            .fill(isHealthy ? Color.green : Color.red)
                            .frame(width: 12, height: 12)
                    }
                }

                Text(isHealthy ? "Running" : "Not responding")
                    .foregroundStyle(.secondary)
            }
            .padding()
            .background(Color.secondary.opacity(0.1))
            .clipShape(RoundedRectangle(cornerRadius: 8))

            Button {
                Task { await checkHealth() }
            } label: {
                HStack {
                    Image(systemName: "arrow.clockwise")
                    Text("Refresh")
                }
            }
            .disabled(isLoading)

            Spacer()
        }
        .padding()
        .task {
            await checkHealth()
        }
    }

    private func checkHealth() async {
        isLoading = true

        do {
            isHealthy = try await brainClient.health()
        } catch {
            isHealthy = false
        }

        isLoading = false
    }
}