"""Task 1（bugfix：token-usage-limit-not-enforced）—— Bug Condition 探索性复现测试。

**这些测试在未修复代码上运行**，目的是在动手修之前先拿到反例，确认根因在
「执行模式默认值 + 缺失的闸门」而不在统计侧（记账口径 / 时区口径 / 陈旧实例误判，
这三条已经在 requirements.bugfix.md 里用代码阅读排除过）。

七条反例对应 design.md "Exploratory Bug Condition Checking" 与 bugfix.md 的
isBugCondition 公式：

1. `business_step` 配置缺省（行缺失即回退到执行模式）—— 期望 **通过**。
   **任务 3.2 已修复**：修复前这一支的安全默认值是 `warn_only`（配置缺省即放行）；
   修复后归类为「配置层缺省」，安全默认值改为 `enforce`，命中限额的请求会被拦截。
   本反例的断言已随修复同步更新为「验证已修复」，不再是「证明成因存在」。
2. `run_compact` 无闸门 —— 期望 **通过**。
   **任务 6.1 已修复**：修复前 `run_compactor.compact_if_needed` 完全不携带 Agent /
   Tenant / 限额判定结果，超限 Agent 走到 80% 压缩水位时 completion 端口照常被
   调用；修复后 `RunCompactInputs` 扩展了 `subjects: BudgetSubjects | None` 字段，
   由 `model_step_service.compact_inputs` 顺带带出，`compact_if_needed` 在
   `_should_compact` 判定为真之后、进入 `_compact_batches` 之前调
   `gate.check(lane=LANE_RUN_COMPACT, ...)`，超限时 `raise
   RunCompactorError("token_budget_exceeded", ...)`，completion 端口不再被调用。
   本反例的断言已随修复同步更新为「验证已修复」（completion 端口未被调用的正向
   断言），不再是「证明闸门缺失」。
3. `planning` 无闸门 —— 期望 **通过**。
   **任务 6.3 已修复**：修复前 `PlanningModelService.complete_once` 完全不携带
   Tenant / TenantTokenCounter / 限额判定结果，超限租户下 completion 端口照常
   被调用；修复后 `complete_once` 在 `_load_model` 之后、`self._completion` 之前
   用 `self._session_factory` 单独开一次会话调
   `gate.load_subjects(db, tenant_id=..., agent=None)`（Planning 是租户级判定，
   只判 `tenant_day` 一档，依赖任务 3.1），再调 `gate.check(lane=LANE_PLANNING,
   ...)`，超限时 `return PlanningModelResult(error_code="token_budget_exceeded",
   retryable=False)`，completion 端口不再被调用。本反例的断言已随修复同步更新为
   「验证已修复」，不再是「证明闸门缺失」。
4. `session_compact` / `group_compact` 无闸门 —— 期望 **通过**。
   **任务 6.2 已修复**：修复前 `_compact_with_model` 完全不携带限额判定结果，
   超限主体下 completion 端口照常被调用；修复后 `CompactModelSelection` 扩展了
   `subjects: BudgetSubjects | None` 字段，由 `_resolve_models` 在它已打开的会话
   里一并 `load_subjects` 带出，`_compact_with_model` 在进入首个 batch 之前调
   `gate.check(lane=...)`（session_compact 或 group_compact），超限时 `raise
   SessionContextCompactorError("token_budget_exceeded", ...)`，completion 端口
   不再被调用。本反例的断言已随修复同步更新为「验证已修复」，不再是「证明闸门
   缺失」。
5. `model_probe` 无闸门 —— 期望 **通过**。
   **任务 6.4 已修复**：修复前 `test_llm_model` 完全不携带 Tenant / TenantTokenCounter /
   限额判定结果，超限租户下 `create_llm_client` 照常被调用；修复后
   `_resolve_probe_budget_clearance` 在 `create_llm_client` 之前用 `current_user.tenant_id`
   单独开一次会话调 `gate.load_subjects(db, tenant_id=..., agent=None)`（model_probe 是
   租户级判定，只判 `tenant_day` 一档，依赖任务 3.1），再调
   `gate.check(lane=LANE_MODEL_PROBE, ...)`，超限时直接
   `return {"success": False, "error_code": "token_budget_exceeded", ...}`，不创建
   LLM client。本反例的断言已随修复同步更新为「验证已修复」（未创建 LLM client 的正向
   断言），不再是「证明闸门缺失」。
6. `group_handoff` 与 `business_step` 口径矛盾 —— 期望 **通过**。
   **任务 7.1 已修复**：`group_handoff._target_budget_available` 已拆分——token 部分
   （原来那段无视执行模式的硬拦）已删除，`_validate_targets` 现在对每个目标调
   `gate.check(lane=LANE_GROUP_HANDOFF, ...)`，与 `business_step` 走的是同一个
   `budget.evaluate()`；非 token 部分（`max_tool_rounds` / `max_llm_calls_per_day`）
   原样保留在更名后的 `_target_run_budget_available()` 里。本反例的性质因此从
   「两个结论今天真的相反」变成了「两个结论现在天然一致」（因为两侧现在共用同一个
   `verdict`），断言已随修复同步更新为「验证两侧口径一致」的正向断言。
7. `budget.evaluate(agent=None, ...)` —— 期望 **通过**（任务 3.1 已修复：只判 `tenant_day`
   一档，不再抛 `AttributeError`）

**CRITICAL**：反例 1-7 均已随对应任务（3.2、6.1、6.2、6.3、6.4、7.1、3.1）的修复转为
正向断言，本文件的 7 个反例现在**全部 PASS**。任务 1 里"两套口径互相矛盾"这个 bug
本身已被修复：`group_handoff` 与 `business_step` 现在共用同一个 `gate.check()` /
`budget.evaluate()` 判定实现与同一份执行模式（2.10）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api import enterprise
from app.models.llm import LLMModel
from app.services.agent_runtime import model_step_service
from app.services.agent_runtime.planning import PlanningModelService
from app.services.agent_runtime.run_compactor import RunCompactorError
from app.services.agent_runtime.session_context_compactor import (
    CompactModelSelection,
    LLMSessionContextCompactor,
    SessionContextCompactorError,
)
from app.services.agent_runtime.session_context_service import (
    SessionContextSnapshot,
)
from app.services.agent_runtime.session_context_completion import (
    SessionCompactRequest,
)
from app.services.token_accounting import budget, gate
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    MODE_WARN_ONLY,
    SCOPE_AGENT_DAY,
    SCOPE_TENANT_DAY,
    reset_enforcement_mode_cache,
)
from app.services.token_accounting.gate import BudgetSubjects

# 复用现有测试模块里的构造函数，与 test_token_budget_enforcement.py 的做法一致
# （见该文件顶部的说明：本仓库不新增 fixture 基础设施，直接复用姊妹模块的构造器）。
from test_agent_runtime_planning import (
    _context as _planning_context,
)
from test_agent_runtime_planning import (
    _session_factory as _planning_session_factory,
)
from test_agent_runtime_planning import (
    _state as _planning_state,
)
from test_agent_runtime_planning import (
    _breached_tenant as _planning_breached_tenant,
)
from test_agent_runtime_planning import (
    _forced_enforce as _planning_forced_enforce,
)
from test_agent_runtime_run_compactor import (
    _model as _rc_model,
)
from test_agent_runtime_run_compactor import (
    _service as _rc_service,
)
from test_agent_runtime_run_compactor import (
    _state as _rc_state,
)
from test_agent_runtime_run_compactor import (
    _step as _rc_step,
)


NOW = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)  # 北京 8/7 00:30
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _reset_enforcement_mode_cache_between_tests():
    """避免用例间通过 30 秒 TTL 的进程内模式缓存互相污染（任务 3.3）。"""
    reset_enforcement_mode_cache()
    yield
    reset_enforcement_mode_cache()


def _breached_agent(**overrides) -> SimpleNamespace:
    """同一个「日上限 100,000、已用 200,000」的超限 Agent，跨反例 1/6 复用。

    字段同时满足 budget.evaluate() 与 group_handoff._target_run_budget_available()
    两侧的读取需要，这样反例 6 才能真正比较「同一个 Agent」在两条链路上的结论。
    """
    base = {
        "id": AGENT_ID,
        "name": "Ada",
        "tenant_id": TENANT_ID,
        "timezone": None,
        "max_tokens_per_day": 100_000,
        "max_tokens_per_month": None,
        "tokens_used_today": 200_000,
        "tokens_used_month": 0,
        "last_daily_reset": NOW,
        "last_monthly_reset": NOW,
        # group_handoff 非 token 的可用性检查项，都设成「未耗尽」，
        # 使 _target_budget_available 的结论只由 token 部分决定。
        "max_tool_rounds": 10,
        "max_llm_calls_per_day": None,
        "llm_calls_today": 0,
        "llm_calls_reset_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _tenant(**overrides) -> SimpleNamespace:
    base = {"id": TENANT_ID, "timezone": "Asia/Shanghai", "max_tokens_per_day": 500_000}
    base.update(overrides)
    return SimpleNamespace(**base)


def _breached_tenant_counter(**overrides) -> SimpleNamespace:
    """租户日上限（500,000）已击穿的计数器，供反例 3/4/5 复用。"""
    base = {"tenant_id": TENANT_ID, "tokens_used_today": 500_000, "last_daily_reset": NOW}
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 反例 1：配置缺省即放行（business_step + warn_only）
# 任务 3.2 已修复：「配置缺省」分支的安全默认值从 warn_only 改成了 enforce
# （design.md「兜底语义的重新定义」表：读取动作成功但值不可用 = 配置层缺省 -> enforce）。
# EXPECTED OUTCOME（修复后）：通过 —— 证明这一成因已被修复：配置缺省时不再被当成
# 「不限制」，而是安全默认为 enforce，命中限额的请求会被拦截。
# ---------------------------------------------------------------------------


async def test_counterexample_1_missing_setting_row_defaults_to_enforce_and_blocks(
    monkeypatch,
) -> None:
    """`system_settings` 无此行时，`current_enforcement_mode()` 现在返回 enforce。

    对应 isBugCondition 里 `lane = business_step AND mode = warn_only` 这一支
    （requirements 1.5 / 1.6）。任务 3.2 修复前，这一支的安全默认值是 warn_only，
    命中限额的请求被放行；修复后，「读取动作成功但值不可用」被归类为「配置层缺省」，
    安全默认值改为 enforce，命中限额的请求会被拦截（expectedBehavior(result) 里
    `allowed = (effective_mode == MODE_WARN_ONLY)` 在 enforce 下取 False）。
    用真实 `budget.evaluate()`，不打桩判定本身，只打桩它读配置的那一层 DAO 方法。
    """

    async def fake_get_value(key, default=None):
        del key
        return default  # 模拟行缺失：DAO 按约定退回调用方传入的 default（今天是 {}）

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    mode = await budget.current_enforcement_mode()
    assert mode == MODE_ENFORCE

    agent = _breached_agent()
    tenant = _tenant()
    counter = SimpleNamespace(tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW)

    verdict = await budget.evaluate(
        agent=agent,
        tenant=tenant,
        tenant_counter=counter,
        now=NOW,
        mode=None,  # 不显式传 mode，让 evaluate 自己走 current_enforcement_mode()
    )

    # 消息形状：命中限额，blocked_scope/used/limit 都被正确标出，且 allowed 现在是
    # False —— 配置缺省不再被当成「不限制」，2.1/2.5/2.6 的期望行为达成。
    assert verdict.blocked_scope == SCOPE_AGENT_DAY
    assert verdict.used == 200_000
    assert verdict.limit == 100_000
    assert verdict.mode == MODE_ENFORCE
    assert verdict.allowed is False, (
        "反例 1 已修复：配置缺省不再被放行 —— 安全默认值现在是 enforce，"
        "命中限额的请求会被拦截"
    )


# ---------------------------------------------------------------------------
# 反例 2：run_compact 无闸门
# 任务 6.1 已修复：`RunCompactInputs` 扩展了 `subjects` 字段，`compact_if_needed`
# 在 `_should_compact` 判定为真之后、进入 `_compact_batches` 之前调
# `gate.check(lane=LANE_RUN_COMPACT, ...)`，超限时抛 `RunCompactorError`，
# completion 端口不再被调用。
# EXPECTED OUTCOME（修复后）：通过 —— 证明这一成因已被修复。
# ---------------------------------------------------------------------------


async def test_counterexample_2_run_compact_now_blocks_before_completion(
    monkeypatch,
) -> None:
    """驱动 `RuntimeRunCompactorService.compact_if_needed` 到达压缩水位。

    修复前 `RunCompactInputs` 完全不携带 Agent / Tenant / 限额判定结果，超限
    Agent 走到 80% 水位时 completion 端口照常被调用；修复后 `compact_if_needed`
    在进入 `_compact_batches` 之前会先判定，超限时短路并抛
    `RunCompactorError("token_budget_exceeded")`，completion 端口未被调用。
    用真实 `budget.evaluate()`，只把执行模式显式钉死为 enforce（与反例 1/7 的
    做法一致），避免结果随执行模式默认值/缓存状态摇摆。
    """
    from app.services.token_accounting import gate as gate_module

    original_evaluate = gate_module.evaluate

    async def forced_enforce_evaluate(**kwargs):
        return await original_evaluate(**{**kwargs, "mode": MODE_ENFORCE})

    monkeypatch.setattr(gate_module, "evaluate", forced_enforce_evaluate)

    # `compact_if_needed` -> `gate.check()` 不传显式 `now`，`budget.evaluate()`
    # 会用 `datetime.now(UTC)`（真实挂钟时间）判断周期是否已翻页——不能像本文件
    # 其它反例那样用固定的 `NOW`（2026-08-06），否则 `last_daily_reset` 相对真实
    # 当下会落在"未来"，导致 `_effective_used` 的翻页判定结果不可预期。
    wall_clock_now = datetime.now(UTC)
    over_limit_agent = _breached_agent(last_daily_reset=wall_clock_now)
    tenant = _tenant()
    counter = SimpleNamespace(
        tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=wall_clock_now
    )
    subjects = BudgetSubjects(agent=over_limit_agent, tenant=tenant, tenant_counter=counter)

    messages = [
        *[
            {"id": f"old-{index}", "role": "user", "content": "old history " * 12}
            for index in range(8)
        ],
        {
            "id": "current",
            "role": "user",
            "content": "EXACT CURRENT INPUT",
            "runtime_input": "current",
        },
    ]
    state, context, tenant_id = _rc_state(messages)

    calls: list[tuple] = []

    async def recording_completion(*args, **kwargs):
        calls.append((args, kwargs))
        return _rc_step()

    with pytest.raises(RunCompactorError) as raised:
        await _rc_service(
            model=_rc_model(tenant_id),
            completion=recording_completion,
            effective_budget=1_000,
            current_tokens=800,  # 80% 水位，触发压缩
            subjects=subjects,
        ).compact_if_needed(state, context)

    assert raised.value.code == "token_budget_exceeded"
    assert raised.value.is_deterministic_compact_error is True
    assert calls == [], (
        "反例 2 已修复：run_compact 链路现在有限额判定，completion 端口在超限"
        " Agent 下不再被调用"
    )


# ---------------------------------------------------------------------------
# 反例 3：planning 无闸门
# 任务 6.3 已修复：`complete_once` 在 `_load_model` 之后、`self._completion` 之前
# 用 `self._session_factory` 单独开一次会话调
# `gate.load_subjects(db, tenant_id=..., agent=None)`，再调
# `gate.check(lane=LANE_PLANNING, ...)`，超限时 `return
# PlanningModelResult(error_code="token_budget_exceeded", retryable=False)`，
# completion 端口不再被调用。
# EXPECTED OUTCOME（修复后）：通过 —— 证明这一成因已被修复。
# ---------------------------------------------------------------------------


async def test_counterexample_3_planning_now_blocks_before_completion(
    monkeypatch,
) -> None:
    """构造租户日上限已击穿的 `Tenant` / `TenantTokenCounter`，驱动
    `PlanningModelService.complete_once`。

    修复前 `PlanningModelService` 完全不读任何 Tenant / TenantTokenCounter，
    超限租户下 completion 端口照常被调用；修复后 `complete_once` 会在真正发起
    provider 请求之前先判定，超限时短路并返回
    `PlanningModelResult(error_code="token_budget_exceeded", retryable=False)`，
    completion 端口未被调用。用真实 `budget.evaluate()`，只把执行模式显式钉死为
    enforce（与反例 1/2/4/7 的做法一致），避免结果随执行模式默认值/缓存状态摇摆。
    """
    _planning_forced_enforce(monkeypatch)

    first, second = uuid.uuid4(), uuid.uuid4()
    tenant_id = uuid.uuid4()
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="openai",
        model="planning-model",
        api_key_encrypted="encrypted",
        label="Planning",
        enabled=True,
        max_output_tokens=2048,
        max_input_tokens=64_000,
    )
    tenant, over_limit_counter = _planning_breached_tenant(tenant_id)
    state = _planning_state((first, second))
    context = _planning_context(
        model_id=model.id,
        tenant_id=tenant_id,
        goal="Research the topic, then coordinate the write-up",  # 非简单问候，走真实模型调用
    )

    calls: list[tuple] = []

    async def recording_completion(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("counterexample probe: stop before producing a plan")

    service = PlanningModelService(
        session_factory=_planning_session_factory(
            model, tenant=tenant, tenant_counter=over_limit_counter
        ),
        completion=recording_completion,
    )

    result = await service.complete_once(state, context)

    assert result.plan is None
    assert result.error_code == "token_budget_exceeded"
    assert result.retryable is False
    assert calls == [], (
        "反例 3 已修复：planning 链路现在有限额判定，completion 端口在租户日上限"
        "已击穿时不再被调用"
    )


# ---------------------------------------------------------------------------
# 反例 4：session_compact / group_compact 无闸门
# 任务 6.2 已修复：`CompactModelSelection` 扩展了 `subjects` 字段，
# `_compact_with_model` 在进入首个 batch 之前调 `gate.check(lane=...)`，超限时抛
# `SessionContextCompactorError("token_budget_exceeded")`，completion 端口不再被
# 调用。
# EXPECTED OUTCOME（修复后）：通过 —— 证明这一成因已被修复。
# ---------------------------------------------------------------------------


async def test_counterexample_4_group_compact_now_blocks_before_completion(
    monkeypatch,
) -> None:
    """驱动 `LLMSessionContextCompactor.compact`（group_compact 分支，agent=None）。

    修复前 `_compact_with_model` 完全不携带限额判定结果，超限主体下 completion
    端口照常被调用；修复后超限的租户日计数会在进入首个 batch 之前短路。用真实
    `budget.evaluate()`，只把执行模式显式钉死为 enforce（与反例 1/2/7 的做法
    一致），避免结果随执行模式默认值/缓存状态摇摆。
    """
    from app.services.token_accounting import gate as gate_module

    original_evaluate = gate_module.evaluate

    async def forced_enforce_evaluate(**kwargs):
        return await original_evaluate(**{**kwargs, "mode": MODE_ENFORCE})

    monkeypatch.setattr(gate_module, "evaluate", forced_enforce_evaluate)

    # `_compact_with_model` -> `gate.check()` 不传显式 `now`，`budget.evaluate()`
    # 会用 `datetime.now(UTC)`（真实挂钟时间）判断周期是否已翻页——不能像本文件
    # 其它反例那样用固定的 `NOW`（2026-08-06），理由与反例 2 相同。
    wall_clock_now = datetime.now(UTC)
    tenant_id = uuid.uuid4()
    tenant = _tenant(id=tenant_id, timezone="UTC")
    over_limit_counter = SimpleNamespace(
        tenant_id=tenant_id, tokens_used_today=500_000, last_daily_reset=wall_clock_now
    )
    subjects = BudgetSubjects(agent=None, tenant=tenant, tenant_counter=over_limit_counter)

    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="openai",
        model="group-compact-model",
        api_key_encrypted="encrypted",
        label="Group Compact",
        enabled=True,
        max_input_tokens=100_000,
        max_output_tokens=256,
    )
    request = SessionCompactRequest(
        tenant_id=tenant_id,
        session_id=uuid.uuid4(),
        source_agent_id=uuid.uuid4(),
        checkpoint_id="checkpoint-terminal",
        snapshot=SessionContextSnapshot(
            version=1,
            summary="old summary",
            requirements=(),
            decisions=(),
            open_items=(),
            evidence_refs=(),
            workspace_refs=(),
            covered_through_message_id=None,
        ),
        messages=(
            {"id": str(uuid.uuid4()), "role": "user", "content": "new message"},
        ),
        delta=None,
    )

    calls: list[tuple] = []

    async def recording_completion(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("counterexample probe: stop before producing a candidate")

    async def resolver(_request):
        return CompactModelSelection(
            primary=model,
            usage_agent_id=None,
            system_scope="group_compact",
            subjects=subjects,
        )

    class _UnusedSessionFactory:
        def __call__(self):
            raise AssertionError("injected model resolver must avoid database access")

    compactor = LLMSessionContextCompactor(
        session_factory=_UnusedSessionFactory(),  # type: ignore[arg-type]
        model_resolver=resolver,
        completion=recording_completion,
    )

    with pytest.raises(SessionContextCompactorError) as exc_info:
        await compactor.compact(request)

    assert exc_info.value.code == "token_budget_exceeded"
    assert calls == [], (
        "反例 4 已修复：session/group_compact 链路现在有限额判定，completion 端口"
        "在超限主体下不再被调用"
    )


# ---------------------------------------------------------------------------
# 反例 5：model_probe 无闸门
# 任务 6.4 已修复：`_resolve_probe_budget_clearance` 在 `create_llm_client` 之前
# 用 `current_user.tenant_id` 单独开一次会话取 tenant / tenant_counter，调
# `gate.check(lane=LANE_MODEL_PROBE, ...)`，超限时直接返回结构化失败体，不创建
# LLM client。
# EXPECTED OUTCOME（修复后）：通过 —— 证明这一成因已被修复。
# ---------------------------------------------------------------------------


class _FakeProbeClient:
    """记录自己被创建/调用的最小连通性测试替身。"""

    def __init__(self) -> None:
        self.completed = 0

    async def complete(self, **kwargs):
        del kwargs
        self.completed += 1
        return SimpleNamespace(content="ok", usage=None, tool_calls=None)

    async def close(self) -> None:
        return None


async def test_counterexample_5_model_probe_now_blocks_before_creating_a_client(
    monkeypatch,
) -> None:
    """租户日上限已击穿 -> 调 `/enterprise/llm-test` -> 断言未创建 LLM client。

    修复前 `test_llm_model` 完全不携带 Tenant / TenantTokenCounter / 限额判定结果，
    超限租户下 `create_llm_client` 照常被调用；修复后会在真正创建 client 之前先
    判定，超限时短路并返回 `error_code == "token_budget_exceeded"`，
    `create_llm_client` 未被调用。用真实 `budget.evaluate()`，只把执行模式显式钉死
    为 enforce（与反例 1/2/3/4/7 的做法一致），避免结果随执行模式默认值/缓存状态
    摇摆。
    """
    from app.services.token_accounting import gate as gate_module

    original_evaluate = gate_module.evaluate

    async def forced_enforce_evaluate(**kwargs):
        return await original_evaluate(**{**kwargs, "mode": MODE_ENFORCE})

    monkeypatch.setattr(gate_module, "evaluate", forced_enforce_evaluate)

    # `_resolve_probe_budget_clearance` -> `gate.check()` 不传显式 `now`，
    # `budget.evaluate()` 会用 `datetime.now(UTC)`（真实挂钟时间）判断周期是否已
    # 翻页——不能像本文件其它反例那样用固定的 `NOW`（2026-08-06），理由与反例 2/4
    # 相同。
    wall_clock_now = datetime.now(UTC)
    over_limit_counter = _breached_tenant_counter(last_daily_reset=wall_clock_now)

    class _FakeDB:
        def __init__(self) -> None:
            self._results = [_tenant(), over_limit_counter]

        async def execute(self, _statement):
            value = self._results.pop(0)
            return SimpleNamespace(scalar_one_or_none=lambda: value)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return False

    monkeypatch.setattr(enterprise, "async_session", lambda: _FakeDB())

    target = enterprise.LLMTestTarget(
        model_id=None,
        provider="ollama",
        model="qwen-local",
        api_key="ollama",
        base_url="http://localhost:11434/v1",
    )

    async def fake_resolve_target(_data, _current_user):
        return target

    created_clients: list[_FakeProbeClient] = []

    def fake_create_llm_client(**kwargs):
        del kwargs
        client = _FakeProbeClient()
        created_clients.append(client)
        return client

    monkeypatch.setattr(enterprise, "_resolve_llm_test_target", fake_resolve_target)
    monkeypatch.setattr(enterprise, "create_llm_client", fake_create_llm_client)

    current_user = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=TENANT_ID)

    result = await enterprise.test_llm_model(
        enterprise.LLMTestRequest(
            provider="ollama",
            model="qwen-local",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
        ),
        current_user=current_user,
    )

    assert created_clients == [], (
        "反例 5 已修复：model_probe 链路现在有限额判定，create_llm_client 在租户日"
        "上限已击穿时不再被调用"
    )
    assert result["success"] is False
    assert result["error_code"] == "token_budget_exceeded"


# ---------------------------------------------------------------------------
# 反例 6：口径矛盾 -> 收敛后口径一致（group_handoff vs business_step）
# 任务 7.1 已修复：`group_handoff._validate_targets` 现在对每个目标调
# `gate.check(lane=LANE_GROUP_HANDOFF, ...)`，与 `business_step` 走的是同一个
# `budget.evaluate()`。两侧现在共用同一个 verdict，结论天然一致（不再需要"恰好写得
# 一样"这种巧合）。
# EXPECTED OUTCOME（修复后）：通过 —— 两个结论现在一致，无论执行模式是什么。
# ---------------------------------------------------------------------------


async def test_counterexample_6_group_handoff_and_business_step_now_agree(
    monkeypatch,
) -> None:
    """同一个超限 Agent：`gate.check(lane=LANE_GROUP_HANDOFF, ...)` 与
    `gate.check(lane=LANE_BUSINESS_STEP, ...)`（`_budget_gate` 内部调用的正是这个）
    现在对同一个 verdict 给出一致结论。

    修复前，`group_handoff._target_budget_available` 自己手写了一套无视执行模式的
    硬拦判断；`_budget_gate` 走 `warn_only` 时会放行同一个超限 Agent——两者结论相反
    （见本文件旧版反例 6）。修复后两条链路都只是 `gate.check()` 的薄封装，判定逻辑
    与执行模式完全共享，因此在 `warn_only`（管理员显式选择只告警）与 `enforce`
    （默认口径）下都应该给出同一个 `allowed` 结论：
      - `warn_only`：两侧都放行（`allowed=True`，只告警不拦截）；
      - `enforce`：两侧都拦截（`allowed=False`）。
    这正是 design.md 2.10 "group_handoff 与直接对话链路复用同一套判定实现与同一份
    执行模式" 的可执行断言。
    """
    agent = _breached_agent()
    tenant = _tenant()
    counter = SimpleNamespace(tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW)

    service = model_step_service.RuntimeModelStepService(
        session_factory=lambda: None,
        context_builder=SimpleNamespace(build=None),
    )
    context = SimpleNamespace(
        tenant_id=str(TENANT_ID),
        agent_id=str(AGENT_ID),
        model_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
    )

    for mode in (MODE_WARN_ONLY, MODE_ENFORCE):

        # 同时钉死 mode 与 now：`_budget_gate`（business_step 侧）不传显式 `now`，
        # 会默认走 `datetime.now(UTC)`（真实挂钟时间）；这里显式把两侧都钉在同一个
        # `NOW`，否则两侧会因为"谁传了 now、谁没传"这个无关的差异而给出不同的周期
        # 翻页判定，掩盖了本反例真正要验证的东西（判定逻辑本身是否一致）。
        async def fake_evaluate(**kwargs):
            return await budget.evaluate(**{**kwargs, "mode": mode, "now": NOW})

        monkeypatch.setattr(gate, "evaluate", fake_evaluate)

        # group_handoff 侧：真实的 gate.check(lane=LANE_GROUP_HANDOFF, ...)，通过
        # BudgetSubjects 直接调用（不经过完整的 _validate_targets，因为那需要构造
        # 更多 Group/Session 相关的脚手架；直接调 gate.check 已经能证明"用同一个
        # verdict"这个核心事实，_validate_targets 只是把它包了一层 GroupAgentHandoffError）。
        handoff_verdict = await gate.check(
            lane=gate.LANE_GROUP_HANDOFF,
            subjects=BudgetSubjects(agent=agent, tenant=tenant, tenant_counter=counter),
            estimated_next_round_tokens=0,
            now=NOW,
        )
        handoff_available = handoff_verdict.allowed

        # business_step 侧：真实的 _budget_gate（内部就是 gate.check(lane=LANE_BUSINESS_STEP)）。
        business_step_result = await service._budget_gate(
            context, agent, (tenant, counter), estimated_next_round_tokens=0
        )
        business_step_available = business_step_result is None

        assert handoff_available == business_step_available, (
            f"反例 6 已修复：mode={mode} 下 group_handoff 与 business_step 必须给出"
            f"一致的结论（handoff_available={handoff_available}, "
            f"business_step_available={business_step_available}）"
        )


# ---------------------------------------------------------------------------
# 反例 7（边界，任务 3.1 已修复）：`agent=None` 只判 tenant_day，不抛异常
# EXPECTED OUTCOME：通过 —— 修复前抛 AttributeError，修复后返回只含 tenant_day 档的
# 正常 verdict。这是三条 system_scope 链路（`planning` / `group_compact` /
# `model_probe`）一旦接上闸门不再「fail-open 永远放行」的必要前提。
# ---------------------------------------------------------------------------


async def test_counterexample_7_evaluate_with_agent_none_only_checks_tenant_day() -> None:
    """`budget.evaluate(agent=None, ...)` 不再抛 `AttributeError`。

    根因（修复前）：`effective_timezone(None, tenant)` -> `get_agent_timezone_sync(None, tenant)`
    直接访问 `agent.timezone`（`timezone_utils.py`），对 `agent=None` 没有防护。
    任务 3.1 在 `budget.evaluate()` 内按 `agent is None` 跳过 agent 档与 `tz_agent`
    计算，只保留 `tenant_day` 一档（`periods.py` 不改动，记账侧共用的红线不碰）。
    """
    tenant = _tenant()
    counter = _breached_tenant_counter()  # 租户日上限（500,000）已击穿

    verdict = await budget.evaluate(
        agent=None,
        tenant=tenant,
        tenant_counter=counter,
        now=NOW,
        mode=MODE_ENFORCE,
    )

    assert verdict.blocked_scope == SCOPE_TENANT_DAY, (
        "修复后：agent=None 时依然能正确判定 tenant_day 一档，不会因为跳过 agent 档"
        "而漏判"
    )
    assert verdict.used == 500_000
    assert verdict.limit == 500_000
    assert verdict.allowed is False
    assert verdict.reset_at is not None
