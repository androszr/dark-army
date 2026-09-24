# Ship preflight checklist

Deterministic checks run by `/ship` Phase 4 against the **plan file**, using bash
and grep only. No agent spawn — that is the point: these must be cheap, boring,
and identical every time.

`$PLAN` = the plan path. Classify each finding `BLOCK` or `WARN`.

---

## 1. Required sections — BLOCK

```bash
for s in "## Idea" "## Context" "## Files to change" "## New files" \
         "## Threading & surfaces" "## Steps" "## Risks & footguns" \
         "## Test plan" "## Acceptance criteria" "## Out of scope"; do
  grep -qF "$s" "$PLAN" || echo "BLOCK: missing section $s"
done
```

*Fix:* the planner dropped a template heading. Re-spawn with `mode: iterate`.

## 2. Plain-language sections present — BLOCK

Phase 5 shows the user only the plain-language zone; without these headings there
is nothing readable to show. `## Technical detail` is required too — it is the
divider check 3 bounds on.

```bash
for s in "## What this does" "## How I'll build it" "## How you'll know it worked" \
         "## Technical detail"; do
  grep -qF "$s" "$PLAN" || echo "BLOCK: missing plain-language section $s"
done
```

## 3. Jargon leaking into the plain-language zone — WARN

The region from `## What this does` down to the `## Technical detail` divider must
read clean for a non-programmer: no paths, no code, no commands, no framework
names, no selectors.

```bash
awk '/^## What this does/{f=1} /^## Technical detail/{f=0} f' "$PLAN" \
  | grep -nE 'host/|panel/|\.py([^a-zA-Z]|$)|\.swift|`|grep|pytest|rumps|AppKit|NSStatus|SwiftUI|asyncio|/api/|[0-9]+\.[0-9]+\.[0-9]+' \
  && echo "WARN: plain-language zone leaks technical detail (lines above) — relocate it below ## Technical detail"
```

*Fix:* rewrite the flagged lines as observable behavior ("the strip turns the
count red"), and keep the paths and symbols in the technical zone. Never delete
the detail — relocate it.

## 4. Unverifiable acceptance criteria — WARN

Every bullet under `## Acceptance criteria` should contain a backtick command,
`grep`, `pytest`, `swift`, `exits 0`, `MANUAL:`, or `exists`.

```bash
awk '/^## Acceptance criteria/{f=1;next} /^## /{f=0} f && /^[-*0-9]/' "$PLAN" \
  | grep -vE '`|grep|pytest|swift|npm|exits 0|MANUAL:|exists' \
  && echo "WARN: criteria above are not command-verifiable"
```

*Fix:* "the panel shows the mesh" → "`cd host && .venv/bin/pytest -q tests/test_mesh.py` passes ≥6 cases".

## 5. Reintroducing a deleted layer — BLOCK

The pixel-art window, the SDL2 simulator, the LVGL render engine, the sprite
pipeline, BLE and the ESP32 device were deliberately deleted (~14,000 lines).
Windows support was explored and fully reverted.

```bash
grep -nE 'sys\.platform|platform\.system|[Ww]indows support|on Windows|LVGL|SDL2|sprite pipeline|RGB565|ESP32|[Bb]luetooth|firmware/|(^|[^A-Za-z])BLE([^A-Za-z]|$)' "$PLAN" \
  && echo "BLOCK: this plan reintroduces a deliberately deleted layer — escalate as a decision, do not plan around it"
```

**Case-sensitive, and word-bounded, on purpose.** An earlier version matched
`windows` and `BLE ` case-insensitively, so it BLOCKed on "rate-limit windows",
"VS Code windows" and — the one that made it unusable — the word *possible*.
Never loosen this pattern back into a substring match: a BLOCK here stalls the
pipeline, and Phase 4 hides the raw output, so a false positive is a stall the
user cannot diagnose.

*Note:* a plan that only *mentions* one of these to say it is out of scope should
say so under `## Out of scope`; override consciously and record why.

## 6. Menu-bar or panel plan without a threading section — BLOCK

```bash
if grep -qiE 'menu ?bar|menubar|dropdown|status item|strip|panel|rumps|NSMenu|NSAlert|AppKit' "$PLAN"; then
  grep -qiE 'main thread|AppKit thread|daemon loop|rumps\.Timer|callAfter|run_coroutine_threadsafe' "$PLAN" \
    || echo "BLOCK: UI plans must name the thread each piece runs on and how it crosses (CLAUDE.md: a blocked AppKit thread freezes the status item)"
fi
```

