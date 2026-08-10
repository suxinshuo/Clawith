# Token 用量限额未生效 Bugfix Design

## Overview

限额判定的实现是通的，从未被打开。`budget.evaluate()` 在命中限额时返回
`allowed = (effective_mode == MODE_WARN_ONLY)`（`budget.py:224`，requirements 记作 225），
而执行模式的唯一来源
`system_settings.token_budget_enforcement_mode` 被迁移写成 `{"mode": "warn_only"}`，
`current_enforcement_mode()` 的三条兜底分支也一律退回 `warn_only`。于是命中限额 →
`allowed=True` → 只落一条 WARNING 日志 → 请求照常发出。

本次修复由五组相互独立、可分批落地的改动组成（在 "Fix Implementation" 里展开为编号 1–7 的七个变更：
下面第 1 组 = 变更 1 + 变更 2，第 2 组 = 变更 3，第 3 组 = 变更 4 的闸门与接入 + 变更 5，
第 4 组 = 变更 4 里的 `clearance` 参数，第 5 组 = 变更 6 + 变更 7；tasks 一律按变更编号引用）：

1. **把默认口径翻过来**：`current_enforcement_mode()` 的"配置层缺省"分支返回 `MODE_ENFORCE`；
   新增迁移把存量 `{"mode": "warn_only"}` 行改写为 `enforce` + 一个有期限的 `grace_until`
   观察窗口，避免按旧口径设定上限的租户升级即被大面积拦住。
2. **补齐产品入口**：新增平台级 `GET/PUT /api/enterprise/token-budget-enforcement`
   （读写执行模式、可见 grace 剩余时间、可一键提前结束 grace），并把这个 key 从通用
   `PUT /system-settings/{key}` 的可写范围里摘出来（今天任何 org_admin 都能改这个全平台开关）。
3. **把判定收敛成一个可复用的闸门**：新增 `token_accounting/gate.py`，承载"取判定主体 →
   调 `evaluate()` → 两级异常分类 → 带 lane 标签落日志"这套今天散在
   `model_step_service` 里的逻辑；`run_compactor` / `session_context_compactor` /
   `planning` / `model_probe` / `group_handoff` 全部改为调它。
4. **让"新增链路必须表态"成为类型约束**：`complete_llm_once()` 增加必填关键字参数
   `clearance: BudgetClearance`。新增一条链路时，不表态就调不通 provider 边界 —— 这正是
   1.8/1.9/1.11 三处缺口共同的成因（`caller.py` 那次也是同一个失败模式：判定挂在一条没有
   生产调用者的路径上，从未生效也从未被发现）。
5. **把无法配置的限额字段收干净**：租户三列补可写接口与前端入口；模型级
   `LLMModel.max_tokens_per_day` 从 API 写面移除（详见"Fix Implementation / 变更 6"，含取舍依据）。

记账侧（`ledger.py` / `normalize.py` / `periods.py`）本次一行不改 —— 上一次修复已把记账
口径修对，任何改动都会直接威胁 3.5。

## Glossary

- **Bug_Condition (C)**：触发缺陷的条件 —— 任一档 token 限额已被击穿，但请求仍被放行。
  成因有两种：走的是有判定的链路但执行模式为 `warn_only`；或走的是根本没有判定的链路。
- **Property (P)**：命中限额时期望的行为 —— 拒绝发起 provider 请求、终止原因为
  `token_budget_exceeded`、用户可见消息包含 blocked_scope / used / limit / reset_at、本轮零消耗。
- **Preservation**：未命中限额时必须逐字节不变的行为 —— 记账口径、周期翻页语义、
  软告警去重、fail-open 分级、判定优先级、系统开销归属。
- **lane（链路）**：一次模型调用的来源分类。今天存在七条：`business_step`（Run 的业务模型步）、
  `run_compact`（Run 级上下文压缩）、`session_compact`（直接会话上下文压缩，按 agent 记账）、
  `group_compact`、`planning`、`model_probe`、`group_handoff`（不发模型请求，但用限额判断目标可用性）。
- **effective_mode（生效模式）**：`configured_mode` 与 `grace_until` 共同决定的、判定实际采用的模式。
  grace 窗口内 `configured_mode=enforce` 的生效模式是 `warn_only`。
- **配置层缺省**：读取动作成功，但读到的值不可用（行缺失 / 缺 `mode` 键 / 值不在 `KNOWN_MODES`
  / value 列 JSON 脏）。安全默认值 = `enforce`。
- **基础设施故障**：读取动作本身失败（DB 不可达、连接被拒、超时、驱动异常）。此时模式未知，
  按 3.6 fail-open = `warn_only`（有新鲜度尚可的缓存时优先用缓存值）。
- **`current_enforcement_mode()`**：`budget.py` 中读执行模式的函数，本次修复的第一现场。
- **`budget.evaluate()`**：唯一的限额判定实现，返回 `BudgetVerdict`。本次不改判定语义，
  只补"`agent` 为 None 时跳过 agent 档"这一种输入形状。
- **`BudgetClearance`**：一次模型调用的限额表态凭证。只能由 `gate.check()`（已判定放行）或
  `BudgetClearance.not_applicable(reason=...)`（显式声明不适用，需写明理由）产生。

## Bug Details

### Bug Condition

缺陷在"任一档限额已被击穿、但请求仍被放行"时显现。放行的成因有两种，且互相独立：
(a) 走 `business_step` 链路时，判定被执行成 `warn_only`，`allowed` 恒为 True；
(b) 走另外四条消耗额度的链路时，根本没有判定这一步。

`group_handoff` 是第三种形态：它自己手写了一套硬拦，无视执行模式，因此同一个超限 Agent
在群聊里不可用、在直接对话里放行 —— 不是"被放行"，而是"两套口径互相矛盾"。

**Formal Specification:**

```
FUNCTION isBugCondition(input)
  INPUT: input of type ModelCallAttempt
    // input.agent          当前 Agent（tokens_used_today/_month + max_tokens_per_day/_month）
    // input.tenant         当前租户（max_tokens_per_day）
    // input.tenant_counter 租户当日计数器
    // input.lane           business_step | run_compact | session_compact
    //                      | group_compact | planning | model_probe
  OUTPUT: boolean

  breached ←
       (input.agent ≠ NULL
        AND input.agent.max_tokens_per_day ≠ NULL
        AND effective_used_day(input.agent) ≥ input.agent.max_tokens_per_day)
    OR (input.agent ≠ NULL
        AND input.agent.max_tokens_per_month ≠ NULL
        AND effective_used_month(input.agent) ≥ input.agent.max_tokens_per_month)
    OR (input.tenant.max_tokens_per_day ≠ NULL
        AND effective_used_day(input.tenant_counter) ≥ input.tenant.max_tokens_per_day)

  RETURN breached AND (
       (input.lane = business_step AND current_enforcement_mode() = warn_only)
    OR (input.lane ≠ business_step)
  )
END FUNCTION
```

`effective_used_*` 沿用 `budget._effective_used`：周期已按 Agent / 租户时区翻页时计数视为 0。

### Examples

