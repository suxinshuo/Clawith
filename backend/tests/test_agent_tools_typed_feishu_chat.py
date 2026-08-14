"""feishu_chat_search / feishu_chat_messages 的 typed 执行契约。"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import agent_tools
from app.services.agent_runtime.tool_execution import ToolExecutionOutcome

CHAT_TOOLS = frozenset({"feishu_chat_search", "feishu_chat_messages"})


def assert_outcome(value, status: str) -> ToolExecutionOutcome:
    assert isinstance(value, ToolExecutionOutcome), f"仍然走 legacy 字符串路径: {value!r}"
    assert value.status == status
    return value


async def execute(tool_name: str, arguments: dict):
    return await agent_tools.execute_builtin_tool_outcome(
        tool_name,
        arguments,
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )


def patch_credentials(app_id: str | None = "app-id", app_secret: str | None = "app-secret"):
    return patch(
        "app.services.agent_tools._get_feishu_credentials",
        new_callable=AsyncMock,
        return_value=(app_id, app_secret),
    )


def test_chat_tools_are_in_native_typed_workset() -> None:
    assert CHAT_TOOLS <= agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES


def test_chat_tools_are_scope_guarded() -> None:
    assert agent_tools._FEISHU_USER_FALLBACK_SCOPES["feishu_chat_search"] == ["im:chat:readonly"]
    assert agent_tools._FEISHU_USER_FALLBACK_SCOPES["feishu_chat_messages"] == ["im:message:readonly"]


@pytest.mark.asyncio
async def test_chat_search_requires_query() -> None:
    outcome = assert_outcome(await execute("feishu_chat_search", {}), "failed")
    assert outcome.error_code == "invalid_tool_arguments"


@pytest.mark.asyncio
async def test_chat_search_without_channel_is_failed() -> None:
    with patch_credentials(None, None):
        outcome = assert_outcome(
            await execute("feishu_chat_search", {"query": "roadmap"}),
            "failed",
        )
    assert outcome.error_code == "feishu_channel_not_configured"


@pytest.mark.asyncio
async def test_chat_search_returns_typed_success_with_chat_ids() -> None:
    response = {
        "code": 0,
        "data": {
            "items": [{"chat_id": "oc_1", "name": "Roadmap Sync", "user_count": 5}],
            "has_more": False,
        },
    }
    with (
        patch_credentials(),
        patch(
            "app.services.feishu_service.feishu_service.search_chats",
            new_callable=AsyncMock,
            return_value=response,
        ) as mock_search,
    ):
        outcome = assert_outcome(
            await execute("feishu_chat_search", {"query": "roadmap"}),
            "succeeded",
        )
    assert "oc_1" in (outcome.result_summary or "")
    mock_search.assert_awaited_once_with("app-id", "app-secret", "roadmap", page_size=20)


@pytest.mark.asyncio
async def test_chat_search_empty_result_is_succeeded_not_failed() -> None:
    with (
        patch_credentials(),
        patch(
            "app.services.feishu_service.feishu_service.search_chats",
            new_callable=AsyncMock,
            return_value={"code": 0, "data": {"items": []}},
        ),
    ):
        outcome = assert_outcome(
            await execute("feishu_chat_search", {"query": "nothing"}),
            "succeeded",
        )
    assert "nothing" in (outcome.result_summary or "")


@pytest.mark.asyncio
async def test_chat_search_null_data_is_succeeded_and_does_not_crash() -> None:
    with (
        patch_credentials(),
        patch(
            "app.services.feishu_service.feishu_service.search_chats",
            new_callable=AsyncMock,
            return_value={"code": 0, "data": None},
        ),
    ):
        assert_outcome(await execute("feishu_chat_search", {"query": "q"}), "succeeded")


@pytest.mark.asyncio
async def test_chat_search_business_rejection_is_failed() -> None:
    # 1254005 不是权限码，因此不应触发用户身份回退。
    with (
        patch_credentials(),
        patch(
            "app.services.feishu_service.feishu_service.search_chats",
            new_callable=AsyncMock,
            return_value={"code": 1254005, "msg": "invalid param"},
        ),
        patch(
            "app.services.agent_tools._get_agent_tenant_id",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_tenant,
    ):
        outcome = assert_outcome(
            await execute("feishu_chat_search", {"query": "roadmap"}),
            "failed",
        )
    assert outcome.error_code == "feishu_chat_search_rejected"
    mock_tenant.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_search_permission_rejection_enters_user_identity_fallback() -> None:
    """回归 Review 结论第 3 条：这两个 provider 方法不走 _parse_api_response，
    所以适配器必须自己登记权限拒绝，否则用户身份回退是死代码。"""
    with (
        patch_credentials(),
        patch(
            "app.services.feishu_service.feishu_service.search_chats",
            new_callable=AsyncMock,
            return_value={"code": 99991672, "msg": "Permission denied"},
        ),
        patch(
            "app.services.agent_tools._get_agent_tenant_id",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_tenant,
    ):
        outcome = assert_outcome(
            await execute("feishu_chat_search", {"query": "roadmap"}),
            "failed",
        )
    # tenant 解析不出来时回退流程原样返回被拒结论，但它必须被走到过。
    assert outcome.error_code == "feishu_chat_search_rejected"
    mock_tenant.assert_awaited()


@pytest.mark.asyncio
async def test_chat_search_network_exception_is_retryable_failure() -> None:
    with (
        patch_credentials(),
        patch(
            "app.services.feishu_service.feishu_service.search_chats",
            new_callable=AsyncMock,
            side_effect=httpx.ReadTimeout("chat search timed out"),
        ),
    ):
        outcome = assert_outcome(
            await execute("feishu_chat_search", {"query": "roadmap"}),
            "failed",
        )
    assert outcome.error_code == "feishu_chat_search_failed"
    assert outcome.retryable is True


@pytest.mark.asyncio
async def test_chat_messages_requires_chat_id() -> None:
    outcome = assert_outcome(await execute("feishu_chat_messages", {}), "failed")
    assert outcome.error_code == "invalid_tool_arguments"


@pytest.mark.asyncio
async def test_chat_messages_rejects_bad_start_time() -> None:
    outcome = assert_outcome(
        await execute("feishu_chat_messages", {"chat_id": "oc_1", "start_time": "昨天"}),
        "failed",
    )
    assert outcome.error_code == "invalid_tool_arguments"


@pytest.mark.asyncio
async def test_chat_messages_returns_typed_success() -> None:
    response = {
        "code": 0,
        "data": {
            "items": [
                {
                    "message_id": "om_1",
                    "sender": {"id": "ou_1"},
                    "body": {"content": '{"text":"hello"}'},
                    "msg_type": "text",
                    "create_time": "1700000000000",
                }
            ],
            "has_more": False,
        },
    }
    with (
        patch_credentials(),
        patch(
            "app.services.feishu_service.feishu_service.list_chat_messages",
            new_callable=AsyncMock,
            return_value=response,
        ) as mock_list,
    ):
        outcome = assert_outcome(
            await execute("feishu_chat_messages", {"chat_id": "oc_1"}),
            "succeeded",
        )
    summary = outcome.result_summary or ""
    assert "om_1" in summary and "hello" in summary
    mock_list.assert_awaited_once_with(
        "app-id",
        "app-secret",
        "oc_1",
        start_time=None,
        end_time=None,
        sort_type="ByCreateTimeDesc",
        page_size=50,
    )


@pytest.mark.asyncio
async def test_chat_messages_permission_rejection_enters_user_identity_fallback() -> None:
    with (
        patch_credentials(),
        patch(
            "app.services.feishu_service.feishu_service.list_chat_messages",
            new_callable=AsyncMock,
            return_value={"code": 99991672, "msg": "Permission denied"},
        ),
        patch(
            "app.services.agent_tools._get_agent_tenant_id",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_tenant,
    ):
        outcome = assert_outcome(
            await execute("feishu_chat_messages", {"chat_id": "oc_1"}),
            "failed",
        )
    assert outcome.error_code == "feishu_chat_messages_rejected"
    mock_tenant.assert_awaited()
