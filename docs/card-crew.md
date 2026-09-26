# The crew on a card

A card has an area lead and named steps. Cipher is chief of staff; the other
nineteen assignable faces lead eight areas. The full roster, responsibilities
and evidence are in [Delivery leads and areas](delivery-leads.md).

## The lead and the steps

`Areas.swift` offers the same eight area tiles on Mac and phone, with a
portrait and a `usually <name>` line. An area's lead is an expectation; a busy
usual lead may be replaced by another member of its pool or a stand-in.

`CrewBand`, below the `Specialists` marker, draws the area's lead face once
and a marker for each canonical stage. A known stage has a short label:
`impl`, `verify`, `audit`, `integ`, `sec`, `plan` or `prep`. The active stage
still determines the step counter, and a pending stage still comes only from
`workflow`. Observed undeclared stages append to the list; no stage is
invented to fill a gap.

The band remains a memory. `record_agents` writes new stage names with no
allocated faces. Existing entries in `crew_trail` are setdefault-shaped and
never rewritten, so a card that recorded a character keeps showing that face
— or, for a slug the 22 Sep 2026 cast rebrand retired, the callsign that took
its index (`identity.current_crew`, applied on read; the column is untouched).
The snapshot's `crew` is absent where empty. Unknown helper names retain the
existing lowercased djb2 fallback.

Both card faces use the strip (`stripCap = 4`, the running stage never
dropped), and both card screens use an uncapped grid. Accessibility text
sizes switch the strip to the grid. The counter stays `step N of M` while a
stage is active and `N of M` otherwise, absent with no stages. `caption` and
the visual counter read the same `counterText`; changing the portraits does
not change the arithmetic.

## Naming the session

`_card_roles_by_session()` reads `(kind, card_id, area)` from the last
published board state. `_role_nickname` prefers Cipher for `refine` and the
area's first free lead for `start`; no area uses Universal. An occupied
usual lead yields to the next free name in pool order. The busy set reserves
stems, so a suffixed nickname also holds its base name.

`identity.ART_ONLY` is always excluded. Overwatch belongs to no session or
area pool: it is the planner banner's face, Cipher's alter ego. If Cipher is
already held, Refine uses ordinary cast allocation. A session started outside
Dark Army can legitimately hold Cipher by hash, and its name is not taken
away. An exhausted area pool's honest copy is rejected by `_role_nickname`,
which falls back to ordinary cast allocation rather than duplicate a live
name.

The preference lands twice. `_assign_nicknames` passes it to
`IdentityStore.name_for(..., preferred=)` for the first pick. Reconciliation
can bind a card after its session was first published, so both binders call
`_rename_for_role`: the one call site of `IdentityStore.reassign` and the
existing narrow exception to stickiness. A later snapshot does not keep
renaming the session.

For a stamped card Start with an area, the daemon composes `area_line`, such
as `Relay · Backbone lead` or `Proxy · Backbone stand-in`. Both clients
draw it verbatim. Ad-hoc, Refine and unstamped rows gain no area line, and a
tombstone retains the field through the same origin path.

## Only helpers this project has

A card's `workflow` describes expected stages. A changed saved-card editor
value is refused when it names a helper the project does not declare;
create drops undeclared names because its composer has no typed specialists
box to fix. An unchanged value is not re-judged, and an empty roster refuses
nothing. Plan stages are filtered through `board_workflow.keep_known_stages`.
An observed helper remains evidence regardless of whether it was declared.

## The tables

`host/dark_army_daemon/areas.py` owns eight ordered pools. Both clients'
`Areas.all` literals match them, including order. The pools form a total,
disjoint partition of `identity.NAMES` minus Cipher; no pool contains the
art-only Overwatch. `crew.ROLES` keeps the seven canonical stage names alone.

| Area | Pool, usual lead first |
|---|---|
| Backbone | Relay, Hex, Forge |
| Desk | Vex, Zosia |
| Pocket | Mira, Ptys |
| Ledger | Audit, Ledger |
| Play | Franio, Quiet |
| Conductor | Velvet, Canon |
| Gate | Nyx, Watch, Captcha, Sawa |
| Universal | Proxy, Androll |

