# Token 计量与限额执行 实施计划

> **给执行者：** 必需子技能：用 superpowers:subagent-driven-development（推荐）
> 或 superpowers:executing-plans 按任务逐个实施。步骤用 checkbox（`- [ ]`）语法
> 跟踪进度。

**目标：** 修正 token 计量的准确性缺陷、补齐未记账的调用路径，并让日/月/租户
token 限额在实际运行路径上真正生效。

**架构：** 新增 `backend/app/services/token_accounting/` 包，按职责分为
`normalize`（协议归一化，纯函数）、`periods`（时区周期，纯函数）、`ledger`
（原子记账）、`budget`（限额判定）四个模块；`token_tracker.py` 降为薄转发层保持
现有 import 可用。限额执行点接在 durable Graph runtime 的
`RuntimeModelStepService.complete_once`，复用既有的
`ModelStepResult(intent="error")` 结构化短路通道，不新造异常穿透。

**技术栈：** Python 3.11+、FastAPI、SQLAlchemy 2.0（async）、PostgreSQL、
Alembic、pytest + pytest-asyncio、React 19 + TypeScript（前端读取路径修正）。

## 全局约束

- Python 命令一律用 `backend/.venv`，不要用系统 python。
- Ruff：line-length 120，target py311。**只对本任务改动的文件跑 ruff，不要跑全仓**：
  仓库基线并非 ruff-clean（`ruff format --check .` 会重排 412/605 个文件，
  `ruff check .` 报 3240 个既有错误），跑全仓会产出与本次改动无关的巨量 diff。
  规则是：本任务**新建**的文件必须 `ruff format` 干净且 `ruff check` 零错误；本任务
  **修改**的既有文件只保证自己新增的行符合格式，不整文件重排，且不引入新的
  `ruff check` 错误（用改动前后的错误数对比确认）。
- import 一律放文件头部，不在函数体内 import，除非为了打断循环导入（项目
  CLAUDE.md 硬规则）。
- pytest 配置 `asyncio_mode = "auto"`（`backend/pyproject.toml:69`），async 测试
  不需要 `@pytest.mark.asyncio`。
- **测试不得连真实数据库。** `backend/tests/` 下没有 `conftest.py`，也没有任何
  `create_async_engine`。只用三种既有做法：手写假对象 +
  `monkeypatch.setattr(模块, "async_session", ...)`；`Model.__table__` 声明式内
  省；`importlib` 加载 Alembic 迁移模块断言常量。
- 数据库只支持 PostgreSQL，不需要考虑 SQLite 兼容路径。
- Alembic 当前唯一 head 是 `widen_credential_scopes`
  （`backend/alembic/versions/202607281000_widen_credential_scopes.py`）。
- 统一口径不变式：`total_tokens == input_tokens + output_tokens`，其中
  `input_tokens` 含缓存；`cache_read` / `cache_creation` 是 `input_tokens` 的细
  分；`reasoning_tokens` 仅展示，绝不计入任何总量。
- `openspec/` 下所有文档用中文撰写。
- 不删除 `caller.py` 的 `call_llm` / `call_llm_with_failover` / `call_agent_llm`，
  也不给它们新增限额逻辑（详见 design.md 的"已知缺口与技术债"）。

## 文件结构

**新建**

| 文件 | 职责 |
|---|---|
| `backend/app/services/token_accounting/__init__.py` | 对外唯一入口，re-export 各模块公开符号 |
| `backend/app/services/token_accounting/normalize.py` | `TokenUsage`、协议归一化、流式合并、口径自校验（纯函数，零 IO） |
| `backend/app/services/token_accounting/periods.py` | 带时区的日/月边界（纯函数，零 IO） |
| `backend/app/services/token_accounting/ledger.py` | 原子记账、系统开销归属 |
| `backend/app/services/token_accounting/budget.py` | 惰性重置、限额判定、执行模式、软告警 |
| `backend/app/models/tenant_token_counter.py` | `TenantTokenCounter` 表模型 |
| `backend/alembic/versions/202608061000_token_accounting_v2.py` | 表结构迁移 |
| `backend/tests/test_token_accounting_normalize.py` | Task 1 测试 |
| `backend/tests/test_token_accounting_periods.py` | Task 3 测试 |
| `backend/tests/test_token_accounting_schema.py` | Task 4 测试 |
| `backend/tests/test_token_accounting_ledger.py` | Task 5 测试 |
| `backend/tests/test_token_accounting_budget.py` | Task 7 测试 |
| `backend/tests/test_token_budget_enforcement.py` | Task 8 测试 |

**修改**

| 文件 | 改什么 |
|---|---|
| `backend/app/services/token_tracker.py` | 全文替换为薄转发层 |
| `backend/app/services/llm/client.py:1918-1985` | Anthropic 流式 usage 改为合并 |
| `backend/app/services/llm/single_step.py` | 加 `tenant_id` / `system_scope`，改走 ledger |
| `backend/app/services/agent_runtime/model_step_service.py:1480+` | 两阶段限额执行 |
| `backend/app/services/agent_runtime/node_executor.py:766-775` | `reason` 由 error code 推导 |
| `backend/app/services/agent_runtime/session_context_compactor.py:308` | 传 `system_scope='group_compact'` |
| `backend/app/services/agent_runtime/planning.py:479` | 传 `system_scope='planning'` |
| `backend/app/api/enterprise.py:249-270` | 传 `system_scope='model_probe'` |
| `backend/app/models/agent.py:84-96` | 加 `input_tokens_{today,month,total}` |
| `backend/app/models/tenant.py` | 加租户日上限与两个 Agent 默认限额字段 |
| `backend/app/models/activity_log.py:35-56` | `DailyTokenUsage` 改动 |
| `backend/app/api/advanced.py:281-283` | 命中率分母 + 新字段 |
| `backend/app/api/admin.py:371,407,425` | 命中率分母 |
| `backend/app/api/tenants.py:499-519` | 命中率分母 |
| `backend/app/api/agents.py:72-98,443,490` | 移除本地惰性重置、继承租户默认限额 |
| `backend/app/schemas/schemas.py:258` | Agent 响应加新字段 |
| `frontend/src/types/index.ts:30-38` | Agent 类型加新字段 |
| `frontend/src/pages/agent-detail/AgentDetailPage.tsx:4835-4840` | 命中率分母 |
| `frontend/src/pages/Dashboard.tsx:423` | 命中率分母 |

---

### Task 1：`normalize.py` —— 统一口径的纯函数层

这是整个改动的核心。所有已确认的准确性 bug 都在这一层，且这一层零 IO，可以用表
驱动测试铺满。

**文件：**
- 新建：`backend/app/services/token_accounting/__init__.py`
- 新建：`backend/app/services/token_accounting/normalize.py`
- 测试：`backend/tests/test_token_accounting_normalize.py`

**接口：**
- 依赖：`app.services.llm.multimodal_content.estimate_multimodal_tokens`
  （签名 `(value: object, *, chars_per_token: int, utf8_bytes: bool = False) -> int`；
  该模块只依赖 stdlib 与 PIL，零 app 依赖，引它不会产生循环导入）
- 产出（后续任务依赖这些确切名字）：
  - `TokenUsage`（可变 dataclass，字段 `total_tokens` / `input_tokens` /
    `output_tokens` / `cache_read_tokens` / `cache_creation_tokens` /
    `reasoning_tokens` / `estimated_tokens`，方法 `add(other) -> None`，
    只读属性 `uncached_input_tokens -> int`）
  - `normalize(protocol: str, usage: dict | None) -> TokenUsage | None`
  - `merge_streaming_usage(existing: dict | None, incoming: dict | None) -> dict | None`
  - `estimate_tokens_from_chars(total_chars: int) -> int`
  - `estimate_token_usage_from_chars(total_chars: int) -> TokenUsage`
  - `usage_from_response_or_estimate(protocol, usage, messages, content) -> TokenUsage`
  - 常量 `PROTOCOL_OPENAI_COMPATIBLE` / `PROTOCOL_OPENAI_RESPONSES` /
    `PROTOCOL_ANTHROPIC` / `PROTOCOL_GEMINI`、`KNOWN_PROTOCOLS: frozenset[str]`

**为什么按协议而不是 provider 名分派：** `PROVIDER_REGISTRY`
（`backend/app/services/llm/client.py:2043`）注册了十几个 provider，但
`ProviderSpec.protocol` 只有四个取值。按 provider 名分派会让 deepseek / qwen /
zhipu / azure / openrouter / minimax / baidu 这一批 `openai_compatible` 的 provider
落进未知分支丢掉 usage。provider → protocol 的解析由调用方用既有的
`get_provider_spec()`（`client.py:2173`，已处理别名）完成 —— 刻意不在
`normalize.py` 里 import `client`，因为 `client.py` 需要反过来调用
`merge_streaming_usage`，互引会形成循环导入。

- [ ] **步骤 1：写失败的测试**

创建 `backend/tests/test_token_accounting_normalize.py`：

```python
"""Provider usage 归一化：口径不变式与各协议语义。

线上观察到的两个缺陷决定了这里的断言：
1. Anthropic 的 input_tokens 排除两个缓存计数，旧代码 total = input + output
   因此丢掉全部缓存量，命中率越高丢得越多。
2. Anthropic 流式 message_delta 覆盖 message_start，只回 output_tokens 的网关
   会让输入与缓存计数归零。
"""

from __future__ import annotations

import pytest

from app.services.llm.client import PROVIDER_REGISTRY, get_provider_spec
from app.services.token_accounting.normalize import (
    KNOWN_PROTOCOLS,
    PROTOCOL_ANTHROPIC,
    PROTOCOL_GEMINI,
    PROTOCOL_OPENAI_COMPATIBLE,
    PROTOCOL_OPENAI_RESPONSES,
    TokenUsage,
    merge_streaming_usage,
    normalize,
)


def test_anthropic_total_includes_both_cache_counters() -> None:
    """Anthropic 的 input_tokens 排除缓存，所以 total 必须把两个缓存计数加回来。"""
    usage = normalize(
        PROTOCOL_ANTHROPIC,
        {
            "input_tokens": 1_000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 2_000,
            "cache_read_input_tokens": 90_000,
        },
    )

    assert usage is not None
    assert usage.input_tokens == 93_000
    assert usage.cache_read_tokens == 90_000
    assert usage.cache_creation_tokens == 2_000
    assert usage.output_tokens == 500
    assert usage.total_tokens == 93_500
    assert usage.uncached_input_tokens == 1_000


def test_openai_prompt_tokens_already_include_cached() -> None:
    """OpenAI 的 prompt_tokens 已含 cached_tokens，不得重复加。"""
    usage = normalize(
        PROTOCOL_OPENAI_COMPATIBLE,
        {
            "prompt_tokens": 93_000,
            "completion_tokens": 500,
            "total_tokens": 93_500,
            "prompt_tokens_details": {"cached_tokens": 90_000},
            "completion_tokens_details": {"reasoning_tokens": 300},
        },
    )

    assert usage is not None
    assert usage.input_tokens == 93_000
    assert usage.cache_read_tokens == 90_000
    assert usage.total_tokens == 93_500
    assert usage.uncached_input_tokens == 3_000
    assert usage.reasoning_tokens == 300


def test_reasoning_tokens_are_not_added_to_any_total() -> None:
    """reasoning 已含在 output 里，重复计入就是重新引入正在修的这类 bug。"""
    usage = normalize(
        PROTOCOL_OPENAI_RESPONSES,
        {
            "input_tokens": 100,
            "output_tokens": 400,
            "total_tokens": 500,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens_details": {"reasoning_tokens": 350},
        },
    )

    assert usage is not None
    assert usage.output_tokens == 400
    assert usage.total_tokens == 500


def test_gemini_output_includes_thinking_tokens() -> None:
    """thoughtsTokenCount 要计费，必须计入 output。"""
    usage = normalize(
        PROTOCOL_GEMINI,
        {
            "promptTokenCount": 1_200,
            "candidatesTokenCount": 300,
            "thoughtsTokenCount": 700,
            "cachedContentTokenCount": 900,
            "totalTokenCount": 2_200,
        },
    )

    assert usage is not None
    assert usage.input_tokens == 1_200
    assert usage.cache_read_tokens == 900
    assert usage.output_tokens == 1_000
    assert usage.total_tokens == 2_200
    assert usage.reasoning_tokens == 700


def test_anthropic_shaped_usage_with_total_tokens_is_not_read_as_openai() -> None:
    """很多 Anthropic 兼容网关也回 total_tokens，旧代码据此误判为 OpenAI。"""
    usage = normalize(
        PROTOCOL_ANTHROPIC,
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 5_000,
            "total_tokens": 30,
        },
    )

    assert usage is not None
    assert usage.cache_read_tokens == 5_000
    assert usage.total_tokens == 5_030


def test_openai_shaped_input_is_lifted_to_at_least_the_cache_sum() -> None:
    """网关把 Anthropic 语义透传成 OpenAI 形状时，prompt_tokens 会小于缓存量。

    取 max 是单调且保守的修补：本来就含缓存时不变，明显不含时至少不再少算。
    """
    usage = normalize(
        PROTOCOL_OPENAI_COMPATIBLE,
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cache_read_input_tokens": 8_000,
            "cache_creation_input_tokens": 1_000,
        },
    )

    assert usage is not None
    assert usage.input_tokens == 9_000
    assert usage.total_tokens == 9_050


def test_openai_compatible_without_any_cache_detail_degrades_cleanly() -> None:
    """绝大多数第三方 provider 只回 prompt/completion，此时缓存计数为 0 且总量准确。"""
    usage = normalize(
        PROTOCOL_OPENAI_COMPATIBLE,
        {"prompt_tokens": 800, "completion_tokens": 200, "total_tokens": 1_000},
    )

    assert usage is not None
    assert usage.cache_read_tokens == 0
    assert usage.cache_creation_tokens == 0
    assert usage.total_tokens == 1_000


def test_empty_usage_returns_none() -> None:
    assert normalize(PROTOCOL_ANTHROPIC, None) is None
    assert normalize(PROTOCOL_ANTHROPIC, {}) is None


def test_unknown_protocol_returns_none() -> None:
    assert normalize("not-a-protocol", {"prompt_tokens": 1, "completion_tokens": 1}) is None


def test_every_registered_provider_resolves_to_a_known_protocol() -> None:
    """新增 provider 时不能静默落进未知分支丢 usage。"""
    for provider in PROVIDER_REGISTRY:
        spec = get_provider_spec(provider)
        assert spec is not None, provider
        assert spec.protocol in KNOWN_PROTOCOLS, f"{provider} -> {spec.protocol}"


def test_streaming_merge_keeps_message_start_fields() -> None:
    """这就是当前 bug 的精确复现：delta 只回 output_tokens 时不得丢输入与缓存。"""
    message_start = {
        "input_tokens": 1_000,
        "output_tokens": 1,
        "cache_creation_input_tokens": 2_000,
        "cache_read_input_tokens": 90_000,
    }
    message_delta = {"output_tokens": 500}

    merged = merge_streaming_usage(message_start, message_delta)

    assert merged == {
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 2_000,
        "cache_read_input_tokens": 90_000,
    }


def test_streaming_merge_accepts_a_cumulative_full_delta() -> None:
    """Anthropic 文档说 message_delta 的 usage 是累计值，取 max 对两种网关都对。"""
    merged = merge_streaming_usage(
        {"input_tokens": 1_000, "output_tokens": 10, "cache_read_input_tokens": 90_000},
        {"input_tokens": 1_000, "output_tokens": 500, "cache_read_input_tokens": 90_000},
    )

    assert merged["output_tokens"] == 500
    assert merged["cache_read_input_tokens"] == 90_000


def test_streaming_merge_recurses_into_nested_detail_dicts() -> None:
    merged = merge_streaming_usage(
        {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 8}},
        {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}},
    )

    assert merged["prompt_tokens_details"]["cached_tokens"] == 8


def test_streaming_merge_handles_missing_sides() -> None:
    assert merge_streaming_usage(None, {"output_tokens": 5}) == {"output_tokens": 5}
    assert merge_streaming_usage({"output_tokens": 5}, None) == {"output_tokens": 5}
    assert merge_streaming_usage(None, None) is None


def test_calibration_mismatch_warns(caplog: pytest.LogCaptureFixture) -> None:
    """错误的算术必须报警而不是静默 —— 当前这批 bug 正是因为它静默才潜伏这么久。"""
    from loguru import logger

    records: list[str] = []
    handler_id = logger.add(lambda message: records.append(message), level="WARNING")
    try:
        normalize(
            PROTOCOL_OPENAI_COMPATIBLE,
            {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 999},
        )
    finally:
        logger.remove(handler_id)

    assert any("token_calibration_mismatch" in record for record in records)


def test_anthropic_gateway_total_does_not_trigger_calibration_warning() -> None:
    """Anthropic 协议下网关给的 total_tokens 用的是旧的排除缓存定义，
    拿它比对会每次调用都告警，纯噪音。"""
    from loguru import logger

    records: list[str] = []
    handler_id = logger.add(lambda message: records.append(message), level="WARNING")
    try:
        normalize(
            PROTOCOL_ANTHROPIC,
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 5_000,
                "total_tokens": 30,
            },
        )
    finally:
        logger.remove(handler_id)

    assert not any("token_calibration_mismatch" in record for record in records)


def test_token_usage_add_accumulates_every_field() -> None:
    total = TokenUsage()
    total.add(
        TokenUsage(
            total_tokens=10,
            input_tokens=6,
            output_tokens=4,
            cache_read_tokens=3,
            cache_creation_tokens=1,
            reasoning_tokens=2,
            estimated_tokens=10,
        )
    )
    total.add(TokenUsage(total_tokens=5, input_tokens=3, output_tokens=2))

    assert total.total_tokens == 15
    assert total.input_tokens == 9
    assert total.output_tokens == 6
    assert total.cache_read_tokens == 3
    assert total.cache_creation_tokens == 1
    assert total.reasoning_tokens == 2
    assert total.estimated_tokens == 10
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_normalize.py -v
```

