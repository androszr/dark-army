---
name: release-ios
description: Runbook for shipping the {{PROJECT}} iOS app — what a push to main
  under ios/ sets in motion (archive, export with the App Store Connect key,
  verify the entitlements from the .ipa, upload to TestFlight), how to confirm
  the build reached the phone, the one-time App Store Connect setup, and the
  rules that keep a build from uploading fine and then silently not working.
  Use when the user says /release-ios, "ship the app", "push a TestFlight
  build", "did the build go up", or when setting up a new app record.
---

# release-ios

**Pushing to the default branch under `ios/` IS the release.**
`.github/workflows/testflight.yml` fires on every push to `main` that touches
`ios/**`, and internal TestFlight testers get the build as soon as App Store
Connect finishes processing it — usually a few minutes, with no Beta App
Review. Nothing in this skill runs an upload by hand. The pipeline never
commits or pushes; releasing is the user's push.

## What the workflows do

| Workflow | Trigger | Does |
|---|---|---|
| `ios.yml` | every PR and push, **no path filter** | runs the guard scripts' own tests; refuses `Double(` near money, `.minute()`, an ATS exception in a shipping plist, drifted entitlements, a keychain group referenced by some files and not others and a missing privacy string (source guards only: no Node, no codegen) |
| `testflight.yml` | push to `main` touching `ios/**`, or manual dispatch | selects the newest Xcode and demands Swift 6.2+; writes the ASC key outside the workspace; archives with `CURRENT_PROJECT_VERSION=<run number>` and `DEVELOPMENT_TEAM` from the `APPLE_TEAM_ID` secret; exports a distribution build; **reads `aps-environment` back from the `.ipa`** and refuses anything but `production` (skipped while the app declares no push capability); uploads; shreds the key |

`ios.yml` deliberately has no `paths-ignore`: a guard that skips the files it
guards is decoration. It costs about a minute of Linux on commits that touch
neither side.

## Rules

- **The build number is the run number.** It must strictly increase per
  (bundle id, marketing version); `github.run_number` does that and stays
  traceable to a run. Never set it by hand.
- **Never cancel an upload in flight.** A half-sent binary still consumes its
  build number and the next run collides with it. The workflow's concurrency
  group is set not to cancel.
- **The runner image is a dependency of the source.** Swift 6.2's default
  main-actor isolation is what the app compiles against; the workflow selects
  the newest Xcode on the image and fails on the toolchain line rather than on
  forty concurrency errors later. When Apple ships a new SDK, bump `runs-on`
  and the version demand together.
- **Certificates are imported, never minted; no profile secrets.** A runner
  is a blank Mac, and left to `-allowProvisioningUpdates` alone it minted a
  new certificate on every run until the Apple account hit its cap ("Choose
  a certificate to revoke"). So the Apple Development AND Apple Distribution
  certificates are exported once from the owner's Keychain as one `.p12`,
  stored base64-encoded in `IOS_DIST_P12` with its password in
  `IOS_DIST_P12_PASSWORD`, and imported into a throwaway keychain. Both are
  needed: automatic signing archives with Development (and refuses a
  Distribution override) and the export re-signs with Distribution. Profiles
  are still fetched with the API key. Key and keychain are shredded on exit.
- **The entitlements that count are the ones in the `.ipa`.** A build signed
  with the sandbox APNs entitlement uploads fine and silently receives no
  pushes. The workflow reads them back; so should you, when in doubt:
  `codesign -d --entitlements :- <App>.app | plutil -extract aps-environment raw -o - -`.
- **A TestFlight build expires 90 days after upload.** So does whatever API
  contract it speaks; if the server has a contract version, the Release
  xcconfig bakes it in for the same reason.
- **Verify with `gh run list --workflow testflight.yml --limit 3`**, then
  App Store Connect → TestFlight → the build shows "Ready to Test". The step
  summary of the run names the build number and commit.

## When asked to release

1. Confirm the working tree is committed on `main` and that the commit
   touches `ios/**` — otherwise the workflow will not fire, and say so.
2. Say what the push will set in motion, in one sentence.
3. After the user pushes, watch `gh run list --workflow testflight.yml
   --limit 1` until green or red. On red, `gh run view <id> --log-failed` and
   report the failing step in plain words — the toolchain demand, the
   entitlement read-back and the upload are the three that fail for real
   reasons.
4. Report the build number and that it will reach the phone when App Store
   Connect finishes processing.

## One-time App Store Connect setup

Do these once per app; the workflow needs nothing else.

1. **Register the bundle id** at developer.apple.com → Identifiers, with the
   capabilities the entitlements files claim (push, keychain sharing, app
   groups, associated domains — whichever apply).
2. **Create the app record** in App Store Connect → Apps → + with that bundle
   id. The marketing version in the project must match the version the record
   expects.
3. **Create an API key**: Users and Access → Integrations → App Store Connect
   API → Team Keys → +, role **App Manager**. Download the `.p8` once (it
   cannot be downloaded again). Note the Key ID and the Issuer ID.
4. **Store the four secrets** in the GitHub repo: `ASC_PRIVATE_KEY` (the
   whole `.p8` file contents), `ASC_KEY_ID`, `ASC_ISSUER_ID`, and
   `APPLE_TEAM_ID` (the 10-character team id from developer.apple.com →
   Account → Membership details; the archive step refuses to run without it).
5. **Export the signing certificates once.** Xcode → Settings → Accounts →
   Manage Certificates → + → Apple Distribution (Apple Development is there
   already). Then Keychain Access → login → My Certificates → ⌘-click both
   "Apple Development: …" and "Apple Distribution: …" → right-click → Export
   2 items… as one `.p12` with a password, and
   `base64 -i signing.p12 | gh secret set IOS_DIST_P12` plus
   `gh secret set IOS_DIST_P12_PASSWORD`. Delete the `.p12` afterwards.
6. **Fill `ios/Config/ExportOptions.plist`** with the team id; `method` stays
   `app-store-connect` and `destination` stays `export` (the workflow flips a
   copy to `upload`).
7. **Add yourself as an internal tester**: App Store Connect → TestFlight →
   Internal Testing → + group → add your Apple ID. Internal testers need no
   review.
8. **Push notifications (if used)**: create an APNs key (Keys → + → Apple Push
   Notifications service), store it where the server sends from, and keep the
   Debug entitlement `development` and the Release entitlement `production` —
   they are different token universes.

## Secrets the workflow expects

| Where | Name | Used by |
|---|---|---|
| GitHub secret | `ASC_PRIVATE_KEY` | archive, export, upload (provisioning) |
| GitHub secret | `ASC_KEY_ID`, `ASC_ISSUER_ID` | the same |
| GitHub secret | `APPLE_TEAM_ID` | archive (`DEVELOPMENT_TEAM`; the team id stays out of the repository) |
| GitHub secret | `IOS_DIST_P12`, `IOS_DIST_P12_PASSWORD` | one .p12 with the Apple Development (archive) and Apple Distribution (export) certificates |

Prefer several small conventional commits with real reasoning in the message
over one blob — the history is bisectable and the *why* is what a future
reader needs.
