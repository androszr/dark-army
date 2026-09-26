# ship — implement mode

Loaded after `references/common.md` in implement mode, and by a plan-mode
session the person has explicitly told to build it now. Phase 6 through the
handoff: implement → blind verify → bug scan → conditional domain review →
handoff.

## Phase 6: implement

*Implement mode only.* In plan mode the skill ended at Phase 5b, and the card
is picked up by pressing Start on the board. If you are here from `/ship
implement <plan path>`, read that plan now — it is the whole brief, and the
session that wrote it is gone. Take the Phase 0 baseline snapshot now if this
session did not already (`SCRATCH="$SCRATCH" bash .claude/skills/ship/gate.sh snapshot`).

Show the **Cipher** banner (`banners/implementer.txt`), then spawn
`{{P}}-implementer` with:

```
plan_path: <abs>
context_path: <abs to docs/context.md>
baseline_patch: <abs to pre-ship.patch>
baseline_staged: <abs to pre-ship-staged.patch>
baseline_status: <abs to pre-ship-status.txt>
baseline_head: <abs to pre-ship-head.txt>
ledger: <abs to $SCRATCH/ship-attempts.json>
dispatch: <n — 1 for the first spawn, counting every re-dispatch>
SCRATCH: <abs — the helper `.claude/skills/ship/gate.sh` needs it>
Everything already in those patches is pre-existing work by the user. Do not
touch it, do not revert it, do not report on it. Where the plan changes a file
that is already dirty, add your hunks alongside — never rewrite the file whole.

Implement per your brief.
```

On a re-dispatch the packet adds `gaps:` — the concrete unresolved findings
and nothing else from the reviewers' prose.


### Phase 6.5: the run budget and the question

At every implementer spawn — the first, a verify re-dispatch, an audit
iteration, a domain-review `BLOCK` repair — append one line to the
same ledger **before** spawning, `<n>` being the dispatch ordinal you pass:

```bash
SCRATCH="$SCRATCH" bash .claude/skills/ship/gate.sh dispatch <n> initial|verify|audit|domain
```

That appends the row to `$SCRATCH/ship-attempts.json`. It carries the ordinal
in `dispatch`, the same field every implementer
row carries, and `attempt` `null`, because a spawn is not a gate run. The file
is JSON Lines — one object per line, appended, never rewritten — and it is the
count: read it, never memory, before deciding whether another dispatch is
allowed. The cap is **six implementer dispatches** per `/ship
implement` run, verify cycles, audit iterations and domain `BLOCK` repairs counted
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

