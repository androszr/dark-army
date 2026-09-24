# Dark Army: team-agent orchestration and analytics audit

**Audit date:** 9 September 2026  
**Scope:** macOS desktop, iPhone, shared daemon, editor bridge and sealed relay  
**Status:** audit delivered; all roadmap items are proposals, not approved product changes  
**Source baseline:** main at e60924f with concurrent uncommitted source, tests and documentation. The accepted-plan run preserved the pre-existing audit/CSV and the root session’s recorded dirty baseline; source observations describe this checkout, not an installed release.

## Decision summary

Dark Army already coordinates real work: it prepares and plans cards, launches agents through guarded routes, queues work by project, shows observed specialists, supports phone intervention and records explicit outcome decisions. Its next useful step is making coordination and evidence easier to trust across those features.

Prioritize three practical outcomes: make concurrent execution risks visible; give the phone a complete decision-and-review path; retain and explain the evidence behind results and rework. Use the existing outcome ledger for analysis. Multi-person permissions and multiple execution hosts are separate product decisions and belong in discovery, not in the first delivery tranche.

There is no demonstrated critical production incident in this audit. P1 means the first delivery tranche, not an emergency security verdict. Source review identified an away-terminal write-budget mismatch and inaccurate widget cadence wording; live reproduction and user-impact measurement remain separate checks.

## Objective

Who benefits: Team members managing agent-based workflows

Intended benefit: A structured improvement roadmap that enhances the application's orchestration capabilities, making team coordination and agent management more effective.

Success criterion: All improvement opportunities have been documented, estimated for effort and impact, and ranked by priority in a shared spreadsheet or document.

## Deliverables and sharing

This document and [the prioritized CSV](2026-09-09-team-agent-orchestration-roadmap.csv) are the editable repository handoff. The CSV is the canonical ranked register; the entries below include the same IDs, estimates and evidence. Import it as UTF-8 CSV into a spreadsheet, preserving the ID column when editing priorities. The audit scope record records the delivery and acceptance checks.

The document connector reported no connected document sessions. These files have **not** been uploaded to a cloud document, committed, pushed or shared with other people. A board attachment is attempted separately and its actual result is recorded in the delivery note. Repository availability is not proof that every team member has access.

All 30 opportunities identified in this audit are documented. No finite source audit can prove that every possible improvement has been discovered. Shared-team accessibility remains unverified until a team member opens the document or an agreed shared destination receives it.

## Method, evidence and limits

- Read the repository architecture and current TODO, inspected relevant production sources and tests, and compared the older mobile opportunity catalogue against today's behavior.
- Used GitNexus CLI bound explicitly to bob-companion for execution/queue, outcomes, collaboration, readiness and human-ownership queries, followed by symbol context and source reads. The original assessment recorded a rebuild of 30,758 nodes, 103,421 edges and 621 flows; those are historical counts. This accepted-plan run queried the available index and reread current sources. Its metadata reports indexing at 2026-09-09 06:56 UTC: 31,134 nodes, 105,733 edges and 612 flows. Concurrent uncommitted changes can postdate that index.
- Graph limitations remain: daemon.py exceeded the 512 KB indexing limit; flow enumeration and some callable candidates were capped; cross-language fields were unresolved. A missing graph edge was not treated as absence of functionality. Current source takes precedence over stale architecture wording or older plans.
- Estimates are planning judgments, not measured delivery times. No stakeholder interviews, interactive Mac/iPhone usability sessions, battery measurements, remote stress tests or penetration test were performed. No production sessions, tokens, transcripts or private board records were exported for this report.
- The audit changes documentation only. It is not an implementation, release-readiness verdict or authorization to widen mobile/agent permissions. The host test gate and artifact checks are recorded under Validation.

## Existing capabilities to preserve

| Area | Desktop today | Mobile today | Audit interpretation |
|---|---|---|---|
| Planning and execution | Prep/Refine/plan attachment/Start; model selection; guarded dispatch; project start and queues | Card creation and editing, plan reading/approval, Start/Refine, queue and per-project parallel controls | Extend and explain these; do not propose basic orchestration as missing. |
| Dependencies and specialists | Cycle-checked blockers; declared workflow versus observed specialist trails | Blocker controls and crew display | Improve chain navigation and outcome evidence; participation is not verification. |
| Human intervention | Seven-kind Inbox, permission/question/reply controls, manual and review handling | Needs you, permissions/replies, durable catch-up and exact notification routing | Mobile information architecture and final acknowledgments have specific remaining gaps. |
| Work evidence | Work report, project file differences and explicit result review | Work-record and per-file difference reading | Keep authorship caveats; add bounded attempt history and offline evidence. |
| Analysis | History trends/projects/sessions; outcome acceptance/rework/wait/cost reports | Read-only outcome reports and operational summaries | Comparison, sharing and coverage explanations are incremental work. |
| Offline and away use | Hosted terminal, per-device grants and sealed transport | Protected card/plan cache, drafts/outbox/receipts, home and away terminal, reconnect/catch-up | Do not recommend blind retry or generic offline dispatch. Away grants already offer 1/3/7/14 days. |
| Access and safety | Enrollment, action capabilities, process identity checks, explicit write boundaries | Pairing, sealed commands, receipts, scoped away grants and confirmation | These are valuable boundaries, not friction to remove. |
| Accessibility and context | Keyboard triage, window scaling, project knowledge and shared agent pack | VoiceOver, scalable text, unclipped prose, widgets | Test real workflows before proposing a visual rewrite. |

Sources: [architecture](../CLAUDE.md), [phone contract](phone-contract.md), [transport contract](transport-contract.md), [specialist evidence](card-crew.md), [outcome measurement](../host/bob_companion_daemon/board_outcomes.py), [mobile dependency controls](../ios/BobPhone/CardDetailView.swift).

## Reconciliation with the 5 September mobile catalogue

The [older catalogue](2026-09-05-mobile-orchestration-opportunities.md) is retained as historical research. These checks prevent completed capabilities becoming duplicate proposals:

| Earlier claim or proposal | Current source and remaining scope |
|---|---|
| Full cards, plan reading and saved-card editing absent | [CardDetailView](../ios/BobPhone/CardDetailView.swift) now contains the full editor and plan reader. R02/R06/R07 address triage and review completion. |
| Queue and capacity controls absent | [PhoneActions](../ios/BobPhone/Actions.swift) includes queue move/unqueue, project start and capacity/preferences. R01/R08/R10 address explanation, concurrency risk and dependency navigation. |
| Away access lasts only 24 hours | [relay.py](../host/bob_companion_daemon/relay.py) offers per-device 1/3/7/14-day grants. Extending a fixed 24-hour limit is no longer an opportunity. |
| Offline full cards/plans and exact notification routing absent | [CardCache](../ios/BobPhone/CardCache.swift), [Router](../ios/BobPhone/Router.swift) and [CatchUpView](../ios/BobPhone/CatchUpView.swift) now provide protected content, receipt routing and retained catch-up. R19 addresses result evidence beyond those caches. |
| Structured result evidence and outcome analytics absent | [WorkRecordView](../ios/BobPhone/WorkRecordView.swift) reads reports and diffs; [OutcomeScreens](../ios/BobPhone/OutcomeScreens.swift) reads retained outcome reports. R05/R12/R14–R18 extend their history, evidence and analysis. |
| Mobile review, remote capture and a separate execution host | Review/manual-clear and outcome decisions remain outside phone action tuples; uploads remain home-only. R06/R07/R20/R26/R29/R30 separate bounded improvements from authority and topology decisions. |

