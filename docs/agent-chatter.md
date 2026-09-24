# Dark Army talks while it waits; nothing spins

Lifted out of `CLAUDE.md` on 6 Sep 2026, unchanged.

**Dark Army talks while it waits; nothing spins** (`AgentChatter.swift`, the
fourth marker-pinned pair — byte-identical below `enum AgentChatter {` on both
sides, pinned by `host/tests/test_agent_chatter.py`, which also counts
`ProgressView` at **zero** across `panel/Sources/BobPanel` and
`ios/BobPhone`). `PHONE_CALL_SITES` in that test is the phone's count:
adding or removing an `AgentChatterView(` on the phone updates the constant
in the same change, so a later daemon ship does not discover the drift.
`AgentChatter.Wait` names what is being waited on and
`pool(for:)` is a shipped, total list of short lowercase lines in Dark Army's own
register, chosen by `Cast`'s djb2 over the seed (copied, never imported) plus
a step, so a repaint cannot reshuffle it; a wait that outlives one line
advances to the next. `AgentChatterView` has **two styles**: `.line`, the
typed sentence with a caret, where there is room for prose (the deleting
overlay, History, the phone's first connect and both catch-up loads);
`.caret`, the cursor alone, where the wait sits in a row of controls (Clear
Done, the reply and wrap-up bars, a card's starting and refining rows). **A caption is never replaced, only the spinner.** **The words ship in code**: the phone draws them exactly when it cannot reach the Mac. **The
clock is a `TimelineView` on `AgentChatter.Schedule`**, which wakes at `tick`
while characters arrive, at `caretPeriod / 2` once the line is typed or in
`.caret` style (pulled in to a line boundary so the next line types on time),
and not at all under reduce motion or while `\.agentChatterRunning` is false
— set by `PanelView` from `client.visible`, left at its default on the phone;
`AgentChatter.cadence` and `nextTick` are the pure rule, every clock snapped to
the millisecond by `AgentChatter.clock`. The reveal is a pure function of
`now - began`, `began` minted once per wait in `.task(id: seed)` with every
call site pinning `.id(seed)`, so a snapshot mid-wait recomputes the same
frame; a hidden fully-revealed sizing copy holds the finished line's frame so
nothing reflows. A tap finishes the line and remembers nothing; reduce motion
shows it whole; a screen reader hears one element reading the **required**
`spoken:` sentence. `Theme.mono` only, no `.lineLimit(`, no
`GeometryReader`, no `.clickable()`.
