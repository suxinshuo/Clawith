"""Gate A（模型可见）与 Gate B（typed 执行事实）必须成对开启。

只开 Gate A 会让 Runtime 把字符串结果判成 unknown 并挂起 run 等人确认
（tool_step_service.py:1455-1522），比工具不可见更糟；只开 Gate B 则工具
永远到不了模型手里，表现为「web 页面能看到、Agent 说没有这个工具」。
"""

from __future__ import annotations

import uuid

import pytest

from app.services import agent_tools
from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS

RESTORED_FEISHU_TOOLS = frozenset(
    {
        "feishu_chat_search",
        "feishu_chat_messages",
        "send_feishu_message",
        "send_feishu_card_kv",
        "send_feishu_card_actions",
        "send_feishu_card_table",
        "send_feishu_card_approval",
    }
)

# 唯一允许「UI 可见但 Runtime 不可见」的飞书工具：typed 适配器
# _feishu_approval_create_outcome 已实现，但刻意等它的确认门落地后再接线，
# 依据见 tests/test_agent_tools_typed_feishu_remaining.py:210-220。
DEFERRED_FEISHU_TOOLS = frozenset({"feishu_approval_create"})


def test_restored_feishu_tools_are_visible_to_durable_runtime() -> None:
    assert RESTORED_FEISHU_TOOLS <= agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES


def test_every_feishu_builtin_is_typed_or_explicitly_deferred() -> None:
    feishu = {
        str(definition["name"]) for definition in BUILTIN_TOOL_DEFINITIONS if definition.get("category") == "feishu"
    }
    assert feishu - agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES == DEFERRED_FEISHU_TOOLS


def test_every_feishu_builtin_only_needs_a_configured_channel() -> None:
    """Gate C：category == feishu 一律得到 feishu_channel 就绪度，
    不能有工具悄悄落进 configured_credentials 分支被静默丢弃。"""
    from app.services.builtin_tool_definitions import builtin_readiness

    for definition in BUILTIN_TOOL_DEFINITIONS:
        if definition.get("category") == "feishu":
            assert builtin_readiness(str(definition["name"])) == "feishu_channel"


@pytest.mark.parametrize("tool_name", sorted(RESTORED_FEISHU_TOOLS))
@pytest.mark.asyncio
async def test_restored_tool_reports_a_typed_fact_without_touching_a_provider(tool_name) -> None:
    """空参数必须换来 typed 的非法参数结论，而不是 legacy 字符串。

    这同时证明 Gate B 分支真的可达：如果分支缺失，调用会落到
    execute_tool() 并返回字符串，isinstance 断言立刻失败。
    """
    outcome = await agent_tools.execute_builtin_tool_outcome(
        tool_name,
        {},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )
    assert isinstance(outcome, ToolExecutionOutcome), f"{tool_name} 仍然走 legacy 字符串路径"
    assert outcome.status == "failed"
    assert outcome.error_code == "invalid_tool_arguments"


_ALL_TYPED_FEISHU_TOOLS = sorted(
    {str(definition["name"]) for definition in BUILTIN_TOOL_DEFINITIONS if definition.get("category") == "feishu"}
    & agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES
)


@pytest.mark.parametrize("tool_name", _ALL_TYPED_FEISHU_TOOLS)
@pytest.mark.asyncio
async def test_every_typed_feishu_tool_reports_a_typed_fact_without_touching_a_provider(tool_name) -> None:
    """通用护栏：任何在 Gate A 开放的飞书工具都必须配一个 Gate B 分支，不只是
    最初恢复的那 7 个。

    如果这个测试集只锁死那 7 个工具名，未来新增的飞书工具可以只开 Gate A（加进
    RUNTIME_TYPED_APPLICATION_TOOL_NAMES）却不接 Gate B（typed 结论分支），
    这套测试仍然全绿——恰好是整个计划想防止的坏状态。

    这里只断言 isinstance（已确认对当前全部 28 个飞书 Gate-A 工具在空参数下
    安全：无网络/DB 访问、不会挂起）；具体的 status == "failed" /
    error_code == "invalid_tool_arguments" 断言留在上面那个硬编码 7 个工具的
    测试里，因为那是这 7 个工具校验行为的具体细节，不该推广到全集。
    """
    outcome = await agent_tools.execute_builtin_tool_outcome(
        tool_name,
        {},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )
    assert isinstance(outcome, ToolExecutionOutcome), f"{tool_name} 仍然走 legacy 字符串路径"


def test_send_feishu_message_is_not_hidden_while_query_roster_still_is() -> None:
    assert "send_feishu_message" not in agent_tools._HIDDEN_FROM_LLM_TOOL_NAMES
    assert "query_roster" in agent_tools._HIDDEN_FROM_LLM_TOOL_NAMES
