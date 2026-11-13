"""Dark Army carrying a session on once its usage limit has reset."""
import asyncio
from datetime import datetime

import pytest

from dark_army_daemon import limit_resume as lr
from dark_army_daemon import steering

NOW = datetime(2026, 11, 12, 13, 0, 0).timestamp()


def card(message="You've hit your limit · resets 1:40pm"):
    return {"hook": "StopFailure", "error_kind": "rate_limit",
            "message": message}


def at(hour, minute=0, day=12):
    return datetime(2026, 11, day, hour, minute, 0).timestamp()


# ── reading the reset ────────────────────────────────────────────────────────

def test_only_the_usage_limit_card_counts():
    assert lr.is_limit_card(card())
    assert not lr.is_limit_card(None)
    assert not lr.is_limit_card({"hook": "Stop", "error_kind": "rate_limit"})
    assert not lr.is_limit_card({"hook": "StopFailure", "error_kind": "overloaded"})


def test_an_epoch_after_a_bar_is_the_reset():
    assert lr.reset_from_message("limit reached|1794600000", NOW) == 1794600000.0


@pytest.mark.parametrize("text,expected", [
    ("resets 1:40pm", at(13, 40)),
    ("resets at 2pm", at(14)),
    ("resets 9am", at(9, day=13)),            # already past today: tomorrow
    ("resets 23:15", at(23, 15)),
    ("resets Nov 13, 9am", at(9, day=13)),
])
def test_a_clock_time_is_the_next_time_the_clock_reads_it(text, expected):
    assert lr.reset_from_message(text, NOW) == expected


@pytest.mark.parametrize("text", [
    "", "you hit your limit", "resets 5", "resets 13pm", "resets 25:00",
    "resets Feb 30, 9am"])
def test_a_sentence_with_no_readable_time_names_none(text):
    assert lr.reset_from_message(text, NOW) is None


def test_a_named_zone_is_honoured():
    here = lr.reset_from_message("resets 3pm", NOW)
    there = lr.reset_from_message("resets 3pm (America/New_York)", NOW)
    assert there is not None and there != here


def test_the_full_window_names_the_reset_and_the_latest_one_wins():
    metrics = [
        {"five_hour_pct": 100.0, "five_hour_resets_at": NOW + 600,
         "seven_day_pct": 40.0, "seven_day_resets_at": NOW + 90000},
        {"five_hour_pct": 99.9, "five_hour_resets_at": NOW + 900},
    ]
    assert lr.reset_from_metrics(metrics, NOW) == NOW + 900


def test_with_no_full_window_the_soonest_future_reset_is_the_guess():
    metrics = [{"five_hour_pct": 80.0, "five_hour_resets_at": NOW + 600,
                "seven_day_pct": 40.0, "seven_day_resets_at": NOW + 90000},
               {"five_hour_resets_at": NOW - 5}, "junk"]
    assert lr.reset_from_metrics(metrics, NOW) == NOW + 600
    assert lr.reset_from_metrics([], NOW) is None


def test_the_sentence_outranks_the_windows_and_a_margin_is_added():
    due, known = lr.resume_at("resets 1:40pm",
                              [{"five_hour_resets_at": NOW + 60}], NOW)
    assert (due, known) == (at(13, 40) + lr.MARGIN_SECONDS, True)


def test_an_unknown_or_unbelievable_reset_waits_the_blind_retry():
    assert lr.resume_at("no time here", [], NOW) \
        == (NOW + lr.UNKNOWN_RESET_RETRY_SECONDS, False)
    far = f"limit|{int(NOW + lr.MAX_WAIT_SECONDS + 3600)}"
    assert lr.resume_at(far, [], NOW) \
        == (NOW + lr.UNKNOWN_RESET_RETRY_SECONDS, False)


# ── the policy ───────────────────────────────────────────────────────────────

def test_it_waits_for_the_reset_then_sends_once():
    policy = lr.LimitResumePolicy()
    due = at(13, 40) + lr.MARGIN_SECONDS
    assert policy.consider("s1", card(), [], NOW) == lr.WAIT
    assert policy.due_at("s1") == due
    assert policy.consider("s1", card(), [], due - 1) == lr.WAIT
    assert policy.consider("s1", card(), [], due) == lr.SEND
    policy.note_sent("s1", due)
    # Typed: the row no longer promises a time, and nothing is typed again
    # while the session settles.
    assert policy.due_at("s1") is None
    assert policy.consider("s1", card(), [], due + 5) == lr.WAIT


def test_a_card_that_went_away_ends_the_episode():
    policy = lr.LimitResumePolicy()
    policy.consider("s1", card(), [], NOW)
    assert policy.consider("s1", None, [], NOW + 5) == lr.PASS
    assert policy.due_at("s1") is None


