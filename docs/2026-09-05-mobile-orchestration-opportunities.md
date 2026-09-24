# Bob Companion: mobile orchestration opportunities

Decision catalogue, 5 September 2026. These are proposals for selection, not approved implementation plans or board cards.

## Direction

Make the phone sufficient to choose useful work, clarify it, approve a plan, supervise execution, inspect evidence, request revisions, and accept the result across projects. The Mac remains the execution host initially. Independence from sitting at the Mac and independence from having a running Mac are separate milestones.

Product success should mean accepted outcomes with fewer human interruptions, not more live agents or more Done cards.

## What exists

The desktop combines a persistent Kanban workspace with a live agent rail, questions and supported replies, plans and local document reading, editable cards, initiative folders, dependencies, queue controls, per-project concurrency, review acknowledgement, and manual-check acknowledgement. It is the stronger work-management surface. Its infrastructure still depends heavily on known projects, open VS Code windows, installed extensions and provider-specific control capabilities.

The phone already offers Needs you, Fleet, Board and usage/profile surfaces; text/dictation and photo capture; editable preparation before saving; assistant/model choice; Save & Refine; Start; supported question and permission answers; stop/close where supported; encrypted home and away connections; push; a widget; offline card capture and drafts; and a recent event diary. These should be extended, not proposed as new features.

Verified limitations in the current source:

- `ios/BobPhone/CardDetailView.swift` is a read-only content reader with column/tool/model actions. It explicitly leaves truncated prompts truncated, saying the Mac has the rest. It has no plan document reader or saved-card text editor.
- `host/bob_companion_daemon/api_server.py` exposes a narrower phone action list than loopback: queue management, review acknowledgement, manual-check clearance, board ask and initiative management are absent from both phone write lists.
- Phone `NeedsYouView.swift` draws waiting agents. Ended cards, manual checks and result reviews do not form a unified decision inbox there. An existing plan, the *needs you lists ended cards* plan, covers part of this gap; reuse it.
- Away writes require a lease renewed by verified home traffic and expire after 24 hours. Reads continue. Photo uploads are home-only.
- Dispatch and project selection depend on known/open project context. An enrolled project is not automatically an independently launchable project.
- Phone notification routing targets tabs, rather than a specific durable card/decision.
- Provider controls differ: notably, Codex does not have Bob reply/typing/permission parity. A visible agent is not necessarily steerable from the phone.
- Offline capture exists; offline execution intent and comprehensive offline board editing do not. Drafts are local to each surface.
- The event diary is bounded to 500 entries / 24 hours. It is useful recent activity, not durable outcome evidence.
- Dependencies already exist, including cross-project choices in the desktop blocker picker. Per-project parallelism also exists. The gap is portfolio policy, mobile control and safe execution isolation, not introducing dependencies or concurrency from scratch.

## Candidate improvements

Size is relative implementation breadth, not a delivery estimate. M is a contained vertical slice; L spans state/API/UI or provider contracts; XL changes execution architecture. Some rows should become several cards after selection.

