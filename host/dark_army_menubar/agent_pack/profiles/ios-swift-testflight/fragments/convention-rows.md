1. **No `Double` near money.** Amounts arrive as strings and go through one
   money module to `Decimal`. A `Double(` in the client is the same bug the
   web preflight greps for as `parseFloat`; the only sanctioned exception is
   chart geometry, in one named file. *(preflight BLOCK, verifier grep, CI)*
2. **Every reachable privacy gate is declared.** A camera, microphone,
   speech, photo or location API the app can reach has its
   `NS…UsageDescription` in **both** app Info.plists, with the same text.
   An undeclared gate does not deny — it terminates the process with no
   error. *(preflight BLOCK, guard script, CI)*
3. **No ATS exception in a shipping plist.** Cleartext localhost lives in the
   `-Debug` twin only. *(preflight BLOCK, CI)*
4. **Debug and Release entitlements match.** The one key allowed to differ
   is `aps-environment` (`development` / `production`), and it is optional:
   an app with no push carries it in neither file, which is consistent. Once
   either file claims it, both must, each with its own value. A capability
   added to one and not the other ships a Release build quietly missing it.
   *(preflight BLOCK, guard script, CI)*
5. **Persisted shapes decode tolerantly.** A new key on anything stored in
   UserDefaults, the keychain, a file or a shared container has a default on
   read; synthesized `Decodable` throws on a missing key even with a default.
   *(preflight WARN, app reviewer)*
6. **Background work is registered.** A new `BGTaskScheduler` identifier is in
   `BGTaskSchedulerPermittedIdentifiers`; a background mode is in
   `UIBackgroundModes`. *(preflight BLOCK)*
7. **Clock times print both digits.** `.minute(.twoDigits)`, never
   `.minute()` — "10:0" beside money is a wrong number. *(verifier grep, CI)*
8. **Never commit or push** unless explicitly asked.
