# Review runs

The Review section is a chore, not a card: pick a project, a provider and the
after-steps, press Start, read the findings as a checklist, tick the fixes,
press Continue, and one terminal fixes exactly those and then performs exactly
the ticked steps, reporting each. The Mac panel has a **Review** tab between
Comm and History; the phone has a **Review** tile on its Menu tab. This is the
long-form contract; `docs/context-host.md`, `docs/context-panel.md`,
`docs/phone-contract.md`, `docs/sealed-reads-contract.md` and
`docs/agent-pack.md` point here.

## Why a record and not a card

`docs/context-board.md` makes a card a person's stated work, shapes every start
by the plan gate, lets only a person reach Done, and keys `card_runs`,
`outcome_runs`, `lifecycle_spans` and `run_health` on implementation runs. A
review run has a lifecycle no card kind carries (`reviewing → picks → fixing →
done`) and a Needs you entry that is a decision no card field expresses; a card
that existed only to time a chore would sit in In progress as a lie. The
card-less precedents are `open_adhoc_terminal` and `open_mission`; a review run
follows them: under `_dispatch_lock`, through `dispatch.adhoc_guard` (the
card-less sibling of `guard`: the same refusal constants, `DISPATCH_COOLDOWN`,
`MAX_CONCURRENT_DISPATCH`, the per-project bound and the folder test), behind
`board_dispatch_enabled`, and recorded in `_review_launches`, which
`_launch_inflight` sweeps exactly as it sweeps `_adhoc_launches`, so the
one-per-project bound holds against `dispatch.guard` in both directions.

## The record and the folder

`review_run.py` is pure (stdlib plus `paths`). The records live in
`paths.REVIEW_RUNS_PATH` (`review-runs.json`, in `paths._PRIVATE_FILES`: roots
and broker handles) and are read forward-compatibly (`load_records`: unknown
keys kept, missing keys defaulted, a corrupt file is no runs). Each run has a
folder `paths.REVIEW_RUNS_DIR/<run id>/` (0700):

| File | Written by | Holds |
|---|---|---|
| `run.json` | Dark Army, at Start | the run id, the ticked step ids, the scope line — the authorisation, nothing else |
| `findings.md` | the assistant | `/review`'s own report: `VERDICT:` first, then the graded findings |
| `picks.json` | Dark Army, at Continue | `{"version": 1, "fix": [...], "skip": [...], "fixes": [{index, grade, line}], "decided_at": ...}`; the assistant matches the picks on the grade and line text, not the number alone |
| `steps.md` | the assistant | one `STEP <id>: done\|failed\|skipped\|started — <words>` per step, `DONE` last |

**Nothing is ever written under a project.** Only `pack_install` writes into a
project root (`docs/agent-pack.md`); no path crosses the wire, so
`_plan_path_refusal` is not needed. Finished runs stay listed for
`KEEP_FINISHED_SECONDS` (a week) so the reports can be read; `prune` drops
older ones and removes their folders.

## The state machine

`STATES = reviewing, picks, fixing, done, exited, ended`.

- **Start** (`start_review`) → `reviewing`. The prompt (`review_run.prompt`)
  leads with `/review remote`, then a `Dark Army review run:` block: the
  folder, the scope line, "write `findings.md` … and end the turn; do not ask",
  "wait for the Continue line", "then read `picks.json`, fix the listed
  findings and no others", the ticked steps with their `how`, "any step not
  listed here is not authorised: do not commit, push, install, restart or
  ship", the ledger rule and the broker rule. `dispatch.prompt_refusal` reads
  it as a slash command and passes.
- **Findings landed** → `picks`. `_observe_review_runs` (executor, in its own
  guarded `_reconcile_review` step beside `_reconcile_board`, so a closed
  board or a raise in the board's pass skips nothing; handed `pty_facts` the
  loop composed) reads
  `findings.md` through `scout_report.read_text` and `parse_findings`
  (a grade line is a finding only when a `Where:`, `Fix:`, `Why it matters:`
  or `Confidence:` line follows it, so summary bullets and the "Do next" list
  are not findings and never shift the numbering; `MAX_FINDINGS`, `MAX_LINE_CHARS`, `MAX_FIX_CHARS`; past the bound
  `truncated` is stated). The run is a `review_picks` Needs you entry.
  Until Continue, `picks` re-reads the file and a new digest replaces the
  list. A `done` run with an open terminal still holds its project.
- **Continue** (`continue_review`) → `fixing`. Refused outside `picks`
  (`REVIEW_NOT_PICKS_REFUSAL`), for an off-list index (`REVIEW_PICK_REFUSAL`)
  while a permission prompt is up (`TERMINAL_PROMPT_REFUSAL`) and, for a run
  not on Codex whose session has not bound, `REVIEW_NOT_BOUND_REFUSAL` (nothing
  is typed: a prompt cannot be looked up without a session). It writes
  `picks.json`, then types one printable line (`review_run.continue_line`)
  through `_terminal_line_input(handle, text, session_id)`, the line leg of
  `terminal_input` by handle, every refusal and the drain unchanged. The line
  names the picks file, not the picks.
- **Steps reported** → `done` when the ledger says `DONE` or every authorised
  step has a non-`started` line.
