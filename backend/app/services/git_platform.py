"""Git platform API clients for creating PRs/MRs.

Supports GitHub, GitLab, and Gitea-compatible APIs.
"""

import httpx
from loguru import logger
from urllib.parse import quote_plus

from app.services.git_credential_helper import (
    detect_git_platform,
    extract_repo_host,
    extract_owner_repo,
)


async def create_pull_request(
    repo_url: str,
    token: str,
    title: str,
    body: str = "",
    base: str = "main",
    head: str = "",
) -> dict:
    """Create a PR/MR on the detected platform."""
    if not head:
        return {"success": False, "error": "head branch is required"}

    platform = detect_git_platform(repo_url)
    owner, repo = extract_owner_repo(repo_url)
    host = extract_repo_host(repo_url)

    if not owner or not repo:
        return {"success": False, "error": f"Cannot parse owner/repo from URL: {repo_url}"}

    if platform == "github":
        return await _create_github_pr(owner, repo, token, title, body, base, head)
    elif platform == "gitlab":
        return await _create_gitlab_mr(host, owner, repo, token, title, body, base, head)
    else:
        return await _create_gitea_pr(host, owner, repo, token, title, body, base, head)


async def _create_github_pr(
    owner: str, repo: str, token: str,
    title: str, body: str, base: str, head: str,
) -> dict:
    """Create a GitHub Pull Request via REST API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body, "base": base, "head": head}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code == 201:
            data = resp.json()
            return {"success": True, "url": data["html_url"], "number": data["number"], "title": data["title"]}
        else:
            error_data = resp.json()
            error_msg = error_data.get("message", "")
            errors = error_data.get("errors", [])
            if errors:
                error_msg += " — " + "; ".join(e.get("message", "") for e in errors)
            return {"success": False, "error": f"GitHub API {resp.status_code}: {error_msg}"}
    except Exception as e:
        logger.exception("[GitPlatform] GitHub PR creation failed")
        return {"success": False, "error": f"Request failed: {str(e)[:200]}"}


async def _create_gitlab_mr(
    host: str, owner: str, repo: str, token: str,
    title: str, body: str, base: str, head: str,
) -> dict:
    """Create a GitLab Merge Request via REST API."""
    project_path = quote_plus(f"{owner}/{repo}")
    url = f"https://{host}/api/v4/projects/{project_path}/merge_requests"
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    payload = {"title": title, "description": body, "target_branch": base, "source_branch": head}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code == 201:
            data = resp.json()
            return {"success": True, "url": data["web_url"], "number": data["iid"], "title": data["title"]}
        else:
            error_data = resp.json()
            error_msg = error_data.get("message", error_data.get("error", ""))
            return {"success": False, "error": f"GitLab API {resp.status_code}: {error_msg}"}
    except Exception as e:
        logger.exception("[GitPlatform] GitLab MR creation failed")
        return {"success": False, "error": f"Request failed: {str(e)[:200]}"}


async def _create_gitea_pr(
    host: str, owner: str, repo: str, token: str,
    title: str, body: str, base: str, head: str,
) -> dict:
    """Create a Gitea/Forgejo Pull Request via REST API."""
    url = f"https://{host}/api/v1/repos/{owner}/{repo}/pulls"
    headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}
    payload = {"title": title, "body": body, "base": base, "head": head}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code == 201:
            data = resp.json()
            return {"success": True, "url": data.get("html_url", data.get("url", "")), "number": data.get("number", 0), "title": data.get("title", title)}
        else:
            error_msg = resp.text[:200]
            return {"success": False, "error": f"Gitea API {resp.status_code}: {error_msg}"}
    except Exception as e:
        logger.exception("[GitPlatform] Gitea PR creation failed")
        return {"success": False, "error": f"Request failed: {str(e)[:200]}"}
