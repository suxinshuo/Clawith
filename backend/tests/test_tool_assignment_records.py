"""Explicit AgentTool records must exist from agent creation onward.

The tool panel and get_agent_tools_for_llm both treat an AgentTool row as the
authority; the is_default fallback is only a legacy safety net. Seeded agents
already got rows, user-created agents got none, so the panel backfill (guarded
by `if assignments:`) never ran for them. These tests pin the service that
closes that asymmetry, plus the Feishu family assignment that runs once the
channel credentials land.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS
from app.services.tool_assignment import (
    assign_default_tool_records,
    assign_feishu_tool_records,
)


class _ListResult:
    def __init__(self, values) -> None:
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _RecordingDB:
    """Returns a fixed tool catalog for every query and records added rows."""

    def __init__(self, tools, assignments=()) -> None:
        self._tools = list(tools)
        self._assignments = list(assignments)
        self._calls = 0
        self.added = []
        self.flushed = False

    async def execute(self, _statement):
        self._calls += 1
        # The services load assignments first, then the candidate tool rows.
        if self._calls == 1:
            return _ListResult(self._assignments)
        return _ListResult(self._tools)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flushed = True


def _tool_row(name: str):
    definition = next(
        item for item in BUILTIN_TOOL_DEFINITIONS if item["name"] == name
    )
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        category=definition["category"],
        is_default=bool(definition.get("is_default")),
        source="builtin",
        enabled=True,
    )


@pytest.mark.asyncio
async def test_default_assignment_mirrors_is_default():
    on = _tool_row("read_file")           # is_default=True
    off = _tool_row("jina_search")        # is_default=False
    assert on.is_default and not off.is_default
    db = _RecordingDB([on, off])

    created = await assign_default_tool_records(db, uuid.uuid4())

    assert created == 2
    by_tool = {row.tool_id: row.enabled for row in db.added}
    assert by_tool[on.id] is True
    assert by_tool[off.id] is False


@pytest.mark.asyncio
async def test_default_assignment_skips_feishu_family():
    """Absence of a Feishu row must keep meaning "not decided yet".

    assign_feishu_tool_records only creates missing rows, so writing
    enabled=False rows here would permanently suppress the family.
    """
    plain = _tool_row("read_file")
    feishu = _tool_row("feishu_calendar_list")
    db = _RecordingDB([plain, feishu])

    await assign_default_tool_records(db, uuid.uuid4())

    assert {row.tool_id for row in db.added} == {plain.id}


@pytest.mark.asyncio
async def test_default_assignment_skips_tools_hidden_from_the_model():
    plain = _tool_row("read_file")
    hidden = _tool_row("send_feishu_message")
    db = _RecordingDB([plain, hidden])

    await assign_default_tool_records(db, uuid.uuid4())

    assert {row.tool_id for row in db.added} == {plain.id}


@pytest.mark.asyncio
async def test_default_assignment_is_idempotent():
    existing = _tool_row("read_file")
    fresh = _tool_row("jina_search")
    db = _RecordingDB(
        [existing, fresh],
        assignments=[SimpleNamespace(tool_id=existing.id, enabled=True)],
    )

    created = await assign_default_tool_records(db, uuid.uuid4())

    assert created == 1
    assert {row.tool_id for row in db.added} == {fresh.id}


@pytest.mark.asyncio
async def test_feishu_assignment_enables_the_family():
    calendar = _tool_row("feishu_calendar_list")
    docs = _tool_row("feishu_doc_search")
    db = _RecordingDB([calendar, docs])

    created = await assign_feishu_tool_records(db, uuid.uuid4())

    assert created == 2
    assert all(row.enabled is True for row in db.added)
    assert {row.tool_id for row in db.added} == {calendar.id, docs.id}


@pytest.mark.asyncio
async def test_feishu_assignment_never_overrides_a_user_decision():
    disabled = _tool_row("feishu_drive_delete")
    fresh = _tool_row("feishu_calendar_list")
    db = _RecordingDB(
        [disabled, fresh],
        assignments=[SimpleNamespace(tool_id=disabled.id, enabled=False)],
    )

    created = await assign_feishu_tool_records(db, uuid.uuid4())

    assert created == 1
    assert {row.tool_id for row in db.added} == {fresh.id}


@pytest.mark.asyncio
async def test_feishu_assignment_skips_tools_hidden_from_the_model():
    hidden = _tool_row("send_feishu_message")
    db = _RecordingDB([hidden])

    created = await assign_feishu_tool_records(db, uuid.uuid4())

    assert created == 0
    assert db.added == []
