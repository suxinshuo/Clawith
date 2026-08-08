# Token 计量与限额执行 — 设计

本文是已确认的技术设计。问题陈述与范围边界请先看 `proposal.md`。

## 已定的决策

| 决策项 | 选择 |
|---|---|
| 范围 | 准确性 + 限额，不做金额/成本 |
| 统一口径 | `billable_total = 未命中输入 + cache_read + cache_creation + output` |
| 历史数据 | 不回填，记录一个口径切换点 |
| 现有阈值 | 保持不动，用"只告警不拦截"的宽限模式覆盖过渡期 |
| 无归属消耗 | 记为租户级系统开销行 |
| 租户限额 | 租户**日**上限 + 租户级"新建 Agent 默认限额" |
| 周期边界 | 按每个 Agent 的有效时区，而非 UTC |
| 限额执行 | 每轮判定 + 预算预检，落在活的 runtime 路径上 |
| 代码组织 | 新增 `token_accounting/` 包，按职责分四个模块 |

## 架构

```
backend/app/services/token_accounting/
  __init__.py    对外唯一入口 —— 其他模块不直接 import 子模块
  normalize.py   provider usage dict -> TokenUsage        （纯函数，零 IO）
  periods.py     带时区的日/月边界                          （纯函数，零 IO）
  ledger.py      原子持久化 + 开销归属                       （有 DB）
  budget.py      惰性重置、限额判定、软告警                    （有 DB）

backend/app/services/token_tracker.py
  薄转发层 —— 现有 import 保持可用，无需改动
```

这样切分的理由：所有已确认的准确性 bug 都在纯计算里 —— provider 语义、流式
合并、命中率分母。把这一层隔离成零 IO，才可能用表驱动测试铺满。
`normalize.py` 与 `periods.py` 不得 import `app.database`。

## `normalize.py` —— 统一口径

### `TokenUsage` 的契约

`input_tokens` 重新定义为**全部输入 token，含缓存部分**。`cache_read_tokens`
与 `cache_creation_tokens` 是它的**细分**，不是它之外的追加。这样就恢复了不变式

```
total_tokens == input_tokens + output_tokens
```

于是"未命中输入 = input − cache_read − cache_creation"可由减法得出，不需要额外
列；同时正确的缓存命中率分母就是 `input_tokens` 本身。

`output_tokens` 包含 reasoning / thinking token。`reasoning_tokens` 单独携带，
**仅用于展示**，绝不加入任何总量 —— 重复计入它就等于重新引入正在修的这类
bug。

`estimated_tokens` 记录 `total_tokens` 中有多少来自字符估算而非 provider。

`TokenUsage` 保留当前可变 dataclass 的形状、`add()` 方法和存储型的
`total_tokens` 字段，这样转发层签名和 `caller.py` 的 8 个测试都不受影响。凡是
`normalize()` 产出的值必定满足上述不变式；遗留的"直接记一个 int"路径会设置
`total_tokens` 但细分未知，并按全部估算处理。

### 显式按协议分派

`extract_token_usage` 现在靠嗅探字典键来判断面对的是哪家 provider ——
`if "total_tokens" in usage` 就选走 OpenAI 分支（`token_tracker.py:62`）。而很多
Anthropic 兼容网关**也会**返回 `total_tokens`，于是 Anthropic 的 usage 被静默
路由进 OpenAI 分支、按错误的语义重新解释。这是不准确的一个隐蔽来源，且完全
静默。

分派键是**协议而不是 provider 名**。`PROVIDER_REGISTRY`（`client.py:2043`）里注册
了十几个 provider（`anthropic`、`openai`、`openai-response`、`azure`、`deepseek`、
`qwen`、`minimax`、`openrouter`、`zhipu`、`baidu`、`gemini` 等），但
`ProviderSpec.protocol` 只有四个取值：`openai_compatible`、`anthropic`、
`openai_responses`、`gemini`。按 provider 名分派会让 deepseek / qwen / zhipu /
azure / openrouter / minimax / baidu 这一大批 `openai_compatible` 的 provider 落进
未知分支并丢掉 usage —— 也就是把现在的问题换个形式保留下来。

`normalize(protocol, usage)` 直接接收协议字符串。provider → protocol 的解析用
既有的 `get_provider_spec()`（`client.py:2173`，已处理 `PROVIDER_ALIASES` 别名归
一），由**调用方**完成。刻意不在 `normalize.py` 里 import `client`：`client.py`
需要反过来调用 `merge_streaming_usage`，两边互引会形成循环导入。把协议当参数
传入同时也保住了这一层的纯粹性。

