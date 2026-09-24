- **Money never touches a `Double`.** Name the money module every amount goes
  through. The one exception is chart geometry, in the one file
  `docs/context.md` names.
- **A permission surface names its strings.** A step that reaches the camera,
  microphone, speech, photos or location names the `NS…UsageDescription` key
  and says it lands in **both** app plists.
- **Every app plist starts with the dictation pair.** A plan that creates
  the app plists (the scaffold, a new target) declares
  `NSMicrophoneUsageDescription` and `NSSpeechRecognitionUsageDescription`
  in both, same text, from the first commit: any free-text field is a
  reachable microphone through the keyboard's dictation key, and
  `scripts/check-privacy-strings.py` requires the pair whatever the plan
  mentions.
- **Freezing a guarded file checks its guard first.** A plan that tells the
  builder to leave `ios/Config/` alone runs the privacy, ATS and
  entitlement guards while planning; one already red is fixed as the plan's
  first step, never left for the builder to find in a file it may not touch.
- **Configuration changes name both twins.** Anything in `ios/Config/` comes in
  pairs (Debug/Release entitlements, `Info` / `Info-Debug` plists); a plan
  touching one names the other and says what stays identical.
- **Background work names its registration.** A new background task names
  its identifier and the `Info.plist` key it is added to.
- **Persisted shapes decode tolerantly.** A new stored field says what its
  default is when the key is absent.
- **Screen plans name the seam.** A view's logic lives in a testable store or
  pure function; the `MANUAL:` criterion, if any, is about what a real screen
  shows, not about whether the logic works.
