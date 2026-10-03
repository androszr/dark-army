"""Review runs — the pure half (docs/review-runs.md).

A review run is a chore, not a card: pick a project, a provider and the
after-steps, press Start, read the findings as a checklist, tick the fixes,
press Continue, and one terminal fixes exactly those and performs exactly the
ticked steps. This module holds every *decision* about that: the closed lists
of steps, the two parsers, the scope sentence, the prompt and the Continue
line, the record's shape and its bounds. It opens no pty, runs no git and
holds no loop state; `daemon_review.ReviewVerbsMixin` is the verbs.

Pure module: stdlib plus `paths`. **Nothing here is ever written under a
project** — the run folder is `paths.REVIEW_RUNS_DIR / <run id>`: Dark Army
writes `run.json` at Start and `picks.json` at Continue; the assistant writes
`findings.md` and `steps.md` there (a known exact path).

**The tick is the permission.** `prompt` names the ticked steps as the only
authorised ones; an unticked person-only step (commit, push, install,
restart, ship) is stated as not authorised.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from . import paths

logger = logging.getLogger("dark-army.review")

STATES = ("reviewing", "picks", "fixing", "done", "exited", "ended")
#: States in which the run still occupies a project (one live run per root).
LIVE_STATES = ("reviewing", "picks", "fixing")

GRADES = ("BLOCK", "FIX", "WARN", "NOTE")

MAX_RUNS = 20
MAX_PUBLISHED_RUNS = 8
MAX_FINDINGS = 40
MAX_LINE_CHARS = 200
MAX_FIX_CHARS = 400
MAX_LEDGER_LINES = 24
MAX_STEPS = 12

FINDINGS_NAME = "findings.md"
PICKS_NAME = "picks.json"
STEPS_NAME = "steps.md"
RUN_NAME = "run.json"

#: How long a finished run stays listed (and its folder kept) so the reports
#: can be read, and the longest a run record is kept at all.
KEEP_FINISHED_SECONDS = 7 * 24 * 3600.0

_RESTART_HOW = (
    "osascript -e 'quit app \"Dark Army\"', wait until the app's pid is gone "
    "(up to 15 seconds; then kill -TERM the 'Dark Army.app/Contents/MacOS/"
    "Dark Army' parent), sleep 2, open -a \"/Applications/Dark Army.app\", "
    "then wait for curl -s http://127.0.0.1:19874/api/state to answer. Never "
    "signal the pty broker: it holds this terminal and this run continues "
    "after the app comes back"
)

#: Dark Army's own five. Each is `{id, label, how}`.
OWN_STEPS = (
    {"id": "rebuild", "label": "Rebuild",
     "how": "cd host && ./build.sh --allow-untagged --install"},
    {"id": "restart", "label": "Restart", "how": _RESTART_HOW},
    {"id": "commit", "label": "Commit",
     "how": "make one commit of the working tree in the repository's "
            "whole-sentence style"},
    {"id": "push", "label": "Push to the remote",
     "how": "git push to the upstream branch"},
    {"id": "testflight", "label": "TestFlight",
     "how": "gh workflow run testflight.yml --ref main, then gh run list "
            "--workflow testflight.yml --limit 1 for the run URL, and write "
            "STEP testflight: started with that URL; if gh is absent the "
            "step is failed in words, never done"},
)

#: What a project with no profile offers. Byte-equal to
#: `dark_army_menubar.review_steps.GENERIC_STEPS` (pinned by test_review_run).
GENERIC_STEPS = (
    {"id": "commit", "label": "Commit",
     "how": "make one commit of the working tree in the repository's "
            "whole-sentence style"},
    {"id": "push", "label": "Push to the remote",
     "how": "git push to the upstream branch"},
)

_STEP_ID_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,23}\Z")


# --- the offer --------------------------------------------------------------

def _clean_step(raw) -> Optional[dict]:
    """One `{id, label, how}` from a dict or an `[id, label, how]` triple;
    None when it is not a well-formed step."""
    if isinstance(raw, dict):
        sid, label, how = raw.get("id"), raw.get("label"), raw.get("how")
    elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
        sid, label, how = raw[0], raw[1], raw[2]
    else:
        return None
    if not isinstance(sid, str) or not _STEP_ID_RE.match(sid):
        return None
    if not isinstance(how, str) or not how.strip():
        return None
    label = label if isinstance(label, str) and label.strip() else sid
    return {"id": sid, "label": label.strip()[:MAX_LINE_CHARS],
            "how": how.strip()[:MAX_FIX_CHARS * 2]}


def steps_for(*, own: bool, after_steps, has_upstream: bool) -> list:
    """The after-steps a project offers: Dark Army's own five for its own
    checkout; else the pack ledger row's `after_steps`; else the generic
    commit and push. `push` is dropped without an upstream. Ids are unique."""
    if own:
        base = [dict(s) for s in OWN_STEPS]
    else:
        base = []
        for raw in after_steps if isinstance(after_steps, list) else []:
            step = _clean_step(raw)
            if step is not None:
                base.append(step)
        if not base:
            base = [dict(s) for s in GENERIC_STEPS]
    seen: set = set()
    out = []
    for step in base[:MAX_STEPS]:
        if step["id"] in seen:
            continue
        if step["id"] == "push" and not has_upstream:
            continue
        seen.add(step["id"])
        out.append(step)
    return out


def scope_for(upstream: str, ahead: int, changed: int) -> tuple:
    """`(scope, line)`: `remote` with a sentence naming the commits ahead and
    the changed files, or `working-tree` when there is no remote branch."""
    upstream = str(upstream or "")
    ahead = max(0, int(ahead or 0))
    changed = max(0, int(changed or 0))
    if not upstream:
        return "working-tree", "no remote branch, uncommitted changes only"
    commits = f"{ahead} commit" + ("" if ahead == 1 else "s")
    files = f"{changed} changed file" + ("" if changed == 1 else "s")
    return "remote", f"everything not on the remote branch, {commits} ahead and {files}"


def pty_name(run_id: str) -> str:
    """The terminal's name: longer than any card's `title[:40]` and carrying
    the run id, so End's identity test can tell it from every other pty."""
    return f"review {run_id} — Dark Army's review run"


