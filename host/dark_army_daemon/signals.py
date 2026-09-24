"""Per-agent health signals: the numbers turned into something to act on.

The panel and the menu already show *what* an agent is — model, idle time, token
counts, context fill. None of that answers the only four questions a person
watching six parallel agents actually has: is it stuck, is it burning money, is
it about to blow context, has it gone off the rails. A signal is one of those
questions answered, in a sentence, with the numbers in it.

Everything here is pure. The rules take one already-built snapshot entry and
return candidate signals; the engine adds the only state involved, which is
hysteresis. No file I/O, no clock of its own, no daemon — a rule is a table row
and a test is a dict, which is the whole reason this does not live in daemon.py.

Two deliberate limits:

* **Still pure, trends included.** Every rule fires from a single `/api/state`
  entry — including the rate rules, because the daemon computes the rates and
  hangs them on the entry as `trend` (see `samples.py`). What is deliberately
  *not* used is the pair that looks like rates and is not: `output_tokens_per_sec`
  and `api_duration_ms/duration_ms` are averages since session start, so after
  an hour they barely move, and they answer "what was this session like" rather
  than "what is happening now". A `trend` field is absent whenever the series
  behind it was too short, too sparse or too stale to support it, and an absent
  rate must never be read as zero.
* **Advisory unless the daemon can genuinely do it.** Nothing *here* can type
  `/compact` into someone's terminal, so nothing here claims it can. `action` is
  only set where a real one-click exists. The daemon sometimes can, through a
  session's channel — see `autocompact.py`, which marks the signal it is acting
  on rather than asking a rule to know whether anyone is listening.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Severity, ordered. One chip per row means the worst one has to win.
INFO, WARN, CRIT = "info", "warn", "crit"
_RANK = {INFO: 0, WARN: 1, CRIT: 2}

# How long a tool may run before silence stops being normal. Bash gets the long
# leash because builds and test suites legitimately take minutes; a fetch that
# has not come back in a minute is a hung fetch.
STALL_SECONDS = {"Bash": 300.0, "WebFetch": 60.0, "WebSearch": 60.0}
STALL_DEFAULT = 120.0
STALL_CRIT_FACTOR = 2.5          # "went for coffee" territory

# Tools that mean "waiting on my own children", which is not a stall at all.
DELEGATING_TOOLS = {"Agent", "Task"}

# Claude's names plus Grok's. A union so we do not rename Grok tools into
# Claude's vocabulary — the row still shows the name the terminal used.
EDIT_TOOLS = (
    "Edit", "Write", "MultiEdit", "NotebookEdit",
    "write", "search_replace",
)
CHURN_EDITS_PER_FILE = 12.0      # 2–5 is normal iterative editing; 12 is a loop

CTX_CRIT_PCT = 90.0              # where auto-compact looms and quality sags

# Context *runway*: how long until this session is full at the rate it is
# currently filling. This is the number 90% was always standing in for — at 90%
# you may have forty minutes or two, and the two cases want different behaviour
# from a person. Fed by `samples.SampleRing`, which refuses to produce a slope it
# cannot stand behind, so `trend` is empty far more often than it is not.
RUNWAY_WARN_SECONDS = 20 * 60.0
RUNWAY_CRIT_SECONDS = 5 * 60.0
# Below this there is nothing to warn about however steep the climb: a session at
# 12% filling fast is a session doing its job, and the estimate is at its least
# reliable when it is extrapolating furthest.
RUNWAY_MIN_CTX_PCT = 50.0

WAITING_WARN_SECONDS = 1800.0    # half an hour blocked on a human
WAITING_CRIT_SECONDS = 4 * 3600.0

NO_METRICS_AFTER_SECONDS = 600.0

BUDGET_WARN_PCT = 80.0
BUDGET_CRIT_PCT = 90.0
BUDGET_MIN_AGENTS = 2            # one agent at 84% is not the same animal
SWARM_SUBAGENTS = 6
SWARM_MIN_BUDGET_PCT = 60.0

# Grok-only, from signals.json. Turn-end truths — do not treat them as live.
DOOM_LOOP_MIN = 1                # one recovery attempt is already off the rails
CANCEL_STREAK = 2
EDIT_RETRY_MIN = 3
EDIT_RETRY_RATIO = 0.15


@dataclass(frozen=True)
class Signal:
    """One answered question. `action` is empty when the only honest move is to
    tell the human something."""
    rule: str
    severity: str
    text: str
    action: str = ""
    # Which account the signal is about. A fleet signal names a budget, and the
    # two providers' budgets are different animals — the panel draws the mark
    # rather than making the reader infer it from the wording.
    provider: str = ""

    def as_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity,
                "text": self.text, "action": self.action,
                "provider": self.provider}


def _dur(seconds: float) -> str:
    """Short, human, and never more precise than it is accurate."""
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def _num(value) -> Optional[float]:
    """Statusline fields arrive as None when absent — never as 0. A rule that
    treats a missing context reading as 0% would report every unreported session
    as healthy, which is the one wrong answer."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def stall_limit(tool: str) -> float:
    return STALL_SECONDS.get(tool, STALL_DEFAULT)