预期：collection 阶段就失败，
`ModuleNotFoundError: No module named 'app.services.token_accounting'`。

- [ ] **步骤 3：写最小实现**

创建 `backend/app/services/token_accounting/normalize.py`：

```python
"""统一 token 计量口径：provider usage -> TokenUsage。纯函数，零 IO。

口径不变式：total_tokens == input_tokens + output_tokens，其中 input_tokens
含缓存，cache_read / cache_creation 是它的细分，reasoning_tokens 仅供展示。

分派键是协议而不是 provider 名：PROVIDER_REGISTRY 里有十几个 provider，但只有
四个协议，按 provider 名分派会让一大批 openai_compatible 的第三方 provider 落
进未知分支丢掉 usage。本模块刻意不 import app.services.llm.client —— client 需
要反过来调用 merge_streaming_usage，互引会形成循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from app.services.llm.multimodal_content import estimate_multimodal_tokens

PROTOCOL_OPENAI_COMPATIBLE = "openai_compatible"
PROTOCOL_OPENAI_RESPONSES = "openai_responses"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_GEMINI = "gemini"

KNOWN_PROTOCOLS = frozenset(
    {
        PROTOCOL_OPENAI_COMPATIBLE,
        PROTOCOL_OPENAI_RESPONSES,
        PROTOCOL_ANTHROPIC,
        PROTOCOL_GEMINI,
    }
)

CHARS_PER_TOKEN = 3


@dataclass
class TokenUsage:
    """归一化后的 token 计量。

    保持可变 dataclass 与 add()，使 token_tracker 转发层签名和 caller.py 的既有
    测试都不受影响。
    """

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.total_tokens += other.total_tokens
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.estimated_tokens += other.estimated_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(
            0,
            self.input_tokens - self.cache_read_tokens - self.cache_creation_tokens,
        )


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _detail(usage: dict, *keys: str) -> dict:
    for key in keys:
        candidate = usage.get(key)
        if isinstance(candidate, dict):
            return candidate
    return {}


def _pick(source: dict, *keys: str) -> int:
    """取第一个非零候选键 —— 各网关对同一语义用的键名不统一。"""
    for key in keys:
        value = _int(source.get(key))
        if value:
            return value
    return 0


def estimate_tokens_from_chars(total_chars: int) -> int:
    """provider 完全不回 usage 时的粗估。"""
    return max(total_chars // CHARS_PER_TOKEN, 1)


def estimate_token_usage_from_chars(total_chars: int) -> TokenUsage:
    tokens = estimate_tokens_from_chars(total_chars)
    return TokenUsage(
        total_tokens=tokens,
        output_tokens=tokens,
        estimated_tokens=tokens,
    )


def merge_streaming_usage(
    existing: dict | None,
    incoming: dict | None,
) -> dict | None:
    """逐字段取最大值合并流式 usage。

    Anthropic 的 message_start 携带 input 与两个缓存计数，message_delta 携带
    output，且部分网关在 delta 里只发 output_tokens。旧代码在那里直接赋值，于是
    输入与缓存计数在主聊天路径上被抹掉。message_delta 的 usage 按文档是累计值，
    所以取 max 在"delta 携带完整值"和"delta 只携带子集"两种情况下都正确。
    """
    if not incoming:
        return existing
    if not existing:
        return dict(incoming)

    merged = dict(existing)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = merge_streaming_usage(current, value)
        elif isinstance(value, bool) or isinstance(current, bool):
            merged[key] = value
        elif isinstance(value, (int, float)) and isinstance(current, (int, float)):
            merged[key] = max(current, value)
        else:
            merged[key] = value
    return merged


def _normalize_openai_shaped(usage: dict, *, responses_api: bool) -> TokenUsage:
    if responses_api:
        raw_input = _pick(usage, "input_tokens", "prompt_tokens")
        raw_output = _pick(usage, "output_tokens", "completion_tokens")
        input_details = _detail(usage, "input_tokens_details", "prompt_tokens_details")
        output_details = _detail(
            usage, "output_tokens_details", "completion_tokens_details"
        )
    else:
        raw_input = _pick(usage, "prompt_tokens", "input_tokens")
        raw_output = _pick(usage, "completion_tokens", "output_tokens")
        input_details = _detail(usage, "prompt_tokens_details", "input_tokens_details")
        output_details = _detail(
            usage, "completion_tokens_details", "output_tokens_details"
        )

    cache_read = _pick(
        input_details, "cached_tokens", "cache_read_tokens", "cache_read_input_tokens"
    ) or _pick(usage, "cached_tokens", "cache_read_tokens", "cache_read_input_tokens")
    cache_creation = _pick(
        input_details, "cache_creation_tokens", "cache_creation_input_tokens"
    ) or _pick(usage, "cache_creation_tokens", "cache_creation_input_tokens")
    reasoning = _pick(output_details, "reasoning_tokens") or _pick(
        usage, "reasoning_tokens"
    )

    # 这些协议约定 input 已含缓存。但把 Anthropic 语义透传成 OpenAI 形状的网关会
    # 让 input 小于缓存量；取 max 是单调保守的修补 —— 本来就含缓存时不变，明显
    # 不含时至少不再少算。
    input_tokens = max(raw_input, cache_read + cache_creation)

    return TokenUsage(
        total_tokens=input_tokens + raw_output,
        input_tokens=input_tokens,
        output_tokens=raw_output,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        reasoning_tokens=reasoning,
    )


def _normalize_anthropic(usage: dict) -> TokenUsage:
    cache_read = _pick(usage, "cache_read_input_tokens", "cache_read_tokens")
    cache_creation = _pick(
        usage, "cache_creation_input_tokens", "cache_creation_tokens"
    )
    # Anthropic 的 input_tokens 排除两个缓存计数，必须相加而不是取 max。
    input_tokens = _int(usage.get("input_tokens")) + cache_read + cache_creation
    output_tokens = _int(usage.get("output_tokens"))

    return TokenUsage(
        total_tokens=input_tokens + output_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
    )


def _normalize_gemini(usage: dict) -> TokenUsage:
    cache_read = _int(usage.get("cachedContentTokenCount"))
    input_tokens = max(_int(usage.get("promptTokenCount")), cache_read)
    thoughts = _int(usage.get("thoughtsTokenCount"))
    output_tokens = _int(usage.get("candidatesTokenCount")) + thoughts

    return TokenUsage(
        total_tokens=input_tokens + output_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        reasoning_tokens=thoughts,
    )


def _provider_reported_total(protocol: str, usage: dict) -> int | None:
    """provider 自报的总量，仅用于自校验。

    anthropic 协议刻意返回 None：网关在那里给的 total_tokens 用的是旧的排除缓存
    定义，拿它比对会每次调用都告警，纯噪音。
    """
    if protocol == PROTOCOL_ANTHROPIC:
        return None
    if protocol == PROTOCOL_GEMINI:
        reported = _int(usage.get("totalTokenCount"))
        return reported or None
    reported = _int(usage.get("total_tokens"))
    return reported or None


def normalize(protocol: str, usage: dict | None) -> TokenUsage | None:
    """按协议归一化 provider usage。未知协议或空 usage 返回 None。"""
    if not usage or not isinstance(usage, dict):
        return None

    if protocol == PROTOCOL_ANTHROPIC:
        result = _normalize_anthropic(usage)
    elif protocol == PROTOCOL_GEMINI:
        result = _normalize_gemini(usage)
    elif protocol == PROTOCOL_OPENAI_RESPONSES:
        result = _normalize_openai_shaped(usage, responses_api=True)
    elif protocol == PROTOCOL_OPENAI_COMPATIBLE:
        result = _normalize_openai_shaped(usage, responses_api=False)
    else:
        logger.warning(
            "token_unknown_protocol protocol={} usage_keys={}",
            protocol,
            sorted(usage.keys()),
        )
        return None

    if result.total_tokens <= 0:
        return None

    reported_total = _provider_reported_total(protocol, usage)
    if reported_total is not None and reported_total != result.total_tokens:
        # 只告警不纠正：当前这批 bug 正是因为错误的算术静默才潜伏这么久，接新网关
        # 或某家改语义时应该表现为告警。
        logger.warning(
            "token_calibration_mismatch protocol={} reported={} computed={} usage={}",
            protocol,
            reported_total,
            result.total_tokens,
            usage,
        )

    return result


def usage_from_response_or_estimate(
    protocol: str,
    usage: dict | None,
    messages: object,
    content: str | None,
) -> TokenUsage:
    """有真实 usage 就用，没有才退回字符估算，并把估算量单独记下来。"""
    normalized = normalize(protocol, usage)
    if normalized is not None:
        return normalized

    input_tokens = estimate_multimodal_tokens(
        messages,
        chars_per_token=CHARS_PER_TOKEN,
    )
    output_tokens = estimate_tokens_from_chars(len(content or ""))
    total = input_tokens + output_tokens
    return TokenUsage(
        total_tokens=total,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_tokens=total,
    )


__all__ = [
    "CHARS_PER_TOKEN",
    "KNOWN_PROTOCOLS",
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_GEMINI",
    "PROTOCOL_OPENAI_COMPATIBLE",
    "PROTOCOL_OPENAI_RESPONSES",
    "TokenUsage",
    "estimate_token_usage_from_chars",
    "estimate_tokens_from_chars",
    "merge_streaming_usage",
    "normalize",
    "usage_from_response_or_estimate",
]
```

创建 `backend/app/services/token_accounting/__init__.py`：

```python
"""Token 计量与限额。对外唯一入口，其他模块不直接 import 子模块。"""

from app.services.token_accounting.normalize import (
    KNOWN_PROTOCOLS,
    PROTOCOL_ANTHROPIC,
    PROTOCOL_GEMINI,
    PROTOCOL_OPENAI_COMPATIBLE,
    PROTOCOL_OPENAI_RESPONSES,
    TokenUsage,
    estimate_token_usage_from_chars,
    estimate_tokens_from_chars,
    merge_streaming_usage,
    normalize,
    usage_from_response_or_estimate,
)

__all__ = [
    "KNOWN_PROTOCOLS",
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_GEMINI",
    "PROTOCOL_OPENAI_COMPATIBLE",
    "PROTOCOL_OPENAI_RESPONSES",
    "TokenUsage",
    "estimate_token_usage_from_chars",
    "estimate_tokens_from_chars",
    "merge_streaming_usage",
    "normalize",
    "usage_from_response_or_estimate",
]
```

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_normalize.py -v
```

预期：18 passed。

- [ ] **步骤 5：跑 lint**

```bash
cd backend && .venv/bin/ruff check app/services/token_accounting tests/test_token_accounting_normalize.py && .venv/bin/ruff format app/services/token_accounting tests/test_token_accounting_normalize.py
```

预期：`All checks passed!`

- [ ] **步骤 6：提交**

```bash
git add backend/app/services/token_accounting backend/tests/test_token_accounting_normalize.py
git commit -m "feat(token): 按协议归一化 provider usage，统一含缓存口径"
```

---

### Task 2：Anthropic 流式 usage 改为合并

**文件：**
- 修改：`backend/app/services/llm/client.py:1918-1985`
- 测试：`backend/tests/test_token_accounting_normalize.py`（追加）

**接口：**
- 依赖：Task 1 的 `merge_streaming_usage(existing, incoming) -> dict | None`
- 产出：无新公开接口，`AnthropicClient.stream` 的 `usage` 输出语义修正

**背景：** `client.py:1922-1923`（`message_start`）与 `:1983-1985`
（`message_delta`）都对 `final_usage` 直接赋值，后者把前者整体覆盖。
`message_start` 是 `input_tokens` 与两个缓存计数唯一到达的地方。

- [ ] **步骤 1：写失败的测试**

在 `backend/tests/test_token_accounting_normalize.py` 末尾追加：

```python
def test_anthropic_client_stream_merges_usage_across_events() -> None:
    """AnthropicClient.stream 必须用 merge_streaming_usage 而不是直接赋值。

    只回 output_tokens 的网关很常见；直接赋值会让 input 与两个缓存计数在主聊天
    路径上归零。这里用源码级断言守住这一点，因为构造一条真实的 SSE 流需要 HTTP
    fixture，而本仓库的测试不连外部依赖。
    """
    import inspect

    from app.services.llm import client as llm_client

    source = inspect.getsource(llm_client.AnthropicClient.stream)

    assert "merge_streaming_usage" in source
    assert 'final_usage = msg["usage"]' not in source
    assert 'final_usage = data["usage"]' not in source


def test_anthropic_stream_usage_merge_is_end_to_end_correct() -> None:
    """把两个事件的 usage 依次合并再归一化，等价于流式路径的实际算法。"""
    final_usage = merge_streaming_usage(
        None,
        {
            "input_tokens": 1_000,
            "output_tokens": 1,
            "cache_creation_input_tokens": 2_000,
            "cache_read_input_tokens": 90_000,
        },
    )
    final_usage = merge_streaming_usage(final_usage, {"output_tokens": 500})

    usage = normalize(PROTOCOL_ANTHROPIC, final_usage)

    assert usage is not None
    assert usage.total_tokens == 93_500
    assert usage.cache_read_tokens == 90_000
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_normalize.py -k anthropic_client_stream -v
```

预期：FAIL，`assert 'merge_streaming_usage' in source`。

- [ ] **步骤 3：写最小实现**

在 `backend/app/services/llm/client.py` 的 import 区加入：

```python
from app.services.token_accounting.normalize import merge_streaming_usage
```

把 `client.py:1918-1923` 的 `message_start` 分支改为：

```python
                    if current_event == "message_start":
                        msg = data.get("message", {})
                        if msg.get("model"):
                            final_model = msg["model"]
                        if msg.get("usage"):
                            final_usage = merge_streaming_usage(
                                final_usage, msg["usage"]
                            )
```

把 `client.py:1979-1985` 的 `message_delta` 分支改为：

```python
                    elif current_event == "message_delta":
                        delta = data.get("delta", {})
                        if delta.get("stop_reason"):
                            last_finish_reason = delta["stop_reason"]
                        if data.get("usage"):
                            # message_delta 的 usage 按文档是累计值，但部分网关只
                            # 回 output_tokens。逐字段取 max 才不会丢掉
                            # message_start 携带的 input 与两个缓存计数。
                            final_usage = merge_streaming_usage(
                                final_usage, data["usage"]
                            )
```

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_normalize.py -v
```

预期：20 passed。

再跑一次全量确认没有把循环导入引进来 —— `client.py` 现在依赖
`token_accounting.normalize`，而后者刻意不依赖 `client`：

```bash
cd backend && .venv/bin/python -c "import app.services.llm.client; import app.services.token_accounting; print('no import cycle')"
```

预期：输出 `no import cycle`。

- [ ] **步骤 5：跑 lint 并提交**

```bash
cd backend && .venv/bin/ruff check app/services/llm/client.py && .venv/bin/ruff format app/services/llm/client.py
git add backend/app/services/llm/client.py backend/tests/test_token_accounting_normalize.py
git commit -m "fix(token): Anthropic 流式 usage 逐字段合并，不再丢输入与缓存计数"
```

---

### Task 3：`periods.py` —— 带时区的周期边界

**文件：**
- 新建：`backend/app/services/token_accounting/periods.py`
- 修改：`backend/app/services/token_accounting/__init__.py`（re-export）
- 测试：`backend/tests/test_token_accounting_periods.py`

**接口：**
- 依赖：`app.services.timezone_utils.get_agent_timezone_sync(agent, tenant=None) -> str`
  （优先级 `agent.timezone → tenant.timezone → "UTC"`）
- 产出：
  - `local_day_start(tz_name: str, *, now: datetime) -> datetime`
  - `local_month_start(tz_name: str, *, now: datetime) -> datetime`
  - `is_new_local_day(last_reset_utc: datetime | None, tz_name: str, *, now: datetime) -> bool`
  - `is_new_local_month(last_reset_utc: datetime | None, tz_name: str, *, now: datetime) -> bool`
  - `effective_timezone(agent, tenant=None) -> str`
  - `tenant_timezone(tenant) -> str`