def folder_for(run_id: str) -> Path:
    return Path(str(paths.REVIEW_RUNS_DIR)) / str(run_id)


# --- the parsers ------------------------------------------------------------

_GRADE_RE = re.compile(
    r"\A\s*(?:[-*#>\s]*)(?:\*\*)?(BLOCK|FIX|WARN|NOTE)(?:\*\*)?\s*[·\-—:|]\s*(.+?)\s*\Z")
#: The report's closing "Do next" list repeats FIX lines; it is not findings.
_DO_NEXT_RE = re.compile(r"\A\s*[#*>\s]*Do next\b", re.IGNORECASE)
_FIELD_RE = re.compile(
    r"\A\s*(?:[-*]\s*)?(?:\*\*)?(Why it matters|Where|Fix|Confidence)"
    r"(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.*?)\s*\Z", re.IGNORECASE)
_VERDICT_RE = re.compile(r"\A\s*VERDICT:\s*(SHIP|STOP)\b", re.IGNORECASE)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def parse_findings(text: str) -> Optional[dict]:
    """The report `/review` writes (`SKILL.md`, step 6) as
    `{verdict, findings: [{index, grade, line, where, fix, confidence}],
    truncated}`, or None when the first non-blank line is not `VERDICT:`.
    Indices are 1-based in file order; every text is clamped; past
    `MAX_FINDINGS` the list stops and `truncated` is True."""
    lines = str(text or "").splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    m = _VERDICT_RE.match(first)
    if not m:
        return None
    verdict = m.group(1).upper()
    cands: list = []
    current: Optional[dict] = None
    for ln in lines[lines.index(first) + 1:]:
        g = _GRADE_RE.match(ln)
        if g:
            current = {"grade": g.group(1),
                       "line": _clip(g.group(2), MAX_LINE_CHARS),
                       "where": "", "fix": "", "confidence": "",
                       "_fields": False}
            cands.append(current)
            continue
        if _DO_NEXT_RE.match(ln) or ln.lstrip().startswith("#"):
            # A heading ends the block above it; what follows is not part of
            # it (the "Do next" list repeats FIX lines with no fields).
            current = None
            continue
        if current is None:
            continue
        f = _FIELD_RE.match(ln)
        if not f:
            continue
        current["_fields"] = True
        name = f.group(1).lower()
        value = f.group(2)
        if name == "where":
            current["where"] = _clip(value, MAX_LINE_CHARS)
        elif name == "fix":
            current["fix"] = _clip(value, MAX_FIX_CHARS)
        elif name == "confidence":
            current["confidence"] = _clip(value, 24)
    # A grade line is a finding only when a field line follows it: a summary
    # bullet list, or the closing "Do next" list, is not findings and must
    # not shift the numbering.
    real = [c for c in cands if c.pop("_fields")]
    truncated = len(real) > MAX_FINDINGS
    findings = []
    for n, c in enumerate(real[:MAX_FINDINGS], 1):
        findings.append({"index": n, **c})
    return {"verdict": verdict, "findings": findings, "truncated": truncated}