### 各协议语义

各家唯一有分歧的点是"`input` 是否已含缓存 token"。下表每一行都由表驱动测试
断言，让假设显式且可被推翻，而不是埋在分支里。

| 协议 | `input_tokens` | `cache_read` | `cache_creation` | `output_tokens` | `reasoning_tokens` |
|---|---|---|---|---|---|
| `openai_compatible` | `prompt_tokens`（已含缓存） | `prompt_tokens_details.cached_tokens` | 0（自动缓存，无写入计数） | `completion_tokens` | `completion_tokens_details.reasoning_tokens` |
| `openai_responses` | `input_tokens`（已含缓存） | `input_tokens_details.cached_tokens` | 0 | `output_tokens` | `output_tokens_details.reasoning_tokens` |
| `anthropic` | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` | `cache_read_input_tokens` | `cache_creation_input_tokens` | `output_tokens` | 不上报 |
| `gemini` | `promptTokenCount`（已含缓存） | `cachedContentTokenCount` | 0（显式建缓存是独立 API） | `candidatesTokenCount + thoughtsTokenCount` | `thoughtsTokenCount` |

对记录总量的净影响：`openai_compatible` 与 `openai_responses` 不变；`anthropic`
增加完整的缓存量；`gemini` 增加思考 token。

`openai_compatible` 覆盖了绝大多数国内外第三方 provider。它们的兼容程度不一，
常见情况是只回 `prompt_tokens` / `completion_tokens` 而没有任何缓存明细 —— 这在
本设计下是正确降级：缓存计数为 0，总量仍然准确。

### 流式合并

`client.py:1985` 让 Anthropic 的 `message_delta` usage 直接覆盖
`message_start` 的 usage。而 `message_start` 正是 `input_tokens` 和两个缓存计数
到达的地方，`message_delta` 是 `output_tokens` 到达的地方，且部分网关在那里
**只**发 `output_tokens`。结果就是主聊天路径上输入与缓存计数丢失。

修法：逐字段取最大值合并。Anthropic 文档说明 `message_delta` 的 usage 是累计
值，所以当 delta 携带完整值时取 max 正确，当它只携带子集时取 max 也正确。

合并本身是纯逻辑，因此放在 `normalize.py` 里作为
`merge_streaming_usage(existing, incoming) -> dict`，由 `AnthropicClient.stream`
调用它而不是直接赋值。放在纯模块里，才能不依赖 HTTP 流 fixture 就直接单测这个
回归。

### 口径自校验

当 provider 自报了总量时，与算出的 `billable_total` 比对一次。不一致则记
WARNING，带上 provider、model、两个数值和原始 usage dict。

这里刻意只告警、不做纠正。要点在于：当前这批 bug 能潜伏这么久，恰恰是因为
错误的算术是静默的。新接一个网关、或某家改了语义，应该表现为**告警**，而不是
悄悄变成错数字。

### 估算

字符估算（约 3 字符/token，`estimate_multimodal_tokens`）只在 provider 完全不
返回 usage 时启用，估算量记入 `estimated_tokens`，使"估算占比"可查询、可在 UI
上标注。估算值永远不会混进一个看起来像 provider 权威数据的字段里。

## `periods.py` —— 带时区的周期

复用既有的 `backend/app/services/timezone_utils.py`
（`get_agent_timezone_sync`、`now_in_timezone`），不重新实现时区解析，从而与
`heartbeat`、`agent_context` 的语义完全一致。有效时区优先级仍为
`agent.timezone → tenant.timezone → UTC`。租户级计数器只用 `tenant.timezone`。

四个纯函数：

- `local_day_start(tz_name, *, now) -> datetime` —— 本地零点对应的 UTC 时刻，
  作为 `DailyTokenUsage.date` 的锚点
- `local_month_start(tz_name, *, now) -> datetime`
- `is_new_local_day(last_reset_utc, tz_name, *, now) -> bool`
- `is_new_local_month(last_reset_utc, tz_name, *, now) -> bool`

已接受的代价：切换当天，一个 Agent 可能有两行 `DailyTokenUsage`（旧的 UTC 零点
锚点 + 新的本地零点锚点）。数据不丢、聚合仍然正确，但那一天的"单日用量"看起来
会被拆开。

## 数据模型

### `Agent` —— 新增三列

```
input_tokens_today   int  default 0
input_tokens_month   int  default 0
input_tokens_total   int  default 0
```

必须加，因为修正后的命中率分母是"输入总量"，而 `Agent` 行现在只带
`tokens_used_*`、`cache_read_tokens_*`、`cache_creation_tokens_*` —— 分母从这些
里算不出来。它们与现有计数器在同一个事务内写入，不产生额外查询。

`tokens_used_*` 保留原名，只是含义扩展为新的统一口径。

### `Tenant` —— 新增三列

```
max_tokens_per_day                  int | None   NULL = 无限（日上限）
default_agent_max_tokens_per_day    int | None   创建 Agent 时带入
default_agent_max_tokens_per_month  int | None
```

不做租户月上限，理由见 `proposal.md` 的范围一节。

### `TenantTokenCounter` —— 新表

```
tenant_id          UUID  主键，外键 -> tenants.id
tokens_used_today  int   default 0
tokens_used_total  int   default 0
last_daily_reset   timestamptz | None
```

刻意不塞进 `tenants` 行：那一行是被高频读取的配置行，每轮模型调用都去 UPDATE
它，会把配置读取和用量写入耦合到同一个热行上，并不断产生新的行版本。单独一个
窄行两个问题都避免了。不设 `*_month` 列，因为没有月上限需要它服务 —— 不留死
字段。

### `DailyTokenUsage` —— 改动

```
agent_id             改为可空，ondelete SET NULL   （原 NOT NULL / CASCADE）
agent_name_snapshot  String(200) | None            新增
system_scope         String(32)  | None            新增
reasoning_tokens     int default 0                 新增
```

`system_scope` 对普通 Agent 行为 `NULL`，对租户系统开销行取
`group_compact` / `planning` / `model_probe` 之一。`agent_name_snapshot` 让
Agent 被删除后归因仍然可读；配合 `SET NULL`，删除 Agent 不会再静默地让历史租户
总量缩水。

本表的 `input_tokens` 采用新含义（全部输入，含缓存），与 `TokenUsage` 一致。

### 一个必须绕开的唯一约束陷阱

当前约束是 `UNIQUE(agent_id, date)`。**PostgreSQL 在唯一约束里把 NULL 之间视为
互不相同**，所以一旦 `agent_id` 可空，`ON CONFLICT` 就永远不会命中系统开销行，
每一次调用都会插入一条新行。聚合数字会随调用次数虚增，而且原因极难定位。

改用两个部分唯一索引：

```sql
CREATE UNIQUE INDEX uq_daily_token_usage_agent_date
  ON daily_token_usage (agent_id, date)
  WHERE system_scope IS NULL;

