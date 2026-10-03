"""Where a session came from — the whole attribution vocabulary, in one file.

A session that appears in the monitor with a character name, a job title and a
project, and nothing at all saying what started it, is worse than no monitor:
the monitor's whole job is to tell you whether something on your machine is
yours. So every place Dark Army starts an assistant stamps the new session's
environment with `BOB_COMPANION_ORIGIN`, the hook handler carries that stamp
back on every message the way it already carries the enrolment key, and the row
is attributed from its very first event rather than from a board join that
lands a frame later.

**This is an attribution boundary, not authentication against a hostile local
process** — exactly what `enrollment.py` says of its own key. `ENV_VAR` is data
a process could set for itself, and a forged stamp buys nothing: it grants no
dispatch, no board write, no channel, no permission and no capability of any
kind. Every gate (`dispatch.guard`, the plan gate, `enrollment.resolve`,
`api_server._authorised`) is untouched by anything here. A forged stamp can
only make a row *claim* an origin; a **missing** stamp is the honest default
and draws nothing at all. The one thing a claimed origin does change is what
the daemon *withholds*: a `mission` stamp keeps its own Stop / Notification
card off the Needs you list (`daemon._mission_reply`), so a process forging
it silences nobody but itself.

Two consequences worth naming rather than detecting:

* **A stamp outlives its spawn.** The environment is inherited by every child
  of the terminal, so a person typing `claude` inside a Dark Army-started terminal
  produces a second session wearing the same stamp. That is arguably true — it
  *was* started from Dark Army's terminal — and it is bounded to the same card.
  First-writer-wins in `_update_session_state` stops the row flapping.
* **Grok's hooks do not run in the session's environment.** They run under
  the shared `grok agent leader`, which inherits the environment of the
  terminal that first started it, so a Grok hook message carries *that*
  terminal's stamp for every session since. The daemon ignores the stamp on
  a Grok message and reads the session's own process instead
  (`pid_resolver.process_origin`, `daemon._probe_grok_origin`).
* **An older persistent broker drops it.** `pty_broker`'s `start` op learned
  the `env` field in this change; a broker already running from an earlier
  build ignores a field it does not know, so a session started while it is up
  is simply unattributed until that broker exits. Degraded, never wrong.

Pure and stdlib-only: this module imports nothing from `dark_army_daemon`,
so the hook handler's own copy of the rule (a bounded `os.environ.get` inside
`NOTIFY_SCRIPT`) can stay stdlib-only too.
"""

import re

#: The one environment variable. Named in full everywhere; there is no prefix
#: family here and nothing else may be smuggled through the same door.
ENV_VAR = "BOB_COMPANION_ORIGIN"

#: What `pty_broker`'s `start` op will accept out of an `env` payload. Exactly
#: this one key: the broker runs in its own process and a wider filter would
#: make the RPC a general-purpose environment injector.
ENV_KEY_RE = re.compile(r"\ABOB_COMPANION_ORIGIN\Z")

#: The kinds of press that start a session. A stamp naming anything else is
#: not a stamp — `stamp()` returns "" and `parse()` returns {}. `adhoc` is the
#: one that names no card: a person pressing "open a terminal here" is still a
#: press Dark Army made a terminal for, and an unlabelled one would be exactly the
#: unattributed row this module exists to remove. `mission` is the other
#: cardless kind: the one standing Mission Control terminal Dark Army opens
#: on the Comm tab's first visit (`mission.py`).
KINDS = ("card-start", "card-refine", "card-consult", "adhoc", "mission",
         "review")

#: The stored and transported bound. Small on purpose: this is a label, and
#: anything longer is either a bug or somebody probing.
MAX_STAMP_CHARS = 200

#: The composed sentence's bound, so a long card title cannot push a row's
#: layout around on either client.
MAX_LINE_CHARS = 160

#: Each of the three fields, individually.
MAX_FIELD_CHARS = 60

#: The kind and the card id are identifiers, never prose. Anything outside
#: this is not cleaned away quietly on the way *in* — see `parse`, which
#: refuses rather than repairs.
_FIELD_RE = re.compile(r"[^A-Za-z0-9_.:-]")

#: A **stage** is the card's `workflow` text, which is free prose a person
#: typed, so a space belongs in it: filtering one out turned "security review"
#: into "securityreview" on the row — a mangling that reads as a bug in Dark Army
#: rather than as the name somebody wrote. The `|` separator is still outside
#: the alphabet, so the three-field split cannot be forged from inside a field.
_STAGE_RE = re.compile(r"[^A-Za-z0-9_.: -]")


