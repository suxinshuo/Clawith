"""Task 7.2（bugfix：token-usage-limit-not-enforced）—— Property 3 域穷举测试。

**Property 3: Preservation** —— 群聊 handoff 在默认口径（`effective_mode = enforce`）
下的排除结果，必须与任务 2 记录的今天 `_target_budget_available` 基线一致，唯有两处
"有意变更"允许偏离（design.md「一处有意的行为变更」小节；bugfix.md 2.10 / 3.2 / 3.9）。

任务 7.1 已经把 `_target_budget_available` 拆成两半：
  - token 部分完全删除，改由 `gate.check(lane=LANE_GROUP_HANDOFF, ...)` 判定
    （内部就是 `budget.evaluate()`，与 `business_step` 等其它链路共用同一份实现）；
  - 非 token 部分（`max_tool_rounds` / `max_llm_calls_per_day`）原样保留在更名后的
    `_target_run_budget_available()` 里，语义逐条不变。

本文件直接驱动这两个函数本身（而不是端到端的 `preflight_group_agent_handoff`），
理由与 `test_token_budget_preservation_baseline.py` 一致：域穷举需要覆盖的组合数
不小（limit 3 档 × used 3 档 × 非 token 2 项各 2-3 档），每个组合都去构造完整的
Group/Session 脚手架成本过高；直接调用被测的核心判定函数（`gate.check()` +
`_target_run_budget_available()`）已经能验证"穷举判定逻辑本身"这个目标。文件末尾
补了一条驱动 `preflight_group_agent_handoff` 的交叉验证测试，用真实的 `_validate_targets`
入口复核"任一个不满足就拦截"的语义在收敛后仍然成立。

域的划分依据"token 部分与非 token 部分相互独立"这条原则（`_validate_targets` 对每个
目标依次调用两个独立的检查函数，任一为 False/allowed=False 就短路拦截）：

1. **token 部分域**（固定非 token 部分为"未耗尽"）：
   `limit ∈ {None, 0, 100_000}` × `used` 在阈值上下（`limit=None` 用大数验证始终
   放行；`limit=100_000` 用 `itertools.product` 穷举 under/at/over 三档；`limit=0`
   单独成一节，见下方"有意变更 1"）。
2. **非 token 部分域**（固定 token 部分为"未超限"）：
   `max_tool_rounds` 是否耗尽（`<=0` vs 正常正整数）；`max_llm_calls_per_day` 是否
   耗尽（`None`=未配置、`reset_at=None`、`reset_at` 落在今天、`reset_at` 落在今天
   之前"尚未重置"四个子场景，因为 `_target_run_budget_available` 对 `llm_calls_reset_at`
   有专门判断，不能只测"耗尽/未耗尽"两档）。
3. **交叉验证**（`itertools.product` 穷举 2x2）：token 部分与非 token 部分各自独立
   击穿/不击穿的全部组合，确认"任一个不满足就拦截"的语义未受影响。

两处有意变更（design.md「一处有意的行为变更」，bugfix.md 2.10）各自有专门的测试节，
断言里显式写明"这是有意变更，不是意外行为"，不让差异被断言语句悄悄吞掉。
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.agent_runtime.group_handoff import (
    GroupAgentHandoffError,
    _target_run_budget_available,
    preflight_group_agent_handoff,
)
from app.services.token_accounting import budget, gate
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    MODE_WARN_ONLY,
    SCOPE_AGENT_DAY,
    reset_enforcement_mode_cache,
)
from app.services.token_accounting.gate import LANE_GROUP_HANDOFF, BudgetSubjects

# 复用 test_agent_runtime_group_handoff.py 里已有的脚手架构造器，风格与
# test_token_budget_gate_lanes.py 复用姊妹模块构造器的做法一致 —— 只在交叉验证
# 那一节需要驱动完整的 `preflight_group_agent_handoff` 时才用得到。
from test_agent_runtime_group_handoff import (
    _DB as _handoff_db,
)
from test_agent_runtime_group_handoff import (
    _cycle_check,
)
from test_agent_runtime_group_handoff import (
    _records,
)
from test_agent_runtime_group_handoff import (
    _settings as _handoff_settings,
)
from test_agent_runtime_group_handoff import (
    _target,
)


NOW = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)
TENANT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
AGENT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

_POSITIVE_LIMIT = 100_000
_USED_FOR_POSITIVE_LIMIT = {
    "under": _POSITIVE_LIMIT - 1,
    "at": _POSITIVE_LIMIT,
    "over": _POSITIVE_LIMIT + 1,
}


@pytest.fixture(autouse=True)
def _reset_enforcement_mode_cache_between_tests():
    """避免用例间通过 30 秒 TTL 的进程内模式缓存互相污染（任务 3.3），与其它
    token 相关测试文件的做法一致。"""
    reset_enforcement_mode_cache()
    yield
    reset_enforcement_mode_cache()


def _agent(**overrides) -> SimpleNamespace:
    """默认「token 与非 token 检查均未耗尽」的目标 Agent。

    覆盖 `gate.check()`（经 `budget.evaluate()`）与 `_target_run_budget_available()`
    两侧都会读取的字段，使调用方只需覆盖本次要测的那一两个字段即可孤立出单一变量。
    """
    base = {
        "id": AGENT_ID,
        "tenant_id": TENANT_ID,
        "timezone": None,
        "max_tokens_per_day": None,
        "max_tokens_per_month": None,
        "tokens_used_today": 0,
        "tokens_used_month": 0,
        "last_daily_reset": NOW,
        "last_monthly_reset": NOW,
        "max_tool_rounds": 10,
        "max_llm_calls_per_day": None,
        "llm_calls_today": 0,
        "llm_calls_reset_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _tenant(**overrides) -> SimpleNamespace:
    """默认「未设租户日上限」的租户，使 token 部分域穷举只由 agent_day 一档决定。"""
    base = {"id": TENANT_ID, "timezone": "UTC", "max_tokens_per_day": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def _counter(**overrides) -> SimpleNamespace:
    base = {"tenant_id": TENANT_ID, "tokens_used_today": 0, "last_daily_reset": NOW}
    base.update(overrides)
    return SimpleNamespace(**base)


def _force_gate_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """强制 `gate.check()` 内部调用的 `evaluate()` 使用给定的 `mode`。

    `gate.check()` 不接受显式 `mode` 参数，缺省会走 `current_enforcement_mode()`
    读取 `system_settings`；测试环境没有真实数据库连接，读取会 fail-open 到
    `warn_only`，掩盖我们真正要验证的判定语义（`_breach` 本身 / 有意变更 1）。
    风格与 `test_agent_runtime_group_handoff.py::_forced_enforce`、
    `test_token_budget_gate_lanes.py` 里同名 helper 完全一致，只是额外支持传入
    `MODE_WARN_ONLY`（用于「有意变更 2」的显式模式选择场景）。
    """
    real_evaluate = budget.evaluate

    async def forced_evaluate(**kwargs):
        return await real_evaluate(**{**kwargs, "mode": mode})

    monkeypatch.setattr(gate, "evaluate", forced_evaluate)


async def _handoff_token_verdict(
    agent: SimpleNamespace,
    tenant: SimpleNamespace,
    counter: SimpleNamespace,
) -> gate.BudgetVerdict:
    return await gate.check(
        lane=LANE_GROUP_HANDOFF,
        subjects=BudgetSubjects(agent=agent, tenant=tenant, tenant_counter=counter),
        estimated_next_round_tokens=0,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# 1. token 部分域穷举（limit ∈ {None, 正数} × used 在阈值上下）
#
# `limit=0` 单独成一节（见下方「有意变更 1」），因为它的期望结果与今天的旧基线
# （`_target_budget_available` 已删除前的行为）不同，不能和 limit=None/正数 混在
# 同一张"结果应与基线一致"的表里。
# ---------------------------------------------------------------------------


TOKEN_DOMAIN_NONE_LIMIT_CASES = [
    ("limit=None,used=0", 0),
    ("limit=None,used=huge", 10**9),
]

TOKEN_DOMAIN_POSITIVE_LIMIT_CASES = [
    (f"limit=positive,used={used_name}", used) for used_name, used in _USED_FOR_POSITIVE_LIMIT.items()
]


@pytest.mark.parametrize("case_id,used", TOKEN_DOMAIN_NONE_LIMIT_CASES)
async def test_property3_token_domain_null_limit_always_allows(
    case_id, used, monkeypatch
) -> None:
    """`limit=None` 档：用量再大也必须放行——与任务 2 域点 1（3.1 NULL=无限制）的
    基线、以及旧 `_target_budget_available` 里 `agent.max_tokens_per_day and ...`
    真值判断对 `None` 的处理（同样放行）完全一致，这一档不属于两处有意变更。
    """
    _force_gate_mode(monkeypatch, MODE_ENFORCE)
    agent = _agent(max_tokens_per_day=None, tokens_used_today=used)
    tenant = _tenant()
    counter = _counter()

    verdict = await _handoff_token_verdict(agent, tenant, counter)

    assert verdict.allowed is True, case_id
    assert verdict.blocked_scope is None, case_id
    # 非 token 部分固定为「未耗尽」，必须独立地保持放行，不受 token 部分域点影响。
    assert _target_run_budget_available(agent, now=NOW) is True, case_id


@pytest.mark.parametrize("case_id,used", TOKEN_DOMAIN_POSITIVE_LIMIT_CASES)
async def test_property3_token_domain_positive_limit_matches_breach_semantics(
    case_id, used, monkeypatch
) -> None:
    """`limit=100_000` 档：`used` 穷举 under/at/over 三个阈值点。

    对正数 limit，旧 `_target_budget_available` 的真值判断（`agent.max_tokens_per_day
    and effective_used_day(agent) >= limit`）与新的 `_breach()` 语义（`used >= limit`）
    结果完全相同——正数不受"真值判断把 0 误当无上限"这个问题影响，所以这一档不属于
    两处有意变更之一，逐点断言必须与今天的基线一致：
      - under（used = limit - 1）：未命中，放行；
      - at（used == limit）：刚好命中，拦截；
      - over（used = limit + 1）：超额命中，拦截。
    """
    _force_gate_mode(monkeypatch, MODE_ENFORCE)
    agent = _agent(max_tokens_per_day=_POSITIVE_LIMIT, tokens_used_today=used)
    tenant = _tenant()
    counter = _counter()
    expect_allowed = used < _POSITIVE_LIMIT

    verdict = await _handoff_token_verdict(agent, tenant, counter)

    assert verdict.allowed is expect_allowed, case_id
    if not expect_allowed:
        assert verdict.blocked_scope == SCOPE_AGENT_DAY, case_id
        assert verdict.used == used, case_id
        assert verdict.limit == _POSITIVE_LIMIT, case_id
    assert _target_run_budget_available(agent, now=NOW) is True, case_id


# ---------------------------------------------------------------------------
# 有意变更 1（显式断言）：limit == 0 现在被拦截，不是放行
#
# 今天（收敛前）的旧实现 `agent.max_tokens_per_day and ...` 用真值判断，把 0 当作
# "无上限"从而放行；收敛后 token 部分唯一实现是 `gate.check()`（内部 `_breach()`），
# 它显式区分 None 与 0（0 参与阈值判断），因此 limit=0 现在总是命中——这与任务 2
# 记录的旧基线（`test_token_budget_preservation_baseline.py` 域点 2：
# "group_handoff._target_budget_available(limit=0, used=0) 今天返回 True（放行）"）
# 不同，是向 `_breach` 语义（3.2）对齐的有意修正，已在任务 7.1 落地（见该任务的
# 实现记录：函数删除与语义修正是同一个不可拆分的改动，无法拆开成"先收敛、再修正"
# 两步）。本测试系统性覆盖多个 `used` 取值，证明这个修正与 `used` 无关——不是巧合
# 命中了某个特定用量，而是 limit=0 本身的语义变化。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("used", [0, 1, 100_000, 10**9])
async def test_intentional_change_1_zero_limit_now_always_blocks(
    used, monkeypatch
) -> None:
    _force_gate_mode(monkeypatch, MODE_ENFORCE)
    agent = _agent(max_tokens_per_day=0, tokens_used_today=used)
    tenant = _tenant()
    counter = _counter()

    verdict = await _handoff_token_verdict(agent, tenant, counter)

    assert verdict.allowed is False, (
        f"【有意变更 1】used={used}：limit==0 现在被拦截，这与任务 2 记录的旧基线"
        "（收敛前 `_target_budget_available` 用真值判断把 0 误当作「无上限」从而"
        "放行）不同——这是向 `budget._breach` 语义（3.2：0 参与阈值判断，不与 None"
        "合并）对齐的有意修正，已在任务 7.1 落地，不是本测试意外触发的回归。"
    )
    assert verdict.blocked_scope == SCOPE_AGENT_DAY
    assert verdict.limit == 0
    # 非 token 部分不受这个变更影响，必须独立保持放行。
    assert _target_run_budget_available(agent, now=NOW) is True


# ---------------------------------------------------------------------------
# 2. 非 token 部分域穷举（固定 token 部分为「未超限」）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "max_tool_rounds,expect_available",
    [
        (10, True),
        (1, True),
        (0, False),
        (-1, False),
    ],
)
def test_property3_max_tool_rounds_domain(max_tool_rounds, expect_available) -> None:
    """`max_tool_rounds` 是否耗尽：`<= 0` 拦截，正整数放行。语义与拆分前逐字段
    不变（`_target_run_budget_available` 只是原样搬运了这段真值/类型/正负判断）。
    """
    agent = _agent(max_tool_rounds=max_tool_rounds)
    assert _target_run_budget_available(agent, now=NOW) is expect_available


@pytest.mark.parametrize(
    "max_llm_calls_per_day,llm_calls_today,llm_calls_reset_at,expect_available,note",
    [
        (None, 999, None, True, "未配置该档限额，用量再大也放行"),
        (10, 5, None, True, "未耗尽（用量低于上限）"),
        (10, 10, None, False, "耗尽且 reset_at 缺失——今天视为仍然耗尽"),
        (
            10,
            10,
            NOW,
            False,
            "耗尽且 reset_at 落在今天（当日已重置，计数是当下有效的）",
        ),
        (
            10,
            10,
            NOW - timedelta(days=1),
            True,
            "耗尽但 reset_at 落在今天之前（尚未重置，计数视为陈旧，放行）",
        ),
    ],
)
def test_property3_max_llm_calls_per_day_domain(
    max_llm_calls_per_day, llm_calls_today, llm_calls_reset_at, expect_available, note
) -> None:
    """`max_llm_calls_per_day` 是否耗尽，含 `llm_calls_reset_at` 的三种形状
    （缺失 / 落在今天 / 落在今天之前）——`_target_run_budget_available` 对这个
    字段有专门判断（`llm_calls_reset_at is None or llm_calls_reset_at.date() ==
    now.date()` 才判定为"仍然耗尽"），域穷举必须覆盖全部三种形状，不能只测
    "耗尽/未耗尽"两档。
    """
    agent = _agent(
        max_llm_calls_per_day=max_llm_calls_per_day,
        llm_calls_today=llm_calls_today,
        llm_calls_reset_at=llm_calls_reset_at,
    )
    assert _target_run_budget_available(agent, now=NOW) is expect_available, note


# ---------------------------------------------------------------------------
# 有意变更 2（显式断言）：configured_mode=warn_only（管理员显式选择）或 grace 窗口
# 内，收敛后跟随模式放行——不再无视模式硬拦。
#
# 今天（收敛前）的旧实现完全不读执行模式，无论 `warn_only` 还是 grace 都照样硬拦；
# 收敛后 token 部分唯一实现是 `gate.check()`，天然跟随 `effective_mode`。理由
# （design.md 明确写出）：子 Run 自己的 `gate.check(lane=business_step, ...)` 在
# `warn_only`/grace 下同样会放行，如果 `group_handoff` 预先按老口径拦截，就会造出
# "群聊里不可用、直接对话里可用"这个矛盾的镜像版本——这正是本次修复要消除的口径
# 矛盾（bugfix.md 1.10 / 2.10），不能让它以相反的方向重新出现。
# ---------------------------------------------------------------------------


async def test_intentional_change_2_explicit_warn_only_mode_now_follows_mode(
    monkeypatch,
) -> None:
    """管理员显式选择 `configured_mode=warn_only`：超限目标现在放行。"""
    _force_gate_mode(monkeypatch, MODE_WARN_ONLY)
    agent = _agent(max_tokens_per_day=100_000, tokens_used_today=200_000)  # 超限
    tenant = _tenant()
    counter = _counter()

    verdict = await _handoff_token_verdict(agent, tenant, counter)

    assert verdict.allowed is True, (
        "【有意变更 2】configured_mode=warn_only 时，收敛后的 group_handoff 判定"
        "跟随模式放行——今天（收敛前）的旧实现会无视执行模式硬拦，即使管理员已经"
        "显式选择只告警。跟随模式放行是有意的：否则会造出「群聊里不可用、直接对话"
        "里可用」这个矛盾的镜像版本，因为子 Run 自己的 business_step 闸门在同一个"
        "warn_only 模式下同样会放行同一个超限 Agent。"
    )
    assert verdict.blocked_scope == SCOPE_AGENT_DAY  # 判定本身仍然识别出命中了哪一档
    assert verdict.mode == MODE_WARN_ONLY
    # 非 token 部分与执行模式无关，必须继续正常参与判定。
    assert _target_run_budget_available(agent, now=NOW) is True


async def test_intentional_change_2_grace_window_now_follows_mode(
    monkeypatch,
) -> None:
    """grace 窗口内（`configured_mode=enforce` 但 `grace_until` 仍在未来）：
    超限目标同样放行。

    与上一条测试的区别：这里不强制 `gate.evaluate()` 的 `mode`，而是让判定走真实
    的 `current_enforcement_mode()` / `current_enforcement_state()` 路径（任务 3.4
    的 grace 语义），更贴近"grace 窗口内"这个措辞本身——`configured_mode` 确实是
    `enforce`，只是 `effective_mode` 因为 grace 生效而暂时是 `warn_only`。
    """
    reset_enforcement_mode_cache()
    # `gate.check()` doesn't pass an explicit `now` through to
    # `current_enforcement_mode()` -> `current_enforcement_state()` (that `now` argument
    # is separate from the `now` used for the token-budget period math below); grace
    # activation is judged against the real wall clock, so `grace_until` must be relative
    # to `datetime.now(UTC)`, not the fixed `NOW` constant used for period math elsewhere
    # in this file (`NOW` is a date in the past relative to the real wall clock).
    grace_until = datetime.now(UTC) + timedelta(days=1)

    async def fake_get_value(key, default=None):
        del key
        return {"mode": MODE_ENFORCE, "grace_until": grace_until.isoformat()}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    agent = _agent(max_tokens_per_day=100_000, tokens_used_today=200_000)  # 超限
    tenant = _tenant()
    counter = _counter()

    verdict = await _handoff_token_verdict(agent, tenant, counter)

    assert verdict.mode == MODE_WARN_ONLY, (
        "grace 生效时 effective_mode 恒为 warn_only，即使 configured_mode 是 enforce"
    )
    assert verdict.allowed is True, (
        "【有意变更 2（grace 窗口版本）】configured_mode=enforce 但仍处于 grace_until"
        "之前时，effective_mode 恒为 warn_only，group_handoff 判定同样跟随这个有效"
        "模式放行——理由与「管理员显式选择 warn_only」的场景一致（避免群聊/直接对话"
        "口径互相矛盾）。"
    )
    assert _target_run_budget_available(agent, now=NOW) is True


# ---------------------------------------------------------------------------
# 3. 交叉验证：token 部分与非 token 部分任一命中都会拦截（2x2 网格）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token_breached,non_token_exhausted",
    list(itertools.product([False, True], [False, True])),
)
async def test_property3_any_single_failure_blocks_the_target(
    token_breached, non_token_exhausted, monkeypatch
) -> None:
    """2x2 网格覆盖 token 部分与非 token 部分独立击穿/未击穿的全部组合：只有两者都
    未击穿时才放行，任一击穿（或两者都击穿）都拦截——与拆分前单函数内先做非 token
    检查、再做 token 检查，一旦任一为 False 就直接返回不可用的既有语义完全一致
    （只是判定逻辑现在分散在两个独立的函数里，调用方 `_validate_targets` 依次调用
    两者）。
    """
    _force_gate_mode(monkeypatch, MODE_ENFORCE)
    agent = _agent(
        max_tokens_per_day=100_000,
        tokens_used_today=200_000 if token_breached else 0,
        max_tool_rounds=0 if non_token_exhausted else 10,
    )
    tenant = _tenant()
    counter = _counter()

    non_token_available = _target_run_budget_available(agent, now=NOW)
    assert non_token_available is (not non_token_exhausted)

    token_verdict = await _handoff_token_verdict(agent, tenant, counter)
    assert token_verdict.allowed is (not token_breached)

    overall_available = non_token_available and token_verdict.allowed
    expect_available = not (token_breached or non_token_exhausted)
    assert overall_available is expect_available, (
        f"token_breached={token_breached}, non_token_exhausted={non_token_exhausted}："
        "任一个检查失败都必须导致整体不可用（拦截），不需要两者同时失败才拦截"
    )


async def test_property3_cross_validation_via_preflight_when_both_checks_fail(
    monkeypatch,
) -> None:
    """在 `_validate_targets` 的真实入口 `preflight_group_agent_handoff` 上复核交叉
    验证结论：token 部分超限 **且** 非 token 部分耗尽（`max_tool_rounds=0`）同时命中
    单个目标时，`_validate_targets` 里先执行的非 token 检查会短路拦截，`gate.check()`
    （token 部分）根本不会被调用——这与拆分前单函数内先做非 token 检查、再做 token
    检查的既有顺序完全一致（`_validate_targets` 循环体：先调
    `_target_run_budget_available`，为 False 就直接抛错，不会往下走到 `gate_check`）。
    这条测试驱动真实的端到端入口，为上面 2x2 网格里"两者都为 True"的那个组合点
    补一层集成级别的确认。
    """
    source_run, scope, context, state = _records()
    both_failing_target = _target(
        tenant_id=source_run.tenant_id,
        max_tokens_per_day=100_000,
        tokens_used_today=200_000,
        last_daily_reset=NOW,
        max_tool_rounds=0,  # 非 token 部分也耗尽
    )
    ensure = AsyncMock(return_value=_cycle_check())
    # 不给 side_effect，只用来确认它从未被 await——证明非 token 检查确实先短路，
    # token 部分的 gate.check() 根本没有机会执行。
    gate_check_spy = AsyncMock()

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
            new=AsyncMock(return_value=(both_failing_target,)),
        ),
        patch(
            "app.services.agent_runtime.group_handoff.AgentCycleGuard.ensure_delegation_allowed",
            new=ensure,
        ),
        patch(
            "app.services.agent_runtime.group_handoff.gate_check",
            new=gate_check_spy,
        ),
    ):
        with pytest.raises(GroupAgentHandoffError) as raised:
            await preflight_group_agent_handoff(
                _handoff_db(),  # type: ignore[arg-type]
                state=state,
                context=context,
                content="Continue",
                mention_participant_ids=(str(both_failing_target.participant_id),),
                settings=_handoff_settings(),
                clock=lambda: NOW,
            )

    assert raised.value.code == "group_handoff_budget_unavailable"
    assert raised.value.repairable is True
    assert ensure.await_count == 0
    gate_check_spy.assert_not_awaited()
