// swift-tools-version: 6.3
import PackageDescription

let package = Package(
    name: "HippoGUI",
    platforms: [.macOS(.v26)],
    products: [
        .library(name: "HippoGUIKit", targets: ["HippoGUIKit"]),
        .executable(name: "HippoGUI", targets: ["HippoGUIPackageApp"])
    ],
    dependencies: [
        .package(url: "https://github.com/realm/SwiftLint", from: "0.57.0")
    ],
    targets: [
        .target(
            name: "HippoGUIKit",
            path: "Sources/HippoGUI",
            plugins: [.plugin(name: "SwiftLintBuildToolPlugin", package: "SwiftLint")]
        ),
        .executableTarget(
            name: "HippoGUIPackageApp",
            dependencies: ["HippoGUIKit"],
            path: "Sources/HippoGUIPackageApp",
            plugins: [.plugin(name: "SwiftLintBuildToolPlugin", package: "SwiftLint")]
        ),
        .testTarget(
            name: "HippoGUITests",
            dependencies: ["HippoGUIKit"],
            path: "Tests/HippoGUITests",
            plugins: [.plugin(name: "SwiftLintBuildToolPlugin", package: "SwiftLint")]
        )
    ],
    swiftLanguageModes: [.v6]
)
