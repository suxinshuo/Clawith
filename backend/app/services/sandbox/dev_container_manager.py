"""Dev container lifecycle manager — create, track idle, cleanup."""

import time
from loguru import logger

# In-memory tracking: agent_id -> last_activity_timestamp
_container_last_activity: dict[str, float] = {}


def record_activity(agent_id: str) -> None:
    """Record that an agent's dev container was just used."""
    _container_last_activity[agent_id] = time.time()


def get_idle_seconds(agent_id: str) -> float:
    """Get seconds since last activity for an agent's container."""
    last = _container_last_activity.get(agent_id)
    if last is None:
        return float("inf")
    return time.time() - last


def remove_tracking(agent_id: str) -> None:
    """Remove tracking for an agent's container."""
    _container_last_activity.pop(agent_id, None)


def get_all_tracked() -> dict[str, float]:
    """Get all tracked containers with their last activity timestamps."""
    return dict(_container_last_activity)


async def cleanup_idle_containers(idle_timeout: int = 1800) -> int:
    """Scan and remove containers idle longer than timeout. Returns count removed."""
    try:
        import docker
        client = docker.from_env()
    except Exception as e:
        logger.warning(f"[DevContainer] Cannot connect to Docker for cleanup: {e}")
        return 0

    removed = 0
    containers = client.containers.list(filters={"label": "clawith.dev_container=true"})

    for container in containers:
        agent_id = container.labels.get("clawith.agent_id", "")
        idle_secs = get_idle_seconds(agent_id)

        if idle_secs > idle_timeout:
            try:
                container.remove(force=True)
                remove_tracking(agent_id)
                removed += 1
                logger.info(f"[DevContainer] Removed idle container for agent {agent_id} (idle {idle_secs:.0f}s)")
            except Exception as e:
                logger.warning(f"[DevContainer] Failed to remove container: {e}")

    return removed
