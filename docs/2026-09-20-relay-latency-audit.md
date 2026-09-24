# Relay latency audit — where the time goes between the phone and the Mac

- **Date:** 2026-09-20
- **Plan:** *relay latency telemetry and audit*
- **Area:** pocket
- **What this is:** a measured account of the phone's two doors — the away
  mailbox and the home Wi-Fi door — with the telemetry that now records
  every trip on both ends, a reproducible local measurement, the
  bottlenecks it shows, the three cards already on file for some of them,
  and a ranked list of what lies beyond those. No card is filed by this
  document; the ranked table is the handoff.

## What was measured and how

**The instrument.** `tools/relay_latency_bench.py` drives the real relay
connector (`host/dark_army_daemon/relay_client.py`) against the test
suite's fake mailbox (`host/tests/test_relay_client.py`, a stdlib HTTP
server on a loopback port) with real sealed envelopes, a fleet of 20
synthetic sessions (~2 KB of last words each, stats and a 90-point trend
per row) and a board of 40 cards. It runs four trips per round — a whole
`state` read, a `state` read quoting the digest (answered `unchanged`), a
`usage` read and one `action` (`inbox_ack` with an empty payload, refused
in words before anything runs) — and reads, per trip: `post_to_pickup`
(the phone's POST landing to the Mac's held GET taking the frame, off the
mailbox's own GET stamps), the Mac's four legs echoed on the reply
envelope (`mac_dwell`, `mac_hold`, `mac_open`, `mac_run`), and `total`
(POST to opened reply). The relay's two per-device buckets
(`RELAY_MAX_FRAMES_PER_MINUTE` 60, `RELAY_MAX_WRITES_PER_MINUTE` 10 in
`host/dark_army_daemon/relay.py`) are lifted for the run — a hundred
trips in ten seconds would otherwise measure the buckets — and are stated
here as what a real phone lives under. Python 3.12 on the Mac, both runs
20 Sep 2026.

**Run 1 — the code path alone** (`cd host && .venv/bin/python
../tools/relay_latency_bench.py --sessions 20 --cards 40 --rounds 30`; the
fake mailbox ticks every 0.05 s while holding a poll, so what is left is
the Mac's and the phone's own work):

| trip | leg | p50 | p90 | max |
|---|---|---|---|---|
| state | post_to_pickup | 0.025 | 0.061 | 0.072 |
| state | mac_dwell | 0.021 | 0.050 | 0.061 |
| state | mac_hold | 0.058 | 0.065 | 0.120 |
| state | mac_open | 0.000 | 0.000 | 0.000 |
| state | mac_run | 0.005 | 0.010 | 0.011 |
| state | total | 0.060 | 0.114 | 0.129 |
| state_unchanged | post_to_pickup | 0.026 | 0.053 | 0.059 |
| state_unchanged | mac_dwell | 0.015 | 0.046 | 0.052 |
| state_unchanged | mac_hold | 0.064 | 0.071 | 0.120 |
| state_unchanged | mac_open | 0.000 | 0.000 | 0.000 |
| state_unchanged | mac_run | 0.004 | 0.005 | 0.011 |
| state_unchanged | total | 0.059 | 0.062 | 0.117 |
| usage | post_to_pickup | 0.018 | 0.049 | 0.059 |
| usage | mac_dwell | 0.016 | 0.046 | 0.055 |
| usage | mac_hold | 0.063 | 0.067 | 0.112 |
| usage | mac_open | 0.000 | 0.000 | 0.000 |
| usage | mac_run | 0.008 | 0.011 | 0.963 |
| usage | total | 0.060 | 0.114 | 1.031 |
| action | post_to_pickup | 0.019 | 0.049 | 0.063 |
| action | mac_dwell | 0.018 | 0.046 | 0.060 |
| action | mac_hold | 0.061 | 0.114 | 1.034 |
| action | mac_open | 0.000 | 0.000 | 0.000 |
| action | mac_run | 0.000 | 0.000 | 0.000 |
| action | total | 0.061 | 0.063 | 0.111 |

| reply | plain bytes | deflated | sealed (base64) |
|---|---|---|---|
| state | 145988 | 4718 | 6328 |
| state_unchanged | 139 | 115 | 192 |
| usage | 1494 | 542 | 760 |
| action | 50 | 45 | 100 |

**Run 2 — the real mailbox's tick** (`… --mailbox-tick 2.0 --rounds 10`;
the fake now sleeps `POLL_MS`'s 2 s between looks while holding, as
`relay/api/box.js` does):

