# The crew

Seven helpers do the work on this project. Each one has a file under
`.claude/agents/` — that file is the brief the helper itself reads, written for
the helper. **This page is the one a person reads**: who they are, what each one
does, at which point it takes its turn, and whether it is allowed to change
anything.

Cipher is chief of staff; the other nineteen faces lead product areas. The
steps under a lead keep their canonical agent names. A portrait changes neither
a helper's job nor what it may touch. See [Delivery leads and areas](delivery-leads.md).

## The seven

In the order they take their turn.

| Agent | Character | What it does | When it runs | Writes? |
|---|---|---|---|---|
| `bc-card-preparer` | — | Drafts a board card — title, summary, goal and instructions — from a sentence you type. | When somebody presses Prepare on the board. | No |
| `bc-planner` | Overwatch (Cipher's alter ego — chief of staff) | Turns an idea into a written plan a person can read and approve. | First, before anything is built (`/ship` Phase 3), and again on `iterate` (Phase 5). | One new file under `plans/`, and nothing else |
| `bc-implementer` | — | Builds an approved plan, then runs the tests and the lint. | When somebody presses Start (`/ship` Phase 6). | Yes — source, tests and docs. Never commits or pushes |
| `bc-verifier` | — | Answers *did it do what the plan said*, one promise at a time, without reading the builder's own account of it. | Straight after the build (`/ship` Phase 6f). | No |
| `bc-bug-auditor` | — | Answers *is it broken in a way the plan never mentioned*, in the code that just changed, and says of each finding whether it is inside the plan's scope. | After the promises check passes (`/ship` Phase 6.7). | No |
| `bc-integration-reviewer` | — | Answers *does it survive being installed* — as a real app, not only in the test suite. | Last, before release (`/ship` Phase 6.8), and on demand via `/integration-pass`. | No |
| `bc-security-reviewer` | — | Answers *can something outside get in* — the phone, pairing, enrolment, and the secrets the app must never hand out. | Before release (`/ship` Phase 6.9) when the change touches a door into the app, and on demand. | No |

The four checkers each answer a **different** question, and the four questions
are what separates them. A finding that belongs to one of the others is that
one's to report.

## Findings outside the plan

The bug auditor and the two reviewers mark each finding as inside or outside
the plan's scope — judged
against the plan's own *Out of scope* list and its acceptance criteria. Only
in-scope findings send the build back to the implementer. An out-of-scope
finding becomes a new card in Prep naming the card and plan it came from, for
the person to decide on; where the session has no card-filing tool it is
listed under *Follow-ups not filed* in the handoff and appended to the plan,
never dropped. A serious out-of-scope finding stops the run and is put to the
person in five parts — requirement, expansion, smallest compliant alternative,
consequences, recommendation — with three choices: fix it here, file it, or
stop. Those three mark; the implementer never answers its own finding.

## The card is the handoff

Pressing Refine or Start opens a brand-new session that has none of the
conversation that wrote the card. The card, and for Start its plan, is
everything that session gets, so whatever was decided has to be written on
it. A card with empty boxes stays in Prep until somebody fills them in. The
list of boxes a ready card holds is in `docs/context-board.md`,
*A complete Prep card is the handoff*.

## Delivery leads and banners

Area selection belongs to the card. Its usual lead is offered in the Area
picker; Start names the session from the area's available pool. Refine
prefers Cipher, the chief of staff. The crew band draws the lead once and
labels the work beneath it with canonical stages, preserving faces already
recorded on older cards.

| Spawn | Banner identity |
|---|---|
| `bc-planner` | Overwatch, Cipher's alter ego — chief of staff |
| `bc-implementer` | bc-implementer |
| `bc-verifier` | bc-verifier |
| `bc-bug-auditor` | bc-bug-auditor |
| `bc-integration-reviewer` | bc-integration-reviewer |
| `bc-security-reviewer` | bc-security-reviewer |

The eight briefs live under `.claude/leads/` and ship through the managed
agent pack. They guide an area's work and grant no additional permissions.
Model settings and callable role identities remain attached to the standard
agent names, not the cast.

## What each helper reads

Every helper reads `CLAUDE.md` — the compact root — and `AGENTS.md`
completely, then its own brief whole, then the subject documents
`docs/agent-context.json` selects for the plan's surfaces and the changed
paths (`docs/context-board.md`, `docs/context-panel.md`,
`docs/context-host.md`, `docs/context-development.md`). The rule is
conservative: an unmapped path, an unknown surface or an empty scope selects
every subject document, and a reviewer derives its own set from the plan and
the delta rather than inheriting the implementer's. The
`/ship` skill itself is one workflow in three references
(`.claude/skills/ship/references/`) behind two thin adapters, one per
assistant. What that costs and saves in bytes, and what was **not** measured,
is `docs/ship-efficiency.md`.

## Which model each one runs on

The current defaults are defined in `agent_models.SHIPPED` and each role's
resolved configuration. Codex uses Sol for implementation, verification and
integration, Astra for planning, bug audit and security, and Luna for card
preparation and shunt work. Machine and project choices may override these.
Read the actual `model` field; do not copy a model identifier from an old plan.

Change it once for the whole machine under **Agent models** in Dark Army's
settings; a project under **Projects** may say otherwise for any single row
(**Inherit** keeps the machine-wide value). A card that names its own model
still wins for Start. The choice reaches a project's briefs at the press
(and on every install and launch resync); the main-session slot is
applied at every dispatch.

The shared pack includes generic integration and security reviewers; profiles
may supply more specific briefs.

## Local Codex roles

This checkout keeps seven same-named `.codex/agents/*.toml` definitions beside
its seven authoritative `.claude/agents/*.md` briefs. Planner and implementer
use `workspace-write`; the preparer and four reviewers use `read-only`. Every
shim requires complete reads of `AGENTS.md`, `CLAUDE.md` and its own brief, and
keeps the existing high reasoning effort; its `model =` line, like each Claude
and Grok brief's `model:` line, is pinned in place from **Agent models** by
`pack_install.pin_own_checkout`. Otherwise these local files are maintained
separately; Dark Army's own checkout is excluded from pack installation.

Ship checks the actual callable canonical roster before dependent work. Passing
TOML/roster tests does not prove the current host loaded a role: a fresh session
must expose and successfully spawn `bc-security-reviewer` by that `agent_type`.
The [Codex ship troubleshooting ladder](codex-ship.md) records this check and
separates discovery, graph, board and close-out evidence.

## Local Grok roles

Seven same-named `.grok/agents/bc-*.md` shims point at the same briefs. Each
carries one **Capability** line — read-only for the preparer and the four
reviewers, edit-the-checkout (never commit or push) for planner and
implementer — pinned against the role by `host/tests/test_agent_briefs.py`.

## The shape a new agent file copies

Every agent file that carries an introduction opens with the same blockquote,
with the same labelled facts in the same order, directly under the frontmatter:

```
> **What I do:** <one or two plain sentences.>
>
> **When I run:** <the phase in words, plus the `/ship` phase number in
> parentheses where one exists.>
>
> **What I may touch:** <the write scope, in plain words.>
>
> **Common failures:** <only where the file has one; today only bc-planner does.>
>
> **Codename:** <the character, and the sentence saying the codename is
> cosmetic.>
```

The frontmatter `description:` says the same three things in three sentences —
what it does, when it runs, what it may touch — in words a non-programmer can
act on. It is not only decoration: `card_prepare.read_roster` hands every
agent's `(name, description)` pair to the Prepare helper, which picks a card's
expected specialists from it.

**`bc-card-preparer.md` is the one exception, deliberately.** Everything below
its closing `---` *is* the prompt Dark Army sends the Prepare helper — see
`card_prepare._body_after_frontmatter` and `read_brief`, and the byte-for-byte
pins in `host/tests/test_card_preparer_brief.py`. A blockquote added there would
ride inside the helper's prompt on every Prepare. That file's `description:` is
rewritten in this shape; nothing below the frontmatter changes. Its plain
sentence, its turn and its character are the row above.

## The words the read-only helpers share

The four checkers state their write scope in identical words, so four
different-sounding promises stop reading as four different promises:

```
> **What I may touch:** Nothing. It reads, searches and runs commands, and
> never edits a file — the `tools:` line in this file's frontmatter is what
> enforces that, not this sentence.
```

That last clause is the important one: the `tools:` line in the frontmatter is
the guarantee. Prose in a brief is a statement of intent; the tool list is what
the harness enforces.

## Adding one

A new helper needs four things, not one:

1. A file under `.claude/agents/`, in the shape above, plus an un-ignore line in
   `.gitignore` (the directory is ignored with a per-file allowlist).
2. A stage description in `Specialists.table`
   (`panel/Sources/BobPanel/Specialists.swift`, byte-mirrored in
   `ios/BobPhone/Specialists.swift`) and a name in
   `host/dark_army_daemon/crew.py`'s `ROLES`, so the board can label it. `host/tests/test_agent_briefs.py` refuses an agent file with no
   stage and a stage with no agent file.
3. A row in this table, and a row in `.claude/skills/ship/SKILL.md`'s spawn table
   if `/ship` is to spawn it.

4. A same-named `.codex/agents/<role>.toml` referencing the authoritative brief,
   with the correct sandbox, plus a fresh-session native discovery check.
