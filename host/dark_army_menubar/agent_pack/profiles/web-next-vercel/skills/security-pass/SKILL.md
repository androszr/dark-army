---
name: security-pass
description: Standalone security audit of the working tree (or the whole app
  before a milestone ships) for {{PROJECT}}. Runs deterministic checks —
  dependency audit, secret scanning, client-bundle leakage, security headers
  against a live URL — then spawns {{P}}-security-reviewer for the judgment
  calls. Use when the user says /security-pass, "audit security", "check the
  auth", or before shipping a milestone.
---

# security-pass

Two halves: cheap deterministic checks first, then the reviewer agent for what
greps cannot decide. Run the greps before spawning — a `BLOCK` from `pnpm audit`
does not need an LLM to confirm it.

## Args

`/security-pass [url]` — optional deployed URL for the header check. Without it,
the header section is skipped and reported as skipped (not as passing).

## Agent spawn visual convention

Before spawning `{{P}}-security-reviewer` in Phase 2, read
`.claude/skills/security-pass/banners/security.txt` and paste its full content
as a fenced code block in your text response — **not** via `cat`, because shell
output collapses in the terminal scroll. Re-dispatches re-show the banner. If
the assistant cannot spawn a sub-agent, run the role yourself from
`.claude/agents/{{P}}-security-reviewer.md`.

| Agent | Character | Phase | Banner file |
|---|---|---|---|
| `{{P}}-security-reviewer` | Watch | 2 | `security.txt` |

## Phase 1: deterministic checks

Run all of these; collect results before spawning anything. No banner here —
Phase 1 spawns nothing. The vendor hosts and the auth-gate symbol come from
`docs/context.md`.

```bash
# 1. Dependency vulnerabilities — high/critical is a BLOCK.
pnpm audit --prod

# 2. Any module reading env without the server-only guard.
grep -rl 'process\.env' src/ | xargs -r grep -L 'server-only'

# 3. Secrets that would ship to the browser.
grep -rnE 'NEXT_PUBLIC_[A-Z_]*(SECRET|KEY|TOKEN|PASSWORD)' src/ .env.example

# 4. Committed secrets in the working tree.
git diff HEAD | grep -iE '^\+.*(SECRET|DATABASE_URL|API_KEY|TOKEN)\s*=\s*["'"'"'][^"'"'"']+'

# 5. Injection surfaces.
grep -rn 'dangerouslySetInnerHTML' src/
grep -rnE 'sql`[^`]*\$\{' src/           # interpolated raw SQL

# 6. The auth gates still present (symbol from docs/context.md).
grep -c '<auth gate symbol>' src/lib/auth.ts    # expect the count docs/context.md states

# 7. Queries not scoped by user.
grep -rn 'db\.select\|db\.query' src/ | grep -v 'userId'

# 8. Client components touching vendor hosts (hosts from docs/context.md).
grep -rlE 'use client' src/ | xargs -r grep -lniE '<vendor hosts>'
```

### Header check (only when a URL was given)

```bash
curl -sSI "$URL" | grep -iE 'strict-transport-security|content-security-policy|x-content-type-options|x-frame-options|referrer-policy|permissions-policy'
```

Every one of those six must be present. Also confirm the CSP contains no
`'unsafe-eval'` and no wildcard origin.

### Client-bundle leak check

Grep the built chunks for the **values**, not the variable names:

```bash
pnpm build >/dev/null 2>&1
for v in $(grep -oE '^[A-Z_]+(SECRET|KEY|TOKEN)=' .env.local 2>/dev/null | tr -d '='); do
  val=$(grep -E "^$v=" .env.local | cut -d= -f2- | tr -d '"')
  [ -n "$val" ] && grep -rlF "$val" .next/static/ 2>/dev/null
done
grep -rl 'postgresql://' .next/static/ 2>/dev/null
echo "clean"
```

Anything printed before `clean` is a `BLOCK`.

> Do **not** grep for a secret's *name* — auth libraries ship lazy
> `process.env` accessor objects into the client chunk, so the name always
> appears and always resolves to `undefined` in the browser. Matching on the
> name produces a permanent false positive that trains you to ignore this check.

## Phase 2: spawn the reviewer

Show the **Watch** banner (`banners/security.txt`), then spawn
`{{P}}-security-reviewer` with:

```
Review the working tree against your brief.
context_path: <abs path to docs/context.md>
Deterministic results already collected:
<paste Phase 1 output>

Do not re-run those. Focus on what they cannot decide: auth flow logic,
allowlist bypass paths, cookie configuration, and whether any new code widens
the threat surface described in your brief.
```

## Phase 3: report

Merge both halves into one table, most severe first, then:

```
VERDICT: BLOCK | WARN | CLEAN
SKIPPED: <any check not run, and why>
```

Be explicit about what was skipped. A report that silently omits the header
check reads as "headers are fine" when nobody looked. No `bob-tldr`, no
`bob-actions`: an audit is not waiting on anybody.
