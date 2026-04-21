import pytest
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.base import BaseSandboxBackend, ExecutionResult, SandboxCapabilities


def test_sandbox_config_default_idle_timeout():
    config = SandboxConfig()
    assert config.idle_timeout == 1800
    assert config.dev_image == "clawith-dev:base"


def test_sandbox_config_from_dict_with_dev_fields():
    config = SandboxConfig.from_dict({
        "sandbox_type": "docker",
        "idle_timeout": "3600",
        "dev_image": "clawith-dev:node",
    })
    assert config.idle_timeout == 3600
    assert config.dev_image == "clawith-dev:node"


def test_sandbox_config_from_dict_defaults():
    config = SandboxConfig.from_dict({})
    assert config.idle_timeout == 1800
    assert config.dev_image == "clawith-dev:base"


# ── Tests for BaseSandboxBackend.execute_command() delegation ──

class StubBackend(BaseSandboxBackend):
    """Concrete stub for testing BaseSandboxBackend default methods."""

    name = "stub"

    def __init__(self):
        self.last_call = None

    async def execute(self, code, language, timeout=30, work_dir=None, **kwargs):
        self.last_call = {"code": code, "language": language, "timeout": timeout, "work_dir": work_dir}
        return ExecutionResult(success=True, stdout="ok", stderr="", exit_code=0, duration_ms=1)

    async def health_check(self):
        return True

    def get_capabilities(self):
        return SandboxCapabilities(
            supported_languages=["bash"], max_timeout=300,
            max_memory_mb=512, network_available=False, filesystem_available=True,
        )


@pytest.mark.asyncio
async def test_execute_command_delegates_to_execute():
    backend = StubBackend()
    result = await backend.execute_command("ls -la", cwd="/tmp", timeout=60)
    assert backend.last_call["code"] == "ls -la"
    assert backend.last_call["language"] == "bash"
    assert backend.last_call["timeout"] == 60
    assert backend.last_call["work_dir"] == "/tmp"
    assert result.success is True
    assert result.stdout == "ok"