- Agent 日上限 100,000、当日已用 200,000，用户在直接对话继续发消息 →
  期望：拒绝发起请求、Run 以 `token_budget_exceeded` 终止、用户看到
  「Agent 当日 token 用量已达上限（200,000/100,000，scope=agent_day）。额度将在 … 释放」。
  实际：请求照常发出并返回结果，仅日志留 `[TokenBudget] … mode=warn_only blocked=False`。
  （requirements 已实测：注入 `enforce` 时同一输入返回 `allowed=False blocked_scope=agent_day`，
  说明拦截链路本身是通的。）
- 同一 Agent 由 cron 触发器唤醒 → 期望与直接对话同一口径中断；实际 Run 执行到底。
  触发链路与对话链路共用同一个 durable worker（`worker_service.build_runtime_worker_components`
  只构造一个 `RuntimeModelStepService`），所以这一条与上一条同源、同修。
- Run 走到上下文压缩（`run_compactor.compact_if_needed` → `complete_llm_once(agent_id=…)`）→
  期望：发起压缩调用前判定，超限则不再消耗 Agent 额度；实际：按 `agent_id` 记账、零判定。
- 群聊压缩 / 规划 / 连通性测试（`system_scope` 三条）→ 期望：至少对租户日上限判定；
  实际：累加 `TenantTokenCounter.tokens_used_today`、零判定。
- 超限 Agent 在群聊被 @ → `group_handoff._target_budget_available`（`group_handoff.py:435`）
  用 `agent.max_tokens_per_day and …` 判为不可用；同一 Agent 在直接对话放行。
  附带缺陷：`and` 的真值判断把 `limit == 0` 当成"无上限"，与 `budget._breach` 显式区分
  `None` / `0` 的语义（3.2）相反。
- 边界（期望行为，必须保持）：`max_tokens_per_day IS NULL` → 无限制，不拦截；
  上一次记账落在按 Agent 时区计算的上一个自然日 → 陈旧计数视为 0，放行。

### 代码勘察对 requirements 的两处更正

> 状态：两条更正已于 2026-08-08 回写进 `bugfix.md`（1.11 正文改为「通过后端 API 写入」并附更正说明；
> 2.11 正文改为「从可写面移除」并记录已采纳的结论）。三份文档在这一点上口径一致。

- **1.11 的表述需要修正**：`LLMModel.max_tokens_per_day` 在前端**没有任何输入框**。
  `frontend/src/pages/enterprise-settings/tabs/LlmTab.tsx` 只在 TS 接口里声明了这个字段
  （第 18 行），模型新增/编辑表单里没有对应控件。它的可写面只有后端
  `LLMModelCreate` / `LLMModelUpdate` 两个 schema 与 `enterprise.py:441` / `enterprise.py:583-584`
  两处赋值，加上响应 schema 的回显。所以 2.11 的"从配置界面移除"落地成本比预想更低。
- **2.7 的"当前生效值对管理员可见"已部分存在**：`GET /api/agents/{id}/advanced` 已返回
  `tokens.budget_enforcement_mode`（`advanced.py:365`），但前端没有任何地方消费它
  （`grep budget_enforcement_mode frontend/src` 无结果）。缺的是写入面与一个能看见它的界面位置。

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- 记账口径（3.5）：四种 provider 协议的归一化、cache_read / cache_creation / reasoning /
  estimated 的计入方式、单事务内 `Agent` 计数器 → `TenantTokenCounter` → `daily_token_usage`
  的固定顺序原子累加。本次修复不修改 `ledger.py` / `normalize.py` / `periods.py`。
- NULL = 无限制、0 = 禁止一切用量（3.1 / 3.2）：`budget._breach` 对 `None` 与 `0` 的显式区分不动。
- 周期翻页语义（3.4）：`_effective_used` 把陈旧计数视为 0，纯 cron 驱动的 Agent 不被过期计数卡死。
- fail-open 与日志分级（3.6）：判定或其依赖加载抛异常时放行，`PROGRAMMING_ERROR_TYPES` 走
  ERROR + `token_budget_enforcement_disabled_bug`，其余走 WARNING +
  `token_budget_enforcement_disabled_transient`。
- 判定优先级（3.7）：`agent_day → agent_month → tenant_day`，第一个命中者写进 verdict。
- 软告警（3.8）：80% 阈值、每周期每 scope 每主体一次的 Redis 去重、Redis 不可用时跳过。
- 群聊 handoff 排除超限 Agent 的结果（3.9），以及 `max_tool_rounds` /
  `max_llm_calls_per_day` 这两项与 token 无关的可用性检查。
- Agent 行被删除时的记账竞态处理（3.10）、系统开销的 `system_scope` 归属与两个部分唯一索引（3.11）、
  统计接口的缓存命中率 / 估算占比 / 算不出来时返回「—」（3.12）。
- 未达上限时的正常路径（3.3）：不引入额外拦截、额外 DB 往返、可感知延迟。

**Scope:**

所有未击穿任何一档限额的输入必须完全不受本次修复影响，包括：

- 未设上限（NULL）的 Agent 与租户的全部请求；
- 用量低于上限的全部请求（含刚好 79,999/100,000 这种软告警边界）；
- 周期已翻页、存量计数陈旧的请求；
- 限额判定自身故障（DB 抖动、Redis 不可用）时的全部请求 —— 仍然放行。

### 一处有意的行为变更（3.9 的边界）

`group_handoff` 收敛为复用统一判定与统一执行模式（2.10）后，会出现两处与今天不同的结果，
都属于"口径统一"的必然代价，需要在评审时确认：

1. **`limit == 0`**：今天 `agent.max_tokens_per_day and …` 把 0 当无上限 → 放行；
   收敛后按 `_breach` 的语义 → 拦截。这个变化的方向与 3.2 一致，是修正而非回归。
2. **`configured_mode = warn_only`（管理员显式选择）或 grace 窗口内**：今天无视模式硬拦，
   收敛后跟随模式放行。理由：此时子 Run 自己的闸门也会放行，预先排除会造成"群聊里不可用、
   直接对话里可用"这同一个矛盾的镜像版本。默认口径（`enforce`、grace 结束后）下结果与今天一致，
   3.9 在默认配置下成立。

## Hypothesized Root Cause

根因已由代码阅读定位到具体行号，不是推测。下表按 requirements 的期望行为条目组织，
每条给出"根因位置 → 修复点"的映射。

