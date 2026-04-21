"""Parse PR/MR event payloads from GitHub and GitLab webhooks."""

from dataclasses import dataclass


@dataclass
class PrEvent:
    """Structured PR/MR event data."""
    platform: str  # "github" | "gitlab"
    action: str  # "opened" | "closed" | "merged" | "comment" | "review_requested" | "approved"
    pr_number: int
    title: str
    body: str
    url: str
    head_branch: str
    base_branch: str
    repo_full_name: str
    author: str = ""
    comment_body: str = ""
    comment_author: str = ""


def parse_pr_event(payload: dict, headers: dict) -> PrEvent | None:
    """Parse a webhook payload into a PrEvent. Returns None if not a PR event."""
    h = {k.lower(): v for k, v in headers.items()}
    gh_event = h.get("x-github-event", "")
    if gh_event:
        return _parse_github(payload, gh_event)
    gl_event = h.get("x-gitlab-event", "")
    if gl_event:
        return _parse_gitlab(payload, gl_event)
    return None


def _parse_github(payload: dict, event_type: str) -> PrEvent | None:
    repo = payload.get("repository", {})
    repo_name = repo.get("full_name", "")

    if event_type == "pull_request":
        pr = payload.get("pull_request", {})
        action = payload.get("action", "")
        if action == "closed" and pr.get("merged"):
            action = "merged"
        return PrEvent(
            platform="github", action=action,
            pr_number=pr.get("number", 0), title=pr.get("title", ""),
            body=pr.get("body", "") or "", url=pr.get("html_url", ""),
            head_branch=pr.get("head", {}).get("ref", ""),
            base_branch=pr.get("base", {}).get("ref", ""),
            repo_full_name=repo_name, author=pr.get("user", {}).get("login", ""),
        )

    if event_type == "issue_comment":
        issue = payload.get("issue", {})
        if "pull_request" not in issue:
            return None
        comment = payload.get("comment", {})
        return PrEvent(
            platform="github", action="comment",
            pr_number=issue.get("number", 0), title=issue.get("title", ""),
            body="", url=comment.get("html_url", ""),
            head_branch="", base_branch="", repo_full_name=repo_name,
            comment_body=comment.get("body", ""),
            comment_author=comment.get("user", {}).get("login", ""),
        )

    if event_type == "pull_request_review":
        pr = payload.get("pull_request", {})
        review = payload.get("review", {})
        action = payload.get("action", "")
        if review.get("state", "") == "approved":
            action = "approved"
        return PrEvent(
            platform="github", action=action,
            pr_number=pr.get("number", 0), title=pr.get("title", ""),
            body="", url=review.get("html_url", ""),
            head_branch=pr.get("head", {}).get("ref", ""),
            base_branch=pr.get("base", {}).get("ref", ""),
            repo_full_name=repo_name,
            comment_body=review.get("body", "") or "",
            comment_author=review.get("user", {}).get("login", ""),
        )

    return None


def _parse_gitlab(payload: dict, event_type: str) -> PrEvent | None:
    if event_type not in ("Merge Request Hook", "Note Hook"):
        return None
    project = payload.get("project", {})
    repo_name = project.get("path_with_namespace", "")

    if event_type == "Merge Request Hook":
        attrs = payload.get("object_attributes", {})
        action = attrs.get("action", "")
        action_map = {"open": "opened", "reopen": "opened", "close": "closed", "merge": "merged", "update": "updated", "approved": "approved"}
        normalized = action_map.get(action, action)
        if attrs.get("state") == "merged":
            normalized = "merged"
        return PrEvent(
            platform="gitlab", action=normalized,
            pr_number=attrs.get("iid", 0), title=attrs.get("title", ""),
            body=attrs.get("description", "") or "", url=attrs.get("url", ""),
            head_branch=attrs.get("source_branch", ""), base_branch=attrs.get("target_branch", ""),
            repo_full_name=repo_name, author=payload.get("user", {}).get("username", ""),
        )

    if event_type == "Note Hook":
        if payload.get("object_attributes", {}).get("noteable_type") != "MergeRequest":
            return None
        note = payload.get("object_attributes", {})
        mr = payload.get("merge_request", {})
        return PrEvent(
            platform="gitlab", action="comment",
            pr_number=mr.get("iid", 0), title=mr.get("title", ""),
            body="", url=note.get("url", ""),
            head_branch=mr.get("source_branch", ""), base_branch=mr.get("target_branch", ""),
            repo_full_name=repo_name,
            comment_body=note.get("note", ""),
            comment_author=payload.get("user", {}).get("username", ""),
        )
    return None


def format_pr_event_context(event: PrEvent) -> str:
    """Format a PrEvent into a human-readable context string for agent prompts."""
    parts = [
        f"PR Event: {event.action.upper()}",
        f"Repository: {event.repo_full_name}",
        f"PR #{event.pr_number}: {event.title}",
        f"URL: {event.url}",
        f"Branches: {event.head_branch} → {event.base_branch}",
    ]
    if event.author:
        parts.append(f"Author: {event.author}")
    if event.body:
        body_preview = event.body[:300] + ("..." if len(event.body) > 300 else "")
        parts.append(f"Description: {body_preview}")
    if event.comment_body:
        parts.append(f"Comment by {event.comment_author}: {event.comment_body[:300]}")
    return "\n".join(parts)
