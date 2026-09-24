# Codex session parity audit — 12 September 2026

## Verdict

**Parity with Claude is not established; concrete gaps are present in the installed build.** Launch guards and the restricted board connection have substantial passing coverage. The interview instructions, role labels, persisted stage history and retained completion report do not provide the same experience. No implementation, installation, session restart or release was performed during this audit.

Requested outcome: sessions started by Dark Army should conduct the appropriate interview and make progress visible throughout planning, implementation, verification and audit. This document records observations; the *codex session parity* plan is the implementation handoff.

## Measurement boundary

- Checkout: `main`, HEAD `aec8b17`, extensive pre-existing tracked and untracked changes. Audit concerns the current working tree, not just HEAD. Existing changes were preserved.
- Installed manifest: `main+48@aec8b17-dirty`, built `2026-09-12T13:25:21Z`, extension artifact `0.1.16`.
- Native CLIs measured: `codex-cli 0.154.0`, Claude Code `2.1.269`.
- Installed `codex_rollouts.py`, `daemon.py`, `daemon_board.py`, `dispatch.py` and `channel_server.py` were byte-equal to checkout sources. Installed notify handler, channel helper and close-out helper were also equal.
- `codex mcp get bob-companion-board`: enabled, stdio, installed channel helper with `--host=codex`. The current session exposes add, attach and close card tools.
- Live `/api/state` contained this native Codex root and its actual planner helper. SSE delivered an initial frame in 0.037 s. This measures connectivity, not event-to-screen latency.
- Running editor extensions, obtained from authenticated `ping`: ports 61905, 61888 and 51333 reported `0.1.11`; port 51235 reported `0.1.15`. These are running versions, not the bundled `0.1.16`. The inspected ports were not attributed to a specific launch target, so this is a conditional launch/close risk.
- UI pixels, a fresh Refine/Start button press, real interview answering, and a full newly launched card lifecycle were not exercised. The running audit session and helper prove observation only, not launch parity.

## Findings

| Priority | Finding and evidence | User-visible consequence |
|---|---|---|
| FIX | `.agents/skills/ship/SKILL.md` Plan says only to ask design-changing questions. It omits Claude's bounded interview procedure, loading `templates/questions.md`, and forwarding an Answers map. The question template still names Claude's `AskUserQuestion`. The shipped agent-pack template has yet another, more explicit Codex fallback. | A Codex Refine can reach planning without the same requirements assessment. The interview is not guaranteed by the local contract. |
| FIX | Real child journal metadata records role `bc-planner` and nickname `Newton`; the live API publishes `subagent_type: Newton`. `load_recent` prefers nickname over role; spawn parsing prefers task name over agent type. | The expected plan/implement/verify role cannot reliably match the helper displayed on the card. |
| FIX | `_record_card_stages` reads only `_session_states[session_id].subagents_seen`. `_refresh_codex_records` maintains roster records without creating that hook history. | Correcting the helper label alone does not record Codex stages in `agent_trail`/`crew_trail`. Completed short stages need observed history, not just a current-helper list. |
| FIX | A two-journal replay marked both parent and child turns completed, and explicitly completed the child in the parent's collaboration event. `load_recent` nevertheless rejoined the retained child as a waiting helper. The parent had `turn_active=False`, `activity=waiting`, but nonempty `stats.agents`; `_reconciled_categories` treats that as running. | A completed run can continue to appear active while a completed helper's journal remains in the discovery window. Active helpers must be separate from retained observed history. |
| FIX | `parse_rollout` never populates `SessionStats.last_report`. A temporary replay containing a `## Work done` assistant message produced nonempty `last_text` but empty `last_report`. Daemon enrichment publishes `stats.last_report` directly. | The durable completion report used by the existing Details surface is missing for Codex; later narration can replace the visible latest message. |
| FIX, conditional on tool availability | A temporary replay of the currently available `request_user_input_async` shape (`questions` with `title` and string options) produced zero questions and activity `working`. The supported synchronous shape produced one question and `waiting`. | If interview uses the asynchronous tool, Dark Army does not recognize the outstanding question. A real async question/answer lifecycle remains to be sampled: the immediate tool result is an acknowledgement, not the person's answer. |
| FIX | Codex skill Handoff still claims its board MCP has no close-card tool; its Plan section says pause for approval while its own modes and repository instructions say attach/file and stop. | The agent can leave a finished implementation card open unnecessarily or ask for approval at the wrong point. |
| WARN | Three running editor connections are `0.1.11`; the private Codex refinement-close protocol requires `0.1.12+`. | A correctly attached plan may leave its terminal open on an older target window. Correct behavior is a clear refusal, not weaker ownership checks. |

