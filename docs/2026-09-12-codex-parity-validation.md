# Codex parity validation — 2026-09-12

## Follow-up: human acknowledgement of a finished native Codex terminal

The user reported the missing **Acknowledge & close terminal** action after
installing the prior work and explicitly requested repair. The installed
18:22:05Z artifact matched the new clean baseline's parser/daemon/board bytes;
the project connection at port 51235 reported extension 0.1.15. This was a
code-contract gap: `can_close` required Codex `can_stop`, available only for
explicit resume. Ordinary fresh native sessions had exact navigation proof
but no close permission. `/tmp/codex-ack-close-runtime.json` is the parent's
read-only inventory; these installed facts supersede the earlier inventory
only for this follow-up. No installation or terminal disposal was performed
by this repair.

The existing human action now admits an affirmative stopped native root,
without attachment receipt. Snapshot reach uses cached navigation proof,
settled parsed root/descendants and a unique compatible editor connection.
The click rechecks complete journals, turn IDs/times, questions/helpers,
record generations and exact process/journal/tty ownership twice; its final
validation runs immediately before strict addressed bridge disposal. New
input invalidates earlier completion even before a new turn-start event.
Only confirmed disposal retires the row and enables existing human card
completion. Stop, typing, Wrap up, automatic close callers and private
planning receipts keep their authority. No public proof field was added.

Baseline: `/var/folders/3s/lc4qb74d0g77tn5x31pz_s3h0000gn/T/codex-ack-close-baseline-ysaflki7`.
Graph refreshed before edits; impacts on parser/enrichment/close/bridge were
LOW. Public close and bridge each had one unresolved dynamic receiver;
API/client callers were checked in source. Global process discovery remains
truncated, so this is not an exhaustive all-clear.

| Follow-up gate | Result |
|---|---|
| Red reproduction (`test_codex_human_close.py`) | 12 failed, 15 passed; `/tmp/codex-ack-close-red.log` |
| First implementation run | 1 failed, 26 passed; fake holder fixture wrote an unused fake field. Corrected it to remove the holder from the process scan; `/tmp/codex-ack-close-green.log` |
| Focused close/parser/refinement/parity | 468 passed in 4.01s, including 42 new cases; `/tmp/codex-ack-close-focused.log` |
| Final independent gates | Python **7348 passed**, 3 warnings, 577.94s; focused **1086 passed**; Swift **969 passed**; release build exit 0 in 140.04s; compileall and standing checks passed |
| Final strict bundle | Exit 0, **18:51:30Z**, extension 0.1.16; deep/strict signature, seven packaged Python modules, skill copies and helpers verified; integration has no BLOCK, live click remains NOT RUN |
| Final audits | Bug audit SHIP, score 0, 11 independent narrow tests; integration WARN only for the unrun live click, no BLOCK |
| Final original documentation check | Concurrent planning additions grew TODO to 154723 bytes: 1 failed, 7 passed. Moved four oldest 6 Sep sections verbatim to TODO-archive.md under the existing size policy; rerun **8 passed**, TODO 146271 bytes. No product source changed |
| Native user click / exact tab disappears | NOT RUN; no live terminal close authorized in this implementation |

Final gates used the immutable snapshot `/var/folders/3s/lc4qb74d0g77tn5x31pz_s3h0000gn/T/codex-ack-close-final-easpudxn/bob-companion`; its `host/dist/Bob Companion.app` is the final artifact. All 1466 captured files stayed unchanged except the regenerated snapshot VSIX. Logs: `/tmp/codex-ack-close-verify-{full,build}.log`. The original checkout received only final evidence bookkeeping afterward; later concurrent edits are excluded from these test results. Installed app remains **18:22:05Z**, without this repair, as confirmed by `/tmp/codex-ack-close-runtime-final.json`.

Focused command: `cd host && .venv/bin/pytest -q tests/test_codex_human_close.py tests/test_close_terminal_button.py tests/test_codex_rollouts.py tests/test_codex_session_parity.py tests/test_board_refine.py`.
Existing `WrapUpReachTests` already covers a Codex row with `can_close=true`;
the panel client already sends `by_person=1`, so no Swift behavior changed.

