import pytest
from app.services.dev_tools import sanitize_output


def test_sanitize_github_pat():
    text = "Cloning with token ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    result = sanitize_output(text)
    assert "ghp_" not in result
    assert "***" in result


def test_sanitize_github_oauth():
    text = "Auth: gho_abcdefghijklmnopqrstuvwxyz1234567890"
    result = sanitize_output(text)
    assert "gho_" not in result
    assert "***" in result


def test_sanitize_gitlab_pat():
    text = "Using token glpat-xxxxxxxxxxxxxxxxxxxx"
    result = sanitize_output(text)
    assert "glpat-" not in result
    assert "***" in result


def test_sanitize_bearer_token():
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def"
    result = sanitize_output(text)
    assert "eyJhbGci" not in result
    assert "***" in result


def test_sanitize_basic_auth_url():
    text = "remote: https://x-access-token:ghp_secret123@github.com/org/repo.git"
    result = sanitize_output(text)
    assert "ghp_secret123" not in result
    assert "***" in result


def test_sanitize_explicit_secrets():
    text = "Token is mysecretvalue123 in the output"
    result = sanitize_output(text, secrets=["mysecretvalue123"])
    assert "mysecretvalue123" not in result
    assert "***" in result


def test_sanitize_preserves_normal_text():
    text = "On branch main\nYour branch is up to date with 'origin/main'."
    result = sanitize_output(text)
    assert result == text


def test_sanitize_multiple_patterns():
    text = "clone https://oauth2:glpat-abc123@gitlab.com/org/repo\nUsing ghp_xyz789"
    result = sanitize_output(text)
    assert "glpat-abc123" not in result
    assert "ghp_xyz789" not in result