| 期望行为 | 根因位置 | 修复点 |
|---|---|---|
| 2.1 / 2.2 / 2.3 / 2.4 | `budget.py:224` `allowed = (effective_mode == MODE_WARN_ONLY)` + `current_enforcement_mode()` 三条兜底全退 `warn_only` + 迁移 `202608061000:137` 写入 `warn_only` | 变更 1（默认值 + 迁移 + grace） |
| 2.5 | 同上；且存量安装的行已存在，仅改代码默认值对它们无效（`ON CONFLICT DO NOTHING`） | 变更 1 必须包含一条改写存量行的迁移，否则老环境永远修不好 |
| 2.6 | `current_enforcement_mode()` 把"配置缺省"与"读取失败"混成同一个返回值 | 变更 2（兜底语义分层 + 缓存 stale-if-error） |
| 2.7 | `SystemSettingDAO` 无写方法；通用 `PUT /system-settings/{key}` 只对 `key == "platform"` 要求平台管理员 | 变更 3（专用端点 + 前端入口 + 通用端点加护栏） |
| 2.8 | `run_compactor.compact_if_needed` → `self._completion(...)`（`run_compactor.py:592`）零判定 | 变更 4（`gate.py` + run_compact 接入） |
| 2.9 | `planning.py:481`、`session_context_compactor.py:388`、`enterprise.py:271/317`（probe 直连 `client.complete`，不经 `complete_llm_once`）零判定 | 变更 4（三条 system_scope 链路 + 顺带覆盖 `session_compact`） |
| 2.10 | `group_handoff.py:435-444` 手写判定，无视 `effective_mode`，且用真值判断合并了 `0` 与 `None` | 变更 5（收敛到 `gate.check()`） |
| 2.11 | `LLMModel.max_tokens_per_day` 只在 `enterprise.py:441` / `583-584` 写入并回显，无任何读取者 | 变更 6（从 API 写面移除，保留列） |
| 2.12 | `TenantQuotaUpdate` 不含这三列；前端 `quotaForm` 也没有 | 变更 7（PATCH 扩展 + 前端字段） |
| 2.13 | 已具备：`node_executor.py:770` `reason = error["code"]` → `checkpoint_side_effects._failure_metadata` → `delivery._safe_failure_content` | 只需保证新增链路也用同一个 code；见"终止原因的落地方式" |
| 2.14 | 看板已展示 used/limit，与"请求仍成功"矛盾的根源就是 2.1；无独立修复点 | 由变更 1 消除；变更 3 顺带把 effective_mode 显示到看板旁 |

### 兜底语义的重新定义（2.6）与 fail-open 的边界（3.6）

2.6（"读不到配置不得当作不限制"）与 3.6（"判定故障要 fail-open"）确实互相拉扯。划清边界的
判据只有一条：

> **读取动作是否成功。读到了值但值不可用 → 属于配置层缺省，走 `enforce`。
> 读取动作本身失败 → 模式未知，走 fail-open。**

| 情形 | 分类 | 生效模式 | 日志 |
|---|---|---|---|
| `system_settings` 无此行（`get_value` 返回 default `{}`） | 配置层缺省 | `enforce` | WARNING `token_budget_enforcement_mode_defaulted reason=row_absent` |
| 有行但缺 `mode` 键，或值不在 `KNOWN_MODES` | 配置层缺省 | `enforce` | WARNING `token_budget_enforcement_mode_defaulted reason=dirty_value` |
| value 列 JSON 损坏，反序列化抛 `ValueError` / `KeyError` | 配置层缺省 | `enforce` | WARNING `token_budget_enforcement_mode_defaulted reason=unparsable` |
| `evaluate(mode=...)` 收到未知的显式覆盖值 | 配置层缺省 | `enforce` | WARNING `token_budget_unknown_mode_override`（今天回退 `warn_only`，改为 `enforce`） |
| 读取抛 `TypeError`/`AttributeError`/`NameError` | 编程错误 | 缓存新鲜则用缓存，否则 `warn_only` | ERROR `token_budget_enforcement_disabled_bug` |
| 读取抛其他异常（`OSError`、DBAPI、超时） | 基础设施故障 | 缓存新鲜则用缓存，否则 `warn_only` | WARNING `token_budget_enforcement_disabled_transient` |
| 判定主体加载失败（租户 / 计数器 / Agent） | 基础设施故障 | 放行（`gate.check` 返回 allowed 的 verdict） | 按上面两类分级，保持今天的关键字 |

为什么"配置层缺省 → enforce"是安全的：enforce 的误判代价被限额自身的作用域限住了 ——
它只影响**已经超过管理员设定上限**的主体，而这正是管理员设上限时要求的结果；未设上限
（NULL）的主体在任何模式下都不受影响。反过来，`warn_only` 的误判代价是**无上界的超额消耗**。
两者不对称，所以"值不可用"时应当偏向 enforce。

为什么"读取失败 → fail-open"仍然保留：此时我们连"管理员是否选择了 warn_only"都不知道，
而 `system_settings` 的读取挂在每一次模型调用上，把存储抖动升级成全平台模型调用中断的代价
远高于一次窗口内的超额消耗。**缓存的 stale-if-error 把这个洞收得更小**：读取失败但进程内
有 10 分钟以内的已知值时，用已知值而不是盲目 fail-open。真正落到 `warn_only` 的只剩
"冷启动 + 存储不可达"这一种窄情形，且带 grep 关键字。

### 终止原因 `token_budget_exceeded` 的落地方式（2.13）与消息形状（2.1）

链路已经存在，本次只需让新增的闸门产出同一个 code：

```
_error("token_budget_exceeded", budget_exceeded_message(verdict))    # model_step_service._budget_gate
  → node_executor._model()：reason = error["code"]                    # node_executor.py:770
  → lifecycle{status: failed, reason: token_budget_exceeded, error: {...}}
  → checkpoint_side_effects._failure_metadata() → DeliveryRequest(failure_code, failure_message)
  → delivery._safe_failure_content()：
       任务执行未完成。
       错误：企业当日 token 用量已达上限（500,000/500,000，scope=tenant_day）。额度将在 2026-08-07T16:00 释放，或请管理员调高上限。
       错误码：token_budget_exceeded
       Run ID：<uuid>
```

`budget_exceeded_message(verdict)` 已经同时给出 scope 中文标签、`scope=` 机器可读值、
`used/limit`（千分位）与 `reset_at`（`isoformat(timespec="minutes")`），满足 2.1 对
blocked_scope / used / limit / reset_at 四项的要求，本次不改这个函数。

各链路的终止原因落点：

| lane | 超限时的返回形状 | 落到 Run 的 reason |
|---|---|---|
| `business_step` | `ModelStepResult(intent="error", error={"code": "token_budget_exceeded"})` | `token_budget_exceeded`（已具备） |
| `run_compact` | `RunCompactorError("token_budget_exceeded", …)`（`is_deterministic_compact_error = True`） | `token_budget_exceeded`（`node_executor._compact` 用 `exc.code` 作 reason） |
| `planning` | `PlanningModelResult(error_code="token_budget_exceeded", retryable=False)` | `planning_scheduler` 用 `error.code` 作 failure_code |
| `session_compact` / `group_compact` | `SessionContextCompactorError("token_budget_exceeded", …)` | 由 `ContextBuilder` 的既有错误传播决定；压缩失败时保留上一份 Session Context |
| `model_probe` | HTTP 200 + `{"success": false, "error_code": "token_budget_exceeded", "error": <message>}` | 不产生 Run；沿用 probe 端点既有的"返回结构化失败而不抛 500"的约定 |
| `group_handoff` | `GroupAgentHandoffError("group_handoff_budget_unavailable", repairable=True)` | 保持不变（3.9） |

`run_compact` 选择"终止 Run"而不是"跳过压缩继续"：压缩是因为上下文已到 80% 水位才触发的，
跳过压缩后紧接着的业务模型步一定更贵，也一定会被自己的闸门拦住；直接以 `token_budget_exceeded`
终止，排查者看到的原因才是真实原因。

## Correctness Properties

Property 1: Bug Condition - 击穿限额的输入必须被拦截且零消耗