def evaluate_agent(entry: dict, category: str) -> list[Signal]:
    """Candidate signals for one snapshot entry. Pure: same dict in, same list
    out. Hysteresis and one-chip-per-row are the engine's job, not a rule's."""
    metrics = entry.get("metrics") or {}
    stats = entry.get("stats") or {}
    idle = float(entry.get("idle_seconds") or 0.0)
    tool = entry.get("current_tool") or ""
    out: list[Signal] = []

    # ── context ──────────────────────────────────────────────────────────────
    ctx = _num(metrics.get("ctx_used_pct"))
    trend = entry.get("trend") or {}
    runway = _num(trend.get("ctx_runway_seconds"))
    if metrics.get("exceeds_200k") or (ctx is not None and ctx >= CTX_CRIT_PCT):
        shown = f"context {ctx:.0f}%" if ctx is not None else "context over 200k"
        # Full, and the severity says how soon that matters. A session sitting at
        # 91% that has barely moved in ten minutes is not the emergency an
        # identical-looking one climbing 4%/min is, and only one of the two is
        # worth a banner — this is the whole reason the ring buffer exists.
        severity = CRIT
        when = ""
        if runway is not None:
            when = f", full in ~{_dur(runway)}"
            if runway > RUNWAY_WARN_SECONDS:
                severity = WARN
        out.append(Signal("ctx-full", severity,
                          f"{shown}{when} — /compact or wrap up", "reveal"))
    elif (runway is not None and ctx is not None
          and ctx >= RUNWAY_MIN_CTX_PCT and runway <= RUNWAY_WARN_SECONDS):
        # Not full yet, but on course to be before you would have noticed. The
        # rate is stated so the estimate can be judged rather than believed.
        rate = _num(trend.get("ctx_pct_per_min"))
        pace = f" (+{rate:.1f}%/min)" if rate else ""
        out.append(Signal("ctx-runway",
                          CRIT if runway <= RUNWAY_CRIT_SECONDS else WARN,
                          f"context {ctx:.0f}% — full in ~{_dur(runway)}"
                          f"{pace}, /compact soon", "reveal"))

    # ── stall ────────────────────────────────────────────────────────────────
    # A parent sitting on Agent/Task while its children run is not stalled; it is
    # doing exactly what delegation looks like. Without this exemption every
    # fan-out would light up the moment it started.
    delegating = tool in DELEGATING_TOOLS and int(entry.get("subagents") or 0) > 0
    if category == "running" and not delegating:
        limit = stall_limit(tool)
        if idle >= limit * STALL_CRIT_FACTOR:
            out.append(Signal("stall", CRIT,
                              f"no activity for {_dur(idle)}"
                              + (f" (in {tool})" if tool else "")
                              + " — check the terminal", "reveal"))
        elif idle >= limit:
            out.append(Signal("stall", WARN,
                              (f"in {tool} for {_dur(idle)}" if tool
                               else f"quiet for {_dur(idle)}")
                              + " — check the terminal", "reveal"))

    # ── churn ────────────────────────────────────────────────────────────────
    edits = sum(int((stats.get("tool_counts") or {}).get(name, 0))
                for name in EDIT_TOOLS)
    files = int(stats.get("files_touched") or 0)
    if files and edits / files >= CHURN_EDITS_PER_FILE:
        out.append(Signal("churn", INFO,
                          f"{edits} edits across {files} "
                          f"file{'s' if files != 1 else ''} — fix loop?"))

    # ── review state ─────────────────────────────────────────────────────────
    if (metrics.get("pr_review_state") or "").upper() == "CHANGES_REQUESTED":
        pr = metrics.get("pr_number")
        label = f"PR #{int(pr)}" if isinstance(pr, (int, float)) else "its PR"
        out.append(Signal("pr-changes", INFO,
                          f"{label} has changes requested", "open_pr"))

    # ── blocked on a human ───────────────────────────────────────────────────
    if category == "waiting" and idle >= WAITING_WARN_SECONDS:
        sev = CRIT if idle >= WAITING_CRIT_SECONDS else WARN
        out.append(Signal("waiting", sev,
                          f"blocked {_dur(idle)} — answer it or stop it", "reveal"))

    # ── no cost and no context ───────────────────────────────────────────────
    # Said once, quietly. A Grok row always carries model_id, so "empty
    # metrics" never fired for the provider this rule exists to explain.
    # Cost and context are the cells a reader compares; if both are absent
    # the row should say why, on either provider.
    has_cost = _num(metrics.get("cost_usd")) is not None
    has_ctx = _num(metrics.get("ctx_used_pct")) is not None
    if (category == "running" and not has_cost and not has_ctx
            and idle < NO_METRICS_AFTER_SECONDS):
        duration = float(stats.get("duration_seconds") or 0.0)
        if duration >= NO_METRICS_AFTER_SECONDS:
            out.append(Signal("no-metrics", INFO,
                              "cost and context unknown"))

    # ── Grok-only: has it gone off the rails ────────────────────────────────
    # These counters arrive from signals.json, so they are turn-end truths.
    doom = _num(metrics.get("doom_loop_attempts"))
    if doom is not None and doom >= DOOM_LOOP_MIN:
        n = int(doom)
        out.append(Signal("doom-loop", WARN,
                          f"Grok recovered from a doom loop "
                          f"{n} time{'s' if n != 1 else ''}"))

    cancels = _num(metrics.get("consecutive_cancellations"))
    if cancels is not None and cancels >= CANCEL_STREAK:
        out.append(Signal("cancellations", WARN,
                          f"cancelled {int(cancels)} times in a row — "
                          f"something is being interrupted"))

    retries = _num(metrics.get("edit_and_retry_count"))
    tools_n = int(stats.get("total_tool_calls") or 0)
    if (retries is not None and retries >= EDIT_RETRY_MIN
            and tools_n and retries / tools_n >= EDIT_RETRY_RATIO):
        out.append(Signal("edit-retry", INFO,
                          f"{int(retries)} edit-and-retry against {tools_n} "
                          f"tools — fighting the files?"))

    if metrics.get("has_reverted") is True:
        out.append(Signal("reverted", INFO, "edits were reverted this session"))

    return out


