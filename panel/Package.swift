// swift-tools-version: 5.9
import PackageDescription

// The combo panel: the window that opens from the menu bar and, once the
// pixel-art window is gone, the only place an agent has a face.
//
// It is a separate executable rather than part of the Python app because AppKit
// through PyObjC is what made the old menu bar expensive and awkward. It talks
// to the daemon over the HTTP + SSE API that already exists, so it shares no
// process state with anything.
let package = Package(
    name: "BobPanel",
    platforms: [.macOS(.v14)],
    dependencies: [
        // Native VT emulator + AppKit view. The hosted session's pane is
        // this control attached to Dark Army's pty, not a CRT grid we draw.
        .package(url: "https://github.com/migueldeicaza/SwiftTerm.git", from: "1.20.0"),
    ],
    targets: [
        .executableTarget(
            name: "BobPanel",
            dependencies: ["SwiftTerm"],
            // Info.plist is excluded as a *resource* — it is not copied
            // anywhere; the linker settings below weld it into the binary.
            exclude: ["Info.plist"],
            // The brand marks and the photo portraits the panel draws.
            // `assets/cast` is the menu-bar strip's pixel art; the menu bar
            // bakes its own icons from it and the panel never reads it.
            resources: [.copy("Resources/brand"), .copy("Resources/portraits")],
            linkerSettings: [
                // A bare executable has no bundle to read an Info.plist out
                // of, so it is embedded in a Mach-O section instead. Without
                // it `Bundle.main.bundleIdentifier` is nil and the process
                // has no application identity for the system to resolve.
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Sources/BobPanel/Info.plist",
                ])
            ]
        ),
        .testTarget(name: "BobPanelTests", dependencies: ["BobPanel"])
    ]
)
