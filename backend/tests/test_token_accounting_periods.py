"""日/月周期边界按租户时区计算。

现在全按 UTC 算，Asia/Shanghai 租户的"今天"在北京时间早上 8 点翻页，与管理员看
日历的直觉不符。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.token_accounting.periods import (
    effective_timezone,
    is_new_local_day,
    is_new_local_month,
    local_day_start,
    local_month_start,
    tenant_timezone,
)

SHANGHAI = "Asia/Shanghai"
NEW_YORK = "America/New_York"


def test_local_day_start_is_the_utc_instant_of_local_midnight() -> None:
    now = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)  # 北京 8/7 00:30

    start = local_day_start(SHANGHAI, now=now)

    assert start == datetime(2026, 8, 6, 16, 0, tzinfo=UTC)
    assert start.tzinfo is UTC


def test_utc_1600_already_belongs_to_the_next_shanghai_day() -> None:
    """这就是切换时区语义要解决的那个具体问题。"""
    before = datetime(2026, 8, 6, 15, 59, tzinfo=UTC)
    after = datetime(2026, 8, 6, 16, 0, tzinfo=UTC)

    assert local_day_start(SHANGHAI, now=before) != local_day_start(SHANGHAI, now=after)


def test_local_day_start_handles_negative_offsets() -> None:
    now = datetime(2026, 8, 6, 3, 0, tzinfo=UTC)  # 纽约 8/5 23:00 (EDT)

    start = local_day_start(NEW_YORK, now=now)

    assert start == datetime(2026, 8, 5, 4, 0, tzinfo=UTC)


def test_local_month_start_uses_the_local_calendar_month() -> None:
    now = datetime(2026, 7, 31, 16, 30, tzinfo=UTC)  # 北京 8/1 00:30

    start = local_month_start(SHANGHAI, now=now)

    assert start == datetime(2026, 7, 31, 16, 0, tzinfo=UTC)


def test_invalid_timezone_falls_back_to_utc_instead_of_raising() -> None:
    """时区字段是自由文本，脏数据不能让记账整条路径崩掉。"""
    now = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)

    assert local_day_start("Not/AZone", now=now) == datetime(2026, 8, 6, 0, 0, tzinfo=UTC)


def test_is_new_local_day_detects_rollover() -> None:
    now = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)
    day_start = local_day_start(SHANGHAI, now=now)

    assert is_new_local_day(day_start - timedelta(seconds=1), SHANGHAI, now=now) is True
    assert is_new_local_day(day_start, SHANGHAI, now=now) is False
    assert is_new_local_day(now, SHANGHAI, now=now) is False


def test_is_new_local_day_treats_never_reset_as_new() -> None:
    now = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)

    assert is_new_local_day(None, SHANGHAI, now=now) is True


def test_is_new_local_day_accepts_naive_timestamps_as_utc() -> None:
    """历史行可能是 naive datetime，不能因此判成"永远需要重置"。"""
    now = datetime(2026, 8, 6, 16, 30, tzinfo=UTC)
    naive_after_start = datetime(2026, 8, 6, 16, 10)  # noqa: DTZ001 — 故意测试 naive 输入

    assert is_new_local_day(naive_after_start, SHANGHAI, now=now) is False


def test_is_new_local_month_detects_rollover() -> None:
    now = datetime(2026, 7, 31, 16, 30, tzinfo=UTC)  # 北京 8/1
    month_start = local_month_start(SHANGHAI, now=now)

    assert is_new_local_month(month_start - timedelta(seconds=1), SHANGHAI, now=now) is True
    assert is_new_local_month(month_start, SHANGHAI, now=now) is False
    assert is_new_local_month(None, SHANGHAI, now=now) is True


def test_effective_timezone_prefers_agent_then_tenant_then_utc() -> None:
    tenant = SimpleNamespace(timezone=SHANGHAI)

    assert effective_timezone(SimpleNamespace(timezone=NEW_YORK), tenant) == NEW_YORK
    assert effective_timezone(SimpleNamespace(timezone=None), tenant) == SHANGHAI
    assert effective_timezone(SimpleNamespace(timezone=None), None) == "UTC"


def test_tenant_timezone_ignores_any_agent_override() -> None:
    """租户级计数器只认租户时区。"""
    assert tenant_timezone(SimpleNamespace(timezone=SHANGHAI)) == SHANGHAI
    assert tenant_timezone(SimpleNamespace(timezone=None)) == "UTC"
    assert tenant_timezone(None) == "UTC"
