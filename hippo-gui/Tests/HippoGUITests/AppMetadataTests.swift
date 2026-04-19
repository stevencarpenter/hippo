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
                "CFBundleVersion": "189"
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

    @Test
    func metadataVersionDescriptionWithVersionOnlyAndNoBuildNumber() {
        let metadata = AppMetadata(
            infoDictionary: [
                "CFBundleShortVersionString": "2.0.0"
            ]
        )

        #expect(metadata.marketingVersion == "2.0.0")
        #expect(metadata.buildNumber == "Unversioned")
        #expect(metadata.versionDescription == "Version 2.0.0")
        #expect(metadata.isReleaseStamped == false)
    }

    @Test
    func metadataFallsBackToBundleName() {
        let metadata = AppMetadata(
            infoDictionary: [
                "CFBundleName": "MyFallbackApp",
                "CFBundleShortVersionString": "1.0.0",
                "CFBundleVersion": "42"
            ]
        )

        #expect(metadata.displayName == "MyFallbackApp")
    }

    @Test
    func metadataPrefersDisplayNameOverBundleName() {
        let metadata = AppMetadata(
            infoDictionary: [
                "CFBundleDisplayName": "DisplayName",
                "CFBundleName": "BundleName"
            ]
        )

        #expect(metadata.displayName == "DisplayName")
    }

    @Test
    func metadataTrimsWhitespaceFromValues() {
        let metadata = AppMetadata(
            infoDictionary: [
                "CFBundleDisplayName": "  HippoGUI  ",
                "CFBundleShortVersionString": "  1.0.0  ",
                "CFBundleVersion": "  100  "
            ]
        )

        #expect(metadata.displayName == "HippoGUI")
        #expect(metadata.marketingVersion == "1.0.0")
        #expect(metadata.buildNumber == "100")
        #expect(metadata.versionDescription == "Version 1.0.0 (100)")
    }

    @Test
    func metadataIgnoresWhitespaceOnlyValues() {
        // A key with only whitespace should be treated as missing
        let metadata = AppMetadata(
            infoDictionary: [
                "CFBundleDisplayName": "   ",
                "CFBundleShortVersionString": "1.0.0"
            ]
        )

        // Falls back to hardcoded default when displayName is whitespace-only
        #expect(metadata.displayName == "HippoGUI")
    }
}