_For any_ 输入 X 满足 `isBugCondition(X)`（任一档限额已击穿，且它走的是 `business_step`
配 `warn_only`、或任一条无判定的链路），修复后的实现 SHALL 在发起 provider 请求之前短路：
不发出 provider 请求、本轮 token 消耗为 0、失败原因为 `token_budget_exceeded`、
用户可见消息同时给出 `blocked_scope` / `used` / `limit` / `reset_at`。

```
FUNCTION expectedBehavior(result)
  INPUT: result of type ModelCallOutcome
  OUTPUT: boolean

  RETURN result.blocked = TRUE
     AND result.provider_request_sent = FALSE
     AND result.reason = "token_budget_exceeded"
     AND result.message CONTAINS result.blocked_scope
     AND result.message CONTAINS format(result.used)
     AND result.message CONTAINS format(result.limit)
     AND result.message CONTAINS format(result.reset_at)
     AND tokens_consumed_by(result) = 0
END FUNCTION
```

能力边界（沿用 `budget.py` 既有设计前提，不在本次修复中改变）：预检基于估算，provider 的真实
用量要等响应返回才知道，因此目标是"超限幅度有界（不超过一轮消耗量）"，不是"一个 token 都不超"。

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.8, 2.9, 2.13**

Property 2: Preservation - 未击穿限额的输入行为逐字节不变

_For any_ 输入 X 不满足 `isBugCondition(X)`（未设上限、用量未达上限、周期已翻页、
或判定自身故障），修复后的实现 SHALL 产生与修复前完全相同的结果：同样发出 provider 请求、
同样的记账写入（`Agent` 计数器 / `TenantTokenCounter` / `daily_token_usage` 三处的增量、
顺序、事务边界一致）、同样的软告警与去重行为、同样的 fail-open 与日志分级。

```
FUNCTION preservationHolds(X)
  INPUT: X of type ModelCallAttempt WHERE NOT isBugCondition(X)
  OUTPUT: boolean

  RETURN attemptModelCall(X)   = attemptModelCall'(X)
     AND ledgerStateAfter(X)   = ledgerStateAfter'(X)
     AND softWarningAfter(X)   = softWarningAfter'(X)
     AND logClassOf(X)         = logClassOf'(X)
END FUNCTION
```

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.10, 3.11, 3.12**

Property 3: Preservation - 群聊 handoff 在默认口径下的排除结果不变

_For any_ 群聊 handoff 目标 Agent，在 `effective_mode = enforce`（新默认值、grace 结束后）
下，收敛为统一判定后的可用性结论 SHALL 与今天 `_target_budget_available` 的结论一致，
包括 `max_tool_rounds` / `max_llm_calls_per_day` 两项与 token 无关的检查，
以及 `group_handoff_budget_unavailable` 这个 repairable 错误码；唯一允许的差异是
`limit == 0` 由"放行"变为"拦截"（向 3.2 对齐）。

**Validates: Requirements 2.10, 3.9**

## Fix Implementation

### 变更 1：默认执行模式翻为 enforce + 存量行迁移 + grace 观察窗口（2.5）

**File**: `backend/app/services/token_accounting/budget.py`

- `current_enforcement_mode()` 的"配置层缺省"分支返回 `MODE_ENFORCE`（详见变更 2 的完整分层）。
- `evaluate()` 中 `mode` 显式覆盖为未知值时的回退，从 `MODE_WARN_ONLY` 改为 `MODE_ENFORCE`，
  与配置层缺省保持同一条判据。
- 新增 `current_enforcement_state()` 返回 `EnforcementState(configured_mode, grace_until, effective_mode, source)`，
  供 API 与看板显示；`current_enforcement_mode()` 保留签名，内部返回 `effective_mode`，
  已有调用方（`advanced.py:365`、`model_step_service`）不受影响。
- grace 语义：value 形状扩展为
  `{"mode": "enforce", "grace_until": "2026-08-13T00:00:00+00:00", "set_by": "migration_token_budget_enforce_default"}`。
  `now < grace_until` 时 `effective_mode = warn_only`，并按
  `token_budget_enforcement_grace_active grace_until=…` 落 INFO 日志（每进程每 TTL 一次，
  不逐调用刷屏）。`grace_until` 缺失 / 已过期 / 不可解析 → 不进入 grace。

**File**: `backend/alembic/versions/2026xxxxxxxx_token_budget_enforce_default.py`（新增，`down_revision = "token_accounting_v2"`）

- `UPDATE system_settings SET value = jsonb_build_object('mode','enforce','grace_until', (now() + interval '7 days')::text, 'set_by','migration_token_budget_enforce_default') WHERE key='token_budget_enforcement_mode' AND value = '{"mode":"warn_only"}'::jsonb;`
- 只改写**与旧迁移写入的形状逐字节相同**的行。任何被管理员改过的行（通过
  `PUT /system-settings/{key}` 写入的 value 一定带别的键，或至少不是这个精确形状）保持不动。
  这个 provenance 判据的已知缺口：管理员若恰好用 `{"mode": "warn_only"}` 这个精确形状显式设置过，
  会被误改写 —— grace 窗口正是为这种情况留的补救时间，且变更 3 的入口让他能立刻改回来。
- 迁移不 `INSERT`：全新安装由代码默认值（`enforce`、无 grace）覆盖，这样"未显式配置即拦截"
  在新环境立即成立，老环境有 7 天缓冲。
- downgrade：把 `set_by = 'migration_token_budget_enforce_default'` 的行改回
  `{"mode": "warn_only"}`，不动其他行。

**灰度与通知**：迁移只负责数据，通知走既有的 `system_settings.notification_bar`
（`GET /enterprise/system-settings/notification_bar/public` 已存在且免鉴权，前端已消费）。
部署清单里加一步：升级后由平台管理员在通知栏挂一条"token 限额将于 X 日起真正拦截，
请按新口径（含缓存与思考 token）复核已有上限值"。选择这个形式的理由：不需要新建通知基础设施，
且这条横幅对所有管理员可见 —— 与"新增一张一次性弹窗表"相比，改动面小得多，而 grace 窗口
已经承担了"误拦保护"这一实质职责，通知只需承担"告知"。

### 变更 2：兜底语义分层 + 进程内模式缓存（2.6 / 3.3 / 3.6）

**File**: `backend/app/services/token_accounting/budget.py`

- 新增 `_CONFIG_DIRT_TYPES = (ValueError, KeyError)`，与既有 `PROGRAMMING_ERROR_TYPES` 并列，
  按"兜底语义"表分三类处理。注意这**改变了现有注释所记录的决策**：今天的注释明确把
  `ValueError`/`KeyError` 划给 transient 分支以避免误报成代码 bug。新分类保留"不吵到 ERROR"
  这一点（仍是 WARNING），只改生效模式（`warn_only` → `enforce`），因为脏配置属于
  "读到了值但不可用"。
- 新增进程内缓存：
  ```
  _MODE_TTL_SECONDS = 30.0            # 正常缓存有效期
  _MODE_STALE_TOLERANCE_SECONDS = 600.0  # 读取失败时允许使用的过期上限
  ```
  命中新鲜缓存直接返回；读取失败且缓存在 stale 容忍期内 → 用缓存值（stale-if-error）；
  否则按分类走 `enforce` / `warn_only`。
