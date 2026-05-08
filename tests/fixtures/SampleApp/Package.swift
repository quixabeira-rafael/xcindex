// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "SampleApp",
    platforms: [
        .macOS(.v13),
    ],
    targets: [
        .target(name: "Core"),
        .target(name: "Domain", dependencies: ["Core"]),
        .target(name: "UI", dependencies: ["Domain"]),
    ]
)
