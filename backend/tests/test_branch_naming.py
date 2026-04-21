import pytest
from app.services.branch_naming import generate_agent_branch, slugify_agent_name, is_protected_branch


def test_slugify_simple_name():
    assert slugify_agent_name("Backend Dev") == "backend-dev"


def test_slugify_chinese_name():
    assert slugify_agent_name("后端开发Agent") == "agent"


def test_slugify_special_chars():
    assert slugify_agent_name("my_agent (v2)") == "my-agent-v2"


def test_slugify_preserves_hyphens():
    assert slugify_agent_name("frontend-dev") == "frontend-dev"


def test_slugify_truncates_long_name():
    name = "a" * 100
    result = slugify_agent_name(name)
    assert len(result) <= 40


def test_generate_agent_branch_basic():
    branch = generate_agent_branch("Backend Dev", "task-123")
    assert branch == "agent/backend-dev/task-123"


def test_generate_agent_branch_with_session_id():
    branch = generate_agent_branch("Frontend Dev", "abc123def456")
    assert branch == "agent/frontend-dev/abc123def456"


def test_generate_agent_branch_sanitizes_context():
    branch = generate_agent_branch("Test Agent", "feat/login page")
    assert branch == "agent/test-agent/feat-login-page"


def test_is_protected_branch():
    assert is_protected_branch("main") is True
    assert is_protected_branch("master") is True
    assert is_protected_branch("develop") is True
    assert is_protected_branch("release/v1.0") is True
    assert is_protected_branch("agent/backend-dev/task-123") is False
    assert is_protected_branch("feature/my-branch") is False