- 新增 `reset_enforcement_mode_cache()`，供测试与变更 3 的写入端点调用（写入后立即失效，
  使同进程内立刻生效；跨 worker 最长 30 秒生效，需在 UI 文案里写明）。

**对 3.3 的影响是净收益**：今天每个模型步调 `_budget_gate` 两次，每次 `current_enforcement_mode()`
各开一次会话 SELECT（`SystemSettingDAO.get_value` → `async with self.session()`），即每步 2 次
额外 SELECT。加缓存后稳态为 0 次。新增的四条链路各需要一次"取判定主体"（租户 + 计数器 2 条
SELECT），但这些链路本身都要发一次 LLM 请求（秒级），2 条 SELECT 不构成可感知延迟；
`business_step` 的往返次数不增加。

### 变更 3：执行模式的产品入口（2.7）+ 通用端点护栏

**File**: `backend/app/dao/system_setting_dao.py`

- 新增 `async def set_value(self, key: str, value: dict) -> SystemSetting`（upsert）。
  今天这个 DAO 只有 `get_by_key` / `get_value`，写只能绕道通用端点。

**File**: `backend/app/api/enterprise.py`

- `GET /api/enterprise/token-budget-enforcement` → `{configured_mode, effective_mode, grace_until, grace_active, set_by, propagation_seconds: 30}`。
  权限：`get_current_admin`（org_admin 可读，便于自查为什么被拦）。
- `PUT /api/enterprise/token-budget-enforcement` → body `{mode: "enforce"|"warn_only", clear_grace?: bool, grace_until?: ISO8601|null}`。
  权限：**必须是平台管理员**（`_is_platform_admin_user`）—— 这是一个全平台单值开关，
  一个租户的 org_admin 不该能替所有租户关掉限额。写入后调 `reset_enforcement_mode_cache()`。
- `PUT /system-settings/{key}` 增加护栏：`key == "token_budget_enforcement_mode"` 时也要求
  平台管理员，并在响应里提示改用专用端点。**这是一个既有的越权面**（今天任何 org_admin 都能
  改这个全平台 key），顺手收掉。

**File**: `frontend/src/pages/EnterpriseSettings.tsx`（quotas tab）

- 新增「Token 限额执行」区块：显示 `effective_mode`（拦截 / 仅告警）、grace 剩余时间、
  一个 mode 下拉、一个「立即启用拦截」按钮（等价于 `clear_grace: true`）。
  非平台管理员只读展示 + 说明文案。文案需写明"修改后最长 30 秒在全部 worker 生效"。

### 变更 4：统一闸门 `gate.py` + 四条链路接入（2.8 / 2.9）

**File**: `backend/app/services/token_accounting/gate.py`（新增）

```python
LANE_BUSINESS_STEP  = "business_step"
LANE_RUN_COMPACT    = "run_compact"
LANE_SESSION_COMPACT= "session_compact"
LANE_GROUP_COMPACT  = "group_compact"
LANE_PLANNING       = "planning"
LANE_MODEL_PROBE    = "model_probe"
LANE_GROUP_HANDOFF  = "group_handoff"

@dataclass(frozen=True, slots=True)
class BudgetSubjects:
    agent: Agent | None
    tenant: Tenant | None
    tenant_counter: TenantTokenCounter | None

@dataclass(frozen=True, slots=True)
class BudgetClearance:
    lane: str
    verdict: BudgetVerdict | None      # None 表示显式声明"不适用"
    not_applicable_reason: str | None = None

async def load_subjects(db, *, tenant_id, agent=None) -> BudgetSubjects
async def check(*, lane, subjects, estimated_next_round_tokens=0,
                run_id=None, now=None) -> BudgetVerdict
def clearance_from(lane, verdict) -> BudgetClearance
```

- `check()` 承载今天散落在 `model_step_service._budget_gate` 里的三件事：调 `evaluate()`、
  两级异常分类（`PROGRAMMING_ERROR_TYPES` → ERROR，其余 → WARNING，两者都 fail-open 返回
  `BudgetVerdict(allowed=True)`）、命中/软告警的日志（日志行新增 `lane=` 字段，其余字段与
  今天逐字段一致，便于既有告警规则继续匹配）。
- 软告警去重仍用 `should_emit_soft_warning(verdict.soft_warning_scope,
  verdict.soft_warning_subject_id, verdict.reset_at)`，键不变（3.8）。

**File**: `backend/app/services/token_accounting/budget.py`

- `evaluate()` 支持 `agent=None`：`checks` 元组按 `agent is None` 条件构造，只保留
  `tenant_day` 一档，`tz_agent` 不再计算。**必须做**：今天 `effective_timezone(None, tenant)`
  会走到 `get_agent_timezone_sync` 的 `agent.timezone` 而抛 `AttributeError`
  （`timezone_utils.py:73`），被 `PROGRAMMING_ERROR_TYPES` 捕获后 fail-open ——
  三条 system_scope 链路会"接了闸门但永远放行"，正是本 bug 的翻版。
  选择在 `budget.evaluate` 内加条件而不是改 `periods.effective_timezone`，是为了不碰
  `periods.py`（记账侧共用，3.5 的红线）。

**File**: `backend/app/services/llm/single_step.py`

- `complete_llm_once(..., *, clearance: BudgetClearance)` 增加必填关键字参数。
  函数内断言 `clearance.verdict is None or clearance.verdict.allowed`，否则抛
  `RuntimeError("budget_clearance_violation")` —— 走到这里说明调用方拿着"拒绝"的判定
  还继续发请求，是编程错误。
- 这是本次修复里**唯一一处为了防复发而扩大的改动面**：4 个生产调用点
  （`model_step_service:1525`、`run_compactor:592`、`planning:481`、
  `session_context_compactor:388`）+ 4 个 `Protocol` 定义 + 6 个测试文件里的替身
  （`tests/test_agent_runtime_planning.py:262-268` 直接断言了传给 completion 的 kwargs 字典，
  必须同步更新）。收益：新增一条链路时不表态就调不通，编译期/测试期即暴露，而不是像
  1.8/1.9 那样静默漏判几个月。
- **评审结论（2026-08-08）：保留这个结构性约束，不拆走**。为了让它可独立验证、必要时可单独回退，
  tasks 里把它单列为一个任务（`clearance` 参数的引入与全部调用点/替身的同步），
  与「各链路接入闸门」分开合入。

**接入点**（每处都在真正发出 provider 请求之前）：

