"""`tools/board_link_dependencies.py` — the person's linker, driven offline.

Every case reads `fixtures/dependency_state.json` (`--state-file`) or a
local stub server on an ephemeral port: no test here, and no verifier, ever
reads or writes a live board. `docs/card-dependencies.md`, *The linker*.
"""
import http.server
import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "board_link_dependencies.py"
MAP = REPO / "tools" / "dependency-maps" / "ai-viber.txt"
STATE = Path(__file__).resolve().parent / "fixtures" / "dependency_state.json"

SPEC = importlib.util.spec_from_file_location("board_link_dependencies", TOOL)
linker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(linker)


@pytest.fixture(autouse=True)
def _no_desk_token_in_the_environment(monkeypatch):
    """A person's exported desk token must not leak into these cases: the
    `--token-file` ones pin the file route."""
    monkeypatch.delenv("DARK_ARMY_DESK_TOKEN", raising=False)


def _cards():
    return json.loads(STATE.read_text())["board"]["cards"]


def _by_title(project="ai-viber"):
    return {c["title"]: c for c in _cards() if c["project"] == project}


def _map_lines():
    return [line for line in MAP.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_the_dry_run_prints_every_link_and_writes_nothing(capsys):
    code = linker.main(["--project", "ai-viber", "--map", str(MAP),
                        "--state-file", str(STATE),
                        "--base-url", "http://127.0.0.1:9"])
    out = capsys.readouterr().out
    assert code == 0
    lines = [line for line in out.splitlines() if line.startswith("would link ")]
    assert len(lines) == len(_map_lines())
    assert "dry run" in out
    # Each line names the dependent and what it waits on, with the column
    # and change number the dry run read.
    cards = _by_title()
    box = ("Build the Box Room window and Room tab")
    line = next(x for x in lines if f'"{box}"' in x.split(" <- ")[0])
    assert f'rev {cards[box]["revision"]}' in line
    assert '"Build the Cockpit: a dashboard that starts as one button and grows"' in line
    assert '"Set up the pixel-art pipeline and final starting-room sprites"' in line


def test_the_command_line_run_matches_the_acceptance_shape(tmp_path):
    """The plan's own command, as a person types it, from the repo root."""
    run = subprocess.run(
        [sys.executable, str(TOOL), "--project", "ai-viber",
         "--map", "tools/dependency-maps/ai-viber.txt",
         "--state-file", "host/tests/fixtures/dependency_state.json"],
        cwd=REPO, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout.count("would link") == len(_map_lines())
    assert "dry run" in run.stdout


def test_a_missing_title_refuses_the_whole_run(tmp_path, capsys):
    bad = tmp_path / "map.txt"
    bad.write_text(MAP.read_text() + "No such card <- Build the economy core "
                   "in the simulation\n")
    code = linker.main(["--project", "ai-viber", "--map", str(bad),
                        "--state-file", str(STATE)])
    captured = capsys.readouterr()
    assert code == 1
    assert "would link" not in captured.out
    assert 'no card is called "No such card"' in captured.err
    assert "nothing written" in captured.err


def test_an_ambiguous_title_refuses_the_whole_run(tmp_path, capsys):
    bad = tmp_path / "map.txt"
    bad.write_text("Unique title <- duplicate TITLE\n")
    code = linker.main(["--project", "vir-sunset", "--map", str(bad),
                        "--state-file", str(STATE)])
    captured = capsys.readouterr()
    assert code == 1
    assert '"duplicate TITLE" names 2 cards' in captured.err
    assert "would link" not in captured.out


def test_titles_resolve_only_within_the_named_project(tmp_path, capsys):
    bad = tmp_path / "map.txt"
    bad.write_text("Unique title <- Build the economy core in the simulation\n")
    code = linker.main(["--project", "vir-sunset", "--map", str(bad),
                        "--state-file", str(STATE)])
    assert code == 1
    assert "no card is called" in capsys.readouterr().err


def test_a_malformed_line_and_a_self_link_are_refused(tmp_path, capsys):
    bad = tmp_path / "map.txt"
    bad.write_text("Unique title waits on nothing in particular\n")
    assert linker.main(["--project", "vir-sunset", "--map", str(bad),
                        "--state-file", str(STATE)]) == 1
    assert "no ' <- '" in capsys.readouterr().err
    bad.write_text("Unique title <- unique title\n")
    assert linker.main(["--project", "vir-sunset", "--map", str(bad),
                        "--state-file", str(STATE)]) == 1
    assert "cannot wait on itself" in capsys.readouterr().err


def test_parse_map_merges_nothing_it_cannot_read():
    rows = linker.parse_map("# comment\n\nA <- B | C\nD <- E\n")
    assert rows == [(3, "A", ["B", "C"]), (4, "D", ["E"])]


class _Stub(http.server.BaseHTTPRequestHandler):
    posts: list = []
    gets: list = []

    def do_GET(self):  # noqa: N802 — the stdlib's spelling
        type(self).gets.append((self.path, self.headers.get("X-Bob-Token")))
        if self.path == "/api/state":
            payload = json.loads(STATE.read_text())
        else:
            payload = {"cards": []}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802 — the stdlib's spelling
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).posts.append((self.path, self.headers.get("X-Bob-Token"),
                                 self.headers.get("Authorization"), body))
        refuse = body.get("card_id") == _Stub.refuse_id
        payload = ({"ok": False, "detail": "those cards already wait on each other"}
                   if refuse else {"ok": True})
        data = json.dumps(payload).encode("utf-8")
        self.send_response(409 if refuse else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


@pytest.fixture
def stub():
    _Stub.posts = []
    _Stub.gets = []
    _Stub.refuse_id = ""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_apply_posts_one_board_update_per_card_with_the_token(
        stub, tmp_path, capsys):
    token = tmp_path / "api-token"
    token.write_text("s3cret\n")
    code = linker.main(["--project", "ai-viber", "--map", str(MAP),
                        "--state-file", str(STATE), "--base-url", stub,
                        "--token-file", str(token), "--apply"])
    assert code == 0, capsys.readouterr()
    assert len(_Stub.posts) == len(_map_lines())
    cards = _by_title()
    for path, header, bearer, body in _Stub.posts:
        assert path == "/api/action"
        assert header == "s3cret"
        assert bearer is None, "panel writes send X-Bob-Token, never Bearer"
        assert body["action"] == "board_update"
        assert set(body) == {"action", "card_id", "blocked_by",
                             "expected_revision"}
    first = _Stub.posts[0][3]
    front = cards["Front door and server-side progress: sign-up, email links, guests"]
    base = cards["Build the game foundation: server-owned saves and the live update loop"]
    assert first["card_id"] == front["id"]
    assert first["blocked_by"] == base["id"]
    assert first["expected_revision"] == front["revision"]
    box = next(b for *_rest, b in _Stub.posts
               if b["card_id"] == cards["Build the Box Room window and Room tab"]["id"])
    assert box["blocked_by"].split("\n") == [
        cards["Build the Cockpit: a dashboard that starts as one button and grows"]["id"],
        cards["Set up the pixel-art pipeline and final starting-room sprites"]["id"]]
    assert "applied: 17 linked, 0 refused." in capsys.readouterr().out


def test_apply_prints_a_refusal_in_the_stores_words_and_exits_nonzero(
        stub, tmp_path, capsys):
    token = tmp_path / "api-token"
    token.write_text("s3cret")
    _Stub.refuse_id = _by_title()[
        "Build the economy core in the simulation"]["id"]
    code = linker.main(["--project", "ai-viber", "--map", str(MAP),
                        "--state-file", str(STATE), "--base-url", stub,
                        "--token-file", str(token), "--apply"])
    out = capsys.readouterr().out
    assert code == 1
    assert ('refused "Build the economy core in the simulation"' in out
            and "already wait on each other" in out)
    assert "16 linked, 1 refused" in out


def test_a_refused_dry_run_never_touches_the_network(stub, tmp_path):
    bad = tmp_path / "map.txt"
    bad.write_text("Ghost <- Build the economy core in the simulation\n")
    token = tmp_path / "api-token"
    token.write_text("s3cret")
    code = linker.main(["--project", "ai-viber", "--map", str(bad),
                        "--state-file", str(STATE), "--base-url", stub,
                        "--token-file", str(token), "--apply"])
    assert code == 1
    assert _Stub.posts == []


def test_the_shipped_map_names_only_cards_the_fixture_holds():
    """Every title in the map — the `# planner:` lines included — resolves
    in the fixture, so the fixture is a faithful offline stand-in."""
    titles = set(_by_title())
    for raw in MAP.read_text().splitlines():
        line = raw.strip()
        if line.startswith("# planner:"):
            line = line[len("# planner:"):].strip()
        elif not line or line.startswith("#"):
            continue
        head, tail = line.split(" <- ", 1)
        for title in [head] + tail.split(" | "):
            assert title.strip() in titles, title


def test_the_linker_is_stdlib_only():
    text = TOOL.read_text()
    assert "dark_army" not in text.replace("dark-army", "")
    for line in text.splitlines():
        if line.startswith(("import ", "from ")):
            module = line.split()[1].split(".")[0]
            assert module in {"__future__", "argparse", "json", "os", "sys",
                              "urllib", "pathlib"}, line


@pytest.mark.parametrize("url", [
    "http://example.com:19874", "https://127.0.0.1:19874",
    "http://10.0.0.5:19874", "http://127.0.0.1.evil.test:19874",
])
def test_a_base_url_off_this_macs_loopback_is_refused(url, tmp_path, capsys):
    """The token is Dark Army's write key: it goes to the loopback door that
    issued it and nowhere else, whatever `--base-url` says."""
    token = tmp_path / "api-token"
    token.write_text("s3cret")
    code = linker.main(["--project", "ai-viber", "--map", str(MAP),
                        "--state-file", str(STATE), "--base-url", url,
                        "--token-file", str(token), "--apply"])
    assert code == 1
    assert "loopback" in capsys.readouterr().err
    for ok in ("http://127.0.0.1:19874", "http://localhost:1", "http://[::1]:2"):
        assert linker.loopback_refusal(ok) == ""


def test_live_reads_carry_no_token_and_the_write_does(stub, tmp_path, capsys):
    """Reads are ungated, so the token rides the POST alone; and no proxy
    is consulted on the way (`_OPENER`)."""
    token = tmp_path / "api-token"
    token.write_text("s3cret")
    code = linker.main(["--project", "ai-viber", "--map", str(MAP),
                        "--base-url", stub, "--token-file", str(token),
                        "--apply"])
    assert code == 0, capsys.readouterr()
    assert [path for path, _ in _Stub.gets] == ["/api/state",
                                                "/api/board?column=done"]
    assert all(header is None for _, header in _Stub.gets)
    assert _Stub.posts and all(h == "s3cret" for _, h, _, _ in _Stub.posts)
    assert isinstance(linker._OPENER, linker.urllib.request.OpenerDirector)
    assert not any(isinstance(h, linker.urllib.request.ProxyHandler)
                   and h.proxies for h in linker._OPENER.handlers)


def test_the_desk_token_beats_the_token_file(stub, tmp_path, monkeypatch,
                                             capsys):
    """`DARK_ARMY_DESK_TOKEN` (or `--token`) wins over `--token-file`: the
    file on disk is the session token, which `board_update` refuses."""
    token = tmp_path / "api-token"
    token.write_text("session-on-disk")
    monkeypatch.setenv("DARK_ARMY_DESK_TOKEN", "desk-from-env")
    code = linker.main(["--project", "ai-viber", "--map", str(MAP),
                        "--state-file", str(STATE), "--base-url", stub,
                        "--token-file", str(token), "--apply"])
    assert code == 0, capsys.readouterr()
    assert _Stub.posts and all(h == "desk-from-env"
                               for _, h, _, _ in _Stub.posts)
    _Stub.posts = []
    code = linker.main(["--project", "ai-viber", "--map", str(MAP),
                        "--state-file", str(STATE), "--base-url", stub,
                        "--token", "desk-from-flag",
                        "--token-file", str(token), "--apply"])
    assert code == 0, capsys.readouterr()
    assert _Stub.posts and all(h == "desk-from-flag"
                               for _, h, _, _ in _Stub.posts)


def test_the_help_names_the_desk_token_and_the_session_file(capsys):
    with pytest.raises(SystemExit):
        linker.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "DARK_ARMY_DESK_TOKEN" in out
    assert "session token" in out
