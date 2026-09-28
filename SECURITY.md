# Security

Dark Army is a local tool. This page says what it opens on your machine and your
network, what it sends out, what it stores, and how to report a problem. The
full contract for the phone doors is
[docs/transport-contract.md](docs/transport-contract.md).

## Reporting a problem

If you find a way past any boundary on this page, please report it privately
through GitHub's **Report a vulnerability** button on the repository's
Security tab, rather than in a public issue. Say what you did, what you
expected and what happened; a short reproduction is the most useful thing you
can send, and please give a fix time to ship before sharing the details
publicly.

## What listens

- **Hook events arrive on a private socket file**, `~/.dark-army/hook.sock`:
  mode 0600 inside the 0700 `~/.dark-army` folder, so only your account can
  open it, and a connection from another account is closed unread. The old
  loopback port `19873` still listens for one release as a **bridge** for
  sessions started before the upgrade (terminals an older terminal helper
  opened, channels already running, and any shell that exports
  `BOB_COMPANION_PORT`); while it stands, another account that binds that
  port during a restart can still hear what those sessions send. A later
  release retires it.
- **Three network listeners, and only two of them are always there.** The
  hook bridge above and `127.0.0.1:19874`, which serves the window's HTTP +
  SSE API, are both bound to loopback. The third, `:19875`, is bound on **every
  interface** — reachable from anything on your network — and exists only while
  *Phone access* is on under Settings → **Devices**, which is off until you
  switch it on.
- **The phone door is sealed.** Every frame on `:19875` is an envelope sealed
  under a key minted when you paired that phone; the plaintext routes are
  refused outright, and the writes it carries out are a fixed list of actions
  chosen by name, never anything the API can do. Pairing by typing the Mac's
  address instead of scanning the QR code uses a password-authenticated
  exchange over the pairing code, and is accepted only for a pairing the Pair
  window armed for it. A shared Wi-Fi still sees the door exists; the seal is
  the boundary.
- **Refused knocks are written down.** Every request either phone door turns
  away is logged with its reason and never its contents, and a burst of them
  raises an alert under Settings → **Security** (*Access log…*).
- **Most reads on the local API are unauthenticated**, so
  `curl :19874/api/state` works; the knowledge notes, conversation and access
  log reads need a token. Anything that changes state needs the **desk token**
  in the `X-Bob-Token` header: it lives only in memory, is handed to the panel
  over a private pipe, and reaches your own command-line tools only through
  Settings → Advanced → **Copy desk key**. The file `~/.dark-army/api-token`
  (mode 0600) is the **session token**, readable by any program running as
  you, so it only closes terminals, brings the panel forward and opens a few
  reads (`docs/transport-contract.md`, *The loopback door has two tokens*).
  Neither is something a web page in your browser can obtain. Every request, read or write, must also be *addressed* to loopback: a
  `Host` header naming anything else is refused, which stops a page using DNS
  rebinding to read what it cannot read cross-origin.

## What reaches out

- **Away access makes the Mac reach out.** Switch it on (Settings → **Devices**,
  off by default) and the daemon long-polls a relay mailbox you deploy yourself
  (`relay/`), carrying the same sealed envelopes; the relay cannot read them.
  *Socket link* adds a live line through a second service you deploy
  (`relay-ws/`), with the same seal. Writes from away need a window that only
  a verified frame on your home Wi-Fi can open or extend, so a phone that never
  comes home loses its writes and keeps its reads, and the away action list is
  separate from, and never larger than, the home one. Un-pairing revokes the
  key at once.
- **A phone buzz is not sealed.** Each buzz goes through your relay and
  Apple's push service in plaintext, and shows on the phone's Lock Screen. It
  carries the agent's name, what kind of thing it is waiting for, a count, the
  title of the card or session it is working on (up to 80 characters), and one
  line saying what is needed: the agent's question or its summary (up to 120
  characters), or which tool it wants to run. The Lock Screen card for the
  agent at the top of Needs you carries the name and that title too. No
  command, path, project or branch is sent. There is no switch to leave these
  lines out, so pair a phone and turn on away access only if that is
  acceptable for your work.
- **Two outbound requests for usage figures**, over verified TLS, with nothing
  about your sessions in either:
  - `GET cli-chat-proxy.grok.com/v1/billing` with the token from
    `~/.grok/auth.json`, only if you use Grok.
  - `GET api.anthropic.com/api/oauth/usage` with the OAuth **access** token from
    your login keychain (the item Claude Code signed you in with), for the
    per-model weekly window. Dark Army reads that one item and never the refresh
    token, and never writes, refreshes or invalidates your login. At most one
    request per five minutes, one per minute while failing. macOS may ask once
    whether Dark Army may read that item; saying no is fine, and the last
    figure stays on screen with a note saying how old it is. Nothing from the
    response is written to disk.
- **Helper calls on your account.** Session titles, card priorities and
  **Prepare** run a short `claude -p` on Haiku with no hooks, no MCP servers
  and no saved transcript; a card set to Codex or Grok prepares on that
  assistant's own small model. They count against your own usage like any
  other call.

## What it can start

- **Dark Army can start agent sessions.** Only a deliberate press or drag on
  the board reaches it; a queued card waits for a place that press already
  asked for, a card you ticked to start when planned starts when its plan is
  attached, and Mission Control opens from the Comm tab or the phone. The executable comes from a fixed list, the card contributes
  exactly one argument (the prompt, last, never through a shell), the working
  directory must be a project you have open and have enrolled, and at most two
  starts are in flight at once, with a per-project limit on top (one agent by
  default). *Dark Army may start sessions* under Settings → **Board** removes
  the capability outright. A started session is an ordinary session with
  ordinary powers: nothing here sandboxes the agent.
- **Destructive actions re-check at the moment they fire.** Stop re-verifies
  that the process it holds is still the one it recorded before signalling.
  Retire deletes only a record in the abandoned bucket, re-checked at deletion.
  Neither touches a transcript.
- **Only enrolled projects are watched.** Each carries a key file; an event
  from any other folder is turned away, so an agent in a repository you were
  passing through cannot use the board to reach your other projects. The key
  is a scoping boundary, not protection from a process that can read it.

## The channel

The channel is the sharpest edge, which is why it is opt-in. A session started
with `--dangerously-load-development-channels server:dark-army` exposes a loopback
port that can put text into that agent's context, and the agent is told to
treat it as if you had typed it. The port is guarded by a password minted per
session and held only in memory, so reaching the socket is not enough — but a
process running as you that can read the daemon's memory has already won, and
nothing here changes that. The board tools it offers act only for the calling
session — its own card, its own project — and none takes a card id or a
project ([docs/channel-tools.md](docs/channel-tools.md)).
Leave the channel off unless you want replies and permission relay.

## What is stored

`~/.dark-army/` (mode 0700) holds session state, a history database of per-turn
token counts and costs, the board database (every card, its instructions and
its project), names minted for sessions, the event and access logs, and the
phone pairing keys. The files that hold more than "Dark Army is installed" are
0600. No transcripts are copied; agents' questions, summaries and reports
are kept in these 0600 files (the session state, the decisions record and the
event log), and the log at `~/Library/Logs/DarkArmy/dark-army.log` records
lengths and ids rather than content.

## Stopping everything

*Kill switch — stop everything*, at the foot of the Settings sidebar, stops
every Dark Army process on this Mac at once, including the hosted terminals
and the channel inside each session, with no clean shutdown and no undo
([docs/kill-switch.md](docs/kill-switch.md)).