| 文件 | 位置 | 主体来源 | 超限时 |
|---|---|---|---|
| `run_compactor.py` | `compact_if_needed` 判定 `_should_compact` 之后、进入 `_compact_batches` 之前 | 扩展 `RunCompactInputs`，由 `model_step_service.compact_inputs` 顺带带出（那里已经在同一个会话里查了 `agent`，只多两条 SELECT 取 tenant / counter） | `raise RunCompactorError("token_budget_exceeded", budget_exceeded_message(verdict))` |
| `session_context_compactor.py` | `_compact_with_model` 首个 batch 之前 | `CompactModelSelection` 增加 `subjects: BudgetSubjects` 字段，由 `_resolve_models` 在它已经打开的那个会话里一并 `load_subjects`（该方法已查过 `session` 与 `agent`，只多两条 SELECT）；`_compact_with_model` 不再需要自己开会话 | `raise SessionContextCompactorError("token_budget_exceeded", …)` |
| `planning.py` | `_load_model` 之后、`self._completion` 之前 | `_load_model` 已返回 `tenant_id`；用 `session_factory` 开一次会话 `load_subjects(agent=None)` | `return PlanningModelResult(error_code="token_budget_exceeded", retryable=False)` |
| `enterprise.py`（probe） | `create_llm_client` 之前 | `current_user.tenant_id`；`tenant_id is None`（平台管理员）时 `BudgetClearance.not_applicable("platform_admin_no_tenant")`，与既有"无法归属则只记日志"的处理保持一致 | 直接 `return {"success": False, "error_code": "token_budget_exceeded", "error": budget_exceeded_message(verdict), …}`，不发 provider 请求 |

`estimated_next_round_tokens` 在这四条链路统一传 0（只做击穿判定，不做预算预扣）。理由：
这些链路都不是主要消耗方，"超限幅度有界"由 `business_step` 自己的两阶段估算保证；
`run_compact` 后续如需更严可以把已经算好的 batch payload 估算量传进来，属于可选增强。

### 变更 5：`group_handoff` 收敛到统一判定（2.10 / 3.9）

**File**: `backend/app/services/agent_runtime/group_handoff.py`

- `_target_budget_available(agent, *, now, tenant=None)` 拆成两半：
  - token 部分删除，改由 `_validate_targets` 内对每个目标调
    `gate.check(lane=LANE_GROUP_HANDOFF, subjects=load_subjects(db, tenant_id=…, agent=mention.agent))`，
    用 `verdict.allowed` 判断；
  - 非 token 部分（`max_tool_rounds`、`max_llm_calls_per_day`）原样保留在一个更名后的
    `_target_run_budget_available()` 里，语义与今天逐条一致。
- 超限时仍抛 `GroupAgentHandoffError("group_handoff_budget_unavailable", repairable=True)`，
  错误码与 repairable 标记都不变（3.9）。
- 判定主体多了 `tenant` / `tenant_counter`：意味着租户日上限击穿时所有目标都不可用。
  这与直接对话口径一致（子 Run 一样会被拦），不再是"两套口径"。
- `_validate_targets` 里目标数通常是 1-3 个，每个目标一次 `evaluate()`；`tenant` /
  `tenant_counter` 在同一个 `db` 会话里查一次后复用，不按目标重复查。

### 变更 6：模型级 `max_tokens_per_day` —— 移除，不接入判定（2.11，已采纳）

**已采纳（评审确认 2026-08-08）：从 API 写面移除，保留数据库列，不接入限额判定。**

取舍依据：

- **它没有可用的判定语义**。`daily_token_usage` 与 `Agent`/`TenantTokenCounter` 三处计数器
  都不按 `llm_model_id` 分桶，`DailyTokenUsage` 也没有模型维度列。要让它生效，必须新增
  "按模型 × 日"的计数维度 —— 那是一次记账口径变更（新表或新列 + 新的原子累加），直接撞上 3.5
  （记账口径逐字节不变），远超本 bugfix 的范围。
- **它没有用户在使用**。前端 LlmTab 从未渲染这个输入框，只有 TS 接口里的类型声明；
  它只能通过直接调 API 写入。所以"移除"的实际影响面接近零，不存在"管理员配了但被拿掉"的问题。
- **保留列而不 DROP**：存量库里可能有历史值，DROP COLUMN 不可逆且会丢数据；本次只收窄
  API 契约，列上补注释说明"未被任何执行路径读取，保留仅为兼容历史数据"。是否 DROP 留给
  独立的清理迁移决定。

**具体改动**：`LLMModelCreate`（`schemas.py:408`）/ `LLMModelUpdate`（`schemas.py:420`）/
`LLMModelOut`（`schemas.py:433`）去掉该字段；`enterprise.py:441` 与 `enterprise.py:583-584`
两处赋值删除；`LlmTab.tsx:18` 的 TS 字段删除；`models/llm.py:59` 加注释。
注意 `LLMModelOut` 是响应模型，去掉字段会改变 API 响应形状 —— 已确认前端除了那一行类型声明
之外没有任何消费点，所以这是安全的收窄；若有外部 API 消费者，需要按兼容策略先保留回显再移除。

### 变更 7：租户三列的接口与前端入口（2.12）

**File**: `backend/app/api/enterprise.py`

- `TenantQuotaUpdate` 增加 `max_tokens_per_day` / `default_agent_max_tokens_per_day` /
  `default_agent_max_tokens_per_month`（均为 `int | None`）。
- **"不变更"与"显式设为无限制"必须能区分**：现有 PATCH 一律用 `if data.x is not None` 判断，
  这三列的 `None` 恰好是有效值（无限制）。改用 Pydantic v2 的 `data.model_fields_set`：
  key 出现在请求体里 → 写入（含 `null` → NULL）；未出现 → 不动。其余既有字段的处理方式不变，
  避免连带改动。
- `GET /tenant-quotas` 返回这三列。

**File**: `frontend/src/pages/EnterpriseSettings.tsx`

- `quotaForm` 增加三个字段，初值 `null`；三个数字输入框，空值 → `null`（无限制），
  与 Agent 设置页 `toPositiveIntOrNull` 的处理一致（非正数一律转 null）。
  因此 `0 = 禁止一切用量`（3.2）仍只能通过 API 设置，前端不提供 —— 与 Agent 级限额今天的
  口径完全一致，不引入新的不一致。
- `max_tokens_per_day` 这一项要标注"含系统开销（群聊压缩 / 规划 / 连通性测试）"，
  与 `models/tenant.py:38` 的注释一致。

### 需要在有库环境复验的结论

本地 PostgreSQL 未启动（Docker daemon 未运行，`docker ps` 连接失败），以下结论只做了
代码级确认，必须在有库环境复验：

1. **`system_settings.token_budget_enforcement_mode` 的实际值与形状**（requirements 1.14）。
   变更 1 的迁移 `WHERE value = '{"mode":"warn_only"}'::jsonb` 依赖它逐字节等于旧迁移写入的形状；
   若实际库里被改成了别的形状（例如带额外键），存量行不会被改写，需要人工处理。
   复验命令：`SELECT key, value FROM system_settings WHERE key = 'token_budget_enforcement_mode';`
2. **用户报告的"配置了上限"究竟配在哪一档**（requirements 1.14）：Agent 级
   （`agents.max_tokens_per_day/_month`）、租户级（`tenants.max_tokens_per_day`）还是
   模型级（`llm_models.max_tokens_per_day`）。若真实数据里只有模型级被填过，变更 6 的
   "移除"建议需要重新评估。
3. **迁移链的 head**：静态扫描显示 `token_accounting_v2` 是一个 head，但仓库里存在多个
   未合并的 head（`merge_v193_creds_focus`、`perf_indexes` 等）。新迁移的 `down_revision`
   需用 `alembic heads` / `alembic history` 在真实库上确认，必要时改为 merge 迁移。