Show the **Ledger** banner (`banners/verifier.txt`), then spawn `{{P}}-verifier`
with **only** `plan_path`, `context_path`, the three baseline paths and
`SCRATCH` (its test row runs `gate.sh classify` on any red id). Never
pass it the implementer's report. Open the shunt exemption window first — `python3
.claude/skills/shunt/exempt.py on`.

- `FAIL` → close the window (`python3 .claude/skills/shunt/exempt.py off`),
  then re-dispatch `{{P}}-implementer` with the FAIL list as `gaps`. **Max
  2 verify cycles**, then surface to the user rather than looping.
- `PASS` / `PASS-WITH-MANUAL` → Phase 6.7. Surface MANUAL items to the user as a
  checklist; they are not done until the user confirms.

**Before surfacing a MANUAL item, try once more to kill it.** A `MANUAL:`
criterion is legitimate only where the observation genuinely cannot be made
in-process — a real screen, a real device, a second real terminal, a timing you
can only feel. Everything else has a seam (a stubbed network call, a fake path,
a store on a temp file, a snapshot fixture). If a MANUAL item can be turned
into a test, write the test and mark the criterion PASS instead of handing back
a chore. More than two surviving MANUAL items means going back to the seams,
not writing the chores out more carefully.

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

Independent `{{P}}-verifier` is required even when every criterion is a
directly executable command. The coordinator never verifies its own work.

**One execution table per verifier pass, and no evidence crosses a role.**
Before the first criterion the verifier lists owner, exact argv and cwd, the
toolchain and environment identity, the digest of the tracked, staged and
untracked inputs, exit status and the full local log path. Where an acceptance
criterion and a standing convention name the **same** command in the same cwd
and environment, one verifier-owned execution satisfies both rows and both
cite the same evidence — the full suite runs once per unchanged verifier pass.
The implementer still runs every gate its brief names and the verifier still
runs its own; neither may cite the other. Any change to code, tests,
dependencies, configuration or the environment invalidates every applicable
row; an unprovable fingerprint forces a rerun; there is no cross-session pass
cache; a nonzero exit or an incomplete log is never a reusable pass. Failure
output is preserved in full on disk before anything is trimmed for a prompt.

### Phase 6.6: the lane

Not every delta earns the full review train. Read the lane once, after 6f
passed, from the delta alone:

```bash
SCRATCH="$SCRATCH" bash .claude/skills/ship/gate.sh lane '<regex>' '<regex>' …
```

— the regexes are the **Reviewers** table's, one argument each, pasted from
`docs/context.md`. It prints `LANE: fast` or `LANE: full` and why: **fast**
when the delta is at or under 150 added-and-deleted lines and no changed path
matches any reviewer row; **full** otherwise. The helper rebuilds the delta
(`$SCRATCH/ship-delta-paths.txt`, `ship-delta.patch`) from the tree minus the
Phase 0 baseline each time.

- **fast** → skip Phase 6.7 and 6.8 and go to Phase 7; the handoff says
  `Lane: fast (<n> lines; no reviewer path)`. A fast lane is not a blind one:
  when the delta does a thing the *floor* paragraph of 6.8 names — a new
  outbound call, a new background task, a new file read at runtime, a new
  persisted key — spawn that reviewer anyway and say why.
- **full** → Phase 6.7 as written, then 6.8.

### Phase 6.7: bug scan

Show the **Hex** banner (`banners/bugauditor.txt`), then spawn
`{{P}}-bug-auditor` with `iteration: <n>`. Open the shunt exemption window first — `python3
.claude/skills/shunt/exempt.py on`.

- `ITERATE` → close the window (`python3 .claude/skills/shunt/exempt.py off`),
  then re-dispatch `{{P}}-implementer` with the **in-scope findings only** as
  `gaps`. **Max 3
  iterations.** From iteration 2 the auditor runs in delta mode.
- `SHIP` → Phase 6.8.

**Out-of-scope rows never drive an iteration.** Before any re-dispatch, and on
`SHIP` alike, file each row `{{P}}-bug-auditor` marked `In scope: no` as a
**Prep** card with `dark_army_add_card` — same-theme rows merged into one card —
using the plan-mode shape: `title` the finding as an imperative change,
`summary` one sentence a non-developer could read, `notes` beginning
`Follow-up from: <card title or plan slug> — <abs plan path>, audit iteration
<n>, finding #<k>` then the finding's *Why it breaks* and *Fix* columns (for a
reviewer row: the phase, the reviewer, the finding's first words, then its
*Impact* and *Fix*), `tool` the assistant running this, `stages: ["{{P}}-planner"]`. The card lands in
Prep whatever the tool's reply says. From iteration 2 pass the titles of every
card filed and every follow-up listed so far to the auditor as
`filed_followups`.

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
outside the plan, or a `BLOCK` a `Reviewers`-table reviewer marked `In scope:
no` — stops the run and asks the person, through the question route Phase 6.5
states, in five parts: **requirement** (what the plan promised), **expansion**
(what the finding would add), **smallest compliant alternative**,
**consequences** (of fixing here and of not), **recommendation**. Three
options: **fix here** — one `{{P}}-implementer` dispatch with that finding as
`gaps`, counted against Phase 6.5's six and recorded in the plan's
`## Iteration log`, then rerun affected verification and the checker that
raised it (delta mode), ledger `reason: audit`; **file it** — the Prep card above, and the run continues;
**stop** — hand off with `STATUS: partial`, the card left open. A second
`ESCALATE` on the same theme in a later round is a repeat and goes to **file
it** by default, so the ceiling is not laundered. This is the second question of the
run's own, beside the spent-budget question — Phase 6f's stop after two failed
verify cycles is the third way a run ends on the person — and the same rule
applies: never decide it yourself.

### Phase 6.8: conditional domain review

`docs/context.md` has a **Reviewers** table: one row per domain reviewer, each
with a path regex. Phase 6.6's `gate.sh lane` already matched every row's
regex against the delta's paths (`$SCRATCH/ship-delta-paths.txt` — the run's
own changes, never the baseline's) and printed the rows that hit under
`reviewers:`. Every row that matched gets its reviewer spawned: show the **Watch** banner
(`banners/reviewer.txt`) and spawn that agent with `plan_path`, `context_path`
and the baseline paths. Open the shunt exemption window first — `python3
.claude/skills/shunt/exempt.py on`.

**The path regex is a floor, not a ceiling.** It misses domain-relevant work
that touches none of those files — a new outbound network call, a new
background task or timer, a new file read off disk at runtime, new parsing of
output the project does not own, a new persisted key. When the diff does any
of those, spawn the relevant reviewer on judgment and say why. A `BLOCK`
verdict returns to `{{P}}-implementer` and does not count against the Phase
6.7 iteration cap — domain fixes are never traded away for loop budget — but
the repair dispatch is counted against Phase 6.5's six. After any repair — a
reviewer `BLOCK` or an auditor `ITERATE` — every Reviewers-table reviewer
whose regex still matches the refreshed delta (`gate.sh lane`) is re-run in
delta mode; a reviewer's verdict is tied to the delta digest it saw.
A `BLOCK` the reviewer marked `In scope: no` does not return to the
implementer: it is escalated as Phase 6.7's *Out of scope* paragraph states,
and the reviewer's `WARN` and `NOTE` rows marked `In scope: no` are filed as
Prep cards the same way.

