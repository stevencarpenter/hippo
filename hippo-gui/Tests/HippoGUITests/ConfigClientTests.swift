import Foundation
import Testing
@testable import HippoGUIKit

struct ConfigClientTests {
    @Test
    func portIsReadFromBrainSection() throws {
        let path = try writeConfig(
            """
            [brain]
            port = 9999
            """
        )
        #expect(ConfigClient(configPath: path).loadPort() == 9999)
    }

    @Test
    func portIsNotConfusedWithSimilarlyPrefixedKey() throws {
        // Regression: parseValue previously matched any key starting with
        // the requested prefix, so `port_override = 1234` returned for
        // `port`. The fix requires exact-key match.
        let path = try writeConfig(
            """
            [brain]
            port_override = 1234
            port = 4242
            """
        )
        #expect(ConfigClient(configPath: path).loadPort() == 4242)
    }

    @Test
    func portFallsBackToDefaultWhenOnlyPrefixedKeyExists() throws {
        // Only a similarly-prefixed key exists. With the old prefix-only
        // match, this would silently return 1234. The fix returns the
        // default port.
        let path = try writeConfig(
            """
            [brain]
            port_override = 1234
            """
        )
        #expect(ConfigClient(configPath: path).loadPort() == ConfigClient.defaultPort)
    }

    @Test
    func keyMatchHonorsSection() throws {
        let path = try writeConfig(
            """
            [other]
            port = 8888

            [brain]
            port = 9001
            """
        )
        #expect(ConfigClient(configPath: path).loadPort() == 9001)
    }

    @Test
    func commentsAndBlankLinesAreSkipped() throws {
        let path = try writeConfig(
            """
            # top comment

            [brain]
            # commented = 1
            port = 9175
            """
        )
        #expect(ConfigClient(configPath: path).loadPort() == 9175)
    }

    @Test
    func queryTimeoutIsParsed() throws {
        let path = try writeConfig(
            """
            [brain]
            query_timeout_secs = 120
            """
        )
        #expect(ConfigClient(configPath: path).loadQueryTimeout() == 120)
    }

    @Test
    func queryTimeoutIgnoresPrefixedKey() throws {
        let path = try writeConfig(
            """
            [brain]
            query_timeout_secs_override = 999
            """
        )
        // No exact `query_timeout_secs` key: must use the default (300),
        // not the prefix-matched 999.
        #expect(ConfigClient(configPath: path).loadQueryTimeout() == 300)
    }

    private func writeConfig(_ content: String) throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("hippo-config-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let path = dir.appendingPathComponent("config.toml")
        try content.write(to: path, atomically: true, encoding: .utf8)
        return path
    }
}
