# Ship token efficiency — the measured contract

<!-- ship-efficiency: claim key=coverage value=static -->
<!-- ship-efficiency: claim key=runtime_input_reduction value=unmeasured -->
<!-- ship-efficiency: claim key=cost_saving value=none -->
<!-- ship-efficiency: claim key=usage_missing value=yes -->
<!-- ship-efficiency: claim key=reduced_default value=pending -->
<!-- ship-efficiency: claim key=shunt value=unmeasured -->

What the *ship token efficiency* plan changed about how much a
`/ship` run makes its agents read, what was measured, what was **not**, and
the map that proves nothing was lost on the way. `python3
tools/ship_efficiency.py` computes every figure here from files on disk; it
launches no agent and executes no recorded command. **Static counts,
observed usage and money are three different things and are kept in three
sections below.**

## What changed

- **`CLAUDE.md` is a compact root** — the universal invariants, the
  architecture map and a table naming which subject document holds each
  area's full contract — under a 30,000-byte ceiling
  (`host/tests/test_claude_md_size.py`). The detail moved **verbatim** into
  four subject documents: `docs/context-board.md`, `docs/context-panel.md`,
  `docs/context-host.md`, `docs/context-development.md`.
- **`docs/agent-context.json` chooses documents, never paragraphs.** The
  union of what a plan's surfaces select, what every changed path selects and
  the cross-cutting additions; an unknown surface, an unmapped path or an
  empty scope selects every reference. Uncertainty never selects less.
- **The ship workflow is written once.** `.claude/skills/ship/SKILL.md` and
  `.agents/skills/ship/SKILL.md` are thin adapters; the workflow is
  `.claude/skills/ship/references/common.md`, `plan.md` and `implement.md`.
  Plan mode loads common + plan, implement mode common + implement, and a
  change of mode loads the other reference. The generic agent pack has the
  same split under its template, rendered and mirrored by the existing pipeline.
- **Handoffs are packets, not conversations**, the blind verifier's packet
  omits the implementer's report entirely, and repairs pass the changed delta
  and the unresolved findings only (`references/common.md`, *The handoff
  packet*).
- **A command runs once per verifier pass, and evidence never crosses a
  role** (`references/implement.md`, Phase 6f; `bc-verifier.md`; the
  implementer keeps its own table). The two full-suite runs stay: one per role.
- **Stale statements corrected**, each named here rather than dropped
  silently: the planner's and auditor's warnings that `CLAUDE.md` still
  documented the deleted menu-bar dropdown; the integration reviewer's threat
  model saying the app talks to nothing on the network (the LAN door and the
  relay are the security reviewer's) and its `NSAlert` sentence; the
  verifier's parenthetical about `test_statusline.py` being known-failing;
  and the TODO rule, which said the file is read whole.
- **Models, reasoning, sandboxes, role independence, the six-dispatch cap,
  the verify and audit limits and the review triggers are unchanged**
  (`host/tests/test_ship_provider_parity.py`, `test_agent_briefs.py`,
  `test_ship_context.py`).

## Static measurements

Byte counts of the explicit load sets — the files a role is told to read
completely — not observed request tokens and not billing. A path is counted
once per load set. The baseline is the tree after the Codex ship reliability
prerequisite landed (`host/tests/fixtures/ship_efficiency/baseline-inventory.json`,
recorded by `tools/ship_efficiency.py inventory --source` on that tree under
the legacy rule: every role reads `AGENTS.md`, the whole `CLAUDE.md` and its
brief; the parent reads the whole skill). **Its `CLAUDE.md` is the saved
pre-ship copy, 140,021 bytes** — the one figure every table here uses and
the same bytes `claude-md-baseline.json` digests; the fixture's `source`
field says so, like the paragraph fixture's. The original audit figures in the plan
(`CLAUDE.md` 139,987 bytes, `AGENTS.md` 6,931, the Claude skill 34,175) are
the same tree a few commits earlier; the post-reliability baseline is the
denominator used here, and the difference between the two is the
prerequisite's own growth, not an efficiency effect.

The "Now" column is the tree on 21 Sep 2026 after the wall-time cuts
(462 bytes under the 28% ceiling); the subject documents and the briefs grow with the
contract (the shunt delegation section alone added roughly 1.5 KB to every
brief between the two passes), so the figure moves and `inventory --check` is
what holds the floor.

| Document | Baseline bytes | Now |
|---|---:|---:|
| `CLAUDE.md` | 140,021 | 23,429 |
| `AGENTS.md` | 7,232 | 9,038 |
| `.claude/skills/ship/SKILL.md` (adapter) | 39,848 | 6,867 |
| `.claude/skills/ship/references/common.md` | — | 13,956 |
| `.claude/skills/ship/references/plan.md` | — | 15,206 |
| `.claude/skills/ship/references/implement.md` | — | 29,445 |
| `docs/context-host.md` | — | 54,627 |
| `docs/context-panel.md` | — | 41,482 |
| `docs/context-development.md` | — | 9,781 |
| `docs/context-board.md` | — | 49,500 |

Three mandatory scenarios, each the sum of every role's load set (the parent
in its mode plus the roles the scenario spawns), measured by
`inventory --root . --check`, which fails unless the aggregate is at least 28%
below the baseline (30% until 21 Sep 2026; the paragraph below says why):

| Scenario | Surfaces | Baseline bytes | Now | Smaller by | References selected |
|---|---|---:|---:|---:|---|
| `plan-only` | tools | 346,588 | 125,350 | 63.8% | development |
| `python-only` | host | 658,695 | 508,349 | 22.8% | host, panel |
| `cross-surface` | host, panel | 972,113 | 789,564 | 18.8% | host, panel |
| **aggregate** | | **1,977,396** | **1,423,263** | **28.0%** | |

**The floor is the aggregate, not the row.** The plan's criterion — and the
only thing `inventory --check` refuses on — is the three scenarios *summed*
at least `LOAD_REDUCTION` below the summed baseline; at 30% that was
1,384,177 bytes against the day's 1,346,527, **37,650 bytes of headroom**.
Two of the three scenarios are individually under the floor and that is not
a failure; it is the cost of the cross-cutting rule, stated below.

