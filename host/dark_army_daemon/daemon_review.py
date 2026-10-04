"""The Review section's verbs — `BobDaemon` inherits `ReviewVerbsMixin`
beside `BoardVerbsMixin` (docs/review-runs.md).

A review run is a chore with no card: `start_review` opens the chosen
provider on Dark Army's own pty with a prompt naming the scope, the ticked
steps (the only authorised ones) and the run folder; the assistant writes
`findings.md` there; `_observe_review_runs` reads it and the run waits on
the person's picks; `continue_review` writes `picks.json` and types one line;
the assistant fixes exactly those, performs exactly the ticked steps and
appends a `STEP` line per step to `steps.md`.

This module must not import `daemon` at import time (`daemon.py` imports it
to build the class), exactly as `daemon_board.py` states. Every read of
`self._pty` is loop-only: the executor pass is handed `pty_facts`, composed
on the loop by `_review_compose_facts`.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from . import dispatch
from . import enrollment
from . import origin
from . import paths
from . import review_run
from . import scout_report
from . import work_record

logger = logging.getLogger("dark-army.review")

# Held across `_review_publish`'s build and rebind (see there).
_PUBLISH_LOCK = threading.Lock()

DISPATCH_OFF_REFUSAL = "Dark Army is not allowed to start sessions (see the ⋯ menu)"
REVIEW_BUSY_REFUSAL = "a review of this project is already running"
REVIEW_STEPS_REFUSAL = "that step is not one this project offers"
REVIEW_NOT_PICKS_REFUSAL = "this review is not waiting on your picks"
REVIEW_PICK_REFUSAL = "that finding is not on the list"
REVIEW_GONE_REFUSAL = "no such review"
REVIEW_ENDED_REFUSAL = "that review has already ended"
REVIEW_CLOSE_FAILED_REFUSAL = ("Dark Army could not confirm the review's "
                               "terminal closed — try End again")
REVIEW_HOST_DOWN_REFUSAL = ("the terminal host is not reachable right now — "
                            "try End again in a moment")
REVIEW_NOT_BOUND_REFUSAL = ("the review's session has not connected yet — "
                            "press Continue again in a moment")
#: How long a run may hold no terminal handle after Start before it is read
#: as exited (the CLI died at spawn and nothing wears the run's name).
HANDLE_BIND_SECONDS = 30.0
REVIEW_IDENTITY_REFUSAL = "that terminal is not this review's"
REVIEW_TERMINAL_GONE_REFUSAL = "this review's terminal has ended"
REVIEW_ROOT_REFUSAL = "that folder is not one of Dark Army's projects"

_RUN_ID_RE = re.compile(r"\A[0-9a-f]{8}\Z")
_PTY_NAME_RE = re.compile(r"\Areview ([0-9a-f]{8}) — Dark Army's review run\Z")


def _porcelain_count(raw: bytes) -> int:
    """How many changed or untracked paths a `status --porcelain -z` output
    names (a rename or copy carries its source as one more field)."""
    count = 0
    fields = raw.split(b"\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        count += 1
        if entry[:1] in (b"R", b"C") or entry[1:2] in (b"R", b"C"):
            i += 1
    return count


class ReviewVerbsMixin:
    """State it expects on the instance (set by `BobDaemon.__init__`):
    `_review_runs` (list of records), `_review_launches`, `_review_attempt`,
    `_review_seen`, `_review_lock`, `_review_busy`, `_review_pty_facts`."""

    # --- the offer -------------------------------------------------------

    def _pack_after_steps(self, canonical: str) -> list:
        """The pack ledger row's `after_steps` for `canonical`, `[]` when the
        project has none. Stdlib JSON, memoised on (mtime_ns, size): the
        daemon imports nothing from the menu-bar package for this."""
        path = Path(str(paths.AGENT_PACK_PATH))
        try:
            st = path.stat()
        except OSError:
            return []
        key = (st.st_mtime_ns, st.st_size)
        memo = getattr(self, "_review_pack_memo", None)
        if memo is not None and memo[0] == key:
            data = memo[1]
        else:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                return []
            self._review_pack_memo = (key, data)
        rows = data.get("projects") if isinstance(data, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            if dispatch.normalise_root(str(row.get("root") or "")) == canonical:
                steps = row.get("after_steps")
                return list(steps) if isinstance(steps, list) else []
        return []

    def review_offer(self, root) -> dict:
        """What a review of `root` would cover and may do afterwards.
        **Blocking** (three git reads); executor only. `available` is False
        with a `reason` in words for a folder Dark Army does not watch."""
        canonical = dispatch.normalise_root(str(root or ""))
        out = {"available": False, "root": canonical, "project": "",
               "scope": "", "scope_line": "", "upstream": "", "ahead": 0,
               "changed": 0, "steps": [], "reason": ""}
        if not canonical:
            out["reason"] = "no folder named"
            return out
        enrolled = {dispatch.normalise_root(r)
                    for r in enrollment.enrolled_roots() if r}
        if canonical not in enrolled:
            out["reason"] = REVIEW_ROOT_REFUSAL
            return out
        out["project"] = enrollment.enrolled_label(canonical)
        ok, raw, _why = self._git_blocking(
            work_record.argv_upstream(canonical), canonical)
        upstream = raw.decode("utf-8", "replace").strip() if ok else ""
        ahead = 0
        if upstream:
            ok, raw, _why = self._git_blocking(
                work_record.argv_ahead(canonical, upstream), canonical)
            try:
                ahead = int(raw.decode("utf-8", "replace").strip()) if ok else 0
            except ValueError:
                ahead = 0
        ok, raw, _why = self._git_blocking(
            work_record.argv_status(canonical), canonical)
        changed = _porcelain_count(raw) if ok else 0
        scope, line = review_run.scope_for(upstream, ahead, changed)
        from .daemon_board import _find_own_checkout
        own_root = _find_own_checkout()
        own = bool(own_root) and dispatch.normalise_root(own_root) == canonical
        steps = review_run.steps_for(
            own=own, after_steps=self._pack_after_steps(canonical),
            has_upstream=bool(upstream))
        out.update({"available": True, "scope": scope, "scope_line": line,
                    "upstream": upstream, "ahead": ahead, "changed": changed,
                    "steps": steps})
        return out

    # --- records -----------------------------------------------------------

    def _review_find(self, run_id: str) -> Optional[dict]:
        for rec in self._review_runs:
            if rec.get("id") == run_id:
                return rec
        return None

    def _review_save(self) -> None:
        """Write the records. Safe on the loop or the executor; the path is
        read off the module at call time so a test can point it elsewhere."""
        with self._review_lock:
            snapshot = [dict(r) for r in self._review_runs]
        review_run.save_records(paths.REVIEW_RUNS_PATH, snapshot)

    def _review_set(self, run_id: str, expect_state: Optional[str] = None,
                    **fields) -> bool:
        """Set fields on one record under the lock; with `expect_state` the
        write lands only when the record still wears that state (the
        executor pass never overrides a press that landed first)."""
        with self._review_lock:
            rec = self._review_find(run_id)
            if rec is None:
                return False
            if expect_state is not None and rec.get("state") != expect_state:
                return False
            rec.update(fields)
        # Outside the lock (`review_snapshot` takes it): the alert gate and
        # the Live Activity read the published list, which must move with
        # the writer, not only when a client happens to ask for `state()`.
        self._review_publish()
        return True

    def _review_notify(self) -> None:
        """Announce the moved section: `state()` carries it. Loop only."""
        try:
            self._notify_board()
        except Exception:
            logger.debug("review notify failed", exc_info=True)

    def _review_write_file(self, run_id: str, name: str, obj: dict) -> None:
        folder = review_run.folder_for(run_id)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        paths.atomic_write_json(folder / name, obj, mode=0o600, indent=2)

    # --- Start -------------------------------------------------------------

    async def start_review(self, root, tool, steps) -> tuple:
        """Open a review run. `(ok, detail)`; `detail` is the run id on
        success (loopback and the doors alike: it is not a handle).

        `open_adhoc_terminal`'s shape, under `_dispatch_lock` for the whole
        body, through `dispatch.adhoc_guard` (the card-less sibling of
        `guard`: same refusal constants, cooldown, bounds and folder test) so
        the one-per-project launch bound holds against `dispatch.guard` in
        both directions through `_launch_inflight`."""
        if not self.board_dispatch_enabled:
            return False, DISPATCH_OFF_REFUSAL
        if not isinstance(steps, list) or any(
                not isinstance(s, str) for s in steps):
            return False, REVIEW_STEPS_REFUSAL
        async with self._dispatch_lock:
            loop = asyncio.get_running_loop()
            offer = await loop.run_in_executor(None, self.review_offer, root)
            canonical = offer["root"]
            if not offer["available"]:
                return False, offer["reason"] or REVIEW_ROOT_REFUSAL
            label = offer["project"]
            ticked = set(steps)
            offered = {s["id"] for s in offer["steps"]}
            if not ticked <= offered:
                return False, REVIEW_STEPS_REFUSAL
            chosen = [s for s in offer["steps"] if s["id"] in ticked]
            with self._review_lock:
                mine = [dict(r) for r in self._review_runs
                        if r.get("root") == canonical]
            # A `done` run whose terminal is still open still has an
            # assistant in this working tree: a second run would share it.
            busy = any(r.get("state") in review_run.LIVE_STATES
                       or (r.get("state") == "done"
                           and self._review_terminal_open(r))
                       for r in mine)
            if busy:
                return False, REVIEW_BUSY_REFUSAL
            roots = {dispatch.normalise_root(r)
                     for r in await loop.run_in_executor(
                         None, enrollment.enrolled_roots) if r}
            roots.discard("")
            all_cards = (await self._board_call("cards") or []
                         if self._board is not None else [])
            ok, detail = dispatch.adhoc_guard(
                root=canonical, project=label, roots=roots,
                in_flight=self._launch_inflight(all_cards),
                tool=str(tool or ""), now=time.time(),
                last_attempt=self._review_attempt or None)
            if not ok:
                return False, detail
            executable = await loop.run_in_executor(
                None, dispatch.resolve_executable, str(tool))
            if not executable:
                return False, dispatch.NOT_INSTALLED_REFUSAL.format(tool=tool)
            run_id = secrets.token_hex(4)
            now = time.time()
            rec = review_run.new_record(
                run_id=run_id, root=canonical, project=label, tool=str(tool),
                scope=offer["scope"], scope_line=offer["scope_line"],
                upstream=offer["upstream"], steps=chosen, now=now)
            text = review_run.prompt(rec)
            refusal = dispatch.prompt_refusal(str(tool), text)
            if refusal:
                return False, refusal
            try:
                await loop.run_in_executor(None, functools.partial(
                    self._review_write_file, run_id, review_run.RUN_NAME, {
                        "version": 1, "id": run_id,
                        "steps": [s["id"] for s in chosen],
                        "scope_line": offer["scope_line"],
                        "started_at": now}))
            except OSError:
                logger.warning("could not write the review folder",
                               exc_info=True)
                return False, "Dark Army could not write the review folder"
            argv = dispatch.argv_for(
                str(tool), executable, text,
                self._agent_model_for(canonical, str(tool), "main"),
                self._agent_effort_for(canonical, str(tool), "main"))
            spawned, spawn_detail, pid = await dispatch.spawn_local(
                canonical, argv, review_run.pty_name(run_id),
                stamp=origin.stamp("review", run_id))
            if not spawned:
                await loop.run_in_executor(
                    None, self._review_remove_folder, run_id)
                return False, spawn_detail
            self._review_attempt = now
            handle = self._pty.owns(pid) or self._review_named_handle(run_id)
            rec["handle"] = handle
            with self._review_lock:
                self._review_runs.append(rec)
                kept, dropped = review_run.prune(self._review_runs, now)
                self._review_runs[:] = kept
            for gone in dropped:
                await loop.run_in_executor(
                    None, self._review_remove_folder, str(gone.get("id")))
            self._review_launches[run_id] = {
                "project": label, "root": canonical, "handle": handle,
                "at": now}
            await loop.run_in_executor(None, self._review_save)
            self._review_notify()
            logger.info("opened a %s review run %s in %s", tool, run_id,
                        canonical)
            return True, run_id

    def _review_remove_folder(self, run_id: str) -> None:
        """Remove one run's folder, only a direct child of the runs folder
        whose name is a run id."""
        if not _RUN_ID_RE.match(str(run_id or "")):
            return
        folder = review_run.folder_for(run_id)
        try:
            if folder.parent == Path(str(paths.REVIEW_RUNS_DIR)) \
                    and folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            logger.debug("could not remove %s", folder, exc_info=True)

    def _review_named_handle(self, run_id: str) -> str:
        """The handle of the one live terminal wearing `pty_name(run_id)`,
        else `""` (also when two wear it: never a guess). Loop only. The
        fallback for a spawn whose pid `owns` could not place."""
        name = review_run.pty_name(run_id)
        hits = [t for t in self._pty.terminals()
                if getattr(t, "name", "") == name
                and not getattr(t, "exited", False)]
        return str(hits[0].handle) if len(hits) == 1 else ""

    def _review_terminal_open(self, rec: dict) -> bool:
        """Whether the run's own terminal is still open: the recorded handle,
        else the one wearing its name. Loop only."""
        handle = str(rec.get("handle") or "") \
            or self._review_named_handle(str(rec.get("id") or ""))
        term = self._pty.get(handle) if handle else None
        return term is not None and not getattr(term, "exited", False)

    def _review_resolve_handle(self, run_id: str, rec: dict) -> str:
        """The record's handle, or - when it holds none - the terminal found
        by name, written back so every later read has it. Loop only."""
        handle = str(rec.get("handle") or "")
        if handle:
            return handle
        handle = self._review_named_handle(run_id)
        if handle:
            self._review_set(run_id, handle=handle)
            # The save is a file write: hand it to the executor, never the
            # loop this runs on.
            asyncio.get_running_loop().run_in_executor(None, self._review_save)
        return handle

    # --- Continue ----------------------------------------------------------

    async def continue_review(self, run_id, fix) -> tuple:
        """The second deliberate press: write the picks, type one line."""
        run_id = str(run_id or "")
        with self._review_lock:
            rec = self._review_find(run_id)
            rec = dict(rec) if rec is not None else None
        if rec is None:
            return False, REVIEW_GONE_REFUSAL
        if rec["state"] == "ended":
            return False, REVIEW_ENDED_REFUSAL
        if rec["state"] != "picks" or run_id in self._review_busy:
            return False, REVIEW_NOT_PICKS_REFUSAL
        indices = {int(f.get("index") or 0) for f in rec["findings"]}
        if not isinstance(fix, list) or any(
                isinstance(i, bool) or not isinstance(i, int) or i not in indices
                for i in fix):
            return False, REVIEW_PICK_REFUSAL
        fix = sorted(set(fix))
        handle = self._review_resolve_handle(run_id, rec)
        term = self._pty.get(handle) if handle else None
        if term is None or term.exited:
            return False, REVIEW_TERMINAL_GONE_REFUSAL
        session_id = str(term.session_id or rec.get("session_id") or "")
        if not session_id and rec.get("tool") != "codex":
            # No session to look a permission prompt up by: the line plus
            # Enter could land in an open approval dialog. Wait for the bind;
            # Codex alone is typed by handle (its prompts are never tracked).
            return False, REVIEW_NOT_BOUND_REFUSAL
        self._review_busy.add(run_id)
        try:
            loop = asyncio.get_running_loop()
            now = time.time()
            skip = sorted(indices - set(fix))
            await loop.run_in_executor(None, functools.partial(
                self._review_write_file, run_id, review_run.PICKS_NAME, {
                    "version": 1, "fix": fix, "skip": skip,
                    "fixes": [{"index": f["index"], "grade": f["grade"],
                               "line": f["line"]}
                              for f in rec["findings"]
                              if f.get("index") in fix],
                    "decided_at": now}))
            ok, why = await self._terminal_line_input(
                handle, review_run.continue_line(rec, fix), session_id)
            if not ok:
                return False, why
            if not self._review_set(run_id, expect_state="picks",
                                    state="fixing", decided_at=now, picks=fix):
                # The run left `picks` while the line was typed (its
                # terminal exited, or End): not a success.
                return False, REVIEW_NOT_PICKS_REFUSAL
            await loop.run_in_executor(None, self._review_save)
            self._review_notify()
            return True, ""
        finally:
            self._review_busy.discard(run_id)

    # --- End ---------------------------------------------------------------

    async def end_review(self, run_id) -> tuple:
        """Close the run's terminal and nothing else. The identity is
        re-checked at the moment it fires (`end_mission`'s rule): the
        terminal at the recorded handle must wear `pty_name(run_id)`."""
        run_id = str(run_id or "")
        async with self._dispatch_lock:
            with self._review_lock:
                rec = self._review_find(run_id)
                rec = dict(rec) if rec is not None else None
            if rec is None:
                return False, REVIEW_GONE_REFUSAL
            if rec["state"] == "ended":
                return False, REVIEW_ENDED_REFUSAL
            if not self._pty.connected:
                # Nothing can be looked up or closed: say so, never "ended".
                return False, REVIEW_HOST_DOWN_REFUSAL
            handle = self._review_resolve_handle(run_id, rec)
            term = self._pty.get(handle) if handle else None
            if term is not None and not term.exited:
                if term.name != review_run.pty_name(run_id):
                    return False, REVIEW_IDENTITY_REFUSAL
                if not await self._pty.close(handle):
                    logger.warning("review close unconfirmed (%s)", handle)
                    return False, REVIEW_CLOSE_FAILED_REFUSAL
            # Else the link is up and neither the handle nor the name finds a
            # live terminal: it is already gone, and the run is ended truly.
            finished = float(rec.get("finished_at") or 0.0) or time.time()
            self._review_set(run_id, state="ended", finished_at=finished)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._review_save)
            self._review_notify()
            logger.info("ended review run %s", run_id)
            return True, "ended"

    # --- observation -------------------------------------------------------

    def _review_compose_facts(self) -> dict:
        """`{handle: (exited, session_id)}` for every live run, and `None`
        for a handle the host no longer lists while the link is up. Loop
        only: the executor never touches the host. While the broker link is
        down nothing is said about any handle."""
        facts: dict = {}
        pty = getattr(self, "_pty", None)
        if pty is None or not pty.connected:
            return facts
        with self._review_lock:
            live = [(r["id"], dict(r)) for r in self._review_runs
                    if r.get("state") in review_run.LIVE_STATES]
        now = time.time()
        for rid, rec in live:
            handle = self._review_resolve_handle(rid, rec)
            if not handle:
                # No terminal found by handle or name: past the bind window
                # that is a run whose CLI never came up, reported as gone.
                if now - float(rec.get("started_at") or 0.0) \
                        > HANDLE_BIND_SECONDS:
                    facts["run:" + rid] = None
                continue
            term = pty.get(handle)
            facts[handle] = None if term is None else (
                bool(term.exited), str(term.session_id or ""))
        return facts

    def _observe_review_runs(self, pty_facts: dict) -> bool:
        """Read each live run's files and the loop's facts; move the state.
        **Executor only**; returns whether anything changed. Writes land
        only on a record still in the state this pass read it in."""
        facts = pty_facts or {}
        with self._review_lock:
            live = [dict(r) for r in self._review_runs
                    if r.get("state") in review_run.LIVE_STATES]
        changed = False
        now = time.time()
        for rec in live:
            run_id = rec["id"]
            state = rec["state"]
            folder = review_run.folder_for(run_id)
            fact = facts.get(rec.get("handle") or "run:" + run_id, "absent")
            if isinstance(fact, tuple) and fact[1] \
                    and fact[1] != rec.get("session_id"):
                changed |= self._review_set(run_id, expect_state=state,
                                            session_id=fact[1])
            if state == "reviewing":
                parsed = review_run.parse_findings(scout_report.read_text(
                    folder / review_run.FINDINGS_NAME))
                if parsed is not None:
                    if self._review_set(
                            run_id, expect_state="reviewing", state="picks",
                            findings_at=now, findings=parsed["findings"],
                            verdict=parsed["verdict"],
                            truncated=bool(parsed["truncated"]),
                            findings_digest=review_run.digest_of(
                                parsed["findings"])):
                        changed = True
                        state = "picks"
            elif state == "picks" and run_id not in self._review_busy:
                # The file can grow after its VERDICT line (an Edit adding
                # the rest): until a person decides, the checklist follows
                # it, and a new digest re-raises the Needs you entry.
                parsed = review_run.parse_findings(scout_report.read_text(
                    folder / review_run.FINDINGS_NAME))
                digest = review_run.digest_of(parsed["findings"]) \
                    if parsed is not None else ""
                if digest and digest != rec.get("findings_digest") \
                        and self._review_set(
                            run_id, expect_state="picks",
                            findings_at=now, findings=parsed["findings"],
                            verdict=parsed["verdict"],
                            truncated=bool(parsed["truncated"]),
                            findings_digest=digest):
                    changed = True
            elif state == "fixing":
                ledger = review_run.parse_steps(scout_report.read_text(
                    folder / review_run.STEPS_NAME))
                lines = ledger["lines"]
                settled = {ln["id"] for ln in lines
                           if ln["status"] != "started"}
                wanted = {s["id"] for s in rec.get("steps") or []}
                finished = ledger["done"] or (bool(wanted) and wanted <= settled)
                fields = {}
                if lines != rec.get("ledger"):
                    fields["ledger"] = lines
                if finished:
                    fields.update(state="done", finished_at=now)
                if fields and self._review_set(
                        run_id, expect_state="fixing", **fields):
                    changed = True
                    if finished:
                        state = "done"
            if state in review_run.LIVE_STATES and (
                    fact is None or (isinstance(fact, tuple) and fact[0])):
                if self._review_set(run_id, expect_state=state,
                                    state="exited", finished_at=now,
                                    error="terminal closed"):
                    changed = True
        if changed:
            self._review_save()
        return changed

    def _review_drifted(self) -> bool:
        """Buy a frame when any run's drawn facts moved since the last
        pass (`_manual_due_drifted`'s shape). Executor only."""
        with self._review_lock:
            keys = {r["id"]: review_run.drift_key(r)
                    for r in self._review_runs}
        last = self._review_seen
        changed = keys != last
        self._review_seen = keys
        return changed

    # --- the section -------------------------------------------------------

    def review_snapshot(self) -> dict:
        """The `review` section of `/api/state`. **Never a handle, a digest
        or a path to the run folder**; the newest `MAX_PUBLISHED_RUNS` runs
        with the bounded fields."""
        return self._review_publish()

    def _review_publish(self) -> dict:
        """Build the `review` section and rebind `_review_published` to its
        runs, whole. Called by `review_snapshot` and by the writer
        (`_review_set`, `_reconcile_review` on the executor), so the picks
        buzz and the Lock Screen card read a list that is current with no
        client connected. Build and rebind are one step under a publish lock,
        so an executor reconcile and a loop verb racing cannot land an older
        list last; `_review_lock` is taken inside it and never the other way
        round (`_review_set` publishes after releasing it)."""
        with _PUBLISH_LOCK:
            return self._review_publish_locked()

    def _review_publish_locked(self) -> dict:
        with self._review_lock:
            newest = sorted(self._review_runs,
                            key=lambda r: float(r.get("started_at") or 0.0),
                            reverse=True)
            # Every live run is always published; finished ones fill what is
            # left of the bound, newest first.
            live = [r for r in newest
                    if r.get("state") in review_run.LIVE_STATES]
            rest = [r for r in newest
                    if r.get("state") not in review_run.LIVE_STATES]
            room = max(0, review_run.MAX_PUBLISHED_RUNS - len(live))
            keep = {id(r) for r in live + rest[:room]}
            runs = [dict(r) for r in newest if id(r) in keep]
        out = []
        for rec in runs:
            ledger = {ln.get("id"): ln for ln in rec.get("ledger") or []}
            out.append({
                "id": rec["id"], "root": rec["root"],
                "project": rec["project"], "tool": rec["tool"],
                "state": rec["state"], "scope": rec["scope"],
                "scope_line": rec["scope_line"], "upstream": rec["upstream"],
                "session_id": rec["session_id"],
                "started_at": float(rec["started_at"]),
                "findings_at": float(int(rec["findings_at"])),
                "decided_at": float(rec["decided_at"]),
                "finished_at": float(rec["finished_at"]),
                "verdict": rec["verdict"], "error": rec["error"],
                "truncated": bool(rec.get("truncated")),
                "findings": [
                    {"index": int(f.get("index") or 0),
                     "grade": str(f.get("grade") or ""),
                     "line": str(f.get("line") or ""),
                     "where": str(f.get("where") or ""),
                     "fix": str(f.get("fix") or ""),
                     "confidence": str(f.get("confidence") or "")}
                    for f in rec.get("findings") or []],
                "picks": list(rec.get("picks") or []),
                "steps": [
                    {"id": s.get("id"), "label": s.get("label"),
                     "status": str((ledger.get(s.get("id")) or {})
                                   .get("status") or ""),
                     "words": str((ledger.get(s.get("id")) or {})
                                  .get("words") or "")}
                    for s in rec.get("steps") or []],
                "ledger": [
                    {"id": ln.get("id"), "status": ln.get("status"),
                     "words": ln.get("words")}
                    for ln in rec.get("ledger") or []],
            })
        # The published list, rebound whole: the alert gate (executor) and
        # the Live Activity (loop) read it lock-free for the picks buzz.
        self._review_published = list(out)
        return {"available": True, "runs": out}

    # --- the launch bound, the handle, restart -----------------------------

    def _review_inflight_entries(self) -> list:
        """`_launch_inflight`'s review leg: one pseudo-entry per run still
        binding, swept here as the ad-hoc entries are (spent when the
        terminal has gone, been named after a session, or the bind window
        ran out; an entry with no handle is held for the whole window).
        Loop only — it reads the host."""
        now = time.time()
        out = []
        for lid, entry in list(self._review_launches.items()):
            handle = str(entry.get("handle") or "")
            spent = now - float(entry.get("at") or 0.0) \
                > dispatch.DISPATCH_BIND_WINDOW
            if handle and not spent:
                term = self._pty.get(handle)
                spent = term is None or bool(term.session_id)
            if spent:
                self._review_launches.pop(lid, None)
                continue
            out.append({"id": f"review:{lid}",
                        "project": entry.get("project") or ""})
        return out

    def _review_handle_if_named(self, session_id: str) -> Optional[str]:
        """The recorded handle of the live run whose last-bound session is
        `session_id`, when the terminal there is alive and not named for a
        different session — else None. Resolves only, never binds."""
        if not session_id:
            return None
        with self._review_lock:
            recs = [dict(r) for r in self._review_runs
                    if r.get("session_id") == session_id]
        for rec in recs:
            handle = str(rec.get("handle") or "")
            term = self._pty.get(handle) if handle else None
            if term is None or term.exited:
                continue
            if term.name != review_run.pty_name(rec["id"]):
                continue
            if term.session_id and term.session_id != session_id:
                continue
            return handle
        return None

    def _adopt_review_terminals(self) -> None:
        """After the startup broker attach, before the first snapshot: a
        live run whose handle the host still holds is kept (and its launch
        entry re-created while the terminal is unnamed); a live run whose
        handle is gone becomes `exited`; a terminal wearing a run's name
        that no record names is adopted from the run's folder."""
        pty = getattr(self, "_pty", None)
        if pty is None:
            return
        now = time.time()
        changed = False
        with self._review_lock:
            known = {r["id"] for r in self._review_runs}
            live = [dict(r) for r in self._review_runs
                    if r.get("state") in review_run.LIVE_STATES]
        for rec in live:
            handle = str(rec.get("handle") or "")
            term = pty.get(handle) if handle else None
            if term is None or term.exited:
                if pty.connected:
                    changed |= self._review_set(
                        rec["id"], state="exited", finished_at=now,
                        error="terminal closed")
                continue
            if not term.session_id and now - float(
                    rec.get("started_at") or 0.0) \
                    < dispatch.DISPATCH_BIND_WINDOW:
                self._review_launches.setdefault(rec["id"], {
                    "project": rec.get("project") or "",
                    "root": rec.get("root") or "", "handle": handle,
                    "at": float(rec.get("started_at") or now)})
        for term in pty.terminals():
            m = _PTY_NAME_RE.match(str(getattr(term, "name", "") or ""))
            if not m or term.exited or m.group(1) in known:
                continue
            adopted = self._review_adopt_from_folder(m.group(1), term, now)
            if adopted is not None:
                with self._review_lock:
                    self._review_runs.append(adopted)
                changed = True
        if changed:
            self._review_save()

    def _review_adopt_from_folder(self, run_id: str, term, now: float):
        """A record rebuilt from `run.json` for a terminal no record names;
        None when the folder says nothing about it."""
        try:
            raw = json.loads((review_run.folder_for(run_id)
                              / review_run.RUN_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        known = {s["id"]: s for s in (*review_run.OWN_STEPS,
                                      *review_run.GENERIC_STEPS)}
        steps = []
        for sid in raw.get("steps") or []:
            if isinstance(sid, str):
                steps.append(dict(known.get(sid) or {
                    "id": sid, "label": sid, "how": ""}))
        root = dispatch.normalise_root(str(getattr(term, "root", "") or ""))
        rec = review_run.new_record(
            run_id=run_id, root=root,
            project=enrollment.enrolled_label(root), tool="",
            scope="", scope_line=str(raw.get("scope_line") or ""),
            upstream="", steps=steps,
            now=float(raw.get("started_at") or now))
        rec["handle"] = str(term.handle)
        rec["session_id"] = str(term.session_id or "")
        return rec


__all__ = ["ReviewVerbsMixin", "REVIEW_BUSY_REFUSAL", "REVIEW_STEPS_REFUSAL",
           "REVIEW_NOT_PICKS_REFUSAL", "REVIEW_PICK_REFUSAL",
           "REVIEW_GONE_REFUSAL", "REVIEW_ENDED_REFUSAL",
           "REVIEW_CLOSE_FAILED_REFUSAL", "REVIEW_IDENTITY_REFUSAL",
           "REVIEW_TERMINAL_GONE_REFUSAL", "DISPATCH_OFF_REFUSAL"]