4. **`jsonb` 比较与 `jsonb_build_object` 的行为**：`value = '{"mode":"warn_only"}'::jsonb`
   的键序无关性依赖 jsonb 语义（成立），但仍需在真实库上跑一次 upgrade / downgrade 往返。
5. **两条部分唯一索引在新增闸门后仍无影响**：本次不写 `daily_token_usage`，理论上无影响；
   仍应在有库环境跑一遍 `tests/` 里依赖真实 DB 的记账测试（如有）以确认 3.5 / 3.11。
6. **多 worker 下模式改动的生效时延**：30 秒 TTL 的实际表现（含 gunicorn/uvicorn 多进程）
   需要在部署环境实测，UI 文案里的数字要与实测一致。

## Testing Strategy

### Validation Approach

两阶段：先在**未修复**的代码上写出能复现缺陷的探索性测试（确认或推翻根因），再写
Fix Checking 与 Preservation Checking。现有 43 个 token 相关测试
（`tests/test_token_budget_enforcement.py` + `tests/test_token_accounting_budget.py`）
全部不依赖数据库、在 3.6 秒内通过，本次新增测试沿用同样的替身风格（`SimpleNamespace`
主体 + `monkeypatch` 注入 `evaluate_budget` / `get_value`），保证不引入 DB 依赖。

### Exploratory Bug Condition Checking

**Goal**: 在未修复代码上surface反例，确认"根因在执行模式与缺失的闸门，不在统计侧"。
若被推翻则需重新假设根因。

**Test Plan**: 用真实的 `budget.evaluate()`（不打桩）配一组超限主体，分别在
`mode` 缺失 / 缺失 `mode` 键 / 脏值三种配置下断言 `allowed`；再对四条无闸门链路
断言"provider 端口被调用了"。这些断言在未修复代码上应当失败。

**Test Cases**:

1. **配置缺省即放行**：`get_value` 返回 `{}` → `current_enforcement_mode()` 返回 `warn_only`
   → 超限主体 `allowed=True`（未修复代码上通过 = 反例成立；修复后应变为 `enforce` / `allowed=False`）。
2. **`run_compact` 无闸门**：构造超限 Agent，驱动 `RuntimeRunCompactorService.compact_if_needed`
   到达水位 → 断言 completion 端口**未**被调用（未修复代码上失败）。
3. **`planning` 无闸门**：构造租户日上限已击穿的 `TenantTokenCounter` → 驱动
   `PlanningModelService.complete_once` → 断言 completion 端口未被调用（未修复代码上失败）。
4. **`session_compact` / `group_compact` 无闸门**：同上，驱动 `LLMSessionContextCompactor.compact`。
5. **`model_probe` 无闸门**：租户日上限已击穿 → 调 `/enterprise/llm-test` → 断言未创建 LLM client。
6. **口径矛盾**：同一超限 Agent，`_target_budget_available` 判为不可用，
   而 `_budget_gate`（`warn_only`）判为可用 —— 断言这两个结论今天真的相反（反例成立）。
7. **`agent=None` 会炸**（边界）：直接 `await budget.evaluate(agent=None, tenant=…, tenant_counter=…)`
   → 断言抛 `AttributeError`。这条在未修复代码上**通过**，正是变更 4 里"必须支持 agent=None"
   的证据；修复后改为断言返回只含 `tenant_day` 档的 verdict。

**Expected Counterexamples**:

- 命中限额但 `verdict.allowed is True`、`verdict.mode == "warn_only"`；
- 四条链路上 completion 端口在超限主体下仍被调用，`ledger.record` 仍被写入；
- `_target_budget_available` 与 `_budget_gate` 对同一 Agent 给出相反结论；
- 可能的成因（逐条排除）：执行模式默认值 ✅ 已确认；缺失闸门 ✅ 已确认；
  统计与判定数据源不一致 ❌ 已在 requirements 排除；时区口径不一致 ❌ 已排除；
  陈旧 Agent 实例误判 ❌ 已排除（`_load` 每步重查库）。

### Fix Checking

**Goal**: 对所有击穿限额的输入，修复后的实现产出 Property 1 描述的行为。

**Pseudocode:**

```
FOR ALL input WHERE isBugCondition(input) DO
  result := attemptModelCall_fixed(input)
  ASSERT expectedBehavior(result)
END FOR
```

**落地为可执行测试**（`tests/test_token_budget_enforcement.py` 扩写 + 新增
`tests/test_token_budget_gate_lanes.py`）：

- `isBugCondition` 的输入域被拆成三个正交因子，用 `itertools.product` 穷举：
  - `scope ∈ {agent_day, agent_month, tenant_day}`
  - `lane ∈ {business_step, run_compact, session_compact, group_compact, planning, model_probe}`
    （`tenant_day` 之外的 scope 与三条 system_scope 链路组合时自动跳过 —— 那些链路没有 agent）
  - `breach_shape ∈ {used == limit, used > limit, used + estimated ≥ limit}`
- 每个组合断言四件事：completion 端口/HTTP client **未**被调用；返回的错误 code 为
  `token_budget_exceeded`；消息里同时出现 `blocked_scope` / 千分位 `used` / 千分位 `limit` /
  `reset_at.isoformat(timespec="minutes")`；`ledger.record` 未被调用（零消耗）。
- 终止原因链路各自补一条：`node_executor._model`（已有测试，保留）、
  `node_executor._compact`（新增：`RunCompactorError("token_budget_exceeded")` →
  `lifecycle.reason == "token_budget_exceeded"`）、`planning_scheduler`（新增）。
- 用户可见消息形状：新增一条驱动 `delivery._safe_failure_content` 的测试，断言渲染结果
  同时包含 `错误码：token_budget_exceeded` 与 `budget_exceeded_message` 的四项信息。

### Preservation Checking

**Goal**: 对所有未击穿限额的输入，修复后的实现与修复前逐字节一致。

**Pseudocode:**

```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT attemptModelCall_original(input) = attemptModelCall_fixed(input)
  ASSERT ledgerStateAfter_original(input) = ledgerStateAfter_fixed(input)
END FOR
```

**Testing Approach**: 这里推荐 property-based 风格，因为需要覆盖的是一整个输入域
（limit 是 NULL/0/正数 × used 在阈值上下 × 周期是否翻页 × 模式 × 故障注入），
手写用例容易漏掉边界。**本仓库没有 `hypothesis` 依赖**（`pyproject.toml` 的 `dev`
只有 pytest / pytest-asyncio / httpx / ruff），因此不引入新依赖，改用确定性的输入域穷举
（`itertools.product` + 固定的 `now`），行为等价于 PBT 的"覆盖整个域"，且无随机性带来的
不可复现。若后续愿意加依赖，这些测试可以平移到 `hypothesis` 的 `@given`。

**Test Plan**: 先在未修复代码上记录每个域点的行为（verdict 各字段、是否调 completion、
`ledger.record` 的入参、日志级别与关键字），把它冻结成期望表；修复后用同一张表断言。

**Test Cases**（每条都对应 requirements 里的一个保留行为）：

1. **3.1 NULL = 无限制**：`limit=None` × `used ∈ {0, 1, 10^9}` → 全部 `allowed=True`、
   `blocked_scope is None`、completion 被调用。