**21 Sep 2026 — the floor moved from 30% to 28%.** The headroom the contract
reserved for growth had been spent to 76 bytes by the Mission Control, run
health and shunt paragraphs. The out-of-scope-findings rule
(the *out of scope findings to prep card* plan) grows the auditor
and reviewer briefs and `references/implement.md` by about 11 KB weighted —
`implement.md` and the auditor count twice, `common.md` three times. A rule
that stops fix rounds growing saves more runtime input than it costs in
prompt bytes, and the person chose to move the floor rather than cut contract
text: `LOAD_REDUCTION = 0.28` in `tools/ship_efficiency.py`, the 28% ceiling
being 1,423,725 bytes. The baseline itself is untouched. The headroom is what the next
person editing a subject document spends: a scenario reads every selected
subject document once per role, so a byte added to `docs/context-host.md`
counts once per load set that selects it — three in the python-only
scenario, five in the cross-surface, eight in all (a five-byte edit to the
host document moved the aggregate by 40 while this pass was open) — so about
4,700 bytes of new host or panel contract flips the floor.
A byte added to a brief or to `AGENTS.md` counts in every scenario it is read
in, so those are dearer still.

**25 Sep 2026 — the host document gave bytes back.** Other sessions'
uncommitted edits had spent the headroom and more: on the live tree
`inventory --check` printed `scenario loads: 1,977,396 -> 1,450,800 bytes
(26.6% smaller)`, 27,075 weighted bytes over the 1,423,725 floor, so any new
line in a subject document failed a card that had not caused it. The
*trim role load context host* plan moved the hook handler's long aside
(relocated paragraph `3171734ffe6d`) on from `docs/context-host.md` into
`docs/hook-door-contract.md`, *The handler*, and shrank the host document's
Live Activity paragraph to a pointer at the phone and transport contracts
that already state every rule it held; the host document went from 56,423 to
48,261 bytes. Afterwards: `scenario loads: 1,977,396 -> 1,385,504 bytes
(29.9% smaller)`, **38,221 weighted bytes under the floor**. The rule this
adds: a relocated paragraph may move on from its subject document into the
long-form contract that document points at, when the task that needs it is
the one that reaches that contract; the map names the new file, the
*Edited after the move* row says where it went, and no role loads the
long-form contract by default, so the move costs nothing in any scenario.
`LOAD_REDUCTION` and the baseline are unchanged. This supersedes the
21 Sep figure for the host document in the table above.

The python-only scenario changes `session_stats.py`, and the session state
model is drawn by both clients, so the map's cross-cutting rule adds the
panel document to that scope — the figure was 47.9% while the rule added the
host document alone, and it is 27.4% now that the rule says what its own
`why` says. The rule was widened to match its reason, not narrowed to hit a
budget. The cross-surface figure is the honest one: a change reaching the
daemon and the panel reads two of the four subject documents, and most of the
old root was those two subjects. The full-contract fallback (every subject
document) is allowed to exceed the budget by design.

**Duplicate reads and commands.** The instruction set no longer asks any role
to read a document twice: the roots are read once, a subject document once,
and the parent no longer carries both modes. The verifier's execution table
removes the second full-suite run that the acceptance criterion and the
standing convention used to name separately inside one pass; the implementer's
and the verifier's suites remain two runs by design. These are counts of what
the instructions ask for; whether an agent obeys them is what the runtime
evaluation below would have measured.

## Observed usage

**Not measured in this run.** No agent was launched by this card, so there
are no observed input, output, cache-read, cache-write or elapsed figures,
and every such field is **unavailable** — never zero. The runtime target the
plan set — a ≥15% median observed input reduction over paired complete runs —
was therefore **not measured** and is **not claimed**. `tools/ship_efficiency.py
report --baseline <runs> --candidate <runs> --output <file>` is ready to
import run evidence when a bounded evaluation is run: it aggregates each
unique provider/session/turn once, never adds a parent total known to include
its children to those children, keeps cold and warm cache records apart,
computes the median only over complete pairs and lists every excluded pair
with its reason. `host/tests/fixtures/ship_efficiency/runs-*.json` are
synthetic shapes for that importer, not observations.

## Delegation

