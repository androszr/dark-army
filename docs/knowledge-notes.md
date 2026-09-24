# A project's knowledge notes

The long form of what `CLAUDE.md` states in three sentences: the per-project
question-and-answer store, the two channel verbs that reach it, and the
seed-once rule that puts the `/knowledge` skill into a project exactly once.

Lifted out of `CLAUDE.md` for the reason the other contract documents were
(6 Sep 2026): that file is the current contract and is
held under 30,000 UTF-8 bytes (`host/tests/test_claude_md_size.py` pins the
ceiling), and this is the argument behind one line of it.

## The store

`host/dark_army_daemon/knowledge_store.py`. One table, `knowledge_entries`,
in `board.db`:

```sql
CREATE TABLE IF NOT EXISTS knowledge_entries (
    root TEXT NOT NULL, key TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '', answer TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    last_confirmed REAL NOT NULL DEFAULT 0,
    stale TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (root, key)
);
```

- **`card_runs`' shape.** A table rather than columns on `cards`, created by
  `CREATE TABLE IF NOT EXISTS` inside `BoardStore.connect()`, with **no
  `_ADDED_COLUMNS` entry**. An older build's tolerant `SELECT * FROM cards`
  cannot see it and cannot blank it; knowledge landed at schema 20 as a
  sibling table, and `SCHEMA_VERSION` stays **22** — the three extra columns
  (`last_confirmed`, `stale`, `source`) are PRAGMA-driven ALTERs in
  `_connect_knowledge`, not a cards migration.
- **`last_confirmed`** is 0 until a person confirms. **`stale`** is `''` off /
  `'1'` on, person-set, not auto-aged. **`source`** is `agent` or `person`;
  empty on old rows displays as agent.
- **`board_outcome_store`'s wiring.** A sibling module mixed into `BoardStore`
  (`class BoardStore(KnowledgeStoreMixin, OutcomeStoreMixin)`), sharing that
  store's connection and its reentrant lock, with `_connect_knowledge()` as the
  last line of `connect()`.
- **Not card-scoped, on purpose.** A project's purpose and its constraints
  outlive every card that happened to be open when somebody wrote them down, so
  the table is keyed on the enrolled **root** and is named at none of
  `board.py`'s three card-deletion sites and in no orphan sweep. It survives
  `delete`, `clear_done` and archival.
- **No surface may write it.** It names no member of `board._WRITABLE` and no
  key in `ApiServer._BOARD_FIELDS`.

Three verbs. `knowledge_for(root)` returns that root's rows ordered by `key`
and `[]` for an empty root — **there is no all-roots mode and there never
should be**: a method that could return every project's rows would put the
scoping `if` in the daemon rather than in the shape of the API.
`knowledge_count(root)` is its counter. `knowledge_put(root, key, question,
answer, author)` returns `(ok, detail)`.

`knowledge_put`'s bounds, and which of them refuse:

| Thing | Rule |
|---|---|
| `key` | normalised to `[a-z0-9._-]{,64}` (`normalise_key`); empty after that **refuses** |
| `question` | clamped at 400 (`close_note`'s clamp-don't-refuse rule) |
| `answer` | clamped at 4000; empty after stripping **refuses** |
| `root` | empty **refuses** |
| count | a **new** key past `MAX_ENTRIES_PER_ROOT` (80) **refuses**; an *update* never does — a project at its limit must still be able to correct what it said |

The write is one `INSERT … ON CONFLICT(root, key) DO UPDATE`, so **`created_at`
never moves**: the row records when the project first answered that question,
and re-answering is not a new fact appearing. `author` is the writing session,
so a person can see who wrote each line.

## The two verbs

`dark_army_knowledge_read` and `dark_army_knowledge_write`, in `channel_server.py`, on the
**Claude** branch of `tools_for_host` alone. Both names are in `call_tool`'s
`known` set, so a Codex caller gets `tool unavailable for codex` rather than
`unknown tool` — omitting a tool from `tools/list` is not a guard.

**The scoping is an omission.** Neither schema declares `project`, `root`,
`session_id` or `card_id`, and there is no "all projects" mode. The read takes
**no arguments at all** (an empty `properties` object) and ignores anything
that arrives anyway; the write takes exactly `key`, `question`, `answer` with
`required: ["key", "answer"]`.

`daemon_board._knowledge_place` is the one resolution seam, and it is
`_handle_board_card_request`'s opening with that path's one widening removed —
**a named project is not consulted, because there is no way to name one**:

1. `port` → `_board_request_session_fresh(port)`; an unattributable request is
   refused outright.
2. `self._board is None` → `KNOWLEDGE_STORE_REFUSAL`.
3. `_session_place(session_id)`, falling back to the channel's own attached
   `cwd` (the session's own statement of where it is, and there a snapshot tick
   before the row is), then `dispatch.normalise_root`.