CREATE UNIQUE INDEX uq_daily_token_usage_system_date
  ON daily_token_usage (tenant_id, system_scope, date)
  WHERE system_scope IS NOT NULL;
```

`on_conflict_do_update` 通过 `index_where` 精确指向对应索引。部分唯一索引在所有
受支持的 PostgreSQL 版本上都可用，所以这样做刻意避开了依赖 PostgreSQL 15 的
`NULLS NOT DISTINCT`，不给部署环境增加隐式的版本下限。

## `ledger.py` —— 持久化

唯一入口：

```python
async def record(
    usage: TokenUsage,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    agent_name: str | None = None,
    system_scope: str | None = None,
) -> None
```

保证：

1. **单一事务**覆盖全部写入目标，使 `TenantTokenCounter`、`Agent`、
   `DailyTokenUsage` 三者不可能不一致。现在 `Agent` 行和 `DailyTokenUsage` 用
   的是不同的并发语义，会永久漂移。
2. **固定写入顺序** `tenant_token_counters → agents → daily_token_usage`，让并发
   事务之间不可能互相死锁。
3. **原子累加**，用 `UPDATE ... SET col = col + :n`，替掉
   `token_tracker.py:195-209` 那个并发下会丢更新的 Python 侧读改写。
4. **无竞态的惰性重置**，在同一事务内、累加之前执行，形式是条件更新：

   ```sql
   UPDATE agents
      SET tokens_used_today = 0, input_tokens_today = 0,
          cache_read_tokens_today = 0, cache_creation_tokens_today = 0,
          last_daily_reset = :now
    WHERE id = :agent_id
      AND (last_daily_reset IS NULL OR last_daily_reset < :local_day_start_utc)
   ```

   构造上即幂等：两个并发轮次同时尝试重置，只会产生一次清零，且任何一方都不会
   丢弃对方已应用的累加。月度计数器用同样的形状对齐 `local_month_start`；
   `tenant_token_counters.last_daily_reset` 按租户自己的时区做完全相同的处理。
5. **失败不再静默。** 序列化失败和死锁给两次有界重试。最终失败按 **ERROR** 记
   录，带上完整的 usage 载荷与归因信息，使该条记录可从日志恢复、也可被告警抓
   到。现在的代码只记 `warning` 然后吞掉。

系统开销行传 `agent_id=None` 加一个 `system_scope`，只触及租户计数器和
`DailyTokenUsage`，绝不触及任何 Agent 的计数器。

## `budget.py` —— 限额

```python
@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    allowed: bool
    scope: str | None        # 'agent_day' | 'agent_month' | 'tenant_day'
    used: int | None
    limit: int | None
    reset_at: datetime | None
    soft_warning: bool = False
