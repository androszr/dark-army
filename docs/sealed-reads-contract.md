# Sealed reads contract

The reads a paired phone and the panel may make through the two sealed doors and their loopback twins: the Mac's diary (`log`), `work_record`, `lifecycle`, `history_week`, `knowledge`, `manual_checks`, `card_changes`, `review_offer`, `worktrees`, `scout_reports` / `scout_report`, `plans` / `plan` (with the `image` read), `conversation`, `done`, `bearings` and `card_sync`. Every one is `log`'s sibling and follows `log`'s rule: a read above the `action` branch, on `_lan_home`'s allowlist and in `_sealed_run`, no lease, no `remote_activity`. Split out of `docs/transport-contract.md` on 3 Oct 2026 so both documents stay under this checkout's whole-file read threshold; the doors, the actions, the access log and the timing stay in `docs/transport-contract.md`, and the short forms are in `docs/context-host.md` (*api_server.py*).

## `event_log.py` — the Mac's diary

**`event_log.py`** — the Mac's diary, for the phone to read *later*.
`/api/state` describes now; this is a bounded, append-only JSONL journal
(`~/.dark-army/event-log.jsonl`, in `paths._PRIVATE_FILES`, ≤ 500
lines, nothing older than 24 h returned or kept) of the moments that
happen while nobody is looking: a row's first live appearance in an
enriched snapshot (`_logged_starts`), a finish with a real `reason`, one
`session_error` per transition into error, a permission ask at both
row-creation sites and its one outcome (staged verdict, reap `why`, or
"the session ended"), and the board verbs — dispatch, agent close, hand
drag into Done (`_after_board_write`, so both drag doors log once), manual
flag, plan attached, and every `dispatch_error` write. **The sentence is
composed in one place**, `event_log.sentence()`, and every surface draws
`text` verbatim; `who` is nickname → title → provider → "an agent", never
an id. **Nothing secret gets in**: hooks copy named keys (`_log_permission`,
`_log_card_event`, `_log_session_event`) and `append` refuses
`FORBIDDEN_KEYS` (`claim`, `port`, `token`, `key`, `secret`) at any depth.
`_log_event` is `_history_write`'s hop with one difference: off the loop
(the bind expiries, the reap, the enrich) it appends **directly** rather
than returning, or every `card_dispatch_failed` would vanish. **The
finish rides `_record_finished`**, the seam every end shares —
`_forget_session`, the roster vanish in `_on_agent_records`, the Codex
settle and refresh, the Grok end, `stop_session`'s non-hook legs and the
terminal close — through `_log_session_end`, which pops `_logged_starts`
and so writes exactly once; logging in `_forget_session` alone left every
Codex, Grok-roster and background row started and never finished. **A
restart is not a start**: `_seed_logged_starts` rebuilds the set from the
diary when it opens (a session counts as started when its newest
start-or-end line is a start), and the enrich loop skips a row whose
`started_at` predates its own logged `session_end`
(`EventLog.last_ts`) — a stale-evicted id returning is the same run.
Loaded whole into a deque on `open()`, so `recent()` costs no I/O. The
deque is trimmed on **every** append, so a read never exceeds the bounds;
the file is rewritten atomically only when an age prune dropped a line or
the on-disk count has reached `MAX_ENTRIES + PRUNE_SLACK` (50) — never
once per append at the cap, which was a full rewrite and fsync per event
while `recent()` waited on the lock. `open()` compacts whatever it
dropped. **One read on
all three doors**: `GET /api/log` on loopback, and the sealed `log` kind
through `_sealed_run` at home and away (`_lan_home` / `_remote_run`) —
a *read* beside `state` and `usage`, so `LAN_ACTIONS` and
`REMOTE_ACTIONS` did not grow; plaintext `GET /api/log` on LAN is 426
like the other three. `since` (float ≥ 0, strictly newer) and `limit`
(1..`MAX_ENTRIES`), 400 otherwise; `available` is stated, never inferred.
Not on `/api/state`, not on SSE. The phone's Fleet tab draws it as
**RECENTLY (n)** under the process table (`RecentlyView.swift`, fold
remembered in `fleet.recently.collapsed`), fetched by `fetchLog` behind a
state 200 every 30 s on whichever leg the poll used and on tab open; the
widget and `backgroundRefresh` never fetch it (`backgroundRun`). Pinned
by `test_event_log.py`, `test_event_log_hooks.py` and
`test_phone_event_log.py`, which imports `PUBLISHED_KEYS`.