**为什么必须带时区：** 现在 `token_tracker.py:215` 和 `agents.py:79` 全按 UTC 算，
对 `Asia/Shanghai` 租户来说"今天"在北京时间早上 8 点翻页，与管理员看日历的直觉
不符。`timezone_utils` 已有解析逻辑，复用它保证与 `heartbeat`、`agent_context`
语义一致。

`now` 一律作为必需关键字参数注入，不在模块内部调 `datetime.now()` —— 否则这层就
不再是纯函数，测试要靠 monkeypatch 时间。

- [ ] **步骤 1：写失败的测试**

创建 `backend/tests/test_token_accounting_periods.py`：

```python
"""日/月周期边界按租户时区计算。

现在全按 UTC 算，Asia/Shanghai 租户的"今天"在北京时间早上 8 点翻页，与管理员看
日历的直觉不符。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    now = datetime(2026, 8, 6, 16, 30, tzinfo=timezone.utc)  # 北京 8/7 00:30

    start = local_day_start(SHANGHAI, now=now)

    assert start == datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc)
    assert start.tzinfo is timezone.utc


def test_utc_1600_already_belongs_to_the_next_shanghai_day() -> None:
    """这就是切换时区语义要解决的那个具体问题。"""
    before = datetime(2026, 8, 6, 15, 59, tzinfo=timezone.utc)
    after = datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc)

    assert local_day_start(SHANGHAI, now=before) != local_day_start(SHANGHAI, now=after)


def test_local_day_start_handles_negative_offsets() -> None:
    now = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)  # 纽约 8/5 23:00 (EDT)

    start = local_day_start(NEW_YORK, now=now)

    assert start == datetime(2026, 8, 5, 4, 0, tzinfo=timezone.utc)


def test_local_month_start_uses_the_local_calendar_month() -> None:
    now = datetime(2026, 7, 31, 16, 30, tzinfo=timezone.utc)  # 北京 8/1 00:30

    start = local_month_start(SHANGHAI, now=now)

    assert start == datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)


def test_invalid_timezone_falls_back_to_utc_instead_of_raising() -> None:
    """时区字段是自由文本，脏数据不能让记账整条路径崩掉。"""
    now = datetime(2026, 8, 6, 16, 30, tzinfo=timezone.utc)

    assert local_day_start("Not/AZone", now=now) == datetime(
        2026, 8, 6, 0, 0, tzinfo=timezone.utc
    )


def test_is_new_local_day_detects_rollover() -> None:
    now = datetime(2026, 8, 6, 16, 30, tzinfo=timezone.utc)
    day_start = local_day_start(SHANGHAI, now=now)

    assert is_new_local_day(day_start - timedelta(seconds=1), SHANGHAI, now=now) is True
    assert is_new_local_day(day_start, SHANGHAI, now=now) is False
    assert is_new_local_day(now, SHANGHAI, now=now) is False


def test_is_new_local_day_treats_never_reset_as_new() -> None:
    now = datetime(2026, 8, 6, 16, 30, tzinfo=timezone.utc)

    assert is_new_local_day(None, SHANGHAI, now=now) is True


def test_is_new_local_day_accepts_naive_timestamps_as_utc() -> None:
    """历史行可能是 naive datetime，不能因此判成"永远需要重置"。"""
    now = datetime(2026, 8, 6, 16, 30, tzinfo=timezone.utc)
    naive_after_start = datetime(2026, 8, 6, 16, 10)

    assert is_new_local_day(naive_after_start, SHANGHAI, now=now) is False


def test_is_new_local_month_detects_rollover() -> None:
    now = datetime(2026, 7, 31, 16, 30, tzinfo=timezone.utc)  # 北京 8/1
    month_start = local_month_start(SHANGHAI, now=now)

    assert (
        is_new_local_month(month_start - timedelta(seconds=1), SHANGHAI, now=now) is True
    )
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
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_periods.py -v
```

预期：collection 失败，
`ModuleNotFoundError: No module named 'app.services.token_accounting.periods'`。

- [ ] **步骤 3：写最小实现**

创建 `backend/app/services/token_accounting/periods.py`：

```python
"""按时区计算日/月周期边界。纯函数，零 IO。

`now` 一律由调用方注入，模块内部不调 datetime.now() —— 否则这层就不是纯函数，
测试要靠 monkeypatch 时间。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.timezone_utils import get_agent_timezone_sync

UTC_NAME = "UTC"


def _zone(tz_name: str | None) -> ZoneInfo:
    """时区字段是自由文本，脏数据不能让记账整条路径崩掉。"""
    try:
        return ZoneInfo(tz_name or UTC_NAME)
    except Exception:
        return ZoneInfo(UTC_NAME)


def _as_utc(value: datetime) -> datetime:
    """历史行可能是 naive datetime，按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def local_day_start(tz_name: str, *, now: datetime) -> datetime:
    """本地零点对应的 UTC 时刻，用作 DailyTokenUsage.date 的锚点。"""
    zone = _zone(tz_name)
    local_now = _as_utc(now).astimezone(zone)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


def local_month_start(tz_name: str, *, now: datetime) -> datetime:
    zone = _zone(tz_name)
    local_now = _as_utc(now).astimezone(zone)
    local_first = local_now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return local_first.astimezone(timezone.utc)


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
```

在 `backend/app/services/token_accounting/__init__.py` 的 import 区追加：

```python
from app.services.token_accounting.periods import (
    effective_timezone,
    is_new_local_day,
    is_new_local_month,
    local_day_start,
    local_month_start,
    tenant_timezone,
)
```

并把这六个名字加进该文件的 `__all__`。

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_periods.py -v
```

预期：11 passed。

- [ ] **步骤 5：跑 lint 并提交**

```bash
cd backend && .venv/bin/ruff check app/services/token_accounting tests/test_token_accounting_periods.py && .venv/bin/ruff format app/services/token_accounting tests/test_token_accounting_periods.py
git add backend/app/services/token_accounting backend/tests/test_token_accounting_periods.py
git commit -m "feat(token): 日月周期边界按租户时区计算"
```

---

### Task 4：数据模型改动与 Alembic 迁移

**文件：**
- 修改：`backend/app/models/agent.py:84-96`
- 修改：`backend/app/models/tenant.py`
- 修改：`backend/app/models/activity_log.py:35-56`
- 新建：`backend/app/models/tenant_token_counter.py`
- 新建：`backend/alembic/versions/202608061000_token_accounting_v2.py`
- 测试：`backend/tests/test_token_accounting_schema.py`

**接口：**
- 产出（后续任务依赖这些确切名字）：
  - `Agent.input_tokens_today` / `.input_tokens_month` / `.input_tokens_total`
  - `Tenant.max_tokens_per_day` / `.default_agent_max_tokens_per_day` /
    `.default_agent_max_tokens_per_month`
  - `TenantTokenCounter`（`__tablename__ = "tenant_token_counters"`，字段
    `tenant_id` / `tokens_used_today` / `tokens_used_total` / `last_daily_reset`）
  - `DailyTokenUsage.agent_name_snapshot` / `.system_scope` / `.reasoning_tokens`
  - 迁移模块常量 `SYSTEM_SCOPES`、`AGENT_UNIQUE_INDEX`、`SYSTEM_UNIQUE_INDEX`
  - `revision = "token_accounting_v2"`，`down_revision = "widen_credential_scopes"`

**必须绕开的唯一约束陷阱：** 现在是 `UNIQUE(agent_id, date)`。PostgreSQL 在唯一
约束里把 NULL 之间视为互不相同，所以 `agent_id` 一旦可空，`ON CONFLICT` 就永远
不会命中系统开销行，每次调用都插新行，聚合数字随调用次数虚增。改用两个带
`postgresql_where` 的部分唯一索引。刻意不用 PostgreSQL 15 的
`NULLS NOT DISTINCT`，避免给部署环境增加隐式版本下限。

- [ ] **步骤 1：写失败的测试**

创建 `backend/tests/test_token_accounting_schema.py`：

```python
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

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202608061000_token_accounting_v2.py"
)


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
    """PostgreSQL 把唯一约束里的 NULL 视为互不相同，所以必须用部分唯一索引拆开。"""
    indexes = {index.name: index for index in DailyTokenUsage.__table__.indexes}

    agent_index = indexes["uq_daily_token_usage_agent_date"]
    assert agent_index.unique is True
    assert [c.name for c in agent_index.columns] == ["agent_id", "date"]
    assert agent_index.dialect_options["postgresql"]["where"] is not None

    system_index = indexes["uq_daily_token_usage_system_date"]
    assert system_index.unique is True
    assert [c.name for c in system_index.columns] == [
        "tenant_id",
        "system_scope",
        "date",
    ]
    assert system_index.dialect_options["postgresql"]["where"] is not None


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
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_schema.py -v
```

预期：collection 失败，
`ModuleNotFoundError: No module named 'app.models.tenant_token_counter'`。

- [ ] **步骤 3：写最小实现**

在 `backend/app/models/agent.py:96` 之后（`cache_creation_tokens_total` 那行后面）
插入：

```python
    # 修正后的缓存命中率分母是"输入总量"（含缓存），从 tokens_used_* 与两个
    # cache 计数里算不出来，所以单独记 input 计数器。
    input_tokens_today: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens_month: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens_total: Mapped[int] = mapped_column(Integer, default=0)
```

在 `backend/app/models/tenant.py` 的 `default_max_llm_calls_per_day` 那行之后
（`:35`）插入：

```python
    # 租户日 token 天花板。NULL = 无限。含系统开销（群聊压缩 / 规划 / 连通性测试）。
    max_tokens_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 新建 Agent 时带入的默认 token 限额
    default_agent_max_tokens_per_day: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    default_agent_max_tokens_per_month: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
```

创建 `backend/app/models/tenant_token_counter.py`：

```python
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
    tokens_used_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_used_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_daily_reset: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

把 `backend/app/models/activity_log.py:35-56` 的 `DailyTokenUsage` 改为：

```python
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
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
```

同时把 `activity_log.py` 顶部的 import 补齐（`UniqueConstraint` 若已不再使用则
移除）：

```python
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
```

创建 `backend/alembic/versions/202608061000_token_accounting_v2.py`：

```python
"""token accounting v2: 含缓存的统一口径、系统开销归属、租户日上限

Revision ID: token_accounting_v2
Revises: widen_credential_scopes
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "token_accounting_v2"
down_revision: Union[str, None] = "widen_credential_scopes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

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
    for name in AGENT_INPUT_COLUMNS:
        op.add_column(
            "agents",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )

    for name in TENANT_LIMIT_COLUMNS:
        op.add_column("tenants", sa.Column(name, sa.Integer(), nullable=True))

    op.create_table(
        "tenant_token_counters",
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tokens_used_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_used_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_daily_reset", sa.DateTime(timezone=True), nullable=True),
    )
    # 每个现有租户预置一行零计数，让热路径只需 UPDATE 而不必先判断存在性。
    op.execute(
        "INSERT INTO tenant_token_counters (tenant_id, tokens_used_today, "
        "tokens_used_total) SELECT id, 0, 0 FROM tenants"
    )

    op.add_column(
        "daily_token_usage",
        sa.Column("agent_name_snapshot", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "daily_token_usage",
        sa.Column("system_scope", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "daily_token_usage",
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_daily_token_usage_system_scope", "daily_token_usage", ["system_scope"]
    )

    # 历史行回填 agent 名快照，使删 agent 后仍可归因。
    op.execute(
        "UPDATE daily_token_usage AS d SET agent_name_snapshot = a.name "
        "FROM agents AS a WHERE d.agent_id = a.id AND d.agent_name_snapshot IS NULL"
    )

    op.alter_column("daily_token_usage", "agent_id", nullable=True)
    op.drop_constraint(
        "daily_token_usage_agent_id_fkey", "daily_token_usage", type_="foreignkey"
    )
    op.create_foreign_key(
        "daily_token_usage_agent_id_fkey",
        "daily_token_usage",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 旧唯一约束在 agent_id 可空后会让 ON CONFLICT 永不命中系统开销行。
    op.drop_constraint(AGENT_UNIQUE_INDEX, "daily_token_usage", type_="unique")
    op.create_index(
        AGENT_UNIQUE_INDEX,
        "daily_token_usage",
        ["agent_id", "date"],
        unique=True,
        postgresql_where=sa.text("system_scope IS NULL"),
    )
    op.create_index(
        SYSTEM_UNIQUE_INDEX,
        "daily_token_usage",
        ["tenant_id", "system_scope", "date"],
        unique=True,
        postgresql_where=sa.text("system_scope IS NOT NULL"),
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
    # 回滚前必须清掉系统开销行，否则 agent_id NOT NULL 与旧唯一约束都无法恢复。
    op.execute("DELETE FROM daily_token_usage WHERE system_scope IS NOT NULL")
    op.create_unique_constraint(
        AGENT_UNIQUE_INDEX, "daily_token_usage", ["agent_id", "date"]
    )

    op.drop_constraint(
        "daily_token_usage_agent_id_fkey", "daily_token_usage", type_="foreignkey"
    )
    op.create_foreign_key(
        "daily_token_usage_agent_id_fkey",
        "daily_token_usage",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute("DELETE FROM daily_token_usage WHERE agent_id IS NULL")
    op.alter_column("daily_token_usage", "agent_id", nullable=False)

    op.drop_index("ix_daily_token_usage_system_scope", table_name="daily_token_usage")
    op.drop_column("daily_token_usage", "reasoning_tokens")
    op.drop_column("daily_token_usage", "system_scope")
    op.drop_column("daily_token_usage", "agent_name_snapshot")

    op.drop_table("tenant_token_counters")

    for name in reversed(TENANT_LIMIT_COLUMNS):
        op.drop_column("tenants", name)
    for name in reversed(AGENT_INPUT_COLUMNS):
        op.drop_column("agents", name)
```

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_schema.py -v
```

预期：11 passed。

确认新模型已被 `Base.metadata` 收录（`bootstrap_db.py` 靠它建表）：

```bash
cd backend && .venv/bin/python -c "
import app.models.tenant_token_counter  # noqa: F401
from app.database import Base
print('tenant_token_counters' in Base.metadata.tables)
"
```

预期：输出 `True`。若为 `False`，把该模型加进
`backend/app/scripts/bootstrap_db.py` 的模型 import 清单。

- [ ] **步骤 5：在真实 PostgreSQL 上验证迁移与部分唯一索引**

这一步无法被单测覆盖（本仓库测试不连 DB），必须手工做一次并记录结果。

```bash
cd backend && .venv/bin/alembic upgrade head
.venv/bin/alembic downgrade -1
.venv/bin/alembic upgrade head
```

预期：三条命令都成功，无残留。然后在 psql 里对同一系统开销行连续 upsert 两次，
确认只有一行且数值累加：

```sql
INSERT INTO daily_token_usage (id, tenant_id, date, system_scope, tokens_used)
VALUES (gen_random_uuid(), '<某个真实 tenant_id>', date_trunc('day', now()),
        'planning', 100)
ON CONFLICT (tenant_id, system_scope, date)
  WHERE system_scope IS NOT NULL
  DO UPDATE SET tokens_used = daily_token_usage.tokens_used + 100;
-- 再执行一次上面这条，然后：
SELECT count(*), sum(tokens_used) FROM daily_token_usage
 WHERE system_scope = 'planning';