_STEP_LINE_RE = re.compile(
    r"\A\s*STEP\s+([a-z][a-z0-9_-]{0,23})\s*:\s*"
    r"(done|failed|skipped|started)\b\s*(?:[—\-:]\s*)?(.*?)\s*\Z",
    re.IGNORECASE)


def parse_steps(text: str) -> dict:
    """`{lines: [{id, status, words}], done}` from `STEP <id>: done|failed|
    skipped|started — <words>` lines; a later line for the same id replaces
    an earlier one in place (`started` then `done` reads as `done`). `done`
    is True when a line reading `DONE` follows. Bounded by
    `MAX_LEDGER_LINES`."""
    order: list = []
    by_id: dict = {}
    done = False
    for ln in str(text or "").splitlines():
        if ln.strip().upper() == "DONE":
            done = True
            continue
        m = _STEP_LINE_RE.match(ln)
        if not m:
            continue
        sid = m.group(1).lower()
        row = {"id": sid, "status": m.group(2).lower(),
               "words": _clip(m.group(3), MAX_LINE_CHARS)}
        if sid not in by_id:
            if len(order) >= MAX_LEDGER_LINES:
                continue
            order.append(sid)
        by_id[sid] = row
    return {"lines": [by_id[s] for s in order], "done": done}


# --- the words the terminal is given ---------------------------------------

def authorised_block(steps) -> str:
    """The ticked steps, each with its `how`, in order."""
    out = []
    for i, step in enumerate(steps or [], 1):
        out.append(f"{i}. {step['id']} — {step['label']}: {step['how']}")
    return "\n".join(out)


_PERSON_ONLY = ("commit", "push", "rebuild", "restart", "testflight")


def prompt(run: dict) -> str:
    """The first user turn. Leads with `/review remote` (so
    `dispatch.prompt_refusal` reads it as a slash command), then the
    `Dark Army review run:` block."""
    folder = f"~/.dark-army/review-runs/{run['id']}/"
    steps = list(run.get("steps") or [])
    ticked = {s["id"] for s in steps}
    lines = [
        "/review remote",
        "",
        "Dark Army review run:",
        f"- Run folder: {folder}",
        f"- Scope: {run.get('scope_line') or ''}",
        f"- Write `{FINDINGS_NAME}` in that folder in the report's own format "
        "(VERDICT line first, then the graded findings) and end the turn; "
        "do not ask anything, Dark Army shows the checklist.",
        "- Then wait for the Continue line from Dark Army. Do nothing else "
        "until it arrives.",
        f"- When it arrives, read `{PICKS_NAME}` in the same folder, fix the "
        "findings it lists and no others.",
    ]
    if steps:
        lines += ["- Then perform exactly these authorised steps, in order:",
                  authorised_block(steps)]
    else:
        lines += ["- No after-steps are authorised."]
    lines += [
        "- Any step not listed here is not authorised: do not commit, push, "
        "install, restart or ship.",
    ]
    unticked = [s for s in _PERSON_ONLY if s not in ticked]
    if unticked:
        lines.append("- Not authorised in this run: " + ", ".join(unticked) + ".")
    lines += [
        f"- After each step append `STEP <id>: done|failed|skipped|started — "
        f"<one line>` to `{STEPS_NAME}` in the run folder. Once the fixes and "
        f"any steps are complete, always write `DONE` as the last line of "
        f"`{STEPS_NAME}`, even when no step was authorised.",
        "- `picks.json` lists each picked finding's number, grade and line "
        "under `fixes`; match the picks on that text, not on the number alone.",
        "- Never signal the pty broker; a restart keeps this terminal alive.",
    ]
    return "\n".join(lines)


def continue_line(run: dict, picks) -> str:
    """One printable line typed into the terminal at Continue. It names the
    picks file rather than the picks, and says no others."""
    n = len(list(picks or []))
    folder = f"~/.dark-army/review-runs/{run['id']}/"
    tail = (f"then perform the authorised steps in order and write a line per "
            f"step to {STEPS_NAME}" if run.get("steps")
            else "and perform no other step")
    line = (f"Dark Army: continue. Read {folder}{PICKS_NAME}, fix those "
            f"{n} finding{'' if n == 1 else 's'} and no others (match on the "
            f"grade and line text), {tail}, then write DONE last in "
            f"{STEPS_NAME}.")
    return "".join(c if 32 <= ord(c) < 127 or ord(c) > 160 else " "
                   for c in line)


