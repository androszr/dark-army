# Codex parity fixture provenance

`native-root.jsonl` and `native-child.jsonl` are minimized from actual native
Codex CLI **0.154.0** journals observed on 2026-09-12 during this implementation.
Capture: root session started 16:51:41Z; child 16:52:21Z; async ask and immediate
`{"accepted":true}` acknowledgement 16:52:57Z. The measured spawn is a
`function_call` named `spawn_agent` with `namespace: collaboration`; its output
contains only `task_name`, joined to child `source.subagent.thread_spawn.agent_path`.
The child carries role `bc-implementer` and nickname `Ohm` separately.

Sanitization retains only session metadata, first task start, that spawn/result
and that async call/ack. UUIDs, call IDs, project path, task instruction and
question/option text are replaced; unrelated metadata and private content are
omitted. Native answer and cancel were NOT OBSERVED at capture time; later
lifecycle transitions in the Python builders are explicitly synthetic tests,
not claims about the installed wire. Synchronous shapes have prior repository
fixture coverage; namespaced aliases and malformed cases are synthetic too.
No full journals, secrets or private prompt corpus are included.

`native-relative-followup.jsonl` is a second native 0.154.0 capture from this
root journal at 17:40:16.602–17:40:17.378Z (source call
`call_yEXeR7D3KVvpv70vcMypD5Jo`). The tool accepts the relative target
`security_review` and returns canonical `task_name: /root/security_review`.
The fixture substitutes `implement` consistently, replaces the private message
and call ID, and retains only the call/result shapes and timestamps. Child
completion, failure and malformed-tail variants remain explicitly synthetic.

`native-review-child.jsonl`: sanitized metadata shape observed in the local
2026-09-19 native review child (Androll): `source.subagent="review"`, a
**top-level** `parent_thread_id`, and a copied `session_id` distinct from the
journal's own `id`. IDs, project paths and review text are synthetic; the five
findings preserve the native review result schema, not private source content.
