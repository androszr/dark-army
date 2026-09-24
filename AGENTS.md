# AGENTS.md

Read this file and [CLAUDE.md](CLAUDE.md) completely once per context.
`CLAUDE.md` owns the invariants and architecture map; the scope rules below
select the remaining documents.

## What this is

Dark Army is a **macOS-only menu-bar monitor** for Claude Code sessions:
hooks → stdlib handler → Python daemon → local HTTP + SSE → menu bar and
SwiftUI panel. Its per-project Kanban board can launch sessions; `dispatch.py`
holds the launch guards. An opt-in paired iPhone and relay read the same state.
No Bluetooth or embedded device.

`host/` is Python, `panel/` and `ios/` are Swift, `relay/` is JavaScript,
`vscode-extension/` is TypeScript, and `tools/` holds asset pipelines.
`CLAUDE.md` has the architecture map and the subject-document index.

## Setup

```bash
cd panel && swift build -c release
cd ../host && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

Python **3.11+**. The system 3.9 cannot install `pyobjc-core`.

## Codex setup

An explicit request to work directly or avoid skills overrides the default
planning route. A spawned specialist follows its assigned brief and returns
to its coordinator; it does not start `/ship`, file cards or spawn the crew.

Codex skills live in `.agents/skills`; ship shares `.claude/skills/ship/references/`.
Roles live in `.codex`; `config.toml` is per-machine, from `config.example.toml`.

## What to read

- **The root, whole.** Read `CLAUDE.md` and this file completely once per context.
- **Subject documents, by scope.** `docs/agent-context.json` maps a role, a
  plan's `Surfaces:` header and the changed paths to the subject documents
  (`docs/context-board.md`, `docs/context-panel.md`, `docs/context-host.md`,
  `docs/context-development.md`) and, through them, to the long-form contracts.
  Read the **union** of what the surfaces and the paths select, plus the
  cross-cutting additions the map names. A selected document is read whole.
- **Uncertainty never selects less.** A path no pattern maps, a surface the
  map does not know, an empty scope, two instructions that conflict, or a
  boundary discovered mid-work each select the fallback: every subject
  document. Say so in the report when that happens; widening is never a
  failure, narrowing is.
- **Reviewers derive their own set.** A verifier, auditor or reviewer works
  out what to read from the plan and the delta by itself, in a fresh context,
  and is never handed the implementer's selection, report or claimed results.
  Read access inside a read-only role is never restricted.
- **Every read is bounded**: a truncated output is an incomplete read.

## Tests

```bash
cd host && .venv/bin/pytest -q
cd panel && swift test
```

The suite is expected green. If a test fails and you did not cause it, report
it rather than working around it; inside a `/ship` run `gate.sh classify`
says whose a red test is (`.claude/skills/ship/references/implement.md`).

## Conventions

- **macOS only.** Windows support was explored and fully reverted (`71a011f`). Do not
  reintroduce `sys.platform` branching without an explicit decision.
- Cast art is generated, not hand-edited — `tools/pixelgrid_ingest.py` then
  `tools/menubar_cast_icons.py`. Read the first tool's docstring before touching
  the conversion; the source art is not on an integer pixel grid.
- Keep the cast in step across `identity.NAMES` (Python) and `Cast.names` (Swift).
- What you found goes where the next person will look: the plan's report,
  a knowledge note (`dark_army_knowledge_write`) or the subject document. A
  follow-up becomes a Prep card on the board. There is no to-do file.
- **A request for a change goes through the planning route first.** Write the plan
  to `plans/<date>-<slug>.md`, file it as a board card, and stop — implementation is
  a separate run started from that card. The exceptions are an explicit "build it
  now", a plan that fails preflight, and anything that changes no code.

## Where to search

Never walk (`find`, `grep -r`, `rg`, `fd`, `ls -R`, `du`, `tree`, bare
`mdfind`) from `/`, `~` or its Documents, Desktop, Downloads, Pictures,
Music, Movies or Library: macOS charges it to Dark Army. Search this
checkout, `~/.dark-army`, your scratch and `~/.dark-army/search-scope.json`'s
folders; exact paths are fine. Plan and card files come from the board
(`plan_path`).

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **dark-army** (38309 symbols, 191326 relationships, 584 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/dark-army/context` | Codebase overview, check index freshness |
| `gitnexus://repo/dark-army/clusters` | All functional areas |
| `gitnexus://repo/dark-army/processes` | All execution flows |
| `gitnexus://repo/dark-army/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