def evaluate_global(snapshot: dict) -> list[Signal]:
    """Signals about the fleet, not any one agent. Rendered once, under the
    section bar — repeating an account-level fact on every row is how a panel
    teaches people to ignore it."""
    running = snapshot.get("running") or []
    everyone = [e for group in snapshot.values() for e in group]
    out: list[Signal] = []

    # Claude's 5h window and Grok's weekly window are different budgets on
    # different accounts. One max() across both would report a number that
    # is true of neither. Group by provider; the engine keys global signals
    # by rule name, so Grok gets its own rules rather than overwriting.
    groups: dict[str, list] = {}
    running_by: dict[str, list] = {}
    for entry in everyone:
        groups.setdefault(entry.get("provider") or "claude", []).append(entry)
    for entry in running:
        running_by.setdefault(entry.get("provider") or "claude", []).append(entry)

    for provider, members in groups.items():
        budgets = [_num((e.get("metrics") or {}).get("five_hour_pct"))
                   for e in members]
        budgets = [b for b in budgets if b is not None]
        if not budgets:
            continue
        worst = max(budgets)
        n_running = len(running_by.get(provider, []))
        grok = provider == "grok"
        if worst >= BUDGET_WARN_PCT and n_running >= BUDGET_MIN_AGENTS:
            sev = CRIT if worst >= BUDGET_CRIT_PCT else WARN
            out.append(Signal("grok-budget" if grok else "budget", sev,
                              f"{_budget_label(provider, members)} {worst:.0f}% "
                              f"with {n_running} agents running — "
                              f"wind down or stagger them",
                              provider=provider))
        subagents = sum(int(e.get("subagents") or 0) for e in members)
        if subagents >= SWARM_SUBAGENTS and worst >= SWARM_MIN_BUDGET_PCT:
            text = (f"{subagents} live Grok subagents — burn is multiplied "
                    f"right now" if grok else
                    f"{subagents} live subagents — burn is multiplied right now")
            out.append(Signal("grok-swarm" if grok else "swarm", INFO, text,
                              provider=provider))
    return out


