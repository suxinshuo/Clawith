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


# ── Tests for SubprocessBackend.execute_command() ──

from app.services.sandbox.local.subprocess_backend import SubprocessBackend
import tempfile
import os


@pytest.mark.asyncio
async def test_subprocess_execute_command_echo():
    config = SandboxConfig(type="subprocess")
    backend = SubprocessBackend(config)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = await backend.execute_command("echo hello", cwd=tmpdir, timeout=10)
        assert result.exit_code == 0
        assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_subprocess_execute_command_cwd():
    config = SandboxConfig(type="subprocess")
    backend = SubprocessBackend(config)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = await backend.execute_command("pwd", cwd=tmpdir, timeout=10)
        assert result.exit_code == 0
        assert tmpdir in result.stdout


@pytest.mark.asyncio
async def test_subprocess_execute_command_timeout():
    config = SandboxConfig(type="subprocess")
    backend = SubprocessBackend(config)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = await backend.execute_command("sleep 30", cwd=tmpdir, timeout=1)
        assert result.exit_code == 124
        assert result.error and "timed out" in result.error.lower()


@pytest.mark.asyncio
async def test_subprocess_execute_command_blocks_dangerous():
    config = SandboxConfig(type="subprocess")
    backend = SubprocessBackend(config)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = await backend.execute_command("rm -rf /", cwd=tmpdir, timeout=10)
        assert result.exit_code == 1
        assert result.error
