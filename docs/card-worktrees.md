# Card worktrees — every started card works on its own branch in its own folder

The long-form contract behind *A started card works in its own worktree*
(`docs/context-board.md`, under `dispatch.py`). Plan:
`plans/2026-09-28-card-worktree-isolation.md`.

## What happens at Start

On a project that is a git checkout (`<root>/.git` exists — a folder, or a
file when the project is itself a linked worktree) with isolation on, Start
(and START HERE, the drop into In progress, batch start, START PROJECT and
the queue's drain) no longer opens the terminal in the project folder. It
opens it in the card's own worktree:

| What | Name | Decided by |
|---|---|---|
| branch | `card/<id8>-<slug>` — the first eight `[a-z0-9]` of the card id, then the title as lowercase `[a-z0-9]` runs joined by `-`, clamped to 40, `work` when empty | `worktrees.branch_name` |
| folder | `<root>/.worktrees/card-<id8>/` | `worktrees.worktree_dir` |
| setup log | `<root>/.worktrees/card-<id8>.setup.log` | `worktrees.setup_log_path` |

A batch works on its **head** card's branch in its head card's folder; each
member is given the same pair as the session is bound to it
(`_advance_batch_locked`).

Refine (planning), Mission Control, consults and ad-hoc terminals stay in the
main checkout.

**Property 4 stays literal.** `dispatch.guard` still tests the card's `root`
against `_known_project_roots()`; the worktree is *derived* from the root at
the moment of spawn and never becomes a root. The session that appears in it
still binds: its `project` label comes from the enrolled root that contains
the cwd (`enrollment.enrolled_label`), and `_candidate_matches` accepts a cwd
under `root + os.sep`. The hook handler's key walk has no `.git` boundary, so
a session in a worktree finds `<root>/.dark-army/key`; `NOTIFY_SCRIPT` is
unchanged.

## Prepare, then dispatch

The fetch, `git worktree add` and the setup script can take minutes, and the
panel's board POST times out in five seconds. So `_dispatch_card_locked`,
after every gate has passed and the executable is resolved:

1. **Reuses** the worktree recorded on the head card when it is still under
   `.worktrees/`, a directory, and named by `git worktree list --porcelain` —
   a reset or a second Start keeps the folder and the branch.
2. Otherwise, if the head is already being prepared, refuses in
   `dispatch.WORKTREE_PREPARING_REFUSAL`'s words. That constant is
   **transient**: a queued replay holds rather than being dequeued.
3. Otherwise starts `_prepare_worktree_then_dispatch` and answers the press
   at once with `worktrees.PREPARING_NOTE`. **Nothing is written to the
   card**: a preparing card is not `dispatching`, because `CardSections` is
   byte-pinned to the phone and knows four link states.

While a card is preparing, `_launch_inflight` lists it as a pseudo-entry
keyed on its own id, so another card of the same project waits in
`PROJECT_BUSY_REFUSAL`'s transient words (and queues). The card itself draws
`worktree_note = PREPARING_NOTE`. `_worktree_preparing` is replaced, never
mutated, on the loop; the task's `finally` always pops it — that is the only
sweep, and `SETUP_TIMEOUT_SECONDS` (900 s) is the only bound, because the
120 s bind window has not started yet.

The task, on the loop:

- `git remote get-url origin`; where there is a remote, `git fetch --quiet
  origin` bounded by `FETCH_TIMEOUT_SECONDS` (20 s). A failed fetch is one log
  line, never a refusal.
- Before anything is made: `<root>/.worktrees` must not be a symbolic link
  (`WORKTREES_SYMLINK_REFUSAL`), and the setup script, if there is one, must
  pass its gate (below).
- The base is `origin/HEAD` (`symbolic-ref`) — unless local `main` already
  contains it (`git merge-base --is-ancestor origin/HEAD main`), in which
  case `main`, so the person's unpushed work is on the base. No remote:
  local `main`, else `HEAD`.
- A folder git still has registered but that is gone from disk (removed by
  hand) counts as absent: `git worktree prune` runs first, then the add —
  a registered path is reused only when it is also a directory.
- `git worktree add --no-track -b <branch> <folder> <base>` (the card's
  branch never follows `origin/main`), or `git worktree add <folder>
  <branch>` when the branch already exists, bounded by
  `WORKTREE_ADD_TIMEOUT_SECONDS` (60 s). A refusal writes
  `ADD_FAILED_REFUSAL` on the card's `dispatch_error` and stops.
- On the executor: `/.worktrees/` appended once to the repository's own
  `info/exclude` (`git rev-parse --git-path info/exclude`, correct when
  `.git` is a file); the trust copies (below); the setup script (below).
- `record_worktree(head, folder, branch)`, pop the preparing entry, and
  **re-enter the verb the person pressed** — `dispatch_card` with the press's
  own `allow_unplanned`, `queued_replay` and `own_terminal`, or `start_cards`
  with the batch — so every gate runs again at that instant
  (`_auto_start_after_refine`'s precedent). A refusal there lands on the
  card's `dispatch_error` unless the card was queued.
- **A failed preparation dequeues a queued card** in the same write as its
  words (`_fail_prepare`, `_queue_hard_refusal`'s rule), so the drain never
  re-prepares it and re-runs the setup script on every pass.
- **A card deleted while its folder was prepared**: `record_worktree` finds
  no card, so the fresh folder is removed (never `--force`) and its trust
  copies taken back.

At the spawn, `dispatch.spawn` / `spawn_local` receive `cwd=<worktree>`
(`cwd` is passed only when it differs from the root, so a start in the main
checkout is byte-identical to before). `vscode_reveal.spawn_agent` still
addresses the one window that owns the root; an owning window on an
extension older than `SUBFOLDER_SPAWN_MIN_VERSION` (0.1.22) answers
`WORKTREE_WINDOW_REFUSAL` — **a refusal, never a quiet start in the main
checkout**, which would switch isolation off without anybody choosing it.
A queued replay refused in those words is dequeued with them, never
retried each pass. The extension's `spawnAgent` accepts a cwd equal to or
contained in a workspace folder — both sides `path.resolve`d and realpath'd,
component-aware — and refuses a cwd that does not exist.

The work-record baseline is taken in the worktree, so `card_runs.root` and
the baseline are the worktree's and the record says what changed on the
card's own branch; `_consider_work_record` reads `worktree_path` before
`root`.

## The setup script

`<root>/.dark-army/worktree-setup.sh`, optional, the person's own file (Dark
Army never creates one; `.dark-army/` is 0700). A Start — a phone's
included — can reach it, so it runs only when **all** of these hold, checked
before any folder is made (`_setup_script_refusal`):

- the project and its `.dark-army` folder are on an **APFS** disk —
  `statfs(2)`'s `f_fstypename`, read through `ctypes`
  (`daemon_board.setup_volume_type`, `worktrees.setup_volume_allows`); any
  other kind, or one that cannot be read, refuses. On Mac OS Extended
  (HFS+) a name carrying an invisible Unicode character (U+200C, U+202A,
  U+FEFF, …) is stored as the name without it, so a tracked
  `.dar\u200ck-army/worktree-setup.sh` lands in the person's own folder, and a
  leading one escapes the `'.*'` listing below: the index checks cannot
  see it, so the script does not run there at all;
- the project already carries the person's trust decision **for the
  assistant being started** — Claude Code's `hasTrustDialogAccepted`, or
  Codex's `trust_level = "trusted"` (`trust_marks.root_trusted`, the same
  reading the trust copies make); any other assistant has no record Dark
  Army can read, so it never qualifies;
- the folder `<root>/.dark-army` is a real directory — `lstat`, so a link is
  refused (a repository can commit `.dark-army` as a link to a tracked
  folder) — owned by the person Dark Army runs as (`os.getuid()`), with
  no group or other write bit (`worktrees.setup_folder_safe`), and not
  itself a repository: no `.git` file or folder inside it;
- the script is a regular file by `lstat` (a symlink is refused, never
  followed), owned by that same person and with no group or other write bit
  (`worktrees.setup_script_safe`);
- `os.path.realpath(script)` is exactly `os.path.join(os.path.realpath(root),
  ".dark-army/worktree-setup.sh")` (`worktrees.setup_script_where`) — no link
  anywhere on the way;
- git says **positively** that the script is untracked or ignored:
  `git status --porcelain -z --ignored=matching --untracked-files=all --
  .dark-army/worktree-setup.sh`, through `_run_git`, must print exactly one
  of `?? .dark-army/worktree-setup.sh\0`, `!! .dark-army/worktree-setup.sh\0`
  or `!! .dark-army/\0` (`worktrees.setup_status_allows`). The last is git's
  answer when the whole folder is ignored — an enrolled project ignores
  `.dark-army/` — and it is safe only because two things hold together:
  nothing under the folder is tracked by this repository (git gives it only
  then), and `.dark-army` is not itself a repository — the file rules refuse
  any `.git` entry inside it (`os.path.lexists`), since git never looks
  inside a nested repository (a stale submodule's leftover gitlink, a clone)
  and would print the same line. Everything else is refused: empty output (a tracked script, a
  script committed under a case-variant spelling such as
  `.DARK-ARMY/Worktree-Setup.sh` on the Mac's case-insensitive disk, or a
  `.dark-army` that is a submodule — git prints nothing for all three), any
  other or additional entry, and **any call that does not succeed** — an
  error or a timeout never reads as "untracked". The gate fails closed. A
  tracked script is whatever the last pull made it;
- **and the index names no other spelling of the same file.** Spelling is
  not identity on this disk: git's `core.ignorecase` folds ASCII only, so
  `.darK-army/worktree-setup.sh` with U+212A KELVIN SIGN, committed
  upstream, lands in the person's `.dark-army/` (APFS folds it) while git's
  status still answers `!! .dark-army/` — and with `core.ignorecase` off a
  plain ASCII variant does the same. So the file is identified **by inode**:
  `git ls-files -z -- '.*'` (every index entry starting with an ASCII `.`,
  the only names that can fold to `.dark-army`), through the same bounded
  runner with its own cap (`worktrees.MAX_INDEX_DOTFILES_BYTES`, 16 MB —
  a Yarn cache alone passes the shared 256 KB one), failing closed on any
  error or timeout, and over that cap saying so in its own words
  (`SETUP_TOO_MANY_HIDDEN_REFUSAL`);
  every entry whose first path component NFKC-normalises and casefolds to
  `.dark-army`, or whose whole path folds to the script's own
  (`worktrees.index_script_candidates`), is `lstat`'d, and one whose
  `(st_dev, st_ino)` is the script's refuses (`worktrees.tracked_as_script`).

The file rules (`_setup_script_file_check`) and the whole git half — the
status answer and the inode comparison (`_setup_script_git_refusal`) — are
taken at the Start, before any folder is made, **and again immediately
before the script runs** (inside `_run_worktree_setup`, on the executor), so
a pull in the gap is judged afresh. The trust rule is taken at the Start.

Otherwise the Start is **refused in words on the card** —
`SETUP_VOLUME_REFUSAL`, `SETUP_UNTRUSTED_REFUSAL`, `SETUP_UNSAFE_REFUSAL`
or `SETUP_TOO_MANY_HIDDEN_REFUSAL` — and never silently
skipped: a worktree without the setup the person asked for is a run that
fails in confusing ways. A script that passes runs once per new worktree,
before the agent starts, as `/bin/bash <script>` with:

- cwd: the new worktree;
- environment: `work_record.git_env` (the frozen bundle's variables removed,
  git unable to prompt) plus `DARK_ARMY_ROOT`, `DARK_ARMY_WORKTREE`,
  `DARK_ARMY_CARD_ID`, `DARK_ARMY_BRANCH`;
- its own process session, so a timeout kills the whole group;
- stdout and stderr to the setup log, opened `O_NOFOLLOW` at 0600 at both
  write sites (a link planted at its name is refused), cut to its last
  `MAX_SETUP_LOG_BYTES`.

It runs **as the person**, with their permissions. A non-zero exit or the
900 s timeout writes `SETUP_FAILED_REFUSAL` (the exit code or "timed out",
and the log's path) on the card, which stays in Backlog; the worktree is
kept, not recorded, and the next press finds it in `git worktree list`,
skips the add and runs the script again.

## The trust copies

`trust_marks.py`, executor only, never inventing a decision:

- **Codex** (`~/.codex/config.toml`): only when the root's `[projects."…"]`
  table says `trust_level = "trusted"` and no table for the worktree exists,
  append `[projects."<worktree>"]` / `trust_level = "trusted" # managed by
  Dark Army`.
- **Claude Code** (`~/.claude.json`): only when the root's entry has
  `hasTrustDialogAccepted: true` and the worktree's entry lacks it, set it,
  keeping every other key.

`unmark` at release removes the Codex table carrying the marker and a Claude
entry whose only key is ours. `_set_codex_title_writer`'s rules apply: a
symlink or an unparseable file is left alone, writes are atomic with the
mode kept, any `OSError` is one log line. Both paths derive from
`paths._home()`, so no test can write the person's own configs.
`~/.claude.json` is Claude Code's own file and it rewrites it whole; the
worst race is the trust screen appearing once.

## The pack copies

A card's folder is made from the base commit, so a pack update the launch
resync has written into the project's main checkout and nobody has committed
yet (new `permissions.allow` rows, a new reference, a `.gitignore` line) would
be missing there, and the agent would run under the old rules. `_sync_pack_copies`
(executor only, `daemon_board.py`) carries it in, from `_finish_worktree`
(after the exclude line, before the trust copies and the setup script) and on
every reuse of a recorded folder.

- **The gate.** Only a project with a row in the pack ledger
  (`pack_ledger.entry(root)`). Dark Army's own checkout can never have one
  (`_refuse_self`), and there a dirty `.claude/agents/*.md` or `CLAUDE.md` is
  the person's work in progress that cards edit on purpose.
- **What is compared.** `git status --porcelain -z --no-renames
  --untracked-files=all` in the main checkout over `pack_install.pack_pathspecs()`,
  decided by `worktrees.pack_copy_plan`: a regular file in main is copied, an
  absent one is deleted in the folder, a link or a non-pack path is dropped.
  The daemon spells `pack_install.is_pack_path` and `pack_pathspecs`, never
  the destinations constant. The main checkout is only ever read; every write
  (`add -N`, `update-index`) runs in the worktree on its own index.
- **How a copy is kept off the branch.** Written to a fresh temp name in the
  destination's folder (`O_EXCL | O_NOFOLLOW`) and `os.replace`d over the
  name after a `realpath` containment check, so a symbolic or hard link at
  the name is replaced, never written through (a branch that commits
  `.claude` as a link is never written through either). Then marked `git
  update-index --skip-worktree`; a new file is made `git add -N` first; a
  deletion is the unlink plus the mark. `git status` is clean and `git add
  -A` + commit leaves them out. Every pathspec goes literally
  (`--literal-pathspecs`). git aborts a whole `update-index --skip-worktree`
  over one path it cannot mark (verified), so a failed `add -N` drops only
  the new copies, and a failed mark puts every carried file back to its HEAD
  bytes and HEAD mode (new copies removed; a link at HEAD is left alone):
  nothing is left committable. The manifest is written the same way (temp
  file, mode 0600, `os.replace`); stale `.dark-army-pack-*.tmp` files a killed
  write left are swept from each destination folder on the next carry; every
  source and digest read opens `O_NOFOLLOW | O_NONBLOCK` and `fstat`s the
  handle for a regular file. The manifest and the carry are keyed by the
  **folder's** name (`card-<id8>`), so a batch's second card starting in its
  head's folder finds the head's manifest.
- **The manifest.** `<root>/.worktrees/card-<id8>.pack.json`
  (`worktrees.manifest_text`): path, kind (`tracked`, `new`, `deleted`) and
  the sha256 written, never content.
  A folder created new first unlinks any manifest of that name, so one left by
  a folder removed by hand never reads fresh HEAD bytes as edits.
- **Refresh.** A second Start rewrites each carried file whose bytes still
  match the manifest and leaves an edited one alone. A path the manifest does
  not name is first checked with one `git status` in the folder: one that
  already differs from the folder's HEAD is the previous run's own edit and is
  not carried; so is a skip-worktree path the manifest does not name (git
  status never lists those), which covers a lost manifest. The manifest is read through `is_pack_path` and never trusted
  to name a path outside the pack.
- **Only the pack's own writes?** Not enforced: `pack_digests` in the ledger
  holds only the managed region of the project-filled files, not whole-file
  bytes (and none for merged files such as `.claude/settings.json`), so it
  cannot tell the pack's write from the person's own uncommitted edit under a
  pack path. Every uncommitted change under a pack path travels.
- **Release.** Before `git worktree remove`, `_unskip_edited_pack_copies`
  un-marks every file whose bytes no longer match the manifest, so git refuses
  and the folder is kept with `KEPT_NOTE`: nothing an agent wrote is thrown
  away. When it cannot tell (the folder holds skip-worktree entries but the
  manifest is missing or malformed, or git fails, or the un-mark fails) the
  folder is kept as well, like the crew-output keep; a folder with no marked
  entries releases as before. An untouched copy stays marked (un-marking an unchanged intent-to-add
  entry shows ` A` and blocks the remove). The manifest is deleted after a
  successful remove.
- **Failure.** One log line, never a refusal; the setup script and the spawn go
  ahead.
- **A footgun for the push leg.** `git merge main` or `rebase` inside the
  folder is refused while a carried path is marked and main's commit touches
  it. The follow-up push / pull-request leg must first run `git update-index
  --no-skip-worktree` over the manifest's paths. The reverse (main, its copy
  still dirty, merging the card branch) succeeds, because the branch never
  touched the path. An agent that edits a carried file sees `git add` stage
  nothing (git prints the sparse-checkout advice); the release keeps the folder
  so the person finds the diff.
- **Verified** on Apple Git-155 (git 2.50.1), 28 Sep 2026: skip-worktree keeps
  status clean and out of `add -A`; a per-worktree `info/exclude` is not read
  and `extensions.worktreeConfig` would override the person's global ignore
  file, so neither is used; `update-index` takes several paths after `--`.

## Release at Done

`_maybe_release_worktree` removes a card's folder only when (carried pack
copies are un-marked first when edited: *The pack copies*):

- the card is in Done (or being deleted), and
- its `link_state` is neither `live` nor `dispatching` — **never from under a
  live shell**, which is why the agent's own `dark_army_close_card`, made
  with its shell still inside, releases nothing, and
- no live session in the agents snapshot has its cwd in the folder or below
  it, and Dark Army holds no spawn receipt for the card — `link_state`
  alone misses a bind window that ran out, or a live card dragged back to
  Backlog, with its terminal still open in the folder (`_session_inside`);
  such a release waits and is tried again on a later pass, and
- no other card sharing the folder (same `worktree_path`, or the same
  `batch_id`) is still open or running, and
- its work record is not still being collected (the record reads the very
  folder).

**The remove re-checks at the moment it fires**
(`_release_still_safe`, immediately before `git worktree remove`): the card
is re-read — a deleted card must still be gone; any other must still be in
Done, still name this folder, with its link neither live nor dispatching —
and it must not be preparing, hold no spawn receipt and have no live
session inside the folder. A card dragged out of Done and started again
while the release was on its way keeps its folder.

**It is never awaited on a hot path.** Every seam queues the card, and the
queue runs as **one detached task** with one in flight
(`_kick_worktree_releases`, `_work_record_task`'s pattern), which looks at
its queue again before it ends so a card queued meanwhile is not left for
the next push (a release it deferred waits for a later pass): the agents-push
path, a Done write, a delete and Clear Done each return without waiting on
a `git worktree remove` (up to 60 s). Five seams queue it: the reconcile's
`mark_ended` seam for a Done card (`_consider_worktree_release`, on the
executor; the task is started from `_flush_work_records`); the reconcile's once-per-process look
at any Done card still naming a folder with its link not live
(`_consider_standing_worktree` — after a restart, or with the folder removed
by hand, which simply clears the pair); a Done arrival through
`_after_board_write` whose link is already not live; `delete_card` after the
terminal close; and Clear Done, for every cleared card. A deleted or
cleared card with no folder recorded (its setup script failed and no second
press came) releases the folder its id names under `.worktrees/`, when that
is a directory — unless the card is still being prepared: then the prepare
task, finding the card gone, discards the folder itself (whether the
preparation succeeded or failed), never `--force`. A deleted card's
release reads a close that landed as the shell gone; if it is deferred (a
session still inside, or a close that did not land) the card is remembered
whole (`_worktree_orphans`) and retried by the live-session test alone.

`git worktree remove <folder>` runs **without `--force`**, ever. Git refuses a
tree with modified tracked files or untracked files (ignored ones — `.venv`,
`node_modules` — do not count), and that refusal is the design: the folder is
kept and the card draws `KEPT_NOTE.format(path)`. **The note is derived,
never remembered** (`_worktree_note`): a Done card, link not live, folder
still a directory, no live session inside it, no release queued or running,
and no other card sharing the folder still open or running
(`_worktree_shared`, the release's own sharing test, handed the whole
board — the Done archive included) — a folder kept for a working batch
sibling is not "unsaved changes".
The crew's own output is the exception to "ignored ones do not count":
`scout/`, `plans/`, `manual-check/` and `docs/research/` are git-ignored, so
git would delete a report, plan or check without refusing. Before the remove
the release asks git about those four folders (`worktrees.argv_crew_output`)
and keeps the folder, with the same note, when anything is there or git
cannot say. A check file flagged from inside a card folder is copied to the
main checkout's `manual-check/` at the flag (`_manual_check_side_folder`),
and that copy is the one the card and the Checks list hold.
So it survives a restart, is absent during a new run after a reset, and
goes when the folder is removed by hand. The person removes a kept folder
by hand. On
success every card sharing the folder has its pair cleared
(`clear_worktree`) and the trust copies are taken back. **The branch is never
deleted**: it is there to merge.

## Stale registrations

Git keeps a registration for every linked worktree under the common git
dir's `worktrees/`. A folder deleted without `git worktree remove` — a
`/ship` baseline tree whose scratch folder went with its session, a card
folder removed by hand — leaves the registration behind, and
`git worktree list --porcelain` marks it `prunable <reason>`.
`git worktree prune` (no `--expire`) drops exactly those and deletes no file
on disk. Measured on Apple Git 2.50.1, two edge cases:

- A **locked** entry prints `locked`, never `prunable`, and is kept even
  when its folder is gone.
- A **present** folder whose own `.git` file was deleted prints
  `prunable gitdir file points to non-existent location`, and git prunes it
  all the same. "Prune only forgets gone folders" is therefore not git's
  guarantee, so the guard is literal: `worktrees.prune_decision` over
  `parse_worktree_entries` returns `(missing, blocked)`, and a repository
  with any `blocked` entry (prunable, path still a directory) is skipped
  whole — git has no per-path prune — with one log line
  (`worktrees.PRUNE_BLOCKED_LINE`) naming the folder.

**Dark Army's sweep.** The executor half of `_reconcile_board` ends with
`_consider_worktree_prunes`: once per process per root of
`enrollment.enrolled_roots()`, a root with a `.git` (folder or a linked
worktree's file) is queued — no git call there. The release task
(`_kick_worktree_releases` → `_flush_worktree_releases`) drains the queue
after its releases, through `_prune_stale_worktrees`: `worktree list`, the
decision, then `worktree prune`, every call through `_run_git` (the 8 s
bound). A root with a card in `_worktree_preparing` waits for a later task,
so a prune never sits beside a `git worktree add`. It is silent unless
something goes wrong: a blocked root or a failed git call is one `info`
line. A registration that goes stale during a run waits for the next launch.

**The ship tooling.** `gate.sh` (the repo's and the pack's generic twin) has
`prune_stale_worktrees` with the same literal guard, called by `snapshot`,
by `baseline_prepare` before its `worktree add` and by `baseline --remove`
after the remove. `baseline --remove` names `$SCRATCH/baseline` to
`git worktree remove --force` (its own tree, patched dirty by design) only
when `git worktree list --porcelain` registers that path and the folder
exists; a registration whose folder is gone goes through the prune.
`close-out.sh` runs it whenever `SCRATCH` is set, not only while the folder
exists. Neither half ever removes a folder, and the daemon's "no `--force`,
anywhere" rule in `worktrees.py` is untouched.

## The switch

On by default for every git project. `board_isolation_by_root` in
`preferences.json` stores only the projects that switched it **off**
(`{canonical root: false}`); switching it on removes the row. The panel's
`ISOLATE ON` / `ISOLATE OFF` chip on the In progress heading (drawn while the
project picker names one git project) sends `set_board_isolation_root
{root, enabled}` on the stdin channel → `_set_board_isolation_root` (store,
save, `set_board_isolation_override`, republish); the app's startup feed
calls `set_board_isolation_overrides`. The daemon's map is replaced, never
mutated (`board_parallel_overrides`' rule). Each card publishes `isolation`
(`"on"` / `"off"`, `""` for a non-git project), the board publishes
`isolation_overrides`. A non-git project starts exactly as before.

## The store

Schema v30 adds `worktree_path` and `worktree_branch` (`TEXT NOT NULL DEFAULT
''`, CREATE path and `_ADDED_COLUMNS`), written by `record_worktree` and
emptied by `clear_worktree` alone (`SINGLE_WRITER`). Neither is in
`_WRITABLE`, `ApiServer._BOARD_FIELDS` or `REVISED_COLUMNS`: daemon
bookkeeping, `batch_id`'s ring, and a surface that could write the path could
aim the release at a folder Dark Army never made. The release also refuses a
path not under `<root>/.worktrees/` (`worktrees.inside`).

A card's `root` is never a card folder: `worktrees.checkout_root`,
`_repair_worktree_roots`.

## The git argv

Two pure modules name the `git` executable — `work_record.py` and
`worktrees.py`, the second built on the first's `_git` head, so
`--no-optional-locks` and `core.quotePath=false` ride every call — and every
call runs through `BobDaemon._run_git` (`subprocess.run` on the executor,
bounded, `git_env()`). `_run_git` takes a wider `timeout` for the add and the
fetch only.

## The excludes

`.gitignore` carries `.worktrees/` in this repository; every other project
gets `/.worktrees/` in its `info/exclude` at the first preparation and in the
agent pack's starter `.gitignore` (`agent_pack/gitignore.txt`, offered once).
`ruff.toml` excludes `.worktrees`; `.vscode/settings.json` excludes it from
the watcher and from search. The repository-walking tests read `git
ls-files` or named subtrees, so none reaches a root-level `.worktrees/`.
GitNexus needs no exclude of its own: `analyze --index-only` run with a
throwaway `.worktrees/x/` file (28 Sep 2026) indexed no symbol from it — the
analyzer honours `.gitignore`.

## What a downgrade sees

An older build reads past the two columns (`SELECT *` into a dict) and never
removes a worktree: the folders and branches stay until the newer build
returns. `preferences.json` gains one dict key an older build ignores.

## Committing on the card branch, and what is not yet done

- **The crew commits on the card branch** (the person's decision of 28 Sep
  2026, made project-wide): a `/ship` session whose `HEAD` is a `card/` or
  `batch/` branch inside `.worktrees/` commits its work there at Phase 7,
  before `close-out.sh` — never on `main`, never a push, merge, tag,
  install or release (the ship references, Dark Army's and the pack's;
  `CLAUDE.md`'s invariant). Both `settings.json` files allow `git add` and
  `git commit` for it. A folder with anything left uncommitted is still
  kept at release (`KEPT_NOTE`).
- **Follow-up legs**: pushing the branch and opening a pull request (with
  merge detection and branch deletion), a warning when two in-flight cards
  plan to change one file, and the phone's copy of the switch.