```

判定顺序由最便宜、最具体到最宽：`agent_day → agent_month → tenant_day`。第一个
命中者胜出并写入 verdict，因此错误信息能说清究竟是哪一档天花板起了作用。

`reset_at` 由 Agent 或租户各自的时区算出，所以提示能如实说明额度何时释放。

### 执行模式

`system_settings` 里加一个 key `token_budget_enforcement_mode`，取值
`warn_only` | `enforce`。迁移时设为 `warn_only`。

`warn_only` 模式下，超限会被记录并在 API 中暴露，但不拦截。这是过渡期的保护：
因为新口径会把此前被丢弃的缓存与思考 token 算进来，重度使用 Anthropic 的 Agent
可能一下跨过那些按少算数字设定的阈值，上线即硬拦会像一次大面积故障。

这个模式是显式的管理员开关，而不是定时器，所以行为永远不会在某个未来日期自行
改变。为了避免 `warn_only` 因无人过问而变成永久状态，租户设置接口会把该模式与
`token_accounting_calibration_switched_at` 一并返回，让管理员看到宽限模式已经跑
了多久、期间吸收了多少次超限。

### 软告警

在起作用的那档限额达到 80% 时告警，每个周期每个 scope 一次。用一个到周期结束
即过期的 Redis key 去重（Redis 7 本来就是技术栈依赖）。Redis 不可用时跳过告警
—— 它只是提示性的，绝不能影响正确性路径或阻塞一次运行。

## 限额执行的接入点

活路径是 `RuntimeModelStepService.complete_once`
（`model_step_service.py:1480`），它的 docstring 已经写着 *"Load pinned inputs,
enforce budget, and perform one business-model call"* —— 意图已声明，实现缺失。

不需要为 WebSocket / IM / trigger 各层新造异常穿透。runtime 已有结构化短路
通道：`_error(code, message)` 产出
`ModelStepResult(intent="error", error={...})`（`model_step_service.py:256`），
在 `node_executor.py:766` 被消费；而 `_prepare_messages` 本来就会提前返回一个
`ModelStepResult` 来中止本轮。

分两个阶段，因为预检需要 prompt 估算值：

**阶段一 —— `_load()` 之后、`_prepare_messages()` 之前。** 先做惰性重置，再检查
已消耗的计数器。开销小，不需要估算值。在 `enforce` 模式下超限即返回
`_error("token_budget_exceeded", ...)`。

**阶段二 —— `_prepare_messages()` 之后、发起 provider 请求之前。** 预检：
`_prepare_messages` 为了管理上下文窗口本来就会算一个 prompt token 估算值
（`_message_token_counter`、`model_capabilities.py` 里的 `RuntimeTokenBudget`）。
把它当作本轮成本的下限；若剩余额度低于它，就直接短路，而不是发出一个必然超支
的请求。因为估算值本来就要算，这一步的额外成本接近于零。

### `node_executor.py` 里必须配套的一处修正

`node_executor.py:766` 对每一个 `intent="error"` 都硬编码
`reason: "model_call_failed"`。超限的 run 会被记录和审计成"模型调用失败"，把排查
的人带向错误方向。`reason` 必须由 error code 推导，从而得到
`reason: "token_budget_exceeded"`。

### 收敛两处手写的周期判定

除了记账路径，还有两处各自手写了"计数器是否已过期"的判断，且都按 UTC `.date()`
比较：

- `agents.py:72-98` 的 `_lazy_reset_token_counters`
- `group_handoff.py:421-451` 的 `_target_budget_available`

后者里那段"`last_daily_reset` 过期就当额度可用"是绕"日计数器不会自动重置"这个 bug
时手糊的补丁。两处都必须改为复用 `periods` 的同一套助手，否则就会与按租户时区的新
判定分叉 —— 等于把这次要修的 bug 换个位置保留下来。

`agents.py` 的读取侧惰性重置**保留**：记账路径已经会重置，但若自午夜起没有任何
LLM 调用，接口仍会显示上一周期的存量数字。保留它，只是不许它自己写一套判定。

## 补齐记账缺口

`complete_llm_once`（`single_step.py:36`）增加两个可选参数 `tenant_id` 和
`system_scope`，改为经 `ledger.record` 写入。它本来就是每次调用后立即记账而非
攒批，所以不存在"未落库 usage"需要 flush —— 比 `caller.py` 的设计少一处漂移
来源。

三个调用点开始传归因信息：

| 调用点 | 归因 |
|---|---|
| `session_context_compactor.py:308`（群会话压缩） | 租户 + `system_scope='group_compact'` |
| `planning.py:479`（多 Agent 规划） | 租户 + `system_scope='planning'` |
| `enterprise.py:249,266`（连通性测试） | 租户 + `system_scope='model_probe'` |

这些消耗计入租户日上限。它们是真实的租户支出；而把它们归到"恰好触发了它"的那个
Agent 头上，会让某个 Agent 的额度被它没有选择的共享工作耗尽。

## 需要修正的读取路径

缓存命中率有六处算成 `cache_read / total_tokens` —— 分母含 output token，而
output 按定义不可能被缓存读取。改为 `cache_read / input_tokens`。

后端：
- `advanced.py:281-283` —— 三个比率，另外暴露 `input_tokens_*` 与估算占比
- `admin.py:371`（平台趋势）、`:407`（Top 公司）、`:425`（Top Agent）
- `tenants.py:518`（租户聚合）

前端：
- `AgentDetailPage.tsx:4838-4840`
- `Dashboard.tsx:423`
- `PlatformDashboard.tsx:527,549` 读的是后端算好的比率，后端修完即可，无需改动

新增响应字段：`input_tokens_{today,month,total}`、修正后的 `cache_hit_rate_*`、
`estimated_share_*`（定义为同周期的 `estimated_tokens / tokens_used`，即该数字
有多少是字符估算而非 provider 上报）、`calibration_switched_at`、
`budget_enforcement_mode`。UI 把切换点之前的数据标注为"旧口径、偏小"，让这个
不连续可读，而不是看起来像一次用量暴涨。

Agent 响应 schema（`schemas.py:258`）与前端 `Agent` 类型
（`frontend/src/types/index.ts:30-38`）都需要加上新字段，因为
`AgentDetailPage` 和 `Dashboard` 是从 agent 对象而不是 metrics 接口算比率的。

## 迁移

一个 Alembic revision：

1. 加 `Agent` 三列、`Tenant` 三列，以及 `tenant_token_counters` 表。
2. 改 `DailyTokenUsage`：`agent_id` 改可空、`ondelete` 由 CASCADE 改 SET NULL；
   新增 `agent_name_snapshot`、`system_scope`、`reasoning_tokens`。
3. 删除约束 `uq_daily_token_usage_agent_date`，创建两个部分唯一索引。旧约束下
   现有数据构造上无重复，因此两个索引都能干净建成。
4. 用现有租户初始化 `tenant_token_counters`，计数器置零。
5. 数据迁移：在 `system_settings` 写入
   `token_budget_enforcement_mode = 'warn_only'` 与
   `token_accounting_calibration_switched_at = now()`。

现有 Agent 的 `max_tokens_per_day` / `max_tokens_per_month` 不作修改。新的租户
默认值只对此后新建的 Agent 生效。

### 为什么不回填历史数据

因为做不到安全回填。`DailyTokenUsage` 没有 provider 或 model 列，而在旧代码下
OpenAI 的行的总量已经包含缓存 token、Anthropic 的行没有 —— 因此逐行判断"该不该
把缓存计数加上去"是不可能的。更进一步，Anthropic 流式覆盖 bug 让很多历史行的
缓存计数本身就已丢失，所以即便有 provider 列也无法重建。按 Agent **当前**绑定的
provider 去猜，对任何换过模型的 Agent 都是错的，而且会把无法验证的数字写进权威
表里。

因此切换点之前的行原样保留，并通过 `calibration_switched_at` 标记为旧口径。

## 能力边界

预检用的是**估算值**，而 provider 的真实用量要等响应返回才知道。所以"一个 token
都不超"做不到。设计目标是**超限幅度有界**：超出部分不会超过一轮的消耗量。这里
明确写下来，避免以后被当成 bug 提。

## 已知缺口与技术债

- **OpenClaw 边缘节点**不上报用量，其消耗显示为零。需要在 `gateway.py` 加协议
  字段，并在 OpenClaw 仓库改客户端。刻意排除在本次范围外。
- **`caller.py` 的死入口**（`call_llm`、`call_llm_with_failover`、
  `call_agent_llm`）在生产代码无调用者，只有测试在用。它们继续通过转发层记账，
  且刻意**不**获得第二套限额实现 —— 两套实现必然分叉。删除它们是另一件事。
- **无租户月上限。** 在现有底座上，需要时加一列加一处判定即可。

## 测试

### 测试基建的既有约束

`backend/tests/` 里**没有任何真实数据库测试** —— 没有 `conftest.py`，也没有一处
`create_async_engine`。仓库既有的做法是三种：

1. 用手写的假对象（如 `RecordingDB`、`DummyResult`，见
   `tests/test_agent_delete_api.py`）配合
   `monkeypatch.setattr(模块, "async_session", ...)` 替掉会话工厂。
2. 用声明式内省断言表结构 —— `Model.__table__.c.<列>`、`__table__.indexes`
   （见 `tests/test_oauth_credential_scope_storage.py`）。
3. 用 `importlib` 加载 Alembic 迁移模块，断言 `revision` / `down_revision` 与模
   块级常量。

本设计的测试一律沿用这三种，不引入真实 DB 依赖。这意味着"部分唯一索引"这类
schema 断言走第 2、3 种（断言 `Index` 对象及其 `postgresql_where`），而**索引在
真实 PostgreSQL 上的运行时行为**需要在部署环境手工验证一次；这一点在下面对应的
测试项里明确标注，不假装被自动化覆盖了。

纯计算层承担大部分权重，因为 bug 就在那里。

**`normalize.py` 表驱动：** 4 个 provider × {流式, 非流式} × {命中, 未命中,
完全无 usage}，断言 `billable_total`、缓存细分、`reasoning_tokens`、
`estimated_tokens`。

**针对现存 bug 的回归：** Anthropic 的 `message_start` 携带完整字段，随后
`message_delta` 只携带 `output_tokens` —— 断言输入与两个缓存计数都存活。这是当前
缺陷的精确复现。

**协议分派：** 一个 Anthropic 形状但同时带 `total_tokens` 的 usage dict
（网关行为）必须按 `anthropic` 协议语义解释，而不能被路由进 OpenAI 分支。

**provider 覆盖面：** 遍历 `PROVIDER_REGISTRY` 的每一个 provider，断言
`get_provider_spec()` 都能解析出一个 `normalize()` 认识的协议 —— 这条测试保证
以后新增 provider 时不会静默落进未知分支丢 usage。

**口径自校验：** provider 自报总量与算出的总量不一致时发出告警。

**部分唯一索引：** 断言 `DailyTokenUsage.__table__` 上存在两个带
`postgresql_where` 的唯一索引、旧的 `uq_daily_token_usage_agent_date` 唯一约束已
不存在，且 `ledger` 生成的 upsert 语句携带正确的 `index_where`。这三条合起来抓
的就是 NULL 不相等那个陷阱。索引在真实 PostgreSQL 上的运行时行为按上面"测试基建
的既有约束"所述，需在部署环境手工验证一次（对同一系统开销行连续 upsert 两次，
确认只有一行且数值累加）。

**周期：** `Asia/Shanghai` 租户在 UTC 16:00 时已属于本地的次日。

**重置幂等：** 两次并发重置尝试只清零一次，且不丢弃期间应用的累加。

**限额：** `agent_day`、`agent_month`、`tenant_day` 各自独立触发；断言
`intent="error"`、error code、命中的 scope 名，以及该 run 的 `reason` 是
`token_budget_exceeded` 而不是 `model_call_failed`。

**预检：** 剩余额度低于 prompt 估算值时，断言没有发出 provider 请求。

**warn_only 模式：** `warn_only` 下的超限不拦截但会被暴露；同一次超限在
`enforce` 下会被拦截。

**兼容性：** 现有的 `token_tracker` import 仍可解析，且
`test_finish_protocol.py` 中 8 个 `caller.py` 测试保持通过。