| trip | leg | p50 | p90 | max |
|---|---|---|---|---|
| state | post_to_pickup | 1.944 | 1.981 | 2.004 |
| state | mac_dwell | 1.940 | 1.980 | 2.000 |
| state | mac_hold | 2.006 | 2.011 | 2.011 |
| state | mac_open | 0.000 | 0.000 | 0.000 |
| state | mac_run | 0.003 | 0.004 | 0.008 |
| state | total | 2.007 | 2.012 | 2.058 |
| state_unchanged | post_to_pickup | 1.946 | 1.958 | 1.990 |
| state_unchanged | mac_dwell | 1.940 | 1.955 | 1.986 |
| state_unchanged | mac_hold | 2.014 | 2.015 | 2.022 |
| state_unchanged | mac_open | 0.000 | 0.000 | 0.000 |
| state_unchanged | mac_run | 0.002 | 0.004 | 0.011 |
| state_unchanged | total | 2.006 | 2.011 | 2.012 |
| usage | post_to_pickup | 1.945 | 1.964 | 1.991 |
| usage | mac_dwell | 1.943 | 1.963 | 1.989 |
| usage | mac_hold | 2.013 | 2.015 | 2.020 |
| usage | mac_open | 0.000 | 0.000 | 0.000 |
| usage | mac_run | 0.005 | 0.595 | 1.056 |
| usage | total | 2.007 | 4.062 | 4.068 |
| action | post_to_pickup | 1.939 | 1.973 | 1.991 |
| action | mac_dwell | 1.938 | 1.971 | 1.990 |
| action | mac_hold | 2.006 | 2.011 | 2.012 |
| action | mac_open | 0.000 | 0.000 | 0.000 |
| action | mac_run | 0.000 | 0.000 | 0.000 |
| action | total | 2.008 | 2.010 | 2.010 |

| reply | plain bytes | deflated | sealed (base64) |
|---|---|---|---|
| state | 145987 | 4717 | 6328 |
| state_unchanged | 138 | 113 | 188 |
| usage | 1494 | 540 | 760 |
| action | 50 | 45 | 100 |

**What the two runs say together.** The Mac's own work on a request is
milliseconds: opening the seal is below the timer's resolution, running
a whole `state` read over 20 sessions and 40 cards is 5 ms typical and
11 ms at worst, and the sealed answer is 6.3 KB where the plain picture
is 146 KB (deflate does 30:1 on this JSON). The one Mac-side outlier is
`usage`, whose run hits 1 s once in thirty — a cold `_usage_report_for`
(the first read of a window is a history query on the executor); every
later read is 8 ms. Everything else in the trip is **waiting on the
mailbox's clock**: with the real 2 s tick, a trip that costs 60 ms of work
costs 2.0 s (p50) — the Mac's held GET sees the frame at the next tick
(`mac_dwell` 1.94 s, `mac_hold` 2.01 s, both a whole tick) — and a reply
that lands after the phone's own held GET has just looked costs a second
tick (`usage` p90 4.06 s: the 0.6 s run pushed the answer past the tick
boundary). The tick arithmetic: `POLL_MS` 2000 ticks inside `WAIT_MS`
20000 under `maxDuration` 25 (`relay/vercel.json`), on **both** legs of a
trip, so the floor under one request inside the active window is
0–2 s to be picked up plus 0–2 s to be answered — **0 to 4 s of pure
waiting**, typically ~2 s, before any network or cold start. The bench
draws the lower half of that range because the fake mailbox's two legs are
in lock-step; the field figures will show the full range.

