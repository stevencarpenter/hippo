# HippoGUI

SwiftUI macOS app for browsing Hippo knowledge, events, sessions, and health status.

## Open in Xcode

HippoGUI now uses a package-first architecture:

- `HippoGUIKit` is the shared Swift package library containing the app UI, view models, services, and models
- `HippoGUI.xcodeproj` is a thin native macOS app host that depends on the local package
- `HippoGUIPackageApp` is a thin package executable host for `swift run` and script-based bundle builds

- Open `hippo-gui/HippoGUI.xcodeproj` for the standard macOS app target experience
- Open `hippo-gui/Package.swift` if you specifically want the Swift Package view

Open the native app project from the terminal with:

```bash
cd /Users/carpenter/projects/hippo/hippo-gui
xed HippoGUI.xcodeproj
```

Open the package view with:

```bash
cd /Users/carpenter/projects/hippo/hippo-gui
xed Package.swift
```

## Run tests

```bash
cd /Users/carpenter/projects/hippo/hippo-gui
swift test
```

The package includes a `HippoGUITests` target built with Swift Testing.

For rapid iteration, prefer editing library code under `Sources/HippoGUI/` and validating with `swift test`. The native Xcode project only owns a tiny host file plus app metadata/resources.

## Release versioning

- `HippoGUI` app bundle versions are stamped by `scripts/stamp-app-version.sh`
- `CFBundleShortVersionString` comes from the repo-wide version in `/Users/carpenter/projects/hippo/Cargo.toml` under `[workspace.package].version`
- `CFBundleVersion` comes from `HIPPO_BUILD_NUMBER`, then `BUILD_NUMBER`, then the current git commit count
- The same stamping flow is used by both `HippoGUI.xcodeproj` and `./scripts/build-native-app.sh`
- `./scripts/release-gui.sh` builds the native `.app`, creates a versioned `.zip`, writes sibling SHA-256 and Markdown release-notes files, and can emit CI-friendly JSON

To cut the next release version, bump the root workspace version and rebuild:

```bash
cd /Users/carpenter/projects/hippo
$EDITOR Cargo.toml
```

To inspect the stamped version in the script-built app:

```bash
cd /Users/carpenter/projects/hippo/hippo-gui
./scripts/build-native-app.sh
/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' dist/debug/HippoGUI.app/Contents/Info.plist
/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' dist/debug/HippoGUI.app/Contents/Info.plist
```

To build a shareable release artifact bundle:

```bash
cd /Users/carpenter/projects/hippo/hippo-gui
./scripts/release-gui.sh
HIPPO_BUILD_NUMBER=501 ./scripts/release-gui.sh release
```

To print release-notes Markdown instead of the human summary:

```bash
cd /Users/carpenter/projects/hippo/hippo-gui
./scripts/release-gui.sh --markdown
```

To emit CI-friendly JSON metadata:

```bash
cd /Users/carpenter/projects/hippo/hippo-gui
./scripts/release-gui.sh --json
```

The release helper writes both:

- `dist/release/HippoGUI.app`
- `dist/release/HippoGUI-<version>-<build>.zip`
- `dist/release/HippoGUI-<version>-<build>.zip.sha256`
- `dist/release/HippoGUI-<version>-<build>.release-notes.md`

To verify the generated archive checksum later:

```bash
cd /Users/carpenter/projects/hippo/hippo-gui/dist/release
shasum -a 256 -c HippoGUI-<version>-<build>.zip.sha256
```

## About and Settings

- Open `HippoGUI > About HippoGUI` for a dedicated About window; repeating the command reuses and focuses the same window
- Open `HippoGUI > Settings…` to view the same build information in Settings
- Native `.app` launches show the stamped version/build metadata; non-bundled development runs fall back to development metadata
- The native Xcode target stamps app versions during build from the same shared versioning flow used by the release scripts

## Previews and mocks

SwiftUI previews are defined in the main view files and use `MockBrainClient` so they render without a live Hippo backend.

Useful files:

- `Sources/HippoGUI/Services/MockBrainClient.swift`
- `Sources/HippoGUI/Services/BrainClientEnvironment.swift`
- `Sources/HippoGUIPackageApp/HippoGUIPackageApp.swift`
- `XcodeApp/HippoGUIXcodeApp.swift`
- `Tests/HippoGUITests/ViewModelTests.swift`
- `Tests/HippoGUITests/DecodingTests.swift`

## App structure

- `Sources/HippoGUI/App/` — shared app shell types used by both hosts
- `Sources/HippoGUI/Models/` — Codable/Sendable response models
- `Sources/HippoGUI/Services/` — HTTP client, environment injection, config, mocks
- `Sources/HippoGUI/ViewModels/` — `@Observable @MainActor` view models
- `Sources/HippoGUI/Views/` — SwiftUI screens and reusable UI components
- `Sources/HippoGUIPackageApp/` — package executable host
- `XcodeApp/` — native Xcode app host
- `HippoGUI.xcodeproj/` — native macOS project that links the local `HippoGUIKit` package
