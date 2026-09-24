import Foundation
import UniformTypeIdentifiers

/// Copies of files attached to a card, and the Prepare-button hint.
///
/// The panel writes the bytes — it is an ordinary process with the user's
/// own access, already reading card documents directly, and an upload route
/// would be a loopback endpoint that writes arbitrary bytes to disk. The
/// daemon re-checks shape and containment at every use, so this is a
/// convenience path, not a trusted one.
enum CardAttachments {
    static let maxPerCard = 8
    static let maxBytes = 20 * 1024 * 1024
    static let maxNameChars = 80
    static let allowedExtensions: Set<String> = [
        "png", "jpg", "jpeg", "gif", "webp", "heic",
        "pdf", "txt", "md", "markdown", "log", "json", "csv", "rtf",
    ]

    /// Where the copies live. A stored `var` rather than a computed one so
    /// tests can point the whole store at a temp directory — the same seam
    /// `stage`/`keepIfOwned`/`resolve` already offer per call, needed here
    /// because `BoardState.resumeDraft` filters a draft's list through
    /// `resolve` with no root to pass. Production never assigns it, and the
    /// default itself is a temp folder under XCTest (`PanelStateDirectory`).
    static var dir: URL = PanelStateDirectory.root
        .appendingPathComponent("attachments", isDirectory: true)

    static var contentTypes: [UTType] {
        allowedExtensions.compactMap { UTType(filenameExtension: $0) }
    }

    /// Lowercased UUID, matching `field_refusal`'s `[a-z0-9-]{8,40}` folder.
    static func mintStagingId() -> String {
        UUID().uuidString.lowercased()
    }

    /// Basename, NFC, charset `[A-Za-z0-9._-]`, collapsed underscores,
    /// leading dots stripped, then capped at `maxNameChars` keeping the
    /// extension. Nil if nothing safe remains.
    static func sanitize(_ name: String) -> String? {
        var base = (name as NSString).lastPathComponent
            .precomposedStringWithCanonicalMapping
        let allowed = CharacterSet(charactersIn:
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
        base = String(base.unicodeScalars.map {
            allowed.contains($0) ? Character($0) : "_"
        })
        while base.contains("__") {
            base = base.replacingOccurrences(of: "__", with: "_")
        }
        while base.hasPrefix(".") {
            base.removeFirst()
        }
        if base.isEmpty { return nil }
        if base.count > maxNameChars {
            let ext = (base as NSString).pathExtension
            let suffix = ext.isEmpty ? "" : ".\(ext)"
            if suffix.count >= maxNameChars {
                base = String(suffix.suffix(maxNameChars))
            } else {
                let stem = String(base.dropLast(suffix.count))
                base = String(stem.prefix(maxNameChars - suffix.count)) + suffix
            }
            while base.hasPrefix(".") { base.removeFirst() }
            if base.isEmpty { return nil }
        }
        return base
    }

    /// Copy accepted files into `dir/<stagingId>/`, mode 0600.
    ///
    /// A symlink is stored as a regular-file copy of its target, so tidying
    /// the original cannot break the card. Directories are refused even when
    /// the name ends in an allowed extension. Over-limit, wrong-type and
    /// unreadable files are refused with a sentence each rather than silently
    /// dropped. Numeric-suffix dedupe on a name collision. Runs off the main
    /// actor — a 20 MB copy must not freeze the sheet.
    /// `root` is the attachments directory; tests pass a temp dir.
    static func stage(urls: [URL], into stagingId: String, root: URL? = nil) async
        -> (staged: [String], refused: [String]) {
        await Task.detached(priority: .userInitiated) {
            copy(urls: urls, into: stagingId, root: root)
        }.value
    }

    /// Fire-and-forget removal of a staging folder. Close-without-save, and
    /// a person deleting the draft that owns it. `root` is the attachments
    /// directory; tests pass a temp dir, the same seam `stage`,
    /// `keepIfOwned` and `resolve` already have.
    static func discard(_ stagingId: String, root: URL? = nil) {
        guard !stagingId.isEmpty else { return }
        let folder = (root ?? dir)
            .appendingPathComponent(stagingId, isDirectory: true)
        try? FileManager.default.removeItem(at: folder)
    }

    /// Files an in-flight `stage` just wrote, reconciled with whether the
    /// composer that started it is still the one on screen.
    ///
    /// Owned: return them for the sheet to list. Not owned: delete only
    /// these files and return []. `staged` is only paths this `stage` newly
    /// created (`uniqueName` never returns an occupied name; `copy` refuses
    /// rather than overwrite), so a mismatch cannot unlink a saved copy.
    /// Never `discard` the folder — a successful save keeps sibling copies
    /// the card now owns, and a new composer has a different stagingId this
    /// must not touch. `root` is the attachments directory; tests pass a
    /// temp dir.
    static func keepIfOwned(_ staged: [String], owned: Bool,
                            root: URL? = nil) -> [String] {
        guard owned else {
            for rel in staged {
                guard let path = resolve(rel, root: root) else { continue }
                try? FileManager.default.removeItem(atPath: path)
            }
            return []
        }
        return staged
    }

    /// Absolute path of a stored relative attachment, or nil if it is not a
    /// readable file inside the attachments directory. Both sides are
    /// symlink-resolved — `BoardDocuments.resolve`'s pattern, different root.
    static func resolve(_ rel: String, root: URL? = nil) -> String? {
        let base = (root ?? dir)
            .standardizedFileURL.resolvingSymlinksInPath()
        let trimmed = rel.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !trimmed.hasPrefix("/") else { return nil }
        let url = base.appendingPathComponent(trimmed)
            .standardizedFileURL.resolvingSymlinksInPath()
        let prefix = base.path.hasSuffix("/") ? base.path : base.path + "/"
        guard url.path.hasPrefix(prefix) else { return nil }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path,
                                             isDirectory: &isDirectory),
              !isDirectory.boolValue else { return nil }
        return url.path
    }

