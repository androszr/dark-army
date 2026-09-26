"""Which signals have earned the right to interrupt you.

`signals.py` answers "what is worth saying about this agent" and is evaluated
every few seconds against a live snapshot. That is the right cadence for
something *displayed* and completely the wrong one for something that makes a
sound and slides onto the screen. Piping one straight into the other is how an
app gets muted in a week, so this module is the gate between them.

The rules, in the order they matter:

* **Transitions only.** A signal that has been true for an hour is not news. An
  alert fires when a signal goes live or escalates, never while it holds.
* **Only what you can act on.** `waiting` and `crit`. Churn and no-metrics are
  observations; they belong in the panel and nowhere else.
* **A card is its own alert.** "Claude is waiting for your input" is the one
  event this app exists for, and it is not a signal at all — it is a notification
  card, which is why the card path is handled here explicitly.
* **A permission prompt outranks everything.** A relayed tool-approval request
  (`channel_server.py`) is a session stopped dead until a human clicks, so it is
  handled before the card and the signals, it is *not* subject to the
  per-session cooldown (a status change can wait out a cooldown; a blocked tool
  call cannot), and it fires exactly once per `request_id` — a request id never
  recurs, so "once per transition" and "once ever" are the same thing here.
* **One per agent per window.** `PER_SESSION_COOLDOWN` throttles a session that
  is having a bad afternoon, so six agents cannot produce six notifications a
  minute between them.
* **Never the same thing twice.** A (session, rule) that has already fired stays
  fired until it clears, so a flapping signal costs one interruption rather than
  one per flap.
* **Muted agents say nothing at all**, and muting is per session because the
  agent you have decided to ignore is rarely the whole fleet.
* **A kind rides every alert.** One word from `KINDS` — permission, question,
  attention, finished — decided here, where the question slot, the card's
  hook and the prompt's tool are all in hand, and never re-derived
  downstream. The phone's buzz reads it for its words and its sound.
* **A finished report is a quiet banner on the Mac and nothing else.** The
  `report` rule (`REPORT_RULE`) raises one `finished`-kind alert the moment
  a `sleeping` row carries a `work_report` (`work_report.py`, published by
  the daemon beside `last_report`) while it is fresh (quiet inside
  `FINISHED_IDLE_GRACE_SECONDS`): "<who> finished", the headline as its
  body, no card, no sound (`notifier.post`), once per report — a report you
  were looking at, had muted, or that a card carried counts as delivered —
  never beside a card or a prompt, and outside the per-session
  cooldown both ways — it neither waits on it nor stamps it, so the next
  real ask is never swallowed behind it. The phone leg withholds the
  `finished` kind before any push, as it always has.
* **And what is needed, in the agent's own words.** `need` is the question
  the agent asked or the one-line summary it left behind — decided here by
  `_need`, mirroring `_kind`'s precedence — and `tool` is the bare name of
  the tool a permission prompt is about. Both are stamped at the gate and
  never re-derived; the phone's buzz composes its third line from them.
  Never the command preview, the tool's `description`, a path, the project
  or the branch: those stay on `body` / `subtitle`, which never leave the Mac.

Pure, like `signals.py` and for the same reason: the policy is the part worth
testing, and it should be testable with a dict and a clock rather than a daemon.
Delivery — actually posting to macOS — belongs to whatever can talk to
`UNUserNotificationCenter`, and that is the **Python menu-bar process**
(`dark_army_menubar/notifier.py`), not the Swift panel this comment used to
name. `UNUserNotificationCenter.current()` traps without a bundle identity, and
the panel binary lives at `Contents/Resources/BobPanel` where there is none —
measured, `Bundle.main.bundleIdentifier` is nil, because CFBundle only walks up
from `Contents/MacOS`. The menu bar *is* the bundle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import cast
from . import work_report
from .protocol import is_ask_user_question
from .session_stats import FINISHED_IDLE_GRACE_SECONDS

# Severity that earns an interrupt on its own.
INTERRUPT_SEVERITIES = {"crit"}

# Rules allowed to interrupt below `crit`. `waiting` is here because being
# blocked on a human is the whole point of the app: nobody is coming unless they
# are told, however long it has been.
INTERRUPT_RULES = {"waiting"}

# Rules that must never interrupt, whatever their severity — they describe the
# session rather than ask anything of you.
NEVER_INTERRUPT = {"churn", "no-metrics", "swarm"}

# The hooks whose card message is `protocol.py`'s stock "Waiting for input" —
# Dark Army's placeholder for "the turn ended", not anybody's words about anything.
# `StopFailure` is deliberately absent: its message is the harness's own error.
_GENERIC_CARD_HOOKS = {"Stop", "Notification"}

# What an interruption *is*, in one word, most urgent first. The order is
# load-bearing: when several alerts collapse into one buzz the phone takes the
# first member any of them carries.
# `security` leads: somebody knocking repeatedly at the phone doors
# (`access_log.BurstDetector`) is the one interruption about the machine
# itself rather than an agent, and a buzz collapsing it with an agent's
# ask should play the more urgent cue.
KINDS = ("security", "permission", "question", "attention", "finished")
# The kinds that hang a portrait. A plain needs-you (`attention`) and a
# machine warning (`security`) do not. The menu bar restates this tuple;
# it does not import this module. `test_phone_buzz_kinds.py` pins them equal.
FACE_KINDS = ("question", "permission", "finished")
KIND_ATTENTION = "attention"
KIND_SECURITY = "security"
KIND_FINISHED = "finished"

#: The rule a finished work report raises its one quiet banner under.
REPORT_RULE = "report"
#: The bucket a report banner may come from: a row at rest. Never `running`
#: (a report read before the Stop lands would banner, then the Stop's card
#: would banner again with the same words), `waiting` (the card speaks
#: there), `finished` (not considered at all) or `abandoned`.
REPORT_CATEGORIES = ("sleeping",)

PER_SESSION_COOLDOWN = 300.0     # seconds between alerts about one agent
MAX_HISTORY = 256                # bounded: one entry per (session, rule) seen


@dataclass(frozen=True)
class Alert:
    """One interruption, already decided on. `actions` name verbs the daemon
    genuinely has — nothing here offers a button that cannot be pressed."""

    id: str
    session_id: str
    nickname: str
    title: str
    body: str
    severity: str
    rule: str
    created_at: float
    actions: tuple[str, ...] = ()
    #: which agent, in the words you would use to find it. A banner is read in
    #: the second it slides past, and "Vex needs you" does not say *which*
    #: Vex when three repos are running.
    subtitle: str = ""
    #: the face and pose to draw on the banner, in `cast.py`'s vocabulary. Same
    #: reasoning as the subtitle, one step further: six banners that differ only
    #: in their text all look identical going past, and a portrait is read before
    #: a sentence is. Decided here, where the category is known, rather than in
    #: the deliverer — which sees an alert and not the bucket it came from.
    character: str = ""
    state: str = ""
    #: one of `KINDS` — what sort of interruption this is, decided at the
    #: gate. A rule name says which *path* raised the alert; the kind says
    #: what the person is being asked for, which is what a buzz they cannot
    #: read yet needs to convey.
    kind: str = KIND_ATTENTION
    #: the permission prompt this alert is about, for a phone that may
    #: answer it from the lock screen; `""` for every other rule.
    request_id: str = ""
    answerable: bool = True
    #: what the agent is asking for, in its own words — the question's text
    #: or the `bob-tldr` summary (`_need`); `""` where it said nothing. The
    #: phone's buzz composes its third line from this, never from `body`.
    need: str = ""
    #: the bare name of the tool a permission prompt is about ("Bash"),
    #: stamped by `_permission`; `""` on every other rule. Never the command.
    tool: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "session_id": self.session_id,
                "request_id": self.request_id,
                "answerable": self.answerable,
                "need": self.need, "tool": self.tool,
                "nickname": self.nickname, "title": self.title,
                "subtitle": self.subtitle,
                "body": self.body, "severity": self.severity,
                "rule": self.rule, "created_at": self.created_at,
                "actions": list(self.actions),
                "character": self.character, "state": self.state,
                "kind": self.kind}


def _title(nickname: str, project: str) -> str:
    """"Vex needs you" is a sentence about someone you know; "session
    78be5105 needs you" is a log line. The nickname is why identity was built."""
    who = nickname or project or "An agent"
    return f"{who} needs you"


