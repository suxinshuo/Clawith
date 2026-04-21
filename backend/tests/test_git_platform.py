import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.git_platform import create_pull_request, _create_github_pr, _create_gitlab_mr


@pytest.mark.asyncio
async def test_create_github_pr_success():
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "html_url": "https://github.com/org/repo/pull/42",
        "number": 42,
        "title": "Add feature",
    }

    with patch("httpx.AsyncClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.post.return_value = mock_response
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = client_instance

        result = await _create_github_pr(
            owner="org", repo="repo", token="ghp_test123",
            title="Add feature", body="Description", base="main", head="feature",
        )
        assert result["success"] is True
        assert result["url"] == "https://github.com/org/repo/pull/42"
        assert result["number"] == 42


@pytest.mark.asyncio
async def test_create_github_pr_conflict():
    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.json.return_value = {
        "message": "Validation Failed",
        "errors": [{"message": "A pull request already exists"}],
    }

    with patch("httpx.AsyncClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.post.return_value = mock_response
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = client_instance

        result = await _create_github_pr(
            owner="org", repo="repo", token="ghp_test",
            title="Add feature", body="", base="main", head="feature",
        )
        assert result["success"] is False
        assert "already exists" in result["error"]


@pytest.mark.asyncio
async def test_create_gitlab_mr_success():
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "web_url": "https://gitlab.com/org/repo/-/merge_requests/10",
        "iid": 10,
        "title": "Add feature",
    }

    with patch("httpx.AsyncClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.post.return_value = mock_response
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = client_instance

        result = await _create_gitlab_mr(
            host="gitlab.com", owner="org", repo="repo", token="glpat-test",
            title="Add feature", body="Description", base="main", head="feature",
        )
        assert result["success"] is True
        assert result["url"] == "https://gitlab.com/org/repo/-/merge_requests/10"


@pytest.mark.asyncio
async def test_create_pull_request_dispatches_github():
    with patch("app.services.git_platform._create_github_pr", new_callable=AsyncMock) as mock_gh:
        mock_gh.return_value = {"success": True, "url": "https://github.com/org/repo/pull/1", "number": 1}
        result = await create_pull_request(
            repo_url="https://github.com/org/repo.git",
            token="ghp_test", title="Test", body="", base="main", head="feature",
        )
        mock_gh.assert_called_once()
        assert result["success"] is True
