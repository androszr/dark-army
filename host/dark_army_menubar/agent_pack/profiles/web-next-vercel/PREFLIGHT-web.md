# Ship preflight — web profile (Next.js on Vercel)

Domain checks run by `preflight.sh` after the structural checklist. `$PLAN` is
the plan path; every finding is one `BLOCK:` or `WARN:` line.

## W1. Framework jargon in the plain-language zone — WARN

```bash
awk '/^## What this does/{f=1} /^## Technical detail/{f=0} f' "$PLAN" \
  | grep -nE 'src/|\.tsx?([^a-zA-Z]|$)|pnpm|npm|React|Next\.js|Drizzle|Vercel|Zod|TanStack|Tailwind' \
  && echo "WARN: plain-language zone names a framework or path (lines above) — relocate below ## Technical detail"
true
```

## W2. Raw hex colors in the plan — WARN

```bash
grep -nE '#[0-9a-fA-F]{3,8}\b' "$PLAN" && echo "WARN: use design tokens, not hex"
true
```

## W3. Security-relevant plan without a reviewer step — BLOCK

Word-bounded on purpose: a bare `auth` matched *author* and a bare `header`
matched *page header*, and a false BLOCK here is a stall the user cannot
diagnose. Never loosen this back into a substring match.

```bash
if grep -qiE '\bauth\b|authenticat|authoriz|allowlist|sign-in|login|proxy\.ts|middleware|/api/|next\.config|process\.env|env schema|dependenc|cookie|security header|response header|http header|headers\(|\bcsp\b|content-security' "$PLAN"; then
  grep -q 'security-reviewer' "$PLAN" \
    || echo "BLOCK: security-relevant plan must name the security reviewer in its Stages line and in a step"
fi
true
```

## W4. Float money math — BLOCK

```bash
grep -nE 'parseFloat|Number\(|parseInt' "$PLAN" \
  && echo "BLOCK: money and quantity math goes through the decimal helper only — never parseFloat/Number()"
true
```

*Note:* `Number()` on a genuinely non-monetary value (an array index, a count)
is fine — override this one consciously after two rounds and say so in the plan.

## W5. Client-side vendor fetch — BLOCK

```bash
if grep -qE "'use client'|\"use client\"" "$PLAN"; then
  grep -qiE 'fetch.*(vendor|provider|api\.[a-z]+\.[a-z]+)|vendor.*client component|client.*calls.*(vendor|provider)' "$PLAN" \
    && echo "BLOCK: third-party data is server-mediated only — route the call through the server contract"
fi
true
```

## W6. Money columns without `numeric` — WARN

```bash
grep -niE 'add (a )?column|new column|schema' "$PLAN" | grep -iE 'price|amount|qty|quantity|fee|rate|cost' \
  | grep -vi numeric \
  && echo "WARN: money/quantity columns must be numeric, never float/real/double"
true
```

## W7. UI plan missing a breakpoint — BLOCK

```bash
if grep -qiE 'screen|component|button|row|nav|layout|page' "$PLAN"; then
  grep -qiE 'desktop|mobile|breakpoint' "$PLAN" \
    || echo "BLOCK: UI plans must state BOTH mobile and desktop behaviour"
fi
true
```

## W8. Polling without gating — WARN

```bash
if grep -qiE 'poll|refetchInterval|interval' "$PLAN"; then
  grep -qiE 'visibilit|hidden|background|hours' "$PLAN" \
    || echo "WARN: polling must be gated on tab visibility (and business hours where they apply)"
fi
true
```

## W9. Destructive migration in one step — BLOCK

Migrations run *before* the new code deploys, so a one-shot drop breaks the
still-live old version.

```bash
if grep -qiE 'drop (column|table)|rename column|not null' "$PLAN"; then
  grep -qiE 'two deploys|additive|backfill|stop writing' "$PLAN" \
    || echo "BLOCK: destructive migration must be split across two deploys (stop writing → deploy → drop)"
fi
true
```
