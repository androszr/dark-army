---
name: review
description: "Review a change — the working tree, one of the last commits, a branch or a pull request — using the GitNexus code graph. With no argument it asks what to review first. Every finding is graded BLOCK / FIX / WARN / NOTE and the report opens with VERDICT: SHIP or VERDICT: STOP. Explains the risks and the fixes in plain language a non-developer could read, and offers the findings as Backlog cards on Dark Army's Kanban board so a review ends in work you can start. Use when the user says /review, \"review my changes\", \"is this safe to commit/merge\", \"what could this break\", or points at a commit or PR and asks what the risk is. For a deep line-by-line correctness sweep with inline fixes, /code-review is the better tool; this one is graph-aware, plain-spoken, graded, and ends in cards."
---

<!-- This file is the canonical /review skill. `.agents/skills/review/SKILL.md`
     (Codex and Grok) and `~/.claude/skills/review/SKILL.md` (the personal
     mirror the other repos read) are byte copies of it: edit
     `.claude/skills/review/SKILL.md`, then run `tools/sync_review_skill.py`.
     A hand edit to a copy is overwritten on the next run and fails
     `host/tests/test_review_skill.py` meanwhile. -->

# /review

Point it at a change. It works out what that change can break, says so in plain
words, grades each thing it finds, recommends a fix, opens with one verdict, and
offers to put the ones you pick onto Dark Army's board.

**Four inputs, one pipeline.** `/review` with nothing **asks** what to review —
the working tree, one of the last five commits, a branch, or a pull request —
before it reads anything. `/review <sha>` reviews a commit. `/review #42`
reviews a pull request. `/review <branch>` reviews a branch. Everything after
step 1 is identical.

**Three assistants, one text.** Claude, Codex and Grok all run this file. Every
GitNexus call below is written twice on one line — the MCP call **or** its CLI
form — so an assistant with no MCP follows the same steps. Where the three
differ (how a question is asked, which board tool exists) the step says so.

## What this is not

`/code-review` is the deep line-by-line correctness sweep and it can apply fixes.
This is the other shape: it reads the **call graph** rather than only the diff, it
writes for somebody who does not read code, and it ends in cards. When both would
fit, say which you are running and why in one sentence, then run one.

`gitnexus-impact-analysis` is the GitNexus call sequence this builds on, and it
is present in both skill trees (`.claude/skills` and `.agents/skills`). Read it
if you need the tool-by-tool detail; do not duplicate it here.

---

## Step 0: refresh the map — do not ask

**Do this first, every time, before any GitNexus call.** GitNexus answers from an
index, not from the working tree. A stale index does not fail — it answers
confidently about callers that no longer exist and misses ones that do. That is
worse than no review, because it is a review you would act on.

**Refreshing is not a decision the user should be asked to make.** Asking put the
choice in the wrong place: the person cannot tell from the question whether a
stale map matters for *this* diff, the honest answer is almost always yes, and a
prompt that is always answered the same way is a prompt that should not exist.
Worse, "want me to?" is a turn that ends in a question — which parks the session
under Dark Army's *Needs you* for a chore. So: check, and if it is behind, refresh it
without asking.

```bash
python3 -c "import json;print(json.load(open('.gitnexus/meta.json'))['lastCommit'][:12])"
git rev-parse HEAD | cut -c1-12
```

- **Match** → carry on, say nothing. Silence is the correct output of a check
  that passed; a line saying the map is current is noise on every single run.
- **Differ** → refresh it, in the foreground, with one line saying what you are
  doing and no question mark:

  > The code map was a few commits behind — refreshing it first (about a minute).

  ```bash
  node .gitnexus/run.cjs analyze
  ```

  Then **re-check** `lastCommit` against `HEAD`. A refresh that ran is not a
  refresh that worked, and the whole point of the gate is not to trust the index
  blind.
