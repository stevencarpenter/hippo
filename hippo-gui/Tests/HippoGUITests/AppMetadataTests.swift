import Foundation
import Testing
@testable import HippoGUIKit

struct AppMetadataTests {
    @Test
    func metadataUsesStampedBundleValues() {
        let metadata = AppMetadata(
            infoDictionary: [
                "CFBundleDisplayName": "HippoGUI",
                "CFBundleIdentifier": "com.hippo.HippoGUI",
                "CFBundleShortVersionString": "0.11.0",
                "CFBundleVersion": "189",
            ]
        )

        #expect(metadata.displayName == "HippoGUI")
        #expect(metadata.bundleIdentifier == "com.hippo.HippoGUI")
        #expect(metadata.marketingVersion == "0.11.0")
        #expect(metadata.buildNumber == "189")
        #expect(metadata.versionDescription == "Version 0.11.0 (189)")
        #expect(metadata.isReleaseStamped)
    }

    @Test
    func metadataFallsBackForDevelopmentRuns() {
        let metadata = AppMetadata(infoDictionary: [:])

        #expect(metadata.displayName == "HippoGUI")
        #expect(metadata.bundleIdentifier == "development")
        #expect(metadata.marketingVersion == "Development")
        #expect(metadata.buildNumber == "Unversioned")
        #expect(metadata.versionDescription == "Development Build")
        #expect(metadata.isReleaseStamped == false)
    }
}
