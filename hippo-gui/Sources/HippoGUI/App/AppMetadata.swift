import Foundation

struct AppMetadata: Sendable {
    let displayName: String
    let bundleIdentifier: String
    let marketingVersion: String
    let buildNumber: String

    init(bundle: Bundle = .main, infoDictionary: [String: Any]? = nil) {
        let isExplicitInfo = infoDictionary != nil
        let info = infoDictionary ?? bundle.infoDictionary ?? [:]

        displayName = Self.stringValue(for: ["CFBundleDisplayName", "CFBundleName"], in: info) ?? "HippoGUI"
        bundleIdentifier = Self.stringValue(for: ["CFBundleIdentifier"], in: info)
            ?? (isExplicitInfo ? nil : bundle.bundleIdentifier)
            ?? "development"
        marketingVersion = Self.stringValue(for: ["CFBundleShortVersionString"], in: info) ?? "Development"
        buildNumber = Self.stringValue(for: ["CFBundleVersion"], in: info) ?? "Unversioned"
    }

    var versionDescription: String {
        if marketingVersion == "Development" {
            return "Development Build"
        }

        if buildNumber == "Unversioned" {
            return "Version \(marketingVersion)"
        }

        return "Version \(marketingVersion) (\(buildNumber))"
    }

    var isReleaseStamped: Bool {
        marketingVersion != "Development" && buildNumber != "Unversioned"
    }

    private static func stringValue(for keys: [String], in info: [String: Any]) -> String? {
        for key in keys {
            if let value = info[key] as? String {
                let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty {
                    return trimmed
                }
            }
        }

        return nil
    }
}
