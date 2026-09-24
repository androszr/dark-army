"""Quit waits off AppKit and invokes the application quit on its run loop."""
import threading
from queue import Queue
from types import SimpleNamespace

import pytest

from dark_army_menubar import app as A


def test_quit_keeps_main_thread_free_until_daemon_stops(monkeypatch):
    main = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    callbacks = Queue()
    events = []
    instance = object.__new__(A.BobCompanionApp)
    instance._restarting = False
    instance._loop = object()
    instance._daemon = SimpleNamespace(
        _shutdown=lambda keep_terminals=True: "shutdown coroutine"
    )
    instance._release_dictation_if_down = lambda reason: events.append((reason, threading.get_ident()))
    instance._panel = SimpleNamespace(quit=lambda: events.append(("panel", threading.get_ident())))

    def wait_for_shutdown(timeout):
        events.append(("daemon", threading.get_ident()))
        assert timeout == 8
        entered.set()
        assert release.wait(3)

    def submit(coro, loop):
        assert coro == "shutdown coroutine" and loop is instance._loop
        return SimpleNamespace(result=wait_for_shutdown)

    monkeypatch.setattr(A.asyncio, "run_coroutine_threadsafe", submit)
    monkeypatch.setattr(A, "callAfter", lambda fn: callbacks.put(fn))
    monkeypatch.setattr(A.rumps, "quit_application", lambda: events.append(("appkit", threading.get_ident())))
    try:
        instance._on_quit(None)
        assert entered.wait(3)
        assert callbacks.empty()  # Main thread is here while shutdown is held.
        instance._on_quit(None)
        instance._on_restart(None)
        assert [event for event, _ in events] == ["quit", "panel", "daemon"]
        assert all(thread != main for _, thread in events)
    finally:
        release.set()
    finish = callbacks.get(timeout=3)
    finish()
    assert events[-1] == ("appkit", main)
    assert callbacks.empty()


@pytest.mark.parametrize("fault", ["panel", "daemon", "appkit"])
def test_quit_failure_keeps_the_existing_forced_exit(monkeypatch, fault):
    events = []
    instance = object.__new__(A.BobCompanionApp)
    instance._release_dictation_if_down = lambda reason: None

    def step(name):
        events.append(name)
        if name == fault:
            raise RuntimeError("fixture failure")

    instance._panel = SimpleNamespace(quit=lambda: step("panel"))
    instance._shutdown_daemon = lambda: step("daemon")
    monkeypatch.setattr(A, "callAfter", lambda fn: fn())
    monkeypatch.setattr(A.rumps, "quit_application", lambda: step("appkit"))
    monkeypatch.setattr(A.logging, "shutdown", lambda: events.append("logging"))
    monkeypatch.setattr(A.os, "_exit", lambda code: events.append(("exit", code)))
    instance._quit_now()
    assert events[-2:] == ["logging", ("exit", 1)]
    assert events.count(("exit", 1)) == 1


@pytest.mark.parametrize("verb", ["quit_app", "restart"])
def test_teardown_panel_action_never_pushes_context_to_a_closing_panel(monkeypatch, verb):
    events = []
    instance = object.__new__(A.BobCompanionApp)
    instance._on_quit = lambda _: events.append("quit_app")
    instance._on_restart = lambda _: events.append("restart")
    instance._push_panel_context = lambda: events.append("respawn")
    monkeypatch.setattr(A, "callAfter", lambda fn: fn())
    instance._on_panel_action(verb)
    assert events == [verb]


def test_quit_during_restart_does_not_start_a_second_teardown(monkeypatch):
    instance = object.__new__(A.BobCompanionApp)
    instance._restarting = True
    calls = []
    monkeypatch.setattr(A.threading, "Thread", lambda **kwargs: calls.append(kwargs))
    instance._on_quit(None)
    assert calls == []
