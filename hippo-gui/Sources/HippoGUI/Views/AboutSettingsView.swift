import AppKit
import SwiftUI

struct AboutSettingsView: View {
    private let metadata: AppMetadata

    init(metadata: AppMetadata = AppMetadata()) {
        self.metadata = metadata
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .center, spacing: 16) {
                Image(nsImage: NSApplication.shared.applicationIconImage)
                    .resizable()
                    .interpolation(.high)
                    .frame(width: 72, height: 72)
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .shadow(color: .black.opacity(0.12), radius: 8, y: 2)

                VStack(alignment: .leading, spacing: 4) {
                    Text(metadata.displayName)
                        .font(.title2.weight(.semibold))
                    Text(metadata.versionDescription)
                        .foregroundStyle(.secondary)
                    Text(metadata.bundleIdentifier)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .textSelection(.enabled)
                }
            }

            Form {
                Section("Build Information") {
                    LabeledContent("Version", value: metadata.marketingVersion)
                    LabeledContent("Build", value: metadata.buildNumber)
                    LabeledContent("Bundle ID", value: metadata.bundleIdentifier)
                }

                Section("Release Workflow") {
                    Text(
                        metadata.isReleaseStamped
                            ? "This app bundle is using stamped release metadata from the repo versioning flow."
                            : "This launch path is not using a stamped app bundle, so fallback development metadata is shown."
                    )
                    .fixedSize(horizontal: false, vertical: true)
                    .foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped)
        }
        .padding(20)
        .frame(minWidth: 420, idealWidth: 460, minHeight: 280)
    }
}

#Preview {
    AboutSettingsView(
        metadata: AppMetadata(
            infoDictionary: [
                "CFBundleDisplayName": "HippoGUI",
                "CFBundleIdentifier": "com.hippo.HippoGUI",
                "CFBundleShortVersionString": "0.11.0",
                "CFBundleVersion": "189",
            ]
        )
    )
}
