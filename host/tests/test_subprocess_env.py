"""py2app's PYTHONHOME must not reach children Dark Army starts."""
import os

from dark_army_daemon import subprocess_env, vscode_reveal


def test_clean_env_drops_the_frozen_app_python_vars(monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/Apps/Dark Army.app/Contents/Resources")
    monkeypatch.setenv("PYTHONPATH", "/Apps/Dark Army.app/Contents/Resources")
    monkeypatch.setenv("RESOURCEPATH", "/Apps/Dark Army.app/Contents/Resources")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = subprocess_env.clean_env()
    for name in subprocess_env.PY2APP_ENV_VARS:
        assert name not in env, name
    assert env["PATH"] == "/usr/bin:/bin"


def test_clean_env_does_not_mutate_the_source():
    base = {"PATH": "/bin", "PYTHONHOME": "/frozen"}
    env = subprocess_env.clean_env(base)
    assert "PYTHONHOME" not in env
    assert base["PYTHONHOME"] == "/frozen"


def test_unset_payload_is_json_nulls_the_extension_deletes():
    payload = subprocess_env.unset_payload()
    assert payload["PYTHONHOME"] is None
    assert payload["GROK_SESSION_ID"] is None
    assert set(payload) == set(subprocess_env.STRIPPED_ENV_VARS)


def test_clean_env_drops_the_session_the_app_was_started_from():
    """An app relaunched from inside an assistant's terminal carries that
    session's identity for as long as the pty broker lives; a child started
    on Dark Army's pty must not be told it is that session (the hook would stamp
    the wrong provider and liveness would evict a live claude as "PID gone",
    the channel would claim somebody else's session id)."""
    base = {
        "PATH": "/bin",
        "HOME": "/Users/x",
        "GROK_SESSION_ID": "01a094cd-f0fa",
        "GROK_AGENT": "1",
        "CLAUDE_CODE_SESSION_ID": "abc",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SSE_PORT": "31045",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_PID": "73996",
        "CLAUDE_EFFORT": "medium",
        "CLAUDE_CODE_EXECPATH": "/x/versions/2.1.269",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_SESSION_ATTENDED": "1",
        "CLAUDE_CODE_MESSAGING_TOKEN": "tok",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/1.sock",
        "CLAUDE_CODE_BRIDGE_SESSION_ID": "session_x",
        "CODEX_THREAD_ID": "t",
        "CODEX_SESSION_ID": "s",
        "BOB_COMPANION_ORIGIN": "card-refine|abc|bc-planner",
        "TERM_PROGRAM": "vscode",
        "TERM_PROGRAM_VERSION": "1.134.0",
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
        "CLAUDE_CONFIG_DIR": "/Users/x/.claude",
        "ANTHROPIC_API_KEY": "sk-x",
    }
    env = subprocess_env.clean_env(base)
    for name in subprocess_env.SESSION_ENV_VARS:
        assert name not in env, name
    for name in base:
        if name.startswith(subprocess_env.SESSION_ENV_PREFIXES):
            assert name not in env, name
    assert env["PATH"] == "/bin" and env["HOME"] == "/Users/x"
    # A setting is not an identity: the title pen stays taken, and so do
    # the config folder and the key a person put in their login environment.
    assert env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == "/Users/x/.claude"
    assert env["ANTHROPIC_API_KEY"] == "sk-x"


def test_spawn_agent_asks_the_extension_to_drop_pythonhome(monkeypatch):
    """A session Dark Army starts in VS Code must not inherit the frozen app's env."""
    posted = []

    async def fake_post(port, token, body, timeout=8.0):
        posted.append(body)
        return {"spawned": True, "terminalName": "t", "shellPid": 1}

    monkeypatch.setattr(vscode_reveal, "_post_json", fake_post)
    monkeypatch.setattr(vscode_reveal, "_spawn_capable_locks",
                        lambda: [{"port": 1, "authToken": "x",
                                  "workspaceFolders": ["/tmp/proj"]}])
    monkeypatch.setattr(vscode_reveal, "_lock_owns", lambda lock, root: True)
    import asyncio
    asyncio.run(vscode_reveal.spawn_agent("/tmp/proj", ["/usr/bin/grok", "-p", "hi"],
                                          "test"))
    assert posted, "spawn was not sent"
    env = posted[0]["env"]
    assert env["PYTHONHOME"] is None
    assert env["PYTHONPATH"] is None
