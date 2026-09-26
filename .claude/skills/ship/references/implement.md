# ship — implement mode

Loaded after `references/common.md` by every adapter in implement mode
(`/ship implement <plan path>`, or a prompt whose first non-empty line is
`Plan: <path>`), and by a plan-mode session the person has explicitly told
to build it now. It runs Phase 6 through the handoff: implement → blind
verify → bug scan → conditional integration review → conditional security
review → handoff. The plan file is the whole brief, and the session that wrote
it is gone.

## Phase 6: implement

*Implement mode only.* In plan mode the skill ended at Phase 5b, and the card
is picked up by pressing Start on the board. If you are here from `/ship
implement <plan path>`, read that plan now — it is the whole brief, and the
session that wrote it is gone. Take the Phase 0 baseline snapshot now if this
session did not already (`SCRATCH="$SCRATCH" bash .claude/skills/ship/gate.sh snapshot`).
The card and its plan are the whole package
(`docs/context-board.md`, *A complete Prep card is the handoff*); never ask
for the chat that prepared them.

Show the **bc-implementer** banner (`banners/implementer.txt`), then spawn
`bc-implementer` with `description: "Implement: <card title, or <slug> where
there is no card>"` and the handoff packet:

```
plan_path: <abs>
context_path: <abs to CLAUDE.md>
references: <the subject documents docs/agent-context.json selects for the
  plan's surfaces and its Files to change — the fallback is every one of them>
baseline_patch: <abs to pre-ship.patch>
baseline_staged: <abs to pre-ship-staged.patch>
baseline_status: <abs to pre-ship-status.txt>
baseline_files: <abs to saved tracked/untracked bytes>
baseline_head: <abs to pre-ship-head.txt>
ledger: <abs to $SCRATCH/ship-attempts.json>
dispatch: <n — 1 for the first spawn, counting every re-dispatch>
Everything already in those patches is pre-existing work by the user. Do not
touch it, do not revert it, do not report on it. Where the plan changes a file
that is already dirty, add your hunks alongside — never rewrite the file whole.

Implement per your brief.
```

Require it to read `.claude/agents/bc-implementer.md` and preserve the
recorded baseline. On a re-dispatch the packet adds `gaps:` — the concrete
unresolved findings, and nothing else from the reviewers' prose.

### Phase 6.5: the run budget and the question

At every implementer spawn — the first, a verify re-dispatch, an audit
iteration, an integration or security `BLOCK` repair — append one line to the
same ledger **before** spawning, `<n>` being the dispatch ordinal you pass:

```bash
SCRATCH="$SCRATCH" bash .claude/skills/ship/gate.sh dispatch <n> initial|verify|audit|integration|security
```

That appends the row to `$SCRATCH/ship-attempts.json`, the one ledger every
implementer row lands in.

That row carries the ordinal in `dispatch`, the same field every implementer
row carries, and `attempt` `null`, because a spawn is not a gate run. The file
is JSON Lines — one object per line, appended, never rewritten — and it is the
count: read it, never memory, before deciding whether another dispatch is
allowed. The cap is **six implementer dispatches** per `/ship
implement` run, verify cycles, audit iterations and `BLOCK` repairs counted
together. "Max 2 verify cycles" and "Max 3 iterations" stay as written; the six
sit above them.

When the implementer reports `partial` with a spent gate budget (three attempts
on one gate, or the same test red on two consecutive attempts — its `LEDGER:`
line says which), **or** a seventh dispatch would be needed, **stop and ask the
person**. Choose the route from the tools actually callable, exactly as Phase 1
states: Claude `AskUserQuestion`; Grok `ask_user_question`; Codex
`request_user_input` where the current collaboration mode permits it, or
`request_user_input_async` where available; otherwise the plain question in the
final response plus `<!-- bob-tldr: <what needs deciding> -->` and "Answer in
the original Codex session". Always the same three options:

