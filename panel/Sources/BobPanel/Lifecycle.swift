import AppKit
import Darwin

/// Making sure there is never more than one panel, and never one nobody owns.
///
/// The window is an ordinary titled one now, so a person can close it by hand —
/// but closing does not end the *process*, and the process is what these rules
/// are about. An orphaned instance whose window is ordered out is invisible,
/// has nothing on screen to click, still holds `~/.dark-army/panel.pid`,
/// and still holds an SSE slot on the daemon; and when the borderless-era
/// version of it *was* visible, it sat over everything showing a snapshot from
/// whenever its SSE connection died, and the only way out was `kill` (a debug
/// build launched from a shell that then exited, stdin on `/dev/null`). Two
/// rules keep it from happening again: nothing outlives whoever launched it,
/// and the menu bar's panel is the only panel.
///
/// **Both rules need a departure that cannot be refused, and for most of this
/// file's life there was not one.** Every quit path here and in `main.swift`
/// went through `NSApplication.terminate`, and that call is a *request*: while
/// a sheet is attached to any window, AppKit bails out above
/// `applicationShouldTerminate` — measured with a throwaway `swiftc` probe, no
/// delegate method is called at all and the process is still alive seconds
/// later. So an orphaned panel with a board card's detail sheet up fired the
/// watchdog every five seconds, lost every time, and sat above every Space:
/// exactly the stranding this file exists to prevent, reached by the one route
/// nothing outside the process escalates (`PanelProcess.quit()` follows with
/// `.kill()`, `PanelLock.end` follows with `SIGKILL`; the watchdog has nobody
/// behind it).
///
/// `PanelExit` is that departure, in two routes and deliberately not one.
/// `requested` is the tidy one for a driver that asked us to go — it detaches
/// any sheet first, then asks, with a one-second backstop. `now` is the last
/// resort and negotiates with nothing. Clearing the SwiftUI `.sheet(item:)`
/// binding is **not** a substitute for the detach: the same probe found
/// `attachedSheet` still non-nil on the turn the binding was cleared, and a
/// terminate issued behind it still defeated. `NSWindow.endSheet(_:)` detaches
/// synchronously and is.

/// True when stdin is a pipe or a socket — somebody on the other end is driving
/// us, so EOF means they have gone.
///
/// This is the condition `--hidden` was standing in for. The flag says how we
/// were *meant* to be run; this says whether anyone is actually there. A tty
/// (`swift run` from a terminal) and `/dev/null` are both "nobody", and quitting
/// on their EOF would make the panel impossible to look at on its own.
func stdinIsDriven() -> Bool {
    var info = stat()
    guard fstat(0, &info) == 0 else { return false }
    let kind = info.st_mode & S_IFMT
    return kind == S_IFIFO || kind == S_IFSOCK
}

/// The path of a running process, or nil if it is gone.
private func executablePath(of pid: pid_t) -> String? {
    var buffer = [CChar](repeating: 0, count: Int(MAXPATHLEN) * 4)
    let written = proc_pidpath(pid, &buffer, UInt32(buffer.count))
    guard written > 0 else { return nil }
    return String(cString: buffer)
}

/// Leaving, in the two ways this process is allowed to leave.
///
/// The split is the whole design. A quit that somebody *asked* for should run
/// the ordinary teardown, because the asker is usually about to start a
/// replacement and a half-released lock file is theirs to inherit. A quit
/// nobody can hear — an orphan — must not be able to fail, because there is no
/// second attempt and no outside escalation.
@MainActor
enum PanelExit {
    /// Go, now, without asking anything.
    ///
    /// Three pieces, and each is what it is for a reason:
    ///
    /// - `Trace.log` is fire-and-forget on its own serial queue, so a lost line
    ///   can never delay the exit that exists because everything else failed.
    ///   The line can be lost — the queue is drained by neither route out of
    ///   this file, since `exit()` does not drain GCD either — and that is the
    ///   accepted trade. Do not "fix" it with a synchronous stderr
    ///   write: on the orphan path the reader of that pipe is the process that
    ///   just died, and a blocking write into a full pipe with no reader is a
    ///   new way to hang the thing that exists so nothing can hang.
    /// - The lock is given back by hand (`PanelLock.release`), because
    ///   `applicationWillTerminate` will not run and its entire body is that
    ///   one call. A pid file left naming a
    ///   dead process is inert (`incumbent()` re-verifies liveness, path and
    ///   basename before it signals anything), but leaving it honest costs one
    ///   guarded `removeItem`.
    /// - `_exit` rather than `exit`, because atexit handlers and static
    ///   destructors run while the SSE task and the stdin reader are still
    ///   live, and a last resort that can hang is not one.
    static func now(_ reason: String) -> Never {
        Trace.log("exit now: \(reason)")
        PanelLock.release()
        _exit(0)
    }

