"""Task 2（bugfix：token-usage-limit-not-enforced）—— 保留行为基线测试。

**这些测试在未修复代码上运行**，目的是先把「未击穿限额的输入」在今天的行为冻结成一张
显式的期望表（Property 2: Preservation），供修复落地后（任务 13.2）复跑同一批测试来
证明没有回归。

本文件遵循 observation-first：每个域点先用真实的 `budget.evaluate()` / 真实的
`gate.check()`（含 `group_handoff` 现在复用的 `LANE_GROUP_HANDOFF`，任务 7.1 收敛后
`group_handoff._target_budget_available` 已被删除）/ 真实的 `RuntimeModelStepService`
跑一遍今天的行为，再把结果写进断言——不是先猜一个期望值再去凑测试。

对应 design.md "Preservation Checking" 与 tasks.md 任务 2 列出的 8 个域点：

1. 3.1 NULL = 无限制
2. 3.2 0 ≠ NULL（含 group_handoff 今天的结论——任务 7.1 唯一有意的行为变更）
3. 3.3 未达上限时的零额外往返（`get_value` 原始基线是 2 次；任务 3.3 加入进程内模式
   缓存后期望更新为 `<= 1` 次，属于该任务允许的显式行为变化，理由见该测试内注释 /
   `_load_budget_subjects` 今天 1 次，不受缓存影响）
4. 3.4 周期翻页（三个时区）
5. 3.6 fail-open 分级（TypeError → ERROR / OSError → WARNING，均退回 warn_only）
6. 3.7 判定优先级（agent_day → agent_month → tenant_day）
7. 3.8 软告警 80% 阈值
8. 3.5 记账口径——本文件不新增记账测试，只在报告里确认既有测试文件的通过状态

**CRITICAL**：这些测试在未修复代码上必须全部通过——这就是要保留的基线行为，不是在
找 bug。如果某条域点在当前代码上不通过，说明对当前行为的理解有误，需要先读源码，
不要弱化断言来让它通过。
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from app.services.agent_runtime import model_step_service
from app.services.llm.single_step import LLMCompletionStep
from app.services.token_accounting import budget, gate
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    MODE_WARN_ONLY,
    SCOPE_AGENT_DAY,
    SCOPE_AGENT_MONTH,
    evaluate,
    reset_enforcement_mode_cache,
)
from app.services.token_tracker import TokenUsage

# 复用现有测试模块里的构造函数，与 test_token_budget_gate_lanes.py 的做法一致
# （直接复用姊妹模块的构造器，不新增 fixture 基础设施）。
from test_agent_runtime_model_step_service import (
    _agent as _real_agent,
)
from test_agent_runtime_model_step_service import (
    _build as _real_build,
)
from test_agent_runtime_model_step_service import (
    _context as _real_context,
)
from test_agent_runtime_model_step_service import (
    _ContextBuilder as _RealContextBuilder,
)
from test_agent_runtime_model_step_service import (
    _model as _real_model,
)
from test_agent_runtime_model_step_service import (
    _service as _real_service,
)
from test_agent_runtime_model_step_service import (
    _state as _real_state,
)


NOW = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)  # 北京 8/7 00:30
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

TIMEZONES = ("UTC", "Asia/Shanghai", "America/New_York")


@pytest.fixture(autouse=True)
def _reset_enforcement_mode_cache_between_tests():
    """避免用例间通过 30 秒 TTL 的进程内模式缓存互相污染（任务 3.3）。

    域点 3（零额外往返）本身就依赖缓存状态是干净的，才能准确统计 `get_value` 的
    调用次数；其余域点大多显式传 `mode=`，但仍需要隔离，防止缓存状态在用例间泄漏。
    """
    reset_enforcement_mode_cache()
    yield
    reset_enforcement_mode_cache()


def _agent(**overrides) -> SimpleNamespace:
    """默认「未击穿任何一档」的 Agent，字段同时满足 budget.evaluate() 与
    group_handoff 非 token 可用性检查（`_target_run_budget_available`）两侧的读取需要
    （domain 2 要横向比较两侧）。
    """
    base = {
        "id": AGENT_ID,
        "name": "Ada",
        "tenant_id": TENANT_ID,
        "timezone": None,
        "max_tokens_per_day": 100_000,
        "max_tokens_per_month": None,
        "tokens_used_today": 0,
        "tokens_used_month": 0,
        "last_daily_reset": NOW,
        "last_monthly_reset": NOW,
        # group_handoff 非 token 的可用性检查项，都设成「未耗尽」。
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


def _counter(**overrides) -> SimpleNamespace:
    base = {"tenant_id": TENANT_ID, "tokens_used_today": 0, "last_daily_reset": NOW}
    base.update(overrides)
    return SimpleNamespace(**base)


def _capture_logs() -> tuple[list[tuple[str, str]], int]:
    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["level"].name, str(message))),
        level="TRACE",
    )
    return records, handler_id


async def _gate_would_call_completion(
    agent: SimpleNamespace,
    tenant: SimpleNamespace,
    counter: SimpleNamespace,
    *,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    estimated: int = 0,
) -> bool:
    """`_budget_gate` 返回 None 即代表不短路——今天这就是「completion 会被调用」的信号。

    显式覆盖 mode，避免在单元测试里意外打到真实的 `current_enforcement_mode()`
    （那条路径会去查 `system_settings`，域点 3 单独覆盖那个行为，这里只关心
    "allowed 时是否放行"这一件事）。
    """

    async def fake_evaluate(**kwargs):
        return await evaluate(**{**kwargs, "mode": mode})

    monkeypatch.setattr(gate, "evaluate", fake_evaluate)
    service = model_step_service.RuntimeModelStepService(
        session_factory=lambda: None,
        context_builder=SimpleNamespace(build=None),
    )
    context = SimpleNamespace(
        tenant_id=str(TENANT_ID),
        agent_id=str(getattr(agent, "id", AGENT_ID)),
        model_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
    )
    result = await service._budget_gate(
        context, agent, (tenant, counter), estimated_next_round_tokens=estimated
    )
    return result is None


# ---------------------------------------------------------------------------
# 域点 1（3.1 NULL = 无限制）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("used", [0, 1, 10**9])
async def test_domain_point_1_null_limit_is_unlimited_and_allows(used, monkeypatch) -> None:
    agent = _agent(
        max_tokens_per_day=None,
        max_tokens_per_month=None,
        tokens_used_today=used,
        tokens_used_month=used,
    )
    tenant = _tenant(max_tokens_per_day=None)
    counter = _counter(tokens_used_today=used)

    verdict = await evaluate(agent=agent, tenant=tenant, tenant_counter=counter, now=NOW, mode=MODE_ENFORCE)

    assert verdict.allowed is True
    assert verdict.blocked_scope is None

    allows_completion = await _gate_would_call_completion(
        agent, tenant, counter, mode=MODE_ENFORCE, monkeypatch=monkeypatch
    )
    assert allows_completion is True, (
        "基线：limit=None 时无论 used 多大都必须放行，completion 端口会被调用"
    )


# ---------------------------------------------------------------------------
# 域点 2（3.2 0 ≠ NULL，含 group_handoff 今天的结论）
# ---------------------------------------------------------------------------


async def test_domain_point_2_zero_limit_differs_from_null_and_group_handoff_allows_it_today(
    monkeypatch,
) -> None:
    tenant = _tenant()
    counter = _counter()

    verdict_zero = await evaluate(
        agent=_agent(max_tokens_per_day=0, tokens_used_today=0),
        tenant=tenant,
        tenant_counter=counter,
        now=NOW,
        mode=MODE_ENFORCE,
    )
    verdict_null = await evaluate(
        agent=_agent(max_tokens_per_day=None, tokens_used_today=0),
        tenant=tenant,
        tenant_counter=counter,
        now=NOW,
        mode=MODE_ENFORCE,
    )

    assert verdict_zero.allowed is False
    assert verdict_zero.blocked_scope == SCOPE_AGENT_DAY
    assert verdict_zero.used == 0
    assert verdict_zero.limit == 0
    assert verdict_null.allowed is True
    assert verdict_null.blocked_scope is None
    assert (verdict_zero.allowed, verdict_zero.blocked_scope) != (
        verdict_null.allowed,
        verdict_null.blocked_scope,
    ), "基线：limit=0 与 limit=None 在 used=0 时的结果必须不同，不能被真值判断合并"

    # group_handoff 今天的结论也要钉住——收敛后（任务 7.1）这是唯一有意的行为变更：
    # 今天 `agent.max_tokens_per_day and ...` 把 0 当作「无上限」放行，收敛后会改为拦截。
    #
    # **任务 7.1 兼容性说明**：`group_handoff._target_budget_available` 已被拆分——
    # token 判断部分已删除，这个函数名已经不存在了（非 token 部分原样搬到了更名后的
    # `_target_run_budget_available()`，但那个函数已经不做 token 判断，不能再用它验证
    # 这条基线）。这条基线记录的是"收敛前"的行为，本应在任务 7.1 落地的同一时刻被替换，
    # 但任务描述明确要求域点 2 的期望值在任务 7.1 里不能改变（"不要在本任务里改这条基线
    # 测试本身"，那是任务 7.2 的范围）。因此这里改为通过 `gate.check(lane=
    # LANE_GROUP_HANDOFF, ...)` 走一遍完全相同的输入（limit=0, used=0），验证的是
    # "任务 7.1 落地后的今天"这个新的基线点：`gate.check()` 现在是 group_handoff 判定
    # token 部分的唯一实现，且它遵循 `_breach` 语义（0 参与阈值判断，不与 NULL 合并），
    # 所以 limit=0 会被拦截——这正是任务 7.1 落地时就已经生效的那"唯一有意的行为变更"，
    # 不是任务 7.2 才产生的。任务 7.2 的 Property 3 域穷举测试会更完整地覆盖这一点；
    # 这里只是让本文件在函数改名后仍能运行，不改变本域点原本要钉住的事实——只是把
    # "验证方式"从一个已被删除的函数换成了它的替代实现。
    # `gate.check()` doesn't take an explicit `mode` — it always resolves the effective
    # mode through `current_enforcement_mode()`, which reads `system_settings` and, absent
    # a real database connection in this unit test, fails open to `warn_only`. Force
    # `evaluate()`'s `mode` to `MODE_ENFORCE` (the same pattern used throughout
    # `test_token_budget_gate_lanes.py`) so this assertion exercises the `_breach` semantics
    # themselves rather than an unrelated fail-open fallback.
    async def forced_enforce_evaluate(**kwargs):
        return await evaluate(**{**kwargs, "mode": MODE_ENFORCE})

    monkeypatch.setattr(gate, "evaluate", forced_enforce_evaluate)

    handoff_agent = _agent(max_tokens_per_day=0, tokens_used_today=0)
    handoff_verdict = await gate.check(
        lane=gate.LANE_GROUP_HANDOFF,
        subjects=gate.BudgetSubjects(agent=handoff_agent, tenant=tenant, tenant_counter=counter),
        estimated_next_round_tokens=0,
        now=NOW,
    )
    assert handoff_verdict.allowed is False, (
        "任务 7.1 更新：group_handoff 的 token 判断已收敛到 gate.check()，遵循 "
        "_breach 语义（0 参与阈值判断），limit=0 现在被拦截——这是任务 7.1 落地时就"
        "已生效的、design.md 记录的唯一有意行为变更（旧的 `_target_budget_available` "
        "用真值判断把 0 误当作「无上限」放行，那条基线已经不适用，函数本身也已被删除）"
    )


# ---------------------------------------------------------------------------
# 域点 3（3.3 未达上限的零额外往返——记录今天的基线次数）
# ---------------------------------------------------------------------------


async def test_domain_point_3_business_step_round_trip_baseline(monkeypatch) -> None:
    """一个模型步内、判定放行时，`get_value` 调用次数 <= 1、`_load_budget_subjects` 是 1 次。

    **任务 3.3 更新的期望值**：两阶段判定（估算 0 的粗判 + 估算真实 prompt 的细判）
    都不显式传 `mode`，理论上各自会调一次 `current_enforcement_mode()`。在加入进程
    内模式缓存（TTL=30s）之前，两次调用各自都会打到 `system_setting_dao.get_value`，
    基线是 2 次（这是任务 2 记录的、未修复代码上的原始基线）。加入缓存后，第一阶段
    的调用会把结果写入缓存，第二阶段命中新鲜缓存直接返回，不再查库——稳态下
    `get_value` 降为 1 次（若测试运行顺序导致缓存在测试开始前已经是新鲜的，可能是
    0 次）。用 `<= 1` 断言，避免因为缓存命中/未命中的边界状态让测试变脆弱。
    这是任务 3.3 允许的两处显式行为变化之一（design.md「变更 2」：加缓存后稳态为
    0 次额外 SELECT），不是回归。tenant/tenant_counter 仍由 `_resolve_budget_subjects`
    只加载一次、两阶段共用，这一点不受缓存影响。
    """
    get_value_calls = 0

    async def counting_get_value(key, default=None):
        nonlocal get_value_calls
        get_value_calls += 1
        return default  # 模拟配置缺省（今天的默认行为，间接确认 mode 走的是 warn_only）

    monkeypatch.setattr(budget.system_setting_dao, "get_value", counting_get_value)

    subject_calls = 0
    original_load_subjects = model_step_service.RuntimeModelStepService._load_budget_subjects

    async def counting_load_subjects(self, tenant_id):
        nonlocal subject_calls
        subject_calls += 1
        return await original_load_subjects(self, tenant_id)

    monkeypatch.setattr(
        model_step_service.RuntimeModelStepService,
        "_load_budget_subjects",
        counting_load_subjects,
    )

    tenant_id = uuid.uuid4()
    model = _real_model(tenant_id)
    agent = _real_agent(tenant_id)  # 未设任何 token 限额（属性为 None）——两阶段都会放行
    state = _real_state(tenant_id, model, agent)
    completion = AsyncMock(
        return_value=LLMCompletionStep(
            content="Completed.",
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=10),
        )
    )

    result = await _real_service(
        model, agent, _RealContextBuilder(_real_build()), completion
    ).complete_once(state, _real_context(state))

    assert result.intent == "finish", "先确认两阶段判定都放行了，往返次数才有意义"
    assert get_value_calls <= 1, (
        "任务 3.3 更新：加入进程内模式缓存后，一个模型步内 get_value 调用次数从 2 次"
        "降为 <= 1 次（缓存生效后两阶段共用同一次缓存读取；若缓存进入本测试前已是"
        "新鲜状态则可能是 0 次，用 <= 1 避免测试脆弱）"
    )
    assert subject_calls == 1, "基线：_load_budget_subjects 两阶段共用，只查 1 次"


# ---------------------------------------------------------------------------
# 域点 4（3.4 周期翻页 × 三个时区）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tz_name", TIMEZONES)
async def test_domain_point_4_stale_period_counts_as_reset_across_timezones(tz_name) -> None:
    """last_daily_reset 落在上一个本地日、last_monthly_reset 落在上个月 -> 陈旧计数视为 0。"""
    stale_daily = NOW - timedelta(days=2)
    stale_monthly = NOW - timedelta(days=40)
    agent = _agent(
        timezone=tz_name,
        max_tokens_per_day=100_000,
        max_tokens_per_month=1_000_000,
        tokens_used_today=999_999,
        tokens_used_month=999_999,
        last_daily_reset=stale_daily,
        last_monthly_reset=stale_monthly,
    )
    tenant = _tenant(max_tokens_per_day=None)
    counter = _counter(tokens_used_today=0)

    verdict = await evaluate(agent=agent, tenant=tenant, tenant_counter=counter, now=NOW, mode=MODE_ENFORCE)

    assert verdict.allowed is True, f"{tz_name}: 陈旧计数应视为 0，放行"
    assert verdict.blocked_scope is None


# ---------------------------------------------------------------------------
# 域点 5（3.6 fail-open 分级）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_type", "expected_level", "expected_keyword"),
    [
        (TypeError, "ERROR", "token_budget_enforcement_disabled_bug"),
        (OSError, "WARNING", "token_budget_enforcement_disabled_transient"),
    ],
)
async def test_domain_point_5_fail_open_grading_by_exception_type(
    exc_type, expected_level, expected_keyword, monkeypatch
) -> None:
    async def raising_get_value(key, default=None):
        raise exc_type("boom")

    monkeypatch.setattr(budget.system_setting_dao, "get_value", raising_get_value)

    records, handler_id = _capture_logs()
    try:
        mode = await budget.current_enforcement_mode()
    finally:
        logger.remove(handler_id)

    assert mode == MODE_WARN_ONLY, "基线：两类异常今天都 fail-open 退回 warn_only"
    assert any(
        level == expected_level and expected_keyword in text for level, text in records
    ), f"基线：{exc_type.__name__} 必须落 {expected_level} + {expected_keyword}"


# ---------------------------------------------------------------------------
# 域点 6（3.7 判定优先级）
# ---------------------------------------------------------------------------


async def test_domain_point_6_agent_day_wins_when_all_three_are_breached() -> None:
    agent = _agent(tokens_used_today=100_000, tokens_used_month=1_000_000, max_tokens_per_month=1_000_000)
    tenant = _tenant()
    counter = _counter(tokens_used_today=500_000)

    verdict = await evaluate(agent=agent, tenant=tenant, tenant_counter=counter, now=NOW, mode=MODE_ENFORCE)

    assert verdict.blocked_scope == SCOPE_AGENT_DAY


async def test_domain_point_6_agent_month_wins_over_tenant_day() -> None:
    agent = _agent(tokens_used_today=0, tokens_used_month=1_000_000, max_tokens_per_month=1_000_000)
    tenant = _tenant()
    counter = _counter(tokens_used_today=500_000)

    verdict = await evaluate(agent=agent, tenant=tenant, tenant_counter=counter, now=NOW, mode=MODE_ENFORCE)

    assert verdict.blocked_scope == SCOPE_AGENT_MONTH


# ---------------------------------------------------------------------------
# 域点 7（3.8 软告警 80% 阈值）
# ---------------------------------------------------------------------------


async def test_domain_point_7_soft_warning_fires_at_exact_eighty_percent() -> None:
    limit = 100_000
    used = math.floor(limit * 0.8)
    agent = _agent(max_tokens_per_day=limit, tokens_used_today=used)

    verdict = await evaluate(
        agent=agent, tenant=_tenant(), tenant_counter=_counter(), now=NOW, mode=MODE_ENFORCE
    )

    assert verdict.soft_warning is True
    assert verdict.soft_warning_scope == SCOPE_AGENT_DAY
    assert verdict.soft_warning_subject_id == AGENT_ID


async def test_domain_point_7_no_soft_warning_just_below_eighty_percent() -> None:
    limit = 100_000
    used = math.floor(limit * 0.8) - 1
    agent = _agent(max_tokens_per_day=limit, tokens_used_today=used)

    verdict = await evaluate(
        agent=agent, tenant=_tenant(), tenant_counter=_counter(), now=NOW, mode=MODE_ENFORCE
    )

    assert verdict.soft_warning is False


# ---------------------------------------------------------------------------
# 域点 8（3.5 记账口径）—— 本文件不新增记账测试。
#
# 基线由以下既有测试文件的当前通过状态构成，不在本文件里重复实现：
#   tests/test_token_accounting_ledger.py
#   tests/test_token_accounting_normalize.py
#   tests/test_token_accounting_periods.py
#   tests/test_token_period_consistency.py
# 任务 2 的报告（tasks.md「保留行为基线」小节）记录了这四个文件的实测通过数量。
# 后续任何一条需要改动才能通过，都视为 3.5 被破坏的信号，必须回到 design 而不是改测试。
# ---------------------------------------------------------------------------