## 7. Hook handler touched without naming the string — BLOCK

`dark-army-notify` is not a file in the repo; it is the `NOTIFY_SCRIPT`
string in `hooks.py`, written out on install, and it must stay stdlib-only.

```bash
if grep -qiE 'hook handler|dark-army-notify|bob-companion-notify|SessionStart|PreToolUse|SubagentStop|UserPromptSubmit|hook event' "$PLAN"; then
  grep -qiE 'NOTIFY_SCRIPT|stdlib' "$PLAN" \
    || echo "BLOCK: a plan touching the hook path must name NOTIFY_SCRIPT in hooks.py and restate the stdlib-only / Python 3.9 constraint"
fi
```

## 8. A destructive verb without its guard — BLOCK

```bash
if grep -qiE 'stop_session|delete_agent|delete_abandoned|retire|kill|terminate|remove the (session|agent|record)' "$PLAN"; then
  grep -qiE 'identity|pid|category|re-check|guard|confirm' "$PLAN" \
    || echo "BLOCK: a destructive verb must name its guard (identity for stop, category for retire) and its confirmation"
fi
```

## 9. Strip addition without a ladder rung — WARN

```bash
if grep -qiE 'menu ?bar (strip|title)|status item title|_render_strip|_compose_strip|add .* to the strip' "$PLAN"; then
  grep -qiE 'STRIP_LADDER|STRIP_BUDGET|collapse|drop(ped|s)? first|280' "$PLAN" \
    || echo "WARN: the strip has a 280pt budget and a collapse ladder — say where this sits and what is given up first"
fi
```

## 10. New runtime dependency or on-disk resource without packaging — BLOCK

py2app freezes a hand-maintained list. A module or asset missing from `setup.py`
imports fine from source and breaks only in `/Applications`.

```bash
if grep -qiE 'new depend|requirements\.txt|pip install|import [a-z_]+ *#? *\(new\)|read (the )?(icon|png|sound|asset|resource)|assets/' "$PLAN"; then
  grep -qiE 'setup\.py|py2app|packages|includes|resources|bundle' "$PLAN" \
    || echo "BLOCK: name the setup.py entry (packages/includes/resources) this needs, or state why it needs none"
fi
```

## 11. Persisted-state change without a compatibility note — WARN

```bash
if grep -qiE 'sessions\.json|preferences\.json|identities\.json|history\.db|session_store|jobs_store|settings\.json' "$PLAN"; then
  grep -qiE 'default|tolerat|backward|older build|migrat|atomic' "$PLAN" \
    || echo "WARN: state on disk is read by older builds and written by newer ones — say how a missing or unknown key is handled, and keep the write atomic"
fi
```

## 12. Cast change out of step — BLOCK

Trigger on the roster itself, not on the word `cast` — which matches
**broadcast**, and the SSE broadcast path is ordinary plan vocabulary here.

```bash
if grep -qE '(^|[^a-z])cast (art|member|roster)|identity\.NAMES|Cast\.names|new character|assets/cast' "$PLAN"; then
  { grep -qi 'identity' "$PLAN" && grep -qi 'Cast.swift\|Cast\.names' "$PLAN"; } \
    || echo "BLOCK: the roster is duplicated in identity.NAMES (Python) and Cast.names (Swift) — both change in the same plan"
  grep -qiE 'pixelgrid_ingest|menubar_cast_icons' "$PLAN" \
    || echo "BLOCK: cast art is generated, never hand-edited — name the tools that regenerate it"
fi
```

## 13. Panel model change without the tolerant decode — WARN

```bash
if grep -qiE 'Models\.swift|/api/state|panel model|Decodable|new field' "$PLAN"; then
  grep -qiE 'tolerant|optional|missing key|default' "$PLAN" \
    || echo "WARN: Swift's synthesized Decodable throws on a missing key even with a default — new fields decode through Models.swift's tolerant helpers or one absent field blanks the panel"
fi
```

## 14. Polling without a reason — WARN

```bash
grep -qiE 'timer|poll|interval|every [0-9]+ ?s' "$PLAN" && \
  ! grep -qiE 'structural|on_agents_change|push|does not fire|no event' "$PLAN" \
  && echo "WARN: this codebase polls only where push cannot work (a session burning budget makes no structural change) — say why a push is not enough"
```