2. **3.2 0 ≠ NULL**：`limit=0` × `used=0` → `blocked_scope` 命中（0 意味着禁止一切）；
   与 `limit=None, used=0` 的结果必须不同。**同一域点在 `group_handoff` 上的结论也要断言**
   —— 这是变更 5 里唯一有意的行为变更，测试必须把它钉住而不是让它悄悄发生。
3. **3.3 未达上限零额外往返**：统计 `SystemSettingDAO.get_value` 与
   `_load_budget_subjects` 的调用次数。断言：一个模型步内 `get_value` 调用次数 ≤ 1
   （缓存生效，今天是 2），`_load_budget_subjects` 仍是 1（两阶段共用，不变）。
   这条测试同时保护"缓存没有把模式改动永久钉住"：`reset_enforcement_mode_cache()` 后必须重读。
4. **3.4 周期翻页**：`last_daily_reset` 落在上一个本地日 / `last_monthly_reset` 落在上个月，
   × Agent 时区 ∈ {UTC, Asia/Shanghai, America/New_York} → `allowed=True`（陈旧计数视为 0）。
5. **3.5 记账口径不变**：断言本次改动没有触碰 `ledger.py` / `normalize.py` / `periods.py`
   的行为 —— 具体做法是保留现有的 `tests/test_token_accounting_*.py` 与
   `tests/test_token_period_consistency.py` 全部不修改、全部通过。**任何一条需要改动
   才能通过的测试都视为 3.5 被破坏的信号**，必须回到设计而不是改测试。
6. **3.6 fail-open 分级**：注入 `TypeError` / `OSError` 到 `get_value` 与 `load_subjects`，
   × 缓存有值 / 无值 → 断言：无缓存时分别 ERROR + `token_budget_enforcement_disabled_bug`
   / WARNING + `token_budget_enforcement_disabled_transient` 且生效模式为 `warn_only`；
   有新鲜缓存时用缓存值。既有的四条相关测试（`test_token_accounting_budget.py:369/387`、
   `test_token_budget_enforcement.py` 两条）保持不变、继续通过。
7. **3.7 判定优先级**：三档同时击穿 → `blocked_scope == "agent_day"`；
   agent_month + tenant_day 同时击穿 → `agent_month`。
8. **3.8 软告警**：`used == floor(limit * 0.8)` → `soft_warning=True`；`used = 0.8*limit - 1`
   → False；去重键仍取 `verdict.soft_warning_scope/subject_id`（既有测试保留）。
9. **3.10 / 3.11 / 3.12**：不新增测试，靠既有测试保持通过（本次不改这些路径）。
10. **既有测试的必要更新清单**（默认值翻转导致，属于期望变化而非回归）：
    `test_enforcement_mode_defaults_to_warn_only_when_setting_absent`（→ `enforce`，改名）、
    `test_unknown_mode_value_falls_back_to_warn_only`（→ `enforce`，改名）；
    `test_enforcement_mode_falls_back_to_warn_only_when_lookup_raises` **不改**
    （读取失败仍 fail-open）。所有涉及缓存的测试需要 autouse fixture 调
    `reset_enforcement_mode_cache()`，否则用例之间会通过缓存互相污染。

### Unit Tests

- `current_enforcement_mode()` 的六条兜底分支（行缺失 / 缺 mode 键 / 未知值 / 脏 JSON /
  编程错误 / 基础设施故障）各自的返回值与日志关键字。
- `current_enforcement_state()` 的 grace 解析：`grace_until` 在未来 / 已过期 / 缺失 /
  不可解析四种形状下的 `effective_mode`。
- 缓存行为：TTL 内不重读、TTL 过期重读、读取失败时 stale-if-error、
  超出 stale 容忍期后 fail-open、`reset_enforcement_mode_cache()` 立即失效。
- `budget.evaluate(agent=None)`：只判 `tenant_day`，`reset_at` 用租户时区，不抛异常。
- `gate.check()`：allowed / blocked / 两类异常 fail-open / 日志含 `lane=`。
- `BudgetClearance`：`verdict.allowed is False` 传进 `complete_llm_once` 时抛
  `budget_clearance_violation`；`not_applicable` 放行。
- 新增/改动的 API：`PUT /token-budget-enforcement` 的平台管理员校验（org_admin → 403）、
  `PUT /system-settings/token_budget_enforcement_mode` 的新护栏、
  `PATCH /tenant-quotas` 的三态语义（key 缺失 = 不变、`null` = 无限制、正整数 = 上限）。

### Property-Based Tests

（以确定性输入域穷举实现，见 Preservation Checking 的说明）

- **Property 1 的域**：`scope × lane × breach_shape` 的全部合法组合 → 全部拦截、零消耗、
  code 与消息形状一致。
- **Property 2 的域**：`limit ∈ {None, 0, 1, 100_000} × used ∈ {0, limit-1, floor(0.8*limit),
  limit-1} × 周期新鲜/陈旧 × 时区 ∈ {UTC, Asia/Shanghai, America/New_York} × mode ∈
  {enforce, warn_only, grace} × 故障注入 ∈ {none, TypeError, OSError}` → 未击穿的每个点
  与修复前逐字段一致。
- **Property 3 的域**：`group_handoff` 目标 × `limit ∈ {None, 0, 正数}` × `used` 上下阈值 ×
  `max_tool_rounds / max_llm_calls_per_day` 是否耗尽 → 在 `enforce` 下与今天
  `_target_budget_available` 的结论逐点一致（`limit == 0` 一点除外，且该点被显式断言为新行为）。

### Integration Tests

- **直接对话全链路**：超限 Agent → `RuntimeModelStepService.complete_once` → `node_executor._model`
  → lifecycle `failed/token_budget_exceeded` → `delivery_from_checkpoint` →
  `_safe_failure_content` 渲染出的用户可见文本包含四项信息。用现有的 node_executor
  测试脚手架驱动，不连库。
- **触发器链路等价性**：同一 Agent 由 `source_type = trigger` 的 Run 驱动，断言 lifecycle
  的 reason 与直接对话完全一致（2.4）—— 二者共用同一个 `RuntimeModelStepService` 实例，
  测试要把这个共用关系钉住，防止未来分叉。
- **压缩 → 业务步的顺序**：Agent 未超限但压缩会把它推过上限时，断言压缩节点先被拦截
  （`token_budget_exceeded`），而不是压缩成功后业务步才失败。
- **群聊 handoff**：超限目标 → `preflight_group_agent_handoff` 抛
  `group_handoff_budget_unavailable(repairable=True)`；模型收到 repair 指令而不是 Run 失败（3.9）。
- **执行模式切换生效**：`PUT /token-budget-enforcement` 从 `warn_only` 切到 `enforce`
  后，同进程内下一次判定立即用新值（缓存被显式失效）。
- **grace 窗口**：`configured_mode=enforce` + `grace_until` 在未来 → 超限请求放行且落
  `token_budget_enforcement_grace_active` 日志；`clear_grace: true` 之后同一请求被拦截。
- **需有库环境**：迁移 upgrade/downgrade 往返、存量 `warn_only` 行被改写、
  被管理员改过形状的行不被动。这几条列入"需要在有库环境复验的结论"，不在无库 CI 中运行。