The older catalogue also described autonomous cross-project missions and budgeted scheduling. This register bounds that direction to human responsibility, dependency navigation and budget awareness (R09/R10/R18); it does not carry forward automatic work selection as an approved or estimated implementation. Broader scheduling remains a separate product-discovery question, outside this audit’s bounded delivery slices.

## Prioritization and estimate rules

**Rank** is a unique overall review order. **P1** is the first tranche: recurring coordination impediments or foundations needed for safe scaling. **P2** extends existing workflows and analysis. **P3** is a larger expansion or discovery task whose user need or authority model needs validation. Within a tier, rank favors broad/high-impact impediments and enabling evidence before optional convenience. Dependencies determine implementation sequence even when the dependent item has a higher review rank. This is an explicit editorial ranking; there is no fabricated usage dataset or false-precision score.

**Impact:** 5 = prevents a major orchestration failure or materially improves coordination across a team; 4 = removes a frequent handoff/review obstacle or improves decisions; 3 = meaningful analysis/recovery/usability improvement; 2 = localized clarity improvement; 1 = minor polish. These are expected benefits to the stated audience, not measured uplift.

**Effort:** total engineer-days for the bounded slice, including relevant daemon/client work, tests, compatibility and review. Shared backend effort is counted once per row, not again per platform. Ranges exclude waiting for product decisions, field studies, deployment and unrelated refactoring. One engineer-day means one focused engineering day; elapsed weeks depend on staffing. For orientation: XS up to 1 day, S 2–4, M 5–9, L 10–15, XL above 15. Items spanning bands retain the numeric range. Do not sum discovery estimates into a production commitment.

**Confidence:** High = direct current source evidence, with a reasonably bounded slice; Medium = source-supported opportunity but user demand/design or integrations need validation; Low = strategic hypothesis requiring discovery. Confidence does not claim live verification.

**Evidence kind:** Source-confirmed limit = a bounded gap in inspected current code; deliberate boundary = an intentional constraint whose expansion requires a decision; source-confirmed mismatch = two inspected behaviors or statements conflict, without measured runtime harm; product/research hypothesis = usefulness or design still needs discovery. No row is classified as a measured defect. High confidence in source evidence does not mean high confidence in the projected benefit.

The review order uses tier, workflow reach, prerequisite value and implementation risk, with effort used to select bounded early slices. Each row records its tie-break rationale. In particular, R02 covers all mobile triage while R03 concerns away terminal input; R04 is a smaller readiness slice ahead of R05’s retention migration. R25 can be delivered independently despite its lower impact and rank. Rank is therefore neither an impact/effort quotient nor a mandatory serial schedule.

**Owners** are proposed disciplines, not assignments to real people. Product should nominate accountable people before execution. Every row has a measurable completion check; its benefit should subsequently be measured against a baseline of the same workflow.

## Ranked roadmap

| Rank / ID | Opportunity | Platform | Priority | Impact / 5 | Effort, engineer-days | Confidence |
|---|---|---|---|---|---|---|
| 1 / R01 | Explain shared-checkout concurrency before raising parallelism | Desktop + Mobile | P1 | 5 | 2–4 | High |
| 2 / R02 | Unify the mobile decision inbox | Mobile | P1 | 5 | 5–9 | High |
| 3 / R03 | Make away terminal input respect its write budget | Mobile | P1 | 5 | 4–8 | High |
| 4 / R04 | Explain provider and host readiness before starting work | Desktop + Mobile | P1 | 4 | 4–7 | High |
| 5 / R05 | Retain bounded evidence for every implementation attempt | Desktop + Mobile | P1 | 5 | 8–14 | High |
| 6 / R06 | Acknowledge reviews and manual checks from the phone | Mobile | P1 | 4 | 3–6 | High |
| 7 / R07 | Give mobile outcome review a clear desktop handoff | Mobile + Desktop | P1 | 4 | 2–4 | High |
| 8 / R08 | Make priority and queue order understandable | Desktop + Mobile | P1 | 4 | 3–5 | High |
| 9 / R09 | Add named coordination and review responsibilities | Desktop + Mobile | P2 | 4 | 6–10 | Medium |
| 10 / R10 | Show dependency chains and their release conditions | Desktop + Mobile | P2 | 4 | 5–9 | High |
| 11 / R11 | Pilot isolated workspaces with explicit integration | Desktop-led + Mobile status | P2 | 5 | 15–25 | Medium |
| 12 / R12 | Distinguish observed specialists from verified stage results | Desktop + Mobile | P2 | 4 | 6–10 | High |
| 13 / R13 | Make queued-plan authorization visible and version-specific | Desktop + Mobile | P2 | 4 | 4–7 | High |
| 14 / R14 | Add comparable outcome reporting periods | Desktop + Mobile | P2 | 4 | 5–9 | High |
| 15 / R15 | Export a redacted report for team review | Desktop + Mobile | P2 | 4 | 4–7 | High |
| 16 / R16 | Explain where time is lost between workflow stages | Desktop + Mobile summary | P2 | 4 | 8–13 | Medium |
| 17 / R17 | Keep analytical coverage and provider accounting explicit | Desktop + Mobile | P2 | 4 | 4–7 | High |
| 18 / R18 | Offer project budget awareness without autonomous cancellation | Desktop + Mobile | P2 | 4 | 5–9 | Medium |
| 19 / R19 | Keep selected result evidence readable offline | Mobile | P2 | 3 | 5–9 | High |
| 20 / R20 | Support multiple mobile drafts and explicit device handoff | Mobile + Desktop | P2 | 3 | 6–10 | High |
| 21 / R21 | Unify recovery diagnostics across host and phone | Desktop + Mobile | P2 | 3 | 4–7 | Medium |
| 22 / R22 | Show agent collaboration as a navigable evidence map | Desktop + Mobile summary | P2 | 3 | 5–9 | High |
| 23 / R23 | Make project knowledge easier to inspect and maintain | Desktop + Mobile read | P2 | 3 | 5–8 | Medium |
| 24 / R24 | Test dense desktop workflows and accessibility with users | Desktop | P2 | 3 | 3–6 | Medium |
| 25 / R25 | Correct the widget refresh expectation | Mobile | P2 | 2 | 0.5–1 | High |
| 26 / R26 | Design an away capture and attachment slice | Mobile | P3 | 3 | 10–18 | Medium |
| 27 / R27 | Define durable operational history and recovery policy | Desktop + Mobile read | P3 | 3 | 8–14 | Medium |
| 28 / R28 | Validate a true multi-person workspace model | Desktop + Mobile | P3 | 5 | 8–15 | Low |
| 29 / R29 | Explore a read-only fleet across multiple Macs | Desktop + Mobile | P3 | 4 | 10–18 | Low |
| 30 / R30 | Evaluate scoped outcome decisions away from the desk | Mobile | P3 | 4 | 8–15 | Medium |

## Delivery sequence