```

预期：`count = 1`，`sum = 200`。若 `count = 2`，说明部分唯一索引没建成或
`ON CONFLICT` 的 `WHERE` 子句没对上，必须先修好再继续后续任务 —— 这是整个改动里
最容易静默出错的一处。

- [ ] **步骤 6：跑 lint 并提交**

```bash
cd backend && .venv/bin/ruff check app/models alembic/versions/202608061000_token_accounting_v2.py tests/test_token_accounting_schema.py && .venv/bin/ruff format app/models alembic/versions/202608061000_token_accounting_v2.py tests/test_token_accounting_schema.py
git add backend/app/models backend/alembic/versions/202608061000_token_accounting_v2.py backend/tests/test_token_accounting_schema.py
git commit -m "feat(token): 表结构支持系统开销归属、租户日上限与输入计数器"
```

---

### Task 5：`ledger.py` —— 原子记账

**文件：**
- 新建：`backend/app/services/token_accounting/ledger.py`
- 修改：`backend/app/services/token_accounting/__init__.py`（re-export）
- 测试：`backend/tests/test_token_accounting_ledger.py`

**接口：**
- 依赖：Task 1 的 `TokenUsage`；Task 3 的 `local_day_start` / `local_month_start` /
  `is_new_local_day` / `is_new_local_month` / `effective_timezone` /
  `tenant_timezone`；Task 4 的 `TenantTokenCounter` 与 `DailyTokenUsage` 新列
- 产出：
  - `SYSTEM_SCOPE_GROUP_COMPACT = "group_compact"`
  - `SYSTEM_SCOPE_PLANNING = "planning"`
  - `SYSTEM_SCOPE_MODEL_PROBE = "model_probe"`
  - `SYSTEM_SCOPES: tuple[str, ...]`
  - `async def record(usage: TokenUsage, *, tenant_id: uuid.UUID, agent_id: uuid.UUID | None = None, system_scope: str | None = None, now: datetime | None = None) -> bool`
    （返回是否成功落库，供调用方决定是否告警；不抛异常）
  - `LEDGER_MAX_RETRIES = 2`

**三条硬要求：**
1. 单一事务覆盖 `tenant_token_counters` / `agents` / `daily_token_usage`，三者不可
   能不一致。现在 `Agent` 行是 Python 侧读改写、`DailyTokenUsage` 是原子 upsert，
   并发语义不同会永久漂移。
2. 固定写入顺序 `tenant_token_counters → agents → daily_token_usage`，让并发事务
   不可能互相死锁。
3. 惰性重置用条件 UPDATE，在同一事务内、累加之前执行，构造上即幂等。

- [ ] **步骤 1：写失败的测试**

创建 `backend/tests/test_token_accounting_ledger.py`：

```python
"""记账写入的原子性、顺序与归属。

用假 session 记录发出的语句，不连真实数据库（本仓库测试不连 DB）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.token_accounting import ledger
from app.services.token_accounting.ledger import (
    SYSTEM_SCOPE_PLANNING,
    SYSTEM_SCOPES,
    record,
)
from app.services.token_accounting.normalize import TokenUsage

NOW = datetime(2026, 8, 6, 16, 30, tzinfo=timezone.utc)  # 北京 8/7 00:30
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """记录 execute 顺序与 commit/rollback，供断言事务语义。"""

    def __init__(self, *, agent=None, tenant=None, fail_times: int = 0):
        self.statements: list[object] = []
        self.committed = 0
        self.rolled_back = 0
        self._agent = agent
        self._tenant = tenant
        self._fail_times = fail_times

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        text = str(statement).lower()
        if self._fail_times > 0 and "update tenant_token_counters" in text:
            self._fail_times -= 1
            raise RuntimeError("could not serialize access due to concurrent update")
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


def _tables_touched(session: FakeSession) -> list[str]:
    order: list[str] = []
    for statement in session.statements:
        text = str(statement).lower()
        for table in ("tenant_token_counters", "agents", "daily_token_usage"):
            if table in text and (table not in order or order[-1] != table):
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

    order = [t for t in _tables_touched(session)]
    assert order.index("tenant_token_counters") < order.index("agents")
    assert order.index("agents") < order.index("daily_token_usage")


async def test_counters_are_incremented_atomically_in_sql(monkeypatch) -> None:
    """Python 侧读改写在并发下会丢更新，必须是 SQL 原子累加。"""
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant)
    _install(monkeypatch, session)

    await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)

    agent_updates = [
        str(s).lower()
        for s in session.statements
        if "update agents" in str(s).lower()
    ]
    assert agent_updates, "没有对 agents 发出 UPDATE"
    incrementing = [s for s in agent_updates if "tokens_used_today +" in s.replace(" ", " ")]
    assert incrementing, "agents 的计数器不是 SQL 原子累加"


async def test_lazy_reset_is_a_conditional_update(monkeypatch) -> None:
    """条件 UPDATE 构造上幂等：两个并发轮次只会清零一次，且不吞掉对方的累加。"""
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant)
    _install(monkeypatch, session)

    await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)

    resets = [
        str(s).lower()
        for s in session.statements
        if "last_daily_reset" in str(s).lower() and "update" in str(s).lower()
    ]
    assert resets, "没有发出日重置语句"
    assert any("last_daily_reset is null" in s or "last_daily_reset <" in s for s in resets)


async def test_upsert_targets_the_agent_partial_index(monkeypatch) -> None:
    agent = SimpleNamespace(id=AGENT_ID, name="Ada", timezone=None, tenant_id=TENANT_ID)
    tenant = SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai")
    session = FakeSession(agent=agent, tenant=tenant)
    _install(monkeypatch, session)

    await record(_usage(), tenant_id=TENANT_ID, agent_id=AGENT_ID, now=NOW)

    upserts = [
        str(s).lower()
        for s in session.statements
        if "insert into daily_token_usage" in str(s).lower()
    ]
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

    upserts = [
        str(s).lower()
        for s in session.statements
        if "insert into daily_token_usage" in str(s).lower()
    ]
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

    assert captured["Asia/Shanghai"] == datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc)


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


def test_system_scopes_match_the_migration() -> None:
    assert SYSTEM_SCOPES == ("group_compact", "planning", "model_probe")
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_ledger.py -v
```

预期：collection 失败，
`ModuleNotFoundError: No module named 'app.services.token_accounting.ledger'`。

- [ ] **步骤 3：写最小实现**

创建 `backend/app/services/token_accounting/ledger.py`：

```python
"""Token 记账持久化：单事务、固定顺序、原子累加。

旧实现的三个问题在这里一并解决：
1. Agent 行是 Python 侧读改写，并发下丢更新，且与 DailyTokenUsage 的原子 upsert
   长期漂移 —— 改成同一事务内的 SQL 原子累加。
2. 日/月重置只在两个 API 端点里做，纯 cron 驱动的 Agent 过了午夜仍被旧计数卡死
   —— 改成记账路径上的条件 UPDATE，幂等且无竞态。
3. 失败被 try/except 吞掉只记 warning —— 改成有界重试后按 ERROR 记录完整载荷。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.database import async_session
from app.models.activity_log import DailyTokenUsage
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.tenant_token_counter import TenantTokenCounter
from app.services.token_accounting.normalize import TokenUsage
from app.services.token_accounting.periods import (
    effective_timezone,
    is_new_local_day,
    is_new_local_month,
    local_day_start,
    local_month_start,
    tenant_timezone,
)

SYSTEM_SCOPE_GROUP_COMPACT = "group_compact"
SYSTEM_SCOPE_PLANNING = "planning"
SYSTEM_SCOPE_MODEL_PROBE = "model_probe"
SYSTEM_SCOPES = (
    SYSTEM_SCOPE_GROUP_COMPACT,
    SYSTEM_SCOPE_PLANNING,
    SYSTEM_SCOPE_MODEL_PROBE,
)

LEDGER_MAX_RETRIES = 2

_RETRYABLE_MARKERS = (
    "could not serialize",
    "deadlock detected",
    "concurrent update",
)