def _budget_label(provider: str, members: list) -> str:
    if provider != "grok":
        return "5h budget"
    cycle = ""
    for entry in members:
        value = (entry.get("metrics") or {}).get("budget_cycle")
        if isinstance(value, str) and value:
            cycle = value
            break
    return f"Grok {cycle} budget" if cycle else "Grok budget"


class SignalEngine:
    """Hysteresis, and nothing else.

    A rule evaluated every three seconds against a live snapshot will flap: one
    slow tool call crosses the stall threshold, the next event clears it, and the
    row blinks. So a signal must hold for `hold` consecutive evaluations before
    it is shown and `hold` more before it is withdrawn. Escalation skips the
    wait — a warning that has become critical is news, and making someone watch
    it queue is the opposite of the point.
    """

    def __init__(self, hold: int = 2):
        self.hold = hold
        self._state: dict[tuple[str, str], dict] = {}

    def evaluate(self, snapshot: dict, global_key: str = "*") -> dict:
        """Returns `{"by_session": {sid: [dict, ...]}, "global": [dict, ...]}`,
        already ordered worst-first and already debounced."""
        candidates: dict[tuple[str, str], Signal] = {}
        owners: dict[str, list[Signal]] = {}

        for category, entries in snapshot.items():
            for entry in entries:
                sid = entry.get("session_id") or ""
                for signal in evaluate_agent(entry, category):
                    candidates[(sid, signal.rule)] = signal
        for signal in evaluate_global(snapshot):
            candidates[(global_key, signal.rule)] = signal

        live_owners = {e.get("session_id") or "" for g in snapshot.values() for e in g}
        live_owners.add(global_key)

        for key, signal in candidates.items():
            state = self._state.setdefault(key, {"up": 0, "down": 0, "live": False,
                                                 "signal": signal})
            previous: Signal = state["signal"]
            state["up"] += 1
            state["down"] = 0
            escalated = _RANK[signal.severity] > _RANK[previous.severity]
            state["signal"] = signal
            if not state["live"] and (state["up"] >= self.hold or escalated):
                state["live"] = True

        for key, state in list(self._state.items()):
            owner = key[0]
            if owner not in live_owners:            # session gone: forget it
                del self._state[key]
                continue
            if key in candidates:
                continue
            state["up"] = 0
            state["down"] += 1
            if state["down"] >= self.hold:
                del self._state[key]

        by_session: dict[str, list[dict]] = {}
        globals_out: list[Signal] = []
        for (owner, _rule), state in self._state.items():
            if not state["live"]:
                continue
            signal: Signal = state["signal"]
            if owner == global_key:
                globals_out.append(signal)
            else:
                owners.setdefault(owner, []).append(signal)

        for owner, found in owners.items():
            by_session[owner] = [s.as_dict() for s in _worst_first(found)]
        return {"by_session": by_session,
                "global": [s.as_dict() for s in _worst_first(globals_out)]}


def _worst_first(signals: list[Signal]) -> list[Signal]:
    """Severity first, then rule name so equal severities keep a stable order —
    a list that reshuffles every push is a list nobody can read."""
    return sorted(signals, key=lambda s: (-_RANK[s.severity], s.rule))


__all__ = ["Signal", "SignalEngine", "evaluate_agent", "evaluate_global",
           "stall_limit", "INFO", "WARN", "CRIT"]
