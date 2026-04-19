import SwiftUI

struct ErrorBannerView: View {
    let message: String
    let retryTitle: String
    let retryAction: @MainActor () async -> Void

    init(message: String, retryTitle: String = "Retry", retryAction: @escaping @MainActor () async -> Void) {
        self.message = message
        self.retryTitle = retryTitle
        self.retryAction = retryAction
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)

            VStack(alignment: .leading, spacing: 8) {
                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(.primary)
                    .textSelection(.enabled)

                Button(retryTitle) {
                    Task { await retryAction() }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
            }

            Spacer(minLength: 0)
        }
        .padding(12)
        .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

#Preview {
    ErrorBannerView(message: "Could not load knowledge nodes.") {}
        .padding()
}