What the shunt skill (the *shunt delegation layer* plan,
`.claude/skills/shunt/SKILL.md`) recorded: every `bulk_read.py` and
`code_write.py` call appends one line to a per-session ledger under
`~/.dark-army/shunt/`, and `python3 tools/ship_efficiency.py shunt
--ledgers <dir> --output <file>` counts them — `delegations`,
`lines_kept_out` (the lines of the files the wrappers sent to the helper or
wrote from it; a delegation whose helper failed is counted with no lines,
because the caller then read the files itself) and `worker_cost`, in total
and per provider and per mode, de-duplicated by `delegation_id`. `worker_cost` is **`unavailable` unless
every record carries a measured USD figure**: Claude's `-p --output-format
json` reports `total_cost_usd`; Codex and Grok report nothing, and nothing is
invented. The same fold puts three figures on a card's *What changed* record
(`work_record.read_shunt_ledger`, `shunt_words`), so the person reading a
finished card sees "3 delegations kept 2,140 lines out of the main model;
helper cost unknown" in the daemon's own words.

**Not measured on this landing.** No ledger exists yet — the skill is being
installed by this change — so there is no delegation figure here, and the
figures that would come are counts of what the wrappers recorded, **not** a
measurement of the main model's context, its input tokens or its cost, and
not a comparison with what the same run would have read without the helper.
The 90% context reduction the Spotify Portal write-up reports for its own
setup is **not claimed**; whether any reduction holds here is what a paired
run with and without the guard would show, and it has not run. A sub-agent
on Codex or Grok with a session id of its own writes its own ledger file,
which this report counts and no card reads. **A read-only role delegates
nothing**: the verifier, the bug auditor and the two reviewers edit no file
and read nothing through a summary, so their briefs carry the guard
exemption alone (`exempt.py on`, read the file whole; pinned by
`test_agent_briefs.py` and `test_agent_pack_contract.py`) and a ledger of
theirs is empty by design, not by omission.
`host/tests/fixtures/ship_efficiency/shunt-ledger-sample.jsonl` is a
synthetic shape for the counter (five delegations, one duplicate id, one
measured cost), not an observation.

## Bounded agent evaluation

<!-- ship-efficiency: evaluation scenario=1 status=not-run claims=none -->
<!-- ship-efficiency: evaluation scenario=2 status=not-run claims=none -->
<!-- ship-efficiency: evaluation scenario=3 status=not-run claims=none -->
<!-- ship-efficiency: evaluation scenario=4 status=not-run claims=none -->
<!-- ship-efficiency: evaluation scenario=5 status=not-run claims=none -->
<!-- ship-efficiency: evaluation scenario=6 status=not-run claims=none -->

The six paired scenarios (clean planning-only; clean implementation with a
duplicate pytest criterion; an unmet acceptance promise; a functional fault
outside the criteria; a resource referenced from source but absent from the
package; a content-only capability widening off the watched path list) **were
not run** in this run: the implementing session may not launch agents, and
the plan's own rule is that a run without evidence claims nothing. The
procedure stands ready and is checked by `tools/ship_efficiency.py
validate-evaluation docs/ship-efficiency.md --fixtures
host/tests/fixtures/ship_efficiency`, **which exits non-zero on this
document today** and names the six missing runs on stdout: a missing run or
inaccessible evidence fails that check, and nothing in this tree can make it
pass without the twelve records.
(`host/tests/fixtures/ship_efficiency/evaluation-synthetic/` holds a complete
*synthetic* set of the twelve, so the validator's own pass path is tested;
those files are shapes, not observations, and the shipped check does not
point at them.)

1. Snapshot the baseline instruction set (the post-reliability tree) and the
   candidate (this tree) into two isolated copies of the repository.
2. For each scenario, run baseline and candidate once each, alternating the
   order, with the same model, reasoning, tool availability, input fixture and
   initial tree; every reviewer in a fresh session; no live app installation
   and no board write.
3. Scenarios 3–6 carry hidden expected blocker or finding labels held by the
   evaluator, never in a reviewer prompt.
4. Record each arm as `scenario-<n>-<arm>.json` in the fixtures directory —
   source-linked, redacted, with the seeded faults, what was caught, whether
   the clean verdict held, the gates that ran, the models and reasoning, and
   whether any pass crossed a role or any verifier saw the implementer's report.
5. Mark each scenario `status=ran` in this document only when both records
   exist; the validator then requires the candidate to catch every seeded
   critical fault and every finding the baseline caught, preserve clean
   verdicts, run every required gate, keep models and reasoning unchanged,
   share no pass across roles and leak no report. It also refuses vacuous
   evidence: a record marked `synthetic`, an empty `gates_run`, unstated
   `models` or `reasoning`, a `verdict` outside SHIP / ITERATE / STOP / PASS /
   FAIL / BLOCK, and — on scenarios 3–6 — an empty `seeded_faults` or a
   SHIP / PASS verdict on either arm, because a planted fault that did not
   stop the work was not caught. Any miss disables the reduced default
   pending repair.

**The reduced default is pending that evaluation.** The compact root is the
only root in the tree — there is no baseline `CLAUDE.md` beside it that a
role falls back to — so `pending` means the quality half of the acceptance
is unproven, not that a baseline default is in force. The plan enables the
reduced default only after the preservation checks *and* the fault-detection
half pass, and the fault-detection half has not run. What the static checks
prove — preservation, coverage, budgets, routing parity, an instruction set
that widens on uncertainty — is proved; what they do not prove is that
deliberately planted faults are still caught, and this report claims nothing
about it. The compact instruction set is in the working tree because that is
where the work is done; it is not a claim that the quality half of the
acceptance was satisfied, and `validate-evaluation` says so by failing until
the twelve records exist. Running the evaluation is the outstanding item on
this card: outstanding, not yet on the board.

## Money

No cost figure is claimed, estimated or implied. Actual billed cost enters a
report only when a provider supplies an attributable amount with its currency
and provenance; fewer bytes of instruction is not a smaller bill, because
automatic instruction injection, repeated tool reads, inherited history,
compaction and provider caching change both accounting and cost.

## Preservation map

Every paragraph of the old `CLAUDE.md` (140,021 bytes, 172 paragraphs
after de-duplication; `host/tests/fixtures/ship_efficiency/claude-md-baseline.json`
holds their digests) is either **retained** in the compact root, **relocated**
verbatim to one subject document — or, since 25 Sep 2026, on into a long-form
contract the map names — or a **deliberate exception** with its reason
and replacement. A digest is the first twelve hex characters of the SHA-256
of the paragraph with its whitespace folded, so a re-wrap survives and a
changed word does not. The subject documents are living contracts, so a
relocated paragraph a later change edits in its new home is found by its
recorded opening (the fixture's first 70 characters) and listed by
`inventory --check` as *edited after the move* — a note, because the
paragraph travelled whole and what the edit did to it is that change's own
review — while a paragraph found by neither is lost, and a problem. The
opening is an anchor and not a proof, so it is held against the length the
fixture recorded (`words`): a head-matched block under half that length is
refused as **gutted** (`GUTTED_FRACTION`), a problem and never a note, and
every note states `old→new words` so a reader can judge the rest.
`inventory --check` and `host/tests/test_ship_context.py` re-derive this map
from the files and fail on any paragraph that has no home.

**The baseline is the saved pre-ship copy of the file, and nothing else.**
The fixture is written by `tools/ship_efficiency.py baseline-paragraphs` from
the copy of `CLAUDE.md` the ship run saved before it began (the working tree's
file, `HEAD 5f02fd9` plus the uncommitted edits then present, 140,021 bytes).
`HEAD` with the pre-ship patch reapplied by hand does not reproduce those
bytes (it yields 140,055), and an earlier fixture built that way disagreed
with the true baseline in nine paragraphs; the fixture's `source` field says
where its bytes came from. Four sentences the earlier fixture had lost are
back in their paragraphs: the phone's `PipelineView` staying
(`docs/context-board.md`), the enrolment scoping rule's "not a local process
that reads the key file" (`docs/context-host.md`), the two reasons for the
strip's pixel art and the icon's LANCZOS resample (`docs/context-development.md`),
and the background-task figure "399 of 2,038, 20 Sep 2026", which
`docs/context-host.md` carries verbatim and `docs/session-state-contract.md`
states in full. One paragraph was extended by a concurrent change while this
card was open (the access-log alert floor, `ALERT_FLOOR_SECONDS`): that
sentence stands as its own paragraph in `docs/context-host.md`, so the baseline
paragraph beside it is still relocated whole.

**Edited after the move.** The relocated paragraphs concurrent work has since
edited in their new homes, as `inventory --check` notes them on 20 Sep 2026.
`test_the_preservation_map_resolves_and_every_old_paragraph_has_a_home` bounds
the notes by this table: a note for a digest not listed here is a relocation
nobody has accounted for and fails the test, so a later edit to a relocated
paragraph adds its row here in the same change. Word counts are the fixture's
against the tree on that date; the digests are the bound, the counts a reading.

| Digest | Opening | Now in | Words then → now |
|---|---|---|---:|
| `3171734ffe6d` | It carries two standing hints into every Claude session | `docs/hook-door-contract.md` (moved on from the host document, 25 Sep 2026: the *trim role load context host* plan) | 766 → 1090 |
| `73a633db9cf2` | Two art trees, one roster. Twenty nickname characters | `docs/context-development.md` | 191 → 218 |
| `8c7abaa2a11f` | bash # Strip: snap the hand-drawn GIF | `docs/context-development.md` | 68 → 63 |
| `e4d8c73fad3c` | Specialists.swift keeps seven stage de | `docs/context-panel.md` | 98 → 107 |
| `3698506c2f36` | What Dark Army observed a run do is a fifth writer ring, and it is a table | `docs/context-board.md` | 294 → 310 |
| `43b3b222d567` | The board (`BoardView.swift`, with | `docs/context-panel.md` | 135 → 153 |
| `5390d3970fe7` | 1. Only a deliberate human gesture put | `docs/context-board.md` | 93 → 100 |
| `69e00bc67248` | Field \| Written by \| Why not wider | `docs/context-board.md` | 232 → 257 |
| `ae654eeeee34` | Both card verbs ask for a summary besi | `docs/context-host.md` | 218 → 219 |
| `4e8667646457` | Everything waiting on a person is one list | `docs/context-panel.md` | 200 → 254 |
| `624fde40351a` | The daemon acts on a session in exactly three places | `docs/context-host.md` (the phone leg is filtered, gated and held, 22 Sep 2026) | 83 → 134 |
| `70a13ec1fd8e` | Close out afterwards — `bash .claude/skills/ship/close-out.sh` | `CLAUDE.md` (retained, edited in place: leaves the terminal open) | 178 → 138 |
| `9ca0c1ed7e6a` | Any request for a change to this codebase goes through `/ship` | `CLAUDE.md` (retained, edited in place: leave the terminal for the person) | 61 → 64 |
| `d2fdb422a543` | And a way to finish reading. `WrapUpBar` closes the terminal tab | `docs/context-panel.md` (close-out posts `close_terminal` only under `--close`) | 267 → 269 |
| `6611c6b0b977` | ```bash # Python tests (host daemon) cd host && .venv/bin/pytest -v | `docs/context-development.md` (the parallel full-suite line, 21 Sep 2026) | 23 → 57 |
| `6640b9eb9f83` | `board.py` — the Kanban store, and the writer rings | `docs/context-board.md` | 125 → 125 |
| `929bbe62b430` | `paths.py` — `ensure_state_dir` is the seam every read and write funnels through | `docs/context-host.md` | 131 → 136 |
| `a258cd9e62e7` | Codex observation and control | `docs/context-host.md` | 152 → 152 |
| `c0edeaae6851` | `terminal_title.py` — the agent's badge on its own tab | `docs/context-host.md` | 231 → 323 |
| `d0c2d8e32680` | Inside the rail, projects are tabs and the fleet is an htop table | `docs/context-panel.md` | 841 → 841 |
| `7e3c27c40067` | Phone sheets, detents, the terminal cover, the 4s/8s cadence | `docs/context-host.md` | 129 → 145 |
| `3bc874381db8` | The LAN door and the relay are two sea | `docs/context-host.md` | 175 → 211 |
| `64bda82b7bfb` | BoardCardSheet is one surface for re | `docs/context-panel.md` | 210 → 249 |
| `3a2cc71a589f` | Prepare writes a card's fields from plain words | `docs/context-panel.md` (the Codex helper model, 22 Sep 2026) | 554 → 554 |
| `aefb499e503f` | A frame that says nothing is not sent | `docs/context-host.md` (the echo floor and the once-per-frame build, 23 Sep 2026) | 96 → 101 |
| `118846449ed7` | `?sections=changed` is the opt-in that keeps an unchanged section off | `docs/context-host.md` (no idle frame to a slim client, the third opt-in `?cards=delta`, 23 Sep 2026) | 128 → 93 |
| `5ae52505c5ca` | The ledger survives delete, Clear Done and archival | `docs/context-board.md` (only moved samples written, 23 Sep 2026) | 89 → 89 |
| `cc83b1808503` | `?done=review` is the second opt-in | `docs/context-host.md` (the real news slot's name, 22 Sep 2026) | 168 → 168 |
| `130d1aa9bad7` | Claude Code hooks (the data-flow diagram) | `CLAUDE.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 105 → 105 |
| `6493eadfcdb8` | The product is called `Dark Army` | `CLAUDE.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 166 → 155 |
| `09d9af449624` | The build flags table | `docs/context-development.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 55 → 72 |
| `12b0deee60fe` | `enrollment.py` — which projects | `docs/context-host.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 95 → 95 |
| `3429cecc2fc5` | The panel is an ordinary macOS window | `docs/context-panel.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 309 → 309 |
| `30335a2130cf` | A card is never moved to Done by the daemon | `docs/context-board.md` (the channel's tools renamed `dark_army_*`, 23 Sep 2026) | 22 → 22 |
| `390f28c44216` | Refine (a Prep card) → `refine_card` | `docs/context-board.md` (the channel's tools renamed `dark_army_*`, 23 Sep 2026) | 63 → 63 |
| `88620543ce52` | Refinement is `dispatch_card`'s sibling | `docs/context-board.md` (the channel's tools renamed `dark_army_*`, 23 Sep 2026) | 180 → 180 |
| `a0cda1a78752` | A card reaches Done only because somebody | `docs/context-board.md` (the channel's tools renamed `dark_army_*`, 23 Sep 2026) | 219 → 219 |
| `ad0d0a6af80e` | How important a card is, is the third | `docs/context-board.md` (the channel's tools renamed `dark_army_*`, 23 Sep 2026) | 183 → 183 |
| `d46331c22687` | A card can name its area (`Areas.swift`) | `docs/context-panel.md` (the channel's tools renamed `dark_army_*`, 23 Sep 2026) | 177 → 177 |
| `38c4fb148689` | dark_army_menubar/ — macOS status bar app | `docs/context-host.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 991 → 991 |
| `45762d689958` | `session_title.py` — the name a row wears | `docs/context-host.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 105 → 105 |
| `61e33fa6fa29` | dark_army_daemon/ — Async Python daemon | `docs/context-host.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 204 → 204 |
| `67b511833b04` | The pid walk is memoised per session | `docs/context-host.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 85 → 85 |
| `7241dbd2b0fb` | `channel_server.py` is the one route | `docs/context-host.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 269 → 269 |
| `9a7ecdd64f41` | `event_log.py` — the Mac's diary | `docs/context-host.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 99 → 99 |
| `b21323c80b45` | The most damaging bug available here is the walk-up | `docs/context-host.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 60 → 70 |
| `b9c8624f1beb` | bob-companion-notify — Standalone hook handler | `docs/context-host.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 103 → 109 |
| `c71aacfefc02` | A SwiftUI/AppKit executable (`BobPanel`) | `docs/context-panel.md` (the installed paths moved to Dark Army, 22 Sep 2026) | 37 → 39 |
| `4d925876cccc` | Do not end the turn on "shall I build it?" | `CLAUDE.md` (the product-name sweep, 22 Sep 2026) | 53 → 54 |
| `2f8029f1db92` | A card may name the model | `docs/context-board.md` (the product-name sweep, 22 Sep 2026) | 89 → 90 |
| `4088b45f3aef` | `ptyhost.py` and `pty_broker.py` keep a hosted terminal | `docs/context-host.md` (the product-name sweep, 22 Sep 2026) | 166 → 167 |
| `4577b76a0050` | Every card carries a change number | `docs/context-board.md` (the product-name sweep, 22 Sep 2026) | 164 → 166 |
| `59180d68faf7` | Two splits are deliberate | `docs/context-board.md` (the product-name sweep, 22 Sep 2026) | 88 → 89 |
| `6b05f9d006e1` | Two of the three assistants knock; one is filtered | `docs/context-host.md` (the product-name sweep, 22 Sep 2026) | 112 → 113 |
| `846bdb9820b0` | Prepare also offers an opinion about which project | `docs/context-panel.md` (the product-name sweep, 22 Sep 2026) | 251 → 252 |
| `899e989df0ea` | Which side decided each | `docs/context-panel.md` (the product-name sweep, 22 Sep 2026) | 108 → 109 |
| `8dfe52ea92c8` | The drop is the dispatch | `docs/context-panel.md` (the product-name sweep, 22 Sep 2026) | 134 → 135 |
| `a6940c4394e4` | A claim ends when the work does | `docs/context-board.md` (the product-name sweep, 22 Sep 2026) | 161 → 162 |
| `ad232a6f9f92` | Cards follow the rename, and they must | `docs/context-board.md` (the product-name sweep, 22 Sep 2026) | 74 → 75 |
| `b40865bf2b54` | At most one item per row and one per card | `docs/context-panel.md` (the product-name sweep, 22 Sep 2026) | 89 → 90 |
| `d87f40572036` | Extension dependencies use `npm ci` | `docs/context-development.md` (the product-name sweep, 22 Sep 2026) | 37 → 38 |
| `387a53d2fbc0` | One source of truth for the buckets | `docs/context-host.md` (`confused` counts only beside a card, 22 Sep 2026) | 61 → 70 |
| `87a6fa8164e0` | The gate lives inside `_dispatch_card_locked` | `docs/context-board.md` (card dependencies, 24 Sep 2026) | 84 → 74 |

| Old section | Paragraphs | Where they are now |
|---|---:|---|
| `# CLAUDE.md` | 2 | deliberate exception (below) |
| `## Project Overview` | 6 | retained in `CLAUDE.md` |
| `### Panel` | 2 | `docs/context-development.md` |
| `### Menu Bar App (.app bundle)` | 1 | `docs/context-development.md` |
| `# Releases omit --allow-untagged and require a clean vX.Y.Z tag.` | 4 | `docs/context-development.md` |
| `### Tests` | 2 | `docs/context-development.md` |
| `# Venv: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt` | 2 | `docs/context-development.md` |
| `### Cast Pipeline` | 7 | `docs/context-development.md` |
| `### Data Flow` | 1 | retained in `CLAUDE.md` |
| `### Data Flow` | 12 | `docs/context-board.md` |
| `### Panel (`panel/`)` | 32 | `docs/context-panel.md` |
| `### Host (`host/`)` | 32 | `docs/context-host.md` |
| `### Host (`host/`)` | 36 | `docs/context-board.md` |
| `### Session State Model` | 5 | `docs/context-host.md` |
| `## Key Constraints` | 3 | retained in `CLAUDE.md` |
| `## How a request becomes work` | 7 | retained in `CLAUDE.md` |
| `## TODO Tracking` | 1 | deliberate exception (below) |
| `## Reviewing a change` | 1 | `docs/context-development.md` |
| `## Reviewing a change` | 1 | retained in `CLAUDE.md` |
| `# GitNexus — Code Intelligence` | 1 | retained in `CLAUDE.md` |
| `# GitNexus — Code Intelligence` | 1 | deliberate exception (below) |
| `## Always Do` | 6 | retained in `CLAUDE.md` |
| `## Never Do` | 4 | retained in `CLAUDE.md` |
| `## Resources` | 1 | deliberate exception (below) |
| `## CLI` | 2 | retained in `CLAUDE.md` |

```json
{
 "exceptions": {
  "9d240e931e6b": {
   "replacement": "CLAUDE.md, '## Key Constraints', '**Menu-bar width**: `STRIP_BUDGET_PT` (310pt)'",
   "why": "The strip budget went from 300pt to 310pt on 25 Sep 2026 when a three-digit to-do count measured 300.2pt and the ladder dropped the Codex cluster; the figure is inside the paragraph's recorded opening, so the opening could not stay '(300pt)'. The rest of the paragraph is unchanged."
  },
  "413b39a0ca65": {
   "replacement": "docs/context-host.md, 'Eight tools, the only inbound verbs here'",
   "why": "The inbound channel verbs went from seven to eight (bob_attach_report); the opening word is the count, so the paragraph's opening could not stay 'Seven tools'."
  },
  "47bfe5c79b1b": {
   "replacement": "none — TODO.md retired 22 Sep 2026 (the *retire todo md* plan); the CI sentence is in CLAUDE.md '## Build and test'",
   "why": "'Read TODO.md at the start of a session' described a whole-file read; the rule became a bounded read (latest three dated sections, widened by heading or topic), and on 22 Sep 2026 the to-do file, its archive and every rule keeping them were retired (the *retire todo md* plan). The paragraph's CI sentence survives in CLAUDE.md '## Build and test'."
  },
  "7222001e5826": {
   "replacement": "CLAUDE.md, first paragraph; host/tests/test_claude_md_size.py",
   "why": "The 140,000-byte ceiling sentence is stale: the root's ceiling is 30,000 bytes now and the test pins the new figure."
  },
  "b738f1813304": {
   "replacement": "CLAUDE.md, the GitNexus block's '## Resources' table (tool-managed)",
   "why": "The GitNexus resources table opens with the index's registry name in its first `gitnexus://repo/<name>/` row, and the indexer rewrites it between the gitnexus:start/end markers. The re-index of 23 Sep 2026 (the *reindex gitnexus dark army name* plan) renamed the index to dark-army, so the opening could not stay; the four rows and their meanings are unchanged."
  },
  "c49bd0cd3165": {
   "replacement": "CLAUDE.md, the GitNexus block (tool-managed)",
   "why": "The GitNexus banner line ('This project is indexed by GitNexus as <the index name> (N symbols, ...)') is rewritten by the indexer on every `analyze` run between the gitnexus:start/end markers; its counts moved during this run and are the tool's, not a rule."
  },
  "cd8285a58210": {
   "replacement": "CLAUDE.md, first paragraph",
   "why": "The one-line header ('Project guidance for coding agents.') was replaced by a paragraph stating the compact contract and its ceiling."
  },
  "3a47448626a1": {
   "replacement": "docs/context-development.md, '**The app icon and the top-bar mark are one picture.**'",
   "why": "On 22 Sep 2026 the Dock tile and the top-bar mark became the same glitch face. The paragraph opened by saying they were two pictures on purpose, so the opening could not stay."
  },
  "39c21aa5042d": {
   "replacement": "docs/context-development.md, '**Cipher is chief of staff; the other nineteen lead eight areas.**'",
   "why": "The paragraph opens with the chief of staff's callsign, which changed in the 22 Sep 2026 cast rebrand, so the opening could not stay."
  },
  "f89ec2ae1817": {
   "replacement": "CLAUDE.md, '## Project Overview': 'Dark Army monitors Claude Code sessions: a Python daemon reads hooks,'",
   "why": "The product-name sweep of 22 Sep 2026 (the *scrap bob names for dark army* plan) changed the paragraph's opening word: the old product or package name opened it, so the opening could not stay. The paragraph itself is where the replacement says, its rule unchanged."
  },
  "59f446d87b63": {
   "replacement": "docs/context-panel.md, '**Dark Army talks while it waits; nothing spins**'",
   "why": "The product-name sweep of 22 Sep 2026 (the *scrap bob names for dark army* plan) changed the paragraph's opening word: the old product or package name opened it, so the opening could not stay. The paragraph itself is where the replacement says, its rule unchanged."
  },
  "b9c8624f1beb": {
   "replacement": "docs/context-host.md, '- **dark-army-notify** — Standalone hook handler'",
   "why": "The product-name sweep of 22 Sep 2026 (the *scrap bob names for dark army* plan) changed the paragraph's opening word: the old product or package name opened it, so the opening could not stay. The paragraph itself is where the replacement says, its rule unchanged."
  },
  "61e33fa6fa29": {
   "replacement": "docs/context-host.md, '- **dark_army_daemon/** — Async Python daemon (asyncio)'",
   "why": "The product-name sweep of 22 Sep 2026 (the *scrap bob names for dark army* plan) changed the paragraph's opening word: the old product or package name opened it, so the opening could not stay. The paragraph itself is where the replacement says, its rule unchanged."
  },
  "12b0deee60fe": {
   "replacement": "docs/context-host.md, '- **`enrollment.py`** — **which projects Dark Army is allowed to watch at all**'",
   "why": "The product-name sweep of 22 Sep 2026 (the *scrap bob names for dark army* plan) changed the paragraph's opening word: the old product or package name opened it, so the opening could not stay. The paragraph itself is where the replacement says, its rule unchanged."
  },
  "b21323c80b45": {
   "replacement": "docs/context-host.md, '**The most damaging bug available here is the walk-up.** Dark Army's own state folder'",
   "why": "The product-name sweep of 22 Sep 2026 (the *scrap bob names for dark army* plan) changed the paragraph's opening word: the old product or package name opened it, so the opening could not stay. The paragraph itself is where the replacement says, its rule unchanged."
  },
  "3aad295e9317": {
   "replacement": "docs/context-host.md, '**Dark Army's own checkout enrols itself on upgrade, and nothing else does**'",
   "why": "The product-name sweep of 22 Sep 2026 (the *scrap bob names for dark army* plan) changed the paragraph's opening word: the old product or package name opened it, so the opening could not stay. The paragraph itself is where the replacement says, its rule unchanged."
  },
  "3698506c2f36": {
   "replacement": "docs/context-board.md, '**What Dark Army observed a run do is a fifth writer ring, and it is a table.**'",
   "why": "The product-name sweep of 22 Sep 2026 (the *scrap bob names for dark army* plan) changed the paragraph's opening word: the old product or package name opened it, so the opening could not stay. The paragraph itself is where the replacement says, its rule unchanged."
  },
  "268ea9d4e385": {
   "replacement": "docs/context-board.md, '- **`dispatch.py`** — Dark Army as launcher'",
   "why": "The product-name sweep of 22 Sep 2026 (the *scrap bob names for dark army* plan) changed the paragraph's opening word: the old product or package name opened it, so the opening could not stay. The paragraph itself is where the replacement says, its rule unchanged."
  },
  "38c4fb148689": {
   "replacement": "docs/context-host.md, '- **dark_army_menubar/** — macOS status bar app (rumps)'",
   "why": "The product-name sweep of 22 Sep 2026 (the *scrap bob names for dark army* plan) changed the paragraph's opening word: the old product or package name opened it, so the opening could not stay. The paragraph itself is where the replacement says, its rule unchanged."
  },
  "09d9af449624": {
   "replacement": "docs/context-development.md, '| Flag | Effect |' under 'Menu Bar App (.app bundle)'",
   "why": "The `--install` row opens the paragraph and named the old bundle's retirement, which the 23 Sep 2026 retirement of the migration code made false."
  },
  "c71aacfefc02": {
   "replacement": "docs/context-panel.md, '## Panel (`panel/`)': 'A SwiftUI/AppKit executable (`BobPanel`). Reads `~/.dark-army/api-token`,'",
   "why": "The paragraph opened by naming the token file through the old settings folder's path (`~/.bob-companion/api-token`'s successor) inside its first seventy characters; the 23 Sep 2026 retirement of the migration code left it the last sentence routing a reader through the old name, and its follow-up (the *reword panel context opening* plan) reworded the sentence to name only `~/.dark-army/api-token`. The rest of the paragraph is byte-identical."
  }
 },
 "relocated": {
  "01f77691b8b4": "docs/context-panel.md",
  "04a6c347a220": "docs/context-board.md",
  "087f61c816e8": "docs/context-panel.md",
  "0a59f229f29d": "docs/context-board.md",
  "118846449ed7": "docs/context-host.md",
  "140c71e6f1c9": "docs/context-board.md",
  "173ed4d685d0": "docs/context-board.md",
  "184597b10778": "docs/context-development.md",
  "23c8974d9c19": "docs/context-board.md",
  "24d42b03d713": "docs/context-development.md",
  "29a9177fe2b9": "docs/context-panel.md",
  "2a7838738357": "docs/context-board.md",
  "2b0f099f586e": "docs/context-panel.md",
  "2f22193dbb3f": "docs/context-host.md",
  "2f8029f1db92": "docs/context-board.md",
  "30335a2130cf": "docs/context-board.md",
  "3171734ffe6d": "docs/hook-door-contract.md",
  "3429cecc2fc5": "docs/context-panel.md",
  "347b12b81587": "docs/context-host.md",
  "3494e2ad7aac": "docs/context-panel.md",
  "387a53d2fbc0": "docs/context-host.md",
  "390f28c44216": "docs/context-board.md",
  "397599bd0eff": "docs/context-board.md",
  "3a2cc71a589f": "docs/context-panel.md",
  "3b72522bd9ed": "docs/context-panel.md",
  "3bc874381db8": "docs/context-host.md",
  "4056d44de344": "docs/context-board.md",
  "4088b45f3aef": "docs/context-host.md",
  "43b3b222d567": "docs/context-panel.md",
  "45762d689958": "docs/context-host.md",
  "4577b76a0050": "docs/context-board.md",
  "462277a7bf44": "docs/context-board.md",
  "46ac20be2379": "docs/context-host.md",
  "4a16e136909b": "docs/context-panel.md",
  "4d0321430695": "docs/context-host.md",
  "4e8667646457": "docs/context-panel.md",
  "514b3ced68b0": "docs/context-board.md",
  "52502ad3235f": "docs/context-panel.md",
  "5390d3970fe7": "docs/context-board.md",
  "54785d73681c": "docs/context-development.md",
  "57bdfa7e207c": "docs/context-panel.md",
  "59180d68faf7": "docs/context-board.md",
  "5a2e75388331": "docs/context-board.md",
  "5ae52505c5ca": "docs/context-board.md",
  "602f3af9a2c4": "docs/context-panel.md",
  "624fde40351a": "docs/context-host.md",
  "62d471b527bd": "docs/context-host.md",
  "64bda82b7bfb": "docs/context-panel.md",
  "6611c6b0b977": "docs/context-development.md",
  "6640b9eb9f83": "docs/context-board.md",
  "67b511833b04": "docs/context-host.md",
  "681bcab59880": "docs/context-development.md",
  "68f8526827aa": "docs/context-development.md",
  "69e00bc67248": "docs/context-board.md",
  "6b05f9d006e1": "docs/context-host.md",
  "6cb1e04effd6": "docs/context-board.md",
  "6d0674c31f24": "docs/context-panel.md",
  "7241dbd2b0fb": "docs/context-host.md",
  "73a633db9cf2": "docs/context-development.md",
  "7d1fd4e9af60": "docs/context-board.md",
  "7e3c27c40067": "docs/context-host.md",
  "8159d716d328": "docs/context-board.md",
  "81e740756c3a": "docs/context-panel.md",
  "846bdb9820b0": "docs/context-panel.md",
  "87a6fa8164e0": "docs/context-board.md",
  "88620543ce52": "docs/context-board.md",
  "89151e3518e5": "docs/context-host.md",
  "899e989df0ea": "docs/context-panel.md",
  "8c7abaa2a11f": "docs/context-development.md",
  "8dfe52ea92c8": "docs/context-panel.md",
  "91bca2b35cb8": "docs/context-host.md",
  "929bbe62b430": "docs/context-host.md",
  "9474c08e9d7e": "docs/context-host.md",
  "96c75879b53b": "docs/context-panel.md",
  "989ed746ac93": "docs/context-panel.md",
  "9a38cdce7bc4": "docs/context-board.md",
  "9a7ecdd64f41": "docs/context-host.md",
  "9e08aa01f70c": "docs/context-host.md",
  "a0cda1a78752": "docs/context-board.md",
  "a258cd9e62e7": "docs/context-host.md",
  "a448c508b07c": "docs/context-development.md",
  "a55e92a6b4c9": "docs/context-host.md",
  "a6940c4394e4": "docs/context-board.md",
  "a85d1eeef27d": "docs/context-board.md",
  "a95084cf761c": "docs/context-panel.md",
  "ac1e783183f6": "docs/context-development.md",
  "ad0d0a6af80e": "docs/context-board.md",
  "ad232a6f9f92": "docs/context-board.md",
  "ae654eeeee34": "docs/context-host.md",
  "aee5a611d5c2": "docs/context-board.md",
  "aefb499e503f": "docs/context-host.md",
  "b15238b891f6": "docs/context-board.md",
  "b40865bf2b54": "docs/context-panel.md",
  "b79ba37518fe": "docs/context-panel.md",
  "bb512c236907": "docs/context-host.md",
  "bc38244bf2ab": "docs/context-board.md",
  "c0edeaae6851": "docs/context-host.md",
  "c531624135b8": "docs/context-board.md",
  "c789627ddbf0": "docs/context-board.md",
  "c79af5f046f6": "docs/context-board.md",
  "c9f6912ee3d4": "docs/context-development.md",
  "cc6f02a04e07": "docs/context-board.md",
  "cc83b1808503": "docs/context-host.md",
  "d0c2d8e32680": "docs/context-panel.md",
  "d2fdb422a543": "docs/context-panel.md",
  "d46331c22687": "docs/context-panel.md",
  "d6c7057236ac": "docs/context-development.md",
  "d76179495c41": "docs/context-host.md",
  "d7b7c5e6cd98": "docs/context-board.md",
  "d87f40572036": "docs/context-development.md",
  "d8dc25fe23c8": "docs/context-development.md",
  "dbcfa3643416": "docs/context-board.md",
  "dbd2798a76b4": "docs/context-development.md",
  "dccf23073986": "docs/context-board.md",
  "e4d8c73fad3c": "docs/context-panel.md",
  "e7202dcecf54": "docs/context-board.md",
  "ea1597ef2abb": "docs/context-board.md",
  "ec32208975d6": "docs/context-panel.md",
  "ef8c55b1ccd8": "docs/context-board.md",
  "f02da1dba3ba": "docs/context-board.md",
  "f67313e5ece0": "docs/context-development.md",
  "fe576598d895": "docs/context-board.md"
 },
 "retained": [
  "6493eadfcdb8",
  "cfb4d0b112d3",
  "af163777a480",
  "3fc05a2f95b4",
  "e152c6d53fae",
  "130d1aa9bad7",
  "b772522344c7",
  "1fcccf69db95",
  "f09c6def7fda",
  "9ca0c1ed7e6a",
  "b2c01b42c8e6",
  "4d925876cccc",
  "70a13ec1fd8e",
  "5954c0889294",
  "1f5ad9d2b175",
  "cf994d2145e0",
  "4f68d53deb5e",
  "55b6a74fa169",
  "beca796bc978",
  "24efa1cfad19",
  "e90c55a520e5",
  "7abbb86d7b6e",
  "ef0ca6e23b55",
  "fdb55632ccb4",
  "1b5d259b2d60",
  "7be4bd66b770",
  "96146118dfc3",
  "d90352092611",
  "7cf9f4c3c667"
 ]
}
```

## Wall time — the 21 Sep 2026 audit

Token loads were measured above; **time** was not, and time is what the
person waits on. Four implement runs of two small cards on 21 Sep 2026 read
from their transcripts and gate logs:

- The full host suite (9,270 tests) took **9:00–10:10 single-process** under
  fleet load, and each card ran it **four to six times** — the implementer
  once or twice, the verifier once plus a whole-suite replay in a baseline
  worktree (10 min, 70 foreign failures to classify), and a repair round all
  over again. 40–55 of each cut run's 70 minutes were pytest wall time; both
  cut runs ($38, $40) never reached the handoff.
- The baseline was never green: `tests/test_review_fixes.py` (a helper
  missing the field commit `f6f2dee` added) and `tests/test_ship_context.py`
  (a retained `CLAUDE.md` paragraph edited in place, which the preservation
  check counted as lost) were red on every run, so every run spent attempts
  deciding what was PRE-EXISTING.
- Every card ran every phase: four helpers minimum, and any `ios/BobPhone/`
  path sent the card through security review with nothing to find.
- The ledger row, the delta digest and the worktree recipe were retyped
  inline per gate, slowly and not always the same way.
- Not a cause: the shunt guard (threshold 1,200 lines here; three refusals
  that day).

What changed, and what it measured:

- **`host/pytest.ini`** gives every test a 300 s ceiling (`pytest-timeout`)
  and the full suite runs `-n auto` (`pytest-xdist`): **3:14 across eight
  workers** against 9–10 minutes, 9,267 passed. Both plugins are in
  `requirements-dev.txt`; a single-file run stays one process. The
  same-day test audit (21 Sep 2026) then cut 180 tests and
  the critical path — the notify session-start pair, the parse-all-sources
  check, the terminal picture test, the pty teardowns — and `--dist
  loadfile` keeps a file's tests on one worker: **2:43 across eight
  workers, 9,166 passed**, the longest single test now the phone's
  parse-all at 26 s.
- **`.claude/skills/ship/gate.sh`** — `snapshot`, `dispatch`, `run`,
  `baseline`, `classify`, `lane`, `digest` — is the one copy of the ledger
  row, the budget and same-failure stops (exit 3 and 4), the text-only
  Phase 0 patch (binaries excluded so a dirty `.vsix` cannot poison
  `git apply`), the baseline replay **by id** in a worktree under `$SCRATCH`
  (never the whole suite, never this tree), and the review floors.
  `host/tests/test_ship_gate.py` runs it on a throwaway repository.
- **Three failure classes.** `YOURS` (the delta touches the test, its
  module, or a tree the test greps, or the id is green at the baseline),
  `PRE-EXISTING` (red at the baseline too — never fixed in the run) and
  `IN-FLIGHT` (a candidate dirty now, clean at this run's baseline, and
  absent from the delta — including `ios/BobPhone/` for phone source-grep
  tests whose Swift moved under another card). Two cards building in one
  tree stop sorting each other's red tests. An unbuildable baseline still
  classifies those dirty sources as `IN-FLIGHT`, not `YOURS`.
- **The full suite runs twice per clean run** — the implementer's once at the
  end, the verifier's once — and after a fix only the failing ids and the
  targeted files, the full suite at most once more when the fix reached a
  module outside them.
- **The lane.** `gate.sh lane` reads the delta: at or under 150 changed lines,
  no integration path, no security floor → `fast`, and Phases 6.7–6.9 are
  skipped and said so in the handoff; otherwise `full`. The judgment floors
  still apply on a fast lane.
- **The security path floor names the phone's doors**, not every phone file.

**21 Sep 2026 — wall time, second cut.** Classify: `docs/` sibling is
`IN-FLIGHT` (inventory tests). Orchestrator packets paths; one impact,
one grep. Verifier cites the full suite for a subset pytest. `LANE:
full` spawns 6.7–6.9 together. New API prose goes in the long-form
contract; inventory is a standing convention; a load-set REQ-minor does
not `ITERATE` when the floor is already over.

Not changed: the role loads. `CLAUDE.md`'s rule that a selected document is
read whole stands, and `inventory --check`'s margin (28% floor) is the bound
on any brief growing.

## Limitations

- Byte counts are not tokens: the plan's rule of four characters per token is
  approximate, and non-ASCII text is longer in bytes than in characters.
- Explicit load sets are what the instructions ask for. Automatic instruction
  injection by an assistant, a tool read an agent chooses to make, inherited
  history and compaction are not counted and were not observed.
- Keyword and parity tests prove routing and required text, not model
  behaviour; the bounded evaluation above is what would, and it has not run.
- The map proves paragraphs survived relocation; it does not prove an agent
  reads the subject document the map selects. The rule that uncertainty widens
  is the guard, and it is a rule in prose.
- A delegation to the shunt helper costs a round trip of 10–30 seconds, so
  a small delegation is counterproductive and a project with many files just
  over the 350-line threshold pays that latency often; the threshold
  (`env.BOB_SHUNT_MIN_LINES` in the project's `.claude/settings.json`) is the
  dial, and the ledger records time per delegation without judging it.
