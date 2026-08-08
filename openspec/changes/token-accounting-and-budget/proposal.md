# Token 计量与限额执行

## 为什么要做

当前展示的每个 Agent 的 token 用量和缓存命中率是错的，而且错得随用量放大；
日/月 token 限额在实际运行路径上根本不生效。

通读代码后确认了三类缺陷。

### 一、数字算错

`total = input + output` 被无差别套用（`backend/app/services/token_tracker.py:125`），
但各家 provider 对"`input` 是否已包含缓存 token"的约定并不一致。Anthropic 的
`input_tokens` **同时排除**两个缓存计数，于是每一个缓存 token 都被静默丢弃 ——
缓存命中率越高，少算得越多。

Anthropic 流式路径里，`message_delta` 的 usage 会整体覆盖 `message_start` 的
usage（`backend/app/services/llm/client.py:1985`）。只在 `message_delta` 里回
`output_tokens` 的网关很常见，这种情况下输入与全部缓存计数直接归零。

缓存命中率在六处被算成 `cache_read / total_tokens`，分母里含 output token ——
而 output 按定义不可能被缓存读取。所以显示出来的命中率系统性偏低。

### 二、有的调用根本不记账

群会话压缩（`backend/app/services/agent_runtime/session_context_compactor.py:308`）、
多 Agent 规划（`backend/app/services/agent_runtime/planning.py:479`）、模型连通性
测试（`backend/app/api/enterprise.py:249`）三处都在消耗 token 且零记录。

`DailyTokenUsage.agent_id` 用的是 `ondelete="CASCADE"`，删除一个 Agent 会连带
抹掉它的历史用量行，导致过去的租户总量凭空缩水。

### 三、限额从未生效

`backend/app/services/llm/caller.py` 里的 `call_llm` / `call_llm_with_failover` /
`call_agent_llm` 在生产代码中**没有任何调用者**，只有
`backend/tests/test_finish_protocol.py` 在跑它们。实际运行路径是 durable Graph
runtime（`backend/app/services/agent_runtime/model_step_service.py` →
`complete_llm_once`），它记账但**完全不做限额检查**。

现存的全部限额行为都在那条死路径上：`caller.py:233-236` 的日/月判定、每 3 轮
复查、80%/96% 轮次告警。唯一活着的检查是 `group_handoff.py:421` 的
`_target_budget_available`，那是群聊话轮准入门，不是执行中的拦截。

另外，日/月计数器只在两个 API 端点里惰性重置
（`backend/app/api/agents.py:226` 和 `:602`）。只要没人打开 Agent 列表或详情页，
计数器就永不翻页。一个纯 cron 驱动的 Agent 一旦触顶就会被永久卡死。
`_target_budget_available` 里已经有针对这个问题手糊的绕法（把过期的
`last_daily_reset` 当作"额度可用"），这本身就说明 bug 是被绕过而非被修掉。

### 四、写入不原子、失败不可见

`token_tracker.py:195-209` 对 `Agent` 行做的是 Python 侧读改写，而
`DailyTokenUsage` 用的是原子 upsert，两者并发语义不同，会长期漂移。整个函数被
一个 `try/except` 包住，失败只记 `warning` 并吞掉，所以丢失的记账不可见。

## 要改什么

### 范围内

- 按 provider **显式分派**（不再嗅探字典键）归一化 usage，统一为
  `billable_total = 未命中输入 + cache_read + cache_creation + output`
- Anthropic 流式 usage 改为逐字段合并，不再整体替换
- 增加口径自校验：provider 自报总量与算出的总量不一致时告警
- 估算值与 provider 真实值可分离，并暴露"估算占比"
- 修正全部缓存命中率的分母（后端四处、前端两处）
- 把三个当前零记录的调用点记入**租户级系统开销账本**
- 记账写入改为原子且三处目标同事务
- 从执行路径触发日/月重置，无竞态，按各 Agent 的有效时区划周期
- 在活的 runtime 路径上每轮执行限额：Agent 日、Agent 月、租户日，外加预算预检
- 新增租户日 token 上限，以及租户级的"新建 Agent 默认限额"

### 范围外

- **金额与成本。** 不做单价表、不做 per-model 计价、不做账单。计量只到 token。
- **租户月上限。** 本次只要求租户**日**上限。计数器表的形状留了余地，以后加月
  上限是加一个字段加一处判定的事，但现在不预先塞入用不上的死字段。
- **OpenClaw 边缘节点上报。** 边缘 Agent 在客户端消耗 token，而
  `backend/app/api/gateway.py` 里连 usage 字段都没有，所以它们的消耗永远显示为
  零。补这个需要改协议并在 OpenClaw 仓库改客户端。记为已知缺口，本次明确不做。
- **删除 `caller.py` 的死入口。** 超出"补全 token 计量"这个范围，且它们仍被一批
  测试引用着；另外 `node_executor.py:24-26` 从 `caller.py` 导入
  `WRITE_FILE_PROTOCOL_*` 常量（注意这条依赖指向 `caller.py`，与
  `token_tracker.py` 无关）。记为技术债。它们会继续通过兼容转发层记账，但**不会**
  拿到第二套限额实现。
- **回填历史数据。** 技术上无法安全回填，原因见 `design.md` 的迁移一节。

## 影响面

- 新增包 `backend/app/services/token_accounting/`；
  `backend/app/services/token_tracker.py` 降为薄转发层，现有 import 全部不破。
- 表结构：`Agent` 加 3 列、`Tenant` 加 3 列、新增 `TenantTokenCounter` 表、
  `DailyTokenUsage` 改动（`agent_id` 可空、`ondelete` 变更、加 3 列、替换唯一
  约束）。
- 一处行为变更：Anthropic 与 Gemini 的 `tokens_used_*` 数字会变大，因为此前被
  丢弃的缓存 token 和思考 token 现在被算进来了。现有限额阈值保持不动，并配一段
  "只告警不拦截"的宽限模式，所以上线不会立刻开始拦人。
