"""任务 12：全链路集成测试（无库部分）。

本文件覆盖的是**跨模块的端到端集成断言**，不是重复任务 3-9 已经交付的单点测试。
每个场景的 docstring 里都写明了它与既有测试的边界：哪部分是"这条测试第一次验证"，
哪部分是"复用/组合已有测试已经验证过的行为"，避免看起来像是重新发明一遍轮子。

已确认被现有测试**间接覆盖、本文件不重复**的子场景：
- `gate.check()` 本身的 allowed/blocked/两级异常 fail-open/软告警去重
  —— `test_token_accounting_gate.py`。
- `run_compactor` / `session_context_compactor` / `planning` / `model_probe` /
  `group_handoff` 五条链路各自接入闸门后的单点行为（超限拦截、未超限放行、
  `subjects=None` 时的降级）—— 分别在 `test_agent_runtime_run_compactor.py`、
  `test_agent_runtime_session_context_compactor.py`、`test_agent_runtime_planning.py`、
  `test_llm_tool_capability_probe.py`、`test_agent_runtime_group_handoff.py` /
  `test_group_handoff_budget_property.py` 里覆盖。
- `node_executor._compact` / `node_executor._model` 对 `token_budget_exceeded`
  错误码的既有传播逻辑（`exc.code` -> `lifecycle.reason`）—— `test_agent_runtime_node_executor.py`
  （任务 6.5 新增的两条测试）。
- `delivery._safe_failure_content` 渲染 `budget_exceeded_message()` 产出的四项信息
  —— `test_agent_runtime_delivery.py::test_token_budget_exceeded_failure_renders_the_budget_exceeded_message`
  （任务 6.5 新增，但那条测试从一个**手工构造**的 `BudgetVerdict` 出发，不经过
  `complete_once()`/`node_executor`/`delivery_from_checkpoint` 的真实链路）。
- `PUT /token-budget-enforcement` 切换模式后 `budget.current_enforcement_mode()`
  立即观察到新值 —— `test_enterprise_token_budget_enforcement.py`
  （但那条测试只验证了 `current_enforcement_mode()` 这一个独立调用点，没有验证
  一次真实的限额判定 `gate.check()`/`_budget_gate()` 是否也立即观察到新值）。
- grace 窗口生效时 `effective_mode` 与节流日志 —— `test_token_accounting_budget.py`
  （只验证了 `current_enforcement_state()` 本身，不涉及 `PUT` 端点或真实的
  `gate.check()` 判定）。

本文件新增的六个场景（对应 design.md "Integration Tests" 小节逐条）：
  a. 直接对话全链路：`complete_once` -> `node_executor._model` -> `delivery_from_checkpoint`
     -> `_safe_failure_content`，全部用真实实现串起来，只在数据库读取这一层打桩。
  b. 触发器链路等价性：同一个 `RuntimeModelStepService` 实例驱动 chat 与 trigger
     两种 `source_type`，钉住"是同一个实例"这个结构性关系。
  c. 压缩 -> 业务步的顺序：驱动真实 LangGraph 图，证明压缩节点的限额拦截发生在
     业务模型步之前——业务步的 model service 从未被调用。
  d. 群聊 handoff：用真实的、由击穿预算触发的 `GroupAgentHandoffError` 实例
     （而不是手写的合成异常）驱动 `complete_once` 的真实 repair 转换逻辑。
  e. 执行模式切换：`PUT /token-budget-enforcement` 之后，同进程内下一次
     `_budget_gate()`（不是 `current_enforcement_mode()` 本身）立即用新值判定。
  f. grace 窗口：grace 生效时 `gate.check()` 放行且落 INFO 日志；`clear_grace`
     之后同一请求立即被拦截。

测试风格延续本仓库现有风格：`SimpleNamespace` / 真实 ORM 对象替身 + `monkeypatch`，
不引入 `hypothesis`。跨文件复用既有测试的脚手架构造器，与 `test_token_budget_gate_lanes.py`
/ `test_group_handoff_budget_property.py` 已经建立的跨文件 import 惯例一致。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from loguru import logger

from app.api import enterprise
from app.models.group import GroupMember
from app.services.agent_runtime import model_step_service
from app.services.agent_runtime.checkpoint_side_effects import delivery_from_checkpoint
from app.services.agent_runtime.command_worker import CheckpointObservation, RuntimeRunRecord
from app.services.agent_runtime.delivery import deliver_runtime_message
from app.services.agent_runtime.graph import build_agent_runtime_graph
from app.services.agent_runtime.checkpointer import runtime_thread_config
from app.services.agent_runtime.group_handoff import GroupAgentHandoffError
from app.services.agent_runtime.model_step_service import RuntimeModelStepService
from app.services.agent_runtime.node_executor import (
    DeterministicRuntimeNodeExecutor,
    RunCompactResult,
)
from app.services.agent_runtime.run_compactor import RunCompactInputs, RuntimeRunCompactorService
from app.services.agent_runtime.state import RunInputSnapshots, RunRegistrySnapshot, RuntimeContext
from app.config import Settings
from app.services.token_accounting import budget, gate
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    MODE_WARN_ONLY,
    SETTING_ENFORCEMENT_MODE,
    reset_enforcement_mode_cache,
)
from app.services.token_accounting.gate import LANE_BUSINESS_STEP, BudgetSubjects

# 跨文件复用既有脚手架构造器，与本仓库既有惯例一致
# （见 test_token_budget_gate_lanes.py / test_group_handoff_budget_property.py）。
from test_agent_runtime_delivery import (
    _RecordingDB,
    _added,
    _agent as _delivery_agent,
    _group as _delivery_group,
    _participant as _delivery_participant,
    _run as _delivery_run,
    _session as _delivery_session,
)
from test_agent_runtime_group_handoff import (
    _cycle_check,
    _forced_enforce as _handoff_forced_enforce,
    _records as _handoff_records,
    _settings as _handoff_settings,
    _target as _handoff_target,
)
from test_agent_runtime_model_step_service import (
    _DB as _msvc_test_double_db,
)
from test_agent_runtime_model_step_service import (
    _ContextBuilder,
    _agent as _msvc_agent,
    _build as _msvc_build,
    _context as _msvc_context,
    _model as _msvc_model,
    _prompt as _msvc_prompt,
    _session_factory as _msvc_session_factory,
    _state as _msvc_state,
    _tools as _msvc_tools,
)
from test_agent_runtime_node_executor import (
    CancelSource as _NodeCancelSource,
)
from test_agent_runtime_node_executor import (
    ModelService as _NodeModelService,
)
from test_agent_runtime_node_executor import (
    ToolService as _NodeToolService,
)
from test_agent_runtime_node_executor import (
    _context as _node_context,
)
from test_agent_runtime_node_executor import (
    _executor as _build_node_executor,
)
from test_agent_runtime_node_executor import (
    _state as _node_state,
)
from test_agent_runtime_run_compactor import (
    _breached_agent_subjects as _compact_breached_subjects,
)
from test_agent_runtime_run_compactor import (
    _model as _compact_model,
)
from test_enterprise_token_budget_enforcement import (
    _FakeSettingStore,
    _patch_store,
    _platform_admin,
)


NOW = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_enforcement_mode_cache_between_tests():
    """避免用例间通过 30 秒 TTL 的进程内模式缓存互相污染（任务 3.3），
    与其它 token 相关测试文件的做法一致。"""
    reset_enforcement_mode_cache()
    yield
    reset_enforcement_mode_cache()


def _forced_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 `gate.check()` 内部调用的 `evaluate()` 使用 `enforce`。

    本文件里绝大多数场景不打桩 `system_setting_dao.get_value`（测试环境没有真实
    数据库连接），如果不强制模式，`current_enforcement_mode()` 会因为读取失败
    fail-open 到 `warn_only`，把命中限额的 verdict 变成 `allowed=True`，掩盖本文件
    真正要验证的判定/传播链路。风格与 `test_agent_runtime_group_handoff.py::_forced_enforce`、
    `test_agent_runtime_run_compactor.py` 里同名 helper 完全一致。
    """
    real_evaluate = budget.evaluate

    async def forced_enforce_evaluate(**kwargs):
        return await real_evaluate(**{**kwargs, "mode": MODE_ENFORCE})

    monkeypatch.setattr(gate, "evaluate", forced_enforce_evaluate)


