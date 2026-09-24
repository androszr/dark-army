- **Money goes through the money module.** Never `Double(` an amount.
- **Both plists, both entitlements.** A key added to a Debug file is added to
  its Release twin in the same pass — `aps-environment` included, with
  `development` in Debug and `production` in Release.
- **UI on the main actor.** Store mutations that feed a view happen on the
  main actor; long work happens off it and hops back.
- **Tolerant decode** for any new persisted field.
- **`.minute(.twoDigits)`**, never `.minute()`.
- **Generated Swift is regenerated, never hand-edited.** If contracts or
  tokens are generated from another tree, run the generator and commit its
  output alongside.