## `work_record` is the sixth sealed read

**`work_record` is the sixth sealed read**, `log`'s sibling and `log`'s
rule: `_work_record_report_for(query)` is one body behind loopback
`GET /api/work-record` and the sealed kind on both doors, **above** the
`action` branch, so neither action tuple grew, no lease is checked and no
`remote_activity` is written. `card=<id>` returns the record with
`available` **stated**; `card=<id>&file=<n>` returns one file's diff, and
`n` is an **integer index into the record's own stored list** — the caller
never supplies a path, so there is nothing to sanitise. The record's
`root` is re-checked against `enrollment.enrolled_roots()` at the moment
of the read, never trusted from the stored row, and the resolved path is
component-contained inside it (`workspace._contains`) as belt and braces.
Only the diff *text* is truncated (`MAX_DIFF_BYTES`), stated rather than
inferred. The Mac's sheet gets the whole record free as
`work_record_full` on the `GET /api/board?card=` it already makes; only a
file's diff costs a second request. The phone reads a **404 as "this
Mac's Dark Army is too old"**, never as "no record".

## `lifecycle` is a sealed read

**`lifecycle` is a sealed read**, `outcomes`' sibling and `log`'s rule: `_lifecycle_report_for(query)` is one body behind loopback `GET /api/lifecycle` and the sealed kind on both doors, **above** the `action` branch, so neither action tuple grew, **no lease is checked** and no `remote_activity` is written. The kind is on `_lan_home`'s allowlist **and** in `_sealed_run`; a kind handled only in `_sealed_run` still 404s at home. Query forms are `root` (project) or `card` (retained timeline), mutually exclusive, with the same frozen `[from,to)` UTC period (default last 30 days, at most 366) and `as_of`. Default page 25, allowed 1..100; UTF-8 JSON is capped at 300 KB before sealing. `lifecycle_supported` on `_pipeline_writable()` is the version marker. Full measurement contract: `docs/lifecycle-timing.md`. Pinned by `test_lifecycle_api.py`.

## `history_week` is a sealed read

