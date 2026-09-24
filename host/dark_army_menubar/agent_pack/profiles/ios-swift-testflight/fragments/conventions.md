| No Double near money | `grep -rn 'Double(' ios --include='*.swift' \| grep -v '<the one sanctioned chart-geometry file from docs/context.md>'` | no output |
| No minute without both digits | `grep -rn --include='*.swift' -e '\.minute()' ios` | no output |
| No ATS exception in a shipping plist | `grep -l 'NSAppTransportSecurity' ios/Config/Info.plist ios/Config/Info-*.plist \| grep -v -- '-Debug'` | no output |
| Entitlements in step | `python3 scripts/check-entitlements.py` | exit 0 |
| Privacy strings declared | `python3 scripts/check-privacy-strings.py` | exit 0 |
| Keychain group not inlined | `python3 scripts/check-keychain-group.py` | exit 0, no output (no file references it, or every file exactly once) |
| Background tasks registered | for each `BGTaskScheduler.register(forTaskWithIdentifier: "<id>"` in `ios/`, `grep -c '<id>' ios/Config/Info.plist` | ≥1 per identifier |

Notes on those checks: the `Double(` grep exempts exactly one file by name —
the chart-geometry file, because Swift Charts plots Doubles — and that
allowlist stays one file wide. The plist checks are parsed, not diffed, so a
comment or a reordering is not a failure and a real key change is.