def _report_fresh(entry: dict) -> bool:
    """Whether the row went quiet inside `FINISHED_IDLE_GRACE_SECONDS` — the
    window in which a report is news. After a restart `_fired` is empty and
    the transcripts give every quiet row its report back; an hour-old report
    is history, not a banner. An unreadable figure reads as fresh."""
    try:
        idle = float(entry.get("idle_seconds") or 0.0)
    except (TypeError, ValueError):
        return True
    return idle <= FINISHED_IDLE_GRACE_SECONDS


def _quiet_before(entry: dict, moment: float, now: float) -> bool:
    """Whether the row went quiet before `moment` — its `quiet_since`, else
    `now - idle_seconds`. `moment` 0 (unknown) is never after anything."""
    if moment <= 0:
        return False
    try:
        quiet = float(entry.get("quiet_since") or 0.0) \
            or now - float(entry.get("idle_seconds") or 0.0)
    except (TypeError, ValueError):
        return False
    return quiet < moment


def report_pending(policy: "AlertPolicy", entry: dict) -> bool:
    """Whether this row's work report could still raise its banner: parsed,
    fresh, and neither bannered nor stamped delivered yet. The daemon asks
    it to pick which sleeping sessions the frontmost poll must ask about."""
    sid = entry.get("session_id") or ""
    return (bool(sid) and isinstance(entry.get("work_report"), dict)
            and _report_fresh(entry)
            and (sid, REPORT_RULE) not in policy._fired)