**The telemetry the phone and the Mac now keep.** Every request on both
doors is timed on the Mac (`host/dark_army_daemon/link_timing.py`;
`relay_client.py`'s `_serve` → `_handle_wire` → `_execute` for the mailbox,
`api_server.py`'s `_lan_home` for Wi-Fi), the Mac's legs ride the reply
envelope as `timing` beside `body`, and one ten-minute rollup per door per
device lands on the access log as a `timing` line
(`host/dark_army_daemon/access_log.py`, `MAX_TIMING_ENTRIES` 600, read
behind `?timing=1`). The phone stamps its own side of every trip
(`ios/BobPhone/LinkTiming.swift`, `RelayTransport.swift`,
`HomeTransport.swift`) and draws both under Profile → access log.
`docs/transport-contract.md`, *Every phone request is timed*, is the
contract.

## The path today

One away request, hop by hop, as the code stands on 2026-09-20:

1. **Phone seals and posts** — `ios/BobPhone/RelayTransport.swift`:
   `RelayChannel.request` serialises every request per mailbox through the
   static `inFlight` map, so a tapped action **waits behind the poll's
   legs**. `send` seals, POSTs to `…/api/box?ch=&dir=to-mac` (10 s
   timeout), then loops GETting `dir=to-phone&wait=1` (30 s timeout) until
   its deadline — 45 s by default, chosen to cover the connector's idle
   ceiling — sleeping **1 s** after any non-200 answer, 204 included.
2. **Mailbox** — `relay/api/box.js`: `POST` is `LPUSH + LTRIM 0 31 +
   EXPIRE 120`; `GET …&wait=1` is an `RPOP` loop ticking every `POLL_MS`
   (2000 ms) for `WAIT_MS` (20 000 ms), then 204; `RATE_PER_MINUTE` 240
   per channel per method. **Both pickups are quantised to that tick.**
3. **Mac picks up** — `host/dark_army_daemon/relay_client.py`:
   `_serve` is one asyncio task per channel on the daemon loop; inside
   `ACTIVE_WINDOW_SECONDS` (600) it long-polls with `&wait=1`, outside it
   it asks once and sleeps `IDLE_GAP_START` 5 s doubling to `IDLE_GAP_MAX`
   30 s. The blocking `urllib` call rides the connector's dedicated
   executor (`POLL_TIMEOUT_SECONDS` 30).
4. **Mac opens and runs** — `_handle_wire` runs inline on the loop: the
   frame bucket, the key re-read, `relay.open_frame` (base64 → AEAD →
   inflate), `relay.note_recv_ctr` (a `relay.json` write on every `action`
   frame, once per `RECV_CTR_PERSIST_SECONDS` 30 for reads), the write
   bucket; `_execute` is its own task: `ApiServer._remote_run` →
   `_sealed_run` → `_state_answer`, which builds the whole frame, digests
   it and, on a miss, serialises it again. Measured: 5 ms typical.
5. **Mac answers** — `_answer`: the per-device send lock,
   `relay.next_send_ctr` (a store write every `SEND_CTR_RESERVE` 256
   answers), `relay.seal_frame` (deflate + AEAD of the reply, on the
   loop), a POST `dir=to-phone` on the executor. The reply body is
   `{"re", "status", "content_type", "body", "timing"}` — the last key new
   here and ignored by every shipped phone.
6. **Phone's check-in is several trips, in series** —
   `ios/BobPhone/Client.swift`: the loop sleeps `noteAttempt()`'s 8 s away
   / 4 s home. Away, `pollViaRelay` does the `state` leg (`relayLegCap`
   45 s), then `pollUsageViaRelay` on **every** poll, then `fetchLog` every
   `logInterval` 30 s, then `fetchTerminalBytes` while the Terminal tab
   shows; then the home probe (`directProbe` 4 s). One away check-in is
   **two to four serialised mailbox round trips**, each paying the tick
   twice; a press queues behind whichever leg is in flight.
   `stateRequestBody` quotes the held digest only inside `fullStateFloor`
   60 s, so the whole picture is refetched at least once a minute.
7. **What a `state` reply weighs** — 146 KB plain for the bench's fleet
   (~80 KB for a typical real one), 4.7 KB deflated, 6.3 KB sealed;
   `unchanged` is 139 plain / 192 sealed. The `usage` reply has no digest
   and is refetched whole every poll.

**The home door** — `api_server.py`'s `_handle_lan_client` →
`_read_request` → `_lan_home`: `_home_open` (the one verifier) →
`_sealed_run(actions=LAN_ACTIONS)` → `_home_answer`. One HTTP round trip,
no mailbox, no tick: the Mac's three legs there (`open`, `run`, `seal`)
are all milliseconds, so the home door's latency is the Wi-Fi's and the
phone's 4 s cadence alone.

## Bottlenecks

Each with its evidence — a figure from the bench, or a line of code and
the arithmetic.

1. **The mailbox's 2 s tick, paid twice per trip.** Run 2: every trip's
   `mac_dwell` and `post_to_pickup` sit at 1.94–2.00 s (p50 to max) with
   6 ms of work behind them; `usage`'s p90 of 4.06 s is the second tick,
   bought by a 0.6 s run that crossed the boundary. `POLL_MS` 2000 in
   `relay/api/box.js`. **Floor: 0–4 s per request, ~2 s typical.** This is
   the largest single term in every away figure and it is a constant of
   the mailbox, not of the Mac or the phone.
2. **Two to four trips per check-in, in series.** `pollViaRelay` runs
   `state`, then `usage` every time, then `log` every 30 s, then the
   terminal leg — each a full mailbox round trip, each paying (1). With
   the tick, an ordinary away check-in is ≥ 4 s of waiting (two trips)
   and up to ~8–16 s at the 30 s mark, on an 8 s cadence. The Mac's work
   for all of them together is under 30 ms (run 1).
3. **A press waits behind the poll's legs.** `RelayChannel.inFlight`
   serialises per mailbox; a tap landing while `usage` is in flight waits
   for that leg's tick(s) before its own two. Arithmetic: worst case a
   press queued behind a fresh `state` + `usage` pair waits ~8 s before
   its own ~2–4 s. The queue card on file answers the *screen* side of
   this (the control is released at once); the wire side is still serial.
4. **The whole picture every minute.** `fullStateFloor` 60 s means the
   146 KB / 6.3 KB-sealed picture is fetched at least once a minute
   whatever changed — cheap on the wire (deflate), but a 5–11 ms run and
   a full JSON decode on the phone every minute, and (with the tick) the
   same 2–4 s wait as any trip, so it costs no *extra* latency, only work.
5. **`usage`'s cold run.** 0.96 s / 1.06 s max in both runs, 8 ms after:
   the first read of a usage window is a history query on the executor.
   One cold hit per daemon start per window, and it is the one Mac-side
   figure that ever crossed a tick boundary in the bench.
6. **Loop-side work in `_handle_wire` / `_answer`.** `open_frame`,
   `seal_frame` and `note_recv_ctr`'s synchronous `relay.json` write run
   on the daemon loop. Measured: `open` below 1 ms; `seal` of a 6 KB
   answer is inside the 60 ms `total`; the store write happens once per
   30 s for reads and per `action`. **Not a bottleneck at this scale** —
   named because it is the one place a bigger fleet would show first.
7. **The phone's fixed 1 s sleep on a 204 and the 30 s reply-poll
   timeout.** `RelayTransport.swift`'s poll loop sleeps 1 s after any
   non-200; with the mailbox holding for 20 s a 204 is rare inside the
   active window, so this costs little — but after the Mac's 20 s hold
   ends and before the phone re-asks, the reply can sit a further second.
8. **Vercel cold starts and the public path** — invisible to the bench;
   only the field figures can size them. Named so nobody reads the
   tables above as the whole away story.

## Already filed — not re-proposed

- the *mailbox poll fast while phone away* plan — the
  connector's 5→10→30 s idle ladder and a push-side active window: the
  first away request after a quiet spell waiting up to `IDLE_GAP_MAX`.
  Covers the idle half of bottleneck 1's cousin (pickup outside the
  active window); nothing here re-proposes it.
