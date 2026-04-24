"""Tests for DockerBackend volume mount logic."""

from unittest.mock import patch, MagicMock

import pytest

from app.services.sandbox.local.docker_backend import DockerBackend
from app.services.sandbox.config import SandboxConfig


@pytest.fixture
def backend():
    config = SandboxConfig(type="docker")
    return DockerBackend(config)


class TestResolveHostPath:
    """Tests for _resolve_host_path translation logic."""

    def test_no_host_dir_returns_work_dir_unchanged(self, backend):
        """When AGENT_DATA_HOST_DIR is empty, return work_dir as-is."""
        with patch("app.services.sandbox.local.docker_backend.get_settings") as mock:
            settings = MagicMock()
            settings.AGENT_DATA_HOST_DIR = ""
            settings.AGENT_DATA_DIR = "/data/agents"
            mock.return_value = settings

            result = backend._resolve_host_path("/data/agents/abc-123")
            assert result == "/data/agents/abc-123"

    def test_host_dir_replaces_prefix(self, backend):
        """When AGENT_DATA_HOST_DIR is set, replace the AGENT_DATA_DIR prefix."""
        with patch("app.services.sandbox.local.docker_backend.get_settings") as mock:
            settings = MagicMock()
            settings.AGENT_DATA_HOST_DIR = "/home/user/Clawith/backend/agent_data"
            settings.AGENT_DATA_DIR = "/data/agents"
            mock.return_value = settings

            result = backend._resolve_host_path("/data/agents/abc-123")
            assert result == "/home/user/Clawith/backend/agent_data/abc-123"

    def test_non_matching_prefix_returns_unchanged(self, backend):
        """When work_dir doesn't start with AGENT_DATA_DIR, return as-is."""
        with patch("app.services.sandbox.local.docker_backend.get_settings") as mock:
            settings = MagicMock()
            settings.AGENT_DATA_HOST_DIR = "/home/user/agent_data"
            settings.AGENT_DATA_DIR = "/data/agents"
            mock.return_value = settings

            result = backend._resolve_host_path("/some/other/path")
            assert result == "/some/other/path"

    def test_similar_prefix_not_falsely_matched(self, backend):
        """Paths like /data/agents_backup should NOT be matched."""
        with patch("app.services.sandbox.local.docker_backend.get_settings") as mock:
            settings = MagicMock()
            settings.AGENT_DATA_HOST_DIR = "/host/agents"
            settings.AGENT_DATA_DIR = "/data/agents"
            mock.return_value = settings

            result = backend._resolve_host_path("/data/agents_backup/file")
            assert result == "/data/agents_backup/file"
