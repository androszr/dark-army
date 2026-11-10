---
name: security-audit
description: Security audit of a codebase by Cloudflare's method, with Dark Army's rules.
  Reconnaissance, a coverage-ledger hunt, adversarial validation, structured
  findings and a report. Use when the user says /security-audit, asks for a
  security audit or pen test, a focused security review, or asks a security
  question about this project.
---

# Security audit

`/security-audit [scope]` — Cloudflare's open-source security-audit method,
unedited, run with Dark Army's house rules. Guidance by default; the six-phase
full audit only on an explicit request for one.

> Dark Army owns this file and rewrites it on every resync; an edit here is
> lost. The same goes for everything under `upstream/` and `upstream-pin.json`.

## 1. Read upstream first

**This file is the adapter; the method is `upstream/SKILL.md` beside it.**
Read it completely, then the companions it names, and follow it with the
substitutions below. Never edit anything under `upstream/`: `upstream-pin.json`
records a SHA-256 for every file, and a resync rewrites the folder from Dark
Army's copy. Where upstream says "the skill directory", that is
`.claude/skills/security-audit/upstream/` in this project.

## 2. Roles

- Upstream's `parent` is this session.
- Upstream's `Task tool` is Claude Code's Agent tool, Codex's spawned agent
  and Grok's task tool.
- The `research` agent is the harness's read-only explorer subagent: Claude
  Code's `Explore`, a Codex `read-only` role, Grok's read-only capability.
- The `general` agent is `general-purpose` on Claude Code and a
  `workspace-write` role on Codex.
- **Never** use a `*-security-reviewer`, `*-verifier`, `*-bug-auditor` or any
  other ship brief for a hunter: each carries a `/ship` phase's instructions
  and would import them into the hunt.

## 3. Output

A full audit writes to `<project>/security-audit/run-<N>/`. Dark Army's ignore
block keeps that folder out of commits, which is what makes it a folder the
person has selected inside the target. Before writing, the parent runs
`git check-ignore -q security-audit/`. (The trailing slash matters: the
ignore line is directory-only, so the bare name does not match a folder that
does not exist yet.) When that fails (the line was removed or
the folder has no `.git`), fall back to upstream's rule: ask for an external
path, default `~/security-audit-skill/<repo-name>/run-<N>`. Hunters search only
the target, the run folder and the session's scratch (`AGENTS.md`, *Where to
search*).

## 4. Sandbox

Dark Army provides no OS-enforced sandbox. Unless the person names one that
meets upstream's list, target code is never executed and leads stay
`needs_validation`, exactly as upstream says.

## 5. Validators and Node

Phase 4 runs `node .claude/skills/security-audit/upstream/validate-findings.cjs <findings.json>`
and `node .claude/skills/security-audit/upstream/validate-coverage-ledger.cjs <ledger>`
(the two allowed rows). Before Phase 1 of a full audit run `command -v node`.
When it is absent, say so at the start, still write `findings.json` and the
ledger, and end in upstream's terminal state (b): `run_status: "incomplete"`,
`incomplete_reason: "validators_unavailable_no_node"`, with the report's first
section saying the validators did not run. Never claim a clean finish.
Guidance mode needs no Node.

## 6. The board

After Phase 6, offer each `confirmed` record as a card through
`dark_army_add_card`, as `/review` does: never unasked, at most 8, the
`summary` written for someone who will never open these instructions.
`needs_validation` records are listed in the closing message and carded only
on request. Never use `/api/action` `board_create`. If the tool is absent, say
so in one sentence and stop.

## 7. Relation to the ship review

The project's `*-security-reviewer` brief is `/ship` Phase 6.9's review of one
change against the project's written door contract. This skill is the
standalone audit of a tree or a scope, run on request, never inside `/ship`.

## 8. Reading

The prose files are under the read guard's default threshold.
`report-schema.json` and the `.cjs` files are over it: run them, read a range
with `sed -n`, or `python3 .claude/skills/shunt/exempt.py on` for a section of
the schema.

## 9. What this overlay assumes about upstream

The update check in Dark Army's own checkout re-reads this list against any
newer text:

- the output-directory rule in `upstream/SKILL.md`, *Full audit setup*;
- the two validator file names and their `Usage:` lines;
- the four role words `parent`, `Task tool`, `research` and `general`;
- the two terminal states (both validators pass, or `run_status: "incomplete"`
  with an exact reason);
- `findings.json` and `coverage-ledger.json` as the shared file names.

## 10. Provenance

Cloudflare's `security-audit-skill`,
https://github.com/cloudflare/security-audit-skill, MIT, licence in
`upstream/LICENSE`, pinned by `upstream-pin.json` to one commit. The pack's
`PORTED-FROM.md` says this folder is not from the pack's own origin.
