"""send_feishu_message 的 typed 执行契约（个人 + 群聊）。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import agent_tools
from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
from app.services.feishu_service import FeishuAPIError


def assert_outcome(value, status: str) -> ToolExecutionOutcome:
    assert isinstance(value, ToolExecutionOutcome), f"仍然走 legacy 字符串路径: {value!r}"
    assert value.status == status
    return value


async def execute(arguments: dict):
    return await agent_tools.execute_builtin_tool_outcome(
        "send_feishu_message",
        arguments,
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )


def patch_channel_config(config):
    """让 async_session() 里的第一次 ChannelConfig 查询返回给定配置。"""
    ctx = patch("app.services.agent_tools.async_session")
    mock_session_ctx = ctx.start()
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: config)
    mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
    mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
    return ctx


FEISHU_CONFIG = SimpleNamespace(app_id="app-id", app_secret="app-secret")


@pytest.fixture
def feishu_channel():
    ctx = patch_channel_config(FEISHU_CONFIG)
    yield
    ctx.stop()


@pytest.fixture
def no_feishu_channel():
    ctx = patch_channel_config(None)
    yield
    ctx.stop()


def test_send_feishu_message_is_in_native_typed_workset() -> None:
    assert "send_feishu_message" in agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES


def test_send_feishu_message_is_no_longer_hidden_from_the_model() -> None:
    assert "send_feishu_message" not in agent_tools._HIDDEN_FROM_LLM_TOOL_NAMES
    # query_roster 仍然必须隐藏，避免把这次解禁误当成「全部解禁」。
    assert "query_roster" in agent_tools._HIDDEN_FROM_LLM_TOOL_NAMES


@pytest.mark.asyncio
async def test_send_feishu_message_requires_message() -> None:
    outcome = assert_outcome(await execute({"target_member_id": "m1"}), "failed")
    assert outcome.error_code == "invalid_tool_arguments"


@pytest.mark.asyncio
async def test_send_feishu_message_rejects_no_target() -> None:
    outcome = assert_outcome(await execute({"message": "hi"}), "failed")
    assert outcome.error_code == "invalid_tool_arguments"


@pytest.mark.asyncio
async def test_send_feishu_message_rejects_both_target_kinds_at_once() -> None:
    outcome = assert_outcome(
        await execute({"target_member_id": "m1", "chat_id": "oc_1", "message": "hi"}),
        "failed",
    )
    assert outcome.error_code == "invalid_tool_arguments"


@pytest.mark.asyncio
async def test_send_feishu_message_still_rejects_legacy_name_and_user_id() -> None:
    outcome = assert_outcome(await execute({"user_id": "ou_1", "message": "hi"}), "failed")
    assert outcome.error_code == "invalid_tool_arguments"
    assert "query_directory" in (outcome.result_summary or "")


@pytest.mark.asyncio
async def test_individual_target_delegates_to_the_typed_channel_path() -> None:
    expected = ToolExecutionOutcome(
        status="succeeded", result_summary="Successfully sent message to 张三.", result_ref=None
    )
    with patch(
        "app.services.agent_tools._send_channel_message_outcome",
        new_callable=AsyncMock,
        return_value=expected,
    ) as mock_send:
        outcome = await execute({"target_member_id": "m1", "message": "hi"})
    assert outcome is expected
    assert mock_send.await_args.args[1] == {
        "target_member_id": "m1",
        "message": "hi",
        "channel": "feishu",
    }


@pytest.mark.asyncio
async def test_group_by_chat_id_succeeds(feishu_channel) -> None:
    with (
        patch(
            "app.services.feishu_service.feishu_service.send_message",
            new_callable=AsyncMock,
            return_value={"code": 0},
        ) as mock_send,
        patch(
            "app.services.agent_tools._save_feishu_card_to_history",
            new_callable=AsyncMock,
        ) as mock_history,
    ):
        outcome = assert_outcome(await execute({"chat_id": "oc_1", "message": "hi team"}), "succeeded")
    assert "oc_1" in (outcome.result_summary or "")
    assert mock_send.await_args.kwargs["receive_id"] == "oc_1"
    assert mock_send.await_args.kwargs["receive_id_type"] == "chat_id"
    # 群发也要落审计轨迹，与卡片工具一致。
    assert mock_history.await_args.kwargs["receive_id_type"] == "chat_id"


@pytest.mark.asyncio
async def test_group_by_chat_name_resolves_then_sends(feishu_channel) -> None:
    with (
        patch(
            "app.services.feishu_service.feishu_service.search_chats",
            new_callable=AsyncMock,
            return_value={"code": 0, "data": {"items": [{"chat_id": "oc_resolved"}]}},
        ),
        patch(
            "app.services.feishu_service.feishu_service.send_message",
            new_callable=AsyncMock,
            return_value={"code": 0},
        ) as mock_send,
        patch("app.services.agent_tools._save_feishu_card_to_history", new_callable=AsyncMock),
    ):
        outcome = assert_outcome(
            await execute({"chat_name": "Roadmap Sync", "message": "hi team"}),
            "succeeded",
        )
    assert "oc_resolved" in (outcome.result_summary or "")
    assert mock_send.await_args.kwargs["receive_id"] == "oc_resolved"


@pytest.mark.asyncio
async def test_group_not_found_is_failed(feishu_channel) -> None:
    with patch(
        "app.services.feishu_service.feishu_service.search_chats",
        new_callable=AsyncMock,
        return_value={"code": 0, "data": {"items": []}},
    ):
        outcome = assert_outcome(
            await execute({"chat_name": "Nonexistent Group", "message": "hi"}),
            "failed",
        )
    assert outcome.error_code == "feishu_chat_not_found"


@pytest.mark.asyncio
async def test_group_provider_rejection_is_failed_not_unknown(feishu_channel) -> None:
    """真实路径：send_message 在 code != 0 时抛 FeishuAPIError，不是返回信封。"""
    with patch(
        "app.services.feishu_service.feishu_service.send_message",
        new_callable=AsyncMock,
        side_effect=FeishuAPIError(stage="send_message", http_status=200, code=230002, msg="bot not in chat"),
    ):
        outcome = assert_outcome(await execute({"chat_id": "oc_bad", "message": "hi"}), "failed")
    assert outcome.error_code == "feishu_message_rejected"


@pytest.mark.asyncio
async def test_group_timeout_is_unknown(feishu_channel) -> None:
    with patch(
        "app.services.feishu_service.feishu_service.send_message",
        new_callable=AsyncMock,
        side_effect=httpx.ReadTimeout("send timed out"),
    ):
        outcome = assert_outcome(await execute({"chat_id": "oc_1", "message": "hi"}), "unknown")
    assert outcome.error_code == "feishu_message_outcome_unknown"


@pytest.mark.asyncio
async def test_group_history_failure_cannot_downgrade_a_confirmed_send(feishu_channel) -> None:
    with (
        patch(
            "app.services.feishu_service.feishu_service.send_message",
            new_callable=AsyncMock,
            return_value={"code": 0},
        ),
        patch(
            "app.services.agent_tools._save_feishu_card_to_history",
            new_callable=AsyncMock,
            side_effect=RuntimeError("history db down"),
        ),
    ):
        assert_outcome(await execute({"chat_id": "oc_1", "message": "hi"}), "succeeded")


@pytest.mark.asyncio
async def test_group_without_channel_is_failed(no_feishu_channel) -> None:
    outcome = assert_outcome(await execute({"chat_id": "oc_1", "message": "hi"}), "failed")
    assert outcome.error_code == "feishu_channel_not_configured"


@pytest.mark.asyncio
async def test_legacy_string_adapter_reuses_the_typed_implementation(feishu_channel) -> None:
    """遗留 execute_tool 路径必须与 typed 路径同源，不能有第二套校验。"""
    with (
        patch(
            "app.services.feishu_service.feishu_service.send_message",
            new_callable=AsyncMock,
            return_value={"code": 0},
        ),
        patch("app.services.agent_tools._save_feishu_card_to_history", new_callable=AsyncMock),
    ):
        text = await agent_tools._send_feishu_message(uuid.uuid4(), {"chat_id": "oc_1", "message": "hi"})
    assert text.startswith("✅")
    assert "oc_1" in text
