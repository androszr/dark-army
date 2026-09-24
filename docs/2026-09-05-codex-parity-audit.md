# Codex parity audit — 5 September 2026

Scope: current Bob Companion source, macOS panel and iPhone home/away paths.
This is an audit and proposal, not an installed-runtime certification. No
feature code was changed or real agent input sent. Existing work by other
sessions was preserved; HEAD advanced from 98246e7 to cac52ac during the audit.

## Findings and proposed fixes

| Function | Claude / Grok baseline | Codex today | Proposed fix |
|---|---|---|---|
| Automatic context compaction | Both have an existing terminal-input implementation. | **Bug:** a reachable Codex PID can pass the automatic path even with `can_type=false` and `can_stop=false`; warning is marked handled. | First priority: reject Codex at both decision and send time; retain the context alert. Later use an explicitly supported managed-session operation. |
| Interrupted-turn status | Hook-based lifecycle monitoring exists for both. | **Bug:** `turn_aborted` is ignored; interrupted work can remain busy until another lifecycle event or eviction. | Handle interruption by turn identity, clear ended-turn tools/questions, and test next-turn recovery. |
| Questions and short answer choices | Claude has question hooks and summary/action parsing; Grok has chat parsing and recent question fixes in the working tree. | Structured question calls are ordinary running tools; question fields remain empty. Summary/action markers are not extracted. | Parse Codex question payloads and existing markers, clear stale questions on matching answers/turn end, and show questions read-only until there is a response transport. |
| Subagent descriptions | Provider-specific subagent tracking exists. | Function calls carry `arguments`, but `_tool_input` reads only `input`; spawn descriptions are lost and a call ID can become a placeholder agent. | Decode both formats, reconcile tool call IDs with actual child IDs, and retain authoritative child-journal metadata. |
| Reply from Mac or phone | Claude requires its channel; Grok requires a reachable resident leader session. | Explicitly unavailable (`channel=false`). Dictation into a session inherits the same restriction. | Add a separate managed Codex transport; expose replies only when connected to the exact thread. |
| Permission answers | Claude supports channel relay and a bounded hook broker. Grok is not equivalent: its permission hooks do not use the Claude broker. | No approval requests are surfaced or answered. | Managed Codex transport must preserve request/thread/turn identity, native verdicts, and resolution events. Never treat ordinary terminal text as an approval protocol. |
| Hide a Codex row on the phone | Desktop Codex Hide already exists. Other providers use different lifecycle actions. | **Mobile gap:** no `canHide` decode, Hide UI, or home/away action allowlisting. | Reuse revision-scoped daemon Hide through explicitly selected home and away verbs; do not stop a process or delete a journal. |
| Stop, Jump, Close | Existing provider-specific process controls. | Available only for selected provable CLI identities; exact resume allows Stop and stopped-terminal Close. App/IDE rows remain narrower. | Preserve identity checks. Explain unavailable actions; do not broaden control using cwd or title decoration alone. Jump is a Mac action. |
| File a board idea / attach its plan | Session-attributed board tools exist for Claude/Grok. | Tools exist in Codex too, but **both failed in this audit session** with “Bob could not tell which session asked.” | Add an explicit, verified thread/session binding for Codex board requests and actionable attribution diagnostics; keep ambiguous/child requests refused. Exact cause of this session's attribution failure was not isolated. |
| Finish a board card / report manual checks | Claude/Grok host tools include close and manual-check reporting. | Board tools expose add and attach only. Human board moves remain available. | Separate follow-on: stronger session attribution and a deliberately expanded scoped tool contract. No arbitrary card ID or automatic launch authority. |
| Historical usage and costs | Desktop history ingestion and filters support Claude/Grok. | Live account bars and context metrics exist, but Codex historical ingestion/filtering is missing; the report currently treats non-Grok providers as Claude. | Add idempotent Codex turn ingestion and a true provider dimension, preserving unknown cost as unknown. The phone has usage bars, not the desktop history screen. |
| Ask about a card | Direct route is Claude-channel-only; fallback starts Claude. | Codex and Grok cards both fall back to a Claude helper. The phone has no Ask for any provider. | Follow the card's assistant where supported; label any fallback. Treat adding phone Ask as a separate cross-provider feature. |
| Prepare with attachments | Claude helper supports attached files. | Codex and Grok helpers explicitly refuse photos; text-only Prepare already follows the assistant. | Separate work: provider-native image/file inputs and scoped readable files. Keep the current clear refusal until supported. |

