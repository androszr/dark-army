---
name: security-audit-update
description: Check Cloudflare's security-audit-skill repository for commits newer than Dark Army's pinned copy, summarise what changed in plain words, and bring it in only on the person's explicit word. Use when the user says /security-audit-update or asks whether the security audit skill is current.
---

# Security audit update

`/security-audit-update` — is Cloudflare's security-audit skill newer than the
copy Dark Army ships, and if so, what changed. **Dark Army's checkout only.**
The master copy is the agent-pack template inside this checkout; a project
running the same steps would change only its own copy, which the next resync
overwrites with the pack's, so the skill would be wasted and misleading there.
It is not in the template.

The copy lives at
`host/dark_army_menubar/agent_pack/template/.claude/skills/security-audit/`:
`SKILL.md` is Dark Army's adapter, `upstream/` is Cloudflare's files byte for
byte, `upstream-pin.json` records the commit and a SHA-256 for each. Never edit
anything under `upstream/` by hand.

## Steps, in order

1. `python3 tools/security_audit_upstream.py check` (needs the network). It
   prints `up to date (<sha>)` or `upstream HEAD <sha> differs from the pin
   <sha>`. Say which, in plain words. Exit 2 means Cloudflare could not be
   reached: say so and stop. When up to date, stop here.
2. `python3 tools/security_audit_upstream.py fetch <fresh directory under the
   session's scratch>`. It refuses a non-empty directory and makes a shallow
   clone. **Never run anything from inside that directory**: no script, no
   test, no install. Read it as data, by path.
3. `python3 tools/security_audit_upstream.py diff <that directory>`. It lists
   the commits since the pin (or says the pin is not within the last 100
   commits), every added, removed and changed file with its two digests, and a
   category line: companions added or removed, workflow files changed
   (`SKILL.md`, `RECONNAISSANCE.md`, `HUNTING.md`, `VALIDATION-AND-REPORTING.md`),
   validators or schema changed, licence changed.
4. Read the changed files and write the summary for the person in plain
   language: what Cloudflare changed and why it matters for an audit. Then
   re-read the adapter's *What this overlay assumes about upstream* list (in
   the template's `SKILL.md`) against the new text and name every assumption
   that moved.
5. **Stop and ask.** The next step runs only after the person's explicit yes
   in words. A new commit is not consent.
6. `python3 tools/security_audit_upstream.py apply <that directory>`.
   **`apply` deliberately triggers a permission prompt**: no allow row covers
   it, and that prompt is the harness-enforced form of the person's yes. Never
   add a row for `apply` or `pin --write`. It refuses when nothing differs,
   when the directory is not a clone of Cloudflare's repository (its
   `remote.origin.url`), when the fresh tree has no
   `skills/security-audit/SKILL.md`, when a file is a symlink or not a regular
   file, when a new file carries a double-brace placeholder, `/Users/` or
   `/home/` (the pack's render and contract tests would fail), or when
   `LICENSE` is missing; a real hit is a decision for the person, never a byte
   to patch. Otherwise it replaces `upstream/`, drops removed files, rewrites
   the pin and runs `tools/sync_security_audit_skill.py`.
7. Run exactly this command from the repository root, then the pytest files
   `apply` names, and report the results honestly:
   `node --test host/dark_army_menubar/agent_pack/template/.claude/skills/security-audit/upstream/validate-findings.test.cjs host/dark_army_menubar/agent_pack/template/.claude/skills/security-audit/upstream/validate-coverage-ledger.test.cjs`

## Never

Never commit, push, tag, install or release. Tell the person that projects
receive the update on their next launch resync and Dark Army's app on its next
build.