## The ring

`crew_trail` remains the v19 `TEXT NOT NULL DEFAULT ''` column, written by
`record_agents` alone and outside `_WRITABLE`, `REVISED_COLUMNS` and
`ApiServer._BOARD_FIELDS`. A surface cannot claim a face did a stage.
Schema 23 adds `area`, a person-stated field validated at the store and
included in ordinary card revision checks. Its empty-only seed can never
overwrite a person's selection.

## What a lead grants

Nothing. Dispatch, the plan gate, the queue, slot claims and every access
refusal keep their existing rules. Neither the area nor a portrait is proof
that a step ran. No older card is retro-filled with invented faces.

## Collaboration evidence

The Collaboration disclosure in saved-card and agent details shows explicit
parent relationships separately from observed SendMessage calls. Counts are
cumulative session-level attempts, including unconfirmed or failed calls; they
prove neither delivery nor a card outcome. Multiple exact card links are shown
without allocating a session's count between its tasks. Role/crew trails never
manufacture a historical helper.

The final provider-adjusted agents picture supplies typed session identities
(provider/session ID), owner-qualified helper identities and source-scoped raw
recipient addresses. Exact lookup comes first; with no exact candidate, only a
trailing 4–16 hex reference is stripped. Within either lookup, session addresses
precede helper IDs. Several candidates, reused addresses or conflicting copies
never choose a first winner. Unknown recipient is distinct from known ended,
ambiguous and present without an observed inbox.

A tombstone may retain its exact previously published registry address, with
provenance, inside the existing 30-minute in-memory retention. Explicit ending
evidence earns ended; quiet eviction stays unknown. No inbox path, credential,
message body or historical scan is added. A missing helper is never declared
ended. Restart and retention expiry lose this evidence.

Card focus includes linked implementation/refinement sessions, their observed
helpers and incident messages with immediate peers. It never follows a peer's
whole unrelated task. Links recheck exact provider/session or card ID/root in
the current picture. A helper opens its owning detail; an absent card (including
Done omission) says “Card unavailable in this view.” Desktop card-window links
use existing panel session notifications and board reveal, then close the editor.

Schema version 1 is additive to API state, slim SSE, home and relay reads.
Missing/unknown versions are unavailable, not a complete empty history.
Projection limits are 256 nodes, 512 edges and 128 KiB JSON, with whole-edge
omission and explicit omitted counts. The transcript's separate 32-recipient cap
and provider/ancestry gaps also mark partial coverage. Desktop initially shows
20 connections; phone shows five. Expansion is local, with no new refresh loop.
All raw addresses are inert selectable text. No map control sends a message.

### Synthetic screen check

Both native test suites have isolated journey hosts using the shared fixture,
without connecting a client or creating production hook traffic. Automated
hosting checks layout and model routes; spoken labels and focus still need a
person.

Mac: run
`BOB_COLLABORATION_MANUAL=1 swift test --filter CollaborationTests.testSyntheticJourneyHost`
from `panel`. The fixture window stays open for up to ten minutes (close it to
finish). Open Task, expand Collaboration, follow the helper to its owner, open
an associated card and press Back. Inspect ended, ambiguous, unresolved and
present-without-inbox labels and the observed-call wording with keyboard and
VoiceOver.

Phone: open `ios/BobPhone.xcodeproj`, add `BOB_COLLABORATION_MANUAL=1` to the
scheme's Test environment, and run only
`CollaborationTests/testSyntheticJourneyHostAtLargestAccessibilitySizeAndBack`
on the iPhone simulator. The isolated host stays for ten minutes, starts at the
largest accessibility size, and uses the existing bounded sheet router.
Follow a card, press Back, expand the retained evidence, and use VoiceOver to
read the long recipient. Expect wrapping, exact destinations and no Send control.
Remove the test environment override afterwards.