def _title_finished(nickname: str, project: str) -> str:
    """"Vex finished" — the same who as `_title`, a different verb."""
    who = nickname or project or "An agent"
    return f"{who} finished"


def marker_current(entry: dict) -> bool:
    """Whether the entry's `bob-tldr` / `bob-actions` markers belong to the
    turn now standing: written at or after the person's last prompt.

    Reads the transcript's own clocks, `marker_at` and `prompt_at` (epoch
    floats, `0.0` where absent), so a caption from a previous turn can never
    count even if a clearing was missed. Both absent reads current — the
    behaviour before the clocks existed, and the reading for Grok and Codex
    rows, which carry no transcript clocks. Pure.
    """
    try:
        marker_at = float(entry.get("marker_at") or 0.0)
        prompt_at = float(entry.get("prompt_at") or 0.0)
    except (TypeError, ValueError):
        # A malformed clock cannot prove the marker stale; the old reading
        # (current) stands, as it does for a row with no clocks at all.
        return True
    return marker_at >= prompt_at


def offered_reply(entry: dict) -> bool:
    """Whether the entry offers the person named choices for this turn: a
    non-empty `reply_options` list whose marker is current
    (`marker_current`). `_kind` and `decision_capture.reconcile` both read
    it, so the buzz and the episode agree. Pure.
    """
    options = entry.get("reply_options")
    return isinstance(options, list) and bool(options) and marker_current(entry)


def _kind(entry: dict, card: Optional[dict], rule: str) -> str:
    """Which of `KINDS` an alert about this entry is.

    The question slot wins over the card's hook, because the card that
    carries a question to this gate *is* the generic idle reminder — the
    hook alone would call every question a finished turn. So does a
    current `bob-actions` offer (`offered_reply`: named choices written
    after the person's last prompt), because a turn that offers choices
    is asking you to pick one. A `bob-tldr` summary alone is *not*
    question evidence since 22 Sep 2026: it is a one-line caption every
    turn may leave, and reading it as a question buzzed the phone for
    turns that asked nothing (RC1 in
    the false-buzz investigation of 20 Sep 2026).
    A generic end-of-turn card with no dialog and no current offer is a
    finished turn; everything else (a `StopFailure`'s real error, a
    `crit` signal, the 30-minute `waiting` signal with no card) is plain
    attention.
    """
    questions = entry.get("questions")
    if isinstance(questions, list) and questions:
        return "question"
    question = entry.get("question")
    if isinstance(question, dict) and question:
        return "question"
    if rule == "card" and card and card.get("hook") in _GENERIC_CARD_HOOKS:
        if offered_reply(entry):
            return "question"
        return "finished"
    return KIND_ATTENTION


