import Foundation

/// Where the panel's pictures live — the portraits, the pixel-art cast and
/// the brand marks — resolved against the app the panel is *running as*.
///
/// SwiftPM's generated `Bundle.module` knows two places: a bundle beside
/// `Bundle.main.bundleURL` (right for a bare executable, wrong for the
/// nested `BobPanel.app`, whose bundle `build.sh` puts under
/// `Contents/Resources`) and a path hard-coded into the binary at build
/// time, pointing into the checkout that built it. An installed panel
/// therefore read its faces out of the *developer's build tree*, and the
/// day that tree was a throwaway worktree deleted right after the install,
/// every portrait missed and every row drew an initial. This resolver looks
/// in the installed app first and keeps `Bundle.module` as the last rung,
/// so a `swift run` from the checkout still finds its art.
enum PanelResources {
    static let bundleName = "BobPanel_BobPanel.bundle"

    /// The candidate directories, in the order they are tried. Pure, so a
    /// test can pin the order without an app bundle on disk.
    static func candidates(main: Bundle = .main) -> [URL] {
        var out: [URL] = []
        if let resources = main.resourceURL {
            out.append(resources.appendingPathComponent(bundleName))
        }
        out.append(main.bundleURL.appendingPathComponent(bundleName))
        return out
    }

    static let bundle: Bundle = {
        for url in candidates() {
            if let found = Bundle(url: url) { return found }
        }
        return Bundle.module
    }()

    /// `<bundle>/<folder>/<file>`, or nil where the folder is not there.
    static func url(folder: String, file: String) -> URL? {
        bundle.url(forResource: folder, withExtension: nil)?
            .appendingPathComponent(file)
    }
}