**The exemption window is closed before any repair and at the handoff.** A
reviewer `BLOCK`, a verifier `FAIL` or an auditor `ITERATE` runs `python3
.claude/skills/shunt/exempt.py off` before the `{{P}}-implementer`
re-dispatch, so the implementer works under the guard; Phase 7 runs it once
more. Read access inside a read-only role is never restricted — a reviewer
refused a read anyway runs `python3 .claude/skills/shunt/exempt.py on`
itself (`references/common.md`, *The handoff packet*).

## Phase 7: handoff

Close the shunt exemption window first: `python3 .claude/skills/shunt/exempt.py off`.

Same principle as Phase 5: lead with plain language, reference technical detail by
path. Open with a 2–4 sentence plain-language summary of what the user can now see
or do (mirror the plan's `## What this does` register), then report:

- plan path, with `## Acceptance criteria` checkboxes updated to reflect verdicts
  — point at the file rather than pasting the criteria list
- files changed (`git diff --stat`), with the Phase 0 out-of-scope paths excluded
  from the count so the number means something
- gate results, one plain line each ("the whole test suite passes") with the raw
  output available on request, then the lane (`Lane: fast (…)` or `Lane: full`)
  and any `PRE-EXISTING` or `IN-FLIGHT` ids the implementer or verifier
  classed, by id, each with its class — never fixed here, named so the person
  knows the red is not this card's
- outstanding MANUAL items as a **numbered checklist somebody can follow** —
  what to open, what to press, what they should see — each ending in its
  one-line `Why not automated:`, and the absolute path of the check file that
  holds them (`manual-check/<YYYY-MM-DD>-<slug>/check.md`, Phase 7b)
- any WARN or NOTE a domain reviewer left — one plain sentence each
- follow-ups filed as Prep cards, by title; those that could not be filed
  under **`Follow-ups not filed`**, each line in the `Follow-up from:` form

Run `SCRATCH="$SCRATCH" bash .claude/skills/ship/close-out.sh` to free the
baseline worktree; the terminal stays open.

**End the handoff message with the standard completion report.** This is on
disk here, and not only in a hook, because Codex has no hooks and this is the
only route by which a Codex-run pipeline produces it:

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

A session started before the rename carries the same verbs as `bob_*`; a
session keeps the tool list it was born with.

If this session was started from a Dark Army board card and the tool `dark_army_close_card`
is available, calling it is the **required last act** of a card-bound run whose
gates hold — not an optional extra. The note is the report's substance
distilled: at most two sentences drawn from the `**Asked:**` and `**Verified:**`
lines (the store clamps it at 400 characters, so never paste the report). When
the card stated a success criterion, **the note's second sentence says how the
result meets, or does not meet, that criterion** — the verifier's
`Success criterion` row, in one plain sentence.

**Only when every one of these holds:**

- Phase 6f returned `PASS`, or `PASS-WITH-MANUAL` with every MANUAL item on
  disk as a check file that passes the checker (below)
- Phase 6.7 returned `SHIP`, or Phase 6.6 explicitly skipped it on the fast
  lane with no judgment floor requiring that review
- no domain reviewer returned `BLOCK`

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
nobody has verified must be somewhere a person will find it.

**The `## Work done` report prints either way.** The gate decides what the
`**Card:**` line *says*, never whether the report appears.

A freshly initialized Codex board MCP can expose `dark_army_close_card`; it changes
only the bound card, not the terminal. After all gates and reviews pass — with
no outstanding manual work, or with every leftover check written down as a check
file — call it when available and report its actual success or refusal; a
check that is written down is checked as far as this run can take it, and no
longer holds the card back. Never claim Done from a
report, silence or session exit. When
`dark_army_needs_manual_check` is unavailable (an older session, or Codex), still
write the check file and close the card, list the exact unchecked steps and
`Why not automated:` in `## Work done`, and
do not claim a manual flag was written. When close is absent or refused, name
the missing tool or returned reason on `**Card:**`. Tool lists last for the
session lifetime; a newly installed tool needs a new session, not retries.
Card closure and the private planning-terminal close are separate outcomes.

Closing a card is not authorization to commit, push or release. The rule below
is unchanged.

**Never commit, never push.** Leave the working tree for the user to review.
Pressing Start on a card authorises the *work* in that card's plan and nothing
else — it is not authorization to commit, and it is not authorization to do
anything the plan does not name. Ask separately.

## Phase 8: deploy or release (only when the user explicitly asks)

Deploying is the user's push. The pipeline never does it. When asked, follow the
profile's skill — `/deploy-web` for the web app, `/release-ios` for the phone
app — which is the runbook for what a push to the default branch sets in
motion and how to confirm it landed. Do not improvise a release here.

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
