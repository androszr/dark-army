# Mobile decrypt validation — 9 September 2026

Implementation of the *mobile decrypt animations* plan. The accepted
criteria are unchanged. Physical-device visual and accessibility checks remain
unverified; simulator unit tests do not establish perceived visual quality.

## Interaction inventory

Every app-owned native Button is a `DecryptButton`: its body retains a native
SwiftUI Button, role, enabled state, style and label. Its action invokes the
original callback at once and does not start an INPUT scramble. Allow and Deny
are that wrapper and still answer at the tap. No press recognizer, action delay
or label replacement was introduced. The source inventory in
`host/tests/test_phone_decrypt_motion.py` rejects any unclassified new Button.

| Family | Integration and boundary |
|---|---|
| Four tabs, initial unlocked arrival, resumed drafts and deep links | `PhoneTabRoot.decryptSurface`, gated per stack by `selectedTab` and notification presentation. `ContentView` routing ownership and draft identities remain unchanged. |
| Agent, card, composer, profile | Destination `decryptSurface` beside the literal title. A UIKit appearance observer reports completed arrivals and departures, including back navigation. |
| Pipeline, Catch up, saved decision, work-record file | Destination `decryptSurface`; live saved decisions delegate to the existing card/agent destination. |
| Notification presentation/dismissal | Existing notification sheet and nested card/agent/Catch-up/saved decision destinations; presenting the sheet gates the underlying tabs, dismissal restores the visible stack. |
| Needs you | Done/send back and answer bars use the semantic wrapper; links receive destination feedback. |
| Fleet and Recently | Project chips, fold/history controls use the wrapper; agent/card links receive destination feedback. |
| Board and outbox | Column chips use the wrapper; the shared changed-column observer emits one screen episode for chips and native page swipes, replacing chip feedback. Folds, Clear Done arm/confirm, archive retry and outbox Retry/Remove use the wrapper. Exact-scope count/token guards unchanged. |
| Pipeline | Queue controls, limit selections, autostart and Start project use the wrapper; existing guards and receipts unchanged. |
| Details/Terminal | Tab arrival starts one OPEN episode; size controls stay `DecryptButton` and do not start INPUT. Decoration is in screen chrome, never inside the emulator. `DetailTab`, watch rules, coordinator and stream untouched. |
| Card details | Blocker clear/select, column/tool/model menus, Start/Start here, Done, Refine, auto-start, delete, plan fold/approval, message/edit/conflict choices use wrapper. |
| Composer | Save, Save/refine, auto-start, draft confirmation, project/model/tool menu selections, project revert, Prepare, attachment removal use wrapper. An explicit Add photos button launches the native picker; its persistent presentation binding emits one coalesced return on selection or Cancel. Upload selection handling is unchanged. |
| Profile | Receipt retry/discard and Forget confirmation use wrapper; the widget picker binding does not start INPUT. |
| Catch up and outcomes | Retry, Load more, Mark caught up, Earlier decisions use wrapper. Time, project and disclosure bindings do not start decoration. |
| Pairing/lock/microphone | Public pairing/lock arrivals and app-owned pair/unlock/microphone callbacks use decoration. Camera preview, biometric dialog, keyboard, native menu/photo picker and system dialog contents retain native rendering. Menu selection callbacks emit; cancelled native menus perform no action. |
| Deliberate refresh | Nine closures: Needs you, Fleet, Board unavailable, Board rows, Usage, Pipeline, Catch up, agents report, timing report. Each awaits the existing operation and does not begin decoration. The native pull indicator stays. |
| Background work | Client polls, reconnects, post-write reads, cache sync, outcome timer and terminal bytes have no decoration call site. |

## Mechanism and limits

`DecryptMotion` is the actual pure Swift model compiled by both the app and host
runtime test: screen 1.5 seconds, 20 Hz maximum glyph updates, at most 24 ASCII
glyphs and a final 0.2-second exact-caption hold. The model still carries button
(1.2s, `INPUT`) and refresh (1.5s, `REFRESH`) so the reducer stays total; no
control begins them. The UI caption is `OPEN`, never request text or success
claims. One screen episode is retained; arrivals supersede, generations prevent
old callbacks from clearing a new surface. No episode queues, new network
polling, dependency, raster surface or asset.

