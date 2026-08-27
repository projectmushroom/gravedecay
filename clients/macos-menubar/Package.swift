// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "GravedecayMenuBar",
    platforms: [.macOS("15.0")],
    dependencies: [.package(path: "../apple/GravedecayKit")],
    targets: [
        .executableTarget(name: "GravedecayMenuBar", dependencies: ["GravedecayKit"]),
        .testTarget(name: "GravedecayMenuBarTests", dependencies: ["GravedecayMenuBar", "GravedecayKit"]),
    ]
)