- **continue** — one more dispatch and a fresh budget on the spent gate; asked
  again on the next spend.
- **stop** — hand off now with `STATUS: partial`, the card left open, never
  `dark_army_close_card`.
- **hand back** — stop, and `dark_army_needs_manual_check` with the failing ids as
  the steps where the tool exists.

The message summarises the ledger in plain words — which gate, how many
attempts, which tests are still red — and says the question puts the session
under Needs you until it is answered. Never continue past a spent budget on
your own, and never relabel a retry as a new phase to escape the count.

### Phase 6f: blind verify

Show the **bc-verifier** banner (`banners/verifier.txt`), then spawn `bc-verifier`
with **only** `plan_path`, `context_path`, the selected references and the
baseline artifacts, including the saved tracked/untracked file bytes and the
delta identity, and `SCRATCH`: its tests row classifies red ids through
`gate.sh classify`, which replays them in a worktree under `$SCRATCH`, never
in this tree. Never pass it the implementer's report. It must read
`.claude/agents/bc-verifier.md`, remain read-only, and verify the plan
blindly. Open the shunt exemption window first — `python3
.claude/skills/shunt/exempt.py on`.

- `FAIL` → close the window (`python3 .claude/skills/shunt/exempt.py off`),
  then re-dispatch `bc-implementer` with the FAIL list as `gaps`. **Max 2
  verify cycles** (at most two repair rounds), then surface to the user rather
  than looping.
- `PASS` / `PASS-WITH-MANUAL` → Phase 6.6 (the lane), then the 6.7–6.9
  panel. Surface MANUAL items to the user as a checklist; they are not
  done until the user confirms.

**Before surfacing a MANUAL item, try once more to kill it.** A `MANUAL:`
criterion is legitimate only where the observation genuinely cannot be made
in-process — a real window on a real screen, a second real terminal, a timing
you can only feel. Everything else has a seam this codebase already uses (a
stubbed `dispatch.spawn`, a fake path, a store on a temp file, `PanelMetrics`
arithmetic, a snapshot dict). If a MANUAL item can be turned into a test, write
the test and mark the criterion PASS instead of handing back a chore. More than
two surviving MANUAL items means going back to the seams, not writing the chores
out more carefully.

What does survive is written as **numbered steps somebody can follow** — what to
open, what to press, what they should see, in words a non-developer could act on
— followed by one line beginning `Why not automated:`. That line is required: a
check that cannot say why it is manual is one that should have been a test.

**The `Success criterion` row is reported, never gated.** When the plan tags a
criterion `(success criterion)` — the person's own test of the work, carried
in from the card's `Objective:` block — the verifier adds one row under its
table: `MET` / `NOT MET` / `CANNOT TELL` and a sentence. Surface that row to
the user verbatim. It never changes `VERIFY VERDICT`: a `NOT MET` beside an
all-PASS table is still `PASS`, a sentence for the person to weigh, and only
the person accepts the outcome.

Independent `bc-verifier` is required even when every criterion is an executable
command. The implementer's test execution is not independent verification.

