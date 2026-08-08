"""Activity log model for tracking agent actions."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentActivityLog(Base):
    """Records every action taken by a digital employee."""

    __tablename__ = "agent_activity_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(
        Enum(
            "chat_reply", "tool_call", "feishu_msg_sent", "agent_msg_sent",
            "web_msg_sent", "task_created", "task_updated", "file_written", "error",
            "schedule_run", "heartbeat", "plaza_post",
            name="activity_action_enum",
            create_constraint=False,
        ),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    detail_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    related_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

class DailyTokenUsage(Base):
    """Rolled up token consumption per agent per day for time-series analytics.

    `agent_id` 可空：租户级系统开销（群聊压缩 / 规划 / 连通性测试）没有归属 Agent。
    `ondelete=SET NULL` + `agent_name_snapshot` 让删除 Agent 不再抹掉历史租户用量。
    """

    __tablename__ = "daily_token_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    agent_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    system_scope: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # PostgreSQL 把唯一约束里的 NULL 视为互不相同，所以可空 agent_id 下
    # UNIQUE(agent_id, date) 的 ON CONFLICT 永远不会命中系统开销行，每次调用都会
    # 插新行、让聚合随调用次数虚增。拆成两个部分唯一索引避开这一点，同时不依赖
    # PostgreSQL 15 的 NULLS NOT DISTINCT。
    __table_args__ = (
        Index(
            "uq_daily_token_usage_agent_date",
            "agent_id",
            "date",
            unique=True,
            postgresql_where=text("system_scope IS NULL"),
        ),
        Index(
            "uq_daily_token_usage_system_date",
            "tenant_id",
            "system_scope",
            "date",
            unique=True,
            postgresql_where=text("system_scope IS NOT NULL"),
        ),
    )
