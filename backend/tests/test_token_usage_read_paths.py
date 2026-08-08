"""缓存命中率的分母。

旧算法是 `cache_read / total_tokens`，分母含 output token —— 而 output 按定义不可能
被缓存读取，所以五处读取路径的显示值系统性偏低。正确分母是输入总量（含缓存）。

本文件的端点测试一律用假 session（本仓库测试不连真实数据库），并且刻意挑
"错分母会算出不同数字" 的样本，这样断言才有鉴别力：tokens_used=1000 /
input=800 / cache_read=600 时，正确答案 0.75，旧算法 0.6。

假行只能证明 Python 侧的算术，证不了"分母取的是哪一列 SQL" —— 假行随便挂一个叫
input_tokens 的属性就能骗过去。所以聚合类站点额外断言**代码真正编译出来的 SQL**：
`RoutingSession` 把每条 statement 的文本和对象都留下来，测试直接查里面有没有
`sum(agents.input_tokens_today)`、CASE 边界是哪几个锚点。断言的是代码构造出的 SQL
（行为），不是源码文本。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api import admin, advanced, tenants
from app.schemas.schemas import AgentOut
from app.services.token_accounting.periods import local_day_start, local_month_start
from app.services.token_accounting.rates import cache_hit_rate, estimated_share

AGENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

# 冻结时刻刻意选在 UTC 与 Asia/Shanghai 不同一天的窗口内（03:00Z = 上海 11:00，
# 上海的"今天"从前一个 UTC 日的 16:00Z 起算），这样"读取侧解析错时区"会算出不同的
# 窗口边界，断言才有鉴别力。
FROZEN_NOW = datetime(2026, 8, 8, 3, 0, tzinfo=UTC)
TENANT_TZ = "Asia/Shanghai"


class _FrozenDatetime(datetime):
    """冻结 advanced.datetime.now()，让窗口锚点可以精确断言而不跟真实时钟赛跑。"""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


def _always(value):
    async def _call(*args, **kwargs):
        return value

    return _call


# ─── 纯函数 ─────────────────────────────────────────────


def test_hit_rate_denominator_is_input_not_total() -> None:
    # 输入 100k（其中命中 90k），输出 10k。旧算法得 90k/110k = 0.818，偏低。
    assert cache_hit_rate(90_000, 100_000) == 0.9


def test_hit_rate_is_zero_when_there_is_no_cache_read() -> None:
    """0.0 的含义只是"命中读取占输入的 0%"，不能反推"这个 Agent 没用过 token"。

    input=0 且 cache_read=0 时 0/0 记为 0%；input>0 而 cache_read=0（从不用 prompt
    caching）同样是真实的 0%。
    """
    assert cache_hit_rate(0, 0) == 0.0
    assert cache_hit_rate(None, None) == 0.0
    assert cache_hit_rate(0, 1_000) == 0.0


def test_hit_rate_is_indeterminate_for_pre_calibration_history() -> None:
    """cache_read > input 只可能来自口径校准之前的历史数据，此时答案是"算不出来"。

    Task 4 的迁移给 `agents.input_tokens_*` 建列时 DEFAULT 0 且**不回填**（历史
    `daily_token_usage.input_tokens` 里 Anthropic 口径不含缓存计数、OpenAI 口径含，
    且没有逐行记录协议，不存在正确的回填算法）。所以上线当天每个存量 Agent 都是
    `input_tokens_total = 0` 而 `cache_read_tokens_total` 很大。

    这种行夹到 1.0 会显示成自信的"100% 命中"，退回 0.0 会显示成自信的"0% 命中"，
    两者都是编出来的数字。必须返回 None，让前端渲染成"—"。
    """
    assert cache_hit_rate(5_000, 0) is None
    assert cache_hit_rate(320, 100) is None
    assert cache_hit_rate(1, None) is None


def test_hit_rate_is_rounded_to_four_places() -> None:
    assert cache_hit_rate(1, 3) == 0.3333


def test_hit_rate_accepts_full_hit() -> None:
    assert cache_hit_rate(100, 100) == 1.0


def test_estimated_share_reports_how_much_is_a_guess() -> None:
    assert estimated_share(250, 1_000) == 0.25
    assert estimated_share(0, 0) == 0.0


def test_estimated_share_clamps_because_overflow_would_be_our_own_bug() -> None:
    """estimated 是 total 的子集，由同一段代码在同一事务里写入。

    比值 > 1 只能是我们自己写入侧的 bug，而不是跨口径的历史数据，所以这里夹到 1.0
    是安全的 —— 与 cache_hit_rate 返回 None 的不对称是刻意的。
    """
    assert estimated_share(2_000, 1_000) == 1.0


# ─── 假 session 基建 ────────────────────────────────────


class FakeResult:
    """一个结果对象同时支持 scalar / one / all / scalars().all()。"""

    def __init__(self, *, scalar=None, one=None, rows=None):
        self._scalar = scalar
        self._one = one
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar

    def one(self):
        return self._one

    def all(self):
        return self._rows

    def scalars(self):
        return self


class RoutingSession:
    """按 SQL 文本分发假结果，避免依赖端点内部的 execute 调用顺序。

    同时留下每条 statement 的文本（`seen`）和对象（`statements`），供"分母取的是哪一
    列"和"窗口边界锚在哪"这类只能从编译出的 SQL 上验证的断言使用。
    """

    def __init__(self, routes: list[tuple[tuple[str, ...], FakeResult]], default: FakeResult):
        self._routes = routes
        self._default = default
        self.seen: list[str] = []
        self.statements: list = []

    async def execute(self, statement, *args, **kwargs):
        text = str(statement).lower()
        self.seen.append(text)
        self.statements.append(statement)
        for markers, result in self._routes:
            if all(marker in text for marker in markers):
                return result
        return self._default


def _only_statement_text(session: RoutingSession, *markers: str) -> str:
    """取唯一一条匹配 markers 的 SQL 文本；匹配到 0 条或多条都算测试自己写错了。"""
    hits = [text for text in session.seen if all(marker in text for marker in markers)]
    assert len(hits) == 1, f"expected exactly 1 statement matching {markers}, got {len(hits)}"
    return hits[0]


def _only_statement(session: RoutingSession, *markers: str):
    hits = [
        stmt
        for stmt in session.statements
        if all(marker in str(stmt).lower() for marker in markers)
    ]
    assert len(hits) == 1, f"expected exactly 1 statement matching {markers}, got {len(hits)}"
    return hits[0]


def _datetime_binds(statement) -> list[datetime]:
    """编译出的 SQL 里按出现顺序排列的 datetime 绑定参数。

    窗口边界是 bind param，str(statement) 里只看得到 `:date_1` 这种占位符，所以只能
    从 compile().params 上取实际值。顺序与 select() 里列的顺序一致。
    """
    compiled = statement.compile()
    return [value for value in compiled.params.values() if isinstance(value, datetime)]


# ─── /agents/{id}/metrics ───────────────────────────────


def _fake_agent(**overrides) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": AGENT_ID,
        "tenant_id": TENANT_ID,
        "name": "tester",
        "status": "idle",
        "container_id": None,
        "timezone": "Asia/Shanghai",
        "tokens_used_today": 1_000,
        "tokens_used_month": 2_000,
        "tokens_used_total": 4_000,
        "input_tokens_today": 800,
        "input_tokens_month": 1_600,
        "input_tokens_total": 3_200,
        "cache_read_tokens_today": 600,
        "cache_read_tokens_month": 1_200,
        "cache_read_tokens_total": 2_400,
        "cache_creation_tokens_today": 10,
        "cache_creation_tokens_month": 20,
        "cache_creation_tokens_total": 40,
        "max_tokens_per_day": None,
        "max_tokens_per_month": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _metrics_session(*, share_row=None, tenant=None) -> RoutingSession:
    row = share_row or SimpleNamespace(
        est_today=250,
        used_today=1_000,
        est_month=200,
        used_month=2_000,
        est_total=400,
        used_total=4_000,
    )
    return RoutingSession(
        [
            (("daily_token_usage",), FakeResult(one=row)),
            (("from tenants",), FakeResult(scalar=tenant)),
        ],
        default=FakeResult(scalar=0),
    )


def _patch_metrics(monkeypatch, agent) -> None:
    async def fake_access(db, user, agent_id):
        return agent, None

    monkeypatch.setattr(advanced, "check_agent_access", fake_access)
    monkeypatch.setattr(advanced, "datetime", _FrozenDatetime)


async def _call_metrics(monkeypatch, agent, *, share_row=None, tenant=None, session=None) -> dict:
    _patch_metrics(monkeypatch, agent)
    monkeypatch.setattr(
        advanced,
        "system_setting_dao",
        SimpleNamespace(get_value=_always({"at": "2026-08-06T00:00:00+00:00"})),
    )
    monkeypatch.setattr(advanced, "current_enforcement_mode", _always("warn_only"))

    return await advanced.get_agent_metrics(
        agent_id=AGENT_ID,
        current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=TENANT_ID),
        db=session if session is not None else _metrics_session(share_row=share_row, tenant=tenant),
    )


@pytest.mark.asyncio
async def test_agent_metrics_hit_rate_uses_input_denominator(monkeypatch) -> None:
    agent = _fake_agent()
    tokens = (await _call_metrics(monkeypatch, agent))["tokens"]

    # 600/800 = 0.75；旧分母 600/1000 = 0.6。
    assert tokens["cache_hit_rate_today"] == 0.75
    assert tokens["cache_hit_rate_month"] == 0.75
    assert tokens["cache_hit_rate_total"] == 0.75
    assert tokens["input_today"] == 800
    assert tokens["input_month"] == 1_600
    assert tokens["input_total"] == 3_200


@pytest.mark.asyncio
async def test_agent_metrics_hit_rate_is_null_for_pre_calibration_agent(monkeypatch) -> None:
    """迁移不回填 input_tokens_*，存量 Agent 只能显示"算不出来"。"""
    agent = _fake_agent(input_tokens_total=0, cache_read_tokens_total=5_000)
    tokens = (await _call_metrics(monkeypatch, agent))["tokens"]

    assert tokens["cache_hit_rate_total"] is None


@pytest.mark.asyncio
async def test_agent_metrics_exposes_estimated_share_and_calibration(monkeypatch) -> None:
    tokens = (await _call_metrics(monkeypatch, _fake_agent()))["tokens"]

    assert tokens["estimated_share_today"] == 0.25
    assert tokens["estimated_share_month"] == 0.1
    assert tokens["estimated_share_total"] == 0.1
    assert tokens["calibration_switched_at"] == "2026-08-06T00:00:00+00:00"
    assert tokens["budget_enforcement_mode"] == "warn_only"


@pytest.mark.asyncio
async def test_agent_metrics_reports_the_basis_the_share_was_computed_against(monkeypatch) -> None:
    """share 的分母是 daily_token_usage 汇总，不是同一个 JSON 里的 used_*。

    020 建 daily_token_usage、050 加 estimated_tokens 都不回填，所以比这两次迁移更老
    的 Agent 永远有 SUM(daily_token_usage.tokens_used) < Agent.tokens_used_total。不把
    basis 一起返回，调用方就会拿 share 乘一个不相干的 used_* 得出错误的绝对量。
    """
    # 三个窗口的汇总值刻意与 Agent 计数器全不相同，否则"basis 其实读的是 used_*"这种
    # 写法会蒙对。
    share_row = SimpleNamespace(
        est_today=70,
        used_today=700,
        est_month=300,
        used_month=1_500,
        est_total=1_000,
        used_total=25_000,
    )
    agent = _fake_agent(
        tokens_used_today=1_000, tokens_used_month=2_000, tokens_used_total=10_000_000
    )
    tokens = (await _call_metrics(monkeypatch, agent, share_row=share_row))["tokens"]

    assert tokens["estimated_basis_today"] == 700
    assert tokens["estimated_basis_month"] == 1_500
    assert tokens["estimated_basis_total"] == 25_000
    # basis 与 used_* 是两个不同的量 —— total 这一档差了两个数量级，正是必须分开暴露的
    # 原因：拿 share 去乘 used_total 会得出一个完全错误的绝对量。
    assert tokens["used_today"] == 1_000
    assert tokens["used_month"] == 2_000
    assert tokens["used_total"] == 10_000_000


@pytest.mark.asyncio
async def test_agent_metrics_estimated_share_comes_from_daily_rollup(monkeypatch) -> None:
    """没有 Agent 级估算计数列，占比只能从 daily_token_usage 汇总来，且只查一趟。"""
    agent = _fake_agent()
    session = _metrics_session()
    _patch_metrics(monkeypatch, agent)
    monkeypatch.setattr(advanced, "system_setting_dao", SimpleNamespace(get_value=_always({})))
    monkeypatch.setattr(advanced, "current_enforcement_mode", _always("enforce"))

    result = await advanced.get_agent_metrics(
        agent_id=AGENT_ID,
        current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=TENANT_ID),
        db=session,
    )

    rollup_queries = [text for text in session.seen if "daily_token_usage" in text]
    assert len(rollup_queries) == 1, "三个窗口必须在同一趟查询里用 CASE 拆开"
    assert result["tokens"]["calibration_switched_at"] is None


@pytest.mark.asyncio
async def test_agent_metrics_window_anchors_resolve_the_tenant_timezone(monkeypatch) -> None:
    """agent.timezone 为 NULL 时，读取侧必须解析出**租户**时区，不能回落到 UTC。

    写入侧 ledger._write_once() 用 effective_timezone(agent, tenant) 定
    daily_token_usage.date 的锚点。读取侧只传 agent 就会回落到 UTC：租户为
    Asia/Shanghai 时写入侧把"今天"锚在 16:00Z、读取侧却按 00:00Z 筛，今天整行被排除，
    estimated_share_today 静默读成 0。所以这里断言的是**实际编译进 SQL 的边界值**。
    """
    agent = _fake_agent(timezone=None)
    session = _metrics_session(tenant=SimpleNamespace(timezone=TENANT_TZ))
    await _call_metrics(monkeypatch, agent, session=session)

    tenant_day = local_day_start(TENANT_TZ, now=FROZEN_NOW)
    tenant_month = local_month_start(TENANT_TZ, now=FROZEN_NOW)
    utc_day = local_day_start("UTC", now=FROZEN_NOW)

    boundaries = _datetime_binds(_only_statement(session, "daily_token_usage"))
    assert set(boundaries) == {tenant_day, tenant_month}
    # 前提检查：如果这两个锚点碰巧相同，上面的断言就废了。
    assert tenant_day != utc_day
    assert utc_day not in boundaries


@pytest.mark.asyncio
async def test_agent_metrics_each_window_gets_its_own_anchor(monkeypatch) -> None:
    """三个窗口必须分别锚在 day_start / month_start / 无边界上。

    假 share_row 是按窗口预先贴好标签的，所以只看返回值的话，窗口边界可以任意写错而
    测试全绿。只能断言编译出的 SQL：CASE 的边界按 select 列顺序应当是
    [day, day, month, month]，total 那两列完全没有边界（所以只有 4 个 datetime 绑定）。
    """
    agent = _fake_agent(timezone=None)
    session = _metrics_session(tenant=SimpleNamespace(timezone=TENANT_TZ))
    await _call_metrics(monkeypatch, agent, session=session)

    day = local_day_start(TENANT_TZ, now=FROZEN_NOW)
    month = local_month_start(TENANT_TZ, now=FROZEN_NOW)
    assert day != month

    boundaries = _datetime_binds(_only_statement(session, "daily_token_usage"))
    assert boundaries == [day, day, month, month], (
        "顺序对应 est_today/used_today/est_month/used_month；est_total/used_total 无边界"
    )


# ─── /admin/metrics/timeseries ──────────────────────────


def _timeseries_session(*, tokens_used: int, input_tokens: int, cache_read: int, day) -> RoutingSession:
    return RoutingSession(
        [
            (("day_series",), FakeResult(rows=[(day, 3, 7)])),
            (
                ("daily_token_usage", "tokens_used"),
                FakeResult(
                    rows=[
                        SimpleNamespace(
                            d=day, c=tokens_used, input_tokens=input_tokens, cache_read=cache_read
                        )
                    ]
                ),
            ),
            (("daily_token_usage",), FakeResult(rows=[SimpleNamespace(d=day, cache_read=cache_read)])),
            (
                ("chat_sessions", "distinct"),
                FakeResult(rows=[SimpleNamespace(d=day, sessions=2, dau=1)]),
            ),
        ],
        default=FakeResult(scalar=0, rows=[]),
    )


async def _call_timeseries(monkeypatch, session, *, day, calibration) -> list[dict]:
    monkeypatch.setattr(admin, "system_setting_dao", SimpleNamespace(get_value=_always(calibration)))
    return await admin.get_platform_timeseries(
        start_date=day,
        end_date=day,
        current_user=SimpleNamespace(id=uuid.uuid4()),
        db=session,
    )


@pytest.mark.asyncio
async def test_platform_timeseries_hit_rate_uses_input_denominator(monkeypatch) -> None:
    day = datetime(2026, 8, 7, tzinfo=UTC)
    session = _timeseries_session(tokens_used=1_000, input_tokens=800, cache_read=600, day=day.date())

    rows = await _call_timeseries(
        monkeypatch, session, day=day, calibration={"at": "2026-08-01 00:00:00+00"}
    )

    assert len(rows) == 1
    # 600/800 = 0.75；旧分母 600/1000 = 0.6。
    assert rows[0]["cache_hit_rate"] == 0.75


@pytest.mark.asyncio
async def test_platform_timeseries_aggregate_selects_the_input_column(monkeypatch) -> None:
    """假行随便挂一个叫 input_tokens 的属性就能骗过算术断言，所以查 SQL。"""
    day = datetime(2026, 8, 7, tzinfo=UTC)
    session = _timeseries_session(tokens_used=1_000, input_tokens=800, cache_read=600, day=day.date())
    await _call_timeseries(monkeypatch, session, day=day, calibration={"at": "2026-08-01 00:00:00+00"})

    text = _only_statement_text(session, "daily_token_usage", "tokens_used")
    assert "sum(daily_token_usage.input_tokens)" in text


@pytest.mark.asyncio
async def test_platform_timeseries_hit_rate_is_null_before_the_calibration_switch(monkeypatch) -> None:
    """校准之前的日汇总行分母是旧口径，比值偏高且 None 兜底不会触发，必须显式压成 None。

    `daily_token_usage.input_tokens` 不是新列：050 迁移就把它建成 NOT NULL DEFAULT 0
    且不回填，当时 Anthropic 分支写入的 usage["input_tokens"] 不含任何缓存计数。这一天
    input=200k / cache_read=180k / output=50k：直接算得 0.9，改动前的旧算法算得 0.72，
    真实值 180k/(200k+180k)=0.4737。0.9 是个"自信的错数"，而且比改动前更糟。
    """
    day = datetime(2026, 8, 7, tzinfo=UTC)
    session = _timeseries_session(
        tokens_used=250_000, input_tokens=200_000, cache_read=180_000, day=day.date()
    )

    rows = await _call_timeseries(
        monkeypatch, session, day=day, calibration={"at": "2026-08-20 00:00:00+00"}
    )

    assert cache_hit_rate(180_000, 200_000) == 0.9, "前提：不加闸门就会报出 0.9"
    assert rows[0]["cache_hit_rate"] is None


@pytest.mark.asyncio
async def test_platform_timeseries_hit_rate_is_null_on_the_switch_day_itself(monkeypatch) -> None:
    """切换当天的日汇总行里新旧语义混在同一个 input_tokens 列里，同样不可信。"""
    day = datetime(2026, 8, 7, tzinfo=UTC)
    session = _timeseries_session(tokens_used=1_000, input_tokens=800, cache_read=600, day=day.date())

    rows = await _call_timeseries(
        monkeypatch, session, day=day, calibration={"at": "2026-08-07 09:30:00+00"}
    )

    assert rows[0]["cache_hit_rate"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("calibration", [{}, None, "2026-08-01", {"at": None}, {"at": "garbage"}])
async def test_platform_timeseries_fails_open_to_null_when_calibration_is_unreadable(
    monkeypatch, calibration
) -> None:
    """设置行缺失或被改坏时，宁可什么都不显示，也不能默认信任跨口径的旧数据。"""
    day = datetime(2026, 8, 7, tzinfo=UTC)
    session = _timeseries_session(tokens_used=1_000, input_tokens=800, cache_read=600, day=day.date())

    rows = await _call_timeseries(monkeypatch, session, day=day, calibration=calibration)

    assert rows[0]["cache_hit_rate"] is None


# ─── /admin/metrics/leaderboards ────────────────────────


def _leaderboards_session(*, company_rows, agent_rows) -> RoutingSession:
    return RoutingSession(
        [
            (("tenants.name", "sum(agents.tokens_used_total)"), FakeResult(rows=company_rows)),
            (("agents.name",), FakeResult(rows=agent_rows)),
        ],
        default=FakeResult(rows=[]),
    )


def _company(name, *, total, cache_read, input_tokens, uncalibrated=0) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        total=total,
        cache_read=cache_read,
        input_tokens=input_tokens,
        uncalibrated_agents=uncalibrated,
    )


@pytest.mark.asyncio
async def test_leaderboards_hit_rate_uses_input_denominator() -> None:
    company_rows = [
        _company("Acme", total=1_000, cache_read=600, input_tokens=800),
        _company("Legacy", total=5_000, cache_read=4_000, input_tokens=0, uncalibrated=1),
    ]
    agent_rows = [
        SimpleNamespace(
            name="tester",
            tenant_name="Acme",
            tokens_used_total=1_000,
            cache_read_tokens_total=600,
            input_tokens_total=800,
        ),
        SimpleNamespace(
            name="legacy-bot",
            tenant_name="Legacy",
            tokens_used_total=5_000,
            cache_read_tokens_total=4_000,
            input_tokens_total=0,
        ),
    ]
    session = _leaderboards_session(company_rows=company_rows, agent_rows=agent_rows)

    result = await admin.get_platform_leaderboards(
        current_user=SimpleNamespace(id=uuid.uuid4()),
        db=session,
    )

    # 600/800 = 0.75；旧分母 600/1000 = 0.6。
    assert result["top_companies"][0]["cache_hit_rate"] == 0.75
    assert result["top_companies"][1]["cache_hit_rate"] is None
    assert result["top_agents"][0]["cache_hit_rate"] == 0.75
    assert result["top_agents"][1]["cache_hit_rate"] is None


@pytest.mark.asyncio
async def test_leaderboards_company_aggregate_selects_the_input_column() -> None:
    session = _leaderboards_session(
        company_rows=[_company("Acme", total=1_000, cache_read=600, input_tokens=800)],
        agent_rows=[],
    )
    await admin.get_platform_leaderboards(
        current_user=SimpleNamespace(id=uuid.uuid4()),
        db=session,
    )

    text = _only_statement_text(session, "tenants.name", "sum(agents.tokens_used_total)")
    assert "sum(agents.input_tokens_total)" in text


@pytest.mark.asyncio
async def test_leaderboards_company_hit_rate_is_null_when_the_sum_mixes_calibrations() -> None:
    """SUM 会把"算不出来"洗成一个像样的数字，所以有污染就整格返回 None。

    组内 A 是校准前存量（只贡献 cache_read）、B 从不用 prompt caching（只贡献
    input），SUM 后得到 1M/3M = 0.3333 —— 组里没有任何一个 Agent 有 33% 的命中率。
    """
    session = _leaderboards_session(
        company_rows=[
            _company("Mixed", total=4_000_000, cache_read=1_000_000, input_tokens=3_000_000, uncalibrated=1)
        ],
        agent_rows=[],
    )
    result = await admin.get_platform_leaderboards(
        current_user=SimpleNamespace(id=uuid.uuid4()),
        db=session,
    )

    assert cache_hit_rate(1_000_000, 3_000_000) == 0.3333, "前提：不加闸门就会报出 0.3333"
    assert result["top_companies"][0]["cache_hit_rate"] is None


@pytest.mark.asyncio
async def test_leaderboards_company_aggregate_counts_uncalibrated_agents_in_sql() -> None:
    """污染计数必须和 SUM 在同一趟聚合里出，否则它判断的不是同一批行。"""
    session = _leaderboards_session(
        company_rows=[_company("Acme", total=1_000, cache_read=600, input_tokens=800)],
        agent_rows=[],
    )
    await admin.get_platform_leaderboards(
        current_user=SimpleNamespace(id=uuid.uuid4()),
        db=session,
    )

    text = _only_statement_text(session, "tenants.name", "sum(agents.tokens_used_total)")
    assert "agents.input_tokens_total = " in text
    assert "agents.cache_read_tokens_total > " in text


# ─── /tenants/me/token-usage ────────────────────────────


def _tenant_usage_row(**overrides) -> SimpleNamespace:
    values: dict[str, object] = {
        "tokens_today": 1_000,
        "tokens_month": 2_000,
        "tokens_total": 4_000,
        "input_today": 800,
        "input_month": 1_600,
        "input_total": 0,
        "cache_today": 600,
        "cache_month": 1_200,
        "cache_total": 2_400,
        "cache_creation_today": 10,
        "cache_creation_month": 20,
        "cache_creation_total": 40,
        "uncal_today": 0,
        "uncal_month": 0,
        "uncal_total": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _call_tenant_usage(session):
    return await tenants.get_my_tenant_token_usage(
        current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=TENANT_ID),
        db=session,
    )


@pytest.mark.asyncio
async def test_tenant_token_usage_hit_rate_uses_input_denominator() -> None:
    session = RoutingSession([], default=FakeResult(one=_tenant_usage_row()))
    result = await _call_tenant_usage(session)

    # 600/800 = 0.75；旧分母 600/1000 = 0.6。
    assert result["today"]["cache_hit_rate"] == 0.75
    assert result["today"]["input_tokens"] == 800
    assert result["month"]["cache_hit_rate"] == 0.75
    # input_total 未回填而 cache_total 累计了全部历史 → 算不出来。
    assert result["total"]["cache_hit_rate"] is None
    assert result["total"]["input_tokens"] == 0


@pytest.mark.asyncio
async def test_tenant_token_usage_aggregate_selects_the_input_columns_per_window() -> None:
    """每个窗口必须取自己那一列 input，拿 _total 顶替 _today/_month 也算错。"""
    session = RoutingSession([], default=FakeResult(one=_tenant_usage_row()))
    await _call_tenant_usage(session)

    text = _only_statement_text(session, "agents.tenant_id")
    assert "sum(agents.input_tokens_today)" in text
    assert "sum(agents.input_tokens_month)" in text
    assert "sum(agents.input_tokens_total)" in text


@pytest.mark.asyncio
async def test_tenant_token_usage_hit_rate_is_null_when_the_sum_mixes_calibrations() -> None:
    """A 校准前（只贡献分子）+ B 从不用 prompt caching（只贡献分母）→ SUM 得 0.3333。

    组里没有任何一个 Agent 有 33% 的命中率，这个数字纯粹是两个"算不出来 / 0%"相加洗
    出来的。污染计数非 0 就整格返回 None。
    """
    row = _tenant_usage_row(
        tokens_total=5_000_000,
        input_total=3_000_000,
        cache_total=1_000_000,
        uncal_total=1,
    )
    result = await _call_tenant_usage(RoutingSession([], default=FakeResult(one=row)))

    assert cache_hit_rate(1_000_000, 3_000_000) == 0.3333, "前提：不加闸门就会报出 0.3333"
    assert result["total"]["cache_hit_rate"] is None
    # 污染只压掉被污染的那个窗口，其余窗口照常给数字。
    assert result["today"]["cache_hit_rate"] == 0.75


@pytest.mark.asyncio
async def test_tenant_token_usage_counts_uncalibrated_agents_per_window_in_sql() -> None:
    """污染计数必须用窗口自己的列，不能三个桶都用 _total。"""
    session = RoutingSession([], default=FakeResult(one=_tenant_usage_row()))
    await _call_tenant_usage(session)

    text = _only_statement_text(session, "agents.tenant_id")
    for window in ("today", "month", "total"):
        assert f"agents.input_tokens_{window} = " in text, window
        assert f"agents.cache_read_tokens_{window} > " in text, window


# ─── Agent 响应模型 ─────────────────────────────────────


def test_agent_schema_exposes_input_counters() -> None:
    """前端从 agent 对象算比率，拿不到 input 就算不出正确分母。"""
    fields = AgentOut.model_fields
    for name in ("input_tokens_today", "input_tokens_month", "input_tokens_total"):
        assert name in fields, name
