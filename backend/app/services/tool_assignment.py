"""Create the explicit AgentTool records the tool panel and LLM loader expect.

Both `get_agent_tools` (panel) and `get_agent_tools_for_llm` treat an AgentTool
row as the authority and fall back to `Tool.is_default` only when no row exists.
Seeded agents received rows from the agent seeder, but user-created agents
received none, so the panel's backfill — guarded by `if assignments:` — never
ran for them. Writing rows at creation removes that asymmetry.

Feishu is handled separately: its tools stay invisible in the panel until the
channel is configured, so "no row" must keep meaning "not decided yet" rather
than "disabled". `assign_feishu_tool_records` fills them in once credentials
land, and never rewrites a row the user already owns.
"""

import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool import AgentTool, Tool
from app.services.agent_tools import _HIDDEN_FROM_LLM_TOOL_NAMES

FEISHU_TOOL_CATEGORY = "feishu"


async def _existing_assignment_tool_ids(
    db: AsyncSession, agent_id: uuid.UUID
) -> set[uuid.UUID]:
    """Return tool IDs this agent already has an explicit decision for."""
    result = await db.execute(
        select(AgentTool).where(AgentTool.agent_id == agent_id)
    )
    return {row.tool_id for row in result.scalars().all()}


async def assign_default_tool_records(
    db: AsyncSession, agent_id: uuid.UUID
) -> int:
    """Mirror `Tool.is_default` into explicit AgentTool rows for one agent.

    Covers the builtin platform catalog only. Admin- and agent-scoped tools are
    still picked up by the panel backfill, whose `if assignments:` precondition
    these rows satisfy. Returns the number of rows created.
    """
    assigned = await _existing_assignment_tool_ids(db, agent_id)
    result = await db.execute(
        select(Tool).where(Tool.enabled.is_(True), Tool.source == "builtin")
    )

    created = 0
    for tool in result.scalars().all():
        if tool.id in assigned:
            continue
        # Feishu rows are owned by assign_feishu_tool_records.
        if tool.category == FEISHU_TOOL_CATEGORY:
            continue
        # A row for a tool the model can never receive would be a dead switch.
        if tool.name in _HIDDEN_FROM_LLM_TOOL_NAMES:
            continue
        db.add(
            AgentTool(
                agent_id=agent_id,
                tool_id=tool.id,
                enabled=bool(tool.is_default),
            )
        )
        created += 1

    if created:
        logger.info(
            "[Tools] agent={} seeded {} default AgentTool records", agent_id, created
        )
    return created


async def assign_feishu_tool_records(
    db: AsyncSession, agent_id: uuid.UUID
) -> int:
    """Enable the Feishu tool family for an agent whose channel is configured.

    Restores the pre-v1.11.2 outcome (a configured channel yields usable Feishu
    tools) without reintroducing the always-inject list that bypassed
    assignment. Only missing rows are created, so a tool the user switched off
    stays off. Returns the number of rows created.
    """
    assigned = await _existing_assignment_tool_ids(db, agent_id)
    result = await db.execute(
        select(Tool).where(
            Tool.enabled.is_(True),
            Tool.category == FEISHU_TOOL_CATEGORY,
        )
    )

    created = 0
    for tool in result.scalars().all():
        if tool.id in assigned:
            continue
        if tool.name in _HIDDEN_FROM_LLM_TOOL_NAMES:
            continue
        db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True))
        created += 1

    if created:
        logger.info(
            "[Tools] agent={} enabled {} Feishu tools after channel configuration",
            agent_id,
            created,
        )
    return created
