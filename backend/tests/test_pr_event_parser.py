import pytest
from app.services.pr_event_parser import parse_pr_event, PrEvent


def test_parse_github_pr_opened():
    payload = {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "title": "Add login feature",
            "body": "Implements user authentication",
            "html_url": "https://github.com/org/repo/pull/42",
            "head": {"ref": "agent/frontend-dev/login"},
            "base": {"ref": "main"},
            "user": {"login": "bot-user"},
            "merged": False,
        },
        "repository": {
            "full_name": "org/repo",
            "html_url": "https://github.com/org/repo",
        },
    }
    headers = {"x-github-event": "pull_request"}

    event = parse_pr_event(payload, headers)
    assert event is not None
    assert event.platform == "github"
    assert event.action == "opened"
    assert event.pr_number == 42
    assert event.title == "Add login feature"
    assert event.head_branch == "agent/frontend-dev/login"
    assert event.base_branch == "main"
    assert event.url == "https://github.com/org/repo/pull/42"
    assert event.repo_full_name == "org/repo"


def test_parse_github_pr_merged():
    payload = {
        "action": "closed",
        "pull_request": {
            "number": 42, "title": "Add login feature", "body": "",
            "html_url": "https://github.com/org/repo/pull/42",
            "head": {"ref": "feature/login"}, "base": {"ref": "main"},
            "user": {"login": "dev"}, "merged": True,
        },
        "repository": {"full_name": "org/repo", "html_url": "https://github.com/org/repo"},
    }
    headers = {"x-github-event": "pull_request"}
    event = parse_pr_event(payload, headers)
    assert event is not None
    assert event.action == "merged"


def test_parse_github_pr_comment():
    payload = {
        "action": "created",
        "comment": {
            "body": "LGTM!",
            "user": {"login": "reviewer"},
            "html_url": "https://github.com/org/repo/pull/42#issuecomment-123",
        },
        "issue": {
            "number": 42, "title": "Add login feature",
            "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/42"},
        },
        "repository": {"full_name": "org/repo", "html_url": "https://github.com/org/repo"},
    }
    headers = {"x-github-event": "issue_comment"}
    event = parse_pr_event(payload, headers)
    assert event is not None
    assert event.action == "comment"
    assert event.comment_body == "LGTM!"
    assert event.comment_author == "reviewer"


def test_parse_gitlab_mr_opened():
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {
            "action": "open", "iid": 10, "title": "Add login feature",
            "description": "Auth implementation",
            "url": "https://gitlab.com/org/repo/-/merge_requests/10",
            "source_branch": "agent/backend-dev/login", "target_branch": "main",
            "state": "opened",
        },
        "user": {"username": "bot-user"},
        "project": {"path_with_namespace": "org/repo", "web_url": "https://gitlab.com/org/repo"},
    }
    headers = {"x-gitlab-event": "Merge Request Hook"}
    event = parse_pr_event(payload, headers)
    assert event is not None
    assert event.platform == "gitlab"
    assert event.action == "opened"
    assert event.pr_number == 10
    assert event.head_branch == "agent/backend-dev/login"


def test_parse_non_pr_event_returns_none():
    payload = {"action": "push", "ref": "refs/heads/main"}
    headers = {"x-github-event": "push"}
    event = parse_pr_event(payload, headers)
    assert event is None