1. **Make current operation understandable:** R01, R04, R07 and R08. R25 is an independent small wording fix. Capture current triage time, failed launch reasons and terminal refusals before changes.
2. **Complete mobile decisions and preserve evidence:** R02, R03, R05 and R06. Plan LAN and away authority separately for R06. Verify ordinary home use, expired/revoked grants, stale views and uncertain delivery.
3. **Improve planning and analysis:** R09–R10 and R12–R24 as indicated by their dependencies. Ship R14 before its export consumer R15, R05/R12 before stage analysis R16, and R17 before budget warnings R18.
4. **Pilot safe parallel work:** R11 after R01/R05. It is a separate engineering track, not a reason to postpone the advisory UI. Do not claim isolation until integration and recovery checks pass.
5. **Validate product expansion:** R26–R30. Multi-person membership, federated hosts and remote acceptance each require their own product/security decision. R28 is a prerequisite for R29/R30, not a promise to build them.

No calendar dates or headcount commitments are implied. Agree a small first tranche, measure it, then re-rank the register with observed value and revised effort. The old mobile catalogue is historical context, not an additional backlog to implement wholesale.

## Detailed opportunity register

Evidence paths below refer to this repository at the audit baseline. Where a gap is an inference from an inspected schema or view, the current-behavior wording limits the claim to that inspected surface.

### R01 · Explain shared-checkout concurrency before raising parallelism

**Priority:** P1 (rank 1) · **Platform:** Desktop + Mobile · **Impact:** 5/5 · **Effort:** 2–4 engineer-days · **Confidence:** High

**Evidence kind:** Deliberate boundary. **Rank rationale:** A small advisory change addresses shared-file risk on both platforms before more parallel work is encouraged.

**Current:** Project slots count board-linked work; parallelism above one permits agents to edit the same file. Manually started sessions do not claim board slots.

**Opportunity:** Show the current checkout, board-linked versus other live work, and a concise shared-file warning beside the parallelism control and launch review. Keep this advisory.

**Completion measure:** In a fixture with two board runs and one manual run, both clients distinguish all three and explain that slot counts do not guarantee file isolation.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** The old overlap gate was deliberately removed. Do not restore it or silently change queue eligibility.

**Evidence:** [host/bob_companion_daemon/board_queue.py:1](../host/bob_companion_daemon/board_queue.py); [host/bob_companion_daemon/board_queue.py:33](../host/bob_companion_daemon/board_queue.py); [host/bob_companion_daemon/daemon_board.py:5556](../host/bob_companion_daemon/daemon_board.py).

### R02 · Unify the mobile decision inbox

**Priority:** P1 (rank 2) · **Platform:** Mobile · **Impact:** 5/5 · **Effort:** 5–9 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Triage spans the whole mobile workflow; review it before the narrower away-terminal path even though their effort ranges overlap.

**Current:** Mobile Needs you lists waiting agents and cards carrying the daemon needs_you flag; desktop Inbox models seven decision kinds. Catch up is a separate historical view.

**Opportunity:** Add manual checks, submitted results awaiting review, permissions and ready plans to one mobile decision list, preserving one item per subject and distinguishing blocking work from FYI.

**Completion measure:** A seven-kind fixture gives the same blocking count on desktop and phone; every row opens the current subject and has an age and next action.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Consume daemon freshness flags; do not infer completion from silence or count ready plans as blocked work.

**Evidence:** [ios/BobPhone/NeedsYouView.swift:29](../ios/BobPhone/NeedsYouView.swift); [panel/Sources/BobPanel/Inbox.swift](../panel/Sources/BobPanel/Inbox.swift); [ios/BobPhone/CatchUpView.swift:45](../ios/BobPhone/CatchUpView.swift).

### R03 · Make away terminal input respect its write budget

**Priority:** P1 (rank 3) · **Platform:** Mobile · **Impact:** 5/5 · **Effort:** 4–8 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed mismatch. **Rank rationale:** Potential input loss deserves first-tranche attention, but its away-only scope and unmeasured occurrence place it after unified triage.

**Current:** Away keys become terminal_input writes; only keys accumulated during an in-flight request are batched. The device budget is ten executed writes per minute; failed payloads are removed with a refusal notice.

**Opportunity:** Provide bounded intentional input batching, backpressure and an unsent-input state that distinguishes definite refusal from uncertain delivery. Preserve receipt-based deduplication.

**Completion measure:** Under more than ten input submissions in a minute, every byte is either confirmed delivered or visibly retained/refused; reconnect never blindly replays an uncertain keystroke.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Static mismatch identified; no live packet-loss reproduction. Do not increase security limits as the default fix.

**Evidence:** [ios/BobPhone/TerminalPane.swift:635](../ios/BobPhone/TerminalPane.swift); [host/bob_companion_daemon/relay.py:92](../host/bob_companion_daemon/relay.py).

### R04 · Explain provider and host readiness before starting work

**Priority:** P1 (rank 4) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 4–7 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Explain launch readiness before committing to the larger attempt-history migration; the smaller cross-platform slice also enables recovery diagnostics.

**Current:** Launch guards and per-row capability flags exist, but capabilities differ by provider and terminal ownership. Codex cannot reply or answer permissions through the companion.

**Opportunity:** Add a compact readiness summary for selected provider, project enrollment, host connection and terminal route; name the supported recovery when an action is absent.

**Completion measure:** For unavailable executable, unenrolled project, old extension and Codex-only row, users can identify the reason and a supported next step before attempting work.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Read capabilities from the daemon and preserve identity guards; never manufacture parity by typing into an unproven process.

**Evidence:** [host/bob_companion_daemon/dispatch.py:408](../host/bob_companion_daemon/dispatch.py); [docs/codex-contract.md](../docs/codex-contract.md); [panel/Sources/BobPanel/SettingsMenuModel.swift:769](../panel/Sources/BobPanel/SettingsMenuModel.swift).

### R05 · Retain bounded evidence for every implementation attempt

**Priority:** P1 (rank 5) · **Platform:** Desktop + Mobile · **Impact:** 5/5 · **Effort:** 8–14 engineer-days · **Confidence:** High

**Evidence kind:** Deliberate boundary. **Rank rationale:** Attempt history enables isolation and stage analysis; its retention and migration design make it a larger foundation after immediate operation clarity.

**Current:** open_run replaces the previous card work record. A separate outcome ledger already preserves acceptance and rework decisions.

**Opportunity:** Retain bounded per-attempt records with session/provider, report, baseline and verification evidence references; let reviewers compare attempts without confusing them with the outcome ledger.

**Completion measure:** Restart a card twice; all attempts remain individually inspectable and a late collector cannot overwrite a newer attempt. Retention and deletion behavior are explicit.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Schema migration and retention decision required. Project diffs remain project observations, not proof of one agent authorship.

**Evidence:** [host/bob_companion_daemon/board.py:1324](../host/bob_companion_daemon/board.py); [host/bob_companion_daemon/board.py:1351](../host/bob_companion_daemon/board.py); [host/bob_companion_daemon/work_record.py:67](../host/bob_companion_daemon/work_record.py).

### R06 · Acknowledge reviews and manual checks from the phone

**Priority:** P1 (rank 6) · **Platform:** Mobile · **Impact:** 4/5 · **Effort:** 3–6 engineer-days · **Confidence:** High

**Evidence kind:** Deliberate boundary. **Rank rationale:** Finish existing review workflows after triage is understood; new phone write authority needs a separate decision.

**Current:** Phone reads manual steps and results, but board_review and board_manual_clear are absent from both phone action allowlists.

**Opportunity:** Add separately scoped, version-gated acknowledgments for reviewed results and performed manual checks with current-state confirmation.

**Completion measure:** An authorized phone can acknowledge a current review/check exactly once; stale or revoked requests fail clearly. Acknowledgment never implies outcome acceptance.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** New LAN and away authority requires a separate implementation plan and security review; never clear checks the person has not performed.

