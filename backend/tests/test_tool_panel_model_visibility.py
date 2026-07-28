"""The Agent tool panel must not offer tools the LLM can never receive.

`_HIDDEN_FROM_LLM_TOOL_NAMES` tools are filtered out of every model-facing
workset.  Listing them in the panel produces a switch that silently does
nothing, so the panel has to apply the same gate.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.tools import get_agent_tools
from app.services import agent_tools
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ListResult:
    def __init__(self, values) -> None:
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _QueuedDB:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.added = []

    async def execute(self, _statement):
        if not self._responses:
            raise AssertionError("unexpected database query")
        return self._responses.pop(0)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        return None


def _builtin_row(name: str):
    definition = next(
        item for item in BUILTIN_TOOL_DEFINITIONS if item["name"] == name
    )
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        display_name=definition.get("display_name", name),
        description=definition["description"],
        type=definition.get("type", "builtin"),
        category=definition["category"],
        icon=definition.get("icon"),
        is_default=bool(definition.get("is_default")),
        parameters_schema=definition["parameters_schema"],
        config=definition.get("config", {}),
        source="builtin",
        tenant_id=None,
        mcp_server_name=None,
        mcp_server_url=None,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_panel_hides_tools_that_can_never_reach_the_model(monkeypatch):
    hidden_name = "send_feishu_message"
    visible_name = "feishu_calendar_list"
    assert hidden_name in agent_tools._HIDDEN_FROM_LLM_TOOL_NAMES

    agent_id = uuid.uuid4()
    db = _QueuedDB(
        [
            _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=uuid.uuid4(), is_system=False)),
            _ListResult([]),
            _ListResult([_builtin_row(hidden_name), _builtin_row(visible_name)]),
        ]
    )

    async def has_feishu(_agent_id):
        return True

    monkeypatch.setattr(agent_tools, "_agent_has_feishu", has_feishu)

    listed = await get_agent_tools(
        agent_id=agent_id,
        current_user=SimpleNamespace(id=uuid.uuid4()),
        db=db,
    )

    names = {item["name"] for item in listed}
    assert visible_name in names
    assert hidden_name not in names
