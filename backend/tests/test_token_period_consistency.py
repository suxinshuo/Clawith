"""周期判定收敛为单一实现，新建 Agent 继承租户默认限额。

`agents.py::_lazy_reset_token_counters` 与 `group_handoff.py::_target_budget_available`
曾各自手写一套"计数器是否已跨周期"的判断，且都按 UTC `.date()` 比较 —— 与
`token_accounting.periods` 里按租户时区判定的 `is_new_local_day` / `is_new_local_month`
不一致。两套定义迟早分叉：UTC 与租户时区的日期分界点本就不同，跨越 UTC 零点未必跨越
Asia/Shanghai (UTC+8) 零点，反之亦然。

这里直接调用被测函数、构造能让"按 UTC 比较"与"按租户时区比较"给出不同答案的具体
时间点，断言结果，而不是检查源码文本里是否出现某个函数名 —— 后者只要函数名出现在
任意位置就会通过，无法发现变量搞反、边界写反等真实回归。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from app.api import agents as agents_api
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.schemas import AgentCreate
from app.services.agent_runtime import group_handoff
from app.services.token_accounting.periods import is_new_local_day, is_new_local_month


class DummyResult:
    def __init__(self, values=()):
        self._values = list(values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class RecordingDB:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.added: list[object] = []
        self.executed: list[object] = []
        self.commit_count = 0

    async def execute(self, statement, params=None):
        self.executed.append(statement)
        if self.responses:
            return self.responses.pop(0)
        return DummyResult()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.commit_count += 1


def make_user(**overrides) -> User:
    values = {
        "id": uuid.uuid4(),
        "username": "alice",
        "email": "alice@example.com",
        "password_hash": "hashed",
        "display_name": "Alice",
        "role": "org_admin",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    return User(**values)


def make_agent(**overrides) -> Agent:
    values = {
        "id": uuid.uuid4(),
        "name": "Ops Bot",
        "role_description": "assistant",
        "creator_id": uuid.uuid4(),
        "status": "idle",
        "agent_type": "native",
        "max_tool_rounds": 50,
    }
    values.update(overrides)
    return Agent(**values)


def make_tenant(**overrides) -> Tenant:
    values = {
        "id": uuid.uuid4(),
        "name": "Acme",
        "slug": f"acme-{uuid.uuid4().hex[:8]}",
    }
    values.update(overrides)
    return Tenant(**values)


def _freeze_agents_module_now(monkeypatch, when: datetime) -> None:
    """Pin `datetime.now(...)` inside `app.api.agents` to a fixed instant.

    `_lazy_reset_token_counters` calls `datetime.now(timezone.utc)` directly rather than
    accepting an injected `now`, so the only way to control "now" from a test is to swap
    the `datetime` class the module resolves at call time.
    """
    frozen = type("FrozenDatetime", (datetime,), {"now": classmethod(lambda cls, tz=None: when)})
    monkeypatch.setattr(agents_api, "datetime", frozen)


# ---------------------------------------------------------------------------
# _lazy_reset_token_counters (backend/app/api/agents.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_reset_does_not_fire_across_a_utc_midnight_that_stays_within_the_tenant_local_day(
    monkeypatch,
):
    """Crossing UTC midnight is not always crossing the tenant's local midnight.

    A naive `last_daily_reset.date() < now.date()` comparison fires here (Aug 7 -> Aug 8 in
    UTC), but Asia/Shanghai (UTC+8, no DST) is still on Aug 8 for both timestamps. The reset
    must not fire.
    """
    fixed_now = datetime(2026, 8, 8, 1, 0, 0, tzinfo=UTC)  # Shanghai: Aug 8, 09:00
    last_daily_reset = datetime(2026, 8, 7, 20, 0, 0, tzinfo=UTC)  # Shanghai: Aug 8, 04:00
    assert last_daily_reset.date() < fixed_now.date(), "naive UTC compare would (wrongly) fire a reset here"
    assert is_new_local_day(last_daily_reset, "Asia/Shanghai", now=fixed_now) is False

    tenant = make_tenant(timezone="Asia/Shanghai")
    agent = make_agent(
        tenant_id=tenant.id,
        timezone=None,
        tokens_used_today=42,
        last_daily_reset=last_daily_reset,
        last_monthly_reset=fixed_now,  # same tenant-local month: isolates the day check
    )
    db = RecordingDB(responses=[DummyResult([tenant])])
    _freeze_agents_module_now(monkeypatch, fixed_now)

    changed = await agents_api._lazy_reset_token_counters(agent, db)

    assert changed is False
    assert agent.tokens_used_today == 42
    assert agent.last_daily_reset == last_daily_reset


@pytest.mark.asyncio
async def test_lazy_reset_fires_on_a_tenant_local_day_rollover_that_utc_date_alone_would_miss(
    monkeypatch,
):
    """The mirror bug: same UTC calendar date on both ends, but Shanghai already rolled over.

    A naive `last_daily_reset.date() == now.date()` comparison would treat this as "still
    today" and skip the reset, leaving a stale count visible after the tenant's local
    midnight has already passed.
    """
    fixed_now = datetime(2026, 8, 8, 20, 0, 0, tzinfo=UTC)  # Shanghai: Aug 9, 04:00
    last_daily_reset = datetime(2026, 8, 8, 1, 0, 0, tzinfo=UTC)  # Shanghai: Aug 8, 09:00
    assert last_daily_reset.date() == fixed_now.date(), "naive UTC compare would (wrongly) skip the reset here"
    assert is_new_local_day(last_daily_reset, "Asia/Shanghai", now=fixed_now) is True

    tenant = make_tenant(timezone="Asia/Shanghai")
    agent = make_agent(
        tenant_id=tenant.id,
        timezone=None,
        tokens_used_today=42,
        last_daily_reset=last_daily_reset,
        last_monthly_reset=fixed_now,
    )
    db = RecordingDB(responses=[DummyResult([tenant])])
    _freeze_agents_module_now(monkeypatch, fixed_now)

    changed = await agents_api._lazy_reset_token_counters(agent, db)

    assert changed is True
    assert agent.tokens_used_today == 0
    assert agent.last_daily_reset == fixed_now


@pytest.mark.asyncio
async def test_lazy_reset_zeroes_the_input_token_counters_too():
    """A rollover must also zero input_tokens_*.

    Task 10's cache-hit-rate denominator is `input_tokens_*` (input including cache), not
    `tokens_used_*`. If a rollover zeroes `tokens_used_today` but leaves `input_tokens_today`
    stale, the next period's hit-rate math mixes tokens from two different periods.
    """
    long_ago = datetime(2000, 1, 1, tzinfo=UTC)
    agent = make_agent(
        tenant_id=None,  # no tenant to load -> effective timezone falls back to UTC
        timezone=None,
        tokens_used_today=100,
        tokens_used_month=1000,
        input_tokens_today=99,
        input_tokens_month=888,
        cache_read_tokens_today=5,
        cache_creation_tokens_today=6,
        cache_read_tokens_month=55,
        cache_creation_tokens_month=66,
        last_daily_reset=long_ago,
        last_monthly_reset=long_ago,
    )
    db = RecordingDB()

    changed = await agents_api._lazy_reset_token_counters(agent, db)

    assert changed is True
    assert agent.tokens_used_today == 0
    assert agent.tokens_used_month == 0
    assert agent.input_tokens_today == 0
    assert agent.input_tokens_month == 0
    assert agent.cache_read_tokens_today == 0
    assert agent.cache_creation_tokens_today == 0
    assert agent.cache_read_tokens_month == 0
    assert agent.cache_creation_tokens_month == 0
    assert db.executed == [], "agent.tenant_id is None -> must not query Tenant"


# ---------------------------------------------------------------------------
# _target_budget_available (backend/app/services/agent_runtime/group_handoff.py)
# ---------------------------------------------------------------------------


def test_target_budget_blocked_when_daily_counter_has_not_rolled_over_in_tenant_local_day():
    """Over budget, and the tenant-local day has not turned over -> blocked.

    Crosses UTC midnight (old `.date()` compare would call this "rolled over" and
    wrongly report the budget as available) but Asia/Shanghai has not reached its own
    midnight yet.
    """
    now = datetime(2026, 8, 8, 0, 10, 0, tzinfo=UTC)  # Shanghai: Aug 8, 08:10
    last_daily_reset = datetime(2026, 8, 7, 23, 50, 0, tzinfo=UTC)  # Shanghai: Aug 8, 07:50
    assert last_daily_reset.date() != now.date(), "naive UTC compare would (wrongly) call this rolled over"
    assert is_new_local_day(last_daily_reset, "Asia/Shanghai", now=now) is False

    tenant = make_tenant(timezone="Asia/Shanghai")
    agent = make_agent(
        timezone=None,
        max_tool_rounds=10,
        max_tokens_per_day=100,
        tokens_used_today=150,
        last_daily_reset=last_daily_reset,
        max_tokens_per_month=None,
        max_llm_calls_per_day=1000,
        llm_calls_today=0,
    )

    assert group_handoff._target_budget_available(agent, now=now, tenant=tenant) is False


def test_target_budget_available_when_daily_counter_has_rolled_over_in_tenant_local_day():
    """Over budget, but the tenant-local day already turned over -> available.

    Same UTC calendar date on both ends (old `.date()` compare would call this "not rolled
    over" and wrongly keep the budget blocked) but Asia/Shanghai has already reached its
    own midnight.
    """
    now = datetime(2026, 8, 8, 16, 5, 0, tzinfo=UTC)  # Shanghai: Aug 9, 00:05
    last_daily_reset = datetime(2026, 8, 8, 0, 5, 0, tzinfo=UTC)  # Shanghai: Aug 8, 08:05
    assert last_daily_reset.date() == now.date(), "naive UTC compare would (wrongly) call this not rolled over"
    assert is_new_local_day(last_daily_reset, "Asia/Shanghai", now=now) is True

    tenant = make_tenant(timezone="Asia/Shanghai")
    agent = make_agent(
        timezone=None,
        max_tool_rounds=10,
        max_tokens_per_day=100,
        tokens_used_today=150,
        last_daily_reset=last_daily_reset,
        max_tokens_per_month=None,
        max_llm_calls_per_day=1000,
        llm_calls_today=0,
    )

    assert group_handoff._target_budget_available(agent, now=now, tenant=tenant) is True


def test_target_budget_blocked_when_monthly_counter_has_not_rolled_over_in_tenant_local_month():
    """Over budget, and the tenant-local month has not turned over -> blocked.

    Crosses the UTC month boundary (old `(year, month)` compare would call this "rolled
    over") but Asia/Shanghai is still in the same local month for both timestamps.
    """
    now = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)  # Shanghai: Aug 1, 18:00
    last_monthly_reset = datetime(2026, 7, 31, 20, 0, 0, tzinfo=UTC)  # Shanghai: Aug 1, 04:00
    assert (last_monthly_reset.year, last_monthly_reset.month) != (now.year, now.month), (
        "naive UTC compare would (wrongly) call this rolled over"
    )
    assert is_new_local_month(last_monthly_reset, "Asia/Shanghai", now=now) is False

    tenant = make_tenant(timezone="Asia/Shanghai")
    agent = make_agent(
        timezone=None,
        max_tool_rounds=10,
        max_tokens_per_day=None,
        max_tokens_per_month=100,
        tokens_used_month=150,
        last_monthly_reset=last_monthly_reset,
        max_llm_calls_per_day=1000,
        llm_calls_today=0,
    )

    assert group_handoff._target_budget_available(agent, now=now, tenant=tenant) is False


def test_target_budget_available_when_monthly_counter_has_rolled_over_in_tenant_local_month():
    """Over budget, but the tenant-local month already turned over -> available.

    Same UTC (year, month) on both ends (old compare would call this "not rolled over")
    but Asia/Shanghai has already reached the first of the next month.
    """
    now = datetime(2026, 8, 31, 20, 0, 0, tzinfo=UTC)  # Shanghai: Sep 1, 04:00
    last_monthly_reset = datetime(2026, 8, 31, 10, 0, 0, tzinfo=UTC)  # Shanghai: Aug 31, 18:00
    assert (last_monthly_reset.year, last_monthly_reset.month) == (now.year, now.month), (
        "naive UTC compare would (wrongly) call this not rolled over"
    )
    assert is_new_local_month(last_monthly_reset, "Asia/Shanghai", now=now) is True

    tenant = make_tenant(timezone="Asia/Shanghai")
    agent = make_agent(
        timezone=None,
        max_tool_rounds=10,
        max_tokens_per_day=None,
        max_tokens_per_month=100,
        tokens_used_month=150,
        last_monthly_reset=last_monthly_reset,
        max_llm_calls_per_day=1000,
        llm_calls_today=0,
    )

    assert group_handoff._target_budget_available(agent, now=now, tenant=tenant) is True


# ---------------------------------------------------------------------------
# create_agent inherits tenant default token limits (backend/app/api/agents.py)
# ---------------------------------------------------------------------------


def _patch_create_agent_collaborators(monkeypatch) -> None:
    """Stub every collaborator of create_agent that isn't the fallback logic under test.

    None of these are DB-round-trip-free in production, but for this test we only care
    that the Agent row created gets the right max_tokens_per_day/month, so quota checks,
    relationship bootstrapping and response serialization are all replaced with no-ops.
    """

    async def fake_quota_check(_user_id):
        return None

    async def fake_ensure_access(_db, _agent, *, created_by_user_id=None):
        return True

    async def fake_agent_to_out(_db, _agent, _viewer_id):
        return object()

    monkeypatch.setattr(agents_api, "check_agent_creation_quota", fake_quota_check)
    monkeypatch.setattr(agents_api, "ensure_access_granted_platform_relationships", fake_ensure_access)
    monkeypatch.setattr(agents_api, "_agent_to_out", fake_agent_to_out)


@pytest.mark.asyncio
async def test_create_agent_inherits_tenant_default_token_limits_when_request_omits_them(monkeypatch):
    """`AgentCreate.max_tokens_per_day/month` left as None means 'use the tenant default'.

    Before this task, `None` silently became "no limit" instead of falling back to the
    tenant's configured default.
    """
    tenant = make_tenant(
        default_agent_max_tokens_per_day=50_000,
        default_agent_max_tokens_per_month=1_000_000,
        default_agent_ttl_hours=0,
        default_max_llm_calls_per_day=1000,
        default_max_triggers=20,
        min_poll_interval_floor=5,
        max_webhook_rate_ceiling=5,
        default_model_id=None,
        min_heartbeat_interval_minutes=0,
    )
    user = make_user(tenant_id=tenant.id)
    db = RecordingDB(responses=[DummyResult([tenant])])
    _patch_create_agent_collaborators(monkeypatch)

    data = AgentCreate(name="New Agent", max_tokens_per_day=None, max_tokens_per_month=None)

    await agents_api.create_agent(
        data=data,
        background_tasks=BackgroundTasks(),
        current_user=user,
        db=db,
    )

    created = next(item for item in db.added if isinstance(item, Agent))
    assert created.max_tokens_per_day == 50_000
    assert created.max_tokens_per_month == 1_000_000


@pytest.mark.asyncio
async def test_create_agent_explicit_request_value_wins_over_tenant_default(monkeypatch):
    """An explicit request value must never be silently overridden by the tenant default."""
    tenant = make_tenant(
        default_agent_max_tokens_per_day=50_000,
        default_agent_max_tokens_per_month=1_000_000,
        default_agent_ttl_hours=0,
        default_max_llm_calls_per_day=1000,
        default_max_triggers=20,
        min_poll_interval_floor=5,
        max_webhook_rate_ceiling=5,
        default_model_id=None,
        min_heartbeat_interval_minutes=0,
    )
    user = make_user(tenant_id=tenant.id)
    db = RecordingDB(responses=[DummyResult([tenant])])
    _patch_create_agent_collaborators(monkeypatch)

    data = AgentCreate(name="New Agent", max_tokens_per_day=12_345, max_tokens_per_month=None)

    await agents_api.create_agent(
        data=data,
        background_tasks=BackgroundTasks(),
        current_user=user,
        db=db,
    )

    created = next(item for item in db.added if isinstance(item, Agent))
    assert created.max_tokens_per_day == 12_345  # explicit value, not the 50_000 tenant default
    assert created.max_tokens_per_month == 1_000_000  # omitted -> still falls back to the tenant default


# ---------------------------------------------------------------------------
# list_agents (backend/app/api/agents.py) — N+1 regression on the lazy reset
# ---------------------------------------------------------------------------


class _FakeVisibleAgentsStmt:
    """Stands in for `build_visible_agents_query(...)`'s return value.

    `list_agents` calls `.order_by(...)` on it before executing; `RecordingDB.execute`
    never inspects the statement it's given, so the object only needs to survive that
    one chained call.
    """

    def order_by(self, *args, **kwargs):
        return self


def _always(value):
    async def _call(*args, **kwargs):
        return value

    return _call


@pytest.mark.asyncio
async def test_list_agents_loads_the_shared_tenant_once_not_once_per_agent(monkeypatch):
    """`list_agents` must not re-query Tenant once per agent.

    `build_visible_agents_query` filters every branch on `Agent.tenant_id ==
    target_tenant_id`, so every agent in the response always shares exactly one tenant.
    Passing that single pre-loaded tenant into `_lazy_reset_token_counters` for every
    agent avoids re-querying it N times — the shape a naive per-agent lookup (this
    task's own first pass, before the fix) would produce.
    """
    tenant = make_tenant(timezone="Asia/Shanghai")
    user = make_user(tenant_id=tenant.id, role="member")
    long_ago = datetime(2000, 1, 1, tzinfo=UTC)
    agents = [
        make_agent(
            tenant_id=tenant.id,
            timezone=None,
            creator_id=user.id,
            last_daily_reset=long_ago,
            last_monthly_reset=long_ago,
        )
        for _ in range(3)
    ]
    db = RecordingDB(responses=[DummyResult(agents), DummyResult([tenant])])

    monkeypatch.setattr(agents_api, "build_visible_agents_query", lambda *a, **kw: _FakeVisibleAgentsStmt())
    monkeypatch.setattr(agents_api, "_build_unread_count_by_agent", _always({}))
    monkeypatch.setattr(agents_api, "_serialize_agent_out", lambda agent, unread_count=0: SimpleNamespace())
    monkeypatch.setattr("app.services.onboarding.onboarded_agent_ids", _always(set()))

    out = await agents_api.list_agents(tenant_id=None, current_user=user, db=db)

    assert len(out) == 3
    # 1 query for the visible-agents list + 1 for the shared Tenant. A per-agent lookup
    # would make this 1 + 3 = 4 instead, and grow with every additional agent.
    assert len(db.executed) == 2
