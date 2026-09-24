# Delivery leads and areas

Cipher is Dark Army's chief of staff. Refine requests are named Cipher when
that name is free. Overwatch is art-only: the planner banner depicts Cipher's
alter ego, and no running session is assigned that face.

Every other character leads one product area. A card's Area can be selected
on the card or in a composer. Its plan header seeds an empty field at attachment
and at launch; Prepare offers a suggestion only into an empty field. A person's
existing selection wins. Clearing an area is allowed, but a later launch can
seed it again from the plan header.

## The eight areas

1. **Backbone** — services, data & transport. Everything that runs without a
   screen: daemons, servers, stores, sockets, pipelines, schedulers.
   Strengths: asyncio/concurrency, idempotent writes, schema migrations,
   bounded queues, forward-compatible reads, sockets & SSE, market-data
   ingestion. Evidence: ~80 daemon-first plans here (hooks, pty broker, SSE
   limiter, enrolment), a sibling finance project's live quote stream, Neon/Drizzle, NBP
   FX, caching.
2. **Desk** — the Mac window & menu bar. Native desktop surfaces: AppKit
   windows, key routing, terminal pane, status strip. Strengths: AppKit
   lifecycle, NSEvent monitors, occlusion/visibility gates, layout from
   constants, the CRT theme, SwiftTerm. Evidence: 53 panel-only plans.
3. **Pocket** — phones & widgets. iPhone apps, widgets, push, background
   refresh, offline/reconnect, accessibility. Strengths: Dynamic
   Type/VoiceOver, sealed transport clients, sheet ladders, WidgetKit, APNs,
   WKWebView shells & touch controls. Evidence: 12 phone-only + 31
   three-surface plans here; a sibling finance project's iOS pivot, widgets, trend lights,
   push alerts; arpg's iOS shell & joystick.
4. **Ledger** — numbers that must be right. Financial and game arithmetic,
   measurement, determinism. Strengths: units & rounding, P&L/FX/options
   pricing, TTK/affix budgets/XP curves, golden traces, Monte Carlo,
   "unavailable beats a guess". Evidence: a sibling finance project's options pricing,
   dividends, concentration, drift; arpg determinism & headless; here
   outcomes, lifecycle timing, cost per outcome.
5. **Play** — worlds, art & feel. Game design docs, level/enemy/art briefs,
   gameplay feel, generated assets, brand & cast art. Strengths: design-doc
   discipline, prompt-to-asset pipelines (Meshy, image models), pixel-grid
   constraints, hitstop/telegraph feel, icon baking. Evidence: most of
   arpg-web; here cast portraits, the glitch mark, icons.
