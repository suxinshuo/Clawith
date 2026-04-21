"""Git credential helper — build auth env vars from resolved credentials.

Uses git's GIT_CONFIG_COUNT/GIT_CONFIG_KEY_*/GIT_CONFIG_VALUE_* mechanism
(git 2.31+) to inject HTTP auth headers without writing files or embedding
tokens in command lines.
"""

import base64
import re
from loguru import logger


def detect_git_platform(repo_url: str) -> str:
    """Detect git platform from repo URL.

    Returns: "github", "gitlab", or "gitea" (default for unknown platforms).
    """
    host = extract_repo_host(repo_url)
    if not host:
        return "gitea"

    host_lower = host.lower()
    if "github.com" in host_lower or "github" in host_lower:
        return "github"
    if "gitlab" in host_lower:
        return "gitlab"
    return "gitea"


def extract_repo_host(repo_url: str) -> str:
    """Extract hostname from git URL (HTTPS or SSH)."""
    url = repo_url.strip()

    # SSH: git@github.com:org/repo.git
    ssh_match = re.match(r"git@([^:]+):", url)
    if ssh_match:
        return ssh_match.group(1)

    # HTTPS: https://github.com/org/repo.git
    https_match = re.match(r"https?://([^/]+)/", url)
    if https_match:
        return https_match.group(1)

    return ""


def extract_owner_repo(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from git URL. Returns ("", "") on failure."""
    url = repo_url.strip()

    # SSH: git@github.com:org/repo.git
    ssh_match = re.match(r"git@[^:]+:([^/]+)/([^/]+?)(?:\.git)?$", url)
    if ssh_match:
        return ssh_match.group(1), ssh_match.group(2)

    # HTTPS: https://github.com/org/repo.git
    https_match = re.match(r"https?://[^/]+/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if https_match:
        return https_match.group(1), https_match.group(2)

    return "", ""


def build_git_auth_env(repo_url: str, token: str | None) -> dict[str, str]:
    """Build git auth environment variables using GIT_CONFIG_COUNT mechanism.

    This injects an HTTP Authorization header scoped to the repo's host.
    Works for clone, push, pull, fetch — any HTTPS git operation.
    No files written, no tokens in command line.

    Args:
        repo_url: The git repository URL.
        token: The access token. Returns empty dict if not provided.

    Returns:
        Dict of environment variables to pass to the git subprocess.
    """
    if not token:
        return {}

    host = extract_repo_host(repo_url)
    if not host:
        logger.warning(f"[GitCredHelper] Cannot extract host from URL: {repo_url}")
        return {}

    platform = detect_git_platform(repo_url)

    # GitHub: username is "x-access-token"
    # GitLab: username is "oauth2"
    # Gitea: username is "x-access-token" (same as GitHub)
    if platform == "gitlab":
        username = "oauth2"
    else:
        username = "x-access-token"

    auth_string = f"{username}:{token}"
    b64_auth = base64.b64encode(auth_string.encode()).decode()

    return {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {b64_auth}",
    }


def get_credential_provider(repo_url: str) -> str:
    """Map repo URL to credential provider name for CredentialResolver.

    The provider name must match what users configure in their credentials.
    """
    platform = detect_git_platform(repo_url)
    if platform == "github":
        return "github"
    elif platform == "gitlab":
        return "gitlab"
    else:
        return "gitea"