**Evidence:** [host/bob_companion_daemon/api_server.py:1895](../host/bob_companion_daemon/api_server.py); [host/bob_companion_daemon/api_server.py:1913](../host/bob_companion_daemon/api_server.py); [ios/BobPhone/CardDetailView.swift:206](../ios/BobPhone/CardDetailView.swift).

### R07 · Give mobile outcome review a clear desktop handoff

**Priority:** P1 (rank 7) · **Platform:** Mobile + Desktop · **Impact:** 4/5 · **Effort:** 2–4 engineer-days · **Confidence:** High

**Evidence kind:** Deliberate boundary. **Rank rationale:** A small desktop handoff makes the intentional acceptance boundary usable while remote acceptance remains a later policy choice.

**Current:** Phone outcome views are read-only; acceptance and revision decisions are deliberately desktop-only.

**Opportunity:** Explain the decision boundary and provide a stable card reference and open-at-desk route carrying the evidence the person was reading.

**Completion measure:** A person inspecting an outcome on the phone can resume the same card and criterion at the Mac without searching; the phone cannot submit acceptance.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Preserve desktop-only decisions. Remote outcome writes are a distinct policy proposal R30.

**Evidence:** [ios/BobPhone/OutcomeScreens.swift:3](../ios/BobPhone/OutcomeScreens.swift); [panel/Sources/BobPanel/OutcomeEditor.swift](../panel/Sources/BobPanel/OutcomeEditor.swift); [host/bob_companion_daemon/api_server.py:1895](../host/bob_companion_daemon/api_server.py).

### R08 · Make priority and queue order understandable

**Priority:** P1 (rank 8) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 3–5 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Clarify both visible and execution order before adding broader planning features; this changes no scheduler authority.

**Current:** Priority is one absolute 0–100 AI suggestion per card using title and summary; board order uses priority while queued work uses queue order. Dragging cannot move a card across unequal scores.

**Opportunity:** Show why the visible order differs from queue order, distinguish suggested versus human-set priority, and offer an explicit human rationale. Keep comparative scoring a later extension.

**Completion measure:** With unequal priorities and a reordered queue, both clients explain which order governs each view; changing a priority does not silently reorder confirmed queued work.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Do not turn suggestions into automatic work selection; preserving scorer provenance needs a small schema addition.

**Evidence:** [host/bob_companion_daemon/card_priority.py:1](../host/bob_companion_daemon/card_priority.py); [host/bob_companion_daemon/board.py:776](../host/bob_companion_daemon/board.py); [host/bob_companion_daemon/board_queue.py:1](../host/bob_companion_daemon/board_queue.py).

### R09 · Add named coordination and review responsibilities

**Priority:** P2 (rank 9) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 6–10 engineer-days · **Confidence:** Medium

**Evidence kind:** Product hypothesis. **Rank rationale:** Begin the second tranche with lightweight human responsibility metadata, whose need must be validated before authenticated roles.

**Current:** Cards retain author and bound agent/session identity, but inspected card schema has no named human assignee or review owner.

**Opportunity:** Add optional coordinator and reviewer labels, handoff notes and explicit next responsibility. Start with metadata for a trusted local workflow.

**Completion measure:** A card can show who coordinates work and who reviews it; reassignments retain a timestamped reason and agent binding is unchanged.

**Dependencies:** None. **Proposed owner:** Product + daemon + both clients.

**Risk or decision:** Human labels are not authenticated identities or access control. Validate need with at least two team members before building.

**Evidence:** [host/bob_companion_daemon/board.py:263](../host/bob_companion_daemon/board.py); [host/bob_companion_daemon/board.py:2141](../host/bob_companion_daemon/board.py); [panel/Sources/BobPanel/Models.swift:666](../panel/Sources/BobPanel/Models.swift).

### R10 · Show dependency chains and their release conditions

**Priority:** P2 (rank 10) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 5–9 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Build on existing blockers to explain handoffs before investing in workspace isolation or richer lifecycle reporting.

**Current:** Cards already support up to eight blockers and reject cycles; Done or missing blockers no longer block. Desktop exposes a list of dependencies.

**Opportunity:** Add an upstream/downstream view and a mobile expandable chain, highlighting the actionable blocker and whether release means Done or an explicitly chosen reviewed/accepted condition.

**Completion measure:** A three-card chain and a deleted blocker are explained correctly; any new release condition is tested separately and existing Done semantics migrate unchanged.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Visualization can ship alone. Gating on review/acceptance changes execution policy and needs its own decision.

**Evidence:** [host/bob_companion_daemon/board.py:230](../host/bob_companion_daemon/board.py); [host/bob_companion_daemon/board.py:2472](../host/bob_companion_daemon/board.py); [host/bob_companion_daemon/board.py:2706](../host/bob_companion_daemon/board.py); [panel/Sources/BobPanel/BoardCardSheet.swift:895](../panel/Sources/BobPanel/BoardCardSheet.swift).

### R11 · Pilot isolated workspaces with explicit integration

**Priority:** P2 (rank 11) · **Platform:** Desktop-led + Mobile status · **Impact:** 5/5 · **Effort:** 15–25 engineer-days · **Confidence:** Medium

**Evidence kind:** Product hypothesis. **Rank rationale:** Review the high-impact isolation pilot early in tranche two, but implement only after concurrency disclosure and attempt evidence.

**Current:** Runs can share a checkout; work records deliberately report project-level changes. Isolation is not supplied by the board launcher.

**Opportunity:** Prototype optional isolated worktrees for a bounded class of projects, with base revision, integration owner and an explicit review/merge handoff.

**Completion measure:** Two test runs edit the same path in separate worktrees; neither changes the primary checkout until a human-approved integration; restart retains ownership.

**Dependencies:** R01; R05. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Estimate is a pilot, not universal isolation. Enrollment, shared databases, build outputs and destructive cleanup require design; never auto-delete dirty worktrees.

**Evidence:** [host/bob_companion_daemon/board_queue.py:1](../host/bob_companion_daemon/board_queue.py); [host/bob_companion_daemon/dispatch.py:408](../host/bob_companion_daemon/dispatch.py); [host/bob_companion_daemon/work_record.py:67](../host/bob_companion_daemon/work_record.py).

### R12 · Distinguish observed specialists from verified stage results

**Priority:** P2 (rank 12) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 6–10 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Stage evidence follows retained attempts and precedes stage-duration analysis; observed participation alone cannot support those claims.

**Current:** workflow declares expected specialists; agent_trail and crew track observed participation. Presence is not a test verdict or proof that the acceptance criterion passed.

**Opportunity:** Attach structured stage outcomes with provenance, attempt, start/end time and linked evidence; show absent, observed, passed, failed and unavailable distinctly.

**Completion measure:** A helper that starts and fails never appears verified; missing stage evidence remains unknown; the card still requires explicit human outcome acceptance.

**Dependencies:** R05. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Agent-reported success must remain distinguishable from checks independently observed by the application.

**Evidence:** [host/bob_companion_daemon/board_workflow.py:1](../host/bob_companion_daemon/board_workflow.py); [docs/card-crew.md](../docs/card-crew.md); [host/bob_companion_daemon/work_record.py:67](../host/bob_companion_daemon/work_record.py).

### R13 · Make queued-plan authorization visible and version-specific

**Priority:** P2 (rank 13) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 4–7 engineer-days · **Confidence:** High

