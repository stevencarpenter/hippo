import Foundation

struct HippoConfig: Codable {
    let brain: BrainConfig

    struct BrainConfig: Codable {
        let port: Int
    }
}

struct ConfigClient: Sendable {
    static let defaultPort = 9175
    static let defaultDataDirectory = ".local/share/hippo"

    private let configPath: URL

    init() {
        let homeDir = FileManager.default.homeDirectoryForCurrentUser
        self.configPath = homeDir.appendingPathComponent(".config/hippo/config.toml")
    }

    func loadPort() -> Int {
        Int(loadValue(forKey: "port", in: "brain") ?? "") ?? Self.defaultPort
    }

    func loadQueryTimeout() -> TimeInterval {
        Double(loadValue(forKey: "query_timeout_secs", in: "brain") ?? "") ?? 300
    }

    func loadDataDirectory() -> URL {
        let homeDirectory = FileManager.default.homeDirectoryForCurrentUser
        let configuredPath = loadValue(forKey: "data_dir", in: "storage")?.trimmingCharacters(in: .whitespacesAndNewlines)
        let fallback = homeDirectory.appendingPathComponent(Self.defaultDataDirectory)

        guard let configuredPath, !configuredPath.isEmpty else {
            return fallback
        }

        if configuredPath.hasPrefix("~/") {
            return homeDirectory.appendingPathComponent(String(configuredPath.dropFirst(2)))
        }

        if configuredPath == "~" {
            return homeDirectory
        }

        return URL(fileURLWithPath: configuredPath, isDirectory: true)
    }

    private func loadValue(forKey key: String, in section: String) -> String? {
        guard let content = try? String(contentsOf: configPath, encoding: .utf8) else {
            return nil
        }

        return parseValue(forKey: key, in: section, from: content)
    }

    private func parseValue(forKey key: String, in section: String, from content: String) -> String? {
        var activeSection: String?

        for line in content.split(separator: "\n", omittingEmptySubsequences: false) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            guard !trimmed.isEmpty, !trimmed.hasPrefix("#") else {
                continue
            }

            if trimmed.hasPrefix("[") && trimmed.hasSuffix("]") {
                activeSection = String(trimmed.dropFirst().dropLast())
                continue
            }

            if activeSection == section && trimmed.hasPrefix(key) && trimmed.contains("=") {
                let parts = trimmed.split(separator: "=", maxSplits: 1)
                if parts.count == 2 {
                    var value = parts[1].trimmingCharacters(in: .whitespaces)
                    if let commentRange = value.range(of: "#") {
                        value = String(value[value.startIndex..<commentRange.lowerBound])
                            .trimmingCharacters(in: .whitespaces)
                    }
                    return value.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                }
            }
        }

        return nil
    }
}
