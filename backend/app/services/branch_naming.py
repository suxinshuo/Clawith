"""Branch naming conventions for agent isolation.

Agents work on isolated branches: agent/{agent_name_slug}/{context_id}
This prevents agents from directly modifying protected branches.
"""

import re
import unicodedata


def slugify_agent_name(name: str) -> str:
    """Convert agent name to a git-safe slug."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lower = ascii_only.lower()
    slug = re.sub(r"[^a-z0-9-]", "-", lower)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-")
    slug = slug[:40].rstrip("-")
    return slug or "agent"


def generate_agent_branch(agent_name: str, context_id: str) -> str:
    """Generate an isolated branch name for an agent.

    Format: agent/{agent_name_slug}/{context_id_slug}
    """
    name_slug = slugify_agent_name(agent_name)
    ctx_slug = re.sub(r"[^a-zA-Z0-9._/-]", "-", context_id)
    ctx_slug = re.sub(r"-{2,}", "-", ctx_slug).strip("-")
    ctx_slug = ctx_slug[:60]
    ctx_slug = ctx_slug.replace("/", "-")
    return f"agent/{name_slug}/{ctx_slug}"


def is_protected_branch(branch: str) -> bool:
    """Check if a branch name is protected."""
    protected_patterns = [
        r"^main$",
        r"^master$",
        r"^develop$",
        r"^release/",
        r"^hotfix/",
    ]
    for pattern in protected_patterns:
        if re.match(pattern, branch):
            return True
    return False