def _need(entry: dict, card: Optional[dict]) -> str:
    """What the agent is asking for, in its own words, or `""`.

    Mirrors `_kind`'s precedence exactly: the entry's question slot wins —
    the flat `question` dict's `text`, else the first non-blank `text` in
    the `questions` list — then, under a generic end-of-turn card (`Stop` /
    `Notification`, the stock "Waiting for input"), the `bob-tldr` summary
    the turn left behind. A `StopFailure` card's message is the harness's
    own error and is never the need; a signal's text is not the agent's
    words either. Pure: reads the two dicts it is handed and nothing else.
    """
    question = entry.get("question")
    if isinstance(question, dict) and question:
        text = str(question.get("text") or "").strip()
        if text:
            return text
    questions = entry.get("questions")
    if isinstance(questions, list) and questions:
        for item in questions:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if text:
                return text
    if card and card.get("hook") in _GENERIC_CARD_HOOKS:
        return str(entry.get("last_summary") or "").strip()
    return ""


def _subtitle(nickname: str, project: str, branch: str) -> str:
    """Where that agent is working: "dark-army · feat/panel".

    Omitted when the project is all the title had to work with — repeating it
    directly under itself says nothing, and an empty subtitle costs no line.
    """
    if not project or (not nickname and project):
        return branch or ""
    return f"{project} · {branch}" if branch else project


