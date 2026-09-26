"""Activity order is irrelevant; changes to terminal identity still invalidate it."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from dark_army_daemon import codex_terminal as terminal
from dark_army_daemon.daemon import BobDaemon
from tests.test_codex_terminal import fixture  # noqa: F401


@pytest.mark.asyncio
async def test_activity_reordering_does_not_bypass_refresh_throttle(fixture, monkeypatch):
    records, captured, _, _ = fixture
    daemon = BobDaemon()
    daemon._codex_records = {r.session_id: r for r in records}
    resolve = AsyncMock(return_value=await terminal.resolve(captured))
    monkeypatch.setattr(terminal, "resolve", resolve)
    monkeypatch.setattr(daemon, "_schedule_agents_push", lambda: None)
    await daemon._refresh_codex_terminals()
    await daemon._codex_terminal_task
    daemon._codex_records = dict(reversed(list(daemon._codex_records.items())))
    await daemon._refresh_codex_terminals()
    await daemon._codex_terminal_task
    resolve.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["path", "cwd", "thread_id"])
async def test_identity_change_during_discovery_still_withdraws_targets(
        fixture, monkeypatch, field):
    records, captured, _, _ = fixture
    daemon = BobDaemon()
    daemon._codex_records = {r.session_id: r for r in records}
    result = await terminal.resolve(captured)
    daemon._codex_terminals = result
    ready = asyncio.Event()

    async def resolve(_):
        await ready.wait()
        return result

    monkeypatch.setattr(terminal, "resolve", resolve)
    monkeypatch.setattr(daemon, "_schedule_agents_push", lambda: None)
    await daemon._refresh_codex_terminals()
    record = records[0]
    value = record.path.with_name("replacement.jsonl") if field == "path" else "changed"
    setattr(record, field, value)
    ready.set()
    await daemon._codex_terminal_task
    assert daemon._codex_terminals == {}
