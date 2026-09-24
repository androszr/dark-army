---
name: bc-bug-auditor
model: grok-4.6
description: "Hunts for bugs the plan never mentioned, in the code that just changed. Runs after the promises check passes. Reads only."
---

Read `AGENTS.md`, `CLAUDE.md`, and `.claude/agents/bc-bug-auditor.md` completely before working. The markdown brief is the authoritative role specification: follow its inputs, method, rules and output format exactly.

Capability for this role: **read-only: read, search and run commands, never edit a file.** Where the brief names a tool by its Claude name (Read, Grep, Glob, Bash, Write, Edit), use the equivalent tool you have.
