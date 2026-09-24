# ship — plan mode

Loaded after `references/common.md` by every adapter in plan mode. It runs
Phases 1–5b: interview, plan, deterministic preflight, file the plan on Dark Army's
Kanban board, close out with `--plan` (Phase 5b). It **stops there**. It does not
implement, and it does not end its turn asking whether it may. A session that
is told to build it now loads `references/implement.md` before Phase 6.

## Phase 1: interview

Load `templates/questions.md` before assessing the idea. Resolve branches from
the brief, the objective and the tree first; ask **at most 3** material design
questions, one at a time, with at most three unanswered questions. When zero
questions are needed, state the settled assumptions and proceed. Never ask the
person to restate a settled objective. If they say "use recommendations" or
"go with your recommendations", record the recommendations as assumptions and
proceed. Record answers and assumptions as a key/value map and pass that map to
the planner with the `Objective:` block verbatim.

Choose the question route from the tools actually callable in this session:
Claude uses `AskUserQuestion`; Grok uses `ask_user_question`. Codex uses
`request_user_input` only when its current collaboration mode permits it, or
`request_user_input_async` when available. An async acknowledgement only means
the question was registered: await the person's answer before dependent work.
Never use a question tool for permission when the environment forbids that.
If no supported question tool is callable, ask one concise plain-language
question in the final response, add `<!-- bob-tldr: <what needs deciding> -->`,
say "Answer in the original Codex session", and end the turn. Do not invent
reply buttons or require an unavailable Claude tool. On the next answer,
resume the interview and planning stage with the recorded map; the reply is
not a new request to implement. Refine owns this assessment; Prepare only
produces an editable draft.

**The card is the whole brief.** A Refine session sees the card's title,
summary, instructions and `Objective:` block, nothing of the chat that filed
it (`docs/context-board.md`, *A complete Prep card is the handoff*). A
missing or stub field becomes one of the three questions, never a guess; when
more is missing than three settle, write no plan and say the card belongs
back in Prep.

## Phase 2: derive the plan path

1. Kebab-case slug from the idea, ≤40 chars, ASCII.
2. `plans/<YYYY-MM-DD>-<slug>.md`. On collision, suffix `-2`, `-3`.

## Phase 3: spawn the planner

Show the **Overwatch** banner (`banners/planner.txt`), then spawn `bc-planner`
— the project custom agent of that name on Codex, `subagent_type` on Claude,
`spawn_subagent` on Grok — with `description: "Plan: <card title, or <slug>
where there is no card>"` and this prompt:

```
Mode: new
Idea: <idea text>
Answers:
- <key>: <value>
objective: <the Objective: block from the dispatched prompt, verbatim — omit
  the line when there was none>
plan_path: <abs path>
template_path: <abs path to .claude/skills/ship/templates/plan.md>
context_path: <abs path to CLAUDE.md>
references: <the subject documents docs/agent-context.json selects for the
  idea's surfaces — widen on discovery; the fallback is every one of them>
shunt: .claude/skills/shunt/SKILL.md — bulk-read a survey or a generated
  file through the helper; never an edit or anything security-relevant

Write the plan per your brief. Read the context file FIRST, then the
references, then verify every path and symbol you cite against the actual
host/ and panel/ trees before quoting it.
```

If the prompt that started this session ends with an `Objective:` block
(`Who benefits:` / `Intended benefit:` / `Success criterion:` lines Dark Army
appended from the card), hand that block to the planner verbatim as
`objective:` — it is the person's own objective, never reworded, and the
plan's `## What this does` names who benefits while at least one acceptance
criterion is tagged `(success criterion)`. Require the planner to read
`.claude/agents/bc-planner.md` as its complete role specification.

Wait for `PLAN: <path>` + abstract.

## Phase 4: deterministic preflight

Read `PREFLIGHT-CHECKLIST.md` and run every check against the plan file using
**bash and grep only — no agent spawn**. Classify each finding `BLOCK` or `WARN`.

- **Any BLOCK** → do not proceed to Phase 5. Show the abstract plus the BLOCK
  findings with fix hints, and offer `iterate` or `stop`.
- WARN-only → carry the warnings into the Phase 5 summary and continue.

This is the one place plan mode is allowed to end on a question. A preflight
BLOCK means the plan is not fit to be filed, and a card carrying a plan nobody
can build is worse than an interruption. Do **not** file it and do not close out:
leave the turn open with a `<!-- bob-tldr -->` saying what needs deciding, and
resume at Phase 4 when the user answers.

