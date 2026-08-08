"""按时区计算日/月周期边界。纯函数，零 IO。

`now` 一律由调用方注入，模块内部不调 datetime.now() —— 否则这层就不是纯函数，
测试要靠 monkeypatch 时间。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.timezone_utils import get_agent_timezone_sync

UTC_NAME = "UTC"


def _zone(tz_name: str | None) -> ZoneInfo:
    """时区字段是自由文本，脏数据不能让记账整条路径崩掉。"""
    try:
        return ZoneInfo(tz_name or UTC_NAME)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(UTC_NAME)


def _as_utc(value: datetime) -> datetime:
    """历史行可能是 naive datetime，按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def local_day_start(tz_name: str, *, now: datetime) -> datetime:
    """本地零点对应的 UTC 时刻，用作 DailyTokenUsage.date 的锚点。"""
    zone = _zone(tz_name)
    local_now = _as_utc(now).astimezone(zone)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(UTC)


def local_month_start(tz_name: str, *, now: datetime) -> datetime:
    zone = _zone(tz_name)
    local_now = _as_utc(now).astimezone(zone)
    local_first = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local_first.astimezone(UTC)


def is_new_local_day(
    last_reset_utc: datetime | None,
    tz_name: str,
    *,
    now: datetime,
) -> bool:
    if last_reset_utc is None:
        return True
    return _as_utc(last_reset_utc) < local_day_start(tz_name, now=now)


def is_new_local_month(
    last_reset_utc: datetime | None,
    tz_name: str,
    *,
    now: datetime,
) -> bool:
    if last_reset_utc is None:
        return True
    return _as_utc(last_reset_utc) < local_month_start(tz_name, now=now)


def effective_timezone(agent, tenant=None) -> str:
    """Agent 的有效时区：agent.timezone -> tenant.timezone -> UTC。"""
    return get_agent_timezone_sync(agent, tenant)


def tenant_timezone(tenant) -> str:
    """租户级计数器只认租户时区，忽略任何 Agent 覆盖。"""
    if tenant is not None and getattr(tenant, "timezone", None):
        return tenant.timezone
    return UTC_NAME


__all__ = [
    "UTC_NAME",
    "effective_timezone",
    "is_new_local_day",
    "is_new_local_month",
    "local_day_start",
    "local_month_start",
    "tenant_timezone",
]
