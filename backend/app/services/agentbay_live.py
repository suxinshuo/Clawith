"""AgentBay live preview helpers.

Provides utility functions for fetching live preview data
(VNC URL, browser snapshots) from active AgentBay sessions.
These are used by the WebSocket handler to push real-time
preview updates to the frontend.
"""

import uuid
from typing import Optional

from loguru import logger


async def get_desktop_live_url(agent_id: uuid.UUID) -> Optional[str]:
    """Get the VNC viewer URL for an agent's active computer session.

    Searches for any active computer session for this agent in the
    session cache, then calls get_link() to obtain the VNC URL.
    Returns None if no computer session is active or the URL
    cannot be retrieved.
    """
    from app.services.agentbay_client import _agentbay_sessions

    # Log all available sessions for debugging
    logger.info(f"[LivePreview] Looking up desktop session for agent {agent_id}")
    logger.info(f"[LivePreview] Available sessions: {list(_agentbay_sessions.keys())}")

    # Try exact key first, then search for any computer-type session
    cache_key = (agent_id, "computer")
    if cache_key not in _agentbay_sessions:
        # Also try UUID string comparison in case of type mismatch
        found = False
        for key in _agentbay_sessions:
            key_agent_id, key_type = key
            if str(key_agent_id) == str(agent_id) and key_type == "computer":
                cache_key = key
                found = True
                logger.info(f"[LivePreview] Found session via string match: {cache_key}")
                break
        if not found:
            logger.warning(f"[LivePreview] No computer session found for agent {agent_id}")
            return None

    client, _last_used = _agentbay_sessions[cache_key]
    logger.info(f"[LivePreview] Found computer session, calling get_live_url()")
    url = await client.get_live_url()
    logger.info(f"[LivePreview] get_live_url() returned: {str(url)[:100] if url else 'None'}")
    return url


async def get_browser_snapshot(agent_id: uuid.UUID) -> Optional[str]:
    """Get a base64-encoded screenshot of an agent's active browser session.

    Returns data:image/jpeg;base64,... string or None if no browser
    session is active or the screenshot fails.
    """
    from app.services.agentbay_client import _agentbay_sessions

    logger.info(f"[LivePreview] Looking up browser session for agent {agent_id}")

    cache_key = (agent_id, "browser")
    if cache_key not in _agentbay_sessions:
        # String-based fallback lookup
        for key in _agentbay_sessions:
            key_agent_id, key_type = key
            if str(key_agent_id) == str(agent_id) and key_type == "browser":
                cache_key = key
                break
        else:
            logger.warning(f"[LivePreview] No browser session found for agent {agent_id}")
            return None

    client, _last_used = _agentbay_sessions[cache_key]
    return await client.get_browser_snapshot_base64()


def detect_agentbay_env(tool_name: str) -> Optional[str]:
    """Detect which AgentBay environment a tool belongs to.

    Returns 'desktop', 'browser', 'code', or None if not an AgentBay tool.
    """
    if tool_name.startswith("agentbay_computer_"):
        return "desktop"
    if tool_name.startswith("agentbay_browser_"):
        return "browser"
    if tool_name in ("agentbay_code_execute", "agentbay_command_exec"):
        return "code"
    return None