The journal names observed in the recent native sessions are bare `exec`, `spawn_agent`, `send_message`, etc. Namespaced aliases are a compatibility consideration, not a demonstrated current failure. A slash prompt failing to activate the skill was **not** reproduced; Refine and Start must be tested through their actual argv before assigning that diagnosis.

## Behaviors already covered, and intentional limits

- Dispatch/refine tests exercise executable resolution, model and prompt guards, the argument separator, card linking and refusal behavior.
- Synchronous Codex questions already parse into the shared question shape. Aborted/new turns and matching results have regression coverage.
- Codex add/attach/close card tools are intentionally restricted and attributed to a proven session. A board close declaration is distinct from closing a terminal.
- Replies, automatic permission answers and generic typing are intentionally unavailable for Codex. The current contract directs a person to the original session. This audit does not authorize weakening process ownership to imitate Claude controls.
- Codex has no hook-derived origin line; that is a documented limitation, not proof the launch failed.
- Roster refresh is gated by `ROSTER_REFRESH_INTERVAL = 2.0`. The parser's passive file source justifies refreshes; latency must be measured separately from successful SSE attachment.
- Existing live helper count is not a percentage completed. Expected workflow, observed role history, currently active roles and actual completion should remain distinct.

## Verification actually run

From repository root:

```sh
host/.venv/bin/pytest -q host/tests/test_codex_rollouts.py host/tests/test_codex_spenders.py host/tests/test_dispatch.py host/tests/test_board_refine.py host/tests/test_channel.py host/tests/test_ship_close_out.py
# 699 passed in 73.23s

host/.venv/bin/pytest -q host/tests/test_crew.py host/tests/test_multi_question.py host/tests/test_work_report_surface.py host/tests/test_agent_pack_render.py host/tests/test_agent_pack_contract.py host/tests/test_close_terminal_button.py
# 207 passed in 1.92s
```

**906 tests passed.** Four additional parser experiments used only temporary files, never synthetic events against the running fleet. They exposed missing behavior rather than fixing it. A green existing suite does not establish the requested end-to-end parity.

## Code graph evidence

Bound repository: `bob-companion` at this checkout. The initial 10 September index was stale. A rebuild with `GITNEXUS_MAX_FILE_SIZE=2048` included the large `daemon.py`; an incremental FTS failure and unsuccessful repair were resolved by a full `--force --index-only` rebuild. Final graph: 34,329 nodes, 121,616 edges, 613 discovered flows. Flow discovery reported truncation and cross-language resolution limits, so absent processes were not treated as evidence of no impact.

Upstream impact queries after that rebuild reported `LOW`: `parse_rollout` 14 nodes, three direct callers (`_load_usage_limits`, `load_recent`, `refinement_close_observation`); `load_recent` 15 nodes, three direct callers (`_codex_record_for_confirm`, `_refresh_codex_records`, `_settle_codex_stop`); `_record_card_stages` one caller (`_reconcile_board`). The first two reach close/stop proof and observations, so parser fixes still require their safety regressions despite the graph's low score. No source symbols were edited and no commit was made.

No full Python suite, Swift build/test, packaging build or phone test was run in this documentation-only audit. They are implementation gates in the plan. No full daemon-probe or integration-pass verdict is claimed.

## Required release evidence

Use a disposable, enrolled project and paired Claude/Codex cards with the same brief. Capture timestamps and snapshots for actual Refine and Start launches, both the hosted terminal and editor routes. Cover an underspecified idea requiring an interview, a fully specified idea needing no redundant questions, an existing approved plan, a brief helper between two refreshes, a retry, an aborted turn, a blocked question, failed verification, a successful plan attachment and a successful final card declaration.

Verify the existing panel and phone views show the same observed stage and honest waiting state, with no fabricated completion. Measure event-to-snapshot and snapshot-to-visible-update latency separately. Check that reports survive later housekeeping until the next user prompt. Record the actual loaded editor version and demonstrate clear refusal on an older connection. Keep failures and unavailable controls explicit.

Acceptance requires evidence from a fresh installed build and newly launched sessions; source tests or an already running session alone cannot satisfy it.

## Planning handoff

Plan preflight passed all 14 checks. Card `a961b4af7da24d2e97652c4e67930cfa` was created once and its plan attached in Backlog. During this actual handoff the first post-create attachment returned “Dark Army could not tell which session asked”; after a read-only snapshot check, another call of the same scoped tool with the same path succeeded. No identity was substituted and no generic board write was used. This is an observed transient attribution refusal, with cause unresolved, not proof of permanent MCP failure; include it in attach/refusal regression and live validation. Eight documentation-size checks also passed after the TODO update.
