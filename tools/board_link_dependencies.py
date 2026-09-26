#!/usr/bin/env python3
"""Link a project's existing cards to the cards they wait on, from a list of
titles. A person's tool, run from a shell; stdlib only.

    python3 tools/board_link_dependencies.py --project ai-viber \\
        --map tools/dependency-maps/ai-viber.txt            # dry run
    python3 tools/board_link_dependencies.py --project ai-viber \\
        --map tools/dependency-maps/ai-viber.txt --apply    # write

The map is one line per card that waits on others:

    <dependent title> <- <dependency title> | <dependency title>

`#` starts a comment line and blank lines are skipped. Titles are matched
exactly — trimmed, case-insensitive — among the named project's cards on
the live board **at the moment this runs**, so no card id is ever written
into a map. Done cards are included (a finished card is a dependency that is
already met).

**Nothing is written unless everything resolves.** A title that matches no
card, or more than one, refuses the whole run before a single write, and
the default is a dry run that prints every link it would make with each
card's column and change number. With `--apply` each dependent is written
with one `board_update` carrying its whole list and the `expected_revision`
the dry run read, so a card edited in between is refused by the store
rather than overwritten; the store also refuses a loop, a self-wait and a
card in another project, in its own words, and those are printed as they
come. The list a line names **replaces** that card's list.

`--state-file` reads an `/api/state` body from disk instead of the live
board — the test seam, so no test ever reads a real board. Contract:
`docs/card-dependencies.md`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:19874"
DEFAULT_TOKEN_FILE = "~/.dark-army/api-token"
#: The store's own bound (`board.MAX_BLOCKERS`), restated: this script runs
#: with no package to import it from, and a longer list would be cut short
#: by the store without a word.
MAX_DEPENDENCIES = 8
ARROW = " <- "
SEPARATOR = " | "
TIMEOUT_SECONDS = 15
#: No proxy: a machine-wide `http_proxy` must not carry a loopback request —
#: and the token on it — away (`tools/first_run_walkthrough.py`'s opener).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
#: The only hosts `--base-url` may name. The token is Dark Army's own write
#: key; it is sent nowhere but the loopback door that issued it.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class MapError(Exception):
    """The map, or its resolution against the board, cannot be applied."""


def parse_map(text: str) -> list:
    """`[(line_number, dependent_title, [dependency_titles])]`, in file order.
    Raises `MapError` naming every malformed line."""
    rows = []
    problems = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ARROW not in line:
            problems.append(f"line {number}: no ' <- ' between the card and "
                            "what it waits on")
            continue
        head, tail = line.split(ARROW, 1)
        dependent = head.strip()
        deps = [part.strip() for part in tail.split(SEPARATOR) if part.strip()]
        if not dependent or not deps:
            problems.append(f"line {number}: a card and at least one card it "
                            "waits on are both needed")
            continue
        rows.append((number, dependent, deps))
    if problems:
        raise MapError("\n".join(problems))
    return rows


def project_cards(state: dict, project: str) -> list:
    """The named project's cards out of an `/api/state` body (or its board
    section alone). Matched on the project's label, trimmed and
    case-insensitive, the name a person sees on the board."""
    board = state.get("board") if isinstance(state.get("board"), dict) else state
    cards = board.get("cards") if isinstance(board, dict) else None
    if not isinstance(cards, list):
        raise MapError("that state has no board in it")
    wanted = project.strip().lower()
    return [c for c in cards if isinstance(c, dict)
            and str(c.get("project") or "").strip().lower() == wanted]


def resolve(rows: list, cards: list) -> list:
    """`[(line, dependent_card, [dependency_cards])]`, or `MapError` naming
    every title that matched no card or more than one. Nothing partial."""
    by_title: dict = {}
    for card in cards:
        key = str(card.get("title") or "").strip().lower()
        if key:
            by_title.setdefault(key, []).append(card)
    problems = []

    def one(title: str, number: int):
        found = by_title.get(title.strip().lower(), [])
        if not found:
            problems.append(f'line {number}: no card is called "{title}"')
            return None
        if len(found) > 1:
            problems.append(f'line {number}: "{title}" names {len(found)} '
                            "cards — rename one first")
            return None
        return found[0]

    out = []
    for number, dependent, deps in rows:
        card = one(dependent, number)
        linked = [one(title, number) for title in deps]
        if card is None or any(d is None for d in linked):
            continue
        if any(d["id"] == card["id"] for d in linked):
            problems.append(f'line {number}: "{dependent}" cannot wait on itself')
            continue
        out.append((number, card, linked))
    # One card named on two lines is one list: merged, in order, bounded.
    merged: dict = {}
    order = []
    for number, card, linked in out:
        if card["id"] not in merged:
            merged[card["id"]] = (number, card, [])
            order.append(card["id"])
        have = merged[card["id"]][2]
        for dep in linked:
            if all(dep["id"] != d["id"] for d in have):
                have.append(dep)
    result = [merged[cid] for cid in order]
    for number, card, linked in result:
        if len(linked) > MAX_DEPENDENCIES:
            problems.append(f'line {number}: "{card.get("title")}" would wait on '
                            f"{len(linked)} cards; the board keeps "
                            f"{MAX_DEPENDENCIES}")
    if problems:
        raise MapError("\n".join(problems))
    return result


def describe(card: dict) -> str:
    return (f'"{card.get("title")}" [{card.get("column_name") or "?"}, '
            f'rev {int(card.get("revision") or 0)}]')


def loopback_refusal(base_url: str) -> str:
    """`""` for a loopback http URL, else the refusal in words."""
    parsed = urllib.parse.urlsplit(str(base_url or ""))
    if parsed.scheme != "http" or (parsed.hostname or "") not in LOOPBACK_HOSTS:
        return (f"--base-url must be http:// on this Mac's loopback "
                f"({', '.join(LOOPBACK_HOSTS)}), not {base_url!r}")
    return ""


def _request(url: str, token: str = "", body=None):
    """GET when `body` is None — reads are ungated, so no token rides them —
    else a POST carrying `X-Bob-Token`."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 method="GET" if body is None else "POST")
    if body is not None:
        if token:
            req.add_header("X-Bob-Token", token)
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=TIMEOUT_SECONDS) as reply:
            return reply.status, json.loads(reply.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as err:
        try:
            payload = json.loads(err.read().decode("utf-8") or "{}")
        except (ValueError, UnicodeError):
            payload = {}
        return err.code, payload


def live_state(base_url: str) -> dict:
    """`/api/state` plus the Done archive (`/api/board?column=done`), so a
    card finished before today's preview window still resolves by title."""
    status, state = _request(base_url.rstrip("/") + "/api/state")
    if status != 200 or not isinstance(state, dict):
        raise MapError(f"Dark Army did not answer /api/state (HTTP {status})")
    board = state.get("board") if isinstance(state.get("board"), dict) else {}
    cards = list(board.get("cards") or [])
    status, done = _request(base_url.rstrip("/") + "/api/board?column=done")
    if status == 200 and isinstance(done, dict):
        seen = {c.get("id") for c in cards}
        cards.extend(c for c in done.get("cards") or []
                     if isinstance(c, dict) and c.get("id") not in seen)
    return {"board": dict(board, cards=cards)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Link a project's cards to the cards they wait on, "
                    "from a list of titles. A dry run unless --apply.")
    parser.add_argument("--project", required=True,
                        help="the project's name as the board shows it")
    parser.add_argument("--map", required=True, dest="map_path",
                        help="the list: '<card> <- <card> | <card>' per line")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--state-file",
                        help="read an /api/state body from this file "
                             "instead of the live board")
    parser.add_argument("--apply", action="store_true",
                        help="write the links (the default only prints them)")
    args = parser.parse_args(argv)
    refusal = loopback_refusal(args.base_url)
    if refusal:
        print(f"refused, nothing written: {refusal}", file=sys.stderr)
        return 1

    try:
        rows = parse_map(Path(args.map_path).read_text(encoding="utf-8"))
        token = ""
        if args.state_file:
            state = json.loads(Path(args.state_file).read_text(encoding="utf-8"))
        else:
            state = live_state(args.base_url)
        cards = project_cards(state, args.project)
        if not cards:
            raise MapError(f'no cards on the board under "{args.project}"')
        links = resolve(rows, cards)
    except (OSError, ValueError, MapError) as err:
        print(f"refused, nothing written: {err}", file=sys.stderr)
        return 1

    for _number, card, linked in links:
        names = " | ".join(describe(d) for d in linked)
        print(f"would link {describe(card)} <- {names}")
    if not args.apply:
        print(f"dry run: {len(links)} card(s) would be linked; nothing "
              "written. Run again with --apply to write them.")
        return 0
    if not token:
        token_path = Path(os.path.expanduser(args.token_file))
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as err:
            print(f"refused, nothing written: {err}", file=sys.stderr)
            return 1

    failed = 0
    for _number, card, linked in links:
        body = {"action": "board_update", "card_id": card["id"],
                "blocked_by": "\n".join(d["id"] for d in linked),
                "expected_revision": int(card.get("revision") or 0)}
        status, reply = _request(args.base_url.rstrip("/") + "/api/action",
                                 token, body)
        ok = status == 200 and bool((reply or {}).get("ok", True))
        detail = str((reply or {}).get("detail") or "").strip()
        word = "linked" if ok else "refused"
        print(f"{word} {describe(card)}" + (f": {detail}" if detail and not ok
                                            else ""))
        if not ok:
            failed += 1
    print(f"applied: {len(links) - failed} linked, {failed} refused.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