- the *phone action queue and ranked inbox* plan — presses
  written to an ordered queue so acting from away never blocks the
  screen. Covers the screen side of bottleneck 3.
- the *away mode latency and prepare hang* plan —
  home-address probing before the relay, `directProbe`, relay-first
  writes. Covers the route choice; the home door's own figures above are
  why choosing it matters.
- `TODO.md`, *The phone: usable with and without the Mac in reach* —
  `relay/api/push.js` is deployed out of band; every relay-side change
  below is a **proposal** for that deployment, not a change here.
- the *websocket away link mvp* plan — a WebSocket fast lane
  beside the mailbox (`relay-ws/`, `relay_ws.py`, `RelaySocket.swift`),
  measured the same way; its figures page is
  `docs/2026-09-21-relay-socket-mvp-figures.md`. The one proposal here
  that removes the tick rather than shortening it.

## Ranked proposals

Ordered by expected saving ÷ effort. Saving is per away request unless
stated; effort is S (an afternoon, one file), M (a day, two or three
files and their tests), L (several days or a deployment outside this
repo). Candidates (a)–(g) from the plan's context are each kept or
dropped with the reason; no card is filed by this document.

| # | Proposal | Expected saving | Effort | Files | Risk |
|---|---|---|---|---|---|
| 1 | Merge `usage` into the `state` reply envelope on the sealed doors: `_state_answer` returns `usage` beside the state (behind a marker), `pollViaRelay` stops making the second trip. Candidate (b). | One full mailbox trip per away check-in: **~2–4 s** of the ≥ 4 s every 8 s, and half the frames against the 60/min bucket. | M | `host/dark_army_daemon/api_server.py`, `ios/BobPhone/Client.swift`, `host/dark_army_daemon/daemon_board.py` (marker) | Low: a read beside a read, no action tuple, the digest unchanged (usage rides the envelope like `timing`). |
| 2 | Shorten `POLL_MS` from 2000 to 500 inside the active window's held GET (`relay/api/box.js`); keep 2000 for the idle bare GET. Candidate (a). | The tick paid twice: **~1.5 s typical, up to 3 s worst** per trip. | S code, L to deploy (out of band) | `relay/api/box.js`, `host/tests/test_relay_box.py` | Upstash command count ×4 during the active window (~52 → ~200 per held poll); cost, not correctness. The idle ladder card already cuts the idle side. |
| 3 | Fold the `log` leg into the `state` envelope the same way as #1, `since`-cursored, so the 30 s check-in is one trip too. Candidate (b), second half. | One trip every 30 s: **~2–4 s** on every fourth check-in. | S once #1 exists | `host/dark_army_daemon/api_server.py`, `ios/BobPhone/Client.swift` | Low; the diary is already bounded and cursor-read. |
| 4 | Let a press jump the queue: `RelayChannel.inFlight` becomes a two-lane serialiser (reads wait for reads; an `action` waits only for an `action`), since the Mac's `_execute` is already one task per frame and `recvCtr` only needs the *phone's* pops ordered per lane by `re`. Candidate (c). | A press stops paying the poll's tick(s): **~2–8 s** off a tap's latency while a check-in is in flight. | M | `ios/BobPhone/RelayTransport.swift`, `ios/BobPhoneTests/SealedEnvelopeTests.swift` | Medium: two concurrent pops on one `to-phone` list must hand each other the frame they took (the "destroys each other's answers" note in the file); needs the `re` match before the counter is advanced. |
| 5 | Warm `_usage_report_for` at daemon start and on the executor after each window change, so the first away `usage` read is 8 ms, not 1 s. Bottleneck 5. | **~1 s once per window per daemon start**, and one fewer tick crossing (the p90 4 s row). | S | `host/dark_army_daemon/api_server.py`, `host/dark_army_daemon/daemon.py` | Low. |
| 6 | Raise `fullStateFloor` from 60 s to 300 s and let the `unchanged` answer carry the *sections that changed* by digest per section, so the minute-mark refetch becomes a delta. Candidate (d). | Work, not latency: ~5–11 ms of Mac run and a 146 KB decode on the phone per minute; **0 s** off any trip with the tick in place. | L | `host/dark_army_daemon/api_server.py`, `ios/BobPhone/Client.swift`, `ios/BobPhone/Models.swift` | Medium: a per-section delta on a sealed body is the `?sections=changed` decoder story again, on the phone this time. **Dropped from the recommended set**: no latency to win under (1)–(3). |
| 7 | Move `note_recv_ctr`'s `action` write and `seal_frame` off the loop onto the connector's executor. Candidate (e). | **< 1 ms** at today's scale (bench: `open` unmeasurable, `seal` inside 60 ms totals). | S | `host/dark_army_daemon/relay_client.py` | Low. **Dropped**: the numbers do not justify a change to the persist-before-execute ordering. |
| 8 | Replace the phone's fixed 1 s sleep after a 204 with an immediate re-ask while inside its own deadline (the mailbox holds for 20 s anyway). Candidate (f). | **≤ 1 s** on the rare 204 inside the active window. | S | `ios/BobPhone/RelayTransport.swift` | Low, but it hammers a refusing mailbox (the sleep's stated reason) — keep the sleep for 4xx/5xx, drop it for 204 only. **Kept, last**: small and cheap. |
| — | Vercel cold starts, candidate (g). | Unknown until the field figures land. | — | `relay/vercel.json` | **Not ranked**: nothing in-process can size it; see *Field figures*. |