def drift_key(run: dict) -> tuple:
    """What a client would redraw for this run: state, finding and ledger
    counts, picks and the end stamp."""
    return (str(run.get("state") or ""), len(run.get("findings") or []),
            len(run.get("ledger") or []), tuple(run.get("picks") or ()),
            str(run.get("session_id") or ""), bool(run.get("ended_at")),
            float(run.get("finished_at") or 0.0), str(run.get("error") or ""))


def digest_of(findings) -> str:
    raw = json.dumps(findings or [], sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# --- the record --------------------------------------------------------------

RECORD_DEFAULTS = {
    "id": "", "root": "", "project": "", "tool": "", "state": "reviewing",
    "scope": "", "scope_line": "", "upstream": "", "handle": "",
    "session_id": "", "started_at": 0.0, "findings_at": 0.0,
    "decided_at": 0.0, "finished_at": 0.0, "verdict": "", "error": "",
    "findings_digest": "", "truncated": False,
}


def new_record(*, run_id: str, root: str, project: str, tool: str, scope: str,
               scope_line: str, upstream: str, steps: list, now: float) -> dict:
    rec = dict(RECORD_DEFAULTS)
    rec.update({"id": run_id, "root": root, "project": project, "tool": tool,
                "scope": scope, "scope_line": scope_line,
                "upstream": upstream, "started_at": float(now)})
    rec["steps"] = [dict(s) for s in steps]
    rec["findings"] = []
    rec["picks"] = []
    rec["ledger"] = []
    return rec


def _normalise(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    out = dict(raw)
    for key, default in RECORD_DEFAULTS.items():
        value = out.get(key)
        if isinstance(default, bool):
            out[key] = value if isinstance(value, bool) else default
        elif isinstance(default, float):
            try:
                out[key] = float(value) if value is not None else default
            except (TypeError, ValueError):
                out[key] = default
        else:
            out[key] = value if isinstance(value, str) else default
    for key in ("steps", "findings", "ledger"):
        value = out.get(key)
        out[key] = [v for v in value if isinstance(v, dict)] \
            if isinstance(value, list) else []
    picks = out.get("picks")
    out["picks"] = [int(p) for p in picks if isinstance(p, int)
                    and not isinstance(p, bool)] \
        if isinstance(picks, list) else []
    if not out["id"]:
        return None
    if out["state"] not in STATES:
        out["state"] = "exited"
    return out


def load_records(path) -> list:
    """The records at `path`: unknown keys kept, missing keys defaulted, a
    corrupt or missing file `[]` — never a raise (this runs in `__init__`)."""
    try:
        raw = Path(str(path)).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("could not read %s", path, exc_info=True)
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("ignoring corrupt %s", path)
        return []
    items = data.get("runs") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        rec = _normalise(item)
        if rec is not None:
            out.append(rec)
    return out[-MAX_RUNS:]


def save_records(path, records: list) -> None:
    """Write atomically at 0600. `OSError` is logged, never raised."""
    path = Path(str(path))
    try:
        paths.ensure_state_dir()
        os.makedirs(path.parent, exist_ok=True)
        paths.atomic_write_json(path, {"version": 1, "runs": list(records or [])},
                                mode=0o600, indent=2)
    except OSError:
        logger.warning("could not write %s", path, exc_info=True)


def prune(records: list, now: float) -> tuple:
    """`(kept, dropped)`: finished runs older than `KEEP_FINISHED_SECONDS`
    are dropped, then the oldest past `MAX_RUNS`; live runs are never
    dropped."""
    kept = []
    dropped = []
    for rec in records:
        finished = float(rec.get("finished_at") or 0.0)
        if rec.get("state") not in LIVE_STATES and finished \
                and now - finished > KEEP_FINISHED_SECONDS:
            dropped.append(rec)
        else:
            kept.append(rec)
    while len(kept) > MAX_RUNS:
        victim = next((r for r in kept if r.get("state") not in LIVE_STATES),
                      None)
        if victim is None:
            break
        kept.remove(victim)
        dropped.append(victim)
    return kept, dropped
