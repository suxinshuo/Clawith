"""Activity logger — simple async function to record agent actions."""

import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.activity_log import AgentActivityLog

# The valid action_type values stored in AgentActivityLog.action_type.
# Mirrors the Enum in app/models/activity_log.py.
ACTIVITY_ACTION_TYPES = frozenset({
    "chat_reply", "tool_call", "feishu_msg_sent", "agent_msg_sent",
    "web_msg_sent", "task_created", "task_updated", "file_written",
    "error", "schedule_run", "heartbeat", "plaza_post",
})

_DEFAULT_LIMIT = 30
_MAX_LIMIT = 100


def _normalize_activity_query(arguments: dict) -> dict:
    """Parse and clamp the get_activity_log tool arguments into safe values.

    Pure function — no DB access. Garbage values fall back to safe defaults
    rather than raising, so a malformed LLM tool call never breaks the loop.
    """
    arguments = arguments or {}

    # limit: int in 1..100, default 30
    try:
        limit = int(arguments.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    limit = max(1, min(_MAX_LIMIT, limit))

    # hours: positive int, else None (no window); 0 means no window
    hours = None
    try:
        h = int(arguments.get("hours")) if arguments.get("hours") is not None else None
        if h and h > 0:
            hours = h
    except (TypeError, ValueError):
        hours = None

    # action_types: list of valid enum values, else None
    raw_types = arguments.get("action_types")
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    action_types = None
    if isinstance(raw_types, (list, tuple)):
        valid = [t for t in raw_types if t in ACTIVITY_ACTION_TYPES]
        action_types = valid or None

    # keyword: non-empty stripped string, else None
    raw_kw = arguments.get("keyword")
    keyword = raw_kw.strip() if isinstance(raw_kw, str) and raw_kw.strip() else None

    return {"limit": limit, "hours": hours, "action_types": action_types, "keyword": keyword}


async def log_activity(
    agent_id: uuid.UUID,
    action_type: str,
    summary: str,
    detail: dict | None = None,
    related_id: uuid.UUID | None = None,
) -> None:
    """Record an agent activity. Fire-and-forget, never raises."""
    try:
        async with async_session() as db:
            db.add(AgentActivityLog(
                agent_id=agent_id,
                action_type=action_type,
                summary=summary,
                detail_json=detail,
                related_id=related_id,
            ))
            await db.commit()
    except Exception as e:
        logger.error(f"[ActivityLog] Failed to log {action_type}: {e}")