- **No `.gitnexus/`** → this is the one case that still asks. Building an index
  from nothing is minutes, not one, and it writes a directory into a project that
  has never had one — a different size of act from bringing an existing one up to
  date. Say the project is not indexed, offer `bunx gitnexus@latest analyze`
  (npm 11's `npx` crashes on it — #1939; `pnpm dlx` also works), and meanwhile
  fall back to a diff-only review that does not claim blast radius at all.

### When the refresh fails

Two failures are known and they are handled differently.

**A corrupted full-text index.** The incremental pass dies with
`FTS index 'file_fts' is inconsistent: document for node offset N is missing
during delete`. Measured here on 22 Aug 2026. It is recoverable and the recovery
is not destructive to the graph:

```bash
node .gitnexus/run.cjs analyze --repair-fts   # then the normal analyze again
```

Do that once, automatically. It is the documented remedy for exactly this error,
not a guess. If it fails a second time, stop retrying.

**Anything else, or a repair that did not take.** Do not loop, and do not
silently proceed as if the map were good. Fall back to the stale-map behaviour
the old step 0 described: say once at the top that the map could not be
refreshed and why, and label **every** graph-derived claim *provisional*. A
review from a map you know is wrong is still worth something as long as the
reader knows which half to distrust.

Uncommitted changes are invisible to the index by definition, and no refresh
changes that — `analyze` reads commits. For a working-tree review, read the
changed files directly for their *content* and use the graph only for *who calls
what*.

### Every repo the review touches

A review in one repo can legitimately reach into another (a card filed against a
second project, a cross-repo finding). Run this gate **per repo, at the moment
you first make a GitNexus call against it** — not once at the top for the repo
you happen to be sitting in.

The repo's name for GitNexus comes from `.claude/review.md` (`gitnexus_repo`),
and **it is usually not the folder name**: an index is named after the *git
remote*. Two of the three repos here differ (`web-game` → `web-game-v2`,
`ledger-app` → `finance-app`). If the profile does not state one, or the name
it states errors, call `list_repos` or `node .gitnexus/run.cjs list` and match
on `path` — never guess from the directory. A wrong name fails loudly (`Repository "x" not found`), which is the
good case; the bad case is a plausible name that answers from another repo.

## Step 1: choose the target

**An explicit argument skips the question exactly as before.** `/review <sha>`,
`/review #<n>` and `/review <branch>` — with or without `--deep` — go straight
to the resolve table below and never ask. `/review` alone, or `/review --deep`
alone, **asks**. A bare `/review` used to mean the working tree silently, and a
review of the wrong thing that looks like a review of the right thing is the
miss this step exists to remove.

Before asking, gather what the question offers:

```bash
git status --porcelain | wc -l           # n — the count for the first option
git log -5 --format='%h %s'              # the five commits, short sha + subject
```

The question offers four choices, in this order:

1. **Uncommitted changes (n files)** — the working tree. A clean tree says so
   in this label (*"Uncommitted changes (none — tree is clean)"*) rather than
   hiding it; the person may still pick it and get the one-line "no changes to
   review" answer.
2. **A recent commit** — the five shown, by short sha and subject.
3. **A branch** — name it.
4. **A pull request** — its number.

**Each assistant asks in its own form.**

- **Claude** asks with `AskUserQuestion`: one question with the four options
  above. Choosing "a recent commit" asks a second `AskUserQuestion` offering the
  first **four** commits as options (the dialog's cap), with the fifth typed via
  the dialog's own free-text row. Branch and pull request take free text.
- **Grok** asks with `ask_user_question`, the same two questions, so the
  answers become buttons in Dark Army's panel.
- **Codex** has no question widget. It prints one **numbered list** — 1
  uncommitted, 2–6 the five commits (short sha and subject each), 7 a branch,
  8 a pull request — and **ends the turn** to wait for the answer. A Codex
  session that keeps going after printing the list has reviewed the working
  tree unasked, which is exactly the behaviour this step removes. The next
  message is the answer: resolve the target from it and continue at step 2
  without asking again.

**This is the one place the skill may end a turn on a question.** When the
question is asked in prose — Codex, or either question tool absent — the message
ends with the two markers, so Dark Army lists the session under *Needs you* with the
choices as buttons:

```
<!-- bob-tldr: Which change should I review? -->
<!-- bob-actions: Working tree | Last commit -->
```

The labels are exactly the words to type back, at most three of them and at most
24 characters each — the bounds of Dark Army's parser; a longer label drops the whole
row of buttons. When a question tool is used, the dialog itself is what Dark Army
shows; the accompanying message still carries the `bob-tldr` line. **Every other
ending of the skill stays marker-free**, as it always has: a finished review is
not waiting on anybody.

### Resolve the target

| Input | Diff | Base |
| --- | --- | --- |
| *working tree* | `git diff HEAD` **plus untracked files** | `HEAD` |
| `<sha>` | `git show <sha>` | `<sha>^` |
| `#<n>` | `gh pr diff <n>` | merge-base with the default branch |
| `<branch>` | `git diff <base>...<branch>` | merge-base with the default branch |

**Untracked files are collected explicitly** (`git status --porcelain` for `??`).
`git diff HEAD` does not show them, and a review that silently skips an entire
new module is the worst kind of miss.

Nothing to review is a real answer: say "no changes to review" in one line and
stop. Do not produce an empty-shaped report. That answer comes **after** the
target is chosen, never before it — an empty working tree is not a reason to
skip the question when a commit or a branch may be what the person meant.

State what you resolved — "reviewing 3 uncommitted files" — so a wrong target is
caught immediately rather than after a page of analysis.

## Step 2: map the change

`detect_changes({scope: "compare", base_ref: "<base>"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "<base>" --repo .`

Gives the changed symbols and the execution flows they sit in. For a working-tree
review the graph lags, so treat its symbol list as a starting set and add
anything the diff touches that it does not name.

## Step 3: blast radius

For each **non-trivial** changed symbol (skip renames, comments, formatting,
test-only edits):

`impact({target: "<symbol>", direction: "upstream"})` or `node .gitnexus/run.cjs impact "<symbol>" --direction upstream --repo .`

Callers that the change breaks and that are **not** in the diff are the highest
value thing this skill produces — that is the bug nobody sees in review.

Use `context({name: "<symbol>"})` or `node .gitnexus/run.cjs context "<symbol>" --repo .`
on a symbol whose role is unclear before drawing any conclusion about it.
Guessing here is how false findings are born.

## Step 4: the repo's own risks (deep runs only)

Read `.claude/review.md` if it exists:

```markdown
reviewer_agent: sf-security-reviewer
risk_domains:
  - money: prices, positions, currency rounding
  - market_data: quote freshness, stale ticks shown as live
never_ship_without: a test that fails before the fix
```

If the file names an agent **and that agent exists** in `.claude/agents/`, hand it
the diff plus the mapped symbols for a domain pass. If the file is missing, or
names an agent that is not there, run generic and **say so in one line** — a
generic review presented as a domain review is a false assurance. The named
agent is a Claude sub-agent; on Codex or Grok the same rule applies — run
generic and say so.

Only on an explicit deep request (`/review --deep`, "review this thoroughly").
The default is fast, because a review you wait five minutes for is a review you
stop running.

## Step 5: verify before anybody sees it

Every candidate finding is re-checked against the real code — open the file, read
the callers, confirm the path is reachable.

**A finding survives only if you can state it as: concrete input or state →
wrong output, crash, or silent bad data.** If you cannot, drop it. Do not soften
it into a "consider…" — a maybe-finding costs the reader exactly as much
attention as a real one and teaches them to skim.

Mark what survives:

- **confirmed** — you traced it and it holds.
- **likely** — the graph says so and the code is consistent with it, but you
  could not fully trace it.

Nothing below `likely` is reported. A review that finds nothing real says
*"nothing worth acting on"* and stops — that is a good outcome, not a failure to
find something.

### Grades

Every finding that survives this step wears exactly one grade, from this set and
no other:

- **BLOCK** — must be fixed before this ships.
- **FIX** — should be fixed; shipping without it is still defensible.
- **WARN** — worth knowing; a risk accepted knowingly.
- **NOTE** — informational.

The grade is assigned the moment a finding survives step 5, and it is the
question *how bad is this if it is real*. Confidence is the other question —
*how sure am I that it is real* — and the two are orthogonal: a `likely` BLOCK
is still a BLOCK, and a `confirmed` NOTE is still a NOTE. Nothing below
`likely` is reported at any grade.

## Step 6: say it in plain language

This is the point of the skill. The reader may not write code.

```
<BLOCK|FIX|WARN|NOTE> · <one line: what actually goes wrong, in plain words>
  Why it matters: <the consequence, for a person>
  Where:          <file:line>
  Fix:            <the specific change to make>
  Confidence:     confirmed | likely
```

Banned from this section: `d=1`, `upstream`, `blast radius`, `taint`, severity
enums, and "consider refactoring". If a sentence needs the graph vocabulary to
make sense, it is not finished. The four grade words — BLOCK, FIX, WARN, NOTE —
are allowed: they are the report's own vocabulary, not graph jargon.

### Verdict

The first line of the report is exactly one of:

```
VERDICT: SHIP
VERDICT: STOP — <the BLOCK finding's one line>
```

The rule that decides it:

- **Any BLOCK → `STOP` regardless of count.** This gate
  cannot be outvoted by a low total.
- Otherwise `SHIP` — with every FIX listed under **Do next** as the things to
  do before the next change, and WARN / NOTE after them.
- NOTE never moves the verdict; WARN never moves the verdict.
- `SHIP` means: safe to deploy, commit or merge the target as it stands.

Then the findings, worst first — BLOCK, then FIX, then WARN, then NOTE. An empty
report is `VERDICT: SHIP` above the one line *"nothing worth acting on"*. The
graph evidence stays available for anyone who asks for it; it just does not
lead.

## Step 7: offer cards

**Never file a card unasked.** A review is a read; a card is a write onto a
surface other people look at. Show the findings, let the user pick.

> Two things worth fixing. Want either of them as cards on the board?

Only BLOCK and FIX findings are offered by default; a WARN becomes a card on
request. For each accepted finding:

```
dark_army_add_card({
  title:   "<what to do, imperative, one line>",
  summary: "<why it matters, 1-3 sentences, plain language>",
  notes:   "<grade, where it is, what goes wrong, and the recommended fix>",
  project: "<the repo the finding is in>",
  tool:    "claude"
})
```

**Who has the tool.** Claude has it through Dark Army's channel. Codex has it through
the board-only `dark-army-board` helper that Dark Army registers (a Codex session
opened before Dark Army registered it will not see it). Grok has no board tool today.
In every "not there" case the rule below applies: say so in one sentence, print
the findings, and do not reach for another route.

Rules, each of which stops something specific:

- **Backlog only.** Never reach for `/api/action` `board_create` to land a card
  anywhere else. Dark Army removed an agent's ability to file into In progress on
  purpose: a card an agent can arm is a loop with no human in it. Arriving in
  In progress is what pressing Start *means*.
- **`summary` is not the title again.** It is the only line on the card written
  for somebody who will never open the instructions. Omit it and the card shows
  the first two lines of `notes` instead, which is the wrong audience.
- **At most 8 cards per review.** Every card's full prompt rides Dark Army's
  `/api/state` payload — measured at ~165 KB for 20 cards against a limiter tuned
  for ~19 KB. Over the cap, file the most serious and **say out loud** what was
  left out. A silent truncation reads as "that was everything".
- **Set `project` explicitly**, since a review in one repo can legitimately find
  work in another. The daemon validates the name against open windows and fails
  closed on one it does not know.
- **If `dark_army_add_card` is not there**, say so in one sentence and stop — for
  Claude, Dark Army's board server is not registered on this machine, which is
  an ordinary state, not a fault (only Dark Army's *channel* needs
  `--dangerously-load-development-channels server:dark-army`). A session
  started before the rename carries the same verbs as `bob_*`; a session
  keeps the tool list it was born with. Print the findings and do not reach
  for another route.

## Step 8: leave the map where you found it — current

Re-check `lastCommit` against `HEAD` one more time, in every repo the review
touched.

A review reads; it does not commit, so the usual answer is that nothing moved and
this step does nothing and says nothing. It is here for the case that is not
unusual at all on this machine: **another session committing while the review
ran.** That happened during this skill's own build — a repo that passed step 0
clean was stale forty minutes later, and the next review would have paid for it.

- **Still matching** → say nothing. Do not report a no-op.
- **Moved** → refresh it, silently, the same way and with the same FTS recovery
  as step 0. One line at the end of the report, not a question:

  > (The project picked up new commits while I was reading; I refreshed the code
  > map so the next review starts current.)

- **The refresh fails** → one line saying the map is now behind and naming
  `node .gitnexus/run.cjs analyze`. Do not retry beyond the one repair attempt,
  and never let this failure change the verdict above it — the review was done
  against a map that was good at the time, and that is still true.

This never blocks the findings. Print the report first, then refresh. The reader
is waiting on the verdict, not on an index.

## Ending the turn

Close with what you reviewed, the verdict, and — if cards were filed — their
titles and that they are in Backlog. If the map could not be refreshed, repeat
that once at the end: it is the thing most likely to have made the review wrong,
and it belongs where the reader stops. A refresh that *worked* gets at most the
one parenthetical from step 8 — a successful chore is not news. No `bob-tldr`
and no `bob-actions` on this message: the only question this skill asks is the
one in step 1.
