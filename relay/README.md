# Dark Army's relay — a sealed mailbox on Vercel

This folder is the whole relay: one serverless function (`api/box.js`, no
dependencies) that carries **sealed envelopes** between your Mac and your
phone when the phone is away from home. The relay can never read, alter or
forge anything it carries — every envelope is ChaCha20-Poly1305, keyed with a
secret only your two devices hold. The security boundary is that seal, not
this service; the relay is deliberately dumb.

Messages live at most **120 seconds**, a flooded channel keeps only its
newest 32 envelopes, and each channel is capped at 240 requests a minute.
Nothing carried is ever logged — there is no logging call in the function at
all.

## Deploy, in five commands

```sh
cd relay
npm i -g vercel            # once, if you don't have the CLI
vercel deploy --prod       # creates the project, prints its URL
vercel kv create bob-relay # or create an Upstash Redis store in the dashboard
vercel env pull            # confirm KV_REST_API_URL / KV_REST_API_TOKEN are set
```

If you create the KV store in the Vercel dashboard instead, link it to the
project so the two environment variables `KV_REST_API_URL` and
`KV_REST_API_TOKEN` exist in production, then redeploy.

Finally, put the deployment's `https://…` address into Dark Army's panel:
**⋯ → Devices → Relay address…**, and turn on **Away access**.

## Push (`api/push.js`) — environment variables

The push route forwards one tiny nudge to Apple so a sleeping phone can show
a banner. It needs five environment variables in production:

- `APNS_KEY_P8` — the `.p8` provider key's contents;
- `APNS_KEY_ID` — that key's id;
- `APNS_TEAM_ID` — your Apple developer team id;
- `APNS_TOPIC` — the app's bundle id;
- `PUSH_SECRET` — a password you invent. The route refuses any request
  whose `Authorization: Bearer …` header does not match it (compared in
  constant time), so only your Mac — which stores the same secret in the
  panel's **Relay address…** sheet — can send a buzz.

All five are required: with **any** of them unset the route answers 503
"unconfigured" and sends nothing — off, never open. (`APNS_TOPIC` included;
without it Apple would refuse every push with MissingTopic, which used to
surface as a generic 502 that looked like an outage.)

Rate caps: 30 pushes a minute per channel and 120 a minute across all
channels, so a rotated channel id cannot dodge the window.

Behaviour details, so an odd answer can be read: the APNs request itself is
bounded at 8 seconds (a hung Apple connection is this route's own 503, never
Vercel's 504); a 403 from Apple — usually a rotated provider key invalidating
the cached JWT — mints a fresh token and retries once; and every buzz carries
a collapse id and a short (10-minute) expiration, so a phone that was offline
wakes to **one** collapsed banner rather than a backlog of everything it
slept through.

**Push titles travel in plaintext.** Unlike everything else the mailbox
carries, a banner cannot be sealed — iOS renders it from the plaintext `aps`
dict, so what the banner says transits Vercel and Apple readable. The route
builds that dict itself from a closed set of short caller fields and nothing
else: `title` (a nickname plus a fixed phrase, `aps.alert.title`), `work`
(one line naming the card or session, clamped to 80 characters, the
subtitle), `need` (one line saying what is needed — the agent's own summary
or question, or a tool's bare name — clamped to 120 characters, the body),
`badge` (a count), `kind` (`security`, `permission`, `question`,
`attention`, `finished` — mapped to a bundled sound name here; an absent or
unknown kind is the default sound), and, for a phone whose lock-screen
switch is on, an `act` word (`permission` / `acknowledge`) plus opaque
`session_id` / `request_id` identifiers that only pick the notification
category. The command being approved, a file path, a project name and a
branch never ride. The same route carries the phone's Live Activity under
`event` (`update` / `end`), its content-state built here from the same
closed set. `face` is a cast slug: when it names one agent the banner may
be adjusted before it is shown, and no image rides the wire.

## Deployment Protection must be OFF for production

The phone talks to this function directly, and it cannot pass a Vercel SSO
wall — so Standard Protection has to be disabled for the production
deployment (Project → Settings → Deployment Protection). That is acceptable
here because the mailbox holds only ciphertext with a 120-second TTL behind a
rate cap: 240/min per channel, 480/min per sending address and 2,400/min
across the whole relay, each per method, so rotating channel ids cannot run
up your Upstash bill without limit; only a sender within its own caps counts
toward the relay-wide one, so one address alone cannot fill it. Knowing a channel's address buys an attacker ciphertext and denial
of service, nothing more; the AEAD on both ends is the boundary.

## What the operator of this deployment can see

You operate this relay, so be honest with yourself about what that position
sees, even though it can decrypt nothing:

- **channel ids** — opaque 32-hex strings, one per paired phone;
- **IP addresses** of your Mac and your phone, whenever they poll;
- **timing and volume** — when envelopes flow, and how big they are.

That is the residual metadata cost of any relay. It is shrunk by the short
TTL, the absence of body logging, and the fact that you are the only tenant —
but it cannot be removed by design, so it is written down here instead.
