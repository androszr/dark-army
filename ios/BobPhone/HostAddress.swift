import Foundation

/// Tidies what somebody typed or pasted into the pairing screen's host box,
/// and refuses in a plain sentence anything still unusable.
///
/// It exists because `URL(string:)` returns nil for the everyday cases — a
/// space, a pasted path — and the pairing screen used to force-unwrap that,
/// killing the app on the one pairing route the simulator has. Every rule
/// here is a pure string test: no Foundation URL parsing, no network, no
/// UIKit. That last part is load-bearing — this file is symlinked into the
/// Mac panel's test target, which is where its rules are exercised, so it
/// must compile on macOS.
enum HostAddress {
    struct Cleaned: Equatable {
        var host: String
        var port: Int
    }

    enum Verdict: Equatable {
        case ok(Cleaned)
        case refused(String)
    }

    /// Characters a host may be made of. Underscore is forgiven: it is
    /// nonstandard in a hostname but harmless, and the URL builder takes it.
    private static let allowed = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-_")

    /// A port carried inside the typed text wins over `typedPort` — it is the
    /// paste the person just made off the Mac's pairing sheet.
    static func check(host raw: String, typedPort: Int) -> Verdict {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)

        for scheme in ["http://", "https://"] where text.count >= scheme.count {
            if text.prefix(scheme.count).lowercased() == scheme {
                text = String(text.dropFirst(scheme.count))
                break
            }
        }
        if text.hasSuffix("/") {
            text = String(text.dropLast())
        }

        if text.contains("/") {
            return .refused("That looks like a web address — type just the host, like 192.168.1.23.")
        }
        if text.contains(" ") {
            return .refused("The host has a space in it — type just the address, like 192.168.1.23.")
        }
        if text.contains("[") || text.contains("]") || text.filter({ $0 == ":" }).count > 1 {
            return .refused("Type the plain address from the Mac's pairing sheet — an IPv6 address won't work here.")
        }

        var host = text
        var port = typedPort
        if let colon = text.firstIndex(of: ":") {
            host = String(text[text.startIndex..<colon])
            let tail = String(text[text.index(after: colon)...])
            guard let embedded = Int(tail), !tail.isEmpty else {
                return .refused("After the colon should be a port number, like 19875.")
            }
            port = embedded
        }

        guard (1...65535).contains(port) else {
            return .refused("The port needs to be a number between 1 and 65535.")
        }
        guard !host.isEmpty, host.allSatisfy({ allowed.contains($0) }) else {
            return .refused("That isn't a usable address — type the host shown on the Mac's pairing sheet.")
        }
        return .ok(Cleaned(host: host, port: port))
    }
}
