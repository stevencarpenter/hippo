import Foundation

struct HippoConfig: Codable {
    let brain: BrainConfig

    struct BrainConfig: Codable {
        let port: Int
    }
}

actor ConfigClient {
    static let defaultPort = 9175
    private let configPath: URL

    init() {
        let homeDir = FileManager.default.homeDirectoryForCurrentUser
        self.configPath = homeDir.appendingPathComponent(".config/hippo/config.toml")
    }

    func loadPort() async -> Int {
        guard FileManager.default.fileExists(atPath: configPath.path) else {
            return Self.defaultPort
        }

        do {
            let content = try String(contentsOf: configPath, encoding: .utf8)
            return parsePort(from: content) ?? Self.defaultPort
        } catch {
            return Self.defaultPort
        }
    }

    private func parsePort(from content: String) -> Int? {
        var inBrainSection = false

        for line in content.split(separator: "\n", omittingEmptySubsequences: false) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed == "[brain]" {
                inBrainSection = true
                continue
            }

            if trimmed.hasPrefix("[") && trimmed.hasSuffix("]") {
                inBrainSection = false
                continue
            }

            if inBrainSection && trimmed.hasPrefix("port") && trimmed.contains("=") {
                let parts = trimmed.split(separator: "=", maxSplits: 1)
                if parts.count == 2 {
                    // Strip inline comments and surrounding whitespace/quotes
                    var value = parts[1].trimmingCharacters(in: .whitespaces)
                    if let commentRange = value.range(of: "#") {
                        value = String(value[value.startIndex..<commentRange.lowerBound])
                            .trimmingCharacters(in: .whitespaces)
                    }
                    value = value.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                    return Int(value)
                }
            }
        }

        return nil
    }
}