## Evidence

- `host/bob_companion_daemon/daemon.py:5859` and `:6021`: auto-compact decision
  and send paths; a mocked reachable Codex row queued and sent
  `\x15/compact`, despite false typing/stopping flags. The mock recorded
  `handled="compact"`. No actual terminal method ran.
- `host/bob_companion_daemon/codex_rollouts.py:191`: only task completion
  closes `turn_open`; three local journals whose last lifecycle event was
  `turn_aborted` all parsed as `working`. Six abort events were present in
  bounded tails of 83 local journals. Only aggregate counts/schema were output.
- `codex_rollouts.py:79`, `:346`, `:352`: `arguments` is not read, assistant
  prose bypasses summary/action extraction, and questions are not recognized.
  Isolated fixtures reproduced empty question, summary, and action fields and
  a spawn placeholder with an empty description. Real local spawn call shapes
  contain `arguments`; no real question call was found in the sampled tails,
  so the question finding is source/fixture evidence, not a live dialog test.
- `daemon.py:4221`, `:5629`, `:5646`: explicit reply, typing, and close limits.
- `ios/BobPhone/Models.swift:193`, `AgentDetailView.swift:242`;
  `panel/Sources/BobPanel/Triage.swift:175`;
  `host/bob_companion_daemon/api_server.py:1549`: phone/desktop Hide gap.
- `host/bob_companion_daemon/channel_server.py:421`: Codex add/attach tools
  already exist; documentation saying Codex has no board tools is stale.
- `host/bob_companion_daemon/history.py:914`,
  `panel/Sources/BobPanel/HistoryView.swift:85`: two-provider history.
- `host/bob_companion_daemon/daemon_board.py:2039`, `:2081`, `:2238`:
  Ask's Claude routing and Prepare's explicit non-Claude attachment refusal.

## Verification and integration direction

`host/.venv/bin/pytest -q host/tests/test_codex_rollouts.py
host/tests/test_channel.py host/tests/test_phone_writes.py
host/tests/test_autocompact.py` passed **249 tests**. The new failure scenarios
above were separate temporary/mocked probes, not additions to that suite.
No desktop window, physical phone, permission dialog, or managed Codex
connection was exercised. No full build, installation, or release was run.
The exposed board MCP tools were exercised: attach and then add both returned
the attribution refusal above. No board card was created, and the human-only
HTTP board route was not used as a workaround.

GitNexus was bound to `bob-companion`. Its MCP runtime could not read storage
version 43 with runtime 42; the CLI fallback worked and refreshed the index
after the concurrent commit. Impact reported LOW for `parse_rollout` (two
direct production callers: `load_recent` and `_load_usage_limits`) and
`_decide_auto_compacts` (direct caller `_enrich_agent_stubs`, then
`detailed_snapshot`). The analyzer warned of bounded/incomplete process
coverage: these results are advisory, not proof that all consumers were found.

Official [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)
describes turn start/steering/interruption and structured question/approval
requests with resolution notifications. This is a candidate for **Bob-managed
sessions**. The documentation does not establish that Bob can attach to and
control any arbitrary already-running Codex app/IDE session; that remains a
separate feasibility question.

Initial implementation handoff:
the *codex desktop mobile parity* plan. Start with monitoring and
capability correctness; the broader integrations above need separate plans.