1. With the repaired artifact loaded and an editor bridge at 0.1.12+, finish a
   disposable native Codex task, open its details, press **Acknowledge & close
   terminal**, confirm, and verify only that terminal disappears. A session
   running a new turn or holding a question/helper must remain open.
   Why not automated: isolated tests prove selection and refusal; a real
   on-screen button and editor terminal disposal require a deliberate human
   action against the loaded app.

## Earlier parity implementation evidence

The source repairs have deterministic replay coverage. Installed parity is
**not verified**: no app was installed or relaunched, no fresh disposable
Refine/Start journey was run, and no on-screen or phone latency was measured.
The live observations below distinguish the old running application from the
edited parser and from the newly built bundle.

## Baseline and scope

Accepted implementation: the *codex session parity* plan; its
acceptance criteria meanings and checkbox status were not changed; only the
manual instructions were formatted with visible steps and standalone reasons. Branch `main`, HEAD `aec8b17`.
Before edits, binary tracked/staged patches, branch/status, untracked paths and
SHA-256 hashes of tracked and untracked files were saved outside the checkout:
`/var/folders/3s/lc4qb74d0g77tn5x31pz_s3h0000gn/T/codex-parity-baseline-x4ci3qey`.
Unrelated pre-existing work is excluded from the implementation delta.
Concurrent Grok, navigation, panel-reveal and documentation-archive edits were
also preserved and excluded from parity ownership. The only parity-owned
`daemon.py` hunk is the `_refinement_busy` call to the new refusal helper. Baseline-relative review artifacts
are in `/tmp/codex-parity-review-delta/`; each original file was hash-validated. The
build's regenerated extension archive is restored to its original saved bytes
after bundle inspection; the built bundle retains the artifact it validated.
No commit, push, installation, release, live board mutation or terminal closure
was performed by this implementation.

GitNexus was refreshed with `GITNEXUS_MAX_FILE_SIZE=2048` (the default omitted
`daemon.py`): 34,378 nodes, 121,693 edges, 615 flows. Upstream edit impacts:
`parse_rollout` LOW/14 (usage, load_recent, refinement-close observation);
`load_recent` LOW/15 (refresh, confirmation, Stop settling);
`CodexRecord` LOW/5; `refine_prompt` LOW/4 (guard/refine/create);
`_record_card_stages` and `_consider_work_record` LOW/1 each through reconcile.
Global process discovery is truncated: the listed flows are not exhaustive.
The parser still reaches private close and Stop proofs. Test-function impacts
returned UNKNOWN; source search confirmed pytest-discovered functions, and
only the nickname/task-as-role assertions changed to the accepted contract.
No graph zero was interpreted as evidence of no callers.

The first complete full suite exited 1: 3 failed, 7263 passed in 461.13s.
The implementation added enough CLAUDE.md text to exceed its byte ceiling;
its own additions were trimmed and the eight size checks pass. Two failures
were present in unchanged baseline bytes: the orphan summary in NeedsYouView
had no accessibility roster entry, and the baseline-untracked LifecycleReport
force-unwrapped a fixed UTC timezone. The plan file list now names these exact
required full-suite seams; criteria are unchanged. A justified count-one
text-only roster entry preserves the control audit, and both byte-pinned Swift
files use `TimeZone.gmt`. Original untracked Swift bytes were saved with the
baseline. Graph impact for utcPeriod was LOW/6 through report bounds/load;
the mirrored symbol and IGNORE_SITES were UNKNOWN, confirmed through direct
source/test references. At that revision, the affected 166 Python checks and 966 Swift tests passed. These are narrow baseline gate repairs, separate from Codex behavior.
The verifier role's explicit hook-import allowlist also omitted the existing
Python 3.9 stdlib `ctypes` dependency used for macOS process inspection. Its
documentation now allows that exact module; no hook code or third-party/package
allowance changed. The plan file list records this stale gate-specification repair.

## Audit repair round

Review found three concrete gaps after the prior full passes: an accepted
path-only helper with no discovered child journal could permit private close;
a native relative follow-up failed to reactivate its completed child; and a
running grandchild disappeared behind its completed parent. No live close was
attempted. Native call/ack tests reproduced the first defect (10 failed, 3 passed)
and three-generation tests reproduced the display loss in both file orders.