**Evidence kind:** Deliberate boundary. **Rank rationale:** Make the existing authorization tradeoff understandable before considering strict replay behavior; no demonstrated incident elevates it to P1.

**Current:** At queue replay, an existing but changed plan skips the changed-plan confirmation; queue membership records the previous human press. Missing plans still refuse.

**Opportunity:** First expose which plan version was confirmed when queued. Evaluate an opt-in hold-on-change policy with a clear reapproval path.

**Completion measure:** Edit a queued plan: the current policy is visible. Under a separately approved strict mode the card holds for reapproval instead of starting changed instructions.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Documented policy tradeoff, not a newly proven defect. Strict mode must be an explicit behavior decision, not an incidental gate change.

**Evidence:** [host/bob_companion_daemon/daemon_board.py:1084](../host/bob_companion_daemon/daemon_board.py); [host/bob_companion_daemon/daemon_board.py:1111](../host/bob_companion_daemon/daemon_board.py).

### R14 · Add comparable outcome reporting periods

**Priority:** P2 (rank 14) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 5–9 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Comparable periods are the first analytics increment because export and stage comparisons need consistent report scope.

**Current:** Outcome reports already measure acceptance, rework, observed waits and covered cost. Backend supports date ranges; the inspected client request exposes card/root and offset only, and phone project report uses a 30-day view.

**Opportunity:** Expose period selection and side-by-side project/cohort comparisons with identical units, denominators and data-coverage labels.

**Completion measure:** Both clients request matching UTC periods and show the same totals; an empty denominator is unavailable and mixed currencies never combine.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Reacceptance and carry-in rework keep existing semantics; outcome cost must not inherit estimated History pricing.

**Evidence:** [panel/Sources/BobPanel/OutcomeClient.swift:4](../panel/Sources/BobPanel/OutcomeClient.swift); [ios/BobPhone/OutcomeScreens.swift:37](../ios/BobPhone/OutcomeScreens.swift); [host/bob_companion_daemon/board_outcomes.py:114](../host/bob_companion_daemon/board_outcomes.py).

### R15 · Export a redacted report for team review

**Priority:** P2 (rank 15) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 4–7 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Sharing depends on comparable periods and needs field selection; sequence it before wider retention/export policy.

**Current:** Outcome and History views display reports, but inspected client sources contain no CSV/report sharing action.

**Opportunity:** Export selected outcome and run summaries to CSV and a readable document, carrying period, scope, coverage, provenance and explicit field selection.

**Completion measure:** Exported totals reconcile with the screen; missing measurements remain empty/unavailable; paths, prompts and evidence are excluded unless deliberately selected.

**Dependencies:** R14. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Sharing may expose project information. Preview selected fields and neutralize spreadsheet formula cells; no automatic publication.

**Evidence:** [panel/Sources/BobPanel/OutcomeViews.swift:53](../panel/Sources/BobPanel/OutcomeViews.swift); [panel/Sources/BobPanel/HistoryView.swift:9](../panel/Sources/BobPanel/HistoryView.swift); [ios/BobPhone/OutcomeViews.swift:53](../ios/BobPhone/OutcomeViews.swift).

### R16 · Explain where time is lost between workflow stages

**Priority:** P2 (rank 16) · **Platform:** Desktop + Mobile summary · **Impact:** 4/5 · **Effort:** 8–13 engineer-days · **Confidence:** Medium

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Duration analysis follows attempt, stage and period foundations; larger collection work keeps it below simpler reporting improvements.

**Current:** Outcome tracking already records observed waiting causes and rework; the short event diary is bounded and there is no end-to-end stage-duration report in the inspected views.

**Opportunity:** Report queue wait, execution, human review and rework separately with distributions and coverage, using durable lifecycle observations.

**Completion measure:** A seeded timeline reproduces known intervals, excludes sleep/restart gaps and distinguishes elapsed card time from human labor; users can drill into delayed cards.

**Dependencies:** R05; R12; R14. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Do not infer developer productivity from token counts, active time or overlapping card-hours.

**Evidence:** [host/bob_companion_daemon/board_outcomes.py:40](../host/bob_companion_daemon/board_outcomes.py); [host/bob_companion_daemon/event_log.py:56](../host/bob_companion_daemon/event_log.py); [panel/Sources/BobPanel/OutcomeViews.swift:53](../panel/Sources/BobPanel/OutcomeViews.swift).

### R17 · Keep analytical coverage and provider accounting explicit

**Priority:** P2 (rank 17) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 4–7 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Coverage explanation is independently useful and must precede budget warnings; it can run alongside the earlier analytics track.

**Current:** History mixes measured and explicitly estimated prices; outcome metrics accept observed amounts and warn about incomplete child coverage. History UI has Claude/Grok toggles while Codex has a separate accounting source.

**Opportunity:** Provide a compact coverage view explaining provider inclusion, parent/child accounting, missing intervals and measured versus estimated amounts across reports.

**Completion measure:** Fixtures with Codex, unpriced turns, missing children and mixed currencies show exclusions clearly; reports never present incompatible amounts as one bill.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** This adds explanation and reconciliation first; provider billing integration requires reliable source evidence.

**Evidence:** [host/bob_companion_daemon/history.py:21](../host/bob_companion_daemon/history.py); [host/bob_companion_daemon/history.py:1458](../host/bob_companion_daemon/history.py); [host/bob_companion_daemon/board_outcomes.py:18](../host/bob_companion_daemon/board_outcomes.py); [panel/Sources/BobPanel/HistoryView.swift:84](../panel/Sources/BobPanel/HistoryView.swift).

### R18 · Offer project budget awareness without autonomous cancellation

**Priority:** P2 (rank 18) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 5–9 engineer-days · **Confidence:** Medium

**Evidence kind:** Product hypothesis. **Rank rationale:** Budget awareness follows coverage work because partial observations cannot support credible caps or forecasts.

**Current:** Usage limits and cost reports exist. Inspected board schema and priority scorer carry no project spend target or forecast input.

**Opportunity:** Add optional project/card observed-spend targets, coverage-aware warnings and a review of queued work when limits approach.

**Completion measure:** Crossing a configured observed amount surfaces one actionable warning on both clients; missing cost never appears under budget, and no agent is stopped automatically.

**Dependencies:** R17. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Observed partial spend is a lower bound; do not promise a billing cap or forecast from incomplete data.

**Evidence:** [host/bob_companion_daemon/board.py:263](../host/bob_companion_daemon/board.py); [host/bob_companion_daemon/board_outcomes.py:18](../host/bob_companion_daemon/board_outcomes.py); [host/bob_companion_daemon/card_priority.py:78](../host/bob_companion_daemon/card_priority.py).

### R19 · Keep selected result evidence readable offline

**Priority:** P2 (rank 19) · **Platform:** Mobile · **Impact:** 3/5 · **Effort:** 5–9 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Offline evidence extends an existing cache after online review and analytics, without granting offline decisions.

**Current:** Protected caches already retain cards/plans; work-record report and diff screens fetch their own evidence and hold it in view state.

**Opportunity:** Extend the bounded protected cache to deliberately selected result, diff and decision evidence with captured-at and incomplete-content indicators.

**Completion measure:** Open a saved result, lose connectivity and reopen it: cached content is identifiable and readable; uncached evidence is explicitly unavailable and approvals remain online.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Define byte limits, eviction and device-forget cleanup. Never turn cached evidence into authorization for a live write.

