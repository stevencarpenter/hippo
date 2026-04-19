// swift-tools-version: 6.3
import PackageDescription

let package = Package(
    name: "HippoGUI",
    platforms: [.macOS(.v26)],
    products: [
        .library(name: "HippoGUIKit", targets: ["HippoGUIKit"]),
        .executable(name: "HippoGUI", targets: ["HippoGUIPackageApp"]),
    ],
    targets: [
        .target(name: "HippoGUIKit", path: "Sources/HippoGUI"),
        .executableTarget(
            name: "HippoGUIPackageApp",
            dependencies: ["HippoGUIKit"],
            path: "Sources/HippoGUIPackageApp"
        ),
        .testTarget(name: "HippoGUITests", dependencies: ["HippoGUIKit"], path: "Tests/HippoGUITests"),
    ],
    swiftLanguageModes: [.v6]
)