def test_a_limit_that_had_not_lifted_is_tried_again_then_given_up():
    policy = lr.LimitResumePolicy()
    message = "limit reached"                 # no reset anywhere: blind retry
    now = NOW
    policy.consider("s1", card(message), [], now)
    for attempt in range(lr.MAX_ATTEMPTS):
        now += lr.UNKNOWN_RESET_RETRY_SECONDS
        assert policy.consider("s1", card(message), [], now) == lr.SEND, attempt
        policy.note_sent("s1", now)
        now += lr.SETTLE_SECONDS + 1
        # Still limited after the settle.
        verdict = policy.consider("s1", card(message), [], now)
        assert verdict == (lr.WAIT if attempt < lr.MAX_ATTEMPTS - 1 else lr.PASS)
    assert policy.due_at("s1") is None
    assert policy.consider("s1", card(message), [], now + 86400) == lr.PASS


def test_a_reset_that_turns_up_later_replaces_the_blind_retry():
    policy = lr.LimitResumePolicy()
    policy.consider("s1", card("limit reached"), [], NOW)
    assert policy.due_at("s1") == NOW + lr.UNKNOWN_RESET_RETRY_SECONDS
    metrics = [{"five_hour_pct": 100.0, "five_hour_resets_at": NOW + 300}]
    assert policy.consider("s1", card("limit reached"), metrics, NOW + 5) == lr.WAIT
    assert policy.due_at("s1") == NOW + 300 + lr.MARGIN_SECONDS


def test_an_unreachable_session_is_retried_a_bounded_number_of_times():
    policy = lr.LimitResumePolicy()
    policy.consider("s1", card(), [], NOW)
    for n in range(1, lr.MAX_UNREACHABLE):
        policy.note_unreachable("s1", NOW + n)
        assert policy.due_at("s1") == NOW + n + lr.UNREACHABLE_RETRY_SECONDS
    policy.note_unreachable("s1", NOW + 99)
    assert policy.due_at("s1") is None
    assert policy.consider("s1", card(), [], NOW + 10 ** 6) == lr.PASS


def test_a_send_that_never_landed_does_not_cost_an_attempt():
    policy = lr.LimitResumePolicy()
    policy.consider("s1", card(), [], NOW)
    policy.note_sent("s1", NOW)
    policy.note_unreachable("s1", NOW + 1)
    assert policy._episodes["s1"].attempts == 0
    assert policy.due_at("s1") == NOW + 1 + lr.UNREACHABLE_RETRY_SECONDS


def test_the_switch_off_drops_every_scheduled_resume():
    policy = lr.LimitResumePolicy()
    policy.consider("s1", card(), [], NOW)
    policy.enabled = False
    assert policy.consider("s1", card(), [], NOW + 10 ** 5) == lr.PASS
    assert policy.due_at("s1") is None


def test_forget_drops_sessions_that_are_gone():
    policy = lr.LimitResumePolicy()
    policy.consider("s1", card(), [], NOW)
    policy.consider("s2", card(), [], NOW)
    policy.forget({"s2"})
    assert policy.due_at("s1") is None and policy.due_at("s2") is not None


# ── the daemon ───────────────────────────────────────────────────────────────

def _daemon(monkeypatch, reachable=True):
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon import vscode_reveal as vr

    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": reachable)
    daemon = BobDaemon(headless=True)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    daemon._active_notifications["s1"] = card("limit reached|1000000000")
    return daemon


def _snapshot(**row):
    entry = {"session_id": "s1", "provider": "claude", "pid": 4242}
    entry.update(row)
    return {"needs_you": [entry], "finished": []}


def _due(daemon, monkeypatch):
    """Open the episode, then move the clock past its blind retry."""
    from dark_army_daemon import daemon as daemon_mod

    snap = _snapshot()
    daemon._decide_limit_resumes(snap)
    assert daemon._limit_resume_queue == []
    real = daemon_mod.time.time
    monkeypatch.setattr(
        daemon_mod.time, "time",
        lambda: real() + lr.UNKNOWN_RESET_RETRY_SECONDS + 1)
    return snap


def _flush(daemon):
    """Run the flush and wait on the lane entries it submitted."""
    async def run():
        lanes = daemon._steering_lanes()
        futures, real = [], lanes.submit
        lanes.submit = lambda *a, **k: futures.append(real(*a, **k)) or futures[-1]
        await daemon._flush_limit_resumes()
        for future in futures:
            if future is not None:
                await future
        await asyncio.sleep(0)       # the done-callbacks
    asyncio.run(run())


