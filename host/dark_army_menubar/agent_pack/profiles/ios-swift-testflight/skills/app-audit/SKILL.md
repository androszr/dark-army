---
name: app-audit
description: Standalone audit of everything {{PROJECT}}'s simulator test suite
  cannot see — entitlement drift between Debug and Release, undeclared privacy
  strings, ATS exceptions in a shipping plist, money as Double, unregistered
  background tasks, keychain-group drift — then spawns {{P}}-app-reviewer for
  the judgment calls. Use when the user says /app-audit, "check the
  entitlements", "will this pass review", "will this survive TestFlight", or
  before cutting a build.
---

# app-audit

Two halves: cheap deterministic checks first, then the reviewer agent for what
greps cannot decide. Run the checks before spawning — a drifted entitlement
does not need an LLM to confirm it.

The premise: the tests pass on the simulator in Debug, and not one of them
exports a distribution build, opens a privacy prompt, wakes in the background,
or reads a file written by last month's version. This is the pass for the gap
between "the tests are green" and "the build on the phone works".

## Args

`/app-audit [--archive]` — with `--archive`, actually archive and export a
Release build (several minutes) and read the entitlements back from the
`.ipa`. Without it, the distribution section is static analysis only and is
reported as **partially checked**, not as passing.

## Agent spawn visual convention

Before spawning `{{P}}-app-reviewer` in Phase 2, read
`.claude/skills/app-audit/banners/audit.txt` and paste its **verbatim**
content as a fenced code block in the text response — not via `cat`, because
shell output collapses in the terminal scroll. Never generate a banner from
memory; if the file cannot be read, show none. Re-dispatches re-show the
banner. If the assistant cannot spawn a sub-agent, run the role yourself from
`.claude/agents/{{P}}-app-reviewer.md`.

| Agent | Character | Phase | Banner file |
|---|---|---|---|
| `{{P}}-app-reviewer` | Watch | 2 | `audit.txt` |

## Phase 1: deterministic checks

Run all of these; collect results before spawning anything. No banner here —
Phase 1 spawns nothing. Paths are the ones in `docs/context.md`; the defaults
below are the layout the guard scripts expect.

```bash
# 1. Entitlements: Debug and Release identical except aps-environment (optional:
#    an app with no push carries it in neither file, and that is consistent).
python3 scripts/check-entitlements.py

# 2. Privacy strings: every reachable gate declared in both app plists, none in extensions.
python3 scripts/check-privacy-strings.py

# 3. ATS exception in a shipping plist.
for p in ios/Config/Info.plist ios/Config/Info-*.plist; do
  case "$p" in *-Debug.plist) continue ;; esac
  grep -q 'NSAppTransportSecurity' "$p" && echo "BLOCK: $p carries an ATS exception"
done

# 4. Money as Double (allowlist: the one chart-geometry file named in docs/context.md).
grep -rn 'Double(' ios --include='*.swift' | grep -v '<chart geometry file>'

# 5. Minute without both digits.
grep -rn --include='*.swift' -e '\.minute()' ios

# 6. Keychain group referenced by build setting: no file references the group is
#    consistent; once one does, every file references it exactly once. Exit 0 and
#    no output is a pass; each ::error:: line names a file that is out of step.
python3 scripts/check-keychain-group.py

# 7. Background task identifiers registered.
grep -rhoE 'forTaskWithIdentifier: *"[^"]+"' ios --include='*.swift' | grep -oE '"[^"]+"' | tr -d '"' | sort -u | while read -r id; do
  grep -q "$id" ios/Config/Info.plist || echo "BLOCK: background task $id is not in BGTaskSchedulerPermittedIdentifiers"
done

# 8. Privacy-gated APIs reachable from code (for the reviewer to match against the strings).
grep -rnE 'UIImagePickerController|PHPickerViewController|AVCaptureDevice|CLLocationManager|SFSpeechRecognizer|AVAudioSession|CNContactStore|EKEventStore|CBCentralManager' ios --include='*.swift'
```

### Distribution check (only with `--archive`)

```bash
xcodebuild archive -project ios/<App>.xcodeproj -scheme <App> -configuration Release \
  -destination 'generic/platform=iOS' -archivePath /tmp/audit.xcarchive -quiet
codesign -d --entitlements :- /tmp/audit.xcarchive/Products/Applications/<App>.app 2>/dev/null \
  | plutil -extract aps-environment raw -o - -
```

Expect `production`. Anything else is a `BLOCK`.

## Phase 2: spawn the reviewer

Show the **Watch** banner (`banners/audit.txt`), then spawn
`{{P}}-app-reviewer` with:

```
Review the working tree against your brief.
context_path: <abs path to docs/context.md>
Deterministic results already collected:
<paste Phase 1 output>

Do not re-run those. Focus on what they cannot decide: persisted-state
tolerance, main-actor discipline, background task completion, the extension
boundary, and whether any new code reaches a privacy gate the plists do not
declare.
```

## Phase 3: report

Merge both halves into one table, most severe first, then:

```
VERDICT: BLOCK | WARN | CLEAN
SKIPPED: <any check not run, and why — the archive step, when --archive was not given>
```

Be explicit about what was skipped. A report that silently omits the archive
check reads as "the distribution build is fine" when nobody looked. No
`bob-tldr`, no `bob-actions`: an audit is not waiting on anybody.
