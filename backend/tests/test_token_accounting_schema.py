"""Token 计量的表结构约束。

用声明式内省与迁移模块断言，不连真实数据库（本仓库无 conftest、无
create_async_engine）。
"""

from __future__ import annotations

from importlib import util
from pathlib import Path

from sqlalchemy import Integer, String

from app.models.activity_log import DailyTokenUsage
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.tenant_token_counter import TenantTokenCounter

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "202608061000_token_accounting_v2.py"


def _load_migration():
    spec = util.spec_from_file_location("token_accounting_v2", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_has_input_token_counters() -> None:
    """修正后的命中率分母是"输入总量"，现有列里算不出来。"""
    for name in ("input_tokens_today", "input_tokens_month", "input_tokens_total"):
        column = Agent.__table__.c[name]
        assert isinstance(column.type, Integer)
        assert column.default is not None


def test_tenant_has_daily_ceiling_and_agent_defaults() -> None:
    for name in (
        "max_tokens_per_day",
        "default_agent_max_tokens_per_day",
        "default_agent_max_tokens_per_month",
    ):
        column = Tenant.__table__.c[name]
        assert isinstance(column.type, Integer)
        assert column.nullable is True, f"{name} 必须可空，NULL 表示无限"


def test_tenant_has_no_monthly_ceiling_field() -> None:
    """本次只做租户日上限，不留用不上的死字段。"""
    assert "max_tokens_per_month" not in Tenant.__table__.c


def test_tenant_token_counter_is_a_narrow_dedicated_row() -> None:
    """不塞进 tenants 行：那是高频读的配置行，每轮 UPDATE 会耦合读写并churn 行版本。"""
    table = TenantTokenCounter.__table__

    assert table.name == "tenant_token_counters"
    assert [c.name for c in table.primary_key.columns] == ["tenant_id"]
    for name in ("tokens_used_today", "tokens_used_total"):
        assert isinstance(table.c[name].type, Integer)
    assert table.c["last_daily_reset"].nullable is True
    assert "tokens_used_month" not in table.c


def test_daily_token_usage_agent_id_is_nullable_and_set_null() -> None:
    """系统开销行没有归属 agent；且删 agent 不该抹掉历史租户用量。"""
    column = DailyTokenUsage.__table__.c["agent_id"]

    assert column.nullable is True
    foreign_key = next(iter(column.foreign_keys))
    assert foreign_key.ondelete == "SET NULL"


def test_daily_token_usage_has_attribution_and_reasoning_columns() -> None:
    table = DailyTokenUsage.__table__

    assert isinstance(table.c["agent_name_snapshot"].type, String)
    assert table.c["agent_name_snapshot"].nullable is True
    assert isinstance(table.c["system_scope"].type, String)
    assert table.c["system_scope"].nullable is True
    assert isinstance(table.c["reasoning_tokens"].type, Integer)


def test_old_agent_date_unique_constraint_is_gone() -> None:
    """留着它，可空 agent_id 会让 ON CONFLICT 永不命中系统开销行。"""
    constraint_names = {c.name for c in DailyTokenUsage.__table__.constraints}

    assert "uq_daily_token_usage_agent_date" not in constraint_names


def test_two_partial_unique_indexes_replace_it() -> None:
    """PostgreSQL 把唯一约束里的 NULL 视为互不相同，所以必须用部分唯一索引拆开。

    断言精确到谓词文本：只断言 `where is not None` 会漏掉"两个谓词写反了"这种最危险
    的缺陷——那会让系统开销行永远去重不到，每次调用都插新行、把用量记账悄悄放大。
    """
    indexes = {index.name: index for index in DailyTokenUsage.__table__.indexes}

    agent_index = indexes["uq_daily_token_usage_agent_date"]
    assert agent_index.unique is True
    assert [c.name for c in agent_index.columns] == ["agent_id", "date"]
    assert str(agent_index.dialect_options["postgresql"]["where"]) == "system_scope IS NULL"

    system_index = indexes["uq_daily_token_usage_system_date"]
    assert system_index.unique is True
    assert [c.name for c in system_index.columns] == [
        "tenant_id",
        "system_scope",
        "date",
    ]
    assert str(system_index.dialect_options["postgresql"]["where"]) == "system_scope IS NOT NULL"


class _OpCallRecorder:
    """啞替身，站在迁移模块的 `op` 位置，记录 `upgrade()` 发出的每一次调用。

    不连接、不模拟任何数据库；`op.execute(sql)` 只把 `sql` 文本存进 `executed`，
    其余任意 `op.*` 调用（`alter_column`/`create_foreign_key`/...）都被 `__getattr__`
    兜底记录进 `calls`，返回 `None`，不做任何真实的 DDL 执行。
    """

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _record(*args: object, **kwargs: object):
            self.calls.append((name, args, kwargs))
            if name == "execute" and args:
                self.executed.append(str(args[0]))

        return _record


def test_migration_upgrade_binds_each_partial_index_name_to_its_own_predicate() -> None:
    """跑迁移真正的 `upgrade()`（`op` 换成录制替身，不连库），逐条核对：

    - `uq_daily_token_usage_agent_date` 只出现在带 `system_scope IS NULL` 谓词的
      语句里，绝不出现在带 `system_scope IS NOT NULL` 谓词的语句里；
    - `uq_daily_token_usage_system_date` 反过来，只绑 `IS NOT NULL`；
    - `agent_id` 外键通过 `create_foreign_key` 以 `ondelete="SET NULL"` 重建。

    旧版本只断言三个子串"在文件某处出现过"，两个谓词整段互换后子串依然齐全、测试
    照样通过——这里改成按索引名精确配对谓词，专门堵住"谓词写反"这类会让系统开销
    行永远去重不到、用量随调用次数虚增的缺陷。
    """
    migration = _load_migration()
    recorder = _OpCallRecorder()
    migration.op = recorder
    try:
        migration.upgrade()

        def statements_with(*needles: str) -> list[str]:
            return [sql for sql in recorder.executed if all(needle in sql for needle in needles)]

        agent_index = migration.AGENT_UNIQUE_INDEX
        system_index = migration.SYSTEM_UNIQUE_INDEX

        agent_with_null = statements_with(agent_index, "system_scope IS NULL")
        assert len(agent_with_null) == 1
        agent_with_not_null = statements_with(agent_index, "system_scope IS NOT NULL")
        assert agent_with_not_null == []

        system_with_not_null = statements_with(system_index, "system_scope IS NOT NULL")
        assert len(system_with_not_null) == 1
        # "IS NOT NULL" 里没有连续的 "IS NULL" 子串（中间隔着 "NOT"），但仍显式排除
        # 带 NOT 的语句，不依赖这个偶然的字符串事实。
        system_with_null_only = [
            sql
            for sql in recorder.executed
            if system_index in sql and "system_scope IS NULL" in sql and "system_scope IS NOT NULL" not in sql
        ]
        assert system_with_null_only == []

        fk_calls = [call for call in recorder.calls if call[0] == "create_foreign_key"]
        assert any(call[2].get("ondelete") == "SET NULL" for call in fk_calls)
    finally:
        del migration.op


def test_migration_upgrade_normalizes_pre_existing_zero_agent_limits_to_null() -> None:
    """0 在旧后端语义下等价于"无限"，新判定把它读成"禁止一切"。为了让本迁移上线不
    悄悄把历史 agent 的有效限额从"无限"变成"全部拦截"，upgrade() 必须把已存的 0
    收窄为 NULL。

    断言用**有序的整段片段**而不是若干个互不相干的子串：`SET`/`WHERE` 写反的版本
    （`SET <col> = 0 WHERE <col> IS NULL`）会把当前所有"无限"的 agent 一次性全部
    封死 —— 正是这条归一化要防的方向 —— 而它同样含有 "UPDATE agents"、列名、
    "= 0"（在 "= 0 WHERE" 里）和 "NULL"（在 "IS NULL" 里）这四个子串，无序子串
    断言抓不到它。
    """
    migration = _load_migration()
    recorder = _OpCallRecorder()
    migration.op = recorder
    try:
        migration.upgrade()

        for column in ("max_tokens_per_day", "max_tokens_per_month"):
            wanted = f"UPDATE agents SET {column} = NULL WHERE {column} = 0"
            matching = [sql for sql in recorder.executed if wanted in sql]
            assert len(matching) == 1, (column, wanted, recorder.executed)

            inverted = f"SET {column} = 0"
            assert not [sql for sql in recorder.executed if inverted in sql], (
                f"{column} 的归一化方向写反了，会把无限额的 agent 全部封死",
                recorder.executed,
            )
    finally:
        del migration.op


def test_migration_is_chained_onto_the_current_head() -> None:
    migration = _load_migration()

    assert migration.revision == "token_accounting_v2"
    assert migration.down_revision == "widen_credential_scopes"


def test_migration_declares_the_three_system_scopes() -> None:
    migration = _load_migration()

    assert migration.SYSTEM_SCOPES == ("group_compact", "planning", "model_probe")


def test_migration_names_both_partial_indexes() -> None:
    migration = _load_migration()

    assert migration.AGENT_UNIQUE_INDEX == "uq_daily_token_usage_agent_date"
    assert migration.SYSTEM_UNIQUE_INDEX == "uq_daily_token_usage_system_date"
