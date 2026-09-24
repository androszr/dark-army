- **Money is never a `Double`.** Amounts go through the money module to
  `Decimal`. **Lead with this one**: the damage is silent and cumulative.
  Plain-language framing: "the numbers would be slightly wrong in a way
  nobody notices until they do not add up."
- **Entitlements and plists in pairs.** Debug and Release twins that drift ship
  a distribution build quietly missing a capability — the widget stops seeing
  the keychain, a passkey ceremony fails, with no error anywhere.
- **Privacy strings.** An undeclared, reachable TCC gate is an app that
  vanishes mid-typing.
- **ATS exceptions.** Cleartext in a shipping plist is a rejected review or a
  silent downgrade.
- **Persisted state, both directions.** A new key without a default, a
  keychain shape changed without migration — one absent field blanks the
  screen.
- **Background work.** An unregistered task never runs; a handler that never
  completes gets the app throttled.
- **Push.** The sandbox APNs entitlement in a distribution build uploads fine
  and silently receives nothing.
- **Main-actor discipline.** A blocking wait on the main actor is a frozen
  screen that reads as an ignored tap.
