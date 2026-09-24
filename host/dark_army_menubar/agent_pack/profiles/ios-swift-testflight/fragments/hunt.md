1. **Decimal safety** — a `Double` in a money path; a string amount parsed
   with `Double(` instead of the money module; a `Decimal` formatted through
   a `Double` conversion; rounding applied twice.
2. **Main-actor discipline** — UI state mutated off the main actor; a
   blocking wait on the main actor (a synchronous network or keychain call
   in a view body); an `@Observable` store touched from a background task
   without isolation. The symptom is a frozen screen, which reads as "the tap
   didn't register".
3. **Persisted-state tolerance** — a new `Codable` field without a default
   decoded through synthesized `Decodable`; a UserDefaults key read as
   non-optional; a keychain item whose shape changed with no migration. One
   absent key blanks the whole screen.
4. **Lifecycle and background** — work scheduled in `init` before the scene
   is active; a `BGTask` handler that never calls `setTaskCompleted`; a task
   that assumes the app is foregrounded; an `.onAppear` that fires twice and
   double-loads.
5. **Networking** — a cleartext URL that only works on the simulator; a
   request without a timeout; a response decoded with no tolerance for a new
   field; a token refreshed on one path and not the other.
6. **Permissions** — a TCC-gated API reachable from a path with no usage
   string; a permission requested before the user has a reason to say yes.
7. **Extension parity** — a widget or extension reading a keychain group or
   app group the app writes differently; a shared model changed on one side
   only.
8. **Accessibility and appearance** — meaning carried by colour alone; a
   control with no accessibility label; a fixed font size that ignores
   Dynamic Type; a layout that breaks in one appearance or on one device
   size.
