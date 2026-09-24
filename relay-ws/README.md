# Dark Army's socket relay — a live sealed line on Fly.io

This folder is the whole socket relay: one small Node service (`server.js`,
one dependency, `ws`) that holds **one live line to your Mac and one to your
phone** per paired phone and passes **sealed envelopes** straight across.
It is the fast lane beside the mailbox in `relay/`: the Mac sends the new
picture the moment it changes and a press goes straight down the line,
while the mailbox stays exactly as it is and takes over the moment the line
drops. The relay can never read, alter or forge anything it carries — every
envelope is ChaCha20-Poly1305, keyed with a secret only your two devices
hold, the same key and the same counters the mailbox uses. The security
boundary is that seal, not this service; the relay is deliberately dumb.

Five properties, each pinned by a test:

- **It can read nothing.** It forwards text frames verbatim and never opens
  one.
- **It logs nothing it carries.** There is no logging call in the file.
- **It caps how fast either side may send** — 240 frames a minute per side
  per channel, then the line is closed with `4008 slow down`.
- **It drops anything oversized** — a frame over 1 MiB closes the line.
- **It holds no password** that could be used against the Mac: no
  environment variable is read and no secret is stored. Knowing a channel's
  address buys an attacker ciphertext and denial of service, nothing more.

A second socket on a side already held replaces the first (`4001
replaced`), so a phone that reconnects does not fight its own ghost. An
upgrade carrying an `Origin` header (a browser) is refused with 403.

## Deploy, in five commands

```sh
cd relay-ws
fly launch --no-deploy   # once: creates the app from fly.toml, asks for a name
fly deploy               # builds the Dockerfile and starts one machine
fly status               # the app's https://…fly.dev address
fly logs                 # nothing carried is ever logged; only starts and stops
fly scale count 1        # one always-on shared machine
```

`fly.toml` keeps `auto_stop_machines = false` and `min_machines_running = 1`
on purpose: a machine Fly puts to sleep drops every open line. The cost is
one always-on shared machine.

Then put the address into Dark Army's panel with `wss://` in front (the
`https://…fly.dev` address with `https` replaced by `wss`): **⋯ → Devices →
Socket address…**, and turn on **Socket link** (Away access must already be
on). A phone copies the socket address at pairing time **while Socket link
is on**, so turn the link on first and pair the phone again at home
afterwards; a phone paired before, or while the link was off, keeps working
through the mailbox.

## What the operator of this deployment can see

You operate this relay, so be honest with yourself about what that position
sees, even though it can decrypt nothing:

- **channel ids** — opaque 32-hex strings, one per paired phone;
- **IP addresses** of your Mac and your phone while a line is open;
- **timing and volume** — when envelopes flow, and how big they are.

That is the mailbox's residual metadata cost again, no more: nothing is
stored, nothing is logged, and you are the only tenant.
