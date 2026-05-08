// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "xcindex-helper",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .executable(name: "xcindex-helper", targets: ["xcindex-helper"]),
    ],
    dependencies: [
        .package(url: "https://github.com/apple/indexstore-db.git", branch: "main"),
    ],
    targets: [
        .executableTarget(
            name: "xcindex-helper",
            dependencies: [
                .product(name: "IndexStoreDB", package: "indexstore-db"),
            ],
            path: "Sources/xcindex-helper"
        ),
    ]
)