    /// The display name — the sanitised filename, not the staging folder.
    static func display(_ rel: String) -> String {
        (rel as NSString).lastPathComponent
    }

    // MARK: - copy (off the main actor)

    private static func copy(urls: [URL], into stagingId: String, root: URL? = nil)
        -> (staged: [String], refused: [String]) {
        var staged: [String] = []
        var refused: [String] = []
        guard !stagingId.isEmpty else {
            return ([], urls.map { _ in "Dark Army could not store that file." })
        }
        let base = root ?? dir
        let folder = base.appendingPathComponent(stagingId, isDirectory: true)
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: folder, withIntermediateDirectories: true)
            try fm.setAttributes([.posixPermissions: 0o700],
                                 ofItemAtPath: base.path)
            try fm.setAttributes([.posixPermissions: 0o700],
                                 ofItemAtPath: folder.path)
        } catch {
            return ([], ["Dark Army could not store that file."])
        }
        let existing = (try? fm.contentsOfDirectory(atPath: folder.path)) ?? []
        var remaining = maxPerCard - existing.count
        for url in urls {
            let accessed = url.startAccessingSecurityScopedResource()
            defer { if accessed { url.stopAccessingSecurityScopedResource() } }
            if remaining <= 0 {
                refused.append("a card can have at most \(maxPerCard) attachments")
                continue
            }
            let ext = url.pathExtension.lowercased()
            if !allowedExtensions.contains(ext) {
                if ext.isEmpty {
                    refused.append("that file type is not allowed")
                } else {
                    refused.append(".\(ext) files are not allowed")
                }
                continue
            }
            guard let name = sanitize(url.lastPathComponent) else {
                refused.append("that file type is not allowed")
                continue
            }
            guard let source = regularCopySource(url, fm: fm) else {
                refused.append("Dark Army could not store \(url.lastPathComponent).")
                continue
            }
            if source.size < 0 {
                refused.append("Dark Army could not read \(url.lastPathComponent).")
                continue
            }
            if source.size > maxBytes {
                refused.append("that file is larger than \(maxBytes / (1024 * 1024)) MB")
                continue
            }
            guard let unique = uniqueName(name, in: folder) else {
                refused.append("Dark Army could not store \(url.lastPathComponent).")
                continue
            }
            let dest = folder.appendingPathComponent(unique)
            if fm.fileExists(atPath: dest.path) {
                refused.append("Dark Army could not store \(url.lastPathComponent).")
                continue
            }
            do {
                try fm.copyItem(at: source.url, to: dest)
                let destAttrs = try fm.attributesOfItem(atPath: dest.path)
                guard destAttrs[.type] as? FileAttributeType == .typeRegular else {
                    try? fm.removeItem(at: dest)
                    refused.append("Dark Army could not store \(url.lastPathComponent).")
                    continue
                }
                try fm.setAttributes([.posixPermissions: 0o600],
                                     ofItemAtPath: dest.path)
            } catch {
                refused.append("Dark Army could not store \(url.lastPathComponent).")
                continue
            }
            staged.append("\(stagingId)/\(unique)")
            remaining -= 1
        }
        return (staged, refused)
    }

    /// The bytes to store: a regular file, following a symlink to its target.
    /// Nil for a directory, a dangling or non-file link, or an unreadable path.
    private static func regularCopySource(_ url: URL, fm: FileManager)
        -> (url: URL, size: Int)? {
        guard let attrs = try? fm.attributesOfItem(atPath: url.path),
              let type = attrs[.type] as? FileAttributeType else {
            return nil
        }
        if type == .typeDirectory {
            return nil
        }
        if type == .typeSymbolicLink {
            let target = url.resolvingSymlinksInPath()
            guard let tAttrs = try? fm.attributesOfItem(atPath: target.path),
                  tAttrs[.type] as? FileAttributeType == .typeRegular,
                  let size = (tAttrs[.size] as? NSNumber)?.intValue else {
                return nil
            }
            return (target, size)
        }
        guard type == .typeRegular else { return nil }
        let size = (attrs[.size] as? NSNumber)?.intValue ?? -1
        return (url, size)
    }

    /// A free basename in `folder` derived from `desired`.
    ///
    /// Clips the stem so `-\(n)` plus the extension still fit under
    /// `maxNameChars`. Sanitising the suffixed name would otherwise
    /// truncate it back to the occupied original. Nil when every
    /// candidate through 999 is taken — never an occupied name.
    private static func uniqueName(_ desired: String, in folder: URL) -> String? {
        let fm = FileManager.default
        if !fm.fileExists(atPath: folder.appendingPathComponent(desired).path) {
            return desired
        }
        let ext = (desired as NSString).pathExtension
        let stem = (desired as NSString).deletingPathExtension
        let suffixTail = ext.isEmpty ? "" : ".\(ext)"
        for n in 1...999 {
            let mark = "-\(n)"
            let reserved = mark.count + suffixTail.count
            guard reserved <= maxNameChars else { return nil }
            let clipped = String(stem.prefix(maxNameChars - reserved))
            let candidate = clipped + mark + suffixTail
            if !fm.fileExists(atPath: folder.appendingPathComponent(candidate).path) {
                return candidate
            }
        }
        return nil
    }
}

/// Why Prepare is greyed, in words. Nil when nothing is missing.
enum PrepareGate {
    static func missing(summaryEmpty: Bool, toolEmpty: Bool,
                        rootEmpty: Bool) -> String? {
        var parts: [String] = []
        // One name for the two boxes either of which satisfies it: the
        // caller passes "both empty", so a sentence naming only one of them
        // would send somebody to the box they had already decided not to use.
        if summaryEmpty { parts.append("your idea (or a description)") }
        if toolEmpty { parts.append("an assistant") }
        if rootEmpty { parts.append("a project") }
        switch parts.count {
        case 0: return nil
        case 1: return "Prepare needs \(parts[0])."
        case 2: return "Prepare needs \(parts[0]) and \(parts[1])."
        default:
            let head = parts.dropLast().joined(separator: ", ")
            return "Prepare needs \(head) and \(parts.last!)."
        }
    }
}
