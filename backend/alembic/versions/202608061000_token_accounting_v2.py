"""token accounting v2: 含缓存的统一口径、系统开销归属、租户日上限

Revision ID: token_accounting_v2
Revises: widen_credential_scopes
Create Date: 2026-08-06 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "token_accounting_v2"
down_revision: str | None = "widen_credential_scopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_SCOPES = ("group_compact", "planning", "model_probe")
AGENT_UNIQUE_INDEX = "uq_daily_token_usage_agent_date"
SYSTEM_UNIQUE_INDEX = "uq_daily_token_usage_system_date"

AGENT_INPUT_COLUMNS = (
    "input_tokens_today",
    "input_tokens_month",
    "input_tokens_total",
)
TENANT_LIMIT_COLUMNS = (
    "max_tokens_per_day",
    "default_agent_max_tokens_per_day",
    "default_agent_max_tokens_per_month",
)


def upgrade() -> None:
    # 与 020/050 迁移同样的纪律：全部用 IF NOT EXISTS 的裸 SQL 建列/建表/建索引，让本
    # 迁移可以在 ALLOW_MIGRATION_FAILURE=true 触发的失败重试、或历史 create_all 已经
    # 建过部分对象的场景下安全地重复执行，而不是在第二次跑时因为"already exists"报错。
    for name in AGENT_INPUT_COLUMNS:
        op.execute(f"ALTER TABLE agents ADD COLUMN IF NOT EXISTS {name} INTEGER NOT NULL DEFAULT 0")
        # ADD COLUMN IF NOT EXISTS 在列已由 create_all 建过（历史库、Python 侧只有
        # default=0、没有 server_default）时会静默跳过，让该库永远没有数据库侧
        # DEFAULT。这一句无条件重跑、幂等，把两条建库路径拉回同一形状。
        op.execute(f"ALTER TABLE agents ALTER COLUMN {name} SET DEFAULT 0")

    # 旧后端语义下 `if not limit` 把 0 和 None 一样当作"无限"；budget.py 改判定后
    # 0 变成"禁止一切"，二者含义不再等价。为了让本迁移上线时不悄悄把历史 agent 的
    # 有效限额从"无限"变成"全部拦截"，把已存的 0 统一收窄为 NULL（唯一的"无限"值），
    # 保持它们的实际限额语义不变。语句按值匹配，重跑是安全的：已经是 NULL 的行不会
    # 再被 `= 0` 命中。
    op.execute("UPDATE agents SET max_tokens_per_day = NULL WHERE max_tokens_per_day = 0")
    op.execute("UPDATE agents SET max_tokens_per_month = NULL WHERE max_tokens_per_month = 0")

    for name in TENANT_LIMIT_COLUMNS:
        op.execute(f"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS {name} INTEGER")

    op.execute(
        "CREATE TABLE IF NOT EXISTS tenant_token_counters ("
        "tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE, "
        "tokens_used_today INTEGER NOT NULL DEFAULT 0, "
        "tokens_used_total INTEGER NOT NULL DEFAULT 0, "
        "last_daily_reset TIMESTAMPTZ"
        ")"
    )
    # CREATE TABLE IF NOT EXISTS 同理可能撞上 create_all 已建好的表；同样无条件补默认值。
    op.execute("ALTER TABLE tenant_token_counters ALTER COLUMN tokens_used_today SET DEFAULT 0")
    op.execute("ALTER TABLE tenant_token_counters ALTER COLUMN tokens_used_total SET DEFAULT 0")
    # 每个现有租户预置一行零计数；这只覆盖迁移执行时已经存在的租户——迁移之后新建的
    # 租户不会自动获得这一行，热路径必须用 INSERT ... ON CONFLICT (tenant_id) DO
    # UPDATE 做 upsert，不能假设行已存在而直接对它做 UPDATE（否则 UPDATE 会静默匹配
    # 0 行，租户日上限对新租户永远不会累计）。
    op.execute(
        "INSERT INTO tenant_token_counters (tenant_id, tokens_used_today, "
        "tokens_used_total) SELECT id, 0, 0 FROM tenants "
        "ON CONFLICT (tenant_id) DO NOTHING"
    )

    op.execute("ALTER TABLE daily_token_usage ADD COLUMN IF NOT EXISTS agent_name_snapshot VARCHAR(200)")
    op.execute("ALTER TABLE daily_token_usage ADD COLUMN IF NOT EXISTS system_scope VARCHAR(32)")
    op.execute("ALTER TABLE daily_token_usage ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE daily_token_usage ALTER COLUMN reasoning_tokens SET DEFAULT 0")
    op.execute("CREATE INDEX IF NOT EXISTS ix_daily_token_usage_system_scope ON daily_token_usage (system_scope)")

    # 历史行回填 agent 名快照，使删 agent 后仍可归因。
    op.execute(
        "UPDATE daily_token_usage AS d SET agent_name_snapshot = a.name "
        "FROM agents AS a WHERE d.agent_id = a.id AND d.agent_name_snapshot IS NULL"
    )

    op.alter_column(
        "daily_token_usage",
        "agent_id",
        existing_type=UUID(as_uuid=True),
        nullable=True,
    )
    # 与本迁移其余步骤同样的幂等纪律：drop_constraint 在约束已不存在时会硬失败，是
    # upgrade() 里唯一一处不可重跑的步骤，换成 DROP CONSTRAINT IF EXISTS 补齐。
    op.execute("ALTER TABLE daily_token_usage DROP CONSTRAINT IF EXISTS daily_token_usage_agent_id_fkey")
    op.create_foreign_key(
        "daily_token_usage_agent_id_fkey",
        "daily_token_usage",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 旧的 uq_daily_token_usage_agent_date 在两条不同建表路径下对应不同的数据库对象：
    # Alembic 拥有的库里，020_add_daily_token_usage.py 用裸 SQL `CREATE UNIQUE INDEX
    # IF NOT EXISTS` 建的，只登记在 pg_indexes、不登记在 pg_constraint；而走
    # bootstrap_db.py（DATABASE_AUTO_CREATE_TABLES）create_all 的历史库里，旧声明式
    # 模型把它定义成 UniqueConstraint，是真实约束，对它直接 DROP INDEX 会报
    # "cannot drop index ... because constraint ... requires it"。所以先按约束名删一
    # 次（如果它根本不是约束，这一步是 no-op），删约束会连带删掉它背后的索引；再按索引
    # 名删一次兜底（如果上一步已经删掉了，这一步也是 no-op）。两种历史形态都能正确处理。
    # agent_id 可空后，旧的单一唯一索引也会让 ON CONFLICT 永不命中系统开销行，必须替换
    # 成下面两个部分唯一索引。
    op.execute(f"ALTER TABLE daily_token_usage DROP CONSTRAINT IF EXISTS {AGENT_UNIQUE_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {AGENT_UNIQUE_INDEX}")
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {AGENT_UNIQUE_INDEX} ON daily_token_usage "
        "(agent_id, date) WHERE system_scope IS NULL"
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {SYSTEM_UNIQUE_INDEX} ON daily_token_usage "
        "(tenant_id, system_scope, date) WHERE system_scope IS NOT NULL"
    )

    # 新口径把此前被丢弃的缓存与思考 token 算进来，数字会变大。默认只告警不拦截，
    # 避免上线即大面积误拦；由管理员显式切到 enforce。
    # value 用 dict 形状，与 system_settings 既有约定一致（见
    # app/dao/system_setting_dao.py 里 value.get("enabled") 的读法）。
    op.execute(
        "INSERT INTO system_settings (key, value) VALUES "
        "('token_budget_enforcement_mode', '{\"mode\": \"warn_only\"}'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )
    op.execute(
        "INSERT INTO system_settings (key, value) VALUES "
        "('token_accounting_calibration_switched_at', "
        "jsonb_build_object('at', now()::text)) ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM system_settings WHERE key IN "
        "('token_budget_enforcement_mode', 'token_accounting_calibration_switched_at')"
    )

    op.drop_index(SYSTEM_UNIQUE_INDEX, table_name="daily_token_usage")
    op.drop_index(AGENT_UNIQUE_INDEX, table_name="daily_token_usage")
    # 回滚前必须清掉系统开销行，否则 agent_id NOT NULL 与旧唯一索引都无法恢复。
    op.execute("DELETE FROM daily_token_usage WHERE system_scope IS NOT NULL")
    # 用 create_index 而不是 create_unique_constraint，还原成 020 迁移里裸
    # CREATE UNIQUE INDEX 的原始形态。
    op.create_index(
        AGENT_UNIQUE_INDEX,
        "daily_token_usage",
        ["agent_id", "date"],
        unique=True,
    )

    op.drop_constraint("daily_token_usage_agent_id_fkey", "daily_token_usage", type_="foreignkey")
    # 020_add_daily_token_usage.py 用裸 SQL `agent_id UUID NOT NULL REFERENCES
    # agents(id)` 建表，没有任何 ondelete 动作（即 NO ACTION）；还原时不引入比原始行为
    # 更激进的 CASCADE。
    op.create_foreign_key(
        "daily_token_usage_agent_id_fkey",
        "daily_token_usage",
        "agents",
        ["agent_id"],
        ["id"],
    )
    op.execute("DELETE FROM daily_token_usage WHERE agent_id IS NULL")
    op.alter_column(
        "daily_token_usage",
        "agent_id",
        existing_type=UUID(as_uuid=True),
        nullable=False,
    )

    op.drop_index("ix_daily_token_usage_system_scope", table_name="daily_token_usage")
    op.drop_column("daily_token_usage", "reasoning_tokens")
    op.drop_column("daily_token_usage", "system_scope")
    op.drop_column("daily_token_usage", "agent_name_snapshot")

    op.drop_table("tenant_token_counters")

    for name in reversed(TENANT_LIMIT_COLUMNS):
        op.drop_column("tenants", name)
    for name in reversed(AGENT_INPUT_COLUMNS):
        op.drop_column("agents", name)
