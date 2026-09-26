# Harness token policy — truncate, compact, cache

The three defaults Dark Army's agent loops follow for keeping their context
small, where each one is already in force, where it is written policy only,
and where it is out of Dark Army's hands. Adopted on 25 Sep 2026 from the
AWS Strands Harness announcement. Dark Army does not run on Strands and does
not depend on it; it borrows three numbers and the reasons behind them.

## Source and claims

The source is the Strands team's announcement,
<https://strandsagents.com/blog/introducing-strands-harness/>, published
21 Sep 2026; Strands Harness is Apache-2.0. Every figure below is the
blog's, reported as the blog states it. Dark Army has not reproduced any of
them.

- Across six benchmarks, about 28% lower cost than Claude Code, Codex and
  others, with equal or better accuracy on the same Claude and GPT models.
- About 77% cheaper than Claude Code with Fable 5, with a higher Terminal
  Bench 2.1 score.
- Deepseek Harness was more token-efficient still, but less accurate.
- The runs were benchmarked through Harbor on EC2.

Two related notes are named here without figures, because neither is in this
repository to cite: Cursor's token-efficiency note (24 Sep 2026) and Google's
AX workload model.

## The three defaults

1. **Truncate or offload tool results over about 1,500 tokens.** A bulky
   tool result is written to a file and the conversation carries the path
   and the answer, not the bytes. Every later turn pays again for whatever
   stays in the window, so a result nobody reads twice is the cheapest thing
   to keep out.
2. **Compact at 85% of the context window.** Compaction is itself a model
   turn that needs room to read what it is rewriting; started at 85% it still
   has that room, where at 90% it has a good deal less.
3. **Prompt-cache the reused front of a request, and recover in the loop on
   overflow.** A request whose fixed head (the brief, the instructions, the
   tool list) comes before its changing data can have that head cached and
   not paid for in full twice; a request that overflows the window is
   compacted or handed on inside the loop rather than failing the run.

The two figures are not the same unit as Dark Army's existing dial: the shunt
guard counts **lines** (350 by default, 1200 in this checkout's
`.claude/settings.json`), the Strands default counts **tokens** (about
1,500). Both are stated here side by side; neither is claimed to equal the
other.

## Where each loop stands

*In force* names the mechanism that already does it. *Policy only* is what
the brief asks of the agent, with nothing enforcing it. *Not ours* means the
request is made by Claude Code, Codex or Grok, not by Dark Army: those clients
make the model calls for an agent's turns, and Claude Code does its own prompt
caching.

The compact line is `CTX_CRIT_PCT` in `host/dark_army_daemon/signals.py`
(85%). At that line `host/dark_army_daemon/autocompact.py` types `/compact`
into a session's terminal where Dark Army can type into it — a VS Code
window with extension 0.1.6+, or a terminal Dark Army hosts — makes one
attempt, and otherwise flags the session to you. Codex is excluded. The long
form is `docs/session-state-contract.md`, *Auto-compact*.

| Loop | Truncate | Compact | Cache |
|---|---|---|---|
| `/ship` planner (`bc-planner`) | In force for whole-file reads: the shunt guard (`.claude/skills/shunt/SKILL.md`) refuses a read over the threshold and a cheap helper answers instead. Policy only for everything else: write bulky output to the run's scratch folder and cite the path. | In force for the top-level session: `autocompact.py` at `CTX_CRIT_PCT`. A subagent's own window is Claude Code's, not ours. | Not ours. |
| Implementer (`bc-implementer`) | As the planner: shunt guard in force; policy only for test logs, which already go to the scratch folder and are cited by path. | As the planner; policy only: at 85%, `/compact` or hand the card on rather than push through. | Not ours. |
| Verifier (`bc-verifier`) | Exempt from the shunt guard by role, so policy only: read the exact section, keep logs in scratch. | Runs as a subagent: Claude Code's own compaction; policy only at 85%. | Not ours. |
| Bug auditor (`bc-bug-auditor`) | As the verifier. | As the verifier. | Not ours. |
| Integration and security reviewers (`bc-integration-reviewer`, `bc-security-reviewer`) | As the verifier. | As the verifier. | Not ours. |
| Mission Control (`host/dark_army_daemon/mission.py`) | Policy only: its brief (`docs/mission-control-brief.md`) reads `/api/state/pretty` in slices, never whole. | In force: it is a terminal Dark Army hosts, so `autocompact.py` reaches it at `CTX_CRIT_PCT`. | Not ours. |
| Card preparer (`host/dark_army_daemon/card_prepare.py`) | Not needed: one-shot, the card fields only. | Not needed: one request, no loop. | Not ours: a fixed brief then the card's fields, so the head is reusable by construction, but the call is `claude -p`, `codex exec` or `grok` and Dark Army sets no cache directive. |
| Priority scorer (`host/dark_army_daemon/card_priority.py`) | Not needed: one-shot. | Not needed. | Not ours: fixed prompt then card data, no cache directive. |
| Title namer (`host/dark_army_daemon/session_title.py`) | Not needed: about 600 characters of data. | Not needed. | Not ours: fixed head then data, `claude -p` on Haiku, no cache directive. |
| Shunt worker (`host/dark_army_menubar/agent_pack/template/.claude/skills/shunt/bulk_read.py`, `code_write.py`) | It *is* the truncation: it returns the answer alone, and `host/dark_army_daemon/work_record.py` counts the lines it kept out. | Not needed: one-shot. | Not ours: `-p` on the worker model, no cache directive. |

## What Dark Army will not adopt from Strands

- The Strands runtime itself as Dark Army's orchestrator, and its Python and
  npm packages and CLI.
- Container deployment of agent loops.
- Session resume by id; a Dark Army card is the handoff, and a fresh session
  starts from the card with the plan and nothing else.
- The helper-subagent-plus-checklist shape; the `/ship` crew is Dark Army's
  own.
- Strands' optional skills; Dark Army has its own skills and agent pack.
- Any paid API dependency.
- A Kubernetes or Google AX migration.

## Owners

- **Cipher** — the policy's wording: this document and the briefs that ask
  agents to follow it.
- **Hex** — the code: the compact line in `signals.py` and every client
  drawing of it, and any future code path, only where a concrete one is
  required.

## Follow-ups

- **Offload bulky tool results to files from inside a run** — a written rule
  and, if a code path is wanted, one scratch-folder convention shared by the
  `/ship` references and the shunt skill for results over about 1,500
  tokens, measured against the shunt ledger before and after (Hex).
- **Ship the harness token policy in the agent pack** — a pointer in the
  pack's template and a copy of this policy for enrolled projects, if their
  crews should follow it too (Cipher).