Only decorative views observe the finite clock. The OPEN caption is drawn only
while that episode is live; idle chrome reserves no inset. Decorations are
silent and noninteractive. Reduce Motion shows the finished caption and sleeps
directly to the deadline. Scene inactivity, lock,
unpair and actual disappearance clear episodes immediately; functional navigation,
draft state and terminal lifetime remain owned by their existing code.

`AlarmOutline` needed no modification: the reusable action wrapper supplies its
finite response in surviving screen chrome while preserving its pressed dimming,
labels and geometry. Shared palette, chatter, markdown, specialist, outcome and
DetailTab copies remain unchanged by this task.

## Measured evidence

- Baseline: `/tmp/mobile-decrypt-baseline.diff` and `.status`; concurrent cast,
  project selection, roster/model and source-pin edits preserved.
- Fresh GitNexus index: 30,908 nodes, 103,685 edges, 621 flows. Graph reports
  unresolved cross-language fields and incomplete process enumeration; absence is
  not proof of no caller. File impacts were CRITICAL (shared declarative/import
  graph). File-qualified symbol impacts with limit 1000: AgentDetailView,
  PhoneDetailTabBar, AnswerBox, PhoneCardDetailView and PhoneTerminalPane HIGH;
  direct callers include Fleet.group, Pipeline.rowDestination, Recently.row,
  Board.cardLink, NeedsYouView and DecisionDetailView. Board page/cardLink and
  recent/pipeline row process families are affected. Warnings reported before
  relevant edits. UNKNOWN app entry/outcome/work-record section checked against
  their actual Swift definitions/call sites.
- Xcode 26.6 (17F113), Swift 6.3.3; iOS 26.5 simulator available, iPhone 17 Pro
  `F77BF5F6-EAAC-4795-825C-7CF7A4178659`.
- First native `build-for-testing`: exit 0. Picker repair targeted gate: 53 passed (runtime, upload integration and doc limits).
- Focused host checks: 175 passed in 1.42 seconds, including the actual Swift
  projection/reducer, integration inventory and existing action/accessibility/text/
  theme/detail-tab/terminal contracts (`/tmp/decrypt-targeted.log`).
- Desktop: 914 tests passed; `swift build -c release` exited 0.
- Full native scheme: 164 app + 25 widget tests passed, exit 0, using serial
  execution with actual simulator-wide `accessibility-extra-extra-extra-large`.
  `content_size` was restored to `large` in a `finally` block. Result:
  `/tmp/BobPhone-decrypt-ax.xcresult`; log `/tmp/decrypt-ios-ax.log`.
- Earlier hosted tests failed because a scene-less UIWindow never appeared;
  attaching UIWindowScene fixed all appearance/layout failures. The initial
  SwiftUI path test counted departure cancellation as a second episode; it now
  accounts for cancellation and asserts no replay once settled. A later full
  runner was killed before XCTest bootstrap while concurrent native work shared
  the environment; the following serial full run passed.
- Dedicated simulator `87CECC9D-0232-4553-BE3E-B00D4673EF26` and derived data
  `/tmp/BobPhoneDecryptDerivedData` isolate final fixture verification from other
  sessions. Full host first run: 6,945 passed / one documentation-size failure; the paragraph was condensed without changing its rules, and all eight size checks pass. The independent verifier subsequently ran the full host suite: 6,948 passed in 476.73s. Final focused native repair run: all 13 decrypt tests passed, exit 0,
  `/tmp/BobPhone-decrypt-repair.xcresult`, on dedicated simulator at global AX XXXL,
  restored to `large`. The isolated full run's one screenshot failure was a 100ms
  appearance timeout under load; its fixture now waits up to 2s for the actual
  UIKit callback. The independent verifier subsequently passed all 190 app/widget tests before the Board pager repair below.