@dataclass
class AlertPolicy:
    """Decides, and remembers just enough to not decide twice."""

    cooldown: float = PER_SESSION_COOLDOWN
    #: (session_id, rule) -> when it last fired
    _fired: dict[tuple[str, str], float] = field(default_factory=dict)
    #: session_id -> when it last produced any alert
    _last_per_session: dict[str, float] = field(default_factory=dict)
    #: sessions the user has silenced
    _muted: set[str] = field(default_factory=set)
    _counter: int = 0
    #: When this daemon started (wall clock; 0 = unknown). A work report
    #: whose row went quiet before then was news for a previous run — its
    #: key was lost with that run's memory — and is seeded as delivered.
    started_at: float = 0.0

    # ── muting ───────────────────────────────────────────────────────────────
    def mute(self, session_id: str) -> None:
        self._muted.add(session_id)

    def unmute(self, session_id: str) -> None:
        self._muted.discard(session_id)

    def is_muted(self, session_id: str) -> bool:
        return session_id in self._muted

    # ── the gate ─────────────────────────────────────────────────────────────
    def evaluate(self, snapshot: dict, cards: dict, now: float,
                 suppressed: Iterable[str] = (),
                 prompts: Optional[dict] = None,
                 panel_focused: Iterable[str] = (),
                 report_hold: Iterable[str] = ()) -> list[Alert]:
        """Alerts to raise for this tick.

        `snapshot` is the assembled agent map, `cards` maps session id to the
        active notification card, and `suppressed` names sessions that should not
        interrupt right now for a reason this module cannot see — chiefly that
        you are already looking at that session's window, where a notification
        would tell you what is on your screen.

        `panel_focused` names the sessions whose Dark Army-owned terminal the panel
        is drawing right now — the rebuilt half of frontmost suppression for
        a terminal VS Code cannot see. Unioned with `suppressed`: the two
        sets are decided by different askers (the extension's `frontmost`
        op, the panel's `panel_terminal` verb) and either alone is enough.

        `prompts` maps session id to that session's oldest open permission
        request (the dict `channel_server.py` relayed). Keyed by session rather
        than handed over as a list so an unresolved prompt — a channel that
        announced itself before its session's first hook event — simply is not
        here yet: it fires on a later tick, the moment Dark Army knows *who* is
        asking, because a banner that cannot name its agent cannot be acted on.
        """
        out: list[Alert] = []
        skip = set(suppressed) | set(panel_focused)
        # Sessions whose report banner waits for a frontmost reading taken
        # after the row went quiet (`daemon._report_hold`): neither fired
        # nor stamped this tick, so a later reading can still suppress it.
        hold = set(report_hold)
        live: set[str] = set()

        for category, entries in snapshot.items():
            if category == "finished":
                # A finished run asks nothing of you — none of these are
                # *considered*. But the bucket also holds live sessions quiet
                # past FINISHED_IDLE_GRACE_SECONDS (120s), flagged `alive`
                # (the same trap `daemon._decide_auto_compacts` steps around).
                # Leaving those out of `live` let `_forget_gone` drop their
                # mute and fired keys the moment they went quiet, so a muted
                # agent re-bannered on its very next wait.
                for entry in entries:
                    sid = entry.get("session_id") or ""
                    if sid and entry.get("alive"):
                        live.add(sid)
                continue
            for entry in entries:
                sid = entry.get("session_id") or ""
                if not sid:
                    continue
                live.add(sid)
                card = cards.get(sid)
                if isinstance(entry.get("work_report"), dict) \
                        and _quiet_before(entry, self.started_at, now):
                    self._fired.setdefault((sid, REPORT_RULE), now)
                # A report already delivered some other way is not news
                # later: one you were looking at when it landed (muted,
                # frontmost, the panel's own terminal) or one a card is
                # carrying — the card's banner has its headline. Stamped
                # here so looking away, or dismissing the card, raises no
                # second "finished" banner for the same report.
                if isinstance(entry.get("work_report"), dict) and (
                        sid in self._muted or sid in skip or card):
                    self._fired.setdefault((sid, REPORT_RULE), now)
                if sid in self._muted or sid in skip:
                    continue

                nickname = entry.get("nickname") or ""
                project = entry.get("project") or ""
                subtitle = _subtitle(nickname, project, entry.get("branch") or "")

                face = cast.character_for(nickname, sid)
                pose = cast.state_for(category)
                # The agent's own ask, decided once here beside the kind and
                # stamped on whichever alert this entry raises.
                need = _need(entry, card)

                # A permission prompt beats even the card, and is deliberately
                # not gated on `category == "waiting"`: the hook event that
                # flips the session into `waiting` races the channel relay, and
                # losing that race must cost milliseconds, not a categorizer
                # cycle. The prompt *is* the proof the session is blocked.
                prompt = (prompts or {}).get(sid)
                if prompt:
                    alert = self._permission(sid, prompt, now, nickname,
                                             project, subtitle, face, pose,
                                             need=need)
                    if alert:
                        out.append(alert)
                        continue           # one interruption per agent per tick

                # The card first: it carries the actual question, so when both a
                # card and a signal are present the card is the better sentence.
                #
                # Its *message*, though, is usually the placeholder both
                # end-of-turn hooks raise — "Waiting for input", which is the
                # whole banner saying only what its title already said. When the
                # agent left a one-line summary behind the `bob-tldr` marker,
                # that is the body: a banner is read in the second it slides
                # past, and a sentence aimed at exactly this slot beats a
                # constant. A `StopFailure` card's message is a real API error
                # and outranks the summary, which describes the turn before it.
                #
                # With no summary, a work report's headline stands in: it is
                # the agent's own words about the turn, one line long, and
                # still better than the constant (`work_report.headline_of`).
                if card and category == "waiting":
                    body = str(card.get("message") or "Waiting for input")
                    summary = str(entry.get("last_summary") or "").strip()
                    if card.get("hook") in _GENERIC_CARD_HOOKS:
                        if summary:
                            body = summary
                        elif work_report.headline_of(entry):
                            body = work_report.headline_of(entry)
                    alert = self._maybe(sid, "card", now, nickname,
                                        _title(nickname, project),
                                        body,
                                        "warn", subtitle, face, pose,
                                        kind=_kind(entry, card, "card"),
                                        need=need)
                    if alert:
                        out.append(alert)
                        continue

                for signal in entry.get("signals") or []:
                    rule = signal.get("rule") or ""
                    severity = signal.get("severity") or ""
                    if rule in NEVER_INTERRUPT:
                        continue
                    # Something else is already doing what the banner would have
                    # asked you to do — today that is `autocompact.py`, which
                    # types `/compact` into the session's VS Code terminal. An
                    # interruption whose only instruction has already been
                    # carried out is worse than no interruption: it teaches you
                    # that Dark Army's banners are things to ignore. The signal is
                    # still on the row, still saying who is handling it, and the
                    # mark is dropped the moment the attempt fails — so this
                    # withholds the alert, it does not cancel it.
                    if signal.get("handled"):
                        continue
                    if severity not in INTERRUPT_SEVERITIES and rule not in INTERRUPT_RULES:
                        continue
                    alert = self._maybe(sid, rule, now, nickname,
                                        _title(nickname, project),
                                        str(signal.get("text") or ""), severity,
                                        subtitle, face, pose,
                                        kind=_kind(entry, card, rule),
                                        need=need)
                    if alert:
                        out.append(alert)
                        signalled = True
                        break               # one interruption per agent per tick
                else:
                    signalled = False
                if signalled:
                    continue

                # Last, and only for a row nobody is being asked anything
                # about: a finished report. No card, no prompt, not waiting.
                if (category in REPORT_CATEGORIES and not card and not prompt
                        and isinstance(entry.get("work_report"), dict)
                        and _report_fresh(entry) and sid not in hold):
                    alert = self._report_alert(sid, entry, now, nickname,
                                               project, subtitle, face, pose)
                    if alert:
                        out.append(alert)

        self._forget_gone(live)
        return out

    def _maybe(self, sid: str, rule: str, now: float, nickname: str,
               title: str, body: str, severity: str,
               subtitle: str = "", character: str = "",
               state: str = "", kind: str = KIND_ATTENTION,
               need: str = "") -> Optional[Alert]:
        key = (sid, rule)
        if key in self._fired:
            return None                     # already told you; still true
        last = self._last_per_session.get(sid)
        if last is not None and now - last < self.cooldown:
            # This agent has already interrupted recently. The rule is still
            # recorded as fired, so it will not queue up and arrive in a burst
            # the moment the cooldown ends.
            self._fired[key] = now
            return None
        self._fired[key] = now
        self._last_per_session[sid] = now
        self._counter += 1
        actions = ("reveal", "dismiss", "mute") if rule == "card" \
            else ("reveal", "mute")
        return Alert(id=f"{sid}:{rule}:{self._counter}", session_id=sid,
                     nickname=nickname, title=title, body=body,
                     severity=severity, rule=rule, created_at=now,
                     actions=actions, subtitle=subtitle,
                     character=character, state=state, kind=kind,
                     need=need)

    def _report_alert(self, sid: str, entry: dict, now: float, nickname: str,
                      project: str, subtitle: str, character: str,
                      state: str) -> Optional[Alert]:
        """One quiet banner per work report: "<who> finished", the headline
        as its body.

        Its own path rather than `_maybe`, for the cooldown's sake in both
        directions. It does not *wait* on `_last_per_session`: a report is
        news once, and a card that fired two minutes earlier must not
        swallow it into `_fired` for good. And it does not *stamp* it: a
        real ask arriving ten seconds after a report is the one thing this
        app exists for, and a finished banner must never hold it back.
        Deduped on `(sid, REPORT_RULE)`; `clear_resolved` re-arms the key
        once the report leaves the row (the person's next prompt), so the
        next report is news again.
        """
        key = (sid, REPORT_RULE)
        if key in self._fired:
            return None
        self._fired[key] = now
        self._counter += 1
        body = work_report.headline_of(entry) or "Wrote a work report"
        return Alert(id=f"{sid}:{REPORT_RULE}:{self._counter}", session_id=sid,
                     nickname=nickname,
                     title=_title_finished(nickname, project),
                     body=body, severity="info", rule=REPORT_RULE,
                     created_at=now, actions=("reveal", "mute"),
                     subtitle=subtitle, character=character, state=state,
                     kind=KIND_FINISHED, need="")

    def _permission(self, sid: str, prompt: dict, now: float, nickname: str,
                    project: str, subtitle: str, character: str,
                    state: str, need: str = "") -> Optional[Alert]:
        """One alert per relayed tool-approval request, cooldown be damned.

        The cooldown exists so a session having a bad afternoon cannot post six
        status updates; a permission prompt is not a status update, it is a
        session stopped dead with a dialog nobody has seen. Swallowing it
        because a card fired two minutes ago would leave the agent blocked in
        silence — the exact failure this feature was built after. The stamp on
        `_last_per_session` still goes down, so the *next* ordinary alert about
        this session honours the window: you were just interrupted about it.

        Deduped on the request id, not on a rule name: a second prompt from the
        same session is a genuinely new question and must fire, while the same
        prompt re-relayed (a daemon restart mid-dialog) must not.
        """
        rid = str(prompt.get("request_id") or "")
        if not rid:
            return None
        key = (sid, f"permission:{rid}")
        if key in self._fired:
            return None
        self._fired[key] = now
        self._last_per_session[sid] = now
        self._counter += 1
        tool = str(prompt.get("tool_name") or "")
        # A channel session relays `AskUserQuestion` through the same prompt
        # route as a tool approval; the kind tells the two apart while the
        # Mac title below keeps naming the tool as it always has.
        kind = "question" if is_ask_user_question(tool) else "permission"
        who = nickname or project or "An agent"
        # "Ledger wants to run Bash" — the verb the dialog is asking about,
        # with the model's own words underneath. Both are untrusted text all
        # the way to the banner, same as the panel treats them.
        title = f"{who} wants to run {tool}" if tool else f"{who} needs permission"
        body = str(prompt.get("input_preview") or "").strip() \
            or str(prompt.get("description") or "").strip() \
            or "Waiting for permission"
        # `reveal` and `mute` only. Allow/Deny from the banner was considered
        # and deferred: the banner shows a truncated preview of
        # the command, and approving a tool call from a surface that cannot
        # show the whole command is the wrong affordance. Reveal lands you on
        # the real dialog. `dismiss` names a card verb and there is no card.
        return Alert(id=f"{sid}:permission:{self._counter}", session_id=sid,
                     nickname=nickname, title=title, body=body,
                     severity="warn", rule="permission", created_at=now,
                     actions=("reveal", "mute"), subtitle=subtitle,
                     character=character, state=state, kind=kind,
                     request_id=rid, tool=tool, need=need,
                     answerable=bool(prompt.get("answerable", True)))

    def clear(self, session_id: str, rule: str) -> None:
        """Let a rule fire again — called when the underlying signal goes away."""
        self._fired.pop((session_id, rule), None)

    def _forget_gone(self, live: set[str]) -> None:
        """Drop everything about sessions that are no longer anywhere.

        Without this the maps grow one entry per session the machine ever ran —
        the exact shape of the four leaks this codebase has already had to fix.
        """
        for key in [k for k in self._fired if k[0] not in live]:
            del self._fired[key]
        for sid in [s for s in self._last_per_session if s not in live]:
            del self._last_per_session[sid]
        for sid in [s for s in self._muted if s not in live]:
            self._muted.discard(sid)
        if len(self._fired) > MAX_HISTORY:
            for key in list(self._fired)[:len(self._fired) - MAX_HISTORY]:
                del self._fired[key]