4. **Reachability and identity are two different questions, and the notes are
   keyed on the second.** Membership is tested on that **cwd**, in
   `_known_project_roots()` — already intersected with the enrolment ledger —
   so reach is exactly the card path's. What is *stored* is
   `enrollment.root_enrolled(cwd)`: the longest enrolled root containing it.
   Either answer empty is `KNOWLEDGE_UNPLACED_REFUSAL`. Both are asked in one
   executor hop (`_knowledge_reach`), because `_known_project_roots` already
   reads the ledger per root.

**Why the two are not the same folder.** `_known_project_roots()` is the
folders of every open VS Code window ∪ the cwd of every live session, so a
session started in `<proj>/host` — which is this repo's own instructions — is a
member of it in its own right. Keying on that cwd gave the subdirectory its own
bucket: a session at the project root read `entries: []` and the skill re-asked
every question somebody had already answered. Found by the bug scan on the
first iteration and invisible to the suite until then, because
`tests/conftest.py`'s autouse door patches `root_enrolled` to identity — the
tests for it put that back (`_real_enrolment`).

**What a forger gains.** Winning the attach race buys reading and overwriting
the notes of the project whose session it displaced — a project it already has
a session inside. Nothing runs, nothing is typed, no card is created or moved,
no file outside `board.db` is written, and no other project's rows are
reachable. That is strictly less than `dark_army_add_card` already tolerates, which
puts a row on a human's board. The cost is misleading prose in one project's
notes, bounded by the clamps and by `MAX_ENTRIES_PER_ROOT`.

**`note_key`, never `key`.** Every message on the hook socket carries the
project's *enrolment* key as `key`, stamped by `ChannelServer._keyed`, which
fills the field only when it is empty. A catalogue key sitting there would be
read by `BobDaemon._enrolled_root` as an enrolment key, resolve to nothing, and
get the write refused at the door on every call. One reserved name, one rename;
`test_knowledge_channel.py` drives the real `_keyed`.

**Three lists a new answered type must join**, and the third is the sharp edge:
`CHANNEL_MESSAGE_TYPES`, `_route_channel`, and the hardcoded tuple in
`_handle_message` that answers an unenrolled request **with a reply**. Without
the third, a session in an unenrolled project blocks for the full 5s
`Server.CALL_TIMEOUT` on every knowledge call.

**The read is bounded, and every shortening is stated.** The store's own
limits allow 80 entries × 4000 characters — ~320 KB, order 80k tokens — and the
skill's method calls the read **first**, on every run, so an uncapped result
would spend most of a session's context before the first question. The cap is
`channel_server.render_knowledge`, not the store: the store's job is to keep
what a person said, and this is a *rendering* for a model. It lives in
`channel_server.py` because that file is *copied* to
`~/.dark-army/dark-army-channel` and may import nothing from the
package.

Each answer clamps at `READ_ANSWER_CHARS` (600) and the whole block at
`READ_RESULT_CHARS` (24000). Four rules, and three of them are about one
failure:

- **The block cap `break`s; it does not `continue`.** Stopping at the first
  entry that does not fit omits every later one whatever its size. Skipping
  the oversize entry and appending the smaller ones after it returns a by-key
  list *with a hole in the middle*, and to a model that is indistinguishable
  from those keys being unanswered — so `SKILL.md`'s step 2 picks them as the
  next questions and `dark_army_knowledge_write` replaces the stored answer, with no
  delete verb and no confirmation anywhere on the path. A cap that costs
  somebody the words they filed is worse than a shorter reply. (Bug scan
  iteration 2; the first test passed only because every fixture entry was the
  same size, so the pin now uses entries of differing sizes.)
- **The omitted keys are named**, belt and braces on top of the ordering, so a
  model that ignores the order still cannot read them as unanswered. Bounded
  by `MAX_ENTRIES_PER_ROOT` × `MAX_KEY_CHARS`.
- **`shortened` counts the appended block, never the entry.** An answer trimmed
  and then dropped by the block cap is not in the reply, and the trailer's one
  job is to let the model tell a trimmed answer *in this reply* from a
  complete one.
- **At least one entry always comes back**, however long. An empty reply reads
  as "nothing is answered", which is the same lie by another route.

**Nothing rides SSE.** No `state()` section, so `_OMITTABLE_SECTIONS` is
unchanged and the measured frame-size problem is not reopened. No
`LAN_ACTIONS` / `REMOTE_ACTIONS` entry, no `_BOARD_FIELDS` key, no
`event_log` kind. Channel `knowledge_put` continues and must not set
`last_confirmed` or clear `stale`.

## The human reader

