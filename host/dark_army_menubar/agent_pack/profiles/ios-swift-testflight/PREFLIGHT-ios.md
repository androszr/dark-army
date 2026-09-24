# Ship preflight — iOS profile (Swift, TestFlight)

Domain checks run by `preflight.sh` after the structural checklist. `$PLAN` is
the plan path; every finding is one `BLOCK:` or `WARN:` line.

## I1. Framework jargon in the plain-language zone — WARN

```bash
awk '/^## What this does/{f=1} /^## Technical detail/{f=0} f' "$PLAN" \
  | grep -nE 'ios/|\.swift([^a-zA-Z]|$)|SwiftUI|UIKit|xcodebuild|Xcode|Codable|Decodable|@Observable|BGTask|plist|entitlement' \
  && echo "WARN: plain-language zone names a framework or path (lines above) — relocate below ## Technical detail"
true
```

## I2. Double near money — BLOCK

```bash
grep -nE 'Double\(' "$PLAN" | grep -iE 'price|amount|qty|quantity|fee|rate|cost|total|money' \
  && echo "BLOCK: money and quantity go through the money module to Decimal — never Double()"
true
```

## I3. Permission surface without a usage string — BLOCK

```bash
if grep -qiE 'camera|microphone|dictation|speech|photo library|photos picker|location|healthkit|calendar|contacts|bluetooth' "$PLAN"; then
  grep -qE 'NS[A-Za-z]+UsageDescription' "$PLAN" \
    || echo "BLOCK: a plan reaching a privacy-gated API must name the NS…UsageDescription key and say it lands in BOTH app plists — an undeclared gate terminates the process"
fi
true
```

## I3b. New app plists without the dictation pair — BLOCK

Any free-text field is a reachable microphone (the keyboard's dictation key
runs in-process), so the guard script requires the pair in every app, and a
scaffold that forgets it leaves the gate red for every later card.

```bash
if grep -qiE '(creat|scaffold)[a-z]* .{0,40}Info(-Debug)?\.plist|new (app )?target' "$PLAN"; then
  { grep -q 'NSMicrophoneUsageDescription' "$PLAN" && grep -q 'NSSpeechRecognitionUsageDescription' "$PLAN"; } \
    || echo "BLOCK: a plan creating the app plists declares NSMicrophoneUsageDescription and NSSpeechRecognitionUsageDescription in BOTH from the first commit — the keyboard's dictation key reaches them from any text field"
fi
true
```

## I3c. Frozen config with a red guard — BLOCK

```bash
if grep -qiE '(do not|don.t|never|must not) (touch|change|modify|edit)[^.]{0,60}(ios/Config|plist|entitlement)' "$PLAN" \
   && [ -f scripts/check-privacy-strings.py ] && ! python3 scripts/check-privacy-strings.py >/dev/null 2>&1; then
  echo "BLOCK: the plan freezes ios/Config/ while scripts/check-privacy-strings.py is already red — fix the plists as the plan's first step, or the builder is stuck between the gate and the plan"
fi
true
```

## I4. ATS exception outside the Debug plist — BLOCK

```bash
if grep -qiE 'NSAppTransportSecurity|NSAllowsArbitraryLoads|cleartext|http://' "$PLAN"; then
  grep -qiE 'Info-Debug|debug plist|debug twin|-Debug\.plist' "$PLAN" \
    || echo "BLOCK: an ATS exception belongs in the -Debug plist only — the plan must say so"
fi
true
```

## I5. Entitlement change without both twins — BLOCK

```bash
if grep -qiE 'entitlement|keychain-access-group|app group|aps-environment|associated domain' "$PLAN"; then
  grep -qiE 'both (files|twins|entitlements)|Release\.entitlements|debug and release' "$PLAN" \
    || echo "BLOCK: an entitlement change names both the Debug and Release entitlements files and what stays identical"
fi
true
```

## I6. New persisted key without a tolerant decode — WARN

```bash
if grep -qiE 'UserDefaults|keychain|Codable|persist|stored (field|key|property)|cache file|app group container' "$PLAN"; then
  grep -qiE 'tolerant|optional|default(s)? (on|when) (read|missing)|decodeIfPresent|missing key|migrat' "$PLAN" \
    || echo "WARN: a new persisted field is read by an older build after a downgrade and written by a newer one — say what its default is when the key is absent"
fi
true
```

## I7. Minute without both digits — WARN

```bash
grep -nF '.minute()' "$PLAN" && echo "WARN: .minute() drops the leading zero ('10:0') — use .minute(.twoDigits)"
true
```

## I8. Background task without registration — BLOCK

```bash
if grep -qiE 'BGTaskScheduler|BGAppRefreshTask|BGProcessingTask|background (refresh|task|fetch)|UIBackgroundModes' "$PLAN"; then
  grep -qiE 'BGTaskSchedulerPermittedIdentifiers|UIBackgroundModes|Info\.plist' "$PLAN" \
    || echo "BLOCK: a background task names its identifier and the Info.plist key it is registered under"
fi
true
```

## I9. Screen plan with logic only checkable on a screen — WARN

```bash
if grep -qiE '^\- \*\*Surfaces:\*\*.*(screen|widget)' "$PLAN"; then
  grep -qiE 'store|view ?model|pure function|@Observable|XCTest|Testing' "$PLAN" \
    || echo "WARN: a screen plan names the testable seam (a store, a view model, a pure function) so its logic is not a MANUAL criterion"
fi
true
```