Recommended order: 1, 3 and 5 (Mac and phone, this repo, no deployment)
first — together they take an ordinary away check-in from ≥ 4 s of tick
waiting to ~2 s and remove the cold-usage outlier; then 2 (a mailbox
deploy) which halves what is left; then 4 once the queue card has landed,
because a press that no longer blocks the screen still deserves to land
sooner.

## Field figures

The bench measures the Mac and the fake mailbox's clock. The true away
figures — Vercel's cold starts, the public internet, a phone on cellular —
come from a day of real use, and both ends now write them down. Steps:

1. Use the phone away from home Wi-Fi for a day as usual: open it a few
   times, pull to refresh, answer one thing.
2. On the phone open **Profile → access log**. The **LINK TIMING** section
   at the top lists one line per route and kind
   (`away · state · … trips · … typical · … slow`) and, under it, the
   newest trips one per row with their legs.
3. Copy the `away` lines into the *phone side* table below.
4. On the Mac run
   `curl -s -H "X-Bob-Token: $(cat ~/.bob-companion/api-token)" "http://127.0.0.1:19874/api/access-log?timing=1&limit=50"`.
   Every line with `"kind": "timing"` and `"door": "relay"` is one
   ten-minute summary; copy the newest three sentences into the *Mac
   side* table below.
