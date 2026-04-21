import pytest
from app.templates.coordinator_agent import (
    render_coordinator_prompt,
    COORDINATOR_TOOLS_WHITELIST,
)


def test_render_coordinator_prompt_basic():
    prompt = render_coordinator_prompt(
        project_name="E-Commerce App",
        repo_url="https://github.com/org/ecommerce.git",
        sub_agents=["frontend-dev", "backend-dev", "test-agent"],
    )
    assert "E-Commerce App" in prompt
    assert "github.com/org/ecommerce" in prompt
    assert "frontend-dev" in prompt
    assert "backend-dev" in prompt
    assert "test-agent" in prompt
    assert "send_message_to_agent" in prompt


def test_render_coordinator_prompt_with_workflow():
    prompt = render_coordinator_prompt(
        project_name="API Service",
        repo_url="https://github.com/org/api.git",
        sub_agents=["backend-dev"],
        workflow_description="Implement features, run tests, create PR",
    )
    assert "API Service" in prompt
    assert "Implement features" in prompt


def test_coordinator_tools_whitelist():
    assert "send_message_to_agent" in COORDINATOR_TOOLS_WHITELIST
    assert "create_task" in COORDINATOR_TOOLS_WHITELIST
    assert "git_create_pr" in COORDINATOR_TOOLS_WHITELIST
    assert "execute_command" not in COORDINATOR_TOOLS_WHITELIST


def test_render_coordinator_prompt_has_delegation_pattern():
    prompt = render_coordinator_prompt(
        project_name="Test",
        repo_url="https://github.com/org/test.git",
        sub_agents=["dev-agent"],
    )
    assert "task_delegate" in prompt or "delegate" in prompt.lower()
    assert "agent/" in prompt
