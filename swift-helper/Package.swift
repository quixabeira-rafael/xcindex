// swift-tools-version:6.2
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
                // Lower-level Swift wrapper of the raw libIndexStore C API.
                .product(name: "IndexStore", package: "indexstore-db"),
            ],
            path: "Sources/xcindex-helper",
            swiftSettings: [
                .swiftLanguageMode(.v6),
                .enableExperimentalFeature("Lifetimes"),
            ],
            linkerSettings: [
                // libsqlite3 ships with macOS; the helper writes the cache through it.
                .linkedLibrary("sqlite3"),
            ]
        ),
    ]
)