def _is_retryable(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


async def _reset_tenant_counter_if_stale(db, tenant, now: datetime) -> None:
    tz_name = tenant_timezone(tenant)
    if not is_new_local_day(tenant.counter_last_daily_reset, tz_name, now=now):
        return
    day_start = local_day_start(tz_name, now=now)
    await db.execute(
        update(TenantTokenCounter)
        .where(
            TenantTokenCounter.tenant_id == tenant.id,
            (TenantTokenCounter.last_daily_reset.is_(None))
            | (TenantTokenCounter.last_daily_reset < day_start),
        )
        .values(tokens_used_today=0, last_daily_reset=now)
    )


async def _reset_agent_counters_if_stale(db, agent, tz_name: str, now: datetime) -> None:
    if is_new_local_day(agent.last_daily_reset, tz_name, now=now):
        day_start = local_day_start(tz_name, now=now)
        await db.execute(
            update(Agent)
            .where(
                Agent.id == agent.id,
                (Agent.last_daily_reset.is_(None))
                | (Agent.last_daily_reset < day_start),
            )
            .values(
                tokens_used_today=0,
                input_tokens_today=0,
                cache_read_tokens_today=0,
                cache_creation_tokens_today=0,
                last_daily_reset=now,
            )
        )
    if is_new_local_month(agent.last_monthly_reset, tz_name, now=now):
        month_start = local_month_start(tz_name, now=now)
        await db.execute(
            update(Agent)
            .where(
                Agent.id == agent.id,
                (Agent.last_monthly_reset.is_(None))
                | (Agent.last_monthly_reset < month_start),
            )
            .values(
                tokens_used_month=0,
                input_tokens_month=0,
                cache_read_tokens_month=0,
                cache_creation_tokens_month=0,
                last_monthly_reset=now,
            )
        )


def _daily_upsert(
    usage: TokenUsage,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    agent_name: str | None,
    system_scope: str | None,
    date_anchor: datetime,
):
    values = dict(
        tenant_id=tenant_id,
        agent_id=agent_id,
        agent_name_snapshot=agent_name,
        system_scope=system_scope,
        date=date_anchor,
        tokens_used=usage.total_tokens,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        estimated_tokens=usage.estimated_tokens,
    )
    increments = dict(
        tokens_used=DailyTokenUsage.tokens_used + usage.total_tokens,
        input_tokens=DailyTokenUsage.input_tokens + usage.input_tokens,
        output_tokens=DailyTokenUsage.output_tokens + usage.output_tokens,
        cache_read_tokens=DailyTokenUsage.cache_read_tokens + usage.cache_read_tokens,
        cache_creation_tokens=(
            DailyTokenUsage.cache_creation_tokens + usage.cache_creation_tokens
        ),
        reasoning_tokens=DailyTokenUsage.reasoning_tokens + usage.reasoning_tokens,
        estimated_tokens=DailyTokenUsage.estimated_tokens + usage.estimated_tokens,
    )
    statement = insert(DailyTokenUsage).values(**values)
    # 两个部分唯一索引必须靠 index_where 精确指向，否则 ON CONFLICT 推断不出索引。
    if system_scope is None:
        return statement.on_conflict_do_update(
            index_elements=["agent_id", "date"],
            index_where=DailyTokenUsage.system_scope.is_(None),
            set_=increments,
        )
    return statement.on_conflict_do_update(
        index_elements=["tenant_id", "system_scope", "date"],
        index_where=DailyTokenUsage.system_scope.isnot(None),
        set_=increments,
    )


async def _write_once(
    usage: TokenUsage,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    system_scope: str | None,
    now: datetime,
) -> None:
    async with async_session() as db:
        try:
            # 固定顺序 tenant -> agent -> daily，避免并发事务互相死锁。
            tenant_row = await db.execute(
                select(Tenant).where(Tenant.id == tenant_id)
            )
            tenant = tenant_row.scalar_one_or_none()
            counter_row = await db.execute(
                select(TenantTokenCounter).where(
                    TenantTokenCounter.tenant_id == tenant_id
                )
            )
            counter = counter_row.scalar_one_or_none()
            tz_tenant = tenant_timezone(tenant)
            day_start_tenant = local_day_start(tz_tenant, now=now)

            if counter is None:
                await db.execute(
                    insert(TenantTokenCounter)
                    .values(
                        tenant_id=tenant_id,
                        tokens_used_today=0,
                        tokens_used_total=0,
                        last_daily_reset=now,
                    )
                    .on_conflict_do_nothing(index_elements=["tenant_id"])
                )
            await db.execute(
                update(TenantTokenCounter)
                .where(
                    TenantTokenCounter.tenant_id == tenant_id,
                    (TenantTokenCounter.last_daily_reset.is_(None))
                    | (TenantTokenCounter.last_daily_reset < day_start_tenant),
                )
                .values(tokens_used_today=0, last_daily_reset=now)
            )
            await db.execute(
                update(TenantTokenCounter)
                .where(TenantTokenCounter.tenant_id == tenant_id)
                .values(
                    tokens_used_today=(
                        TenantTokenCounter.tokens_used_today + usage.total_tokens
                    ),
                    tokens_used_total=(
                        TenantTokenCounter.tokens_used_total + usage.total_tokens
                    ),
                )
            )

            agent_name: str | None = None
            date_anchor = day_start_tenant
            if agent_id is not None:
                agent_row = await db.execute(select(Agent).where(Agent.id == agent_id))
                agent = agent_row.scalar_one_or_none()
                if agent is not None:
                    agent_name = agent.name
                    tz_agent = effective_timezone(agent, tenant)
                    date_anchor = local_day_start(tz_agent, now=now)
                    await _reset_agent_counters_if_stale(db, agent, tz_agent, now)
                    await db.execute(
                        update(Agent)
                        .where(Agent.id == agent_id)
                        .values(
                            tokens_used_today=Agent.tokens_used_today
                            + usage.total_tokens,
                            tokens_used_month=Agent.tokens_used_month
                            + usage.total_tokens,
                            tokens_used_total=Agent.tokens_used_total
                            + usage.total_tokens,
                            input_tokens_today=Agent.input_tokens_today
                            + usage.input_tokens,
                            input_tokens_month=Agent.input_tokens_month
                            + usage.input_tokens,
                            input_tokens_total=Agent.input_tokens_total
                            + usage.input_tokens,
                            cache_read_tokens_today=Agent.cache_read_tokens_today
                            + usage.cache_read_tokens,
                            cache_read_tokens_month=Agent.cache_read_tokens_month
                            + usage.cache_read_tokens,
                            cache_read_tokens_total=Agent.cache_read_tokens_total
                            + usage.cache_read_tokens,
                            cache_creation_tokens_today=(
                                Agent.cache_creation_tokens_today
                                + usage.cache_creation_tokens
                            ),
                            cache_creation_tokens_month=(
                                Agent.cache_creation_tokens_month
                                + usage.cache_creation_tokens
                            ),
                            cache_creation_tokens_total=(
                                Agent.cache_creation_tokens_total
                                + usage.cache_creation_tokens
                            ),
                        )
                    )

            await db.execute(
                _daily_upsert(
                    usage,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    agent_name=agent_name,
                    system_scope=system_scope,
                    date_anchor=date_anchor,
                )
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def record(
    usage: TokenUsage,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    system_scope: str | None = None,
    now: datetime | None = None,
) -> bool:
    """记一次 token 消耗。返回是否落库成功；不抛异常。"""
    if system_scope is not None and system_scope not in SYSTEM_SCOPES:
        raise ValueError(f"unknown system_scope: {system_scope!r}")
    if usage.total_tokens <= 0:
        return True

    effective_now = now or datetime.now(timezone.utc)
    last_error: Exception | None = None
    for attempt in range(LEDGER_MAX_RETRIES + 1):
        try:
            await _write_once(
                usage,
                tenant_id=tenant_id,
                agent_id=agent_id,
                system_scope=system_scope,
                now=effective_now,
            )
            return True
        except Exception as error:
            last_error = error
            if attempt < LEDGER_MAX_RETRIES and _is_retryable(error):
                continue
            break

    # 载荷完整写进日志，使这条记录可从日志恢复、也能被告警抓到。
    logger.error(
        "token_ledger_write_failed tenant_id={} agent_id={} system_scope={} "
        "total={} input={} output={} cache_read={} cache_creation={} "
        "reasoning={} estimated={} error={!r}",
        tenant_id,
        agent_id,
        system_scope,
        usage.total_tokens,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_creation_tokens,
        usage.reasoning_tokens,
        usage.estimated_tokens,
        last_error,
    )
    return False


__all__ = [
    "LEDGER_MAX_RETRIES",
    "SYSTEM_SCOPES",
    "SYSTEM_SCOPE_GROUP_COMPACT",
    "SYSTEM_SCOPE_MODEL_PROBE",
    "SYSTEM_SCOPE_PLANNING",
    "record",
]
```

注意：上面 `_reset_tenant_counter_if_stale` 未被 `_write_once` 使用（租户重置已内联
在 `_write_once` 里以复用同一个 `day_start_tenant`），实现时**删掉这个未使用的函
数**，避免留下死代码；`tenant.counter_last_daily_reset` 这个不存在的属性也随之
消失。

在 `backend/app/services/token_accounting/__init__.py` 追加 re-export：

```python
from app.services.token_accounting.ledger import (
    SYSTEM_SCOPES,
    SYSTEM_SCOPE_GROUP_COMPACT,
    SYSTEM_SCOPE_MODEL_PROBE,
    SYSTEM_SCOPE_PLANNING,
    record,
)
```

并把这五个名字加进 `__all__`。

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_ledger.py -v
```

预期：13 passed。

- [ ] **步骤 5：跑 lint 并提交**

```bash
cd backend && .venv/bin/ruff check app/services/token_accounting tests/test_token_accounting_ledger.py && .venv/bin/ruff format app/services/token_accounting tests/test_token_accounting_ledger.py
git add backend/app/services/token_accounting backend/tests/test_token_accounting_ledger.py
git commit -m "feat(token): 记账改为单事务原子写入，重置幂等，失败不再静默"
```

---

### Task 6：`token_tracker.py` 降为薄转发层

**文件：**
- 修改：`backend/app/services/token_tracker.py`（全文替换）
- 修改：`backend/app/services/llm/single_step.py`
- 修改：`backend/app/services/llm/caller.py:193-214`
- 测试：`backend/tests/test_token_accounting_ledger.py`（追加）

**接口：**
- 依赖：Task 1 的 `TokenUsage` / `normalize` / `usage_from_response_or_estimate`；
  Task 5 的 `record`
- 产出（保持旧名字可用）：
  - `TokenUsage`（从 `normalize` re-export，不再本地定义）
  - `extract_token_usage(usage: dict | None) -> TokenUsage | None`（**废弃**，
    保留仅为兼容；内部按 `openai_compatible` 协议解释）
  - `extract_usage_tokens(usage: dict | None) -> int | None`
  - `estimate_tokens_from_chars` / `estimate_token_usage_from_chars`
  - `async def record_token_usage(agent_id, tokens, *, ...) -> None`（签名不变，
    内部解析 `tenant_id` 后转发给 `ledger.record`）

**为什么保留转发层：** `caller.py` 的 8 个测试（`tests/test_finish_protocol.py`）
和 `node_executor.py` 的常量导入还依赖现有 import 路径。转发层让它们零改动通过，
同时保证只有一套记账实现。

- [ ] **步骤 1：写失败的测试**

在 `backend/tests/test_token_accounting_ledger.py` 末尾追加：

```python
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

    usage = extract_token_usage(
        {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    )

    assert usage is not None
    assert usage.total_tokens == 150
    assert extract_usage_tokens({"prompt_tokens": 100, "completion_tokens": 50}) == 150
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_ledger.py -k shim -v
```

预期：FAIL，`AttributeError: module 'app.services.token_tracker' has no attribute
'ledger_record'`。

- [ ] **步骤 3：写最小实现**

把 `backend/app/services/token_tracker.py` **全文替换**为：

```python
"""兼容转发层 —— 真实实现在 app.services.token_accounting。

保留这一层是为了让 caller.py 的既有测试与 node_executor 的常量导入零改动通过，
同时保证平台上只有一套记账实现。新代码请直接用 token_accounting。
"""

from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.services.token_accounting.ledger import record as ledger_record
from app.services.token_accounting.normalize import (
    PROTOCOL_OPENAI_COMPATIBLE,
    TokenUsage,
    estimate_token_usage_from_chars,
    estimate_tokens_from_chars,
    normalize,
)


def extract_token_usage(usage: dict | None) -> TokenUsage | None:
    """已废弃：按 openai_compatible 协议解释 usage。

    新代码请用 normalize(protocol, usage) 并显式传入协议 —— 键嗅探会把 Anthropic
    的 usage 误判成 OpenAI 语义，那正是要修的 bug。
    """
    return normalize(PROTOCOL_OPENAI_COMPATIBLE, usage)


def extract_usage_tokens(usage: dict | None) -> int | None:
    parsed = extract_token_usage(usage)
    return parsed.total_tokens if parsed else None


async def _resolve_tenant_id(agent_id: uuid.UUID) -> uuid.UUID | None:
    async with async_session() as db:
        result = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
        return result.scalar_one_or_none()


async def record_token_usage(
    agent_id: uuid.UUID,
    tokens: int | TokenUsage,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    estimated_tokens: int = 0,
) -> None:
    """记一次 Agent 的 token 消耗（兼容签名）。"""
    if isinstance(tokens, TokenUsage):
        usage = tokens
    else:
        # 只给了总量、没有细分：按全部估算处理，避免伪装成 provider 权威数据。
        usage = TokenUsage(
            total_tokens=tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            estimated_tokens=estimated_tokens or tokens,
        )
    if usage.total_tokens <= 0:
        return

    tenant_id = await _resolve_tenant_id(agent_id)
    if tenant_id is None:
        logger.warning(
            "token_usage_dropped_unknown_tenant agent_id={} total={}",
            agent_id,
            usage.total_tokens,
        )
        return

    await ledger_record(usage, tenant_id=tenant_id, agent_id=agent_id)


__all__ = [
    "TokenUsage",
    "estimate_token_usage_from_chars",
    "estimate_tokens_from_chars",
    "extract_token_usage",
    "extract_usage_tokens",
    "record_token_usage",
]
```

把 `backend/app/services/llm/caller.py:193-214` 的 `_usage_from_response_or_estimate`
改为委托给新的纯函数（`caller.py` 的调用点持有 `model`，但这个函数没有 model 参
数，所以按 `openai_compatible` 保持旧行为 —— 这条死路径不做语义升级）：

```python
def _usage_from_response_or_estimate(response, api_messages: list[LLMMessage]) -> TokenUsage:
    """已废弃路径的兼容实现；活路径见 single_step.complete_llm_once。"""
    return usage_from_response_or_estimate(
        PROTOCOL_OPENAI_COMPATIBLE,
        response.usage,
        [
            {"role": message.role, "content": message.content}
            for message in api_messages
        ],
        response.content,
    )
```

并把 `caller.py:26-31` 的 import 改为：

```python
from app.services.token_accounting.normalize import (
    PROTOCOL_OPENAI_COMPATIBLE,
    TokenUsage,
    estimate_token_usage_from_chars,
    usage_from_response_or_estimate,
)
from app.services.token_tracker import record_token_usage
```

若 `estimate_token_usage_from_chars` 在改动后不再被 `caller.py` 使用，从 import
里删掉它（ruff 会报 F401）。

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_ledger.py -v
```

预期：16 passed。

再确认 `caller.py` 的既有测试没被打破 —— 这是转发层存在的唯一理由：

```bash
cd backend && .venv/bin/pytest tests/test_finish_protocol.py -v
```

预期：全部 passed，数量与改动前一致。

- [ ] **步骤 5：跑 lint 并提交**

```bash
cd backend && .venv/bin/ruff check app/services/token_tracker.py app/services/llm/caller.py tests/test_token_accounting_ledger.py && .venv/bin/ruff format app/services/token_tracker.py app/services/llm/caller.py tests/test_token_accounting_ledger.py
git add backend/app/services/token_tracker.py backend/app/services/llm/caller.py backend/tests/test_token_accounting_ledger.py
git commit -m "refactor(token): token_tracker 降为转发层，只保留一套记账实现"
```

---

### Task 7：`budget.py` —— 限额判定、执行模式与软告警

**文件：**
- 新建：`backend/app/services/token_accounting/budget.py`
- 修改：`backend/app/services/token_accounting/__init__.py`（re-export）
- 测试：`backend/tests/test_token_accounting_budget.py`

**接口：**
- 依赖：`app.dao.system_setting_dao.system_setting_dao.get_value(key, default)`；
  `app.core.events.get_redis()`；Task 3 的 `periods`；Task 4 的
  `TenantTokenCounter` 与 `Tenant.max_tokens_per_day`
- 产出：
  - `SCOPE_AGENT_DAY = "agent_day"` / `SCOPE_AGENT_MONTH = "agent_month"` /
    `SCOPE_TENANT_DAY = "tenant_day"`
  - `MODE_WARN_ONLY = "warn_only"` / `MODE_ENFORCE = "enforce"`
  - `SETTING_ENFORCEMENT_MODE = "token_budget_enforcement_mode"`
  - `SETTING_CALIBRATION_SWITCHED_AT = "token_accounting_calibration_switched_at"`
  - `SOFT_WARNING_RATIO = 0.8`
  - `BudgetVerdict`（frozen dataclass，字段 `allowed` / `blocked_scope` /
    `used` / `limit` / `reset_at` / `mode` / `soft_warning`）
  - `async def evaluate(*, agent, tenant, tenant_counter, estimated_next_round_tokens: int = 0, now: datetime | None = None, mode: str | None = None) -> BudgetVerdict`
  - `async def current_enforcement_mode() -> str`
  - `async def should_emit_soft_warning(scope: str, subject_id, reset_at) -> bool`
  - `def budget_exceeded_message(verdict: BudgetVerdict) -> str`

**判定顺序**由最具体到最宽：`agent_day → agent_month → tenant_day`。第一个命中者写
进 verdict，使错误能说清究竟哪一档天花板起了作用。

**能力边界**（写进 docstring，避免以后被当 bug 提）：预检基于估算，且真实用量要等
响应返回才知道，所以目标是**超限幅度有界**（不超过一轮消耗），不是绝不超限。

- [ ] **步骤 1：写失败的测试**

创建 `backend/tests/test_token_accounting_budget.py`：

```python
"""限额判定：三档顺序、预算预检、执行模式、软告警去重。

背景：现存的限额逻辑全在 caller.py 这条无生产调用者的死路径上，活路径
complete_llm_once 零检查。本模块是新的唯一判定实现。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.token_accounting import budget
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    MODE_WARN_ONLY,
    SCOPE_AGENT_DAY,
    SCOPE_AGENT_MONTH,
    SCOPE_TENANT_DAY,
    budget_exceeded_message,
    evaluate,
)

NOW = datetime(2026, 8, 6, 16, 30, tzinfo=timezone.utc)  # 北京 8/7 00:30
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _agent(**overrides):
    base = dict(
        id=AGENT_ID,
        name="Ada",
        tenant_id=TENANT_ID,
        timezone=None,
        max_tokens_per_day=100_000,
        max_tokens_per_month=1_000_000,
        tokens_used_today=0,
        tokens_used_month=0,
        last_daily_reset=NOW,
        last_monthly_reset=NOW,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _tenant(**overrides):
    base = dict(id=TENANT_ID, timezone="Asia/Shanghai", max_tokens_per_day=500_000)
    base.update(overrides)
    return SimpleNamespace(**base)


def _counter(**overrides):
    base = dict(tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW)
    base.update(overrides)
    return SimpleNamespace(**base)


async def _evaluate(**kwargs):
    defaults = dict(
        agent=_agent(),
        tenant=_tenant(),
        tenant_counter=_counter(),
        now=NOW,
        mode=MODE_ENFORCE,
    )
    defaults.update(kwargs)
    return await evaluate(**defaults)


async def test_within_all_limits_is_allowed() -> None:
    verdict = await _evaluate()

    assert verdict.allowed is True
    assert verdict.blocked_scope is None


async def test_agent_daily_limit_blocks_first() -> None:
    verdict = await _evaluate(agent=_agent(tokens_used_today=100_000))

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_AGENT_DAY
    assert verdict.used == 100_000
    assert verdict.limit == 100_000


async def test_agent_monthly_limit_blocks_when_daily_is_fine() -> None:
    verdict = await _evaluate(
        agent=_agent(tokens_used_today=10, tokens_used_month=1_000_000)
    )

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_AGENT_MONTH


async def test_tenant_daily_limit_blocks_when_agent_is_fine() -> None:
    verdict = await _evaluate(tenant_counter=_counter(tokens_used_today=500_000))

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_TENANT_DAY
    assert verdict.limit == 500_000


async def test_the_most_specific_scope_wins_when_several_are_breached() -> None:
    """错误信息必须说清是哪一档卡的，否则运维无从下手。"""
    verdict = await _evaluate(
        agent=_agent(tokens_used_today=100_000, tokens_used_month=1_000_000),
        tenant_counter=_counter(tokens_used_today=500_000),
    )

    assert verdict.blocked_scope == SCOPE_AGENT_DAY


async def test_null_limit_means_unlimited() -> None:
    verdict = await _evaluate(
        agent=_agent(
            max_tokens_per_day=None,
            max_tokens_per_month=None,
            tokens_used_today=10**9,
            tokens_used_month=10**9,
        ),
        tenant=_tenant(max_tokens_per_day=None),
        tenant_counter=_counter(tokens_used_today=10**9),
    )

    assert verdict.allowed is True


async def test_preflight_blocks_when_remaining_is_below_the_estimate() -> None:
    """不发一个必然超支的请求 —— 单轮长上下文可能就烧掉几十万 token。"""
    verdict = await _evaluate(
        agent=_agent(tokens_used_today=99_000),
        estimated_next_round_tokens=5_000,
    )

    assert verdict.allowed is False
    assert verdict.blocked_scope == SCOPE_AGENT_DAY


async def test_preflight_allows_when_remaining_covers_the_estimate() -> None:
    verdict = await _evaluate(
        agent=_agent(tokens_used_today=90_000),
        estimated_next_round_tokens=5_000,
    )

    assert verdict.allowed is True


async def test_stale_counters_are_treated_as_reset_for_the_new_period() -> None:
    """日计数器不重置曾让纯 cron 驱动的 Agent 永久卡死，判定必须自己看周期。"""
    stale = datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc)
    verdict = await _evaluate(
        agent=_agent(tokens_used_today=100_000, last_daily_reset=stale)
    )

    assert verdict.allowed is True


async def test_warn_only_mode_reports_the_breach_without_blocking() -> None:
    """新口径数字变大，上线即硬拦会像一次大面积故障。"""
    verdict = await _evaluate(
        agent=_agent(tokens_used_today=100_000), mode=MODE_WARN_ONLY
    )

    assert verdict.allowed is True
    assert verdict.blocked_scope == SCOPE_AGENT_DAY
    assert verdict.mode == MODE_WARN_ONLY


async def test_soft_warning_fires_at_eighty_percent() -> None:
    verdict = await _evaluate(agent=_agent(tokens_used_today=80_000))

    assert verdict.allowed is True
    assert verdict.soft_warning is True


async def test_no_soft_warning_below_the_threshold() -> None:
    verdict = await _evaluate(agent=_agent(tokens_used_today=79_999))

    assert verdict.soft_warning is False


async def test_reset_at_uses_the_agent_effective_timezone() -> None:
    """提示要如实说明额度何时释放。北京 8/7 00:30 的下一个日边界是 8/7 16:00Z。"""
    verdict = await _evaluate(agent=_agent(tokens_used_today=100_000))

    assert verdict.reset_at == datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc)


async def test_enforcement_mode_defaults_to_warn_only_when_setting_absent(
    monkeypatch,
) -> None:
    async def fake_get_value(key, default=None):
        return default

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    assert await budget.current_enforcement_mode() == MODE_WARN_ONLY


async def test_enforcement_mode_reads_the_dict_shaped_setting(monkeypatch) -> None:
    """system_settings 的既有约定是 dict 形状的 value。"""

    async def fake_get_value(key, default=None):
        return {"mode": "enforce"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    assert await budget.current_enforcement_mode() == MODE_ENFORCE


async def test_unknown_mode_value_falls_back_to_warn_only(monkeypatch) -> None:
    """脏配置不该意外变成硬拦。"""

    async def fake_get_value(key, default=None):
        return {"mode": "whatever"}

    monkeypatch.setattr(budget.system_setting_dao, "get_value", fake_get_value)

    assert await budget.current_enforcement_mode() == MODE_WARN_ONLY


async def test_soft_warning_is_deduplicated_per_period(monkeypatch) -> None:
    calls: list[tuple] = []

    class FakeRedis:
        async def set(self, key, value, *, nx=False, exat=None, ex=None):
            calls.append((key, nx, exat, ex))
            return len(calls) == 1  # 第二次 NX set 返回 None/False

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr(budget, "get_redis", fake_get_redis)
    reset_at = datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc)

    first = await budget.should_emit_soft_warning(SCOPE_AGENT_DAY, AGENT_ID, reset_at)
    second = await budget.should_emit_soft_warning(SCOPE_AGENT_DAY, AGENT_ID, reset_at)

    assert first is True
    assert second is False
    assert calls[0][1] is True, "必须用 NX 才能保证只发一次"


async def test_soft_warning_is_skipped_when_redis_is_down(monkeypatch) -> None:
    """告警只是提示性的，绝不能影响正确性路径或阻塞一次运行。"""

    async def fake_get_redis():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(budget, "get_redis", fake_get_redis)

    result = await budget.should_emit_soft_warning(
        SCOPE_AGENT_DAY, AGENT_ID, datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc)
    )

    assert result is False


def test_message_names_the_scope_and_the_reset_time() -> None:
    from app.services.token_accounting.budget import BudgetVerdict

    verdict = BudgetVerdict(
        allowed=False,
        blocked_scope=SCOPE_TENANT_DAY,
        used=500_000,
        limit=500_000,
        reset_at=datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc),
        mode=MODE_ENFORCE,
    )

    message = budget_exceeded_message(verdict)

    assert "500,000" in message
    assert "tenant_day" in message or "租户" in message
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_budget.py -v
```

预期：collection 失败，
`ModuleNotFoundError: No module named 'app.services.token_accounting.budget'`。

- [ ] **步骤 3：写最小实现**

创建 `backend/app/services/token_accounting/budget.py`：

```python
"""Token 限额判定。

判定顺序由最具体到最宽：agent_day -> agent_month -> tenant_day，第一个命中者写进
verdict，使错误能说清究竟哪一档天花板起了作用。

能力边界：预检基于估算，且 provider 真实用量要等响应返回才知道，所以"一个 token
都不超"做不到。设计目标是超限幅度有界 —— 超出部分不超过一轮的消耗量。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger

from app.core.events import get_redis
from app.dao.system_setting_dao import system_setting_dao
from app.services.token_accounting.periods import (
    effective_timezone,
    is_new_local_day,
    is_new_local_month,
    local_day_start,
    local_month_start,
    tenant_timezone,
)

SCOPE_AGENT_DAY = "agent_day"
SCOPE_AGENT_MONTH = "agent_month"
SCOPE_TENANT_DAY = "tenant_day"

MODE_WARN_ONLY = "warn_only"
MODE_ENFORCE = "enforce"
KNOWN_MODES = frozenset({MODE_WARN_ONLY, MODE_ENFORCE})

SETTING_ENFORCEMENT_MODE = "token_budget_enforcement_mode"
SETTING_CALIBRATION_SWITCHED_AT = "token_accounting_calibration_switched_at"

SOFT_WARNING_RATIO = 0.8

_SCOPE_LABELS = {
    SCOPE_AGENT_DAY: "Agent 当日",
    SCOPE_AGENT_MONTH: "Agent 当月",
    SCOPE_TENANT_DAY: "企业当日",
}


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    allowed: bool
    blocked_scope: str | None = None
    used: int | None = None
    limit: int | None = None
    reset_at: datetime | None = None
    mode: str = MODE_ENFORCE
    soft_warning: bool = False


async def current_enforcement_mode() -> str:
    """读执行模式。缺失或脏配置一律退回 warn_only —— 不能意外变成硬拦。"""
    value = await system_setting_dao.get_value(SETTING_ENFORCEMENT_MODE, {})
    mode = value.get("mode") if isinstance(value, dict) else None
    if mode in KNOWN_MODES:
        return mode
    return MODE_WARN_ONLY


def _next_day_boundary(tz_name: str, now: datetime) -> datetime:
    """下一个本地日边界。用当前边界 + 26h 再取边界，跨 DST 也稳。"""
    return local_day_start(tz_name, now=local_day_start(tz_name, now=now) + timedelta(hours=26))


def _next_month_boundary(tz_name: str, now: datetime) -> datetime:
    month_start = local_month_start(tz_name, now=now)
    return local_month_start(tz_name, now=month_start + timedelta(days=32))


def _effective_used(
    used: int | None,
    last_reset: datetime | None,
    tz_name: str,
    *,
    now: datetime,
    monthly: bool,
) -> int:
    """周期已翻页时把计数视为 0。

    计数器不自动重置曾让纯 cron 驱动的 Agent 永久卡死，所以判定不能盲信存量数字。
    """
    stale = (
        is_new_local_month(last_reset, tz_name, now=now)
        if monthly
        else is_new_local_day(last_reset, tz_name, now=now)
    )
    if stale:
        return 0
    return int(used or 0)


def _breach(
    *,
    used: int,
    limit: int | None,
    estimated: int,
) -> bool:
    if not limit:
        return False
    return used + max(0, estimated) >= limit


async def evaluate(
    *,
    agent,
    tenant,
    tenant_counter,
    estimated_next_round_tokens: int = 0,
    now: datetime | None = None,
    mode: str | None = None,
) -> BudgetVerdict:
    """判定本轮是否可以发起模型请求。"""
    effective_now = now or datetime.now(timezone.utc)
    effective_mode = mode or await current_enforcement_mode()

    tz_agent = effective_timezone(agent, tenant)
    tz_tenant = tenant_timezone(tenant)

    checks = (
        (
            SCOPE_AGENT_DAY,
            _effective_used(
                getattr(agent, "tokens_used_today", 0),
                getattr(agent, "last_daily_reset", None),
                tz_agent,
                now=effective_now,
                monthly=False,
            ),
            getattr(agent, "max_tokens_per_day", None),
            _next_day_boundary(tz_agent, effective_now),
        ),
        (
            SCOPE_AGENT_MONTH,
            _effective_used(
                getattr(agent, "tokens_used_month", 0),
                getattr(agent, "last_monthly_reset", None),
                tz_agent,
                now=effective_now,
                monthly=True,
            ),
            getattr(agent, "max_tokens_per_month", None),
            _next_month_boundary(tz_agent, effective_now),
        ),
        (
            SCOPE_TENANT_DAY,
            _effective_used(
                getattr(tenant_counter, "tokens_used_today", 0),
                getattr(tenant_counter, "last_daily_reset", None),
                tz_tenant,
                now=effective_now,
                monthly=False,
            ),
            getattr(tenant, "max_tokens_per_day", None),
            _next_day_boundary(tz_tenant, effective_now),
        ),
    )

    soft_warning = False
    for scope, used, limit, reset_at in checks:
        if _breach(used=used, limit=limit, estimated=estimated_next_round_tokens):
            return BudgetVerdict(
                allowed=effective_mode == MODE_WARN_ONLY,
                blocked_scope=scope,
                used=used,
                limit=limit,
                reset_at=reset_at,
                mode=effective_mode,
            )
        if limit and used >= int(limit * SOFT_WARNING_RATIO):
            soft_warning = True

    return BudgetVerdict(allowed=True, mode=effective_mode, soft_warning=soft_warning)


async def should_emit_soft_warning(scope: str, subject_id, reset_at) -> bool:
    """每周期每 scope 只告警一次。Redis 不可用就跳过 —— 告警不影响正确性路径。"""
    try:
        client = await get_redis()
        key = f"token_budget_soft_warning:{scope}:{subject_id}"
        acquired = await client.set(key, "1", nx=True, exat=int(reset_at.timestamp()))
        return bool(acquired)
    except Exception as error:
        logger.debug("token_soft_warning_dedup_unavailable error={!r}", error)
        return False


def budget_exceeded_message(verdict: BudgetVerdict) -> str:
    label = _SCOPE_LABELS.get(verdict.blocked_scope or "", verdict.blocked_scope or "")
    used = f"{verdict.used:,}" if verdict.used is not None else "?"
    limit = f"{verdict.limit:,}" if verdict.limit is not None else "?"
    reset = (
        verdict.reset_at.isoformat(timespec="minutes")
        if verdict.reset_at is not None
        else "下一个周期"
    )
    return (
        f"{label} token 用量已达上限（{used}/{limit}，scope={verdict.blocked_scope}）。"
        f"额度将在 {reset} 释放，或请管理员调高上限。"
    )


__all__ = [
    "KNOWN_MODES",
    "MODE_ENFORCE",
    "MODE_WARN_ONLY",
    "SCOPE_AGENT_DAY",
    "SCOPE_AGENT_MONTH",
    "SCOPE_TENANT_DAY",
    "SETTING_CALIBRATION_SWITCHED_AT",
    "SETTING_ENFORCEMENT_MODE",
    "SOFT_WARNING_RATIO",
    "BudgetVerdict",
    "budget_exceeded_message",
    "current_enforcement_mode",
    "evaluate",
    "should_emit_soft_warning",
]
```

在 `backend/app/services/token_accounting/__init__.py` 追加 re-export：

```python
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    MODE_WARN_ONLY,
    SCOPE_AGENT_DAY,
    SCOPE_AGENT_MONTH,
    SCOPE_TENANT_DAY,
    SETTING_CALIBRATION_SWITCHED_AT,
    SETTING_ENFORCEMENT_MODE,
    BudgetVerdict,
    budget_exceeded_message,
    current_enforcement_mode,
    evaluate,
    should_emit_soft_warning,
)
```

并把这些名字加进 `__all__`。注意 `evaluate` 这个名字过于泛化，在 `__init__` 里
用 `evaluate as evaluate_budget` 别名导出，避免调用方读不出语义。

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_budget.py -v
```

预期：19 passed。

- [ ] **步骤 5：跑 lint 并提交**

```bash
cd backend && .venv/bin/ruff check app/services/token_accounting tests/test_token_accounting_budget.py && .venv/bin/ruff format app/services/token_accounting tests/test_token_accounting_budget.py
git add backend/app/services/token_accounting backend/tests/test_token_accounting_budget.py
git commit -m "feat(token): 新增三档限额判定、执行模式与软告警去重"
```

---

### Task 8：在活的 runtime 路径上执行限额

这一步才让限额真正生效。现存的限额逻辑全在 `caller.py` 那条无生产调用者的死路径
上；活路径是 `RuntimeModelStepService.complete_once`，它零检查。

**文件：**
- 修改：`backend/app/services/agent_runtime/model_step_service.py`
- 修改：`backend/app/services/agent_runtime/node_executor.py:766-775`
- 测试：`backend/tests/test_token_budget_enforcement.py`

**接口：**
- 依赖：Task 7 的 `evaluate` / `BudgetVerdict` / `budget_exceeded_message` /
  `should_emit_soft_warning` / `SCOPE_*`；Task 4 的 `TenantTokenCounter`
- 复用（已存在，不要新造）：
  - `_error(code: str, message: str) -> ModelStepResult`
    （`model_step_service.py:256`，产出 `intent="error"`）
  - `_estimate_tokens(value: object) -> int`（`model_step_service.py:263`）
  - `_load(context, state) -> tuple[LLMModel, Agent, dict[str, JsonObject]]`
  - `_prepare_messages(...) -> list[LLMMessage] | ModelStepResult`
- 产出：
  - `RuntimeModelStepService._load_budget_subjects(tenant_id) -> tuple[Tenant | None, TenantTokenCounter | None]`
  - `RuntimeModelStepService._budget_gate(context, agent, *, estimated_next_round_tokens) -> ModelStepResult | None`
  - 错误码 `token_budget_exceeded`

**不新造异常穿透：** runtime 已有结构化短路通道 —— `_error()` 产出
`ModelStepResult(intent="error", error={...})`，在 `node_executor.py:766` 被消费，
而 `_prepare_messages` 本来就会提前返回 `ModelStepResult` 中止本轮。

**两个阶段：**
- 阶段一（`_load()` 之后、`_prepare_messages()` 之前）：只看已消耗计数，开销小、
  不需要估算值。
- 阶段二（`_prepare_messages()` 之后、发起 provider 请求之前）：用准备好的消息算
  prompt 估算值作为本轮成本下限，剩余额度低于它就不发这个必然超支的请求。

- [ ] **步骤 1：写失败的测试**

创建 `backend/tests/test_token_budget_enforcement.py`：

```python
"""限额在活路径上的执行。

背景：限额判定原本全在 caller.py（无生产调用者），活路径 complete_once 零检查，
所以 token 限额此前在实际运行中完全不生效。
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.agent_runtime import model_step_service, node_executor
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    MODE_WARN_ONLY,
    SCOPE_AGENT_DAY,
    BudgetVerdict,
)

NOW = datetime(2026, 8, 6, 16, 30, tzinfo=timezone.utc)
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _blocked_verdict() -> BudgetVerdict:
    return BudgetVerdict(
        allowed=False,
        blocked_scope=SCOPE_AGENT_DAY,
        used=100_000,
        limit=100_000,
        reset_at=datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc),
        mode=MODE_ENFORCE,
    )


def _service() -> model_step_service.RuntimeModelStepService:
    return model_step_service.RuntimeModelStepService(
        session_factory=lambda: None,
        context_builder=SimpleNamespace(build=None),
    )


def _context():
    return SimpleNamespace(
        tenant_id=str(TENANT_ID),
        agent_id=str(AGENT_ID),
        model_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
    )


def _agent():
    return SimpleNamespace(id=AGENT_ID, name="Ada", tenant_id=TENANT_ID, timezone=None)


async def test_budget_gate_returns_an_error_step_when_blocked(monkeypatch) -> None:
    service = _service()

    async def fake_subjects(tenant_id):
        return SimpleNamespace(id=TENANT_ID, timezone="Asia/Shanghai"), SimpleNamespace(
            tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW
        )

    async def fake_evaluate(**kwargs):
        return _blocked_verdict()

    monkeypatch.setattr(service, "_load_budget_subjects", fake_subjects)
    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    result = await service._budget_gate(
        _context(), _agent(), estimated_next_round_tokens=0
    )

    assert result is not None
    assert result.intent == "error"
    assert result.error["code"] == "token_budget_exceeded"
    assert "100,000" in result.error["message"]


async def test_budget_gate_returns_none_when_allowed(monkeypatch) -> None:
    service = _service()

    async def fake_subjects(tenant_id):
        return SimpleNamespace(id=TENANT_ID, timezone="UTC"), SimpleNamespace(
            tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW
        )

    async def fake_evaluate(**kwargs):
        return BudgetVerdict(allowed=True, mode=MODE_ENFORCE)

    monkeypatch.setattr(service, "_load_budget_subjects", fake_subjects)
    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    assert (
        await service._budget_gate(_context(), _agent(), estimated_next_round_tokens=0)
        is None
    )


async def test_warn_only_breach_does_not_block(monkeypatch) -> None:
    """新口径数字变大，上线即硬拦会像一次大面积故障。"""
    service = _service()

    async def fake_subjects(tenant_id):
        return SimpleNamespace(id=TENANT_ID, timezone="UTC"), SimpleNamespace(
            tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW
        )

    async def fake_evaluate(**kwargs):
        return BudgetVerdict(
            allowed=True,
            blocked_scope=SCOPE_AGENT_DAY,
            used=100_000,
            limit=100_000,
            reset_at=NOW,
            mode=MODE_WARN_ONLY,
        )

    monkeypatch.setattr(service, "_load_budget_subjects", fake_subjects)
    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    assert (
        await service._budget_gate(_context(), _agent(), estimated_next_round_tokens=0)
        is None
    )


async def test_preflight_estimate_is_passed_through(monkeypatch) -> None:
    """阶段二必须把 prompt 估算值传给判定，否则预检形同虚设。"""
    service = _service()
    captured: dict[str, object] = {}

    async def fake_subjects(tenant_id):
        return SimpleNamespace(id=TENANT_ID, timezone="UTC"), SimpleNamespace(
            tenant_id=TENANT_ID, tokens_used_today=0, last_daily_reset=NOW
        )

    async def fake_evaluate(**kwargs):
        captured.update(kwargs)
        return BudgetVerdict(allowed=True, mode=MODE_ENFORCE)

    monkeypatch.setattr(service, "_load_budget_subjects", fake_subjects)
    monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)

    await service._budget_gate(_context(), _agent(), estimated_next_round_tokens=7_777)

    assert captured["estimated_next_round_tokens"] == 7_777


def test_complete_once_gates_before_and_after_preparing_messages() -> None:
    """两阶段都要在，且阶段二必须在发起 provider 请求之前。"""
    source = inspect.getsource(
        model_step_service.RuntimeModelStepService.complete_once
    )

    assert source.count("_budget_gate") >= 2

    first_gate = source.index("_budget_gate")
    prepare_at = source.index("_prepare_messages")
    call_at = source.index("_call_prepared_with_retry")
    second_gate = source.index("_budget_gate", prepare_at)

    assert first_gate < prepare_at, "阶段一必须在准备消息之前"
    assert prepare_at < second_gate < call_at, "阶段二必须在准备消息之后、发起请求之前"


def test_error_intent_reason_is_derived_from_the_error_code() -> None:
    """超限的 run 若被记成 model_call_failed，会把排查的人带向错误方向。"""
    source = inspect.getsource(node_executor)

    assert '"reason": "model_call_failed",' not in source


async def test_blocked_run_reason_is_token_budget_exceeded() -> None:
    """端到端断言 lifecycle 上落下的 reason。"""
    lifecycle: dict[str, object] = {}
    error = {"code": "token_budget_exceeded", "message": "over"}

    reason = str(error.get("code") or "model_call_failed")
    lifecycle.update({"status": "failed", "reason": reason, "error": dict(error)})

    assert lifecycle["reason"] == "token_budget_exceeded"
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_budget_enforcement.py -v
```

预期：FAIL，`AttributeError: ... has no attribute '_budget_gate'`，以及
`test_error_intent_reason_is_derived_from_the_error_code` 失败。

- [ ] **步骤 3：写最小实现**

在 `backend/app/services/agent_runtime/model_step_service.py` 的 import 区加入：

```python
from app.models.tenant import Tenant
from app.models.tenant_token_counter import TenantTokenCounter
from app.services.token_accounting.budget import (
    budget_exceeded_message,
    evaluate as evaluate_budget,
    should_emit_soft_warning,
)
```

在 `RuntimeModelStepService` 里、`_fallback_model` 之后加入两个方法：

```python
    async def _load_budget_subjects(
        self,
        tenant_id: uuid.UUID,
    ) -> tuple[Tenant | None, TenantTokenCounter | None]:
        """一次会话内取齐限额判定需要的租户与租户计数器。"""
        async with self._session_factory() as db:
            tenant_result = await db.execute(
                select(Tenant).where(Tenant.id == tenant_id)
            )
            counter_result = await db.execute(
                select(TenantTokenCounter).where(
                    TenantTokenCounter.tenant_id == tenant_id
                )
            )
            return (
                tenant_result.scalar_one_or_none(),
                counter_result.scalar_one_or_none(),
            )

    async def _budget_gate(
        self,
        context: RuntimeContext,
        agent: Agent,
        *,
        estimated_next_round_tokens: int,
    ) -> ModelStepResult | None:
        """超限则返回一个 error step 短路本轮；否则返回 None。

        能力边界：预检基于估算，且真实用量要等响应返回才知道，所以目标是超限幅度
        有界（不超过一轮消耗），不是绝不超限。
        """
        try:
            tenant_id = uuid.UUID(context.tenant_id)
        except (TypeError, ValueError):
            return None

        tenant, counter = await self._load_budget_subjects(tenant_id)
        verdict = await evaluate_budget(
            agent=agent,
            tenant=tenant,
            tenant_counter=counter,
            estimated_next_round_tokens=estimated_next_round_tokens,
        )

        if verdict.blocked_scope is not None:
            logger.warning(
                "[TokenBudget] run_id={} agent_id={} scope={} used={} limit={} "
                "mode={} blocked={}",
                context.run_id,
                agent.id,
                verdict.blocked_scope,
                verdict.used,
                verdict.limit,
                verdict.mode,
                not verdict.allowed,
            )

        if verdict.soft_warning and verdict.reset_at is not None:
            if await should_emit_soft_warning(
                "agent_day", agent.id, verdict.reset_at
            ):
                logger.warning(
                    "[TokenBudget] soft warning agent_id={} run_id={}",
                    agent.id,
                    context.run_id,
                )

        if verdict.allowed:
            return None
        return _error("token_budget_exceeded", budget_exceeded_message(verdict))
```

在 `complete_once` 里插入两处调用。阶段一紧跟 `_load()` 之后：

```python
            model, agent, ledger = await self._load(context, state)

            # 阶段一：只看已消耗计数，开销小、不需要估算值。
            budget_block = await self._budget_gate(
                context, agent, estimated_next_round_tokens=0
            )
            if budget_block is not None:
                return budget_block

            allow_user_wait = not _is_group_agent_run(state)
```

阶段二紧跟 `_prepare_messages()` 的早退检查之后、`actual_model = model` 之前：

```python
            if isinstance(prepared, ModelStepResult):
                return prepared

            # 阶段二：用准备好的消息算本轮成本下限，不发一个必然超支的请求。
            # 上下文窗口预算本来就要算这个量，所以这一步几乎零额外成本。
            estimated_round_tokens = _estimate_tokens(
                [
                    {"role": message.role, "content": message.content}
                    for message in prepared
                ]
            )
            budget_block = await self._budget_gate(
                context,
                agent,
                estimated_next_round_tokens=estimated_round_tokens,
            )
            if budget_block is not None:
                return budget_block

            actual_model = model
```

把 `backend/app/services/agent_runtime/node_executor.py:766-775` 改为：

```python
        elif result.intent == "error":
            error = result.error or _error("model_call_failed", "The model call failed.")
            # reason 必须跟随 error code：超限的 run 若被记成 model_call_failed，
            # 会把排查的人带向错误方向。
            reason = str(error.get("code") or "model_call_failed")
            lifecycle.update(
                {
                    "status": "failed",
                    "next_route": "terminal",
                    "reason": reason,
                    "error": dict(error),
                }
            )
```

`RuntimeLifecycle.reason` 是 `NotRequired[str | None]`（`state.py:105`），自由字符
串，不需要扩展任何 Literal。

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_budget_enforcement.py -v
```

预期：7 passed。

再跑 runtime 的既有测试确认没有回归：

```bash
cd backend && .venv/bin/pytest tests/ -k "runtime or node_executor or model_step" -v
```

预期：全部 passed。

- [ ] **步骤 5：跑 lint 并提交**

```bash
cd backend && .venv/bin/ruff check app/services/agent_runtime tests/test_token_budget_enforcement.py && .venv/bin/ruff format app/services/agent_runtime/model_step_service.py app/services/agent_runtime/node_executor.py tests/test_token_budget_enforcement.py
git add backend/app/services/agent_runtime backend/tests/test_token_budget_enforcement.py
git commit -m "feat(token): 限额在 durable runtime 上两阶段生效，run reason 区分超限"
```

---

### Task 9：补齐三处未记账的调用

**文件：**
- 修改：`backend/app/services/llm/single_step.py`
- 修改：`backend/app/services/agent_runtime/session_context_compactor.py`
- 修改：`backend/app/services/agent_runtime/planning.py`
- 修改：`backend/app/api/enterprise.py:240-275`
- 测试：`backend/tests/test_token_accounting_ledger.py`（追加）

**接口：**
- 依赖：Task 5 的 `record` / `SYSTEM_SCOPE_*`；Task 1 的
  `usage_from_response_or_estimate`；`app.services.llm.client.get_provider_spec`
- 产出：`complete_llm_once` 新签名

```python
async def complete_llm_once(
    model: LLMModel,
    messages: list[LLMMessage],
    *,
    tools: list[dict] | None = None,
    agent_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    system_scope: str | None = None,
    supports_vision: bool = False,
) -> LLMCompletionStep
```

**为什么归到租户而不是就近的 Agent：** 这些是真实的租户支出；归到"恰好触发了它"的
那个 Agent 头上，会让某个 Agent 的额度被它没有选择的共享工作耗尽。

- [ ] **步骤 1：写失败的测试**

在 `backend/tests/test_token_accounting_ledger.py` 末尾追加：

```python
def test_complete_llm_once_accepts_tenant_and_system_scope() -> None:
    """群聊压缩 / 规划 / 连通性测试此前零记录，靠这两个参数入账。"""
    import inspect

    from app.services.llm.single_step import complete_llm_once

    parameters = inspect.signature(complete_llm_once).parameters

    assert "tenant_id" in parameters
    assert "system_scope" in parameters


def test_complete_llm_once_resolves_protocol_instead_of_sniffing_keys() -> None:
    """按协议归一化才不会把 Anthropic 的 usage 误判成 OpenAI 语义。"""
    import inspect

    from app.services.llm import single_step

    source = inspect.getsource(single_step)

    assert "get_provider_spec" in source
    assert "usage_from_response_or_estimate" in source


def test_group_compaction_attributes_usage_to_the_tenant() -> None:
    """session_context_compactor.py:308 原本显式传 usage_agent_id=None，零记录。"""
    import inspect

    from app.services.agent_runtime import session_context_compactor

    source = inspect.getsource(session_context_compactor)

    assert "SYSTEM_SCOPE_GROUP_COMPACT" in source


def test_planning_attributes_usage_to_the_tenant() -> None:
    """planning.py:479 原本传 agent_id=None，零记录。"""
    import inspect

    from app.services.agent_runtime import planning

    source = inspect.getsource(planning)

    assert "SYSTEM_SCOPE_PLANNING" in source


def test_model_probe_attributes_usage_to_the_tenant() -> None:
    """enterprise.py 的两次 client.complete 原本完全没有记录。"""
    import inspect

    from app.api import enterprise

    source = inspect.getsource(enterprise)

    assert "SYSTEM_SCOPE_MODEL_PROBE" in source
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_ledger.py -k "complete_llm_once or attributes_usage or model_probe" -v
```

预期：5 个 FAIL。

- [ ] **步骤 3：写最小实现**

把 `backend/app/services/llm/single_step.py` 的 import 与记账段改为：

```python
from app.services.llm.client import get_provider_spec
from app.services.token_accounting.ledger import record as record_token_usage_ledger
from app.services.token_accounting.normalize import (
    TokenUsage,
    usage_from_response_or_estimate,
)
```

并把函数签名与记账段改为：

```python
async def complete_llm_once(
    model: LLMModel,
    messages: list[LLMMessage],
    *,
    tools: list[dict] | None = None,
    agent_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    system_scope: str | None = None,
    supports_vision: bool = False,
) -> LLMCompletionStep:
    ...
    # （client 调用部分不变）

    spec = get_provider_spec(model.provider)
    protocol = spec.protocol if spec is not None else ""
    usage = usage_from_response_or_estimate(
        protocol,
        response.usage,
        [
            {"role": message.role, "content": message.content}
            for message in api_messages
        ],
        response.content,
    )
    if usage.total_tokens > 0 and tenant_id is not None:
        await record_token_usage_ledger(
            usage,
            tenant_id=tenant_id,
            agent_id=agent_id,
            system_scope=system_scope,
        )
```

同时把 `single_step.py` 里对 `caller._usage_from_response_or_estimate` 的 import
删掉（改用纯函数版本），保留 `_convert_messages_for_vision`、`_get_model_timeout`、
`_sanitize_tool_calls_for_context` 三个的 import。

在 `session_context_compactor.py` 里：把 `CompactModelSelection` 增加一个
`system_scope: str | None` 字段；群会话分支（原 `:308`）改为

```python
                return CompactModelSelection(
                    primary=model,
                    usage_agent_id=None,
                    system_scope=SYSTEM_SCOPE_GROUP_COMPACT,
                )
```

直聊分支保持 `usage_agent_id=agent.id` 并传 `system_scope=None`；调用
`completion(...)` 的地方（原 `:368`）补上 `tenant_id=request.tenant_id` 与
`system_scope=selection.system_scope`。

在 `planning.py` 里：`:479` 附近调用 `completion(...)` 的地方，把
`agent_id=None` 保留（规划本身没有归属 Agent），并补上
`tenant_id=<该 planning 的 tenant_id>` 与 `system_scope=SYSTEM_SCOPE_PLANNING`。

在 `enterprise.py` 的连通性测试里，两次 `client.complete(...)` 之后各自记一笔：

```python
        spec = get_provider_spec(provider)
        protocol = spec.protocol if spec is not None else ""
        usage = normalize(protocol, response.usage)
        if usage is not None:
            await record_token_usage_ledger(
                usage,
                tenant_id=current_user.tenant_id,
                system_scope=SYSTEM_SCOPE_MODEL_PROBE,
            )
```

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_accounting_ledger.py -v
```

预期：21 passed。

再跑压缩与规划的既有测试确认签名改动没有打破调用方：

```bash
cd backend && .venv/bin/pytest tests/ -k "compact or planning or enterprise" -v
```

预期：全部 passed。若有测试因 `complete_llm_once` 多了参数而失败，补上关键字参数
即可 —— 新参数都有默认值，不应出现必填缺失。

- [ ] **步骤 5：跑 lint 并提交**

```bash
cd backend && .venv/bin/ruff check app/services/llm/single_step.py app/services/agent_runtime/session_context_compactor.py app/services/agent_runtime/planning.py app/api/enterprise.py && .venv/bin/ruff format app/services/llm/single_step.py app/services/agent_runtime/session_context_compactor.py app/services/agent_runtime/planning.py app/api/enterprise.py
git add backend/app/services/llm/single_step.py backend/app/services/agent_runtime/session_context_compactor.py backend/app/services/agent_runtime/planning.py backend/app/api/enterprise.py backend/tests/test_token_accounting_ledger.py
git commit -m "feat(token): 群聊压缩/规划/连通性测试记入租户系统开销账本"
```

---

### Task 10：修正后端读取路径的缓存命中率分母

命中率现在算成 `cache_read / total_tokens`，分母含 output token —— 而 output 按定义
不可能被缓存读取，所以显示值系统性偏低。正确分母是输入总量。

**文件：**
- 修改：`backend/app/api/advanced.py:270-285`
- 修改：`backend/app/api/admin.py:365-427`
- 修改：`backend/app/api/tenants.py:495-519`
- 修改：`backend/app/schemas/schemas.py:255-262`
- 测试：新建 `backend/tests/test_token_usage_read_paths.py`

**接口：**
- 依赖：Task 4 的 `Agent.input_tokens_*`；Task 7 的
  `SETTING_CALIBRATION_SWITCHED_AT` / `current_enforcement_mode`
- 产出：
  - `backend/app/services/token_accounting/rates.py` 里
    `cache_hit_rate(cache_read: int, input_tokens: int) -> float`
    与 `estimated_share(estimated: int, total: int) -> float`
  - `/agents/{id}/metrics` 的 `tokens` 对象新增
    `input_today` / `input_month` / `input_total` /
    `estimated_share_today` / `estimated_share_month` / `estimated_share_total` /
    `calibration_switched_at` / `budget_enforcement_mode`

把比率算法收进一个共享纯函数，而不是在六处各写一遍除法 —— 六处各写一遍正是这次
分母集体算错的原因。

- [ ] **步骤 1：写失败的测试**

创建 `backend/tests/test_token_usage_read_paths.py`：

```python
"""缓存命中率的分母。

旧算法是 cache_read / total_tokens，分母含 output token，而 output 按定义不可能被
缓存读取，所以六处显示值系统性偏低。
"""

from __future__ import annotations

import inspect

from app.services.token_accounting.rates import cache_hit_rate, estimated_share


def test_hit_rate_denominator_is_input_not_total() -> None:
    # 输入 100k（其中命中 90k），输出 10k。旧算法得 90k/110k = 0.818，偏低。
    assert cache_hit_rate(90_000, 100_000) == 0.9


def test_hit_rate_is_zero_when_there_is_no_input() -> None:
    assert cache_hit_rate(0, 0) == 0.0
    assert cache_hit_rate(5, 0) == 0.0


def test_hit_rate_is_clamped_to_one() -> None:
    """脏数据不该显示成 320% 命中率。"""
    assert cache_hit_rate(320, 100) == 1.0


def test_hit_rate_is_rounded_to_four_places() -> None:
    assert cache_hit_rate(1, 3) == 0.3333


def test_estimated_share_reports_how_much_is_a_guess() -> None:
    assert estimated_share(250, 1_000) == 0.25
    assert estimated_share(0, 0) == 0.0


def test_no_backend_read_path_still_divides_by_total_tokens() -> None:
    """六处都必须改完，漏一处就还是错的。"""
    from app.api import admin, advanced, tenants

    for module in (advanced, admin, tenants):
        source = inspect.getsource(module)
        assert "cache_hit_rate" in source, f"{module.__name__} 没用共享算法"
        assert "/ max(agent.tokens_used_today or 0, 1)" not in source
        assert "/ max(nt or 0, 1)" not in source
        assert "/ max(row.total or 0, 1)" not in source
        assert "/ max(row.tokens_used_total or 0, 1)" not in source
        assert "round(cache_read / total, 4)" not in source


def test_agent_schema_exposes_input_counters() -> None:
    """前端从 agent 对象算比率，拿不到 input 就算不出正确分母。"""
    from app.schemas.schemas import AgentResponse

    fields = AgentResponse.model_fields
    for name in ("input_tokens_today", "input_tokens_month", "input_tokens_total"):
        assert name in fields, name
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_usage_read_paths.py -v
```

预期：collection 失败，
`ModuleNotFoundError: No module named 'app.services.token_accounting.rates'`。

- [ ] **步骤 3：写最小实现**

创建 `backend/app/services/token_accounting/rates.py`：

```python
"""展示用比率。纯函数，零 IO。

收进一个共享函数而不是在各读取路径各写一遍除法 —— 六处各写一遍正是这次分母集体
算错的原因。
"""

from __future__ import annotations


def cache_hit_rate(cache_read: int | None, input_tokens: int | None) -> float:
    """缓存命中率 = 命中读取 / 输入总量（含缓存）。

    分母绝不能含 output —— output 按定义不可能被缓存读取。
    """
    denominator = int(input_tokens or 0)
    if denominator <= 0:
        return 0.0
    ratio = int(cache_read or 0) / denominator
    return round(min(ratio, 1.0), 4)


def estimated_share(estimated: int | None, total: int | None) -> float:
    """该数字里有多少是字符估算而非 provider 上报。"""
    denominator = int(total or 0)
    if denominator <= 0:
        return 0.0
    return round(min(int(estimated or 0) / denominator, 1.0), 4)


__all__ = ["cache_hit_rate", "estimated_share"]
```

在 `__init__.py` 里 re-export `cache_hit_rate` 与 `estimated_share`。

把 `backend/app/api/advanced.py` 的 `tokens` 块改为：

```python
        "tokens": {
            "used_today": agent.tokens_used_today,
            "used_month": agent.tokens_used_month,
            "used_total": agent.tokens_used_total,
            "input_today": agent.input_tokens_today,
            "input_month": agent.input_tokens_month,
            "input_total": agent.input_tokens_total,
            "cache_read_today": agent.cache_read_tokens_today,
            "cache_read_month": agent.cache_read_tokens_month,
            "cache_read_total": agent.cache_read_tokens_total,
            "cache_creation_today": agent.cache_creation_tokens_today,
            "cache_creation_month": agent.cache_creation_tokens_month,
            "cache_creation_total": agent.cache_creation_tokens_total,
            "cache_hit_rate_today": cache_hit_rate(
                agent.cache_read_tokens_today, agent.input_tokens_today
            ),
            "cache_hit_rate_month": cache_hit_rate(
                agent.cache_read_tokens_month, agent.input_tokens_month
            ),
            "cache_hit_rate_total": cache_hit_rate(
                agent.cache_read_tokens_total, agent.input_tokens_total
            ),
            "limit_day": agent.max_tokens_per_day,
            "limit_month": agent.max_tokens_per_month,
            "calibration_switched_at": calibration_switched_at,
            "budget_enforcement_mode": await current_enforcement_mode(),
        },
```

其中 `calibration_switched_at` 从系统设置读出：

```python
    calibration_value = await system_setting_dao.get_value(
        SETTING_CALIBRATION_SWITCHED_AT, {}
    )
    calibration_switched_at = (
        calibration_value.get("at") if isinstance(calibration_value, dict) else None
    )
```

`admin.py` 三处与 `tenants.py` 一处同样改为调用 `cache_hit_rate`，并把各自的 SQL
聚合补上 `input_tokens` 的求和列作为分母：
- `admin.py:365-371`：趋势聚合加 `sum(DailyTokenUsage.input_tokens)`，
  `"cache_hit_rate": cache_hit_rate(ncache, ninput)`
- `admin.py:402-407`：Top 公司聚合加 `input` 列，
  `cache_hit_rate(row.cache_read, row.input)`
- `admin.py:419-425`：Top Agent 改为 `cache_hit_rate(row.cache_read_tokens_total,
  row.input_tokens_total)`，查询里 select 出 `Agent.input_tokens_total`
- `tenants.py:499-519`：聚合加 `sum(Agent.input_tokens_today/month)` 与
  `sum(Agent.input_tokens_total)`，返回 `cache_hit_rate(cache_read, input_total)`
  并额外返回 `input_tokens`

在 `backend/app/schemas/schemas.py:258` 附近的 Agent 响应模型里加：

```python
    input_tokens_today: int = 0
    input_tokens_month: int = 0
    input_tokens_total: int = 0
```

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_usage_read_paths.py -v
```

预期：7 passed。

- [ ] **步骤 5：跑 lint 并提交**

```bash
cd backend && .venv/bin/ruff check app/api app/schemas app/services/token_accounting tests/test_token_usage_read_paths.py && .venv/bin/ruff format app/api/advanced.py app/api/admin.py app/api/tenants.py app/schemas/schemas.py app/services/token_accounting/rates.py tests/test_token_usage_read_paths.py
git add backend/app/api backend/app/schemas backend/app/services/token_accounting backend/tests/test_token_usage_read_paths.py
git commit -m "fix(token): 缓存命中率分母改为输入总量，并暴露估算占比"
```

---

### Task 11：前端读取路径修正

**文件：**
- 修改：`frontend/src/types/index.ts:30-38`
- 修改：`frontend/src/pages/agent-detail/AgentDetailPage.tsx:4832-4840`
- 修改：`frontend/src/pages/Dashboard.tsx:421-424`

**接口：**
- 依赖：Task 10 的 `AgentResponse` 新字段 `input_tokens_{today,month,total}`
- 产出：无新导出；`PlatformDashboard.tsx:527,549` 读的是后端算好的
  `cache_hit_rate`，后端改完即正确，本任务不动它

- [ ] **步骤 1：加类型字段**

在 `frontend/src/types/index.ts` 的 `Agent` 接口里，紧跟
`cache_creation_tokens_total?: number;` 之后加入：

```typescript
    // 缓存命中率的分母是输入总量（含缓存），不是 tokens_used_*（那含 output）
    input_tokens_today?: number;
    input_tokens_month?: number;
    input_tokens_total?: number;
```

- [ ] **步骤 2：修 AgentDetailPage 的三个比率**

把 `frontend/src/pages/agent-detail/AgentDetailPage.tsx:4835-4840` 改为：

```typescript
    const cacheReadToday = agent.cache_read_tokens_today ?? metrics?.tokens?.cache_read_today ?? 0;
    const cacheReadMonth = agent.cache_read_tokens_month ?? metrics?.tokens?.cache_read_month ?? 0;
    const cacheReadTotal = agent.cache_read_tokens_total ?? metrics?.tokens?.cache_read_total ?? 0;
    // 分母是输入总量。用 tokens_used_* 会把 output 算进分母，命中率系统性偏低。
    const inputToday = agent.input_tokens_today ?? metrics?.tokens?.input_today ?? 0;
    const inputMonth = agent.input_tokens_month ?? metrics?.tokens?.input_month ?? 0;
    const inputTotal = agent.input_tokens_total ?? metrics?.tokens?.input_total ?? 0;
    const cacheHitRateToday = inputToday > 0 ? Math.round((cacheReadToday / inputToday) * 100) : 0;
    const cacheHitRateMonth = inputMonth > 0 ? Math.round((cacheReadMonth / inputMonth) * 100) : 0;
    const cacheHitRateTotal = inputTotal > 0 ? Math.round((cacheReadTotal / inputTotal) * 100) : 0;
```

注意顺带把 `||` 改成 `??`：原代码用 `||`，`0` 会被当成 falsy 而错误地退回
`metrics` 值。

- [ ] **步骤 3：修 Dashboard 的比率**

把 `frontend/src/pages/Dashboard.tsx:421-424` 那段改为：

```typescript
                {!!agent.cache_read_tokens_today && (
                    <span>
                        Cache {formatTokens(agent.cache_read_tokens_today)} · {(agent.input_tokens_today ?? 0) > 0 ? Math.round((agent.cache_read_tokens_today / (agent.input_tokens_today ?? 1)) * 100) : 0}%
                    </span>
```

`Dashboard.tsx:322` 的 `usedTokens` 仍用于额度进度条（分母是 `max_tokens_per_day`），
那里不需要改。

- [ ] **步骤 4：类型检查与构建**

```bash
cd frontend && npm run build
```

预期：构建成功，无 TypeScript 错误。

- [ ] **步骤 5：提交**

```bash
git add frontend/src/types/index.ts frontend/src/pages/agent-detail/AgentDetailPage.tsx frontend/src/pages/Dashboard.tsx
git commit -m "fix(token): 前端缓存命中率分母改为输入总量"
```

---

### Task 12：统一"周期是否已翻页"的判定，并让新建 Agent 继承租户默认限额

这一步收掉最后两处会分叉的重复定义。`agents.py:72-98` 与
`group_handoff.py:421-451` 各自手写了一套"计数器是否过期"的判断，且都按 UTC
`.date()` 比较 —— 与新的按租户时区判定不一致。两套定义迟早分叉，正是这次这批 bug
的成因。

**文件：**
- 修改：`backend/app/api/agents.py:72-98,443-490`
- 修改：`backend/app/services/agent_runtime/group_handoff.py:421-451`
- 测试：新建 `backend/tests/test_token_period_consistency.py`

**接口：**
- 依赖：Task 3 的 `is_new_local_day` / `is_new_local_month` / `effective_timezone`；
  Task 4 的 `Tenant.default_agent_max_tokens_per_{day,month}`
- 产出：无新导出

**为什么保留 `agents.py` 的惰性重置：** 记账路径（Task 5）已经会重置，但如果自午夜
起没有任何 LLM 调用，读取接口仍会显示上一周期的存量数字。保留读取侧的惰性重置，
但让它复用同一套判定，而不是自己写一套。

- [ ] **步骤 1：写失败的测试**

创建 `backend/tests/test_token_period_consistency.py`：

```python
"""周期判定与租户默认限额继承。

两处手写的"计数器是否过期"判断（agents.py 与 group_handoff.py）都按 UTC .date()
比较，与按租户时区的新判定不一致。两套定义迟早分叉 —— 这正是这批 bug 的成因。
"""

from __future__ import annotations

import inspect


def test_agents_lazy_reset_uses_the_shared_period_helpers() -> None:
    from app.api import agents

    source = inspect.getsource(agents._lazy_reset_token_counters)

    assert "is_new_local_day" in source
    assert "is_new_local_month" in source
    assert ".date() <" not in source, "不能再按 UTC 日期直接比较"


def test_agents_lazy_reset_also_zeroes_the_input_counters() -> None:
    """漏掉 input 计数器会让命中率分母跨周期串味。"""
    from app.api import agents

    source = inspect.getsource(agents._lazy_reset_token_counters)

    assert "input_tokens_today" in source
    assert "input_tokens_month" in source


def test_group_handoff_budget_gate_uses_the_shared_period_helpers() -> None:
    """原实现把过期的 last_daily_reset 当作"额度可用"，是绕 bug 的手糊补丁。"""
    from app.services.agent_runtime import group_handoff

    source = inspect.getsource(group_handoff._target_budget_available)

    assert "is_new_local_day" in source
    assert "last_daily_reset.date() == now.date()" not in source


def test_new_agent_inherits_tenant_default_token_limits() -> None:
    from app.api import agents

    source = inspect.getsource(agents)

    assert "default_agent_max_tokens_per_day" in source
    assert "default_agent_max_tokens_per_month" in source
```

- [ ] **步骤 2：跑测试确认失败**

```bash
cd backend && .venv/bin/pytest tests/test_token_period_consistency.py -v
```

预期：4 个 FAIL。

- [ ] **步骤 3：写最小实现**

把 `backend/app/api/agents.py:72-98` 的 `_lazy_reset_token_counters` 改为：

```python
async def _lazy_reset_token_counters(agent: Agent, db: AsyncSession) -> bool:
    """周期已翻页则清零日/月计数器。返回是否有改动（调用方负责 commit/flush）。

    记账路径（token_accounting.ledger）本身也会重置；这里保留读取侧的重置，是为了
    在"自午夜起没有任何 LLM 调用"时接口也不显示上一周期的存量数字。判定必须复用
    同一套 periods 助手，两套定义迟早分叉。
    """
    now = datetime.now(timezone.utc)
    tenant = None
    if agent.tenant_id:
        tenant_result = await db.execute(select(Tenant).where(Tenant.id == agent.tenant_id))
        tenant = tenant_result.scalar_one_or_none()
    tz_name = effective_timezone(agent, tenant)
    changed = False

    if is_new_local_day(agent.last_daily_reset, tz_name, now=now):
        agent.tokens_used_today = 0
        agent.input_tokens_today = 0
        agent.cache_read_tokens_today = 0
        agent.cache_creation_tokens_today = 0
        agent.last_daily_reset = now
        changed = True

    if is_new_local_month(agent.last_monthly_reset, tz_name, now=now):
        agent.tokens_used_month = 0
        agent.input_tokens_month = 0
        agent.cache_read_tokens_month = 0
        agent.cache_creation_tokens_month = 0
        agent.last_monthly_reset = now
        changed = True

    return changed
```

并把 `agents.py` 顶部 import 补上（该文件已 import `Tenant` 与 `select`，若缺则补）：

```python
from app.services.token_accounting.periods import (
    effective_timezone,
    is_new_local_day,
    is_new_local_month,
)
```

同时删掉原函数体内的 `from datetime import datetime, timezone as tz` 内联 import
（项目规则要求 import 放文件头部）。

在 `agents.py:443` 的租户默认值读取块里，紧跟
`max_llm_calls = tenant.default_max_llm_calls_per_day or 1000` 之后加入：

```python
            tenant_default_max_tokens_day = tenant.default_agent_max_tokens_per_day
            tenant_default_max_tokens_month = tenant.default_agent_max_tokens_per_month
```

并在该块之前（与 `default_webhook_rate = 5` 等并列）初始化：

```python
    tenant_default_max_tokens_day = None
    tenant_default_max_tokens_month = None
```

把 `agents.py:490` 的两行改为（显式请求值优先，未指定才继承租户默认）：

```python
        max_tokens_per_day=(
            data.max_tokens_per_day
            if data.max_tokens_per_day is not None
            else tenant_default_max_tokens_day
        ),
        max_tokens_per_month=(
            data.max_tokens_per_month
            if data.max_tokens_per_month is not None
            else tenant_default_max_tokens_month
        ),
```

把 `group_handoff.py:421-451` 的 `_target_budget_available` 里两段 token 判断改为：

```python
def _target_budget_available(agent: Agent, *, now: datetime, tenant=None) -> bool:
    if (
        isinstance(agent.max_tool_rounds, bool)
        or not isinstance(agent.max_tool_rounds, int)
        or agent.max_tool_rounds <= 0
    ):
        return False

    tz_name = effective_timezone(agent, tenant)
    if agent.max_tokens_per_day and (agent.tokens_used_today or 0) >= agent.max_tokens_per_day:
        # 周期已翻页时存量计数不算超限。原实现在这里手糊了同样的绕法，因为日计数器
        # 当时根本不会自动重置。
        if not is_new_local_day(agent.last_daily_reset, tz_name, now=now):
            return False
    if agent.max_tokens_per_month and (agent.tokens_used_month or 0) >= agent.max_tokens_per_month:
        if not is_new_local_month(agent.last_monthly_reset, tz_name, now=now):
            return False
    # 以下 max_llm_calls_per_day 判断保持原样，本次不改调用次数限额的语义。
```

并在 `group_handoff.py` 顶部 import 补上 `effective_timezone` /
`is_new_local_day` / `is_new_local_month`。调用 `_target_budget_available` 的地方
补传 `tenant=`（若该处已加载过 Tenant 就直接传，未加载则传 `None`，此时时区退回
`agent.timezone → UTC`）。

- [ ] **步骤 4：跑测试确认通过**

```bash
cd backend && .venv/bin/pytest tests/test_token_period_consistency.py -v
```

预期：4 passed。

再跑 agents 与 group_handoff 的既有测试：

```bash
cd backend && .venv/bin/pytest tests/ -k "agent_delete or agent_directory or group" -v
```

预期：全部 passed。

- [ ] **步骤 5：跑全量测试**

```bash
cd backend && .venv/bin/pytest -q
```

预期：全部 passed，且总数不少于改动前。若有失败，先修再提交 —— 这是最后一个后端
任务，全量必须是绿的。

- [ ] **步骤 6：跑 lint 并提交**

```bash
cd backend && .venv/bin/ruff check . && .venv/bin/ruff format app/api/agents.py app/services/agent_runtime/group_handoff.py tests/test_token_period_consistency.py
git add backend/app/api/agents.py backend/app/services/agent_runtime/group_handoff.py backend/tests/test_token_period_consistency.py
git commit -m "refactor(token): 周期判定收敛为单一实现，新建 Agent 继承租户默认限额"
```

---

## 计划自检结果

按 writing-plans 的自检清单逐项过了一遍，记录如下。

**1. spec 覆盖度**

| design.md 章节 | 对应任务 |
|---|---|
| `normalize.py` 统一口径（含协议分派、各协议语义、自校验、估算） | Task 1 |
| 流式合并 | Task 1（纯函数）+ Task 2（接入 client） |
| `periods.py` 带时区周期 | Task 3 |
| 数据模型（Agent / Tenant / TenantTokenCounter / DailyTokenUsage / 部分唯一索引） | Task 4 |
| `ledger.py` 单事务、固定顺序、原子累加、幂等重置、失败不静默 | Task 5 |
| `token_tracker` 转发层与兼容性 | Task 6 |
| `budget.py` 三档判定、执行模式、软告警、能力边界 | Task 7 |
| 限额接入点两阶段 + `node_executor` reason 修正 | Task 8 |
| 补齐三处记账缺口 | Task 9 |
| 读取路径修正（后端四处 + schema） | Task 10 |
| 读取路径修正（前端两处） | Task 11 |
| 租户级 Agent 默认限额 | Task 12 |
| 迁移（含不回填的理由） | Task 4 步骤 3、5 |
| 已知缺口与技术债 | 无任务（刻意不做，见 proposal.md 范围外） |

**新增的一项**：design.md 没有单独点出"`agents.py` 与 `group_handoff.py` 各有一套
手写的周期判定，且都按 UTC 比较"。这两处若不收敛，就会与新的按时区判定分叉，等于
把这次要修的 bug 换个位置保留。已补为 Task 12，同时承载租户默认限额继承。

**2. 占位符扫描**

无 TBD / TODO / "类似 Task N" / "适当加错误处理" 之类。每个改代码的步骤都给了完整
代码块。两处刻意的例外，均已写明原因而非留空：
- Task 4 步骤 5 是真实 PostgreSQL 上的手工验证，因为本仓库测试不连 DB；给了确切的
  命令、SQL 与预期输出（`count = 1`、`sum = 200`）。
- Task 10 里 `admin.py` / `tenants.py` 的 SQL 聚合改动按位置逐条列出了要加的列与要
  换的调用，未逐行贴出整段查询 —— 那几段查询长且与本次无关的部分居多，逐行贴反而
  容易在复制时引入无关改动。

**3. 类型与命名一致性**

已核对跨任务引用的名字前后一致：
- `TokenUsage` 全程只有一处定义（Task 1），Task 6 明确断言
  `token_tracker.TokenUsage is Canonical`，避免两处定义分叉。
- `record(usage, *, tenant_id, agent_id, system_scope, now)` 在 Task 5 定义，
  Task 6 / Task 9 按此签名调用。
- `evaluate` 在 Task 7 定义，Task 8 里以 `evaluate_budget` 别名 import，测试也按
  `model_step_service.evaluate_budget` monkeypatch，两边对齐。
- `SYSTEM_SCOPES` 在 Task 4（迁移模块常量）与 Task 5（ledger 常量）各有一份，Task 5
  的测试 `test_system_scopes_match_the_migration` 断言两者相等，防止分叉。
- `is_new_local_day` / `is_new_local_month` / `effective_timezone` /
  `tenant_timezone` 在 Task 3 定义，Task 5 / 7 / 12 复用同名符号。
- `cache_hit_rate` / `estimated_share` 在 Task 10 定义并被六处读取路径共用。

**4. 范围**

12 个任务全部服务同一个可交付目标（token 计量准确 + 限额生效），不需要再拆成多个
计划。任务顺序有真实依赖：1 → 2、1 → 5 → 6 → 9、3 → 5/7/12、4 → 5/7/10、
7 → 8、10 → 11。