**`history_week` is a sealed read**, `log`'s sibling and `log`'s rule: the phone's History screen, the Mac's last seven days across every project. `_history_week_for` sits **above** the `action` branch, on `_lan_home`'s allowlist **and** in `_sealed_run`, so neither `LAN_ACTIONS` nor `REMOTE_ACTIONS` grew, **no lease is checked** and no `remote_activity` is written. It takes **no query**: the body is ignored and the week is fixed — the same `agent_efficiency_report(7, "")` the Mac's `/api/history?range=7d` makes with no project. The answer is a **closed projection**, rebuilt row by row from `HISTORY_WEEK_CARD_KEYS` (`card_id`, `sessions`), `HISTORY_WEEK_SESSION_KEYS` (the session id, provider, phase, `bound_at`, `known`, the token cost and its provider split, `token_unpriced_turns`, `reported_cost_usd`) and `HISTORY_WEEK_DAY_KEYS` (the leftover day's provider token costs, reported dollars and token-unpriced counts and ids); the body adds `available`, `range_days`, `from`, `to`, `generated_at`, `partial` (the join was capped) and `codex_history_partial`. The body also adds `limits` — Claude's budget over the week, projected from `limits_report(7)` (Claude-only, one executor hop) through `HISTORY_WEEK_LIMITS_KEYS` and `HISTORY_WEEK_LIMIT_POINT_KEYS`, absent when the store is closed, the read fails or no Claude reading exists; `current` and the burn rate never ride it. No title, person, project root, model or turn count rides it. A null or absent value is **absent**, never 0 (a 0 would price a day at $0.00); a stored 0 stays 0. Nothing is priced here: the phone folds it with `LedgerWeek`, the fold the Mac's History calls (`docs/phone-contract.md`). The page is bound at 300 KB of plaintext before sealing by halving `cards` from the tail, with `truncated: true` stated, since a cut lowers the week's total. Loopback `GET /api/history-week` is token-gated like `/api/knowledge` (`X-Bob-Token`, empty Origin allowed on GET, the Host check above the routing table). On the LAN door plaintext `GET /api/history-week` is 426 in `HOME_UPDATE_REFUSAL`'s words, recorded `plaintext`; `GET /api/history` is not a LAN route and stays 404 `not_found`. `history_week_supported` on `_pipeline_writable()` is the version marker. Pinned by `test_history_week_api.py` and `test_lan_door.py`.

## `knowledge` and `manual_checks` are sealed reads

**`knowledge` is a sealed read**, `log`'s sibling and `log`'s rule: one enrolled project's notes, behind loopback `GET /api/knowledge?root=` (token-gated, `X-Bob-Token`, empty Origin allowed) and the sealed kind on both doors, **above** the `action` branch, so neither action tuple grew, **no lease is checked** and no `remote_activity` is written. The kind is on `_lan_home`'s allowlist **and** in `_sealed_run`; a kind handled only in `_sealed_run` still 404s at home. `root` rides the JSON body, never a query string. Empty or unenrolled is refused in words. Cap 300 KB plaintext before sealing, `break` not skip-and-continue. Person writes `knowledge_confirm` / `knowledge_stale` / `knowledge_edit` are loopback `BOARD_ACTIONS` only and 404 as a sealed `action`. `knowledge_supported` on `_pipeline_writable()` is the version marker. Pinned by `test_knowledge_api.py` and `test_phone_knowledge.py`. **`manual_checks` is a sealed read**, `knowledge`'s shape and `log`'s rule: loopback `GET /api/manual-checks` (token-gated, `X-Bob-Token`, empty Origin allowed) and the sealed kind on both doors, **above** the `action` branch, on `_lan_home`'s allowlist **and** in `_sealed_run` — no lease is checked and no `remote_activity` is written. With `root` / `q` / `status` it is the Checks section's list: every `manual-check/<YYYY-MM-DD>-<slug>/check.md` under the enrolled roots (`manual_check.scan`), **`root` empty meaning every enrolled root** (the person's own list across their projects), a named root refused in words unless enrolled, `q` a casefolded search over title, check, steps, outcome and card (≤ 200 characters), `status` one of `all`, `open`, `passed`, `failed`; open first, then newest first by `Created`, each row joined to its card through `manual_check_path`. A file out of shape is listed `malformed` with its first problem, never dropped. With `path` it is one file's text, `available: false` with the reason outside an enrolled project's folder. On the sealed doors every key rides the JSON body. 300 KB of plaintext before sealing, the tail dropped with `truncated: true`. One executor hop each; nothing rides `/api/state` or SSE. `manual_checks_supported` is the version marker. Pinned by `test_manual_check_api.py`.

## `card_changes` is a sealed read

**`card_changes` is a sealed read**, `manual_checks`' sibling and `log`'s rule: a Done card's branch against the main line — its commits, its files with added and removed counts, and one file's changes — behind loopback `GET /api/card-changes?card=<id>[&file=<n>&tip=<sha>]` (token-gated, either token on `SESSION_READS`, empty Origin allowed on GET, the Host check above the routing table) and the sealed kind on both doors, **above** the `action` branch, on `_lan_home`'s allowlist **and** in `_sealed_run` — so neither action tuple grew, **no lease is checked** and no `remote_activity` is written. Both call `_card_changes_for(params)`; on the sealed doors `card`, `file` and `tip` ride the JSON body, never a query string. `file` is an **integer index** into the listing the daemon recomputes in the same hop, never a path, and `tip` is **required** and must equal the branch's current tip (`CHANGES_MOVED`, 409; missing or malformed, 400 in words), so a diff is never drawn against a listing that has moved. The answer is a closed key set (`merges.CHANGES_KEYS`): `available`, `card_id`, `branch`, `trunk`, `merge_base` (sha8), `branch_tip` (the full hash the clients echo back), `ahead`, `behind`, `commits` (`sha8`, `author`, `at`, `subject`; at most 100), `files` (`path`, `added`, `removed`, `binary`; at most `work_record.MAX_FILES`), `files_total`, `files_truncated`, `commits_truncated`, `merge_offered` and `merge_refusal` (the gate's words minus the busy rungs), `merge_state`, `merge_note`, `review` (`verdict`, `tip` sha8, `current`), `generated_at`, `reason`. A file's changes are `{available, path, text, truncated, reason}`, `text` cut at `MAX_DIFF_BYTES` and the cut stated, the path component-contained in the real root (`workspace._contains`). No board, no card, no branch, an unenrolled root, a non-checkout or a git failure is a 200 `available: false` with the reason, never a 500; the page is bound at 300 KB of plaintext before sealing by dropping `files` from the tail with `files_truncated: true`. Read-only git from the project's root (a few calls, serialised by `_changes_lock`, one executor hop), fetched when a client's CHANGES section opens — never from `/api/state`, SSE, the phone's poll, `backgroundRefresh` or the widget. `card_changes_supported` is the version marker. Pinned by `test_card_changes_api.py`. The sibling sealed `worktrees` read (loopback `GET /api/worktrees`, desk token) is the same kind of read, on both doors and in neither tuple, and lists every card's side folder with a plain status, git on demand only, no absolute path on the page.
**`review_offer` is a sealed read**, `knowledge`'s sibling (`root` in the body); `review_start`, `review_continue` and `review_end` are on both phone tuples, each its own decision: `docs/review-runs.md`.

## `scout_reports` and `scout_report` are sealed reads

**`scout_reports` and `scout_report` are sealed reads**, `log`'s siblings and `log`'s rule: the list of every scout report Dark Army knows about and one report's text. Both sit **above** the `action` branch, on `_lan_home`'s allowlist **and** in `_sealed_run`, so neither `LAN_ACTIONS` nor `REMOTE_ACTIONS` grew, **no lease is checked** and no `remote_activity` is written. On loopback they are `GET /api/scout-reports` (optional `root=`) and `GET /api/scout-report?path=`, token-gated like `/api/knowledge` (`X-Bob-Token`, empty Origin allowed on GET, the Host check above the routing table). On the sealed doors `root` / `path` ride the JSON body, never a query string: a path must not reach the relay's logs.

The list (`host/dark_army_daemon/scout_index.py`, `build`) is derived from two facts alone — the dated folders `scout/<YYYY-MM-DD>-<slug>/report.md` under every enrolled root and the cards' `report_path` (which may name an older prose report) — newest first by the file's time, each row its title, verdict, question, project, day and card, and **never a body**. A `root` present but empty, or not enrolled, is refused in words. The page is bound at 300 KB of plaintext before sealing by dropping from the tail — the **oldest** rows, since the list is newest first — naming the count in `omitted` with `truncated: true`, never skip-and-continue.

The body read accepts a **closed set**, re-checked at the moment of the read (`scout_index.locate`): a card's stored `report_path` inside that card's enrolled root, or `<enrolled root>/scout/<dated folder>/report.md`, both through the attach's own `_plan_path_refusal` realpath rule. Anything else is a 200 `available: false` with "that report is not one Dark Army lists"; a missing `path` or a repeated parameter is a 400 in words. The text is split into the answer block (`scout_report.parse_header`'s dict) and the body with the H1 and the block removed. Both reads are one `run_in_executor` hop each; nothing rides `/api/state`, SSE or the sealed `state` read. `scout_reports_supported` on `_pipeline_writable()` is the version marker. Pinned by `test_scout_reports_api.py`, `test_scout_index.py` and `test_scout_reports_surface.py`.

**The list read also searches the reports' text** (`scout_index.search`): `?q=` on loopback, `"q"` in the JSON body on both sealed doors — never a query string there, a search term must not reach the relay's logs any more than a path. The candidates are exactly `build`'s (one function, `_candidates`, both read), so a hit is always a report the index lists and the body read would open; only the **body** is searched — `_split`'s text with the H1 and the answer block removed, since title, verdict, question and the rest are the index's fields and the clients' instant filter's. Whitespace in `q` is collapsed to single spaces (the clients send it that way, cut to 200); a `q` under `MIN_QUERY_CHARS` (3), before or after folding, or over `MAX_QUERY_CHARS` (200) is a 400 in words. At most `SEARCH_MAX_BODIES` (400) bodies are read, newest first, each through `scout_report.read_text`'s 64 KiB bound — a report over it, or unreadable, is counted in `unsearched` and skipped, **never clipped** — and the search stops at `SEARCH_MAX_HITS` (50). Case and combining marks are folded (`scout_index.fold`). The reply is `build`'s shape with only the matching rows, each carrying `snippet` (one whitespace-collapsed body line of at most 160 characters around the match — a snippet, not a citation), `match_line` (1-based, in the body) and `match: "body"`, never `text` or `body`; the page adds `query`, `searched`, `unsearched`, `search_truncated` (the reading bound bit) and `hits_truncated` (the hit cap did), and the 300 KB page bound still applies. The search runs inside the same single `run_in_executor` hop as the list, **one text search at a time per daemon** (`_scout_search_lock`; the plain list never waits on it); neither action tuple grew and nothing new rides a snapshot. `scout_reports_body_search_supported` on `_pipeline_writable()` is its marker: a phone against an older Mac decodes false and sends no `q`. Pinned by the same three tests.

## `plans` and `plan` are sealed reads

**`plans` and `plan` are sealed reads**, `scout_reports`' siblings and its rule: the list of every written plan Dark Army knows about and one plan's text. Both sit **above** the `action` branch, on `_lan_home`'s allowlist **and** in `_sealed_run`, so neither `LAN_ACTIONS` nor `REMOTE_ACTIONS` grew, **no lease is checked** and no `remote_activity` is written. On loopback they are `GET /api/plans` (optional `root=`) and `GET /api/plan?path=`, token-gated like `/api/scout-reports` (`X-Bob-Token`, empty Origin allowed on GET, the Host check above the routing table). On the sealed doors `root` / `path` ride the JSON body, never a query string: a path must not reach the relay's logs.

**`image` is a sealed read**, `plan`'s sibling and its rule: one picture from a session's project, for the phone's Conversation tab. It sits **above** the `action` branch and below the bot's Read grant, on `_lan_home`'s allowlist and in `_sealed_run`; neither action tuple grew, no lease is checked, no `remote_activity` is written, and there is no loopback route. `session` and `path` ride the JSON body. `image_preview.locate` confines the path to the session's project (the nearest `.git` folder above its working folder, never the home folder, `/` or any folder holding the home folder, judged by on-disk identity so `/users` and `/System/Volumes/Data/Users` count; and a file whose folders up to the root pass through home or anything holding it is refused, so a root that holds home under another name serves nothing), and the session's working folder must lie in Dark Army's own checkout or an enrolled project (`_onboarded` + `_in_projects`, by on-disk identity, walking up from the working folder), the root then narrowed to the nearer of the git root and that project so an enrolled subfolder of a bigger repository opens only itself, and a worktree outside every enrolled folder shows nothing (a corrupt ledger entry is skipped; an unreadable ledger shows nothing and an enrolled entry that is home, holds it or is a whole disk (`os.path.ismount`) is ignored, so Documents, Desktop and the home folder are never readable); it refuses a `..` walk-out, a symlink leaving the project, any hidden path part and any extension outside PNG, JPEG, GIF, SVG, WebP and HEIC; a relative path is tried against the working folder, the root, then as the tail of a file inside the project (bounded, hidden and build folders skipped). `render` never sends a file's own bytes except an animated GIF ImageIO itself identifies that already fits: everything else is decoded and re-encoded (at most 2048 pixels, at most `PREVIEW_MAX_BYTES` = 450 000 so the base64 fits one 900 000-byte frame), so a secret named `.png` is refused as unreadable; a picture stating more than 100 million pixels is never decoded, and an animated GIF travels as itself only while its frames together stay under 40 million pixels (the phone holds the same cap). Reads are served one at a time (`BobDaemon._image_gate`). An SVG that is not plain UTF-8, or naming any external `href`, `src`, `url()`, `@import`, entity or `foreignObject` is refused before AppKit draws it. Refusals are `available: false` with `reason` in words; a missing or oversized `session` / `path` is a 400. `host/tests/test_image_preview.py` pins it.

The list (`host/dark_army_daemon/plan_index.py`, `build`) is derived from two facts alone — the dated files `plans/<YYYY-MM-DD>-<slug>.md` under every enrolled root (the name rule alone leaves the folder's `README.md`, its question list and any directory out; at most 600 names per root, the newest) and the cards' `plan_path` (which may name a plan outside `plans/`; the first card in `CARD_ORDER_SQL` annotates a shared plan's row) — newest first by the **file name's day**, then the file's time, then the path, so a plan iterated today keeps its place; each row its title, `Status:` and `Area:` from the header list, project, name, slug, day and card (`card_id`, `card_title`, `card_column`), and **never a body**. Only a 16 KiB head of each file is read. A `root` present but empty, or not enrolled, is refused in words. The page is bound at 300 KB of plaintext before sealing by `_scout_reports_page_bytes`, dropping from the tail — the **oldest** rows — naming the count in `omitted` with `truncated: true`.

The body read accepts a **closed set**, re-checked at the moment of the read (`plan_index.locate`): a card's stored `plan_path` inside that card's enrolled root, or `<enrolled root>/plans/<dated name>.md` — exactly two components under the root after realpath, so a symlinked file or folder resolving elsewhere is refused — both through the attach's own `_plan_path_refusal` realpath rule; never "any `.md` under an enrolled root". Anything else is a 200 `available: false` with "that plan is not one Dark Army lists"; a missing or empty `path`, one over 4096 characters or a repeated parameter is a 400 in words. The text comes back whole with its first H1 line removed (the client draws the title) through `scout_report.read_text`, bounded at 64 KiB — the same number as `MAX_PLAN_BYTES` — and a plan over it is "that plan could not be read", never clipped. Both reads are one `run_in_executor` hop each; nothing rides `/api/state`, SSE or the sealed `state` read. `plans_supported` on `_pipeline_writable()` is the version marker. Pinned by `test_plans_api.py`, `test_plan_index.py` and `test_phone_plans.py`.

## `conversation`

**`conversation` is the eleventh sealed read**, `log`'s sibling and `log`'s
rule: `_conversation_report_for` sits above `action` — neither tuple grew,
no lease, no record. Query: `session` (required, ≤ 200), `since` (0..10**9,
default 0), `key` (≤ 64 `[A-Za-z0-9:_-]`). A repeated key is 400 in words.
Page bounded at 150 turns / 200 000 bytes, with `more` / `next_seq` /
`reset`. Unknown session is 404 in words; missing journal is 200
`available: false`. No path on the page. A reply typed in Dark Army's
panel or on the phone — the `isMeta` `user` record with `origin.kind`
`channel` whose text opens `<channel source="…" kind="user">`, read by
`session_stats._is_channel_user_message` — is a `user` turn with both tags stripped; `kind="fleet"`, a look-alike without that origin and every
other `isMeta` record are no turn (23 Sep 2026). `conversation_supported` absent
decodes false. Loopback GET is token-gated like `/api/knowledge`. Pinned
by `test_conversation.py`.

**`agent`** (1–64 `[A-Za-z0-9_-]`, else 400) pages that Claude session's own `subagents/agent-<id>.jsonl`, sidechain lines kept; a missing file or a non-Claude session is `available: false` in words. `subagent_conversation_supported` absent decodes false: no helper tabs.

## `done` is the tenth sealed read

**`done` is the tenth sealed read**, `log`'s sibling and `log`'s rule:
`_done_archive_for(payload)` sits **above** the `action` branch, so neither
`LAN_ACTIONS` nor `REMOTE_ACTIONS` grew, **no lease is checked** and no
`remote_activity` is written — expiry bounds what the phone may *do*, never
what it may see. It answers `ApiServer._done_archive()`, the one builder
behind loopback `GET /api/board?column=done` and this kind, so the two doors
can never drift into two answers about the same column. The cards come back
in the **snapshot's own shape** — trimmed, decorated, `thread_count` and
`work_record` included — because a client splices them into `board.cards`,
and a second shape would be a second idea of what a card is. Bounded at
`CARD_SYNC_MAX_BYTES` (300 000, `_outcome_page_bytes`' own figure and its
reason) with an integer `offset` from the payload and `more` / `next_offset`
**stated**. Any `column` other than `done` on the loopback read is a **400 in
words**, `BOARD_RANGES`' precedent; `?range=` and `?card=` are untouched.

**`done=review` on the sealed `state` read is the phone's half of the same
opt-in.** The phone sends `"done": "review"` in the state body
unconditionally, and `_state_answer` builds `self.state(done_review=…)`
**before** `_state_digest(state)`, so the digest a phone quotes always
fingerprints the picture it was actually sent. The board that comes back
still carries every finished card that wants a person — `board.review_only`
drops exactly the cards stamped `done_preview`, which is the record of which
store read produced the row and never a second judgment — and the rest is
this kind's to fetch. `unchanged: true` stays a **present key**; nothing
here turns any part of that answer into an omission.

**Two tokens ride the board, and they are not interchangeable.**
`done_clear_token` is exact membership over the Done ids and is the **only**
thing `clear_done` ever compares — a content-sensitive token there would
refuse a Clear Done because somebody retitled a finished card.
`done_view_token` is what a *held* copy can go stale against: membership,
any revised column, and a review. `BoardStore.done_tokens()` reads both
under one lock, so they can never disagree about which cards exist.

## `bearings` is a sealed read

**`bearings` is a sealed read**, `log`'s sibling and `log`'s rule:
`_bearings_report_for(query)` sits **above** the `action` branch, on
`_lan_home`'s allowlist **and** in `_sealed_run`, so neither
`LAN_ACTIONS` nor `REMOTE_ACTIONS` grew, **no lease is checked** and no
`remote_activity` is written. `since` rides the query string and narrows
Recently landed alone. The body copies no key, digest, claim, port or
channel id. Loopback `GET /api/bearings` and `/api/bearings/text`; LAN
plaintext is 426 in `HOME_UPDATE_REFUSAL`'s words. Composer:
`docs/context-host.md`.

## `card_sync` is the delta card read

**`card_sync` is the delta card read, and it is a kind for `log`'s
reason.** `_card_sync_for(payload)` parses
`ids=<id>,…&plans=<id>:<sha256hex>,…` (`_log_report_for`'s discipline:
400 on a repeated key, an over-long id or more than `CARD_SYNC_MAX_IDS`
(50) of either; a malformed stamp is **dropped, never fatal**) and makes
exactly one executor hop into `_cards_sync_page`, whose per-card body is
**`_card_collect(with_plan=True)` and nothing else** — one reader for a
card and its plan, one containment rule (`_plan_path_refusal`), pinned by
a grep in `test_card_sync_read.py`. A plan whose digest the caller
already holds comes back `unchanged` with **no text**, which is what
makes a re-check of 50 unchanged plans cost 50 stats and zero bytes.
Bounded at `CARD_SYNC_MAX_BYTES` (300 000, `_outcome_page_bytes`' own
figure and its reason), with `more` / `unserved` **stated**. It is
served at home *and* away, checks no lease, writes no `remote_activity`
and appears on **neither** action tuple: expiry bounds what the phone
may *do*, never what it may see. A Mac too old to know the kind 404s it,
and the phone **latches that** (`PhoneClient.cardSyncUnsupported`): an
unfillable cache would otherwise report every card stale on every pass
and ask again for ever. A card carrying a real `revision` lifts it.