**Evidence:** [ios/BobPhone/CardCache.swift:4](../ios/BobPhone/CardCache.swift); [ios/BobPhone/WorkRecordView.swift:127](../ios/BobPhone/WorkRecordView.swift); [ios/BobPhone/WorkRecordView.swift:213](../ios/BobPhone/WorkRecordView.swift).

### R20 · Support multiple mobile drafts and explicit device handoff

**Priority:** P2 (rank 20) · **Platform:** Mobile + Desktop · **Impact:** 3/5 · **Effort:** 6–10 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** Multiple drafts and explicit handoff improve capture but do not unblock existing submitted work; establish them before away file intake.

**Current:** Desktop banks multiple local drafts; mobile has one composer draft slot. Neither local draft store is a shared project board.

**Opportunity:** Add named mobile drafts first, then an explicit handoff of a chosen draft to the other device with attachment status and conflict handling.

**Completion measure:** Two unfinished ideas survive app restart without overwriting each other; a handed-off draft is not mistaken for a submitted card.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Do not silently sync unfinished text or upload attachments; define ownership and explicit handoff semantics.

**Evidence:** [ios/BobPhone/Outbox.swift:317](../ios/BobPhone/Outbox.swift); [ios/BobPhone/Outbox.swift:374](../ios/BobPhone/Outbox.swift); [panel/Sources/BobPanel/Drafts.swift](../panel/Sources/BobPanel/Drafts.swift).

### R21 · Unify recovery diagnostics across host and phone

**Priority:** P2 (rank 21) · **Platform:** Desktop + Mobile · **Impact:** 3/5 · **Effort:** 4–7 engineer-days · **Confidence:** Medium

**Evidence kind:** Product hypothesis. **Rank rationale:** Recovery diagnostics build on launch readiness; actual support demand should select which component checks to expose first.

**Current:** Health status, enrollment feedback, reconnect information and developer probe skills already exist in separate places.

**Opportunity:** Create a user-facing readiness/recovery checklist using existing component health: host, hook/provider evidence, editor bridge, phone link and push delivery.

**Completion measure:** Injected failures for a dead bridge, expired away grant and stale snapshot identify the failed component and a safe next action; secret values never enter diagnostics.

**Dependencies:** R04. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Do not label absent activity as a broken hook. Export diagnostics only after review; no automatic reinstall or restart.

**Evidence:** [panel/Sources/BobPanel/SettingsMenuModel.swift:769](../panel/Sources/BobPanel/SettingsMenuModel.swift); [docs/phone-contract.md](../docs/phone-contract.md); [.agents/skills/daemon-probe/SKILL.md](../.agents/skills/daemon-probe/SKILL.md).

### R22 · Show agent collaboration as a navigable evidence map

**Priority:** P2 (rank 22) · **Platform:** Desktop + Mobile summary · **Impact:** 3/5 · **Effort:** 5–9 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed limit. **Rank rationale:** A collaboration map improves investigation once execution and evidence basics are clear; edge counts do not establish delivery.

**Current:** Mesh joins observed SendMessage recipients, counts and last timestamps. Unresolved recipients are kept; reachability does not mean Dark Army can send to them.

**Opportunity:** Provide a task-focused graph/list of parent, helper and observed communication edges with unresolved and present-but-unreachable states and links to associated cards.

**Completion measure:** A live helper, ended recipient and ambiguous address remain distinct; edges show observed activity only and offer no unsupported Send action.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Counts do not prove successful delivery or causation; never inject into private agent inboxes.

**Evidence:** [host/bob_companion_daemon/daemon.py:7803](../host/bob_companion_daemon/daemon.py); [host/bob_companion_daemon/session_stats.py:53](../host/bob_companion_daemon/session_stats.py); [docs/card-crew.md](../docs/card-crew.md).

### R23 · Make project knowledge easier to inspect and maintain

**Priority:** P2 (rank 23) · **Platform:** Desktop + Mobile read · **Impact:** 3/5 · **Effort:** 5–8 engineer-days · **Confidence:** Medium

**Evidence kind:** Product hypothesis. **Rank rationale:** Knowledge inspection improves maintained project context, but its human workflow needs validation before adding readers and writes.

**Current:** Project-scoped knowledge Q&A already exists through the agent channel; it is outside snapshots and has no general all-project read.

**Opportunity:** Add a project-bound reader and a desktop edit/review flow with provenance, last-confirmed date and explicit stale-note status.

**Completion measure:** A person can inspect the enrolled project knowledge used by agents; another project cannot read it, and archived advice is visibly stale.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** New human UI/read capability needs explicit project scoping. Do not auto-promote agent assertions into standing policy.

**Evidence:** [docs/knowledge-notes.md](../docs/knowledge-notes.md); [host/bob_companion_daemon/knowledge_store.py](../host/bob_companion_daemon/knowledge_store.py); [docs/channel-tools.md](../docs/channel-tools.md).

### R24 · Test dense desktop workflows and accessibility with users

**Priority:** P2 (rank 24) · **Platform:** Desktop · **Impact:** 3/5 · **Effort:** 3–6 engineer-days · **Confidence:** Medium

**Evidence kind:** Research hypothesis. **Rank rationale:** Usability research targets the existing dense desktop experience; its scope and small fixes depend on observed participant problems.

**Current:** Desktop has keyboard triage, scaled layout and a dense rail; mobile already has explicit Dynamic Type/VoiceOver contracts. Fixed-size typography and truncated blocker labels remain in desktop views.

**Opportunity:** Run a bounded usability pass on 20-session triage, long card names, keyboard-only review and VoiceOver; turn observed failures into targeted layout/accessibility fixes.

**Completion measure:** Test participants locate a blocked card and review its evidence; keyboard focus stays predictable and long blocker names are available in full.

**Dependencies:** None. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Discovery estimate includes small fixes only. No assertion that the current UI fails accessibility without interaction testing.

**Evidence:** [panel/Sources/BobPanel/BoardCardSheet.swift:906](../panel/Sources/BobPanel/BoardCardSheet.swift); [panel/Sources/BobPanel/HistoryView.swift:9](../panel/Sources/BobPanel/HistoryView.swift); [docs/panel-window-contract.md](../docs/panel-window-contract.md); [docs/phone-contract.md](../docs/phone-contract.md).

### R25 · Correct the widget refresh expectation

**Priority:** P2 (rank 25) · **Platform:** Mobile · **Impact:** 2/5 · **Effort:** 0.5–1 engineer-days · **Confidence:** High

**Evidence kind:** Source-confirmed mismatch. **Rank rationale:** The wording mismatch is narrow and independent: it may ship immediately as a small fix despite its lower overall review rank.

**Current:** Profile says at least this often when iOS allows; background scheduling uses an earliest refresh time and explicitly allows OS delays.

**Opportunity:** Say the chosen interval is the earliest requested refresh, with iOS deciding actual delivery; retain last-updated information.

**Completion measure:** The settings text promises no maximum staleness and matches the background scheduler contract.

**Dependencies:** None. **Proposed owner:** iOS engineer.

**Risk or decision:** Confirmed source wording inconsistency; no scheduler change needed.

**Evidence:** [ios/BobPhone/ProfileView.swift:133](../ios/BobPhone/ProfileView.swift); [ios/BobPhone/BackgroundRefresh.swift:15](../ios/BobPhone/BackgroundRefresh.swift).

### R26 · Design an away capture and attachment slice

