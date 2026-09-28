# The shared agent pack

Lifted out of `CLAUDE.md` on 6 Sep 2026, unchanged. Dark Army owns the
`/ship` + `/review` + specialist briefs and keeps enrolled projects in step
after a confirmed press per project.

- `pack_render.render` is pure: a `dict[str, bytes]`, no writes, no
  subprocess.
- `pack_install` is the only module under `host/` that writes into a project
  root; `PACK_DESTINATIONS` is the allowlist, re-checked (enrolled,
  contained, not-self) at the instant of the write.
- Markers splice `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` /
  `docs/context.md` / `.claude/review.md`; `.claude/settings.json` is a key
  merge of owned `permissions.allow` rows.
- **A filled-in `docs/context.md` is the project's** (24 Sep 2026, after a
  resync put the web+iOS template's `pnpm` / `xcodebuild` gate rows over
  vir-sunset's real `npm` ones). The template is a page of blanks that lives
  wholly between the markers, so it is in `pack_render.PROJECT_FILLED_KEYS`:
  `_write_render` rewrites its region only when the file is absent, has no
  markers yet (the region goes above the project's text), equals today's
  render, or is byte-for-byte the region the pack last wrote there —
  `pack_render.managed_digest` (SHA-256 of the region, markers included, CRLF
  normalised), recorded per key in the ledger row's `pack_digests`. Any other
  region was edited by the project and the file is left alone, whatever the
  marker line says. A row an older build wrote has no digests, so an edited
  region is kept and an unedited one stops taking template changes; deleting
  the file re-seeds it, as with `SEED_ONCE_KEYS`. Pinned by
  `test_agent_pack_filled.py`.
- **An adapted `scripts/` or `.github/workflows/` file is the project's**
  (24 Sep 2026, after a resync put the iOS profile's stock
  `check-entitlements.py` and `check-privacy-strings.py` over arpg-web's
  adapted ones and its gate went red on files nobody there had touched).
  Same rule as above on the whole file, since these carry no markers:
  `pack_render.PROJECT_ADAPTED_PREFIXES`, `pack_render.file_digest`
  (SHA-256, CRLF normalised) in `pack_digests`. The pack writes only when the
  file is absent, equals today's render, or is exactly what the pack last
  wrote; a file that differs with no digest on record is kept. Pinned by
  `test_agent_pack_adapted.py`. The shipped entitlements guard also finds its
  Debug twin under either `{{APP}}-Debug.entitlements` or
  `{{APP}}.entitlements`.
- The ledger is `~/.dark-army/agent-pack.json` (`paths.AGENT_PACK_PATH`,
  in `_PRIVATE_FILES`, 0600).
- Three `PANEL_ACTIONS` (`install_agent_pack`, `stop_agent_pack_sync`,
  `resync_agent_packs`) — not on loopback, not on `LAN_ACTIONS`.
- Launch resync is a daemon thread with `SYNC_BUDGET_SECONDS` (20).
- **Every brief carries the model its role runs on.** `render(models=)`
  takes the resolved per-role table for that project
  (`agent_models.resolve(settings, root)`: project override → machine-wide
  `agent_models` → shipped) and writes `model:` into each Claude brief's
  frontmatter (`pin_model`, after `name:`) plus its two shims — `model = "…"`
  in the Codex `.toml`, `model:` in the Grok `.md` — each with its **own**
  provider's value, and no line at all where the value is empty. The
  allowlist is `dispatch.MODELS` plus the preparer's
  `card_prepare.HELPER_MODELS` for the `card-preparer` slot alone. The two
  writers pass the table in (`install_pack(models=)`,
  `resync_all(models_for=)`), so the renderer stays pure; the line lands on
  the next install, the next launch resync and at once on a model press
  (`_set_agent_model` resyncs). Dark Army's own checkout is never rendered;
  `pack_install.pin_own_checkout` rewrites only the model line of its
  `bc-*` briefs and shims in place, and leaves `workers.json` alone.
  The shipped Codex policy is Sol for the main session, implementer, verifier
  and integration reviewer; Astra for planner, bug auditor and security
  reviewer; and Luna for card preparer and shunt worker. Explicit global and
  project choices retain the precedence above, and an explicit card model
  still wins for Start.
- The master tree ships as `dark_army_menubar/agent_pack` through
  `setup.py` `resources`.
- **The generic `/ship` skill is an adapter plus three references** (20 Sep
  2026, the *ship token efficiency* plan):
  `template/.claude/skills/ship/SKILL.md` carries the modes, the spawn
  shapes and the banner table, and loads `references/common.md` in both
  modes, then `references/plan.md` or `references/implement.md` for its
  mode. The three references are ordinary template files: `_read_tree`
  renders them, `mirror_skills` byte-copies them under `.agents/skills/ship/`
  beside the adapter, and `.claude/skills/` / `.agents/skills/` are already
  in `PACK_DESTINATIONS`, so the installer needed no widening. The profile
  overlays (`PREFLIGHT-*.md`, the Reviewers table, the questions) are
  untouched; both local and generic workflows require an independent verifier,
  including command-only criteria. Codex starts each role with `fork_turns="none"`. `test_agent_pack_render.py` and
  `test_ship_context.py` pin that every rendered profile carries all four
  files and that the resolved text still names every stage.

- **The shunt skill ships with the pack** (20 Sep 2026,
  the *shunt delegation layer* plan): five placeholder-free
  files under `template/.claude/skills/shunt/` — `SKILL.md`, `bulk_read.py`,
  `code_write.py`, `exempt.py`, `workers.json` — rendered by `_read_tree`
  and mirrored under `.agents/skills/shunt/` plus `agents/openai.yaml` like
  every other skill. The wrappers land 0644 and run as `python3 <path>`
  (`executable_keys` is unchanged). `workers.json` is pinned like the
  `model:` lines: `pin_worker_models` rewrites it from each provider's
  `worker` cell of the resolved table (an empty cell writes the shipped
  default, `card_prepare.WORKER_MODELS`) **before** `mirror_skills`, so the
  twin is byte-identical; `render(models=None)` leaves the template bytes.
  `settings.json` gains three owned `permissions.allow` rows
  (`Bash(python3 .claude/skills/shunt/{bulk_read,code_write,exempt}.py:*)`),
  replaced on resync by the owned-rows rule. The pack also owns narrow allow
  rows for the test runner, Swift
  builds/tests, npm build, Ruff and the two board MCP names (`mcp__dark-army`,
  `mcp__bob`), and two git write rows, `Bash(git add:*)` and
  `Bash(git commit:*)`: a `/ship` run inside a card worktree commits its
  work to its own card branch at Phase 7 (`docs/card-worktrees.md`), and a
  prompt per commit would stop an unattended run. It never allows `Edit`,
  `Write`, any other git write (push is never allowed and asks each time;
  reset and checkout are denied),
  deletion or `defaultMode`. **What the pack does not
  write:** no `hooks` key — the guard is machine-wide through
  `~/.claude/settings.json`, `~/.grok/hooks/` and `~/.codex/hooks.json`
  like every Dark Army hook, and arms itself per project on the skill file's
  presence — and no `env` key: a project's own `env.BOB_SHUNT_MIN_LINES` is
  a foreign key `merge_settings` keeps. Dark Army's own checkout keeps its
  copies in step through `tools/sync_shunt_skill.py` (`--check`), pinned by
  `test_shunt_skill.py`; the settings window's **Worker** row is drawn only
  where `pack_render.ships_shunt()` says the template carries the skill.

- **The gate helper ships with the pack** (21 Sep 2026, the ship-run audit):
  `template/.claude/skills/ship/gate.sh` is the generic twin of Dark Army's
  own `.claude/skills/ship/gate.sh` — the attempt ledger (`dispatch`, `run`
  with the two-and-three budgets and the same-failure stop), the delta
  (`delta`: the tree minus the Phase 0 baseline, since the pack has no
  delta identity of its own), the baseline replay (`baseline`, driven by
  `SHIP_BASELINE_CMD` with `{id}`, since the pack knows no runner), the
  three red-test classes (`classify`: YOURS / PRE-EXISTING / IN-FLIGHT) and
  the lane (`lane <regex>…`, the Reviewers table's regexes as arguments).
  Failing ids are read from pytest, XCTest, Jest/Vitest output or
  `SHIP_FAIL_REGEX`. It lands 0755 like the other `.sh` files, is mirrored
  under `.agents/skills/ship/`, and `settings.json` gains one owned allow row
  (`Bash(bash .claude/skills/ship/gate.sh:*)`). The generic implementer,
  verifier and `references/implement.md` route every gate, every ledger row
  and the Phase 6.6 lane through it, as the local briefs do.
  `test_agent_pack_gate.py` runs each subcommand against a throwaway repo.

- **Every profile ships the whole crew, profiles are folders, and a profile
  picks its areas** (22 Sep 2026): the template carries generic
  `{{P}}-integration-reviewer` and `{{P}}-security-reviewer` briefs, so a
  rendered project has seven roles (a profile's same-named brief overlays
  the generic one, as web's security reviewer does), and both shipped
  profiles route them from their Reviewers rows. A profile is any folder
  holding a `profile.json`: `web` / `ios` / `both`, any other vendored
  folder, or one of the person's own under `~/.dark-army/profiles/<id>/`
  (`paths.USER_PROFILES_PATH`, read-only to Dark Army). A shipped name
  always wins; a symlinked folder, or a link inside one, is never read.
  `pack_render.normalise_profile` fills every key a short file leaves out
  (no `reviewers` → the two generic checkers on common door and install
  paths), and `settings_allow` rows are JSON-quoted, so one line of JSON is
  a working profile. `available_profiles` lists them for the panel
  (`agent_pack.profiles`, drawn by the Install submenu; an older daemon
  leaves the fixed three), cached until a `profile.json` changes.
  `valid_profile_id` is the AppKit thread's file-free check. A profile's
  `areas` choose which `.claude/leads/*.md` ship (plus `universal`; absent
  means all eight), a profile's own `leads/` overlays them, and the shipped
  briefs name nothing of Dark Army's. A resync removes a lead it no longer
  ships only when its bytes are in `shipped_lead_digests` — the legacy
  digests of every brief an earlier pack wrote, plus today's — so a brief
  somebody wrote or edited survives. Pinned by
  `test_agent_pack_profiles.py`.

- **The pack offers a starter `.gitignore`** (23 Sep 2026,
  the *pack installs starter gitignore* plan). The base list is
  `agent_pack/gitignore.txt`, a sibling of `template/` and never inside it
  (a `template/.gitignore` would be rendered as a pack file and would act on
  this repository's own template tree; `test_agent_pack_contract.py` pins
  both); its last group is Dark Army's key folder, `plans/`,
  `docs/research/`, `/scout/`, `/manual-check/` and the person's private
  `user-data/` (added 24 Sep 2026, the *starter gitignore user-data* plan),
  then `/.worktrees/`, where each started card's own worktree lives
  (28 Sep 2026, `docs/card-worktrees.md`; existing projects receive it on
  their next resync); a
  profile folder may add its own `gitignore.txt` (web: `.next/`, `out/`,
  `.vercel`, `coverage/`; iOS: `xcuserdata/` and friends), read by
  `pack_render.gitignore_lines`, which `_overlay_profile` never sees.
  After the pack's files are written, `pack_install._offer_gitignore`
  appends the lines the project lacks under one `# managed by Dark Army`
  header, extending that block on
  a later pass. **Equivalent lines are one line** (`pack_gitignore.canonical`:
  `/plans/`, `plans`, `plans/`, `**/plans/`), a plain line is skipped where
  the project un-ignored it (`!.env` keeps `.env` out), and **a negation is
  always appended** — `!.env.example` works only after the `.env.*` it
  excepts. **Offered once per line per project**: the canonical forms go
  into the ledger row's `gitignore_offered`, so a line somebody deletes
  stays deleted and only a line never offered (a later release's) arrives
  on a later resync; a row an older build wrote has none and receives the
  whole block on its next launch, which is the rule, not a regression. A
  folder with no `.git` (folder or worktree file) gets no file and an
  unchanged list. A read or write failure leaves the list unchanged, keeps
  the install `ok`, and says so in `last_result` (`ok; could not write
  .gitignore`), which Settings ▸ Projects draws. An existing file's mode is
  kept. Dark Army's own checkout is refused before the merge like every
  other pack write. Pinned by `test_agent_pack_gitignore.py`.

- **Scout reports have a folder, a shape and a checker** (24 Sep 2026,
  the *scout folder structured reports* plan). Scout is its own skill,
  `template/.claude/skills/scout/` — `SKILL.md` (`/scout <brief>`; the ship
  adapter keeps `/ship scout <brief>` as a one-line alias),
  `references/scout.md` and the checker — mirrored under
  `.agents/skills/scout/` plus `agents/openai.yaml` like every other skill.
  The reference writes `scout/<YYYY-MM-DD>-<slug>/report.md` with an answer
  block and five headings, and the scout runs the checker on it:
  `template/.claude/skills/scout/scout_check.py`, a placeholder-free byte
  copy of the daemon's `scout_report` module
  (`host/dark_army_daemon/scout_report.py`; Dark Army's own
  `.claude/skills/scout/scout_check.py` is the third copy, and
  `test_scout_report.py` pins all three equal, stdlib-only and parsing
  under Python 3.9). It lands 0644 and runs as `python3 <path>`
  (`executable_keys` is unchanged), and `settings.json` gains one owned
  allow row (`Bash(python3 .claude/skills/scout/scout_check.py:*)`). The starter
  block gains `/scout/` after `docs/research/`, which stays — anchored,
  so a `scout` folder deeper in the project is never ignored. A changed
  line is a new line, so a project already offered `docs/research/`
  keeps it and receives `/scout/` on its next launch resync, not at once.
  `docs/context.md`'s template names the folder in one table row.
- **A leftover check is a file with a checker** (25 Sep 2026, the *manual
  check folder* plan). The implement reference's Phase 7b and the
  implementer brief write each manual check as
  `manual-check/<YYYY-MM-DD>-<slug>/check.md` (an answer block, `## Steps`,
  `## Why not automated`), check it, flag the card with its path and then
  close the card. The checker ships beside the implement reference:
  `template/.claude/skills/ship/manual_check.py`, a placeholder-free byte
  copy of `host/dark_army_daemon/manual_check.py` (Dark Army's own
  `.claude/skills/ship/manual_check.py` is the third copy;
  `test_manual_check_file.py` pins all three), mirrored under
  `.agents/skills/ship/`, 0644, run as `python3 <path>`, with one owned allow
  row (`Bash(python3 .claude/skills/ship/manual_check.py:*)`). The starter
  block gains `/manual-check/` after `/scout/`, anchored; offer-once, so a
  project already offered `/scout/` receives it on its next launch resync.
  `docs/context.md`'s template names the folder in one table row.

Pinned by `host/tests/test_agent_pack_*.py`.

## Where an agent may search

Added 24 Sep 2026 (the *agent search scope rule* plan). A session hosted in
Dark Army's panel terminal looked for a plan file with `find /` and then
`find ~`; the pty broker is a child of `Dark Army.app`, so macOS charged the
walk to Dark Army and raised Photos, Music and Documents privacy prompts
naming it. The board already held the file's path. The answer is a written
rule, not a refusing hook.

**The rule.** Never run a recursive file walk — `find`, `grep -r` / `-R`,
`rg`, `fd`, `ls -R`, `du`, `tree`, `mdfind` without `-onlyin`, and the like —
rooted at `/`, `~`, or `~/Documents`, `~/Desktop`, `~/Downloads`,
`~/Pictures`, `~/Music`, `~/Movies` or `~/Library`. Search only the current
project, `~/.dark-army`, the session's own scratch folder and the folders in
`~/.dark-army/search-scope.json` (Dark Army's own checkout and every
enrolled project, either direction). Reading a known exact path is always
allowed. A plan or card file is never searched for: a session started from a
card was handed its plan path, a card's `plan_path` is on the board (the
`dark_army_*` board tools, the panel), and otherwise the agent asks.

**The list.** `host/dark_army_daemon/search_scope.py` writes
`paths.SEARCH_SCOPE_PATH` as `{"version": 1, "roots": [...]}` — sorted,
de-duplicated, folder paths and nothing else (never a digest, key, code or
claim), 0600 in `paths._PRIVATE_FILES` beside `enrollment.json`. `compose()`
is pure; `refresh()` rewrites the file only when its bytes differ and never
raises. It runs after every `enrollment.save()` (so enrol and un-enrol move
it at once) and once in `app.main()` after `enroll_self()`, before the hooks
install.

**Four carriers**, because no one of them reaches every assistant:

| Carrier | Reaches | Roots |
|---|---|---|
| `SEARCH_SCOPE_HINT` in `NOTIFY_SCRIPT` (`host/dark_army_menubar/hooks.py`), printed on `session_start` after `WORK_REPORT_HINT` | Claude, in every enrolled project | read from the file at that moment, one per line |
| `## Where to search` in `GROK_RULES_TEXT`, written to `~/.grok/rules/dark-army.md` on launch | Grok, everywhere | a pointer to the file |
| `## Where to search` in the pack's `template/AGENTS.md`, spliced on install and every launch resync | Claude, Codex and Grok in every project the pack keeps in step | a pointer to the file |
| `## Where to search` in this repository's `AGENTS.md` | every assistant in Dark Army's own checkout | a pointer to the file |

The two pointers never carry an inline list: the pack resyncs at launch, not
on enrol, so a list there would go stale. **With no file** (a newer script
before the app has written it, or a malformed one), the hook prints the
fallback words *Dark Army has not written its folder list yet - search only
this project, ~/.dark-army and your scratch folder*; an older build never
reads the file. Pinned by `host/tests/test_search_scope.py` (the file, the
round trip, no secret, the four carriers), `test_notify_script.py` (the
printed hint) and `test_hooks_install.py` (the Grok rules).