Loopback `GET /api/knowledge?root=` is token-gated (`X-Bob-Token`;
`Authorization: Bearer` 403; empty Origin allowed on GET). The sealed kind
`knowledge` carries `root` in the JSON body, never a query string, on
`_lan_home`'s allowlist **and** `_sealed_run`, `log`'s rule (no lease, neither
action tuple). The named root must be an exact member of
`enrollment.enrolled_roots()` after `enrollment.normalise`; empty or unenrolled
is refused in words, never `knowledge_for("")`. No all-roots dump. Cap 300_000
bytes, `break` not skip-and-continue.

Person writes `knowledge_confirm` / `knowledge_stale` / `knowledge_edit` are
loopback `BOARD_ACTIONS` only, handled before the `card_id` check, **not** on
`LAN_ACTIONS` / `REMOTE_ACTIONS` / `_LAN_BOARD`. A sealed `action` with those
names 404s. Confirm sets `last_confirmed`, `source='person'`, `stale=''`. Mark
stale sets `stale='1'` and does not move `last_confirmed`. Edit updates prose
as a person and does not confirm or un-stale. There is no delete verb.

`knowledge_supported` / `knowledge_writable` ride `_pipeline_writable()`.
Absent decodes false: Settings → Knowledge and the phone screen are drawn
**absent**, never empty. Mac: Settings → Projects, one reused knowledge
window. Phone: Profile → Knowledge, picker over enrolled roots, read-only.

`render_knowledge` prefixes `STALE` when `stale=='1'` and `UNCONFIRMED` when
`last_confirmed` is missing/0. The `[key]` token stays. Cap semantics
unchanged (`break` not `continue`, omitted keys named, at least one entry).

## Seed-once

`pack_install.SEED_ONCE_KEYS` — the `/knowledge` skill and its question
catalogue in both trees, plus the `openai.yaml` `pack_render.mirror_skills`
synthesises from the `.claude` copy. Those five keys are written **only where
the destination does not already exist**, as the **first** branch of
`_write_render`'s per-key loop, above the `SPLICED_KEYS` and `SETTINGS_KEY`
forks: seeding is a decision about whether to write at all, and it must not be
reachable past a fork that has already merged something.

`dest.exists()` rather than `dest.is_file()`: a directory sitting at that path
is also "already present". `_destination` realpaths, so a **symlink** is judged
by its target — a link to a real file reads as present and is left alone. The
one gap is a **broken** link, which both predicates call absent: there the seed
proceeds and writes the link's missing target, which `_destination` has already
confirmed is inside the project. Recorded rather than left accidental
(`test_knowledge_pack_seed.py`).

**Why, and what it costs.** A project answers its own questions, and a resync
that reinstated the shipped catalogue would throw those answers away. The other
half of the bargain is that a later fix to the shipped text never reaches a
project that already has the file; the escape hatch is deleting the file so the
next resync re-seeds it, and the skill's own header says so. This is the single
largest cost of the chosen distribution.

**And it freezes the mirror too.** Seeding both trees and then leaving both
alone means a project that reworks its `.claude` catalogue leaves the
`.agents` one on the shipped questions, under different keys. The mirror keys
are **not** removed from `SEED_ONCE_KEYS` to fix that — `_unlink_strays`
sparing an edited mirror depends on their being in the rendered mapping, and
dropping them would put the clobber one directory over. The cost is paid in
words instead: the skill's own header says editing one copy does not update the
other, and that deleting the `.agents` copy re-seeds it from the **shipped**
text rather than from yours. It bites the day Codex or Grok gets these verbs;
today they have neither.

The decision needs the filesystem, so it lives in `pack_install` and nowhere
else — `pack_render.py` stays pure
(`test_agent_pack_contract.test_renderer_is_pure`). `PACK_DESTINATIONS` is
**not** widened: `.claude/skills/` and `.agents/skills/` already admit these
paths. The mechanic is deliberately **not** extended to
`agent_pack/template/.claude/skills/probe/SKILL.md`, which the pack still
overwrites; that is a separate decision about files already installed in
projects.

## This repo's own four copies

Dark Army's checkout is refused as a pack destination
(`pack_install._is_bobs_own`), so nothing installs the skill for us.
`tools/sync_knowledge_skill.py` is that install: canonical is the **template**
pair, targets are `.claude/skills/knowledge/` and `.agents/skills/knowledge/`.
Bytes in, bytes out; atomic replace; a target already in step is not touched;
`--check` writes nothing and exits 1 naming every drifted file. Pinned by
`host/tests/test_knowledge_skill.py`, which also parses the catalogue's
structure — lettered sections, one `key:` line each, one `>` prompt, three or
more option bullets, keys unique and unchanged by the store's own
`normalise_key`.

This is **not** the seed-once rule. A project that has the file keeps it for
ever; this repo's four copies are held byte-identical on purpose, because a
drift here would mean the text we ship and the text we run are two different
texts.
