import pytest
import uuid
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_check_dev_permission_all_mode():
    """When dev_tools_access_mode is 'all', any user with agent access can use dev tools."""
    from app.services.agent_tools import _check_dev_permission

    agent = MagicMock()
    agent.dev_tools_access_mode = "all"
    agent.id = uuid.uuid4()

    result = await _check_dev_permission(agent, uuid.uuid4())
    assert result is True


@pytest.mark.asyncio
async def test_check_dev_permission_restricted_no_permission():
    """When restricted and user has no dev_tools permission, should be denied."""
    from app.services.agent_tools import _check_dev_permission

    agent = MagicMock()
    agent.dev_tools_access_mode = "restricted"
    agent.id = uuid.uuid4()
    user_id = uuid.uuid4()

    with patch("app.services.agent_tools.async_session") as mock_session:
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _check_dev_permission(agent, user_id)
        assert result is False


@pytest.mark.asyncio
async def test_check_dev_permission_restricted_with_permission():
    """When restricted and user has dev_tools permission, should be allowed."""
    from app.services.agent_tools import _check_dev_permission

    agent = MagicMock()
    agent.dev_tools_access_mode = "restricted"
    agent.id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_permission = MagicMock()
    mock_permission.id = uuid.uuid4()

    with patch("app.services.agent_tools.async_session") as mock_session:
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_permission
        mock_db.execute.return_value = mock_result
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _check_dev_permission(agent, user_id)
        assert result is True
