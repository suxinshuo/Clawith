"""Tests for the get_activity_log tool: arg normalization, query construction,
formatting, and execute_tool wiring.

NOTE: imports from app.services.agent_tools are deferred into test functions to
avoid the pre-existing circular import between agent_tools and llm.caller at
collection time. Importing app.services.activity_logger at module top-level is
safe (it only depends on app.database and app.models).
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.activity_logger import _normalize_activity_query


def test_normalize_defaults():
    out = _normalize_activity_query({})
    assert out == {"limit": 30, "hours": None, "action_types": None, "keyword": None}


def test_normalize_limit_clamped_high():
    assert _normalize_activity_query({"limit": 9999})["limit"] == 100


def test_normalize_limit_clamped_low_and_garbage():
    assert _normalize_activity_query({"limit": 0})["limit"] == 1
    assert _normalize_activity_query({"limit": -5})["limit"] == 1
    assert _normalize_activity_query({"limit": "abc"})["limit"] == 30  # falls back to default


def test_normalize_hours():
    assert _normalize_activity_query({"hours": 24})["hours"] == 24
    assert _normalize_activity_query({"hours": 0})["hours"] is None      # 0 = no window
    assert _normalize_activity_query({"hours": "bad"})["hours"] is None


def test_normalize_action_types_filters_invalid():
    out = _normalize_activity_query({"action_types": ["tool_call", "nonsense", "chat_reply"]})
    assert out["action_types"] == ["tool_call", "chat_reply"]
    # all-invalid collapses to None (no filter rather than match-nothing surprise)
    assert _normalize_activity_query({"action_types": ["nope"]})["action_types"] is None
    # a bare string is accepted and wrapped
    assert _normalize_activity_query({"action_types": "tool_call"})["action_types"] == ["tool_call"]


def test_normalize_keyword():
    assert _normalize_activity_query({"keyword": "  hello  "})["keyword"] == "hello"
    assert _normalize_activity_query({"keyword": "   "})["keyword"] is None
    assert _normalize_activity_query({"keyword": 123})["keyword"] is None


class _DummyResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _RecordingSession:
    """Async-context-manager fake that records the executed select statement
    and returns preset rows."""
    def __init__(self, rows):
        self._rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _DummyResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _compiled(statement) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_query_activities_scopes_and_filters():
    from app.services import activity_logger
    agent_id = uuid.uuid4()
    rows = [SimpleNamespace(action_type="tool_call", summary="x", created_at=datetime.now(timezone.utc))]
    session = _RecordingSession(rows)

    with patch.object(activity_logger, "async_session", lambda: session):
        result = await activity_logger.query_activities(
            agent_id, limit=5, hours=24, action_types=["tool_call"], keyword="foo",
        )

    assert result == rows
    sql = _compiled(session.statements[0]).lower()
    assert agent_id.hex in sql                  # agent scoping (UUID compiles to hex, no hyphens)
    assert "action_type in" in sql              # action_types filter
    assert "like" in sql                        # keyword filter (ilike compiles to LIKE)
    assert "created_at >=" in sql               # hours window
    assert "limit 5" in sql                     # limit applied


@pytest.mark.asyncio
async def test_query_activities_minimal_only_agent_and_limit():
    from app.services import activity_logger
    agent_id = uuid.uuid4()
    session = _RecordingSession([])

    with patch.object(activity_logger, "async_session", lambda: session):
        result = await activity_logger.query_activities(agent_id)

    assert result == []
    sql = _compiled(session.statements[0]).lower()
    assert agent_id.hex in sql
    assert "limit 30" in sql
    assert "action_type in" not in sql
    assert "like" not in sql


from app.services.activity_logger import format_activity_log


def _row(action_type, summary, dt):
    return SimpleNamespace(action_type=action_type, summary=summary, created_at=dt)


def test_format_empty():
    assert format_activity_log([], hours=24) == "No matching activity."


def test_format_orders_chronologically_with_header():
    # query returns newest-first; formatter must show oldest-first
    t_new = datetime(2026, 6, 3, 9, 15, tzinfo=timezone.utc)
    t_old = datetime(2026, 6, 3, 9, 14, tzinfo=timezone.utc)
    rows = [_row("chat_reply", "replied", t_new), _row("tool_call", "searched", t_old)]
    out = format_activity_log(rows, hours=24)
    lines = out.splitlines()
    assert lines[0] == "2 activity log entries (last 24h):"
    assert lines[1] == "- [06-03 09:14] tool_call: searched"
    assert lines[2] == "- [06-03 09:15] chat_reply: replied"


def test_format_no_window_header_and_truncation():
    long_summary = "a" * 300
    rows = [_row("error", long_summary, datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc))]
    out = format_activity_log(rows, hours=None)
    lines = out.splitlines()
    assert lines[0] == "1 activity log entry:"
    # summary truncated to 150 chars + ellipsis
    assert "a" * 150 + "…" in lines[1]
    assert "a" * 151 not in lines[1]


@pytest.mark.asyncio
async def test_execute_tool_get_activity_log_wires_query_and_format():
    from pathlib import Path
    from unittest.mock import AsyncMock
    from app.services import agent_tools

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    rows = [
        SimpleNamespace(action_type="tool_call", summary="searched web",
                        created_at=datetime(2026, 6, 3, 9, 14, tzinfo=timezone.utc)),
    ]

    with patch.object(agent_tools, "ensure_workspace", AsyncMock(return_value=Path("/tmp/ws"))), \
         patch.object(agent_tools, "_get_agent_tenant_id", AsyncMock(return_value=None)), \
         patch("app.services.activity_logger.query_activities", AsyncMock(return_value=rows)) as q:
        result = await agent_tools.execute_tool(
            "get_activity_log",
            {"hours": 24, "limit": 5, "action_types": ["tool_call"], "keyword": "web"},
            agent_id,
            user_id,
        )

    # query was called scoped to this agent with normalized kwargs
    q.assert_awaited_once()
    assert q.await_args.args[0] == agent_id
    assert q.await_args.kwargs == {"limit": 5, "hours": 24,
                                   "action_types": ["tool_call"], "keyword": "web"}
    # output is the formatted compact log
    assert result.splitlines()[0] == "1 activity log entry (last 24h):"
    assert "- [06-03 09:14] tool_call: searched web" in result


@pytest.mark.asyncio
async def test_execute_tool_get_activity_log_empty():
    from pathlib import Path
    from unittest.mock import AsyncMock
    from app.services import agent_tools

    with patch.object(agent_tools, "ensure_workspace", AsyncMock(return_value=Path("/tmp/ws"))), \
         patch.object(agent_tools, "_get_agent_tenant_id", AsyncMock(return_value=None)), \
         patch("app.services.activity_logger.query_activities", AsyncMock(return_value=[])):
        result = await agent_tools.execute_tool(
            "get_activity_log", {}, uuid.uuid4(), uuid.uuid4()
        )
    assert result == "No matching activity."


def test_get_activity_log_registered_in_both_catalogs():
    from app.services.agent_tools import AGENT_TOOLS
    from app.services.tool_seeder import BUILTIN_TOOLS
    agent_names = {t["function"]["name"] for t in AGENT_TOOLS}
    builtin_names = {t["name"] for t in BUILTIN_TOOLS}
    assert "get_activity_log" in agent_names
    assert "get_activity_log" in builtin_names