def clear_resolved(policy: AlertPolicy, snapshot: dict, cards: dict) -> None:
    """Re-arm rules whose signal has gone.

    A signal that clears and returns is a genuinely new event — an agent that
    got unblocked and blocked again should say so — so the fired set has to be
    released when the underlying condition disappears, not only when the session
    does.
    """
    for category, entries in snapshot.items():
        for entry in entries:
            sid = entry.get("session_id") or ""
            if not sid:
                continue
            present = {s.get("rule") for s in (entry.get("signals") or [])}
            if sid in cards and category == "waiting":
                present.add("card")
            # The report key holds for as long as the row carries its
            # report — through `sleeping` and the `finished` bucket alike —
            # and is released when the next prompt clears it.
            if isinstance(entry.get("work_report"), dict):
                present.add(REPORT_RULE)
            # Permission keys are never re-armed here. A prompt is not a signal,
            # so it is never in `present` — clearing its key would re-fire the
            # same banner on every tick the dialog stays open, which is the flap
            # this whole module exists to prevent. And re-arming is pointless
            # besides: the key is a request id, and request ids do not recur.
            # The entries die with their session in `_forget_gone`.
            for key in [k for k in list(policy._fired)
                        if k[0] == sid and k[1] not in present
                        and not k[1].startswith("permission:")]:
                del policy._fired[key]


__all__ = ["Alert", "AlertPolicy", "clear_resolved", "PER_SESSION_COOLDOWN",
           "INTERRUPT_RULES", "INTERRUPT_SEVERITIES", "NEVER_INTERRUPT",
           "KINDS", "KIND_FINISHED", "REPORT_RULE", "marker_current",
           "offered_reply", "report_pending"]
