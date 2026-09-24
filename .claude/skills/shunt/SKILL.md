---
name: shunt
description: Keep big files and boilerplate out of the expensive model. Send a question plus large files to a cheap helper and get the answer alone; have the helper write a new boilerplate file straight to disk. A read of a file over the threshold is refused by a hook and pointed here.
---

# shunt — delegate big reads and boilerplate writes to a cheap helper

Dark Army installs a guard that runs before every file read and every shell
read on Claude, Codex and Grok. A whole-file read of anything longer than the
**threshold** (350 lines unless the project says otherwise) is refused, and
the refusal names this skill. The two commands below are the answer: the
cheap helper on the same assistant reads the files or writes the boilerplate,
and only the short result enters your context. Neither command needs Dark
Army running; both work in any project that carries this folder.

## When to bulk-read

Send a read to the helper when you are **surveying**, answering *where is X
handled*, or reading **generated or fixture files** — anything where the
answer is a few lines and the input is a lot of them.

```bash
python3 .claude/skills/shunt/bulk_read.py --question 'Where is the retry limit decided, and what is its default?' src/scheduler.py src/config.py
```

The helper is told to use no tools; it reads exactly the files you list and
prints the answer, nothing else. Pass several files in one call rather than
one call per file: a delegation costs 10–30 seconds of round trip, so **never
send a file under the threshold** — read that one yourself.

Either command gives up on the helper after 110 seconds — inside Claude
Code's default 120-second Bash timeout, so the wrapper always cleans up
after itself rather than being killed mid-write.

## When to code-write

Have the helper write a **new** test file or module that must match a named
exemplar — the same shape, imports and style as a file that already exists:

```bash
python3 .claude/skills/shunt/code_write.py --spec 'A test module for src/limits.py: one test per public function, the fixture pattern of the reference.' --reference tests/test_scheduler.py --out tests/test_limits.py
```

`--reference` is required (a write with nothing to copy the style from is a
guess), and the target must not exist: **an edit is never delegated**. The
helper prints the file, the wrapper writes it atomically and prints only
`wrote <path> (N lines)`; the content never enters your context. Read the
result with `sed -n` if a section needs checking.

## When never

- **An edit.** Read the exact section yourself with `sed -n '120,160p' <file>`
  or `Read` with `limit` — the guard allows `sed -n`, `grep`, `rg`, `awk`,
  `wc`, a `head`/`tail` with a count at or under the threshold (`tail -n +2`
  and `head -n -5` are whole-file reads; `head -c N` and `tail -c N` are
  judged by the lines inside those bytes), anything piped through another command, a command
  whose stdout goes to a file (`cat big.py > copy.txt`) and a heredoc body.
  A binary file (a NUL byte or invalid UTF-8 in its first chunk) and a
  `Read` that names `pages` are never over: a PNG or a PDF has no lines.
- **Debugging**, anything **security-relevant**, and any **review**: a
  reviewer reads by itself, never through a summary.
- A file under the threshold: the round trip costs more than the read.

## The threshold and its overrides

350 lines. A project may say otherwise with `env.BOB_SHUNT_MIN_LINES` in its
`.claude/settings.json` (read by the guard on every assistant, not only
Claude), and a person may set `BOB_SHUNT_MIN_LINES` in the environment for
one session. A project with many 360-line files may want 600; the threshold
is the dial and the latency is the reason to turn it.

## The worker model

`workers.json` beside this file names the cheap model per assistant. Dark
Army writes it from the **Worker** row of its Agent models setting on every
pack install and launch resync; `BOB_SHUNT_WORKER_MODEL` in the environment
overrides it for one call. Dark Army's own checkout is never installed
into, so there the file is the shipped table and `BOB_SHUNT_WORKER_MODEL` is
the only way to choose another model.

## Reviewers are exempt

A verifier, bug auditor, integration reviewer or security reviewer is never
refused a read: the guard recognises the role by name where the assistant
says who is asking, and the ship workflow opens a per-session exemption
window before it spawns a reviewer:

```bash
python3 .claude/skills/shunt/exempt.py on      # before a reviewer spawn
python3 .claude/skills/shunt/exempt.py off     # before any implementer re-dispatch
python3 .claude/skills/shunt/exempt.py status
```

A reviewer refused a read anyway runs `exempt.py on` itself and reads the
file whole. The marker is keyed by session id, so a sub-agent that shares
its parent's id shares the window.

## Instruction files are exempt

The text an agent follows is never refused, whoever reads it and however
long it grows: any markdown file under `.claude/skills/`, `.agents/skills/`,
`.claude/agents/`, `.claude/leads/` or `plans/`, and the root `AGENTS.md`,
`CLAUDE.md`, `GEMINI.md` and `docs/context.md`. A helper's summary of a
runbook drops the very steps the run needs, so read these whole — never
through `bulk_read.py`.

## What is recorded

Each delegation appends one line to Dark Army's ledger for the session
(`~/.dark-army/shunt/<session id>.jsonl`): when, which assistant and
model, which mode, the file paths, how many lines stayed out of the main
model, what the helper cost where the assistant reports it (Claude only —
Codex and Grok report nothing, and nothing is invented), and whether it
worked. No file content, question or spec is ever written. The card's
*What changed* record and `tools/ship_efficiency.py shunt` read the ledger.