def test_the_row_says_when_and_nothing_is_typed_before_then(monkeypatch):
    daemon = _daemon(monkeypatch)
    snap = _snapshot()
    daemon._decide_limit_resumes(snap)
    assert daemon._limit_resume_queue == []
    assert snap["needs_you"][0]["limit_resume_at"] > 0


def test_a_due_session_is_queued_and_continue_is_typed_after_the_kill_line(monkeypatch):
    daemon = _daemon(monkeypatch)
    sent = []

    async def _send(pid, tty, text, newline=True):
        sent.append((pid, text))
        return {"matched": True, "sent": True, "terminalName": "zsh"}

    monkeypatch.setattr("dark_army_daemon.vscode_reveal.send_text", _send)
    _due(daemon, monkeypatch)
    daemon._decide_limit_resumes(_snapshot())
    assert daemon._limit_resume_queue == [("s1", 4242)]
    _flush(daemon)
    assert sent == [(4242, "\x15continue")]
    assert daemon._limit_resume_queue == []
    assert daemon.__dict__["_limit_resume"]._episodes["s1"].attempts == 1


@pytest.mark.parametrize("row", [
    {"provider": "codex"}, {"provider": "grok"}, {"kind": "background"}])
def test_only_a_claude_session_with_an_input_line_is_considered(monkeypatch, row):
    daemon = _daemon(monkeypatch)
    snap = _snapshot(**row)
    daemon._decide_limit_resumes(snap)
    assert "limit_resume_at" not in snap["needs_you"][0]
    assert daemon.__dict__["_limit_resume"]._episodes == {}


def test_an_unreachable_session_is_not_queued(monkeypatch):
    daemon = _daemon(monkeypatch, reachable=False)
    _due(daemon, monkeypatch)
    daemon._decide_limit_resumes(_snapshot())
    assert daemon._limit_resume_queue == []
    assert daemon.__dict__["_limit_resume"]._episodes["s1"].unreachable == 1


def test_the_switch_off_stops_the_daemon_deciding_anything(monkeypatch):
    daemon = _daemon(monkeypatch)
    daemon.limit_resume_enabled = False
    snap = _snapshot()
    daemon._decide_limit_resumes(snap)
    assert snap["needs_you"][0]["limit_resume_at"] is None
    assert daemon._limit_resume_queue == []


def test_a_session_on_a_permission_prompt_is_not_typed_at(monkeypatch):
    """`sendText` ends in a newline a tool-approval dialog reads as confirm."""
    daemon = _daemon(monkeypatch)
    sent = []

    async def _send(pid, tty, text, newline=True):
        sent.append(text)
        return {"sent": True}

    monkeypatch.setattr("dark_army_daemon.vscode_reveal.send_text", _send)
    _due(daemon, monkeypatch)
    daemon._decide_limit_resumes(_snapshot())
    monkeypatch.setattr(daemon, "_prompts_by_session", lambda: {"s1": {}})
    _flush(daemon)
    assert sent == []
    episode = daemon.__dict__["_limit_resume"]._episodes["s1"]
    assert (episode.attempts, episode.unreachable) == (0, 1)


def test_a_person_who_typed_first_is_not_typed_over(monkeypatch):
    daemon = _daemon(monkeypatch)
    sent = []

    async def _send(pid, tty, text, newline=True):
        sent.append(text)
        return {"sent": True}

    monkeypatch.setattr("dark_army_daemon.vscode_reveal.send_text", _send)
    _due(daemon, monkeypatch)
    daemon._decide_limit_resumes(_snapshot())
    daemon._active_notifications.pop("s1")          # their prompt cleared it
    _flush(daemon)
    assert sent == []
    assert daemon.__dict__["_limit_resume"]._episodes["s1"].unreachable == 0


def test_a_send_that_does_not_land_goes_back_to_the_policy(monkeypatch):
    daemon = _daemon(monkeypatch)

    async def _send(pid, tty, text, newline=True):
        return None

    monkeypatch.setattr("dark_army_daemon.vscode_reveal.send_text", _send)
    _due(daemon, monkeypatch)
    daemon._decide_limit_resumes(_snapshot())
    _flush(daemon)
    episode = daemon.__dict__["_limit_resume"]._episodes["s1"]
    assert (episode.attempts, episode.unreachable, episode.sent_at) == (0, 1, None)


def test_the_typing_rides_the_steering_lane_and_never_the_channel():
    import inspect
    from dark_army_daemon.daemon import BobDaemon

    flush = inspect.getsource(BobDaemon._flush_limit_resumes)
    assert "_steer_nowait" in flush and "send_text" not in flush
    body = inspect.getsource(BobDaemon._limit_resume_now)
    assert "push_channel_event" not in body and "send_text" in body
    assert "_limit_resume_now" in steering.TYPING_SITES
    assert steering.KINDS["limit_resume"] == steering.Kind(False, False, "own")