- **Terminal gone** → `exited` (`error = "terminal closed"`).
- **End** (`end_review`) → `ended`: closes the terminal at the recorded handle
  and nothing else. `REVIEW_HOST_DOWN_REFUSAL` answers while the broker link
  is down (nothing can be closed, so nothing is "ended");
  `REVIEW_CLOSE_FAILED_REFUSAL` only when a live terminal wearing the run's name
  exists and its close was not confirmed; with the link up and no live
  terminal found by handle or name, the run is ended truthfully. A run with no
  handle past `HANDLE_BIND_SECONDS` and no terminal wearing its name is read
  `exited`. Before closing it re-checks at the moment it fires that the terminal wears
  `review_run.pty_name(run_id)` (`end_mission`'s rule); an unconfirmed close
  keeps the record (`REVIEW_CLOSE_FAILED_REFUSAL`); a second End is
  `REVIEW_ENDED_REFUSAL`.

## The tick is the permission

Nothing commits, pushes, installs, restarts or ships unless it was ticked
before Start: the ticks ride the prompt and `run.json`, the form starts with
every step unticked, and the assistant's instructions say a step not listed is
a step the person did not ask for. `CLAUDE.md`'s "never commit, push … unless
the person asked for exactly that" is satisfied by the tick and by nothing
else. Steps are offered by `review_offer`: Dark Army's own checkout offers
`rebuild`, `restart`, `commit`, `push` and `testflight`
(`review_run.OWN_STEPS`); any other project offers its profile's `after_steps`
(written onto the pack ledger row by the installer; the daemon reads the JSON
with stdlib), else the generic `commit` and `push` (`GENERIC_STEPS`, a
byte-equal copy of `dark_army_menubar.review_steps.GENERIC_STEPS`, pinned by
`test_review_run.py`). `push` is dropped for a branch with no upstream.

## Scope

`review_offer(root)` (executor; three bounded git reads through the argv
builders in `work_record.py` — `argv_upstream`, `argv_ahead`, `argv_status` —
run by `_git_blocking`) says what a review covers: *everything not on the
remote branch, N commits ahead and M changed files*, or *no remote branch,
uncommitted changes only*. The skill's fifth input, `/review remote`, is the
working tree measured against `@{u}`.

## The restart step and survival

The restart step follows the one sanctioned recipe (`osascript -e 'quit app
"Dark Army"'`, wait for the pid, sleep 2, `open -a`) and **never touches the
pty broker**: the terminal lives in the broker, which outlives the app
(`docs/pty-broker-contract.md`). On relaunch `_adopt_review_terminals` (after
the startup broker attach, before the first snapshot) keeps a live run whose
handle the host still holds, marks a run whose handle is gone `exited`, and
adopts a terminal wearing a run's name that no record names from the run's
folder. `_review_handle_if_named` answers the recorded handle for the run's
last-bound session id, resolve-only (`_pty_handle_for`'s row-less rung).
While the app is down the hook stream drops events, so the run's session row
may be evicted and return as a roster stub; the review record is keyed by
handle and unaffected. A Codex run on a hosted pty may never bind a session id,
so the Terminal pane and button may stay absent for it while findings,
Continue, the ledger and End all work by handle.

## The wire

- `state.review` (`review_snapshot`, in `_OMITTABLE_SECTIONS`): `available`
  and the newest `MAX_PUBLISHED_RUNS` runs — state, scope sentence, findings,
  picks, steps with their last status, the ledger. **Never a handle, a digest
  or a folder path.** The daemon buys a frame on `review_run.drift_key` only
  (`_review_drifted`).
- Actions on `/api/action`, `LAN_ACTIONS` and `REMOTE_ACTIONS`, each its own
  decision: `review_start` `{root, tool, steps}`, `review_continue`
  `{run_id, fix}`, `review_end` `{run_id}`. A list may arrive as a JSON list or
  one comma-joined string (the panel's `post` sends strings only).
  Away, a `review_start` ticking `rebuild` or `restart` is 403
  `REVIEW_HOME_ONLY_REFUSAL`: the person chose the Rebuild & restart press, not a review run ticking those steps. Any door but home refuses.
- Reads: loopback `GET /api/review?root=` and the sealed `review_offer` kind
  (`root` in the body, never a query string).
- Markers on `_pipeline_writable()`: `review_supported` (the section and the
  read) and `review_section_writable` (the three actions). `review_writable`
  already meant "Mark reviewed" and is untouched.
- The inbox kind `review_picks`, key `r:<run id>`, fingerprint
  `inbox_ack.review_picks_material(run_id, findings_at)` — the run id and the
  whole second its findings landed, spelled the same on both clients
  (`ReviewRules.fingerprintMaterial`). A plain wait on a run's terminal while
  the run is in `picks` or `done` is not listed a second time (`coveredStates`);
  while it is `fixing` a wait is real news and is listed.

## What each client draws

Both clients draw the daemon's words verbatim and derive nothing:
`ReviewRules` (Foundation-only, byte-pinned between `panel/Sources/BobPanel/`
and `ios/BobPhone/` from `enum ReviewRules {` down, `test_review_rules.py`)
holds the state words, the enabling rules, the lines and the Needs you entry.
The Mac's rail and pane are `ReviewRail` and `ReviewPane`; the phone's are
`ReviewView` and `ReviewRunView`. Start and Continue are two deliberate
presses; End is armed then confirmed. The terminal is drawn only on its own
screen so one pane states `panel_terminal` for that session at a time.

## Not here

A board card for a run, `card_runs` rows or a cost line; a buzz or Live
Activity for the picks state (the inbox entry is the whole signal); reading old
findings after a run is pruned; watching the TestFlight workflow to
completion (the step reports the run URL); editing or reordering findings or
steps; a second live review of one project (refused).