The repair records successful canonical follow-up paths, retains private
journal completeness, request-call timestamps and latest turn start/ID/completion,
and shares one conservative settlement rule between live merge and
cached and freshly parsed close guards. A unique matching child must start a
new turn after the successful request call and explicitly complete that turn; missing,
malformed, incomplete, ambiguous or merely chattering children cannot release
close. Unresolved relative replies block without inventing a child identity.
Rejected spawns create no role or row. Active descendant rows and observed
roles fold through completed ancestors independently of journal order; no
completed ancestor is made live. Graph impacts: private observation LOW/1,
`_refinement_busy` LOW/4, `load_recent` LOW/15. The daemon delta is only the
new busy-guard call; concurrent Grok hunks remain excluded from ownership.

The final focused repair gate passed 521, including all 95 journey cases.
Security review additionally reproduced five causal timing failures: a fast
new turn completed before its delayed acknowledgement was revived, while an
older turn ending after acknowledgement could wrongly settle a queued follow-up.
The shared rule now handles both orders, mismatched IDs and missing request/turn
timestamps. Private `path_requests` and UUID-followup `child_requests` carry call
times; latest `turn_started_at`, `turn_completed_at` and `turn_completed_id`
provide matching new-turn evidence. Legacy UUID spawns without follow-up retain
their existing behavior. An initial overbroad legacy application caused six
focused failures; it was narrowed before the final 521-pass run. Native relative
follow-up fixture provenance is 0.154.0 at 17:40:16.602–17:40:17.378Z; child
terminal/failure/malformed variants are synthetic. The final audited snapshot results below supersede the earlier revision gates.
Earlier failures and passes remain recorded as historical evidence. Logs: `/tmp/codex-parity-path-close-before.log`,
`/tmp/codex-parity-path-close-after.log`, `/tmp/codex-parity-subtree-before.log`,
`/tmp/codex-parity-repair-focused.log`, `/tmp/codex-parity-repair-journey.log`,
`/tmp/codex-parity-causal-before.log`, `/tmp/codex-parity-causal-focused.log`.

## P01–P14 source replay

`host/tests/test_codex_session_parity.py` contains 95 isolated cases. It uses
real parsing, daemon snapshot/category/board adapters and temporary stores;
only external process, editor, launch and filesystem-location seams are faked.
No test writes to the running board or launches a paid session.

| ID | Result | Evidence and limit |
|---|---|---|
| P01 | PASS — source | Refine captured through both spawn routes; one argv prompt, planning-only project skill path, full brief/objective/attachment exactly once; inherited identity cleanup; existing dispatch gate covers allowlist/model/bind guards. Refine intentionally retains default model; a card model applies to Start. |
| P02 | PASS — source/native pending | Actual 0.154.0 async call/ack + child metadata replay into waiting snapshot, question wins over helper, no typing/channel, original-session guidance. Synthetic sync and explicit text fallback also replayed. Fresh model interview not run. |
| P03 | PASS — source | Ack/unrelated/unknown/stale outputs preserve pending; matching answers/cancel/user reply/new turn/abort release it. Object/JSON-string aliases covered. Only pending+ack have native capture; reply/cancel transitions are synthetic. |
| P04 | PASS — instruction/state contract | Zero-question assessment publishes no invented role; local/rendered skills require at most three material questions, recommendation assumptions and answers/objective handoff. Model compliance remains a live check. |
| P05 | PASS — source/native metadata | Canonical role wins over task/nickname; native task-path-only spawn result joins child agent_path. No task path becomes a child UUID or workflow role. |
| P06 | PASS — source | Fast completed/aborted stages survive reconcile/store reopen with stable faces and no live helper rows; failed spawn invents none; duplicate journals/cache copies isolated; ID/path and native relative follow-ups reactivate observed roles; live descendants remain visible through completed ancestors and settle without losing history. |
| P07 | PASS — source | Bound/refining and authored-card attachment use the same card; second attach refuses without duplicate; wrong session refuses. Skill tests cover absent tools and preflight BLOCK with no attach/implementation. Native attribution refusal/recovery not replayed as a live session. |
| P08 | PASS — source | Plan-first Start ignores leftover idea; changed/missing plan gates hold; full project queues candidate in Backlog without spawning. Existing dispatch/refine regressions also run. |
| P09 | PASS — source | Observed implementer/verifier/auditor drive retained history; milestone prose publishes; skipped integration review is not recorded. Skills preserve existing repair caps. Fresh model role sequence not run. |
| P10 | PASS — source | Report retained across commentary/tool/synthetic context, cleared by next real user prompt; finished row and actual work-record queue preserve it; Swift renders it once. |
| P11 | PASS — source | Session end/report do not declare Done; another session cannot close; attributed close writes actual Done. Tool absence/refusal/manual-check wording pinned in local/rendered skills. |
| P12 | PASS — source | Real receipt/close adapter permits exactly one valid request; absent/expired receipt, pending async ack, helper, new turn, old bridge, changed tty/process and lost response refuse without clear/signal fallback. Unresolved successful path receipts and malformed child journals refuse; a later matching terminal child releases. Existing full close matrix covers further ownership races. |
| P13 | PASS — source | Partial/malformed tail, async payload validation, duplicate/same-cwd roots and deep-copy history isolation; existing Codex regression suite covers uncertain process inspection, restart/admission and exact resume. No controls fabricated. |
| P14 | PASS — source | Claude transcript and Codex journal use actual snapshot/reconcile/store paths for report/history; explicit Codex capability/guidance differences preserved. Fresh Claude reference not run. |

