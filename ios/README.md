# Dark Army on the phone (`BobPhone`)

A read-only iPhone view of Dark Army: the live fleet and the four board
columns. Pair once by scanning a QR code on the Mac, then Face ID (or the
phone passcode) on every open.

**This is not built by `host/build.sh`.** It is not part of the py2app bundle.
The binary reaches the phone through **TestFlight**,
signed with the team named in `ios/Config/Identity.xcconfig`. At home the phone talks to the Mac on this
Wi-Fi (port 19875); away from home it rides the sealed relay mailbox
(`relay/`, one zero-dependency Vercel function), and the same Vercel app's
`/api/push` forwards alert buzzes to APNs — so Dark Army **does** have push, and
the relay is the one deployed piece outside this repo's build.

## Download it (TestFlight)

Once the app record and GitHub secrets exist (below), a push to `main`
that touches `ios/**` uploads a build. Internal testers see it in
TestFlight after App Store Connect finishes processing — no Beta App
Review.

Separately, **every** push touching `ios/**` — on any branch — compiles the
app plus the widget and runs both test bundles (`BobPhoneTests` and
`BobPhoneWidgetTests`) on a simulator, through `tests.yml`'s
`Phone (xcodebuild test)` job. The TestFlight upload is still `main`-only.

1. Install **TestFlight** from the App Store, open the Dark Army build.
2. On the Mac, rebuild and restart Dark Army. Panel → ⋯ → Devices → turn on
   **Phone access** → **Pair a device…**
3. Unlock Dark Army on the phone with Face ID, scan the QR.

## First time (your Apple and GitHub accounts)

These are the steps that need your Apple / GitHub accounts. The phone's
identity is set in one file, `ios/Config/Identity.xcconfig`: put your team
id in `DEVELOPMENT_TEAM` and your bundle id in `DARK_ARMY_BUNDLE_ID`. The
widget, the notification extension, the App Group
(`group.<your bundle id>`) and the background task id all derive from it.

1. [developer.apple.com](https://developer.apple.com) → Identifiers →
   register your bundle id (App, your team).
2. [App Store Connect](https://appstoreconnect.apple.com) → My Apps →
   New App → iOS, your bundle id, name **Dark Army**.
   Add yourself as an internal tester.
3. Your GitHub repository → Settings → Secrets → copy the
   three App Store Connect API key values: `ASC_PRIVATE_KEY`, `ASC_KEY_ID`, `ASC_ISSUER_ID`.
4. Merge / push the `ios/` tree to `main`, or run the **TestFlight**
   workflow by hand. The first upload creates the TestFlight build.

## Push notifications — one-time setup (your accounts, not the code's)

The buzz travels Mac → relay (`/api/push`) → APNs → phone. The relay signs
its own APNs requests, so it needs an Apple push key in its environment:

1. [developer.apple.com](https://developer.apple.com) → Certificates,
   Identifiers & Profiles → **Keys** → **+** → tick **Apple Push
   Notifications service (APNs)** → Continue → Register. Download the
   `.p8` file (one chance) and note the **Key ID**.
2. Vercel → the relay project → Settings → **Environment Variables** →
   add four, for Production:
   - `APNS_KEY_P8` — the `.p8` file's entire contents (the PEM text)
   - `APNS_KEY_ID` — the Key ID from step 1
   - `APNS_TEAM_ID` — your team id (`DEVELOPMENT_TEAM` in `ios/Config/Identity.xcconfig`)
   - `APNS_TOPIC` — your bundle id (`DARK_ARMY_BUNDLE_ID` in the same file)
3. Redeploy the relay (Deployments → ⋯ → Redeploy). The functions in
   `relay/api/` only reach Vercel when you deploy them; nothing in this
   repo's build does it.
4. On the Mac: panel → ⋯ → Devices → **away access** on, then pair (or
   re-pair) the phone. ⋯ → Notifications → **Phone push notifications**
   is the off-switch for the buzzes alone.
5. First device build after this change: let Xcode's automatic signing
   register the push capability and the App Group on the App ID.

One honest wrinkle: a TestFlight build's token is valid against Apple's
**production** push host, an Xcode device run's against the **sandbox**
one. The app reports which it is (`env` on registration) using `#if
DEBUG`, so an Xcode *Release* device run mislabels itself and its buzzes
go nowhere — use TestFlight or a plain Debug run.

## Run from Xcode (the cable, for debugging)

The team is already in `ios/Config/Base.xcconfig`. Open
`ios/BobPhone.xcodeproj`, select the **BobPhone** scheme, pick your
iPhone, press Run. The simulator cannot scan a camera QR; use the
manual host / port / code fields there.

iOS 17 or later. A paired phone can file a new card, move one between
columns, reply to an agent that can hear it, start or refine work, stop
a run, delete a leftover, answer a permission prompt, and close the
empty terminal a finished report left behind. Pairing is the whole key.
An older Mac that has not learned these commands says so in words.