**Priority:** P3 (rank 26) · **Platform:** Mobile · **Impact:** 3/5 · **Effort:** 10–18 engineer-days · **Confidence:** Medium

**Evidence kind:** Deliberate boundary. **Rank rationale:** Away capture broadens transport policy and depends on draft ownership; it is outside the initial coordination tranche.

**Current:** Text/dictation and local capture exist; attachment upload is deliberately LAN-only. The old opportunities document already proposes wider capture.

**Opportunity:** Design explicit share-sheet intake and sealed, bounded away attachment transfer with cancellation, staged-file cleanup and clear delivery status.

**Completion measure:** A deliberately shared file is transferred once under a valid grant or remains visibly unsent; cancellation and device revocation remove access as designed.

**Dependencies:** R20. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Policy and transport expansion; estimate is one file-transfer slice, not arbitrary background synchronization.

**Evidence:** [ios/BobPhone/Client.swift:1150](../ios/BobPhone/Client.swift); [docs/transport-contract.md](../docs/transport-contract.md); [docs/2026-09-05-mobile-orchestration-opportunities.md:48](../docs/2026-09-05-mobile-orchestration-opportunities.md).

### R27 · Define durable operational history and recovery policy

**Priority:** P3 (rank 27) · **Platform:** Desktop + Mobile read · **Impact:** 3/5 · **Effort:** 8–14 engineer-days · **Confidence:** Medium

**Evidence kind:** Deliberate boundary. **Rank rationale:** Longer retention and restore follow attempt history and export; short diary retention is deliberate and no loss incident was measured.

**Current:** Event diary retains at most 500 entries/24 hours; outcome evidence is retained separately. SQLite state and private-file protections exist, but the diary is not a long-term team audit trail.

**Opportunity:** Define separate retention for operational events, attempts and outcomes; add bounded archive/export and a documented backup/restore rehearsal.

**Completion measure:** A restore into an isolated profile preserves card/attempt relationships and never resurrects runnable queue commands without review; expired data follows the published policy.

**Dependencies:** R05; R15. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Do not expand retention by silently keeping transcripts or secrets. Short diary retention is a deliberate bound, not data-loss proof.

**Evidence:** [host/bob_companion_daemon/event_log.py:56](../host/bob_companion_daemon/event_log.py); [host/bob_companion_daemon/board.py:1324](../host/bob_companion_daemon/board.py); [host/bob_companion_daemon/board_outcome_store.py](../host/bob_companion_daemon/board_outcome_store.py).

### R28 · Validate a true multi-person workspace model

**Priority:** P3 (rank 28) · **Platform:** Desktop + Mobile · **Impact:** 5/5 · **Effort:** 8–15 engineer-days · **Confidence:** Low

**Evidence kind:** Product hypothesis. **Rank rationale:** High potential impact does not override unknown demand and authority design; validate roles after simpler responsibility metadata.

**Current:** Current authority is project enrollment and paired-device access to one Mac, not authenticated human membership with per-project roles.

**Opportunity:** Interview a small team and design owner/operator/reviewer/observer roles, audit attribution and revocation before permitting unrelated people to operate a host.

**Completion measure:** A reviewed prototype maps each role to read/start/reply/approve/stop rights and demonstrates device revocation and project isolation.

**Dependencies:** R09. **Proposed owner:** Product + security architect.

**Risk or decision:** Discovery/prototype only; production identity and authorization are unestimated until scope is accepted. Do not share the host write token.

**Evidence:** [docs/transport-contract.md](../docs/transport-contract.md); [host/bob_companion_daemon/api_server.py:1913](../host/bob_companion_daemon/api_server.py); [host/bob_companion_daemon/board.py:263](../host/bob_companion_daemon/board.py).

### R29 · Explore a read-only fleet across multiple Macs

**Priority:** P3 (rank 29) · **Platform:** Desktop + Mobile · **Impact:** 4/5 · **Effort:** 10–18 engineer-days · **Confidence:** Low

**Evidence kind:** Product hypothesis. **Rank rationale:** Multiple hosts follow an agreed human trust model and remain a read-only prototype until identity and routing are settled.

**Current:** The documented topology is one Mac daemon with a paired phone and optional sealed relay, not a federated scheduler.

**Opportunity:** Prototype read-only host grouping with namespaced identities, per-host freshness and host-specific action routing; defer cross-host scheduling.

**Completion measure:** Two hosts with colliding local card IDs remain distinguishable and disconnected state stays per host; the prototype cannot dispatch to the wrong host.

**Dependencies:** R28. **Proposed owner:** Product + platform/security engineers.

**Risk or decision:** Prototype estimate only; discovery must settle identity, trust, enrollment and data placement before production federation.

**Evidence:** [CLAUDE.md:35](../CLAUDE.md); [docs/transport-contract.md](../docs/transport-contract.md); [ios/BobPhone/Models.swift](../ios/BobPhone/Models.swift).

### R30 · Evaluate scoped outcome decisions away from the desk

**Priority:** P3 (rank 30) · **Platform:** Mobile · **Impact:** 4/5 · **Effort:** 8–15 engineer-days · **Confidence:** Medium

**Evidence kind:** Deliberate boundary. **Rank rationale:** Remote acceptance is last because it widens human decision authority; it depends on the desktop handoff and an explicit trust model.

**Current:** Acceptance and revision decisions are intentionally unavailable through mobile actions despite read-only outcome views.

**Opportunity:** Only if remote decision-making is approved, design per-action authority with fresh criterion/revision, visible evidence and an explicit human confirmation.

**Completion measure:** Policy review passes before implementation; stale criterion, revoked grant and pending manual check all refuse; an agent cannot exercise human acceptance authority.

**Dependencies:** R07; R28. **Proposed owner:** Product + daemon + client engineers.

**Risk or decision:** Conditional estimate for a bounded decision path after identity policy exists. No authority expansion is approved by this audit.

**Evidence:** [ios/BobPhone/OutcomeScreens.swift:3](../ios/BobPhone/OutcomeScreens.swift); [host/bob_companion_daemon/api_server.py:1895](../host/bob_companion_daemon/api_server.py); [host/bob_companion_daemon/board_outcome_store.py](../host/bob_companion_daemon/board_outcome_store.py).

## Measuring whether the roadmap helps

Before each tranche, record a small baseline from the same representative projects: time to find and answer a blocked request, time from submitted result to review, number of refused/uncertain mobile actions, repeated run attempts and percentage of outcomes with usable evidence. After delivery, compare like-for-like scenarios and report sample size and missing observations. Do not claim improvements in team productivity from more tokens, more agents or more parallel slots.

Keep a clear distinction between card Done, review acknowledgment and accepted outcome. The existing outcome ledger already encodes that distinction. Use its existing rework cohort, coverage, currency and wait semantics; R14–R18 should make those measurements usable without quietly changing their meaning.

## Validation

The next two subsections preserve evidence that was already present when the accepted-plan run began. Their counts and reviewer verdicts are historical reports, not checks executed by this implementation. Current-run checks are recorded after them.

### Historical initial assessment

Artifact checks passed: 30 unique consecutive ranks, nonempty fields, impact/effort bounds, valid dependency IDs, existing evidence paths and line bounds, document links, and matching document/CSV opportunity IDs. All 14 deterministic plan preflight checks passed without BLOCK or WARN.