def _clean(value: str) -> str:
    """A field as it is allowed to be: the safe alphabet, bounded."""
    return _FIELD_RE.sub("", str(value or ""))[:MAX_FIELD_CHARS]


def _clean_stage(value: str) -> str:
    """A stage name as it is allowed to be: the safe alphabet plus the space.

    Runs of whitespace collapse to one and the ends are stripped, so the
    cleaned form is idempotent — `parse` compares against it, and a rule that
    did not round-trip would refuse the very stamps `stamp` writes.
    """
    collapsed = " ".join(_STAGE_RE.sub("", str(value or "")).split())
    # Stripped *after* the bound too: a cut that lands just past a space would
    # otherwise leave a trailing one, and the second clean would differ.
    return collapsed[:MAX_FIELD_CHARS].strip()


def stamp(kind: str, card_id: str = "", stage: str = "") -> str:
    """The string handed to a new session's environment, or `""`.

    `"kind|card_id|stage"`, the kind and card id filtered to
    `[A-Za-z0-9_.:-]` and the stage to the same alphabet **plus the space**
    (`_clean_stage`: a stage is free text a person typed), each bounded. An
    unknown `kind` returns `""`, which every caller treats as "add nothing" —
    an unstamped spawn is byte-identical to the spawn this change found.
    """
    if kind not in KINDS:
        return ""
    return "|".join(
        (kind, _clean(card_id), _clean_stage(stage)))[:MAX_STAMP_CHARS]


def parse(raw: str) -> dict:
    """`{"by", "card_id", "stage"}` for a stamp this module could have made.

    Fails **closed**: `{}` for an empty string, for anything that is not
    exactly three `|`-separated fields, for an unknown kind, for an over-long
    string and for any field carrying a character outside its own safe
    alphabet (the stage's admits a space, the other two do not). Nothing is
    repaired — a stamp that does not round-trip is not one.
    """
    text = str(raw or "")
    if not text or len(text) > MAX_STAMP_CHARS:
        return {}
    parts = text.split("|")
    if len(parts) != 3:
        return {}
    kind, card_id, stage = parts
    if kind not in KINDS:
        return {}
    if _clean(card_id) != card_id or _clean_stage(stage) != stage:
        return {}
    return {"by": kind, "card_id": card_id, "stage": stage}


def env_with(base, stamp_value: str = "") -> dict:
    """A copy of `base` carrying the stamp, or a plain copy for an empty one.

    Never mutates `base` — the caller is routinely handing in `os.environ`.
    """
    out = dict(base or {})
    if stamp_value:
        out[ENV_VAR] = str(stamp_value)[:MAX_STAMP_CHARS]
    return out


#: What each kind of press was for, in the words a person reads on the row.
_VERBS = {
    "card-start": "to build",
    "card-refine": "to plan",
    "card-consult": "to answer a question about",
}

#: The kinds that name no card at all, and so are a whole sentence rather than
#: a verb waiting for a title.
_WHOLE_LINES = {
    "adhoc": "Dark Army opened this terminal for you",
    "mission": "Dark Army opened this terminal as Mission Control",
    "review": "Dark Army opened this terminal to review a project",
}


def sentence(by: str, card_title: str = "", stage: str = "") -> str:
    """The one line both clients draw **verbatim**, or `""`.

    Composed here, on the daemon, for `queue_reason`'s reason: two surfaces
    each re-deriving a sentence is two surfaces that can disagree about what
    happened. The card **id** is deliberately never in it — an id is not a
    thing a person recognises, and the row already carries the id in
    `origin_card` for anything that needs to route.

    The stage rides in brackets when there is one, **as it was written**: a
    stage that does not survive `_clean_stage` untouched is dropped rather
    than repaired, because a half-mangled trade name ("securityreview") reads
    as a fault in Dark Army rather than as the words somebody actually typed.
    """
    key = str(by or "")
    whole = _WHOLE_LINES.get(key)
    if whole:
        return whole[:MAX_LINE_CHARS]
    verb = _VERBS.get(key)
    if not verb:
        return ""
    title = " ".join(str(card_title or "").split())
    what = title or "a board card"
    line = f"Dark Army started this {verb} {what}"
    role = str(stage or "")
    if role and _clean_stage(role) == role:
        line += f" ({role})"
    return line[:MAX_LINE_CHARS]
