# host/tests/test_relay_ws_server.py
"""Source pins on the socket relay (`relay-ws/`), `test_relay_box.py`'s
pattern: the properties the plan's security argument rests on, pinned from
this side so a drift fails the suite rather than dying quietly on Fly. Plus
one case that runs the service's own `node --test` suite when its `ws`
dependency is installed, and skips with a reason otherwise."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / "relay-ws"
SERVER = FOLDER / "server.js"
PACKAGE = FOLDER / "package.json"
LOCK = FOLDER / "package-lock.json"
FLY = FOLDER / "fly.toml"
DOCKER = FOLDER / "Dockerfile"
README = FOLDER / "README.md"
BOX = ROOT / "relay" / "api" / "box.js"
CI = ROOT / ".github" / "workflows" / "tests.yml"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def test_no_logging_of_anything_carried():
    text = _read(SERVER)
    assert "console.log" not in text
    assert "console." not in text


def test_small_enough_to_audit_in_one_read():
    assert len(_read(SERVER).splitlines()) <= 200


def _code(text: str) -> str:
    """The file without its comment lines: the pins are about what runs."""
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))


def test_no_environment_variable_and_no_secret():
    text = _read(SERVER)
    assert "process.env" not in text
    code = _code(text)
    for word in ("Bearer", "secret", "SECRET", "token", "password"):
        assert word not in code, word


def test_the_frame_cap_is_the_librarys_bound():
    text = _read(SERVER)
    assert "maxPayload: MAX_FRAME_BYTES" in text
    assert re.search(r"const MAX_FRAME_BYTES = 1024 \* 1024;", text)


def test_the_channel_shape_is_the_mailboxs():
    server = _read(SERVER)
    box = _read(BOX)
    shape = re.search(r"const CH_SHAPE = (/[^;]+/);", box).group(1)
    assert f"const CH_SHAPE = {shape};" in server
    assert 'new Set(["mac", "phone"])' in server


def test_one_dependency_and_it_is_ws():
    package = json.loads(_read(PACKAGE))
    assert list(package["dependencies"]) == ["ws"]
    assert package["scripts"]["start"] == "node server.js"
    assert package["scripts"]["test"].startswith("node --test ")
    assert package["engines"]["node"] == ">=22"
    assert "devDependencies" not in package
    lock = json.loads(_read(LOCK))
    assert set(lock["packages"][""]["dependencies"]) == {"ws"}


def test_newest_wins_and_slow_down_close_codes():
    text = _read(SERVER)
    assert "const CLOSE_REPLACED = 4001;" in text
    assert "const CLOSE_SLOW_DOWN = 4008;" in text
    assert 'close(CLOSE_REPLACED, "replaced")' in text
    assert 'close(CLOSE_SLOW_DOWN, "slow down")' in text
    assert "const RATE_PER_MINUTE = 240;" in text
    assert "const MAX_CONNECTIONS = 512;" in text
    assert "const PING_MS = 25000;" in text


def test_the_peer_prefix_is_refused_from_clients():
    text = _read(SERVER)
    assert 'if (text.startsWith("peer:")) return;' in text
    assert '"peer:1"' in text and '"peer:0"' in text


def test_an_origin_upgrade_is_refused():
    text = _read(SERVER)
    assert "req.headers.origin !== undefined" in text
    assert 'refuse(socket, 403, "Forbidden")' in text
    assert 'refuse(socket, 503, "Service Unavailable")' in text
    assert 'refuse(socket, 400, "Bad Request")' in text


def test_frames_are_forwarded_verbatim_or_dropped():
    text = _read(SERVER)
    assert "if (isOpen(peer)) peer.send(text);" in text
    assert "if (isBinary) return;" in text
    # No store: nothing is queued for an absent side.
    for word in ("LPUSH", "redis", "fetch(", "Map<string"):
        assert word not in text, word


def test_fly_keeps_the_machine_up_and_https_on():
    fly = _read(FLY)
    assert fly.count("force_https = true") == 1
    assert fly.count("auto_stop_machines = false") == 1
    assert "min_machines_running = 1" in fly
    assert "internal_port = 8080" in fly
    assert 'path = "/healthz"' in fly
    docker = _read(DOCKER)
    assert docker.startswith("FROM node:22-alpine")
    assert "npm ci --omit=dev" in docker
    assert "EXPOSE 8080" in docker
    server = _read(SERVER)
    assert "const PORT = 8080;" in server
    assert '"/healthz"' in server


def test_the_readme_names_the_five_properties_and_the_pairing_note():
    text = _read(README)
    for phrase in ("fly launch --no-deploy", "fly deploy", "fly status",
                   "fly logs", "fly scale count 1", "auto_stop_machines = false",
                   "Socket address", "pair the phone", "channel ids",
                   "IP addresses", "timing and volume", "wss://"):
        assert phrase in text, phrase


def test_ci_runs_the_service_suite_in_the_host_job():
    text = _read(CI)
    host = text.split("  host:")[1].split("  panel:")[0]
    assert "relay-ws" in host
    assert "npm ci" in host
    assert "node --test" in host


def test_the_service_suite_passes_under_node():
    if not (FOLDER / "node_modules" / "ws").is_dir():
        pytest.skip("relay-ws/node_modules/ws is not installed "
                    "(cd relay-ws && npm ci)")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    proc = subprocess.run(
        [node, "--test", *sorted(str(p) for p in (FOLDER / "tests").glob("*.test.mjs"))],
        capture_output=True, text=True, timeout=120, cwd=str(FOLDER))
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert "fail 0" in proc.stdout
