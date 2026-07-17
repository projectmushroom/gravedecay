// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "GravedecayKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "GravedecayKit", targets: ["GravedecayKit"]),
    ],
    targets: [
        .target(name: "GravedecayKit"),
        .testTarget(name: "GravedecayKitTests", dependencies: ["GravedecayKit"]),
    ]
)
