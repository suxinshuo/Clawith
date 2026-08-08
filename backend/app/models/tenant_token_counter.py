"""租户级 token 计数器 —— 服务租户日上限的热路径计数。

刻意不并入 tenants 表：那一行是被高频读取的配置行，每轮模型调用都去 UPDATE 它会
把配置读取和用量写入耦合到同一个热行上，并不断产生新的行版本。不设 *_month 列，
因为本次不做租户月上限，不留死字段。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TenantTokenCounter(Base):
    """Per-tenant rolling token counters for the tenant daily ceiling."""

    __tablename__ = "tenant_token_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tokens_used_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tokens_used_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_daily_reset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