Fixtures in `host/tests/fixtures/codex_session_parity/` retain only native
session metadata, first task start, one spawn/result, one async call/ack and
one relative-follow-up call/result.
Source: native Codex **0.154.0**, this implementation's root and implementer
journals at 16:51–16:52Z. UUIDs, paths, prompts and question text are replaced;
no private corpus is copied. Child role `bc-implementer`, nickname `Ohm` and
`agent_path` are separate measured fields. Native answer/cancel was not
observed; this limitation is explicit in the fixture README. The four original
parser defects were first reproduced by four failing isolated tests before
production edits.

## Commands and results

Final gates ran in the immutable source snapshot:
`/var/folders/3s/lc4qb74d0g77tn5x31pz_s3h0000gn/T/codex-parity-final-fyabbb32/bob-companion`.
Its 1,465 tracked/untracked source hashes matched capture and remained unchanged
through verification, except the expected generated VSIX in that isolated copy.
The verifier checked module `__file__`/`co_filename`, including test modules, to
exclude imports from the moving original checkout. Final source hashes include
`codex_rollouts.py` **612fbeaeb295df8d6ee89d0431be8af19d5259ff424ebacd53beb9600845f219**
and `daemon.py` **2a6e04d3a35bb45047eee966fa61e15e75385db8a73849d551a14b1e393a361f**.
Only this ledger and the implementation's TODO notes were finalized afterward
in the original checkout; the verified snapshot remains untouched.

| Command / gate | Final result |
|---|---|
| `cd host && .venv/bin/pytest -q tests/test_codex_session_parity.py` | PASS — 95 |
| Provider/local/rendered pack acceptance suites | PASS — 49 |
| Rollout/spenders/workflow/crew/report/questions acceptance suites | PASS — 324 |
| Dispatch/refine/channel/close-out/terminal acceptance suites | PASS — 576, 84.50s; focused acceptance total 1044 |
| `cd host && .venv/bin/pytest -q` | PASS — 7305 passed, 3 warnings, 476.27s; actual exit 0 |
| `cd host && .venv/bin/python -m compileall -q bob_companion_daemon bob_companion_menubar` | PASS |
| `cd panel && swift test` | PASS — 969, zero failures, after snapshot-only cache cleanup |
| `cd panel && swift build -c release` | PASS — exit 0, 95.51s |
| `cd host && ./build.sh --allow-untagged` | PASS — strict build, exit 0, 18:15:08Z; no installation |
| Skill frontmatter validator | PASS — unchanged validated workflow source |
| Independent security recheck | CLEAN — 30 causal/uncertainty replays |
| Independent bug audit | SHIP — 0 findings after repair; original cases plus 24 ordering replays |
| Final source/bundle integration | WARN, no BLOCK — native Mac/phone/timing evidence remains unrun |