6. **Conductor** — agents, prompts & LLM features. Briefs, skills, packs, the
   Prepare helper, crew naming, LLM-generated product features. Strengths:
   prompt craft, tool boundaries, output validation (reject-don't-truncate),
   skill idempotence, model choice, evals. Evidence: /ship, agent pack, card
   preparer, crew, priority scorer; a sibling finance project's day report narrative &
   screenshot import.
7. **Gate** — security, release & operations. Doors into the app and the way
   it ships: enrolment, pairing, sealed transport, tokens, builds, CI,
   TestFlight, logs, kill switch. Strengths: threat modelling, fail-closed
   refusals, reproducible builds, version gates, rotation/retention, no secret
   ever on a snapshot. Evidence: per-project key, seal the home path, relay
   lease, repeatable extension build, release versions, CI phone gate;
   a sibling finance project's security pass & deploy.
8. **Universal** — the fixer. Cards that cut across areas or fit none; audits,
   cleanups, docs hygiene, cross-repo skills. Strengths: breadth, reading a
   whole codebase fast, refactors, docs-match-product sweeps. Evidence:
   claude-md-under-ceiling, docs-match-shipped-product,
   split-large-panel-files, review skill, unify-recovery-diagnostics.

## The roster

| Area | Usual lead | Remaining pool, in order |
|---|---|---|
| Backbone | Relay | Hex, Forge |
| Desk | Vex | Zosia |
| Pocket | Mira | Ptyś |
| Ledger | Audit | Ledger |
| Play | Franio | Quiet |
| Conductor | Velvet | Canon |
| Gate | Nyx | Watch, Captcha, Sawa |
| Universal | Proxy | Androll |

The nineteen delivery leads plus Cipher are the existing twenty assignable
characters. The 22 Sep 2026 rebrand swapped fourteen names in place, so the
hash order did not change. Cipher belongs to no area pool; Overwatch remains
art-only.

The Ledger area and the Ledger character share a word and nothing else. Area
lookups take area slugs and character lookups take nicknames, so a Ledger
member leading its own area reads `Ledger · Ledger lead`, which is correct;
`test_areas.py` pins that `ledger` is the only slug the two namespaces
share.

## Choosing a lead

`areas.py` owns the area table. Both clients carry matching copies verified
by tests. `areas.allocate` tries the usual lead first, then walks the pool in
order for a free name. Busy checks reserve a name's stem, including a suffixed
name. Two Backbone starts can therefore take Relay and Hex.

If every member is busy, the naming caller rejects the allocator's exhausted
copy and uses the ordinary cast-name allocation. A session outside the area's
pool is labeled as a stand-in: `Proxy · Backbone stand-in`. A member of the
pool is labeled as a lead: `Hex · Backbone lead`. These are daemon-composed
lines that the clients draw verbatim, attached only to a stamped card Start
with an area. A card without an area uses the Universal pool for naming and
shows no area line.

Refine requests prefer Cipher. An unrelated, ordinarily named session can
already hold Cipher; in that case Refine keeps an ordinary available cast
name rather than renaming a session somebody is reading. Existing names stay
sticky except for the existing card-binding rename seam.

Start appends the area and its usual lead to the opening prompt. When the
brief is present it also asks the assistant to read
`.claude/leads/<area>.md` first; a missing brief omits that read instruction.
The area line does not change the prompt examined by the dispatch guard.

## The steps under a lead

Stages keep their canonical names: `bc-card-preparer`, `bc-planner`,
`bc-implementer`, `bc-verifier`, `bc-bug-auditor`,
`bc-integration-reviewer` and `bc-security-reviewer`. They have jobs, not
character pools. The crew band draws one area lead face and a marker for
each step, retaining the same step counter. Faces already recorded on older
cards survive; a new recording never rewrites them. A face recorded under a
name the 22 Sep 2026 rebrand retired is shown as the callsign that took its
place (`identity.current_crew`); the stored record is left as it was.

The planner banner keeps Overwatch as the chief of staff's alter ego.
Other ship banners identify the plain agent role.

## The briefs and their delivery

Eight Markdown briefs live under `.claude/leads/`, one per area slug. Each
states the area, strengths, reading and verification checklist, and ordered
lead pool. The shared agent pack's template tree carries project-neutral
copies (22 Sep 2026): the same heading, shape and lead pool, but no Dark Army
contract, file or rule, because they are written into other people's
projects.

For projects that have enabled the managed pack, the next pack sync writes
the briefs **the project's profile picks** (its `areas`, plus `universal`)
under `.claude/leads/`, and removes a brief it no longer ships only when the
file's bytes are one the pack itself once wrote
(`pack_render.shipped_lead_digests`). Dark Army's own checkout is protected
by its self marker; `test_agent_briefs.py` keeps both copies' shape and pools
in step. Per-project brief tuning and changes to the separate
starter-pack repository are outside this change.

## What a lead grants

A lead grants nothing. Areas and names add context, not authority. They
change no dispatch, queue, slot, tool-access, approval or gate rule. Model
settings remain attached to canonical agent roles. A brief is read as
instructions; Start does not load it through a new `--agent` flag.