| ID | Candidate | Observable result | Size | Priority / dependency |
|---|---|---|---|---|
| 1 | Full mobile card and plan workspace | Read the entire prompt and contained plan; edit saved instructions; ask for plan changes and approve a specific plan revision before Start. | L | First; foundation for informed remote decisions. |
| 2 | Unified decision inbox | One place for questions, permissions, plans awaiting approval, ended cards, manual checks and results awaiting review. Each entry names the project, needed decision and next action. | L | First; extend the existing ended-card plan. |
| 3 | Mobile review and completion | Acknowledge review and manual checks; accept work or request a revision with a reason. Preserve the difference between an agent declaring Done and a human accepting its result. | M–L | First; builds on desktop semantics and #1. |
| 4 | Away access for a whole trip | Explicitly authorized remote access can remain useful beyond 24 hours, with visible expiry, device revocation and scoped reauthorization. | L | First; policy decision required before implementation. |
| 5 | Projects that can start without desk preparation | Persistent enrolled-project catalogue, readiness checks, and opening an existing project from the phone even when its editor window is closed. Show unavailable paths, missing tools and authentication needs in words. | L | First; separate project enrolment from execution readiness. |
| 6 | Mobile queue and capacity controls | Reorder/cancel queued work, pause further dispatch, manage blockers and change project concurrency from the phone. A press is confirmed against current server state. | M–L | Next; existing desktop mechanisms provide the base. |
| 7 | Evidence attached to every result | Retain a structured result with summary, changed files, test outcomes, preview/screenshots, unresolved checks and provenance. Read it after the agent and terminal are gone. | L | First thin slice, then expand; strengthens #3. |
| 8 | Capability-aware steering and recovery | Before Start, show whether the selected assistant supports mobile questions, replies, stop and recovery. For unsupported control, offer an explicit fresh run carrying a reviewed handoff. | L | First capability display; provider integrations separately. |
| 9 | Cross-project mission planning | State an outcome, have Bob propose milestones and cards across projects, inspect dependencies and approve the selected work. Project folders continue to organize local work; missions link its larger purpose. | L | After #1–3; selecting work automatically is a new authority decision. |
| 10 | Budgeted orchestration of approved work | Prioritize approved cards across projects under machine capacity, provider availability and spending limits; show why work is waiting. Stop or ask when a bound is reached. | XL | After #6, #8, #9 and reliable cost data; no unlimited autonomous backlog pickup. |
| 11 | Safe parallel execution | Isolated worktrees or equivalent isolation per run, declared shared-resource constraints, and a deliberate integration step. Two agents can progress without silently editing the same checkout. | XL | Before making higher concurrency a product promise. |
| 12 | Work continuity through poor connectivity | Cache full cards/plans; support offline edits with revision conflicts; show durable command receipts distinguishing unsent, accepted and observed completion. Keep expired approvals from replaying. | L | Extend the existing outbox; never treat a timeout as proof nothing happened. |
| 13 | Capture useful context from anywhere | Away photo/file upload, share-sheet intake for links/screenshots, multiple drafts and desktop–phone draft handoff. | L | Existing text/dictation/photo capture is the base; secure transfer is a separate slice. |
| 14 | Notifications that open the exact decision | Tap a push to reach its card/question, retain pending decisions beyond a session's life, and offer a cross-project catch-up digest. | M–L | After #2 and durable result/decision IDs. |
| 15 | Project objectives and value tracking | Record intended benefit, beneficiary, acceptance evidence and a later outcome check. Track accepted results, rework, cost per accepted outcome and time waiting on people. | L | Start objective fields early; connect #7 and #9 for reporting. |
| 16 | A dependable execution host | Host readiness on the phone, restart recovery and durable jobs; later offer a dedicated always-on Mac runner. Running after the personal Mac is off requires another reachable executor. | XL | Establish readiness now; independent runner is a separate architectural milestone. |

## Recommended first selection

Choose #1, #2, #3, #4 and #5 as the core mobile-independence programme. Include a narrow #7 result summary/evidence slice so completion can be judged, and the capability-display portion of #8 so unsupported controls are visible before work starts. Sequence #6 next to make cross-project supervision practical.

Build #9, #10 and #11 as a second programme once one task can reliably complete from a phone. The scheduler must execute a human-approved scope; broad autonomous selection would change the current deliberate-gesture invariant and needs a separate product decision.

The desktop's next role is a detailed planning/review workspace and execution-host control panel: richer result inspection, integration/conflict handling, configuration, and host diagnostics. It should share durable work state with the phone so switching devices preserves the same decisions and outcomes.

## Acceptance scenario and measures

For a proposed first milestone, spend 72 hours away from the desk and handle work across three existing enrolled projects: capture an idea, read and revise its plan, start it, resolve an interruption, inspect evidence, request a correction, accept the result, and reorder the next work. Keep the Mac reachable for this milestone. Count every step that still requires desktop intervention and record the reason.

Measure: percentage of eligible cards completed without desktop interaction; accepted outcomes per week; median human-blocked time; desktop interventions per completed card; rework/reopen rate; cost per accepted outcome where provider data is available; and command delivery failures or duplicate execution. No baseline was measured in this review.

## Evidence and limits

This is a source and documentation review of the current working tree, not a hands-on evaluation of the installed Mac app or TestFlight build. Existing manual checks in TODO remain unverified; no test pass or deployed-version parity is claimed. Other work was modifying the checkout during the review.

Read CLAUDE.md and the current TODO, inspected phone readers/composer/actions/outbox/routing, desktop card review/dependency controls, and the daemon action boundaries. GitNexus was bound explicitly to bob-companion and refreshed from an index one commit behind. Its CLI query traced create/refine and plan attachment; context confirmed home and remote paths converge at `_sealed_run`, with remote lease checking. MCP had a database storage-version mismatch, so the successful CLI was used. Graph flow extraction reports coverage limits, so source inspection substantiated capability gaps; absent graph edges were not treated as absence of behavior.

Apple schedules background refresh and grants limited runtime: https://developer.apple.com/documentation/backgroundtasks/choosing-background-strategies-for-your-app . Consequently, reliable scheduling should remain on a reachable execution host; phone background refresh is for freshness, not the orchestration clock.

No implementation plan, board card, app-code change, deployment or commit was made for this catalogue.
