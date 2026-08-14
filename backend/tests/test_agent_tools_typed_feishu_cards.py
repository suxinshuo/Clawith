"""4 个 send_feishu_card_* 工具的 typed 执行契约。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import agent_tools
from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
from app.services.feishu_service import FeishuAPIError

CARD_TOOLS = frozenset(
    {
        "send_feishu_card_kv",
        "send_feishu_card_actions",
        "send_feishu_card_table",
        "send_feishu_card_approval",
    }
)

VALID_ARGS = {
    "send_feishu_card_kv": {"fields": [{"key": "Status", "value": "On track"}]},
    "send_feishu_card_actions": {
        "body": "Approve deployment?",
        "actions": [{"label": "Approve", "action_id": "approve"}],
    },
    "send_feishu_card_table": {"columns": ["Name", "Score"], "rows": [["Alice", "9"]]},
    "send_feishu_card_approval": {
        "title": "Expense approval",
        "summary_text": "$120 team lunch",
        "approval_id": "appr-1",
    },
}


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


def patch_target(monkeypatch, *, err=None, id_type="chat_id", receive_id="oc_1"):
    config = SimpleNamespace(app_id="app-id", app_secret="app-secret")

    async def fake_resolve(agent_id, args, session_id):
        del agent_id, args, session_id
        if err:
            return (None, None, None, None, err)
        return (config, receive_id, id_type, None, None)

    monkeypatch.setattr(agent_tools, "_resolve_feishu_card_target", fake_resolve)


def patch_history(monkeypatch):
    async def noop(*args, **kwargs):
        del args, kwargs

    monkeypatch.setattr(agent_tools, "_save_feishu_card_to_history", noop)


def test_card_tools_are_in_native_typed_workset() -> None:
    assert CARD_TOOLS <= agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES


@pytest.mark.parametrize("tool_name", sorted(CARD_TOOLS))
@pytest.mark.asyncio
async def test_card_send_succeeds_via_cardkit(monkeypatch, tool_name) -> None:
    patch_target(monkeypatch)
    patch_history(monkeypatch)
    with patch(
        "app.services.feishu_service.feishu_service.send_card_with_fallback",
        new_callable=AsyncMock,
        return_value={"code": 0, "msg": "ok", "card_id": "card-1"},
    ):
        outcome = assert_outcome(await execute(tool_name, VALID_ARGS[tool_name]), "succeeded")
    summary = outcome.result_summary or ""
    assert "卡片" in summary and "oc_1" in summary


@pytest.mark.parametrize("tool_name", sorted(CARD_TOOLS))
@pytest.mark.asyncio
async def test_card_send_succeeds_via_markdown_fallback(monkeypatch, tool_name) -> None:
    patch_target(monkeypatch)
    patch_history(monkeypatch)
    with patch(
        "app.services.feishu_service.feishu_service.send_card_with_fallback",
        new_callable=AsyncMock,
        return_value={"code": 0, "msg": "ok"},  # 没有 card_id 即走了降级
    ):
        outcome = assert_outcome(await execute(tool_name, VALID_ARGS[tool_name]), "succeeded")
    assert "降级 markdown" in (outcome.result_summary or "")


@pytest.mark.parametrize("tool_name", sorted(CARD_TOOLS))
@pytest.mark.asyncio
async def test_card_provider_rejection_is_failed_not_unknown(monkeypatch, tool_name) -> None:
    """回归 Review 结论第 2 条：降级路径会抛 FeishuAPIError（如机器人不在该群），
    必须判为 failed；判成 unknown 会让整个 run 挂在人工确认上。"""
    patch_target(monkeypatch)
    with patch(
        "app.services.feishu_service.feishu_service.send_card_with_fallback",
        new_callable=AsyncMock,
        side_effect=FeishuAPIError(stage="send_card_md_fallback", http_status=200, code=230002, msg="bot not in chat"),
    ):
        outcome = assert_outcome(await execute(tool_name, VALID_ARGS[tool_name]), "failed")
    assert outcome.error_code == "feishu_card_rejected"


@pytest.mark.parametrize("tool_name", sorted(CARD_TOOLS))
@pytest.mark.asyncio
async def test_card_timeout_is_unknown(monkeypatch, tool_name) -> None:
    patch_target(monkeypatch)
    with patch(
        "app.services.feishu_service.feishu_service.send_card_with_fallback",
        new_callable=AsyncMock,
        side_effect=httpx.ReadTimeout("card send timed out"),
    ):
        outcome = assert_outcome(await execute(tool_name, VALID_ARGS[tool_name]), "unknown")
    assert outcome.error_code == "feishu_card_outcome_unknown"


@pytest.mark.parametrize("tool_name", sorted(CARD_TOOLS))
@pytest.mark.asyncio
async def test_card_target_resolution_failure_is_failed(monkeypatch, tool_name) -> None:
    patch_target(monkeypatch, err="❌ 当前 Agent 没有配置飞书通道")
    outcome = assert_outcome(await execute(tool_name, VALID_ARGS[tool_name]), "failed")
    assert outcome.error_code == "feishu_card_target_unresolved"
    # result_summary is a plain execution fact, not display text — only the
    # legacy string adapter's rendered text should carry the "❌ " glyph.
    # Regression guard against a doubled "❌ ❌ ..." prefix.
    summary = outcome.result_summary or ""
    assert not summary.startswith("❌"), f"result_summary should not carry a display glyph: {summary!r}"
    assert summary == "当前 Agent 没有配置飞书通道"


@pytest.mark.parametrize("tool_name", sorted(CARD_TOOLS))
@pytest.mark.asyncio
async def test_card_history_failure_cannot_downgrade_a_confirmed_send(monkeypatch, tool_name) -> None:
    patch_target(monkeypatch)

    async def boom(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("history db down")

    monkeypatch.setattr(agent_tools, "_save_feishu_card_to_history", boom)
    with patch(
        "app.services.feishu_service.feishu_service.send_card_with_fallback",
        new_callable=AsyncMock,
        return_value={"code": 0, "msg": "ok", "card_id": "card-1"},
    ):
        assert_outcome(await execute(tool_name, VALID_ARGS[tool_name]), "succeeded")


@pytest.mark.parametrize(
    "tool_name,bad_args",
    [
        ("send_feishu_card_kv", {"fields": []}),
        ("send_feishu_card_actions", {"actions": [{"label": "A", "action_id": "a"}]}),
        ("send_feishu_card_actions", {"body": "b", "actions": []}),
        ("send_feishu_card_actions", {"body": "b", "actions": [{"label": "A"}]}),
        (
            "send_feishu_card_actions",
            {"body": "b", "actions": [{"label": str(i), "action_id": str(i)} for i in range(5)]},
        ),
        ("send_feishu_card_table", {"rows": [["a"]]}),
        ("send_feishu_card_table", {"columns": ["a"], "rows": "not-a-list"}),
        ("send_feishu_card_approval", {"title": "t", "summary_text": "s"}),
    ],
)
@pytest.mark.asyncio
async def test_card_invalid_arguments_are_typed_failures(tool_name, bad_args) -> None:
    outcome = assert_outcome(await execute(tool_name, bad_args), "failed")
    assert outcome.error_code == "invalid_tool_arguments"


@pytest.mark.parametrize("tool_name", sorted(CARD_TOOLS))
@pytest.mark.asyncio
async def test_legacy_string_wrappers_reuse_the_typed_implementation(monkeypatch, tool_name) -> None:
    """字符串版必须是薄适配层，不允许存在第二套参数校验。"""
    patch_target(monkeypatch)
    patch_history(monkeypatch)
    wrapper = getattr(agent_tools, f"_{tool_name}")
    with patch(
        "app.services.feishu_service.feishu_service.send_card_with_fallback",
        new_callable=AsyncMock,
        return_value={"code": 0, "msg": "ok", "card_id": "card-1"},
    ):
        text = await wrapper(uuid.uuid4(), VALID_ARGS[tool_name], "")
    assert text.startswith("✅")