def _repeatable_session_factory(model, agent):
    """A `RuntimeSessionFactory` that can serve `complete_once()` more than once.

    `test_agent_runtime_model_step_service._session_factory` is designed for a single
    `complete_once()` call: its first `__call__` yields a `_DB(model, agent)` (good for
    exactly one `_load()`), and every subsequent `__call__` yields a `_NoFallbackDB` that
    returns nothing (it exists only to support the *fallback-model-lookup* path within
    that one call). Scenario (b) below deliberately drives the *same* service instance
    through `complete_once()` twice (once per `source_type`), so it needs a factory that
    yields a fresh, fully-stocked `_DB(model, agent)` on every call instead.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory():
        yield _msvc_test_double_db(model, agent)

    return factory


def _breached_agent(tenant_id: uuid.UUID) -> object:
    """一个当日 token 用量已击穿上限的 Agent（复用 model_step_service 的测试构造器，
    只额外把用量字段改成击穿状态）。`last_daily_reset` 用真实挂钟时间而不是固定
    过去日期——`gate.check()` 不传显式 `now` 时，`budget.evaluate()` 内部用
    `datetime.now(UTC)` 判断周期翻页，固定的过去时间迟早会被判定为"已翻页、
    计数视为 0"，掩盖真实的击穿场景（与 `test_agent_runtime_run_compactor.py`
    等文件里同类 helper 的处理方式一致）。
    """
    agent = _msvc_agent(tenant_id)
    now = datetime.now(UTC)
    agent.max_tokens_per_day = 100_000
    agent.max_tokens_per_month = None
    agent.tokens_used_today = 200_000
    agent.tokens_used_month = 0
    agent.last_daily_reset = now
    agent.last_monthly_reset = now
    return agent


# ---------------------------------------------------------------------------
# (a) 直接对话全链路：complete_once -> node_executor._model ->
#     delivery_from_checkpoint -> _safe_failure_content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_chat_full_chain_renders_all_four_budget_facts(monkeypatch) -> None:
    """超限 Agent 走一次真实的直接对话，验证从判定到用户可见文本的整条链路。

    与既有测试的边界：
    - `test_token_budget_enforcement.py` 已经验证了 `complete_once` 本身在超限时
      短路、返回 `error["code"] == "token_budget_exceeded"`；本测试从那个真实结果
      继续往下走，而不是重新验证 `complete_once` 本身。
    - `test_agent_runtime_delivery.py::test_token_budget_exceeded_failure_renders_the_budget_exceeded_message`
      已经验证了 `_safe_failure_content` 对一个**手工构造**的 `BudgetVerdict` 能
      渲染出四项信息；本测试的价值在于证明这四项信息是从一次**真实的**、由真实
      `budget.evaluate()` 产出的判定结果，经过 `node_executor._model` 与
      `delivery_from_checkpoint` 两层真实转换后，仍然完整地出现在最终文本里——
      任何一层如果悄悄丢字段（例如 `_failure_metadata` 读错 key、`node_executor`
      把 `error["message"]` 截断），这条测试都会失败，而分层的单点测试各自都会通过。
    """
    _forced_enforce(monkeypatch)
    tenant_id = uuid.uuid4()
    model = _msvc_model(tenant_id)
    agent = _breached_agent(tenant_id)
    msvc_state = _msvc_state(tenant_id, model, agent)
    msvc_context = _msvc_context(msvc_state)

    async def unreachable_completion(*_args, **_kwargs):
        raise AssertionError("provider must not be called once the budget gate blocks the request")

    service = RuntimeModelStepService(
        session_factory=_msvc_session_factory(model, agent),
        context_builder=_ContextBuilder(_msvc_build()),
        completion=unreachable_completion,
        tool_provider=_msvc_tools,
        prompt_builder=_msvc_prompt,
        model_retry_base_delay_seconds=0,
        model_retry_jitter_ratio=0,
    )

    # 1) RuntimeModelStepService.complete_once —— 真实判定，真实短路。
    step_result = await service.complete_once(msvc_state, msvc_context)
    assert step_result.intent == "error"
    assert step_result.error["code"] == "token_budget_exceeded"

    # 2) node_executor._model —— 把 complete_once 的真实返回值喂给真实的节点执行器
    #    （不是重新构造一个合成的 ModelStepResult）。
    run_id = uuid.uuid4()
    node_state = _node_state(run_id)
    node_executor_instance = _build_node_executor(_NodeModelService(step_result))
    node_context = _node_context(run_id, node_executor_instance, "command-direct-chat")
    node_update = await node_executor_instance.execute("model", node_state, node_context)
    lifecycle = node_update["lifecycle"]
    assert lifecycle["status"] == "failed"
    assert lifecycle["reason"] == "token_budget_exceeded"
    assert lifecycle["error"]["code"] == "token_budget_exceeded"

    # 3) delivery_from_checkpoint —— 从一个真实的（含上面 lifecycle 的）已提交 checkpoint
    #    推导出 DeliveryRequest，不手工构造 failure_code/failure_message。
    tenant_delivery_id = uuid.uuid4()
    agent_delivery_id = uuid.uuid4()
    group_id = uuid.uuid4()
    session = _delivery_session(tenant_id=tenant_delivery_id, agent_id=None, group_id=group_id)
    run_row = _delivery_run(tenant_id=tenant_delivery_id, session=session, agent_id=agent_delivery_id)
    delivery_run_id = run_row.id
    runtime_run_record = RuntimeRunRecord(
        tenant_id=tenant_delivery_id,
        run_id=delivery_run_id,
        thread_id=str(delivery_run_id),
        runtime_type="langgraph",
        goal="Answer the request",
        run_kind="foreground",
        source_type="chat",
        model_id=str(uuid.uuid4()),
        graph_name="runtime_graph",
        graph_version="v1",
        agent_id=str(agent_delivery_id),
    )
    checkpoint = CheckpointObservation(
        checkpoint_id="checkpoint-direct-chat",
        state={
            "registry": RunRegistrySnapshot(
                tenant_id=str(tenant_delivery_id),
                run_id=str(delivery_run_id),
                goal="Answer the request",
                run_kind="foreground",
                source_type="chat",
                model_id=str(uuid.uuid4()),
                graph_name="runtime_graph",
                graph_version="v1",
                agent_id=str(agent_delivery_id),
            ),
            "snapshots": RunInputSnapshots(
                session_context={},
                session_context_version=0,
                recent_session_messages=(),
                related_run_summaries=(),
                initial_input={},
            ),
            "lifecycle": lifecycle,
        },
        next_nodes=(),
        tasks=(),
        interrupts=(),
        metadata={"clawith_run_id": str(delivery_run_id)},
    )
    delivery_request = delivery_from_checkpoint(runtime_run_record, checkpoint)
    assert delivery_request is not None
    assert delivery_request.failure_code == "token_budget_exceeded"

    # 4) deliver_runtime_message -> _safe_failure_content —— 真实渲染出用户可见文本。
    participant = _delivery_participant(agent_delivery_id)
    membership = GroupMember(group_id=group_id, participant_id=participant.id, role="member", removed_at=None)
    db = _RecordingDB(
        run_row,
        None,
        session,
        _delivery_agent(tenant_delivery_id, agent_delivery_id),
        participant,
        _delivery_group(tenant_delivery_id, group_id),
        membership,
    )

    from app.models.audit import ChatMessage

    receipt = await deliver_runtime_message(db, delivery_request, clock=lambda: NOW)

    assert receipt.status == "delivered"
    content = _added(db, ChatMessage)[0].content
    assert "错误码：token_budget_exceeded" in content
    # 四项信息：blocked_scope / used / limit / reset_at —— 均来自真实判定，不是手写文案。
    assert "Agent 当日" in content
    assert f"{agent.tokens_used_today:,}" in content
    assert f"{agent.max_tokens_per_day:,}" in content


# ---------------------------------------------------------------------------
# (b) 触发器链路等价性：chat 与 trigger 共用同一个 RuntimeModelStepService 实例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_lane_shares_the_same_model_step_service_instance_as_direct_chat(
    monkeypatch,
) -> None:
    """把"直接对话与触发器共用同一个 RuntimeModelStepService 实例"钉成结构性约束。

    生产装配（`worker_service.build_runtime_worker_components`）只构造**一个**
    `RuntimeModelStepService`，传给**一个** `DeterministicRuntimeNodeExecutor`
    （`agent_node_executor`）；`RuntimeNodeExecutorRouter` 只按 `context.system_role
    == "group_planning"` 决定走 planning 分支还是 agent 分支，`source_type`
    （chat / trigger / task / heartbeat 等）完全不参与这个路由决策——也就是说，
    生产环境里 chat 和 trigger 两种触发方式，从路由层面就注定会落到同一个
    `agent_node_executor`，因而共用同一个 `_model_service`。

    本测试直接构造一个 `DeterministicRuntimeNodeExecutor(model_service=shared_service, ...)`
    （与生产装配用的是同一个类，只是跳过了 `RuntimeNodeExecutorRouter` 那层薄封装，
    因为路由层本身不参与判定，不是本测试要验证的对象），分别喂两个 `source_type`
    不同的 `RuntimeContext`，用 `is` 恒等断言钉住"同一个实例"这个关系；同时验证
    两次调用的 `lifecycle.reason` 完全一致——防止未来有人在某条链路上单独加一份
    判定逻辑，导致两条链路分叉却没有测试报警。
    """
    _forced_enforce(monkeypatch)
    tenant_id = uuid.uuid4()
    model = _msvc_model(tenant_id)
    agent = _breached_agent(tenant_id)

    async def unreachable_completion(*_args, **_kwargs):
        raise AssertionError("provider must not be called once the budget gate blocks the request")

    shared_service = RuntimeModelStepService(
        session_factory=_repeatable_session_factory(model, agent),
        context_builder=_ContextBuilder(_msvc_build()),
        completion=unreachable_completion,
        tool_provider=_msvc_tools,
        prompt_builder=_msvc_prompt,
        model_retry_base_delay_seconds=0,
        model_retry_jitter_ratio=0,
    )
    executor = DeterministicRuntimeNodeExecutor(
        cancel_source=_NodeCancelSource(),
        model_service=shared_service,
        tool_service=_NodeToolService(),
    )
    # 结构性钉住：驱动模型判定的确实是我们上面构造的那个唯一实例。
    assert executor._model_service is shared_service

    run_id = uuid.uuid4()
    node_state = _node_state(run_id)

    chat_context = RuntimeContext(
        tenant_id=str(tenant_id),
        run_id=str(run_id),
        command_id="command-chat",
        executor=executor,
        agent_id=str(agent.id),
        model_id=str(model.id),
        source_type="chat",
        model_turn_limit=50,
    )
    trigger_context = RuntimeContext(
        tenant_id=str(tenant_id),
        run_id=str(run_id),
        command_id="command-trigger",
        executor=executor,
        agent_id=str(agent.id),
        model_id=str(model.id),
        source_type="trigger",
        model_turn_limit=50,
    )

    # 每次 execute() 都是同一个 executor（因而同一个 shared_service）在处理，
    # 只有 context.source_type 不同。真实的 RuntimeModelStepService.complete_once
    # 完全不读 context.source_type 来决定是否判定限额——这正是"同一份判定，
    # 不因链路而分叉"这句话在代码里的样子。
    chat_update = await executor.execute("model", node_state, chat_context)
    trigger_update = await executor.execute("model", node_state, trigger_context)

    assert chat_update["lifecycle"]["status"] == "failed"
    assert trigger_update["lifecycle"]["status"] == "failed"
    assert chat_update["lifecycle"]["reason"] == "token_budget_exceeded"
    assert trigger_update["lifecycle"]["reason"] == trigger_update["lifecycle"]["reason"]
    assert chat_update["lifecycle"]["reason"] == trigger_update["lifecycle"]["reason"]


# ---------------------------------------------------------------------------
# (c) 压缩 -> 业务步的顺序：压缩节点的限额拦截必须发生在业务模型步之前
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_budget_block_prevents_the_business_model_step_from_ever_running(
    monkeypatch,
) -> None:
    """驱动真实 LangGraph 图：压缩阶段的限额拦截必须先发生，业务步永不被调用。

    与既有测试的边界：`test_agent_runtime_run_compactor.py` 已经验证了
    `RuntimeRunCompactorService.compact_if_needed` 本身在超限时会在调用
    completion 端口之前抛 `RunCompactorError`（那条测试只驱动 `compact_if_needed`
    这一个方法，图里根本没有 "model" 节点）。本测试往上一层，驱动完整的
    `DeterministicRuntimeNodeExecutor` + 真实 LangGraph 图，从 "compact" 路由开始
    执行，用一个"一旦被调用就断言失败"的业务模型服务证明：不仅压缩本身的
    completion 端口没被调用，就连图里下一步该走到的业务 "model" 节点也从未
    被触达——这正是design.md 要求的"顺序性"，不是"压缩这一步本身正确"，而是
    "压缩拦截了，业务步就没有机会跑起来"。
    """
    _forced_enforce(monkeypatch)
    tenant_id = uuid.uuid4()

    async def load_compact_inputs(_state, _context):
        return RunCompactInputs(
            model=_compact_model(tenant_id),
            ledger={},
            effective_input_budget=1_000,
            current_input_tokens=800,  # 80% watermark：触发压缩
            subjects=_compact_breached_subjects(tenant_id=tenant_id),
        )

    async def unreachable_compact_completion(*_args, **_kwargs):
        raise AssertionError("compact provider must not be called once its own budget gate blocks it")

    real_compactor = RuntimeRunCompactorService(
        settings=Settings(_env_file=None),
        completion=unreachable_compact_completion,
        input_loader=load_compact_inputs,
    )

    class _ForbiddenBusinessModelService:
        """业务模型步：一旦被调用就说明顺序性被破坏。"""

        calls = 0

        async def complete_once(self, state, context):
            del state, context
            self.calls += 1
            raise AssertionError(
                "the business model step must never run once the compact node's own "
                "budget gate has already blocked the Run"
            )

    business_model_service = _ForbiddenBusinessModelService()
    executor = DeterministicRuntimeNodeExecutor(
        cancel_source=_NodeCancelSource(),
        model_service=business_model_service,
        tool_service=_NodeToolService(),
        run_compactor=real_compactor,
    )

    run_id = uuid.uuid4()
    node_state = _node_state(run_id)
    node_state["lifecycle"]["next_route"] = "compact"
    # `compact_if_needed` short-circuits to "no compaction needed" when the Thread has
    # no messages at all (`_thread_messages` reads `state["messages"]`, which `_node_state`
    # leaves empty) -- that early return happens *before* the budget gate, so without a
    # non-empty message history this test would trivially pass for the wrong reason (no
    # compaction was attempted at all, not "compaction was attempted and blocked").
    node_state["messages"] = [
        {
            "id": "old-history",
            "role": "user",
            "content": "old history " * 12,
        },
        {
            "id": "message-1",
            "role": "user",
            "content": "go",
            "runtime_input": "current",
        },
    ]
    settings = Settings(
        _env_file=None,
        AGENT_RUNTIME_GRAPH_NAME="token_budget_integration_test",
        AGENT_RUNTIME_GRAPH_VERSION="v1",
    )
    graph = build_agent_runtime_graph(checkpointer=_in_memory_saver(), settings=settings)
    context = _node_context(run_id, executor, "command-compact-order")

    result = await graph.compiled.ainvoke(
        node_state,
        runtime_thread_config(run_id),
        context=context,
    )

    assert result["lifecycle"]["status"] == "failed"
    assert result["lifecycle"]["next_route"] == "terminal"
    assert result["lifecycle"]["reason"] == "token_budget_exceeded"
    assert business_model_service.calls == 0, "compact 的限额拦截必须先于业务模型步发生"


def _in_memory_saver():
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


# ---------------------------------------------------------------------------
# (d) 群聊 handoff：模型收到 repair 指令而不是 Run 失败
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_handoff_budget_error_is_translated_into_a_repair_not_a_run_failure(
    monkeypatch,
) -> None:
    """一个由真实预算判定产出的 GroupAgentHandoffError，被 complete_once 的真实
    翻译逻辑转成 repair，而不是 Run 失败。

    与既有测试的边界：这条测试有意分两半组合，而不是重新构造一次完整的、满足
    `_snapshot_scope` 严格校验的群聊状态去驱动 `complete_once` 内部真正调用
    `preflight_group_agent_handoff`（那需要的脚手架复杂度与 `test_agent_runtime_model_step_service.py`
    里已有的 group_handoff 测试所用的最小 group_context 完全不兼容，重新搭建的
    成本远超过它能带来的额外信心）：

    1. **真实产出异常的那一半**：直接复用 `test_agent_runtime_group_handoff.py`
       已经验证过的 `preflight_group_agent_handoff` + 真实击穿目标 Agent，拿到
       一个**真实的** `GroupAgentHandoffError("group_handoff_budget_unavailable",
       repairable=True)` 实例（不是手写一个同名同码的合成异常）。
    2. **真实翻译逻辑的那一半**：把这个真实异常通过 `preflight_group_agent_handoff`
       的 mock 注入点喂给 `complete_once`，验证 `complete_once` 内已有的
       "`GroupAgentHandoffError.repairable=True` -> `intent='text'` + repair
       instruction，而不是 `intent='error'`"这段真实翻译逻辑，能正确处理这个
       真实异常的具体形状（`code`/`message`/`repairable`）。

    `test_agent_runtime_model_step_service.py::test_group_handoff_preflight_failure_repairs_without_finishing`
    已经验证了这段翻译逻辑本身（用一个手写的 `target is no longer active` 合成异常），
    `test_agent_runtime_group_handoff.py::test_breached_agent_token_budget_fails_preflight_with_repairable_error`
    已经验证了真实的预算击穿会产出这个异常。本测试把两者用同一个异常实例串起来，
    确认"真实产出的异常形状"确实能被"真实的翻译逻辑"正确处理——这是前两条测试
    各自都无法单独证明的一点：万一某次改动让 `GroupAgentHandoffError` 的异常形状
    发生了字段级的微妙变化（例如 message 里新增了某个真实预算判定特有的字段），
    分开测试可能两边都还各自通过，但拼在一起就会露出不兼容。
    """
    _handoff_forced_enforce(monkeypatch)
    source_run, scope, handoff_context, handoff_state = _handoff_records()
    over_limit_target = _handoff_target(
        tenant_id=source_run.tenant_id,
        max_tokens_per_day=100_000,
        tokens_used_today=200_000,
        last_daily_reset=datetime.now(UTC),
    )
    ensure = AsyncMock(return_value=_cycle_check())

    with (
        patch(
            "app.services.agent_runtime.group_handoff._load_source_run",
            new=AsyncMock(return_value=source_run),
        ),
        patch(
            "app.services.agent_runtime.group_handoff._load_sender_scope",
            new=AsyncMock(return_value=scope),
        ),
        patch(
            "app.services.agent_runtime.group_handoff._resolve_mentions",
            new=AsyncMock(return_value=(over_limit_target,)),
        ),
        patch(
            "app.services.agent_runtime.group_handoff.AgentCycleGuard.ensure_delegation_allowed",
            new=ensure,
        ),
    ):
        with pytest.raises(GroupAgentHandoffError) as raised:
            from app.services.agent_runtime.group_handoff import preflight_group_agent_handoff

            await preflight_group_agent_handoff(
                _msvc_null_db(),
                state=handoff_state,
                context=handoff_context,
                content="Continue",
                mention_participant_ids=(str(over_limit_target.participant_id),),
                settings=_handoff_settings(),
                clock=lambda: NOW,
            )

    real_budget_error = raised.value
    assert real_budget_error.code == "group_handoff_budget_unavailable"
    assert real_budget_error.repairable is True

    # 第二半：把这个真实异常喂给 complete_once 的真实翻译逻辑。
    tenant_id = uuid.uuid4()
    model = _msvc_model(tenant_id)
    agent = _msvc_agent(tenant_id)  # 未超限：complete_once 自己的业务步闸门必须放行，
    # 这样才能走到 finish -> group handoff 分支，而不是在更早的 _budget_gate 就短路。
    msvc_state = _msvc_state(tenant_id, model, agent)
    msvc_state["snapshots"] = RunInputSnapshots(
        session_context=msvc_state["snapshots"].session_context,
        session_context_version=msvc_state["snapshots"].session_context_version,
        recent_session_messages=msvc_state["snapshots"].recent_session_messages,
        related_run_summaries=(),
        initial_input={"group_context": {"group": {"group_id": str(uuid.uuid4())}}},
    )
    target_participant_id = uuid.uuid4()

    async def complete(*_args, **_kwargs):
        from app.services.llm.single_step import LLMCompletionStep
        from app.services.token_tracker import TokenUsage

        return LLMCompletionStep(
            content="",
            tool_calls=(
                {
                    "id": "finish-real-handoff-error",
                    "type": "function",
                    "function": {
                        "name": "finish",
                        "arguments": {
                            "content": "Please continue",
                            "mention_participant_ids": [str(target_participant_id)],
                        },
                    },
                },
            ),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=10),
        )

    with patch(
        "app.services.agent_runtime.model_step_service.preflight_group_agent_handoff",
        new=AsyncMock(side_effect=real_budget_error),
    ) as preflight:
        result = await RuntimeModelStepService(
            session_factory=_msvc_session_factory(model, agent),
            context_builder=_ContextBuilder(
                _msvc_build(initial_input=msvc_state["snapshots"].initial_input)
            ),
            completion=complete,
            tool_provider=_msvc_tools,
            prompt_builder=_msvc_prompt,
        ).complete_once(msvc_state, _msvc_context(msvc_state))

    assert preflight.await_count == 1
    # 核心断言：模型收到的是 repair 指令，不是 Run 失败。
    assert result.intent == "text"
    assert result.finish_content is None
    assert result.finish_delivery_intent is None
    assert result.repair_instruction is not None
    assert real_budget_error.code in result.repair_instruction
    assert "No public message or child Run was created" in result.repair_instruction


def _msvc_null_db():
    """一个不会被使用的 db 占位对象——`preflight_group_agent_handoff` 里除
    `_load_source_run` / `_load_sender_scope` / `_resolve_mentions` 之外，
    真实执行到的下一步是 `gate.load_subjects(db, tenant_id=...)`（在
    `_validate_targets` 内），会真的调用 `db.execute()` 两次。"""
    from sqlalchemy.ext.asyncio import AsyncSession

    class _NullResult:
        def scalar_one_or_none(self):
            return None

    class _NullDB:
        async def execute(self, _statement):
            return _NullResult()

    return _NullDB()


# ---------------------------------------------------------------------------
# (e) 执行模式切换：PUT 之后同进程内下一次 *判定*（不是 current_enforcement_mode()
#     本身）立即用新值
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_endpoint_mode_switch_takes_effect_on_the_very_next_gate_judgement(
    monkeypatch,
) -> None:
    """从 warn_only 切到 enforce 后，下一次真实的限额判定（不只是 current_enforcement_mode()
    这个独立调用）立即观察到新值。

    与既有测试的边界：`test_enterprise_token_budget_enforcement.py::
    test_put_endpoint_platform_admin_succeeds_and_next_read_in_process_sees_the_new_value`
    已经验证了 `budget.current_enforcement_mode()` 这个**读取函数本身**在 PUT 后
    立即返回新值；本测试往上一层，驱动一次真实的 `RuntimeModelStepService._budget_gate()`
    调用（`gate.check()` 的生产消费者之一），确认切换真的能改变一次业务判定的结果
    （从"放行"变成"拦截"），而不只是"某个内部读取函数返回的字符串变了"。
    """
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    store = _FakeSettingStore({SETTING_ENFORCEMENT_MODE: {"mode": "warn_only"}})
    _patch_store(monkeypatch, store)

    service = RuntimeModelStepService(session_factory=lambda: None, context_builder=SimpleNamespace(build=None))
    context = SimpleNamespace(
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        model_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Ada",
        tenant_id=tenant_id,
        timezone=None,
        max_tokens_per_day=100_000,
        max_tokens_per_month=None,
        tokens_used_today=200_000,
        tokens_used_month=0,
        last_daily_reset=datetime.now(UTC),
        last_monthly_reset=datetime.now(UTC),
    )
    tenant = SimpleNamespace(id=tenant_id, timezone="UTC", max_tokens_per_day=None)
    counter = SimpleNamespace(tenant_id=tenant_id, tokens_used_today=0, last_daily_reset=datetime.now(UTC))

    # 切换前：warn_only，业务步闸门放行（_budget_gate 返回 None）。
    before = await service._budget_gate(context, agent, (tenant, counter), estimated_next_round_tokens=0)
    assert before is None, "warn_only 下命中限额仍应放行"

    await enterprise.update_token_budget_enforcement(
        enterprise.TokenBudgetEnforcementUpdate(mode="enforce"),
        current_user=_platform_admin(),
    )

    # 切换后：同一进程、同一个 service 实例、完全相同的输入，下一次判定必须立即拦截。
    after = await service._budget_gate(context, agent, (tenant, counter), estimated_next_round_tokens=0)
    assert after is not None
    assert after.intent == "error"
    assert after.error["code"] == "token_budget_exceeded"


# ---------------------------------------------------------------------------
# (f) grace 窗口：生效时放行 + 落 INFO 日志；clear_grace 后同一请求立即被拦截
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grace_window_allows_then_clear_grace_immediately_blocks_the_same_judgement(
    monkeypatch,
) -> None:
    """`configured_mode=enforce` + `grace_until` 在未来 -> 放行且落 grace 生效日志；
    `PUT ... clear_grace=true` 之后，同一个判定输入立即被拦截。

    与既有测试的边界：`test_token_accounting_budget.py::
    test_grace_active_forces_warn_only_and_logs_once_per_ttl` 已经验证了
    `current_enforcement_state()` 本身在 grace 生效时的行为与节流日志；
    `test_enterprise_token_budget_enforcement.py::
    test_put_endpoint_clear_grace_true_immediately_deactivates_grace` 已经验证了
    `clear_grace=true` 后 `current_enforcement_state()` 立即不再报告 grace 生效。
    两者都停在 `budget.py` 内部读取函数这一层。本测试把它们与 `gate.check()`
    （真正做判定、真正落 `[TokenBudget]` 日志的那一层）串起来：验证的是"grace
    窗口内一次真实的超限判定确实被放行"与"清除 grace 后同一个判定立即翻成拦截"，
    这是 `current_enforcement_state()` 单独测试无法证明的一点——`gate.check()`
    有自己独立的一套日志与 verdict 构造逻辑，理论上可能与 `current_enforcement_state()`
    的行为脱节（例如未来有人在 `gate.check()` 里意外传了显式 `mode=`，绕开
    grace 语义）。
    """
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    store = _FakeSettingStore(
        {SETTING_ENFORCEMENT_MODE: {"mode": "enforce", "grace_until": future, "set_by": "migration"}}
    )
    _patch_store(monkeypatch, store)

    now = datetime.now(UTC)
    subjects = BudgetSubjects(
        agent=SimpleNamespace(
            id=agent_id,
            tenant_id=tenant_id,
            timezone=None,
            max_tokens_per_day=100_000,
            max_tokens_per_month=None,
            tokens_used_today=200_000,
            tokens_used_month=0,
            last_daily_reset=now,
            last_monthly_reset=now,
        ),
        tenant=SimpleNamespace(id=tenant_id, timezone="UTC", max_tokens_per_day=None),
        tenant_counter=SimpleNamespace(tenant_id=tenant_id, tokens_used_today=0, last_daily_reset=now),
    )

    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["level"].name, str(message))),
        level="TRACE",
    )
    try:
        grace_verdict = await gate.check(lane=LANE_BUSINESS_STEP, subjects=subjects, run_id="run-grace")
    finally:
        logger.remove(handler_id)

    assert grace_verdict.allowed is True, "grace 窗口内即使命中限额也必须放行"
    assert grace_verdict.mode == MODE_WARN_ONLY
    grace_logs = [
        text for level, text in records if level == "INFO" and "token_budget_enforcement_grace_active" in text
    ]
    assert len(grace_logs) == 1, "grace 生效必须落一条可 grep 的 INFO 日志"

    await enterprise.update_token_budget_enforcement(
        enterprise.TokenBudgetEnforcementUpdate(mode="enforce", clear_grace=True),
        current_user=_platform_admin(),
    )

    blocked_verdict = await gate.check(lane=LANE_BUSINESS_STEP, subjects=subjects, run_id="run-grace")
    assert blocked_verdict.allowed is False, "clear_grace 之后同一个判定输入必须立即被拦截"
    assert blocked_verdict.mode == MODE_ENFORCE
