# The socket beside the mailbox — figures for the day-away test

- **Date:** 2026-09-21 (bench figures taken 22 Sep 2026)
- **Plan:** *websocket away link mvp*
- **Baseline:** `docs/2026-09-20-relay-latency-audit.md`
- **Area:** pocket
- **What this is:** the code-path figures for both away lanes, measured on
  this Mac before any deployment; the steps for the field figures; two
  empty tables to paste them into; and the three questions the decision
  rests on. **No decision is written here** — the person writes it, after
  a day away.

## The code path, on this Mac

Both runs: `cd host && .venv/bin/python ../tools/relay_latency_bench.py
--sessions 20 --cards 40 --rounds 5` and the same with `--socket`. Python
3.12, 22 Sep 2026, the same fleet the audit used (20 sessions, 40 cards,
~146 KB plain picture). The mailbox run keeps the fake mailbox's 0.05 s
tick, so it shows the Mac's own work; the real mailbox adds 0–4 s of tick
per trip (the audit's run 2). The socket run has no tick to add: what it
shows is what the lane costs, plus the public path on the day.

**Mailbox (`relay_client`, fake mailbox, tick 0.05 s, hold 1.0 s)**

| trip | leg | p50 | p90 | max |
|---|---|---|---|---|
| state | post_to_pickup | 0.042 | 0.063 | 0.063 |
| state | mac_dwell | 0.023 | 0.047 | 0.047 |
| state | mac_hold | 0.063 | 0.132 | 0.132 |
| state | mac_open | 0.000 | 0.002 | 0.002 |
| state | mac_run | 0.008 | 0.018 | 0.018 |
| state | total | 0.069 | 0.124 | 0.124 |
| state_unchanged | post_to_pickup | 0.045 | 0.082 | 0.082 |
| state_unchanged | mac_dwell | 0.033 | 0.065 | 0.065 |
| state_unchanged | mac_hold | 0.080 | 0.142 | 0.142 |
| state_unchanged | mac_open | 0.000 | 0.000 | 0.000 |
| state_unchanged | mac_run | 0.006 | 0.008 | 0.008 |
| state_unchanged | total | 0.073 | 0.117 | 0.117 |
| usage | post_to_pickup | 0.033 | 0.039 | 0.039 |
| usage | mac_dwell | 0.024 | 0.036 | 0.036 |
| usage | mac_hold | 0.070 | 0.076 | 0.076 |
| usage | mac_open | 0.000 | 0.000 | 0.000 |
| usage | mac_run | 0.025 | 1.766 | 1.766 |
| usage | total | 0.069 | 1.838 | 1.838 |
| action | post_to_pickup | 0.031 | 0.069 | 0.069 |
| action | mac_dwell | 0.025 | 0.064 | 0.064 |
| action | mac_hold | 0.063 | 0.767 | 0.767 |
| action | mac_open | 0.000 | 0.000 | 0.000 |
| action | mac_run | 0.000 | 0.000 | 0.000 |
| action | total | 0.069 | 0.130 | 0.130 |

| reply | plain bytes | deflated | sealed (base64) |
|---|---|---|---|
| state | 146129 | 4782 | 6416 |
| state_unchanged | 137 | 113 | 188 |
| usage | 1495 | 542 | 760 |
| action | 50 | 45 | 100 |

**Socket (`relay_ws`, fake socket relay on loopback, push coalescing 0.5 s)**

| trip | leg | p50 | p90 | max |
|---|---|---|---|---|
| state | post_to_pickup | 0.000 | 0.000 | 0.000 |
| state | mac_dwell | 0.000 | 0.000 | 0.000 |
| state | mac_hold | 0.000 | 0.000 | 0.000 |
| state | mac_open | 0.000 | 0.000 | 0.000 |
| state | mac_run | 0.009 | 0.012 | 0.012 |
| state | total | 0.027 | 0.035 | 0.035 |
| state_unchanged | post_to_pickup | 0.000 | 0.000 | 0.000 |
| state_unchanged | mac_dwell | 0.000 | 0.000 | 0.000 |
| state_unchanged | mac_hold | 0.000 | 0.000 | 0.000 |
| state_unchanged | mac_open | 0.000 | 0.003 | 0.003 |
| state_unchanged | mac_run | 0.007 | 0.010 | 0.010 |
| state_unchanged | total | 0.016 | 0.021 | 0.021 |
| usage | post_to_pickup | 0.000 | 0.000 | 0.000 |
| usage | mac_dwell | 0.000 | 0.000 | 0.000 |
| usage | mac_hold | 0.000 | 0.000 | 0.000 |
| usage | mac_open | 0.000 | 0.000 | 0.000 |
| usage | mac_run | 0.010 | 1.429 | 1.429 |
| usage | total | 0.018 | 1.434 | 1.434 |
| action | post_to_pickup | 0.000 | 0.000 | 0.000 |
| action | mac_dwell | 0.000 | 0.000 | 0.000 |
| action | mac_hold | 0.000 | 0.000 | 0.000 |
| action | mac_open | 0.000 | 0.001 | 0.001 |
| action | mac_run | 0.000 | 0.001 | 0.001 |
| action | total | 0.007 | 0.020 | 0.020 |
| push | post_to_pickup | 0.000 | 0.000 | 0.000 |
| push | mac_dwell | 0.000 | 0.000 | 0.000 |
| push | mac_hold | 0.000 | 0.000 | 0.000 |
| push | mac_open | 0.000 | 0.000 | 0.000 |
| push | mac_run | 0.000 | 0.000 | 0.000 |
| push | total | 0.543 | 0.546 | 0.546 |

| reply | plain bytes | deflated | sealed (base64) |
|---|---|---|---|
| state | 146127 | 4779 | 6412 |
| state_unchanged | 139 | 115 | 192 |
| usage | 1493 | 540 | 760 |
| action | 50 | 45 | 100 |
| push | 147723 | 5320 | 7132 |

**Reading the two tables together.** On the Mac's own hand a `state` trip
down the socket is 27 ms whole against 69 ms through the fake mailbox —
and against 2.0 s through the real one (the audit's run 2), because the
socket has no tick to pay on either leg. A press (`action`) is 7 ms. The
`push` trip is the coalescing interval almost entirely: the Mac waits
`WS_PUSH_MIN_INTERVAL` (0.5 s) after the first change so a burst of hook
events is one push, then builds and seals the picture in ~10 ms; the phone
therefore sees a change about half a second after it happened on the Mac,
plus the public path. The socket's `usage` p90 shows the same cold
`_usage_report_for` outlier the audit recorded — the Mac's, not the lane's.
The mailbox's legs (`post_to_pickup`, `mac_dwell`, `mac_hold`) read zero on
the socket because there is no mailbox; the `push` row carries no Mac echo
(a push is unasked), so its `mac_run` is left at zero here and the Mac's
access-log `timing` line under door `ws` has it.

**Two things to read the field figures by.** First, while the phone lives
on the socket its frames do **not** stamp the mailbox connector's liveness
(`relay.note_recv_ctr(..., liveness=False)`), so the mailbox connector idles
down its ladder (5 s → 30 s between polls) — that is the Upstash saving the
socket is meant to show, and its cost is that the first mailbox trip after
a socket drop can wait up to one idle gap (30 s), inside the phone's 45 s
deadline. Second, the phone's socket deadline is 10 s: a request that gets
no answer down the line in that time goes the mailbox way as the *same*
request, so a `ws · state` line and an `away · state` line can both hold a
trip that started as one press.

## Field figures — the day away

The bench measures the Mac and a fake relay on loopback. The true away
figures — Fly.io's public path, a phone on cellular — come from a day of
real use, and both ends write them down. Steps (the plan's last criterion
has the full twelve, deploy and pairing included):

1. Deploy `relay-ws/` (`cd relay-ws && fly launch --no-deploy && fly
   deploy`), set the `wss://…` address under **⋯ → Devices → Socket
   address…**, tick **Socket link** (Away access on), and pair the phone
   again at home.
2. Use the phone away from home Wi-Fi for a day as usual: open it a few
   times, answer one thing, press one thing.
3. On the phone open **Profile → access log**. **LINK TIMING** lists one
   line per route and kind: `away · state · …` for the mailbox and
   `ws · state · …`, `ws · action · …`, `ws · push · …` for the socket, in
   that order. Copy every `away` and `ws` line into the *phone side* table.
4. On the Mac run
   `curl -s -H "X-Bob-Token: $(cat ~/.bob-companion/api-token)" "http://127.0.0.1:19874/api/access-log?timing=1&limit=50"`.
   Every line with `"kind": "timing"` is one ten-minute summary; those with
   `"door": "relay"` begin `relay:` and those with `"door": "ws"` begin
   `socket:`. Copy the newest three of each into the *Mac side* table.
5. Match windows by the trips they contain, not by the clock alone: a
   window closes only on the next request after its ten minutes are up
   (`LinkTimer.add`), so a phone that went quiet files its last window on
   its return.

**Phone side — paste here** (one row per `route · kind` line; `away` rows
and `ws` rows for the same kind beside each other)

| route · kind | trips | typical (s) | slow (s) | newest row (legs, or age for a push) |
|---|---|---|---|---|
| away · state | | | | |
| ws · state | | | | |
| away · action | | | | |
| ws · action | | | | |
| ws · push | | | | |

**Mac side — paste here** (`relay:` and `socket:` sentences for the same
ten minutes beside each other)

| ten-minute window (ts) | door | sentence |
|---|---|---|
| | relay | |
| | socket | |
| | relay | |
| | socket | |
| | relay | |
| | socket | |

## Decide

Three lines, answered from the tables above and nothing else; the person
writes the answer and the decision under them.

1. **Does a press land in under a second?** `ws · action` typical, phone
   side, against `away · action` typical.
2. **Does the picture arrive within two seconds of a change?** `ws · push`
   typical (the age on arrival), against the `away · state` cadence of one
   check-in every 8 s plus its trip.
3. **Are the mailbox's commands per hour down while the socket is up?** The
   `relay:` sentences' request counts for a window the socket carried,
   against a window before the socket was switched on (or the audit's
   ≈ 6 commands a minute idle).

Answer: _(not yet written)_

Decision: _(not yet written)_
