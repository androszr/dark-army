- **The distribution build.** Simulator tests run a Debug build with the
  Debug entitlements (and the development APNs entitlement, when the app
  declares push) and the Debug plist. Only the exported `.ipa`
  says which entitlements shipped — the TestFlight workflow reads them back
  from the archive for exactly this reason.
- **The device.** Push delivery, background refresh timing, the keyboard's
  dictation key, the home indicator, a real keychain group shared with an
  extension.
- **A privacy prompt.** The simulator surfaces a missing usage string as a
  crash with no message; nothing in a unit test opens the camera.
- **A file written by the previous build.** Tests decode fixtures the current
  code wrote; a user upgrades from last month's shape.
- **The extension boundary.** A widget reads the keychain through its own
  access group; a mismatch renders "sign in" beside a signed-in app with no
  error anywhere.