**One execution table per verifier pass, and no evidence crosses a role.** The
verifier builds its table before it checks the first criterion: owner, exact
argv and cwd, the toolchain and environment identity, the digest of the
tracked, staged and untracked inputs the command reads, exit status and the
full local log path. Where an acceptance criterion and a standing convention
name the **same** command in the same cwd and environment, one verifier-owned
execution satisfies both rows and both cite the same evidence — the full host
suite runs **once** per unchanged verifier pass, never twice to say the same
thing. If the standing Tests gate is that full suite, a criterion whose
pytest argv is a subset of `tests/` (the plan's related-files list included)
cites the full-suite row; do not run the subset first. The implementer still
runs every gate its brief names, and the verifier still runs its own full
suite: the two executions are independent and neither
may cite the other. Any
change to code, tests, dependencies, configuration or the environment
invalidates every applicable row; a fingerprint that cannot be proved forces
a rerun; there is no cross-session pass cache; a nonzero exit or an incomplete
log is never a reusable pass. The orchestrator consumes the latest valid
evidence a reviewer produced rather than launching a third copy to restate it.
Failure output is preserved in full on disk before anything is trimmed for a
prompt.

### Phase 6.6: the lane

After the verifier's `PASS`, run `SCRATCH="$SCRATCH" bash
.claude/skills/ship/gate.sh lane` against the refreshed delta identity. It
prints `LANE: fast` when the delta is at or under 150 added-plus-deleted
lines, touches no integration path and matches neither security floor (the
patterns live in `gate.sh`, the one copy); otherwise `LANE: full`.

- **fast** → skip Phase 6.7, 6.8 and 6.9 and go to Phase 7; the handoff says
  `Lane: fast (<n> lines; no door, no packaging)`. A fast lane is not a blind
  one: when the delta does a thing the *floor* paragraphs of 6.8 and 6.9 below
  name — a new file read off disk, a new thread or subprocess, a new action in
  a tuple, a new place a header is trusted — spawn that reviewer anyway and
  say why.
- **full** → Phases 6.7, 6.8 and 6.9 **together**, next heading.

### Phases 6.7–6.9: one parallel panel

After verifier `PASS` / `PASS-WITH-MANUAL` and `LANE: full`, open the
shunt exemption window **once** and spawn every reviewer that applies, in
one parallel panel — they are all read-only on a stable delta:

- always `bc-bug-auditor` (Phase 6.7 below)
- `bc-integration-reviewer` when the integration floor or its judgment
  paragraph matches (Phase 6.8)
- `bc-security-reviewer` when either security floor or its judgment
  paragraph matches (Phase 6.9)

Wait for all. Then apply the `ITERATE` / `BLOCK` / out-of-scope filing
rules already written on those phases. An auditor `ITERATE` or a door
`BLOCK` still returns to the implementer. After the repair, re-run affected
verification, the checker that raised it and every door reviewer whose
`gate.sh lane` floor still matches, all in delta mode: a door verdict is
tied to the delta digest it saw. Do not wait for the auditor to finish
before starting a door review.

### Phase 6.7: bug scan

Show the **bc-bug-auditor** banner (`banners/bugauditor.txt`), then spawn
`bc-bug-auditor` with `iteration: <n>`. It must read
`.claude/agents/bc-bug-auditor.md`. Open the shunt exemption window first — `python3
.claude/skills/shunt/exempt.py on`.

- `ITERATE` → close the window (`python3 .claude/skills/shunt/exempt.py off`),
  then re-dispatch `bc-implementer` with the **in-scope findings only** as
  `gaps`. **Max 3
  iterations** (at most three audit/repair rounds). From iteration 2 the
  auditor runs in delta mode.
- `SHIP` → consume the door reviewers' results from the same panel (already
  spawned), or Phase 7 when neither door review ran.

**Out-of-scope rows never drive an iteration.** Before any re-dispatch, and on
`SHIP` alike, file each row the auditor marked `In scope: no` as a **Prep**
card with `dark_army_add_card` — same-theme rows merged into one card — using the
plan-mode shape: `title` the finding as an imperative change, `summary` one
sentence a non-developer could read, `notes` beginning `Follow-up from: <card
title or plan slug> — <abs plan path>, audit iteration <n>, finding #<k>` then
the finding's *Why it breaks* and *Fix* columns (for a reviewer row: the phase,
the reviewer, the finding's first words, then its *Impact* and *Fix*), `tool`
the assistant running this, `stages: ["bc-planner"]`. The card lands in Prep whatever the tool's
reply says. From iteration 2 pass the titles of every card filed and every
follow-up listed so far to the auditor as `filed_followups`.

**Never file what the plan names.** The planning run filed every `## Out of
scope` follow-up; a second copy sits in Prep until Refine finds the work
planned or done. A finding or `FOLLOW-UPS` line the plan's `## Out of scope`
already names is reported by title, not filed.

**When `dark_army_add_card` is not among this session's callable tools** — Grok never
has it, and a session keeps the tool list it was born with — the rows go under
a heading **`Follow-ups not filed`** in the Phase 7 handoff, one line each in
the same `Follow-up from:` form, and the same lines are appended to the plan
under `## Iteration log` as a dated entry. A follow-up is **never dropped** and
never becomes a fix round.

**An out-of-scope row marked `ESCALATE`** — a FUNC blocker the auditor placed
outside the plan, or a `BLOCK` a Phase 6.8 / 6.9 reviewer marked `In scope:
no` — stops the run and asks the person, through the question route Phase 6.5
states, in five parts: **requirement** (what the plan promised), **expansion**
(what the finding would add), **smallest compliant alternative**,
**consequences** (of fixing here and of not), **recommendation**. Three
options: **fix here** — one implementer dispatch with that finding as `gaps`,
counted against Phase 6.5's six and recorded in the plan's `## Iteration log`,
then rerun affected verification and the checker that raised it (delta mode),
ledger `reason: audit`; **file it** — the Prep card above, and the run continues; **stop** — hand off
with `STATUS: partial`, the card left open. A second `ESCALATE` on the same
theme in a later round is a repeat and goes to **file it** by default, so the
ceiling is not laundered. This is the second question of the run's own, beside
the spent-budget question — Phase 6f's stop after two failed verify cycles is
the third way a run ends on the person — and the same rule applies: never
decide it yourself.

Both conditional review stages below run independently, each reviewer reading
its authoritative `.claude/agents/<role>.md` brief and remaining read-only.
Packaging, hooks, API/auth, persistence, dependencies, panel launch,
destructive actions and installed-runtime behavior require integration review.
Access and secrets require security review.

### Phase 6.8: conditional integration review

Run against this run's baseline delta, including staged and new content:
`gate.sh lane` (Phase 6.6) printed `integration:` — `no`, or the paths in
`$SCRATCH/ship-delta-paths.txt` that match `INTEGRATION_PATHS` in
`.claude/skills/ship/gate.sh`, the executable copy of the pattern: `host/setup.py`,
`host/build.sh`, `host/requirements*`, the menu bar's `hooks`, `first_run`,
`preferences`, `notifier` and `panel_process`, the daemon's `api_server`,
`session_store`, `jobs_store` and `signals`, the panel's `Package.swift`,
`Lifecycle.swift` and its API layer (`Models`,
`BoardModels`, `EnrollmentModels`, `TerminalModels`, `ActionModels`,
`DaemonClient`, `BoardClient`, `Fetchers`, `OutcomeClient`), and anything
under `vscode-extension/`.

If it names a path, show the **bc-integration-reviewer** banner (`banners/integration.txt`) and
spawn `bc-integration-reviewer`. Open the shunt exemption window first — `python3
.claude/skills/shunt/exempt.py on`.

**This path grep is a floor, not a ceiling.** It is file-path based and misses
integration-relevant work that touches none of those files — a module that starts
**reading a new file off disk** (invisible until the bundle is built), a new
**thread, timer or subprocess**, a new **shell-out** (`afplay`, `swift`,
`osascript`, `claude agents --json`), or new **parsing of output we do not own**
(the transcript JSONL, `~/.claude.json`), or a **reference a rendered or
packaged copy has to reach** (a new file under the agent pack template, a
resource the frozen bundle must carry). When the diff does any of those, spawn
the reviewer on judgment and say why. A `BLOCK` returns to `bc-implementer`; rerun affected verification and the
blocking review after repair. It does not consume the bug-audit allowance and
never permits a success handoff while unresolved; the repair dispatch is
counted against Phase 6.5's six. A `BLOCK` the reviewer marked `In scope: no`
does not return to the implementer: it is escalated as Phase 6.7's *Out of
scope* paragraph states, and the reviewer's `WARN` and `NOTE` rows marked
`In scope: no` are filed as Prep cards the same way. The two door
triggers are independent; security may already have been spawned beside
this one.

### Phase 6.9: conditional security review

Evaluate independently, whether or not integration review ran.
Two floors, both printed by `gate.sh lane` on its `security:` line, and
**either** match spawns the agent. The first is over the paths the change
touched — `$SCRATCH/ship-delta-paths.txt` against `SECURITY_PATHS` in
`.claude/skills/ship/gate.sh`, the executable copy:
`ios/BobPhone/(Client|Pairing|Push|RelayTransport|RemoteAuth|HomeTransport|HostAddress|Outbox|LeaseReminder|BobPhoneApp)\.swift`,
`ios/BobPhoneWidget/`,
`dark_army_daemon/(relay|relay_client|devices|lan_hosts|enrollment|paths|channel_server|terminal_stream|command_receipts|api_server)\.py`
and `panel/Sources/BobPanel/(PairingView|RelaySheet)\.swift`. That is the
phone's transport, pairing, push and auth files, **not every phone file**: a
phone screen that draws a card is the verifier's to check, and until 21 Sep
2026 a blanket `ios/BobPhone/` match sent every phone card through a security
review with nothing to find. The second is over the diff's own **content** —
`$SCRATCH/ship-delta.patch` against `SECURITY_CONTENT`,
`LAN_ACTIONS|REMOTE_ACTIONS|_home_open|SealedCodec|note_lan_proof|set_lease_days|_PRIVATE_FILES|X-Bob-Token`
— because a hunk that touches a door is security-relevant wherever it lives.

If either names a match, show the **bc-security-reviewer** banner (`banners/security.txt`) and
spawn `bc-security-reviewer`. Open the shunt exemption window first — `python3
.claude/skills/shunt/exempt.py on`.

**This path grep is a floor, not a ceiling.** Both greps are mechanical and miss
work that is security-relevant without naming any of those strings — a **new
action added to a tuple in a file not on the list**, a **new file read out of
the state directory**, a **new header** or a new place an old one is trusted, or
a **new refusal that fails open**. When the diff does any of those, spawn the
reviewer on judgment and say why. A `BLOCK` returns to `bc-implementer`; rerun affected verification and the
blocking review after repair. It does not consume the bug-audit allowance and
never permits a success handoff while unresolved; the repair dispatch is
counted against Phase 6.5's six. A `BLOCK` the reviewer marked `In scope: no`
does not return to the implementer: it is escalated as Phase 6.7's *Out of
scope* paragraph states, and the reviewer's `WARN` and `NOTE` rows marked
`In scope: no` are filed as Prep cards the same way.

**The exemption window is closed before any repair and at the handoff.** A
reviewer `BLOCK`, a verifier `FAIL` or an auditor `ITERATE` runs `python3
.claude/skills/shunt/exempt.py off` before the `bc-implementer` re-dispatch,
so the implementer works under the guard; Phase 7 runs it once more. Read
access inside a read-only role is never restricted — a reviewer refused a
read anyway runs `exempt.py on` itself (`references/common.md`, *The handoff
packet*).

## Gates

Run gates reached by the final delta:

- Always: `cd host && .venv/bin/pytest -q -n auto -p no:cacheprovider` —
  across workers, about three minutes; nine to ten single-process under
  fleet load, which is where the two-hour runs went
- Python: `cd host && .venv/bin/python -m compileall -q dark_army_daemon dark_army_menubar`
- Swift: `cd panel && swift build -c release`
- Extension: `cd vscode-extension && npm run build`
- Packaging/hooks: `cd host && ./build.sh --allow-untagged`

Development packaging uses `--allow-untagged` for the version gate alone;
panel and extension freshness stay strict. Never use `--dev` or `--install`
for this gate. Release builds keep their strict tagged, clean version gate.

The implementer runs these in order, every one through `gate.sh run`: the
plan's test files and the touched modules' test files first
(`pytest-targeted`), then the other gates the delta reaches, and the full
suite once at the end (`pytest-full`) — after a fix the failing ids and the
targeted files again, the full suite at most once more and only when the fix
reached a module the targeted files do not cover, never a third time. Three
counted attempts per gate per dispatch, read from and appended to
`$SCRATCH/ship-attempts.json` by the helper; the same test red on two
consecutive attempts stops at once. A red id in a file the delta did not
touch goes through `gate.sh classify` once and comes back `PRE-EXISTING` (red
at the baseline too — replayed **by id** in a worktree, never the whole
suite), `IN-FLIGHT` (another card's half-built work in the same tree,
including a phone grep test whose `ios/BobPhone/` sources are dirty outside
this delta, or an inventory test whose `docs/` sibling is — reported, never
fixed, never counted; the last run to finish is the one that must be green
on the whole tree) or `YOURS`. The full suite runs twice per
clean run — the implementer's and the verifier's, independent by design —
and not more. A Swift build is waited on, never edited under.

Before any explicitly authorized commit, run GitNexus
`detect_changes({scope: "all"})`, or the CLI fallback
`node .gitnexus/run.cjs detect-changes --scope all --repo .`.
Compare mode may supplement branch regression review; it cannot replace the
all-changes check on this checkout. `partial`, `truncated`, unavailable and
`UNKNOWN` results are unresolved: do not commit on any of them. Resolve the
failure and rerun; report incomplete if a complete result cannot be obtained.
Commit, push, install and release still require explicit authorization.

## Phase 7: handoff

Close the shunt exemption window first: `python3 .claude/skills/shunt/exempt.py off`.

Same principle as Phase 5: lead with plain language, reference technical detail by
path. Open with a 2–4 sentence plain-language summary of what the user can now see
or do (mirror the plan's `## What this does` register), then report:

- plan path and verifier verdicts; never rewrite the accepted acceptance criteria
- the lane (`Lane: fast|full`, with the line count) and which phases it skipped
- the `PRE-EXISTING:` and `IN-FLIGHT:` ids with the helper's line for each;
  an in-flight id names the other run's file, and it is that run's to fix
- files changed (`git diff --stat`), with the Phase 0 out-of-scope paths excluded
  from the count so the number means something
- gate results, one plain line ("the whole test suite passes") with the raw
  output available on request — the role that owns each execution named, and
  skipped gates with their reasons
- outstanding MANUAL items as a **numbered checklist somebody can follow** —
  what to open, what to press, what they should see ("1. Open the menu bar with
  three sessions running. 2. Expect the strip to still fit inside its budget.")
  — each ending in its one-line `Why not automated:`, and the absolute path of
  the check file that holds them (`manual-check/<YYYY-MM-DD>-<slug>/check.md`,
  Phase 7b)
- any WARN or NOTE the integration or security reviewer left — one plain
  sentence each, and any baseline files left untouched
- follow-ups filed as Prep cards, by title; those that could not be filed
  under **`Follow-ups not filed`**, each line in the `Follow-up from:` form

Discoveries go to the plan's report, a knowledge note (`dark_army_knowledge_write`)
or the subject document; follow-ups are Prep cards. Installation and
release remain separate explicit decisions.

Run `SCRATCH="$SCRATCH" bash .claude/skills/ship/close-out.sh` to free the
baseline worktree; the terminal stays open.

**End the handoff message with the standard completion report** — the same
shape Dark Army's SessionStart hint asks every agent for, so a finished run reads
the same in every project. This is also the only route by which a Codex-run
pipeline (no hooks, no SessionStart hint) produces the standard report:

```
## Work done

**Asked:** <the request, one or two sentences, in the user's terms>
**Changed:** <what changed, up to ~6 short bullets or sentences; file paths welcome>
**Verified:** <each check that ran and its result, in plain words>
**Unchecked:** <numbered steps a person can follow — open this, press that,
  expect this — then one line beginning "Why not automated:"; or exactly
  "Nothing - every check above ran.">
**Card:** <"Moved to Done: <one-sentence note>", plus " — check: <absolute
  path of the check file>" when one was left, or "Left open: <why>" when the
  run stopped short — reflecting Phase 7b's outcome below>
```

The four labelled lines always appear in that order; an empty section says so
out loud ("Nothing - every check above ran.") rather than going quiet; the
whole report stays under ~25 lines; and the message carries no `bob-tldr` and
no `bob-actions` marker — a completion report is the end of the work, not a
wait.

### Phase 7b: close the card once every check ran or is written down as a check file

A freshly initialized Codex board MCP can expose `dark_army_close_card`; it changes
only the bound card, not the terminal. After all gates and reviews pass — with
no outstanding manual work, or with every leftover check written down as a check
file — call it when available and report its actual success or refusal; a
check that is written down is checked as far as this run can take it, and no
longer holds the card back (below). Never claim Done
from a report, silence or session exit. When
`dark_army_needs_manual_check` is unavailable (an older session, or Codex), still
write the check file and close the card, list the exact unchecked steps and
`Why not automated:` in `## Work done`, and
do not claim a manual flag was written. When close is absent or refused, name
the missing tool or returned reason on `**Card:**`. Tool lists last for the
session lifetime; a newly installed tool needs a new session, not retries.
Card closure and the private planning-terminal close are separate outcomes.

Born before the rename: `bob_*`.

If this session was started from a Dark Army board card and the tool `dark_army_close_card`
is available, calling it is the **required last act** of a card-bound run whose
gates hold — not an optional extra. The note is the report's substance
distilled: at most two sentences drawn from the `**Asked:**` and `**Verified:**`
lines (the store clamps it at 400 characters, so never paste the report). When
the card stated a success criterion, **the note's second sentence says how the
result meets, or does not meet, that criterion** — the verifier's
`Success criterion` row, in one plain sentence. The
card moves itself to Done carrying your name and that note, lands at the top of
the Done column wearing a **FINISHED · REVIEW** banner until a person presses
Reviewed, and nobody has to drag it across on trust.

**Only when every one of these holds:**

- Phase 6f returned `PASS`, or `PASS-WITH-MANUAL` with every MANUAL item on
  disk as a check file that passes the checker (below)
- Phase 6.7 returned `SHIP`, or Phase 6.6 explicitly skipped it on the fast
  lane with no judgment floor requiring that review
- no integration or security reviewer returned `BLOCK`
- no required capability or review remains incomplete

A finding marked `In scope: no` — filed or listed — never blocks the close; an
`ESCALATE` row awaiting the person's answer does, because the run ended on that
question.

**A leftover check is a file, and then the card closes.** Every MANUAL item
that survived "try once more to kill it" is written, before any board call, as
`manual-check/<YYYY-MM-DD>-<slug>/check.md` at the project root — one folder per
check, git-ignored, anything gathered beside the file — in exactly this shape:

```
# <what is being checked, as a title>

- **Card:** <the card's title, as the prompt gave it>
- **Project:** <the project's name>
- **Check:** <what is being checked, one line>
- **Created:** <ISO 8601 with time and offset, e.g. 2026-09-25T14:32:00+02:00>
- **Status:** open
- **Outcome:** none
- **Checked at:** none

## Steps

1. <one action per line: what to open, what to press, what they should see>

## Why not automated

<one line at least>
```

Run `python3 .claude/skills/ship/manual_check.py <the file>` until it prints
`ok`. The run's checks share one file unless they are genuinely separate. Then,
in this order: `dark_army_needs_manual_check` with the same numbered steps and
the `Why not automated:` line as `steps` and the file's absolute path as
`path`; **then** `dark_army_close_card`. The card goes to Done wearing a
manual-check badge; the check waits in the Checks section of both apps, open,
until a person records **Passed** or **Failed**, which Dark Army writes into
the file's `Status`, `Outcome` and `Checked at` lines — those three are Dark
Army's to rewrite, never the agent's after the flag. The `**Card:**` line reads
`Moved to Done: <note> — check: <absolute path of the file>`.

A MANUAL item that is only a sentence in the terminal does not qualify: what
nobody has verified must be somewhere a person will find it, which is the file,
the badge and the Checks section — not the last message of a terminal.

**The `## Work done` report prints either way.** The gate decides what the
`**Card:**` line *says*, never whether the report appears: on a close it reads
"Moved to Done:" with the note and the check file's path; when the run stopped
short it reads "Left open:" and says why, so what nobody checked is on screen
in the terminal and not only in the transcript.

Either tool may simply not exist — a session that was already open when Dark Army was
upgraded keeps the tool list it was given at startup. Report the missing tool and leave the card alone.

Closing a card is not authorization to commit, push or release. The rule below
is unchanged.

**Never commit, never push.** Leave the working tree for the user to review.
Pressing Start on a card authorises the *work* in that card's plan and nothing
else — it is not authorization to commit, and it is not authorization to do
anything the plan does not name. Ask separately.

## Phase 8: install and release (only when the user explicitly asks)

There is **no CI runner for macOS**; everything is built locally.

- **Try it live** — `cd host && ./build.sh --install` builds the panel, freezes
  the bundle with py2app, and installs to `/Applications`. Without `--install`
  the build lands in `host/dist` and the running app is untouched.
- Hooks are read at Claude Code **session start**. After changing the hook
  handler, existing sessions keep the old one — say so rather than debugging a
  change that is not loaded.
- **Cutting a version is a different skill.** Tagging and the GitHub release go
  through `.claude/skills/releasing/SKILL.md`. Do not improvise a release here.

Prefer several small conventional commits with real reasoning in the message over
one blob — the history is bisectable and the *why* is what a future reader needs.

## Batch: several cards in one session

Entered when the prompt's first line begins `/ship batch: implement` — Dark
Army's START n TOGETHER on several planned Backlog cards. The prompt lists
`## Card k of n — <title>` blocks, each with a `Card id:` and a `Plan:`
line. Dark Army binds this session to one card at a time: card 1 at the
start, each next card when you call `dark_army_next_card`. For each card, in
order, one at a time:

1. `SCRATCH=<run scratch>/card-<k>`, then Phase 6 → 7b on that card's plan
   **exactly as a single run**: its own `gate.sh snapshot` baseline (so card
   k's work is pre-existing to card k+1's review), its own six-dispatch
   ledger, lane, panel and check file.
2. Print that card's `## Work done` **first** — Dark Army records it as
   that card's report when you move on — then close as Phase 7b says:
   `dark_army_needs_manual_check` where a check is left, **then**
   `dark_army_close_card`, then `dark_army_next_card`. Never flag after the
   next call: it would land on the next card.
3. A run that stops short (a spent budget answered `stop`, `hand back`, a
   missing role, an `ESCALATE` answered `stop`) prints its report with
   `**Card:** Left open: …`, leaves that card unclosed and still calls
   `dark_army_next_card`; Dark Army leaves it as ended work for a person. A
   card you left stays left: never close it later.
4. Run `close-out.sh` bare with that card's scratch. Never start card
   k+1's implementer before card k's close or next call.

`dark_army_next_card` answers with the next card's title and plan, or says
the batch is finished; refused right after the start, wait a few seconds and
retry once. Never commit, push, install or release. End with one summary
naming every card and the column it actually landed in.