- Hosted screenshots at 320/375/430 points contain synthetic public test content
  only. Local trait overrides alone did not change UIFontMetrics; a subsequent
  simulator-wide AX XXXL run confirmed real scaling. That run exposed synthetic
  VStack compression, so the final fixture now uses a ScrollView like the app's
  pages and preserves full prose. Earlier attachments remain in
  `/tmp/decrypt-ax-screenshots`; final six fixture attachments are in
  `/tmp/decrypt-final-screenshots/manifest.json`. Inspected the final 320pt AX
  capture: caption fits, prose wraps in the scroll view without ellipsis, and
  outlined/plain labels retain their text. No frame-pacing
  recording or physical-device observation is claimed.

## Unverified observations — required manual checks

1. Open the built app on a small and large iPhone, record all four tabs, agent
   Details/Terminal/back, card/file/composer/Profile/Pipeline/Catch up and notification
   open/dismiss. Activate representative outlined/plain, filter/menu/microphone and
   arm/confirm controls with disposable data, then refresh all seven sites with
   unchanged data and offline. Repeat landscape and largest accessibility text.
   Check 1.2/1.5-second settling, immediate actions, truthful errors, wrapping,
   reachability and smooth green presentation. Device model, OS, orientation, text
   size, video timestamps and observed frame pacing: **not measured**.
   Why not automated: perception and interaction on physical phone screens cannot
   be established by deterministic glyph or simulator layout tests.
2. Enable Reduce Motion and VoiceOver, repeat screen/button/refresh interactions;
   verify static silent decoration and intact labels. Disable Reduce Motion,
   refresh then change tabs/back/app, lock mid-sequence, cancel a back swipe and a
   dragged-off press, and leave a page idle for 30 seconds. Check no stale replay,
   no protected outgoing image, preserved drafts/terminal and quiet automatic polls.
   VoiceOver focus, cancelled native gestures and app-switcher privacy image:
   **not observed**.
   Why not automated: native accessibility focus, interactive transition gestures
   and protected app-switcher capture need operating-system presentation review.

## Board pager audit repair

The audit found that page-style column swipes lacked an explicit event. The
existing changed-column observer now shares `BoardView.columnSelectionChanged`
between native pager commits and chips. It is gated by active tab/scene, rejects
unchanged selections and replaces the chip's INPUT with one 1.5s OPEN episode.
Only chip assignment and the pager binding write the stored column; poll reads
remain silent. Native/model and source-inventory regressions added. This small
repair followed the green full host (6,948) and full native (190) runs. Post-repair
focused host checks passed 46 tests; the two new native model/hosted-selection tests
passed on the dedicated simulator (`/tmp/BobPhone-decrypt-board.xcresult`,
`/tmp/decrypt-ios-board.log`). Physical swipe feel remains part of the manual check.

The independent verifier then passed 80 affected host checks and all 15 native
decrypt tests, including the hosted pager regression:
`/tmp/BobPhone-decrypt-verifier-board-final.xcresult`. Bug audit round two:
SHIP, score 0, no functional blockers. Integration review: CLEAN. Security review:
CLEAN, including a final recheck of picker return, Board selection and root owner
lifetime; no capability or security regression found.

**The pager is gone** (the *board rows instead of columns* plan,
12 Sep 2026). The phone's board is four stacked, foldable rows in one
scroll — the Mac's own shape — so there is no `TabView`, no chip strip, no
stored column and no `columnSelectionChanged`; the two native pager tests
and their fixtures went with it. A row's fold toggle is an ordinary
`DecryptButton` press, emitting **no** screen episode and no INPUT scramble:
nothing changes screens, the row opens or closes in place. The flipped set
rides `@AppStorage("board.rowFlips")` through `BoardRowFold`, the panel's
rule copied byte-equal into `ios/BobPhone/BoardView.swift` and pinned by
`host/tests/test_board_rows.py`; `test_phone_decrypt_motion.py` now pins the
absence of the pager and the shape of that press.

Product verification is PASS-WITH-MANUAL. The strict reviewer script separately
reports FAIL because its old import whitelist omits `ctypes`; the hook was not
changed here. Independent extraction found no non-stdlib imports, and system
Python 3.9.6 imported `ctypes` successfully. This is a reviewer-checklist defect,
not a waived test failure. No checklist or hook code was changed by this work.
