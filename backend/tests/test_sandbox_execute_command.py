import pytest
from app.services.sandbox.config import SandboxConfig


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