Final logs: `/tmp/codex-parity-final-verify-full.log`,
`/tmp/codex-parity-final-verify-build.log`,
`/tmp/codex-parity-final-verify-swift-test-clean.log`,
`/tmp/codex-parity-final-verify-swift-release.log`, and
`/tmp/codex-parity-final-verify-{parity,pack,parser,controls}.log`.
The first Swift attempt failed because copied caches contained absolute paths
from the original checkout. Its output remains in
`/tmp/codex-parity-final-verify-swift-test.log`; `swift package clean` affected
only generated snapshot files, and the clean rebuild/test above passed.

Historical development gates included focused 997, later focused 521, Swift
966 and two full runs of 7267 (459.66s/462.38s). These describe earlier source
revisions and are not substituted for the final audited snapshot's gates.
Logs remain under `/tmp/codex-parity-{source-gates,causal-focused,swift-test,full-pytest}.log`.

The second full run exited 1: 6 failed, 7260 passed in 461.66s. One was
TODO.md's 150,010-byte ceiling; the implementation's top entry was compacted
(148,245 bytes at final-gate start). Five were stale `inspect.getsource` line mappings after
concurrent daemon edits during the imported test process: a requested snapshot
method returned the source of a different method. Fresh-process execution of
those exact nodes plus all size variants passed 13/13 in 2.52s. No product
behavior or source-inspection test was changed to waive those failures. The
pre-audit full run and the independent verifier run both completed with captured
source/doc hashes unchanged. The parent captured 1,464 tracked/untracked files
at 17:32:35Z in the immutable fallback snapshot
`/var/folders/3s/lc4qb74d0g77tn5x31pz_s3h0000gn/T/codex-parity-stable-d3o0hoow/bob-companion`;
all before/after/copy hashes matched. No test run is attributed to that fallback.
Second-run evidence: `/tmp/codex-parity-full-pytest-second.log` and
`/tmp/codex-parity-full-pytest-second-result.json`. CLAUDE's relevant Codex
summary now links its full contract and leaves size headroom (139,136 bytes);
concurrent Grok prose is preserved. The corrected exact hook-purity check emits
no findings. Those two pre-audit full-suite results were actual exit-0 runs;
the final audited snapshot is separately recorded above.
The three warnings are the existing synchronous test carrying an asyncio mark
and two Pillow `getdata` deprecation warnings; no tests were skipped or waived
for these warnings. Independent verification's first attempt had two transient
doc-size failures (CLAUDE 140,189/TODO 150,845), 7,265 passes in 465.67s;
its pre-audit fresh-process rerun passed after the documented compaction.

An early current-tree full run was bounded out at 180s/75%, with failures while
edits were underway; it is **not a baseline PASS or proof of a deadlock**.
The independent PTY diagnostic collected 61 cases, passed 7, and reached its
90s bound during `test_pty_persist.py` fixture teardown's `_kill_broker`
`time.sleep`/PID-exists loop. No faulthandler deadlock dump occurred. Zombie PID
cleanup delay is an inference, not a verified root cause. No live app or broker
was interrupted. Any final failure/bounded-out test prevents a full-pass claim.

## Running application versus source and bundle

Read-only inventory at **2026-09-12 16:53:55Z**:

- Installed app `/Applications/Bob Companion.app`: PID **16672**; panel PID
  **16713**, parent 16672. Installed build **2026-09-12T13:25:21Z**,
  `main+48@aec8b17-dirty`, bundle extension **0.1.16**.
- Codex CLI **0.154.0**, Claude **2.1.269**. Codex `bob-companion-board`
  registration is enabled; fresh capability list has add/attach/close only.
- Before edits, installed/source bytes matched for `codex_rollouts.py`,
  `daemon.py`, `daemon_board.py`, `dispatch.py`, `channel_server.py`; installed
  notify/channel/close-out helpers also matched. Disk equality does not prove
  Python module bytes held by a long-lived loaded process.
