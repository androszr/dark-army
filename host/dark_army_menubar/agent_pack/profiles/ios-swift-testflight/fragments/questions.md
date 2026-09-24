## A. Surface

> Where does this show up?

- A new screen
- An existing screen — which one?
- A widget or extension
- No screen — data, networking or background behaviour only

## B. Data source

> Where does the information come from?

- The app's own server API (through the existing client)
- Data already on the device (store, keychain, defaults)
- A system framework (photos, camera, location, health, calendar) — this is a
  permission surface
- Derived from data already loaded

## C. Offline and persistence

*Ask only when the idea shows or edits data.*

> What happens with no network?

- Show the last known data, marked as stale
- Show an empty state and retry when back
- Queue the change and sync later
- Not applicable — it is local only

## D. Background

*Ask only when the idea implies work while the app is not on screen.*

> Does this need to run in the background?

- No — foreground only
- Refresh on a schedule (background refresh task)
- Triggered by a push notification
- Continuous (location, audio) — needs a background mode and a strong reason

## E. Permission

*Ask only when branch B chose a system framework.*

> When should the app ask for permission?

- The first time the user taps the feature that needs it
- On first launch, with an explanation screen first
- Never proactively — only when the system forces it

## F. Priority when the trade-off is real

*Ask only when the idea implies a genuine tension.*

> If these conflict, which wins?

- Fewer taps
- Never show a stale number
- Battery and data use
- Match the platform's own conventions
