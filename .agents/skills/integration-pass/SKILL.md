---
name: integration-pass
description: Audit a Dark Army change across the frozen py2app bundle, installed Claude hook, loopback API and token, persisted state, panel launch contract, dependencies, and destructive actions. Use when the user requests an integration audit or when a change reaches behavior not covered by hermetic tests. Read-only unless the user separately asks for fixes.
---

# Integration pass

Audit the current delta without modifying it. Read `AGENTS.md`, `CLAUDE.md`, `.claude/skills/integration-pass/SKILL.md`, and `.claude/agents/bc-integration-reviewer.md` completely; the Claude files remain the detailed, product-neutral checklist and threat model.

## Procedure

1. Capture `git status --short`, tracked diff, and untracked files. If a ship baseline is supplied, audit only the delta beyond that baseline.
2. Run every deterministic check from `.claude/skills/integration-pass/SKILL.md` that applies. Never invoke a real destructive API action from a probe.
3. If `--build` was requested, build and inspect the actual bundle as specified by the source checklist. Otherwise state that installed-bundle behavior remains unverified.
4. Spawn the project custom agent `bc-integration-reviewer` for the judgment pass. Keep it read-only and pass the baseline plus deterministic results.
5. Merge duplicate findings and classify them as `BLOCK`, `WARN`, or `NOTE` using the source checklist's definitions.

Return the deterministic results, reviewer findings with file references, final verdict, and exact manual probes still required. Do not fix findings unless the user asks for implementation.
