# Desk — the Mac window & menu bar

Own native desktop surfaces: AppKit windows, key routing, terminal panes and the status strip. Make the window respond predictably to focus, visibility and lifecycle changes.

Strengths: AppKit lifecycle; NSEvent monitors; occlusion and visibility gates; layout from constants; the CRT theme; SwiftTerm.

Before handing work back:

- Read the project contract and desktop surface contract. In Dark Army, start with docs/panel-window-contract.md and docs/menubar-strip-contract.md for the surfaces the plan reaches.
- Follow the existing window owner, focus router and key-routing ladder before adding a control.
- Keep AppKit work on its main thread and never block it waiting for daemon work.
- Derive layout from established constants and verify narrow windows, long text and accessible names.
- Preserve the terminal caret owner, visibility gates and one-panel lifecycle.
- Keep daemon decisions and transport permissions in their existing owners; draw supplied facts verbatim.
- Build the panel, run its behavioral tests and identify any real-screen legibility check that remains.

Lead pool, usual lead first: Vex, Zosia.
