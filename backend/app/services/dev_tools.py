"""Dev tools — execute_command and git tool implementations."""

import re
import uuid

from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.registry import get_sandbox_backend


def check_repo_allowed(repo_url: str, allowed_repos: list[str]) -> bool:
    """Check if a repo URL matches the whitelist.

    Supports:
    - Exact: "github.com/org/repo"
    - Wildcard: "github.com/org/*"
    - HTTPS: "https://github.com/org/repo.git"
    - SSH: "git@github.com:org/repo.git"
    """
    if not allowed_repos:
        return False

    # Normalize URL to "host/owner/repo" format
    normalized = _normalize_repo_url(repo_url)
    if not normalized:
        return False

    for pattern in allowed_repos:
        pattern = pattern.strip().rstrip("/")
        if pattern.endswith("/*"):
            prefix = pattern[:-1]  # "github.com/org/"
            if normalized.startswith(prefix):
                return True
        else:
            if normalized == pattern or normalized.startswith(pattern + "/"):
                return True

    return False


def _normalize_repo_url(url: str) -> str:
    """Normalize repo URL to 'host/owner/repo' format."""
    url = url.strip()
    # SSH format: git@github.com:org/repo.git
    ssh_match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
    if ssh_match:
        return f"{ssh_match.group(1)}/{ssh_match.group(2)}"
    # HTTPS format: https://github.com/org/repo.git
    https_match = re.match(r"https?://([^/]+)/(.+?)(?:\.git)?/?$", url)
    if https_match:
        return f"{https_match.group(1)}/{https_match.group(2)}"
    return url


def truncate_output(output: str, head: int = 200, tail: int = 50) -> str:
    """Truncate long output, keeping first `head` and last `tail` lines."""
    lines = output.split("\n")
    if len(lines) <= head + tail:
        return output
    omitted = len(lines) - head - tail
    return "\n".join(
        lines[:head]
        + [f"\n... (省略 {omitted} 行) ...\n"]
        + lines[-tail:]
    )


# Token patterns to redact from command output
_SENSITIVE_PATTERNS = [
    # GitHub PAT / OAuth / fine-grained tokens
    re.compile(r"(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{6,}"),
    # GitLab PAT
    re.compile(r"glpat-[A-Za-z0-9\-_]{20,}"),
    # Bearer / JWT tokens
    re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]{20,}"),
    # Basic auth in URLs: https://user:token@host
    re.compile(r"(https?://[^:]+:)[^@\s]{8,}(@)"),
    # Generic long hex/base64 strings that look like tokens (40+ chars)
    re.compile(r"(?<=[=:\s])[A-Za-z0-9+/\-_]{40,}(?=[&\s\n]|$)"),
]


def sanitize_output(text: str, secrets: list[str] | None = None) -> str:
    """Redact known token patterns and explicit secrets from command output."""
    if not text:
        return text

    result = text

    # Redact explicitly provided secrets first (highest priority)
    if secrets:
        for secret in secrets:
            if secret and len(secret) >= 8:
                result = result.replace(secret, "***")

    # Redact known patterns
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.groups >= 2:
            # Pattern with capture groups (e.g., URL auth) — preserve structure
            result = pattern.sub(r"\1***\2", result)
        else:
            result = pattern.sub("***", result)

    return result


async def execute_command_tool(
    command: str,
    cwd: str,
    timeout: int,
    agent_id: uuid.UUID,
    sandbox_config: SandboxConfig,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
) -> str:
    """Execute a shell command via sandbox backend. Returns formatted result string."""
    backend = get_sandbox_backend(sandbox_config)
    repos_dir = cwd

    result = await backend.execute_command(
        command=command,
        cwd=repos_dir,
        timeout=timeout,
        env=env,
        agent_id=str(agent_id),
        work_dir=repos_dir,
    )

    return _format_command_result(command, result, secrets=secrets)


async def git_tool(
    sub_command: str,
    cwd: str,
    agent_id: uuid.UUID,
    sandbox_config: SandboxConfig,
    env: dict[str, str] | None = None,
    timeout: int = 60,
    secrets: list[str] | None = None,
) -> str:
    """Execute a git command via sandbox backend."""
    command = f"git {sub_command}"
    backend = get_sandbox_backend(sandbox_config)

    result = await backend.execute_command(
        command=command,
        cwd=cwd,
        timeout=timeout,
        env=env,
        agent_id=str(agent_id),
        work_dir=cwd,
    )

    return _format_command_result(command, result, secrets=secrets)


def _format_command_result(command: str, result: ExecutionResult, secrets: list[str] | None = None) -> str:
    """Format ExecutionResult as a readable string for LLM."""
    parts = [f"$ {sanitize_output(command, secrets=secrets)}"]

    stdout = truncate_output(result.stdout.strip()) if result.stdout.strip() else ""
    stderr = result.stderr.strip() if result.stderr.strip() else ""

    if stdout:
        parts.append(sanitize_output(stdout, secrets=secrets))
    if stderr:
        parts.append(f"stderr: {sanitize_output(stderr, secrets=secrets)}")
    if result.error and result.exit_code != 0:
        parts.append(f"❌ {result.error}")
    elif result.exit_code == 0 and not stdout:
        parts.append("✅ Command executed successfully (no output)")

    parts.append(f"[exit_code={result.exit_code}, {result.duration_ms}ms]")
    return "\n".join(parts)