The full host command, `host/.venv/bin/pytest -q host/tests`, finished with **6,885 passed, 27 failed, three warnings** in 415.33 seconds. Twenty-six failures in `test_notify_script_matches_protocol_converter` came from an inherited `BOB_COMPANION_ORIGIN` marker appearing in the hook output. The other failure, `test_fleet_chip_order_and_size_are_unchanged`, expects a literal `.sorted()` in a phone property that concurrent work moved into `FleetProjects.names`.

Reran the affected test files and documentation size checks from `host/` with `env -u BOB_COMPANION_ORIGIN .venv/bin/pytest -q tests/test_notify_script.py tests/test_phone_fleet_and_board_legibility.py tests/test_claude_md_size.py`: **117 passed, one failed** in 34.63 seconds. The origin-related failures cleared; the concurrent phone source-pin failure remains. This is **not a green full-suite gate**, and no known-failure allowance was applied. This audit did not change the phone source or its tests. Test logs are local temporary artifacts, not included in the shared report.

Swift builds, simulator/device checks, packaging, installed-runtime probes and a security audit were not run: this task changes documentation only. No commit, installation or release was performed. GitNexus symbol impact was inapplicable because no code symbol was edited; graph change analysis before commit was inapplicable because no commit was requested. Graph navigation's coverage limits are stated above.

During the original assessment, both attributed board tools refused with “Dark Army could not tell which session asked.” No card was created or attached by that attempt; no generic board write was used. Later TODO entries record a separate recovery that attached this plan in Backlog. Suggested review-card title: **Review the team-agent orchestration roadmap**. Summary: **Review 30 prioritized desktop and mobile opportunities, select the first delivery tranche, and confirm the team's shared roadmap location.**

Source-backed findings describe observed implementation; proposed effects and estimates remain hypotheses until tested. The documented/estimated/ranked portion of the success criterion is met; shared-team accessibility remains unverified.

### Historical independent handoff validation

A follow-up review found these artifacts already present and preserved their roadmap content. It independently validated all 30 rows, required values, estimate bounds, dependency IDs, evidence paths and document headings. The independent verifier additionally matched every CSV detail against the document and ran all 14 deterministic preflight checks without findings. A fresh GitNexus rebuild reported 30,873 nodes, 103,618 edges and 621 flows; the large-file, callable-candidate, cross-language and flow-enumeration limits remain unresolved coverage limits.

The verifier reported **Documentation verification: PASS**, but its formal **VERIFY VERDICT: FAIL** followed a stale standing hook allowlist that excludes `ctypes`. `ctypes` is part of Python's standard library and is documented in the current architecture; this is a verifier-guidance discrepancy, not a documentation change regression. No product code or verifier policy was changed.

**Success criterion: CANNOT TELL** — all 30 identified opportunities are documented, estimated and ranked, but another team member’s access to a shared destination remains unverified.

The document connector again returned no connected sessions, and both attributed board tools again refused session attribution. No cloud publication or board attachment occurred. The shared-team portion of the objective remains unverified. The follow-up's environment-isolated targeted command reproduced **117 passed, one failed** in 35.26 seconds: the remaining failure is the phone's inline-sorting source assertion described above. The follow-up full command `host/.venv/bin/pytest -q host/tests` completed with **6,891 passed, 27 failed, three warnings** in 450.88 seconds. The failures are the same 26 inherited-origin hook cases and one concurrent phone source assertion. The extra six passing cases reflect the project-selection tests now in the working tree. This remains a failed full-suite gate; the earlier run is retained above as a historical check.

### Accepted-plan run: 9 September 2026

Preserved the 30 existing IDs, ranks, estimates and proposed scopes, added explicit evidence kinds and per-row ranking rationales, reconciled changed mobile capabilities, and corrected drifted source references. GitNexus navigation preceded current source reads; no product symbol was edited. Source checks reconfirmed the mobile action boundaries, away input batching/write budget, project-level work-record attribution, queue replay policy and widget wording mismatch. These are static checks, not runtime reproductions.

The implementer’s stdlib CSV check passed for all 30 rows: required fields, consecutive unique ranks, numeric bounds, every detailed field matched against the document, dependency IDs, 68 unique source references with existing paths and valid line bounds, all document links, and absence of formula-leading CSV cells. `git diff --check` also passed. The root session independently checked the dependency graph for cycles.

The root session reports that all 14 deterministic preflight blocks passed without BLOCK or WARN. Its fresh document-connector query found no connected sessions; its attributed plan-attachment attempt returned “Dark Army could not tell which session asked.” No duplicate card or generic board write was used. A previous recovery is recorded in TODO, but this run did not establish the live card’s current column. The fresh full-host gate and independent artifact verification follow.

The exact required command, `cd host && .venv/bin/pytest -q`, exited 1 with **6,923 passed, 26 failed and three warnings** in **457.68 seconds**. Every failure was `test_notify_script_matches_protocol_converter` at `tests/test_notify_script.py:552`: the inherited `BOB_COMPANION_ORIGIN` adds an origin field absent from the converter fixture. The historical phone sorting assertion did not fail in this run. This remains a failed full-suite gate, with no known-failure exemption.

The affected-file command from `host/`, `env -u BOB_COMPANION_ORIGIN .venv/bin/pytest -q tests/test_notify_script.py tests/test_phone_fleet_and_board_legibility.py tests/test_claude_md_size.py`, initially produced **117 passed, one failed** in **35.72 seconds**. The hook and phone checks passed; this run's TODO addition exceeded the 150,000-byte document ceiling (150,611 bytes). Condensing this audit's duplicated TODO history repaired that documentation regression; historical results remain in this document.

The identical affected-file command then exited 0: **118 passed in 35.67 seconds**. This validates the TODO repair and environment diagnosis; it is not a repeat of the full suite.

Independent artifact verification checked all 30 detailed entries and ranked summary against the CSV, 68 source references, 107 document links, dependency acyclicity and formula-leading cells. Byte-compilation, platform/header checks, cast parity and the inspected AppKit coroutine sites passed. Baseline hashes show only this document, its CSV and TODO changed; the accepted plan and all product files are unchanged.

Formal verification also records two existing guidance discrepancies: the accepted plan's manual-access criterion lacks numbered steps and a `Why not automated:` line, supplied in this document's sharing instructions; and the standing hook allowlist rejects `ctypes`. The verifier independently imported `ctypes` and compiled the handler under system Python 3.9.6, confirming standard-library compatibility. The accepted plan and verifier policy were preserved. No Swift build, simulator, packaged-runtime or security audit was required for the documentation delta; bug audit was not advanced past the formal verification failure. No commit was requested, so pre-commit graph change analysis was not run; GitNexus MCP was unavailable and source navigation used its CLI.


Final independent verdict: **Documentation verification: PASS. Shared-team access: MANUAL. VERIFY VERDICT: FAIL.** The formal failures are the exact host gate, stale hook allowlist and accepted-plan manual-check format; no remaining documentation-content defect was found.

**Success criterion: CANNOT TELL — All 30 identified opportunities are documented, estimated for effort and impact, and ranked, but another team member’s access to and ability to edit the shared register remains unverified.**

## Review and sharing follow-through

1. Open this document and the CSV; select the first tranche and replace proposed disciplines with accountable people.
2. Have another team member open the chosen repository/shared-document location and confirm they can read and edit the register.
3. Create individual implementation plans for selected IDs, preserving the cited guardrails and running symbol impact analysis before code edits.

Why not automated: roadmap selection and team access depend on human priorities and an actual shared destination. No connected document session was available, and this audit did not publish repository changes.