- This project's editor connection, port **51235**, runs extension **0.1.15**.
  `starter-pack` **51333**, `arpg-web` **61888**, a finance project **61905** each
  run **0.1.11**, below the private planning-close minimum **0.1.12**. Bundle
  version is not evidence those windows upgraded.
- Actual implementation root/child observed in the old API: nickname `Ohm`
  instead of role `bc-implementer`, no questions after the actual async ack,
  category running. Read-only parsing of that native root by the edited source
  reports one pending question, waiting, and observed `bc-implementer`. This
  proves native parser repair, not a loaded-application or launch-matrix PASS.
- Loopback connectivity: `/api/state` approximately **0.002s**, initial SSE
  frame approximately **0.001s**. These are **not** event-to-visible-panel
  latency measurements. Existing listeners/auth/hook/roster checks were healthy.
- One paired-device registration exists; physical availability/visibility was
  not confirmed. No phone result is inferred from registration.

Sanitized inventory artifacts: `/tmp/codex-parity-runtime-inventory.json`,
`/tmp/codex-parity-runtime-checks.json`,
`/tmp/codex-parity-upstream-checks.json`. No secrets are copied into this ledger.
Final artifact:
`/var/folders/3s/lc4qb74d0g77tn5x31pz_s3h0000gn/T/codex-parity-final-fyabbb32/bob-companion/host/dist/Bob Companion.app`.
It was built **2026-09-12T18:15:08Z**, strict mode, version
**main+48@aec8b17-dirty**, extension **0.1.16** built in that snapshot. Both pack
copies, close-out helper, extension and six Python modules match snapshot bytes;
bundle imports/rendering checks and `codesign --verify --deep --strict` pass.
The original checkout's `host/dist` holds an earlier build and is not this final
artifact. Earlier 17:23:19Z/17:28:25Z builds passed their own resource/signature
checks; their original-source VSIX was restored after those checks. The final
build modified only the copied snapshot VSIX.

No new bundle was installed or launched. Read-only inventory at **17:35:13Z**
confirmed installed build **13:25:21Z**, app PID **16672** and panel PID **16713**
unchanged. At that time, the three inspected parity modules (`codex_rollouts`,
`daemon_board`, `dispatch`) differed between source and installation as expected.
Evidence: `/tmp/codex-parity-runtime-final.json`. That dated inventory does not
establish deployed parity for the later audited build.

## Live acceptance routes

| Route | Result | Reason |
|---|---|---|
| Fresh Codex Refine, editor terminal | NOT RUN | No disposable live card/model interview launched; edited app not installed. |
| Fresh Codex Refine, hosted terminal | NOT RUN | Source spawn route replayed; no actual new hosted planning session. |
| Fresh Codex Start, editor terminal | NOT RUN | No disposable implementation card launched. |
| Fresh Codex Start, hosted terminal | NOT RUN | No disposable implementation card launched. |
| Fresh Claude Refine/Start reference | NOT RUN | Hermetic reference replay only. |
| Existing paired phone, same session | NOT RUN | Device registration is not a device observation. |
| Old bridge / absent MCP tool | PASS — read-only inventory and simulated refusal only | 0.1.11 connected windows measured; automatic close refusals replayed. No live disposal attempted. |
| Question/helper/report event to visible Mac ≤5s | NOT RUN | No on-screen transition measurement; HTTP/SSE connectivity timings do not meet this criterion. |
| Hidden panel reopen freshness/legibility | NOT RUN | No interactive screen check performed. |

1. On an enrolled disposable project, run the plan's Mac live matrix on both
   supported terminal routes and the Claude reference: Refine, answer in the
   original session, attach the same card, Start the accepted disposable plan,
   observe real stages/report/actual column and close refusal/success. Record
   loaded build/connection identity and event-to-screen times, including hidden
   panel reopening. Expect each healthy visible transition within five seconds.
   Why not automated: fixture tests cannot establish fresh model skill selection,
   human-answer continuation, loaded editor state or on-screen legibility.
2. On the existing paired iPhone, inspect the same session in Needs you/Details,
   answer in the original Codex session, refresh and inspect helpers, retained
   stages and report. Expect shared state and no unsupported answer controls.
   Why not automated: an actual device and loaded transport/app are required.