    /// How long `requested` waits for the polite route before taking the hard
    /// one.
    ///
    /// One second, bounded from above by the outside world: `PanelProcess.quit()`
    /// in `host/dark_army_menubar/panel_process.py` waits 2s and then
    /// `.kill()`s us, so the panel must be able to win its own exit and never
    /// turn an ordinary Restart into a SIGKILL.
    /// `nonisolated` only so it can be a default argument on an isolated
    /// method; it is a constant.
    nonisolated static let backstopSeconds: TimeInterval = 1.0

    /// Somebody asked us to go: tidy up, ask, and leave anyway if the ask does
    /// not take.
    ///
    /// Two things this buys less of than it looks, both worth stating where the
    /// call is rather than in a plan nobody reads twice:
    ///
    /// - The trace line is exactly as best-effort as `now`'s. `Trace.log` hands
    ///   the line to a private serial queue, `terminate` reaches `exit()`, and
    ///   `exit()` drains no GCD queue — so an `exit requested:` line can be lost
    ///   just as an `exit now:` one can. The backstop's line is the one
    ///   exception, and only because it is written **synchronously** below
    ///   rather than through `Trace`: it is the single line anybody needs to
    ///   trust, since its absence is what says the polite route won on its own.
    ///   That synchronous write is legal *here* and must not be pushed down
    ///   into `now`, where the orphan path has no reader on the far end of the
    ///   pipe and a blocking write is a new way to hang the last resort.
    /// - The backstop covers a `terminate` that **returns** without acting,
    ///   which is the measured failure (a sheet attached, no delegate method
    ///   called). It cannot cover a `terminate` that **blocks**: it is armed on
    ///   `RunLoop.main` and fired on the main thread, so a teardown wedged in
    ///   `exit()`'s atexit handlers takes the backstop and the orphan watchdog
    ///   down with it. On the stdin `quit` route `PanelProcess.quit()`'s 2s
    ///   `.kill()` still covers that; on the stdin EOF route the driver is by
    ///   definition already gone and nothing does. Moving the backstop onto a
    ///   private `DispatchSourceTimer` calling `_exit` would close it, and is
    ///   deliberately not done here — no such hang has been observed, and a
    ///   second thread that can kill the process is not something to add on a
    ///   hypothesis.
    static func requested(_ reason: String, backstop: TimeInterval = backstopSeconds) {
        Trace.log("exit requested: \(reason)")
        detachSheets()
        // `.common` rather than `.default`: the panel's own menus track outside
        // the default mode, and a fallback a popped menu can outlast is not a
        // fallback. (An open menu starves the main queue entirely; that is a
        // different failure, and it self-resolves when the pointer moves.)
        let timer = Timer(timeInterval: backstop, repeats: false) { _ in
            // Explicit `-> Void`: `now` returns `Never`, which the generic
            // parameter would otherwise infer and then conflict with.
            // Written by hand, before `now`, for the reason given above: this
            // is the one line whose absence is load-bearing, and `Trace.log`
            // would hand it to a queue that `_exit` never lets run. Safe on
            // this path only — `requested` is reached solely when a driver is
            // or was on the other end of the pipe, so a dead reader answers
            // with EPIPE rather than blocking.
            FileHandle.standardError.write(Data("trace exit now: \(reason) — backstop\n".utf8))
            MainActor.assumeIsolated { () -> Void in
                PanelExit.now("\(reason) — backstop")
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        // The sender is nil; it is spelled out so this one polite request stays
        // greppably distinct from the four unguarded calls it replaced.
        let sender: Any? = nil
        NSApp.terminate(sender)
    }

    /// End every attached sheet, so the request above is not refused.
    ///
    /// **Only legal on a path that ends in process death.** This detaches the
    /// sheet behind SwiftUI's back and leaves `BoardState.editing` set, so
    /// AppKit's view of what is open and SwiftUI's have deliberately been
    /// desynchronised; a caller that survived would draw a board whose state
    /// says a card is open over a screen that says it is not. Keep it inside
    /// this enum.
    ///
    /// Sheets are themselves in `NSApp.windows`, so a nested sheet is covered
    /// by the outer iteration. The identity check is the loop's only exit
    /// condition should `endSheet` ever fail to detach.
    private static func detachSheets() {
        for window in NSApp.windows {
            while let sheet = window.attachedSheet {
                window.endSheet(sheet)
                sheet.orderOut(nil)
                if window.attachedSheet === sheet { break }
            }
        }
    }
}

/// Quit when the process that launched us is gone.
///
/// Orphaning is the one state a panel cannot recover from: nothing can send it
/// `hide`, and if it never took key nothing can dismiss it either. `getppid() == 1`
/// is the whole test — launchd reparents orphans to pid 1, and both legitimate
/// launchers (the menu bar, a terminal) are real processes that outlive us.
///
/// Checked on a timer as well as at launch, because the parent usually goes
/// *after* we start: a rebuild-and-restart replaces the menu bar underneath us.
///
/// Both branches go through `PanelExit.now` and deliberately **not**
/// `PanelExit.requested`. This is the last resort — nothing escalates behind it
/// — so it must not negotiate with a modal, and there is nothing to gain by
/// trying: `applicationWillTerminate`'s whole body is the lock release that
/// `now` performs by hand. Asking politely here is what left a panel with a
/// board card's sheet up firing every five seconds and losing every time.
@MainActor
func installOrphanWatchdog(interval: TimeInterval = 5) {
    if getppid() == 1 {
        PanelExit.now("orphaned: parent gone")
    }
    Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { _ in
        MainActor.assumeIsolated {
            if getppid() == 1 { PanelExit.now("orphaned: parent gone") }
        }
    }
}

/// The record of which panel is the panel.
///
/// `~/.dark-army/panel.pid` holds the running instance's pid and executable
/// path. A menu-bar-owned instance (`--hidden`) takes the slot on launch and ends
/// whoever held it — a leftover must never share the screen with the real one,
/// which is exactly the "I click and see two" symptom. An instance run by hand
/// records itself but evicts nobody, so `swift run` next to a running app still
/// works and the app's panel keeps its authority.
enum PanelLock {
    static var path: URL {
        PanelStateDirectory.root
            .appendingPathComponent("panel.pid")
    }

    /// Claim the slot. `owned` is true for the menu bar's instance.
    static func claim(owned: Bool) {
        if owned, let other = incumbent() { end(other) }
        write()
    }

    /// Give the slot up, but only if it is still ours — a `--hidden` instance
    /// that evicted us has already overwritten it, and clearing that would leave
    /// the live panel unrecorded.
    static func release() {
        guard let (pid, _) = read(), pid == getpid() else { return }
        try? FileManager.default.removeItem(at: path)
    }

    /// The pid in the file, if it is alive and really is another panel.
    ///
    /// The path check is not decoration: pids are reused, and this file names one
    /// that is about to be sent a signal. Killing a stranger who inherited the
    /// number would be a far worse bug than the one this fixes.
    private static func incumbent() -> pid_t? {
        guard let (pid, recorded) = read(), pid != getpid(), kill(pid, 0) == 0,
              let live = executablePath(of: pid),
              panelExecutableNames.contains((live as NSString).lastPathComponent),
              recorded.isEmpty || recorded == live
        else { return nil }
        return pid
    }

    private static func end(_ pid: pid_t) {
        kill(pid, SIGTERM)
        // Ask, then insist. A panel that has wedged badly enough to ignore
        // SIGTERM is precisely the one that must not be left on screen.
        for _ in 0..<20 {
            if kill(pid, 0) != 0 { return }
            usleep(50_000)
        }
        kill(pid, SIGKILL)
    }

    private static func read() -> (pid_t, String)? {
        guard let text = try? String(contentsOf: path, encoding: .utf8) else { return nil }
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        guard let first = lines.first, let pid = pid_t(first.trimmingCharacters(in: .whitespaces))
        else { return nil }
        let recorded = lines.count > 1 ? String(lines[1]) : ""
        return (pid, recorded)
    }

    private static func write() {
        let file = path
        try? FileManager.default.createDirectory(at: file.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        let body = "\(getpid())\n\(executablePath(of: getpid()) ?? "")\n"
        try? body.write(to: file, atomically: true, encoding: .utf8)
    }
}

/// The binary's own name, matched against a recorded pid's real path.
/// `Dark Army` is the installed Dock name; `BobPanel` is the Swift product
/// and any bundle built before that rename.
let EXECUTABLE_NAME = "BobPanel"
let panelExecutableNames: Set<String> = [EXECUTABLE_NAME, "Dark Army"]
