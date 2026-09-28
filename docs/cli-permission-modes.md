# Command-line assistants and permission asks

Dark Army requires Claude Code. Codex and Grok are optional. The board and
phone publish which of `claude`, `codex` and `grok` Dark Army found on PATH or
at their usual install sites. A missing assistant stays visible with **not
installed on this Mac** and cannot be selected for a new run. Start rechecks
the executable and gives the same refusal if it disappears after a snapshot.

| Assistant | Default asking mode | Ask in Needs you | Answer from Dark Army |
|---|---|---|---|
| Claude Code | Its own configured mode | A trusted `PermissionRequest` hook shows the tool and command | Allow or Deny once through the hook, on panel or phone |
| Codex 0.155.1+ | Its own configured approval preset | A trusted `PermissionRequest` hook shows the tool and command | Pending a recorded concurrent-dialog check; until then the row says to answer in its Codex terminal |
| Grok 1.0.41 | Normal mode | A `permission_prompt` notification plus an open `permission_requested` event shows the ask | A hosted terminal can be answered with the once-only digit, after Dark Army checks the live screen and event log |

Dark Army never grants an always-allow rule, edits a permission policy, or
answers an ask raised inside a helper agent. A helper ask says **Raised inside
a helper agent — answer it in the terminal.** An ask in a terminal Dark Army
did not open says **Answer this in its terminal — Dark Army did not open that
terminal.** A missing or changed Grok dialog is refused with a reason and
left for the person at the terminal.

## Codex's one-time trust step

After a build installs the new `PermissionRequest` group in
`~/.codex/hooks.json`, open any Codex terminal and enter `/hooks`. Trust Dark
Army's **PermissionRequest** command. Codex records the choice as a
`permission_request` entry under `[hooks.state]` in `~/.codex/config.toml`.
Until that group is installed and trusted, Codex does not deliver permission
asks to Dark Army; the ordinary terminal dialog remains the way to answer.

Codex's answer route is deliberately held off until
`tools/permission_hold_livefire.py --tool codex` produces a genuine
`VERDICT: CONCURRENT` or `VERDICT: SERIAL` on a hosted terminal. The route
must be checked again after a Codex CLI version bump, because the dialog's
ordering belongs to Codex. See
`docs/2026-09-23-codex-permission-hold-verification.md` when that record is
available.

## The narrow project allow rows

The shared project pack pre-approves only `.venv/bin/pytest`, `swift build`,
`swift test`, `npm run build`, `ruff check`, and the board MCP servers
`mcp__dark-army` and `mcp__bob`. This checkout adds its `cd … &&` forms and
the inventory check. None of these rows permits editing, committing,
pushing, deletion or a default mode change. Claude Code and Grok read the
project's `.claude/settings.json` rows; Codex keeps its own approval policy.

The same pack also owns `deny` rows that keep Claude Code's file reader and
the obvious `cat` spellings off Dark Army's key files — the enrolment key,
the session-token file and the phone, relay and bot keys — and this checkout
carries the same rows (`docs/agent-pack.md`). They are a second layer, not
the fix: the file key is the session token and opens no desk verb.

## Recording a live ask

The exact Grok option labels and Codex hook payload must be recorded from a
live default-mode dialog before treating fixture-based tests as evidence of
key delivery. The planned live recorder takes `--tool grok` or
`--tool codex`; the Codex hold test is
`tools/permission_hold_livefire.py --tool codex`. Their recorded output
belongs under `host/tests/fixtures/permission_dialogs/` and the Codex
verification page above. A synthetic test can check the parser's refusal
rules, but cannot prove that the installed CLI currently draws those labels.
