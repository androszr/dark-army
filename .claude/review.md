# Review profile — dark-army

Read by the `/review` skill. Names this project's reviewer and what "risky"
means here, so a review is about *this* codebase rather than generic.

```yaml
reviewer_agent: bc-integration-reviewer
gitnexus_repo: dark-army
```

## Risk domains

The hermetic test suite cannot see any of these, which is exactly why they
belong in a review. Every one has cost a silent failure here before.

- **The frozen py2app bundle.** A module that imports fine in the checkout and
  is missing once frozen. The checkout inherits a shell's PATH and a real
  package tree; the bundle has neither.
- **The installed hook handler.** `dark-army-notify` lives as the embedded
  `NOTIFY_SCRIPT` string in `hooks.py` and is written out to
  `~/.dark-army/`. It must stay **stdlib-only** — that directory has no
  package to import from. A second divergent copy is the mistake already made
  once here: it imported daemon modules and could never have run.
- **The installed channel copy.** `dark-army-channel` is copied out on every
  launch, but a channel process **already running** keeps its old
  `initialize_result` for the life of that session. A verb added today is simply
  absent in a session opened yesterday — a silent absence, never a wrong action.
  Call it out whenever the channel's tool surface changes.
- **The loopback API and its token.** Writes need `X-Bob-Token`, not
  `Authorization: Bearer`; reads are ungated, so the wrong header looks like it
  works and every write silently 403s. `Host` must be loopback (DNS rebinding),
  and an empty token must fail closed.
- **Persisted state, both directions.** `sessions.json`, `board.db`,
  `titles.json`. A write must never drop a column it did not write, and a read
  must ignore a key it does not know — a downgrade still has to open the file.
- **Destructive verbs and their guards.** `stop_session` is guarded by
  *identity* (the PID must still be the harness recorded); `delete_abandoned_agent`
  by *category*, re-checked at the moment of deletion. Never by whatever a
  surface last rendered.
- **Dispatch.** Dark Army starting a session is the one capability here that acts on
  the world. Only a confirmed human press reaches it; argv only, no shell; the
  root must be a known project; bounds are enforced under a lock, not computed.
- **The panel launch contract.** Exactly one panel, owned by someone. An orphan
  cannot be driven *or* dismissed and is pinned above every Space until killed.
- **Broadcast cost.** Anything that adds to `/api/state` rides every SSE frame.
  The limiter is tuned around ~19 KB; full card prompts already push a
  20-card board to ~165 KB.
- **Ragged payload decoding in Swift.** Synthesised `Decodable` throws on a
  missing key even when the property has a default, and this payload is
  legitimately ragged. One absent field must never blank the panel.

## Notes for the reviewer

- `never_ship_without`: a test for anything in the hermetic half; for the rest,
  a stated manual check — several of these are only observable on an installed
  bundle.
- The house style is **fail closed**: ambiguity refuses rather than guesses. A
  change that makes an ambiguous case resolve to "first match" is a finding.
