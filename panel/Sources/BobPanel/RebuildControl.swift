import Foundation

/// The words Rebuild & restart wears on the window's top bar and on an agent's
/// own bar. Foundation-only so a test can pin them; the menu bar owns the
/// in-flight flag and the outcome, and both reach the panel on the context
/// push, so this derives nothing — it only chooses the words.
enum RebuildControl {
    /// Said beside the amber tint, so the colour is never the only sign.
    static let staleNote = "The running build is older than the source."

    /// "Rebuilding…" wins over a past failure (a second try is running),
    /// "Rebuild failed — try again" after one, else the menu bar's own label
    /// ("Rebuild & Deploy" / "Rebuild & Reload": the two really do different
    /// things).
    static func title(label: String, rebuilding: Bool, outcome: String) -> String {
        if rebuilding { return "Rebuilding…" }
        if outcome == "failed" { return "Rebuild failed — try again" }
        return label
    }
}
