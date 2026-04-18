// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "HippoGUI",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "HippoGUI", targets: ["HippoGUI"])],
    targets: [.executableTarget(name: "HippoGUI", path: "Sources/HippoGUI", resources: [.process("Resources")])]
)