5. Compare: for the same ten minutes, the phone's typical `pickup` should
   sit within about two seconds of the Mac's "picked up in … typical".
   A phone figure far above the Mac's is the public path and the cold
   start — the part only this table can show. One thing to read the Mac's
   lines by: a window closes only on the **next** request after its ten
   minutes are up (`LinkTimer.add`; nothing flushes a quiet window), so a
   phone that went quiet files its last window on its return and that
   line's window is longer than ten minutes — match windows by the trips
   they contain, not by the clock alone.

**Phone side — paste here**

| route · kind | trips | typical (s) | slow (s) | newest row (legs) |
|---|---|---|---|---|
| | | | | |
| | | | | |

**Mac side — paste here**

| ten-minute window (ts) | sentence |
|---|---|
| | |
| | |
| | |

## What this did not measure

- **Vercel cold starts.** The fake mailbox is a warm process on loopback;
  the real `api/box.js` function may be cold on the first GET after a
  quiet spell. Only the field figures can show it.
- **Cellular versus Wi-Fi, and the public internet at all.** Every byte
  in the bench travelled over loopback.
- **iOS backgrounding.** The bench's "phone" is a Python coroutine that
  never sleeps; a real phone's poll is suspended in the background and
  resumes on `.active`, which is its own latency and not the link's.
- **The idle ladder.** Both runs kept the channel inside its active
  window; the first request after ten quiet minutes waits up to
  `IDLE_GAP_MAX` 30 s, which the mailbox-poll card on file addresses.
- **A fleet bigger than 20 rows and 40 cards**, and a `state` reply near
  `RELAY_FRAME_MAX_BYTES` — the run cost grows with the picture, and the
  bench's `--sessions` / `--cards` are the dials to find where.
