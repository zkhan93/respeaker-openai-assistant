// swift-tools-version: 5.9
import PackageDescription

// SwiftPM builds the executable; `make bundle` assembles the .app around
// it. Deliberately no .xcodeproj — the bundle layout, Info.plist and
// signing are all scripted, so the build is reproducible from a clean
// checkout and reviewable as text.
let package = Package(
    name: "Raneen",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "Raneen",
            path: "Sources/Raneen"
        ),
        .testTarget(
            name: "RaneenTests",
            dependencies: ["Raneen"],
            path: "Tests/RaneenTests"
        )
    ]
)
