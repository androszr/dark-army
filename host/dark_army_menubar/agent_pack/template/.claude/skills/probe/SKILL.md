---
name: probe
description: Read-only smoke alarm for {{PROJECT}} — a fixed set of shell
  checks against the running system (a deployed URL, a local service, a
  device build) that says in one screen whether it is healthy. Spawns no
  agent. Use when the user says /probe, "is it up", "is the data fresh", or
  before trusting a deploy.
---

<!-- TEMPLATE. Replace every <...> with the real check for this project and
     delete this comment. The shape is the point: all bash, all read-only, one
     banner, one verdict, no agent. A probe that spawns an agent is a review;
     a probe that writes anything is a mutation. Keep it under a minute. -->

# probe

A smoke alarm, not a review. Runs a fixed list of cheap checks against the
live system and prints one verdict. It never changes anything and never
spawns an agent — if a finding needs judgment, that is `/review` or the
profile's audit skill, not this.

## Args

`/probe [target]` — optional: a URL, a host, or a build identifier. Without
it, use the default from `docs/context.md`.

## Banner

Read `.claude/skills/probe/banners/intro.txt` and paste it verbatim as a
fenced code block before the first check. Never generate it from memory; if
the file is missing, show none.

## Checks

Run every one; collect all results before printing. Each check is one command
with one pass condition. Report a check that could not run as **SKIPPED**,
never as passing.

| # | What | Command | Pass |
|---|---|---|---|
| 1 | <it answers> | `curl -sf --max-time 5 <url>/health` | exit 0 |
| 2 | <the data is fresh> | `curl -s <url>/api/<freshness endpoint> \| jq .updated_at` | within <n> minutes |
| 3 | <the last deploy landed> | `gh run list --limit 3` | newest run green |
| 4 | <the scheduled job ran> | `<command>` | <condition> |

## Report

```
PROBE: <target>
| # | Check | Result | Evidence |
|---|---|---|---|

VERDICT: HEALTHY | DEGRADED | DOWN
SKIPPED: <any check not run, and why>
```

`DOWN` on any failure of check 1. `DEGRADED` on any other failure. A report
that omits a skipped check reads as "that was fine" when nobody looked, so
name every skip. No `bob-tldr`, no `bob-actions`: a probe is not waiting on
anybody.
