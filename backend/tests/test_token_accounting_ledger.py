"""记账写入的原子性、顺序与归属。

用假 session 记录发出的语句，不连真实数据库（本仓库测试不连 DB）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib import util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.token_accounting import ledger
from app.services.token_accounting.ledger import (
    SYSTEM_SCOPE_PLANNING,
    SYSTEM_SCOPES,
    record,
)
from app.services.token_accounting.normalize import TokenUsage

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "202608061000_token_accounting_v2.py"

NOW = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)  # 北京 8/7 00:30
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDbError(Exception):
    """模拟 SQLAlchemy 的 DBAPIError：真正的错误码挂在 `.orig.sqlstate` 上，不在
    消息文本里 —— 用来验证 `_is_retryable` 优先按 SQLSTATE 分类，而不是碰巧从消
    息里匹配到关键词。
    """

    def __init__(self, sqlstate: str, message: str = "unrelated wording, no known marker here"):
        super().__init__(message)
        self.orig = SimpleNamespace(sqlstate=sqlstate)


class FakeSession:
    """记录 execute 顺序与 commit/rollback，供断言事务语义。"""

    def __init__(
        self,
        *,
        agent=None,
        tenant=None,
        fail_times: int = 0,
        fail_error: Exception | None = None,
    ):
        self.statements: list[object] = []
        self.committed = 0
        self.rolled_back = 0
        self._agent = agent
        self._tenant = tenant
        self._fail_times = fail_times
        self._fail_error = fail_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        text = str(statement).lower()
        if self._fail_times > 0 and "update tenant_token_counters" in text:
            self._fail_times -= 1
            raise self._fail_error or RuntimeError("could not serialize access due to concurrent update")
        if "from agents" in text:
            return FakeResult(self._agent)
        if "from tenants" in text:
            return FakeResult(self._tenant)
        return FakeResult()

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1


def _install(monkeypatch, session: FakeSession) -> None:
    monkeypatch.setattr(ledger, "async_session", lambda: session)


def _usage() -> TokenUsage:
    return TokenUsage(
        total_tokens=93_500,
        input_tokens=93_000,
        output_tokens=500,
        cache_read_tokens=90_000,
        cache_creation_tokens=2_000,
        reasoning_tokens=0,
        estimated_tokens=0,
    )


_TABLE_CLAUSES = {
    "tenant_token_counters": ("insert into tenant_token_counters", "update tenant_token_counters"),
    "agents": ("from agents", "update agents"),
    "daily_token_usage": ("insert into daily_token_usage",),
}


def _tables_touched(session: FakeSession) -> list[str]:
    """按 SQL 子句而非裸表名子串匹配 —— 裸子串会把像 `default_max_agents` 这种
    列名误判成命中了 `agents` 表。
    """
    order: list[str] = []
    for statement in session.statements:
        text = str(statement).lower()
        for table, needles in _TABLE_CLAUSES.items():
            if any(needle in text for needle in needles) and (not order or order[-1] != table):
                order.append(table)
                break
    return order


async def test_zero_usage_is_not_written(monkeypatch) -> None:
    session = FakeSession()
    _install(monkeypatch, session)

    assert await record(TokenUsage(), tenant_id=TENANT_ID, agent_id=AGENT_ID) is True
    assert session.statements == []


async def test_write_order_is_fixed_to_avoid_deadlocks(monkeypatch) -> None:
    """并发事务若以不同顺序锁这三张表就会互相死锁。"""
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant)
    _install(monkeypatch, session)

    await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)

    order = _tables_touched(session)
    for table in ("tenant_token_counters", "agents", "daily_token_usage"):
        assert table in order, f"{table} 未出现在发出的语句里: {order}"
    assert order.index("tenant_token_counters") < order.index("agents")
    assert order.index("agents") < order.index("daily_token_usage")


async def test_counters_are_incremented_atomically_in_sql(monkeypatch) -> None:
    """Python 侧读改写在并发下会丢更新，必须是 SQL 原子累加。"""
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant)
    _install(monkeypatch, session)

    await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)

    agent_updates = [str(s).lower() for s in session.statements if "update agents" in str(s).lower()]
    assert agent_updates, "没有对 agents 发出 UPDATE"
    incrementing = [s for s in agent_updates if "tokens_used_today +" in s]
    assert incrementing, "agents 的计数器不是 SQL 原子累加"


async def test_lazy_reset_is_a_conditional_update(monkeypatch) -> None:
    """条件 UPDATE 构造上幂等：两个并发轮次只会清零一次，且不吞掉对方的累加。

    专门绑定到 `tenant_token_counters` 语句上：如果只按"某条语句同时含
    last_daily_reset 和 update"匹配，agents 表的日重置会顶替满足这条断言，删掉
    tenant 的重置逻辑测试也照样通过。见下面的删除-变红证据。

    同时钉住"重置先于累加"这一顺序：两者在同一事务里，如果重置被挪到累加之后，
    本地新一天的第一笔写入会被随后发出的重置语句清零，token 悄悄丢失。tenant 和
    agent 两条重置路径都要钉。
    """
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant)
    _install(monkeypatch, session)

    await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)

    tenant_resets = [
        str(s).lower()
        for s in session.statements
        if "update tenant_token_counters" in str(s).lower() and "last_daily_reset" in str(s).lower()
    ]
    assert tenant_resets, "没有对 tenant_token_counters 发出日重置语句"
    assert any("last_daily_reset is null" in s or "last_daily_reset <" in s for s in tenant_resets)

    def _index_of(*needles: str) -> int:
        """复用文件里其它测试已经在用的子串匹配方式，只是额外记录下标以比较顺序。"""
        for i, statement in enumerate(session.statements):
            text = str(statement).lower()
            if all(needle in text for needle in needles):
                return i
        raise AssertionError(f"没有语句同时匹配 {needles}: {[str(s).lower() for s in session.statements]}")

    tenant_reset_index = _index_of("update tenant_token_counters", "last_daily_reset")
    tenant_increment_index = _index_of("update tenant_token_counters", "tokens_used_today +")
    assert tenant_reset_index < tenant_increment_index, (
        "tenant 惰性重置必须先于累加执行——顺序颠倒后，本地新一天的第一笔 token 会被"
        f"随后发出的重置语句清零。实际顺序 reset_index={tenant_reset_index} "
        f"increment_index={tenant_increment_index}"
    )

    agent_reset_index = _index_of("update agents", "last_daily_reset")
    agent_increment_index = _index_of("update agents", "tokens_used_today +")
    assert agent_reset_index < agent_increment_index, (
        "agent 惰性重置必须先于累加执行——顺序颠倒后，本地新一天的第一笔 token 会被"
        f"随后发出的重置语句清零。实际顺序 reset_index={agent_reset_index} "
        f"increment_index={agent_increment_index}"
    )


async def test_agent_daily_reset_is_skipped_when_not_yet_stale(monkeypatch) -> None:
    """agent.last_daily_reset 已经落在本地当日内时，不该再发一次日重置语句 ——
    `_reset_agent_counters_if_stale` 在 Python 侧先判断 `is_new_local_day`，只有
    真的过期才会发 UPDATE。用一个恰好等于当天零点的 last_daily_reset 验证"未过期
    就不发"，同时确认计数器累加不受影响、照常发出。

    （tenant_token_counters 的惰性重置没有对应的 Python 侧短路 —— 它总是发出同一
    条带 WHERE 谓词的条件 UPDATE，是否真的清零由数据库按 WHERE 求值决定，跟假
    session 无关，所以这个"未过期就不发语句"的断言只能落在 agent 分支上。）
    """
    tz_name = "Asia/Shanghai"
    day_start = ledger.local_day_start(tz_name, now=NOW)
    month_start = ledger.local_month_start(tz_name, now=NOW)
    agent = SimpleNamespace(
        id=AGENT_ID,
        name="Ada",
        timezone=None,
        tenant_id=TENANT_ID,
        last_daily_reset=day_start,
        last_monthly_reset=month_start,
    )
    tenant = SimpleNamespace(id=TENANT_ID, timezone=tz_name)
    session = FakeSession(agent=agent, tenant=tenant)
    _install(monkeypatch, session)

    await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)

    agent_resets = [
        str(s).lower()
        for s in session.statements
        if "update agents" in str(s).lower() and "last_daily_reset" in str(s).lower()
    ]
    assert agent_resets == [], "agent 未过期时不该发出日重置语句"

    agent_increments = [
        str(s).lower()
        for s in session.statements
        if "update agents" in str(s).lower() and "tokens_used_today +" in str(s).lower()
    ]
    assert agent_increments, "即便跳过重置，agents 的计数器累加仍必须照常发出"


async def test_upsert_targets_the_agent_partial_index(monkeypatch) -> None:
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant)
    _install(monkeypatch, session)

    await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)

    upserts = [str(s).lower() for s in session.statements if "insert into daily_token_usage" in str(s).lower()]
    assert upserts
    assert "on conflict" in upserts[0]
    assert "system_scope is null" in upserts[0]


async def test_system_overhead_row_targets_the_system_partial_index(monkeypatch) -> None:
    """系统开销行的 agent_id 是 NULL，必须走另一个部分唯一索引。"""
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(tenant=tenant)
    _install(monkeypatch, session)

    await record(
        _usage(),
        tenant_id=TENANT_ID,
        system_scope=SYSTEM_SCOPE_PLANNING,
        now=NOW,
    )

    upserts = [str(s).lower() for s in session.statements if "insert into daily_token_usage" in str(s).lower()]
    assert upserts
    assert "system_scope is not null" in upserts[0]


async def test_system_overhead_never_touches_agent_counters(monkeypatch) -> None:
    """共享开销不该拖累任何单个 Agent 的额度。"""
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(tenant=tenant)
    _install(monkeypatch, session)

    await record(
        _usage(),
        tenant_id=TENANT_ID,
        system_scope=SYSTEM_SCOPE_PLANNING,
        now=NOW,
    )

    assert not any("update agents" in str(s).lower() for s in session.statements)


async def test_unknown_system_scope_is_rejected(monkeypatch) -> None:
    session = FakeSession()
    _install(monkeypatch, session)

    with pytest.raises(ValueError):
        await record(_usage(), tenant_id=TENANT_ID, system_scope="not_a_scope", now=NOW)


async def test_neither_agent_id_nor_system_scope_is_rejected(monkeypatch) -> None:
    """两者都不传时，`_daily_upsert` 会按 `system_scope is None` 走 agent 分支，但
    `agent_id` 是 NULL —— PostgreSQL 把唯一索引里的 NULL 视为互不相同，ON CONFLICT
    永远不命中，每次调用都插新行，日聚合随调用次数虚增。必须在最前面拒绝这个组合。
    """
    session = FakeSession()
    _install(monkeypatch, session)

    with pytest.raises(ValueError, match="agent_id"):
        await record(_usage(), tenant_id=TENANT_ID, now=NOW)

    assert session.statements == [], "校验必须在任何语句发出之前就拒绝"


async def test_both_agent_id_and_system_scope_is_rejected(monkeypatch) -> None:
    """两者都传时，agent 计数器会照常累加（`agent_id is not None`），但
    `_daily_upsert` 按 `system_scope is not None` 把明细行路由进系统开销分桶 ——
    Agent 被记上了共享开销的额度，`tokens_used_today` 与它自己的日明细行永久漂移。
    必须在最前面拒绝这个组合。
    """
    session = FakeSession()
    _install(monkeypatch, session)

    with pytest.raises(ValueError, match="system_scope"):
        await record(
            _usage(),
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            system_scope=SYSTEM_SCOPE_PLANNING,
            now=NOW,
        )

    assert session.statements == [], "校验必须在任何语句发出之前就拒绝"


async def test_missing_agent_row_keeps_tenant_write_but_skips_daily_row(monkeypatch) -> None:
    """Agent 可能在 LLM 调用之后、记账之前被删除 —— `daily_token_usage.agent_id`
    的 `ondelete="SET NULL"` 正是为了让这种情况下历史仍可留存。这些 token 依然真
    实消耗、依然属于该租户，租户计数必须照常累加；但明细行不能带着一个不存在的
    `agent_id` 去 upsert（FK 是 NO ACTION，会导致整个事务连同租户计数一起回滚），
    也不能改道系统开销分桶（那会把 Agent 自己的开销误记成共享开销）。
    """
    from loguru import logger

    records: list[str] = []
    handler_id = logger.add(lambda message: records.append(message), level="WARNING")

    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=None, tenant=tenant)  # agent 查询返回 None：已被删除
    _install(monkeypatch, session)

    try:
        ok = await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)
    finally:
        logger.remove(handler_id)

    assert ok is True
    assert session.committed == 1
    assert any(
        "insert into tenant_token_counters" in str(s).lower() or "update tenant_token_counters" in str(s).lower()
        for s in session.statements
    ), "租户计数写入不该被跳过"
    assert not any("insert into daily_token_usage" in str(s).lower() for s in session.statements), (
        "明细行不该带着已不存在的 agent_id 去 upsert"
    )
    assert any("token_ledger_agent_missing_daily_row_skipped" in r for r in records)
    assert any(str(AGENT_ID) in r for r in records)
    # 跟 ERROR 路径的 test_retries_then_reports_failure_without_raising 一样钉住载
    # 荷数字：WARNING 也必须带着完整用量，才能在 agent 已被删的情况下从日志里把这
    # 笔 token 恢复出来。
    warning_record = next(r for r in records if "token_ledger_agent_missing_daily_row_skipped" in r)
    assert "total=93500" in warning_record
    assert "input=93000" in warning_record
    assert "output=500" in warning_record
    assert "cache_read=90000" in warning_record
    assert "cache_creation=2000" in warning_record


async def test_daily_row_date_anchor_uses_local_midnight(monkeypatch) -> None:
    """UTC 16:30 对 Asia/Shanghai 已是次日，锚点必须是 8/6 16:00Z。"""
    captured: dict[str, object] = {}
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant)
    _install(monkeypatch, session)

    original = ledger.local_day_start

    def spy(tz_name, *, now):
        result = original(tz_name, now=now)
        captured[tz_name] = result
        return result

    monkeypatch.setattr(ledger, "local_day_start", spy)

    await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)

    assert captured["Asia/Shanghai"] == datetime(2026, 8, 6, 16, 0, tzinfo=UTC)


async def test_retries_then_reports_failure_without_raising(monkeypatch) -> None:
    """记账失败必须可见（返回 False + ERROR 日志），而不是静默 warning 后吞掉。"""
    from loguru import logger

    records: list[str] = []
    handler_id = logger.add(lambda message: records.append(message), level="ERROR")

    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant, fail_times=99)
    _install(monkeypatch, session)

    try:
        ok = await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)
    finally:
        logger.remove(handler_id)

    assert ok is False
    assert session.rolled_back >= 1
    assert any("token_ledger_write_failed" in record for record in records)
    assert any("93500" in record or "93,500" in record for record in records)


async def test_transient_failure_is_retried_and_then_succeeds(monkeypatch) -> None:
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant, fail_times=1)
    _install(monkeypatch, session)

    assert await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW) is True
    assert session.committed == 1


async def test_transient_failure_classified_by_sqlstate_is_retried(monkeypatch) -> None:
    """消息文本故意不含任何已知关键词，只靠 `.orig.sqlstate == "40001"` 判定可重试 ——
    验证分类不依赖 locale 相关的消息子串匹配。
    """
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    error = _FakeDbError("40001")
    session = FakeSession(agent=agent, tenant=tenant, fail_times=1, fail_error=error)
    _install(monkeypatch, session)

    assert await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW) is True
    assert session.committed == 1


def test_is_retryable_prefers_sqlstate_over_message_text() -> None:
    assert ledger._is_retryable(_FakeDbError("40001")) is True
    assert ledger._is_retryable(_FakeDbError("40P01")) is True
    # 23505 = unique_violation：真实错误，不该被当成可重试的瞬时失败。
    assert ledger._is_retryable(_FakeDbError("23505")) is False
    # 23505 但消息文本恰好含有可重试关键词："could not serialize"——如果分类误
    # 优先用消息子串而不是 SQLSTATE，这一条就会被错判成可重试。必须仍判定为不可
    # 重试，才能证明 SQLSTATE 真的压制了消息文本兜底，而不是两条用例都恰好绕开了
    # 消息匹配。
    assert ledger._is_retryable(_FakeDbError("23505", "could not serialize")) is False


def test_is_retryable_falls_back_to_message_text_without_sqlstate() -> None:
    assert ledger._is_retryable(RuntimeError("could not serialize access due to concurrent update")) is True
    assert ledger._is_retryable(RuntimeError("permission denied for table agents")) is False


async def test_tenant_counter_is_upserted_not_bare_updated(monkeypatch) -> None:
    """tenant_token_counters 只在迁移时预置了存量租户的行；新建租户没有行，
    裸 UPDATE 会静默匹配 0 行、天花板永远不累加。必须先 upsert 出这一行。
    """
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant)
    _install(monkeypatch, session)

    await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)

    inserts = [str(s).lower() for s in session.statements if "insert into tenant_token_counters" in str(s).lower()]
    assert inserts, "没有为 tenant_token_counters 发出 upsert，新租户会被裸 UPDATE 静默跳过"
    assert "on conflict" in inserts[0]
    assert "tenant_id" in inserts[0]


def _load_migration():
    """与 test_token_accounting_schema.py 里的同名 helper 同一套 importlib 加载
    方式：直接从迁移文件路径加载模块，不连数据库。
    """
    spec = util.spec_from_file_location("token_accounting_v2", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_system_scopes_match_the_migration() -> None:
    """ledger.SYSTEM_SCOPES 是从迁移常量手抄过来的字面量，会跟迁移悄悄漂移；改成
    直接对比迁移模块本身，两者不一致时测试立刻能抓到。
    """
    migration = _load_migration()

    assert migration.SYSTEM_SCOPES == SYSTEM_SCOPES


async def test_record_token_usage_shim_forwards_to_the_ledger(monkeypatch) -> None:
    """旧入口必须转发到新 ledger，不能存在第二套记账实现。"""
    from app.services import token_tracker

    calls: list[dict] = []

    async def fake_record(usage, **kwargs):
        calls.append({"usage": usage, **kwargs})
        return True

    async def fake_resolve(agent_id):
        return TENANT_ID

    monkeypatch.setattr(token_tracker, "ledger_record", fake_record)
    monkeypatch.setattr(token_tracker, "_resolve_tenant_id", fake_resolve)

    await token_tracker.record_token_usage(AGENT_ID, _usage())

    assert len(calls) == 1
    assert calls[0]["agent_id"] == AGENT_ID
    assert calls[0]["tenant_id"] == TENANT_ID
    assert calls[0]["usage"].total_tokens == 93_500


def test_token_tracker_reexports_the_canonical_token_usage() -> None:
    """两处各自定义 TokenUsage 迟早会分叉。"""
    from app.services import token_tracker
    from app.services.token_accounting.normalize import TokenUsage as Canonical

    assert token_tracker.TokenUsage is Canonical


def test_legacy_extract_token_usage_still_reads_openai_shaped_usage() -> None:
    from app.services.token_tracker import extract_token_usage, extract_usage_tokens

    usage = extract_token_usage({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})

    assert usage is not None
    assert usage.total_tokens == 150
    assert extract_usage_tokens({"prompt_tokens": 100, "completion_tokens": 50}) == 150