**Two rounds, then the user decides.** If the *same* check BLOCKs after two
iterate rounds, stop translating and show the **raw matched lines**, name the
check, and offer an explicit override — the plan proceeds with that check waived
and the waiver recorded in the plan's `## Iteration log`. These checks are
keyword-triggered against prose; a false BLOCK the user cannot see is a stall
they cannot diagnose, and this loop is the only one in `/ship` without a natural
cap.

Whenever a BLOCK or WARN is surfaced to the user (here or in Phase 5),
**translate each finding into one plain sentence** — what it means and why it
matters (e.g. "the plan would do slow work on the thread that draws the menu bar,
which is how the icon stops responding to clicks"). Never show raw check names,
check numbers, or command output; those stay in your context.

## Phase 5: file the plan on the board

**The plan is the deliverable, and this is where plan mode stops.** A plan
nobody can find is not delivered: the handoff in this repository is a Backlog
card on Dark Army's Kanban board, not a sentence in a terminal somebody has already
scrolled past.

1. Re-read the plan file. The card is read by a person deciding what to pick up
   next, not by the agent that will pick it up, so everything on it comes from
   the plan's **plain-language zone** — never the technical zone.
Only a confirmed no-refinement attribution result permits creating a new
card. An attribution/ownership refusal or unknown reply is not that result:
report it and leave the existing card alone; never create a duplicate or guess
another identity. If attach is absent, create only when this is known to be a
hand-run planning session without an existing refinement card; otherwise hand
back the plan path and the missing tool. After authoring a card, attach once
to that authored card and report the returned actual column. An unsuccessful
attachment leaves Prep, not an invented Backlog. A preflight BLOCK attaches
nothing and starts no implementation.

2. **Attach first.** If this session was dispatched by Dark Army's Refine button, a
   card for this work already sits in Prep — creating a second one is the
   failure. So call the attach tool before anything else:

   ```
   mcp__dark-army__dark_army_attach_plan({ path: "<abs plan path>" })
   ```

   If it answers ok, the card this session was refining now carries the plan
   and has moved to **Backlog** — **do not also create a card**. Skip straight
   to the user summary below, naming that card and saying it moved to Backlog
   with the plan attached.

   If it refuses because this session is not refining any card (a hand-run
   `/ship`), fall through to step 3 and file one.
3. File it:

   ```
   mcp__dark-army__dark_army_add_card({
     title: "<the change, one line, imperative — 'Add X', not 'Plan for X'>",
     summary: "<what this is for, 1-3 sentences a non-developer could read>",
     plan: "<abs plan path>",
     notes: "Plan: <abs plan path>\n\nRead that plan first, then run: /ship implement <abs plan path>\nThe plan carries its own acceptance criteria — implement it, do not re-plan it.\n\n<the plan's `## What this does`, verbatim>",
     tool: "<claude | codex | grok — the assistant running this>",
     stages: ["<each specialist from the plan's Stages header, in order>"]
   })
   ```

   Split the plan's `Stages` value on `|`, trim each name, preserve its order,
   and omit blanks. Pass that normalized list exactly; do not add a stage that
   the plan did not declare.

   **`summary` is not optional and it is not the title again.** It is the line
   the card *leads with* on the board — the title names the change, the summary
   says what it is for — and it is the only field on that card written for
   somebody who will never open the instructions. Leave it out and the card
   falls back to showing the first two lines of `notes`, which is the
   `Plan: /Users/…` handoff line: correct, and useless to anyone deciding what
   to pick up next. Take it from the plan's `## What this does`, cut to fit
   (400 characters is the store's ceiling, two or three sentences is the point).

   Leave `project` out. The daemon attributes a card to the calling session's own
   project by pid, and naming it by hand is how a card lands on somebody else's
   board.

   **Two things about `notes`, both measured rather than guessed.** They used
   to become the dispatched session's *entire* command-line prompt
   (`dispatch.argv_for` hands over one positional). That is no longer
   load-bearing for a planned card: `dispatch.start_prompt` builds the
   implement handoff from `plan_path`, because `attach_plan` never rewrote
   the leftover idea. Leading with the `Plan:` line is still what keeps an
   agent-written summary on the safe side of `dispatch.guard` if the card has
   no `plan_path` yet, so do not reorder it. And the board snapshot on
   `/api/state` **truncates the prompt to 400 characters** for the panel,
   while `board.db` keeps the whole thing: the text is not lost, but
   everything past 400 characters is invisible to the person deciding
   whether to press Start. Put the path and the `/ship implement` line first,
   and keep the summary short enough to be read on the card.

   **Do not ask for a column, and do not reach past this tool to get one.**
   `/api/action` `board_create` would let you file straight into In progress, and that
   is precisely the thing `channel_server.py` took away on purpose: a card an
   agent can arm is a loop with no human in it. Arriving in In progress is what a
   dispatch *means*, and pressing Start is the human's half of this skill.

   **`plan` is what attaches the plan.** With it the card lands in **Backlog**
   with `plan_path` set, in this one call, and the reply says so — do not call
   `dark_army_attach_plan` for it. The `Plan:` line in `notes` is only the
   person's pointer: a card whose plan is named in `notes` alone stays in
   Prep, where Start opens a *planning* run no tool can finish, and the card
   sticks there however much work that run does. Only when the tool refuses
   the `plan` argument (a session born before Dark Army learned it) file
   without it, then call `dark_army_attach_plan` once with the same path — and
   file this session's other cards only *after* that attach, because a second
   unplanned Prep card makes the attach fail closed.

   **`dark_army_add_card` present and `dark_army_attach_plan` absent is its own case, and
   it is the common one.** A channel process keeps the tool list it was born
   with for the life of its session, so a session that started before the
   attach verb shipped has the create verb and not the attach verb — the card
   is filed and simply stays in **Prep** with no plan. Do not treat that as the
   attach refusing: check whether the tool exists at all before reading a
   refusal into its silence. When it is missing, say so in the summary in one
   plain sentence — the card is in Prep, the plan is written and its path is on
   the card — and name the plan path as the handoff. Do **not** press Refine's
   route onto the user as the repair: Refine dispatches a *fresh* planning
   session that will write a second plan from scratch, unaware of the one that
   already exists, which is the failure this note exists to prevent.

   **Codex's restricted board MCP can provide `dark_army_add_card`, `dark_army_attach_plan`
   and `dark_army_close_card`.** Attach first; create only under the attribution rule
   above. An older session can lack these tools: print the plan path and say
   attachment did not happen. Never bypass missing attribution through a generic
   board API write.
4. If the tools are not there at all, say so in one sentence and carry on to
   Phase 5b anyway. This is a real case, not a bug to work around: a machine
   where Dark Army's board server is not registered has no `dark_army_add_card`
   and no `dark_army_attach_plan` (only the channel needs `server:dark-army`).
   Born before the rename, a session has them as `mcp__bob__bob_*`. The plan
   file is still written, and naming its path is then the whole handoff.

**The plan's follow-ups are filed here and only here.** After the plan's own
card, each `## Out of scope` bullet beginning `Follow-up card:` becomes one
Prep card (`notes` beginning `Follow-up from: <plan slug> — <abs plan path>,
Out of scope`, `stages: ["bc-planner"]`); with no add verb, list them in the
summary. The implement run never files them, so there is one card each.

Then tell the user, and keep it to four things:

- the card's title, and the column it actually landed in — **Backlog** when the
  attach succeeded, **Prep** when the attach verb was missing. State the column
  the board is really showing, never the one the happy path would have produced:
  a summary claiming Backlog over a card sitting in Prep is a lie the user acts
  on, and they find out by pressing Start on a card the plan gate refuses;
- the plan path;
- the plan's `## What this does`, quoted;
- any Phase 4 WARN, one plain sentence each (see Phase 4).

**Do not paste the technical zone**, do not ask for `accept`, and do not offer
`iterate`. There is nothing to hold the turn open for: a plan that turns out to
be wrong gets edited when it is picked up, and the card is where that is
noticed. Implementation starts from the card or existing explicit
authorization; do not add an approval question.

Leave **no** `<!-- bob-tldr -->` and **no** `<!-- bob-actions -->` on that
message. Both of those mark a turn that is waiting on somebody, and this one is
not — writing one is how the session lands back under *Needs you*, which is the
exact outcome this phase exists to avoid.

## Phase 5b: close out

After the card is filed, write the summary above as your message and then,
as the **very last act** of the run, run
`bash .claude/skills/ship/close-out.sh --plan`. The plan is on the card, so
the tab has nothing left to read: it frees the baseline worktree and closes
this planning terminal, for Claude, Codex and Grok alike, but only when Dark
Army's board names this session as a card's planning run; otherwise it
prints `close-out: terminal left open; …`. After a close say nothing;
when it prints a `close-out:` line instead, end the turn on that line,
verbatim, as the last line of your final message (that line is what files
the tab under Idle rather than *Needs you*). Never retry a refusal and
never `/clear`. Editing the project copy does not update the
installed helper.
