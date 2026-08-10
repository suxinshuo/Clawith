# Bugfix Requirements Document

## Introduction

上一次「agent token 用量统计不准确」的修复（commit `f7e3280a feat(token): 重写 token 计量、缓存命中率与预算限额体系`）重写了整套记账与限额体系。记账口径本身修对了：`token_accounting/ledger.py` 在单事务内原子累加 `Agent.tokens_used_today/_month`、`TenantTokenCounter.tokens_used_today` 与 `daily_token_usage` 明细，限额判定 `token_accounting/budget.py` 读的正是同一批字段，两侧数据源一致，时间窗口也共用 `token_accounting/periods.py` 的同一套按租户/Agent 时区计算的日/月边界。

但用量达到配置的日上限 / 月上限之后，系统并不拦截。经代码阅读与实测确认，根因不在统计侧，而在「执行模式」这一层：

`budget.evaluate()` 在命中限额时返回 `allowed = (effective_mode == MODE_WARN_ONLY)`（`budget.py:225`）。执行模式来自 `system_settings` 的 `token_budget_enforcement_mode`，而迁移 `202608061000_token_accounting_v2.py` 只把它写成 `{"mode": "warn_only"}`，且 `current_enforcement_mode()` 的所有兜底分支（行缺失、值脏、读取抛异常）也一律退回 `warn_only`。于是命中限额时判定结果是「允许」，只在 `model_step_service._budget_gate` 里落一条 `[TokenBudget] ... blocked=False` 的 WARNING 日志，模型请求照常发出。

实测（`mode` 分别注入 `warn_only` / `enforce`，Agent 当日用量 200,000 / 上限 100,000）：

```
warn_only -> allowed=True  blocked_scope=agent_day
enforce   -> allowed=False blocked_scope=agent_day
```

即拦截机制本身是通的，只是从未被打开。而要打开它，产品面上没有任何入口：前端没有相关设置项，后端 `SystemSettingDAO` 连写方法都没有，只能靠通用的 `PUT /api/enterprise/system-settings/{key}` 或直连数据库改值。

除此之外还查出四个相关缺口：Run 级上下文压缩与三条 system_scope 链路消耗额度但完全不过限额判定；`group_handoff.py` 自带一套无视执行模式的硬拦逻辑，与直接对话链路口径矛盾；模型级「每日 token 上限」（`llm_models.max_tokens_per_day`，只能由 API 直接写入，前端无输入框）和租户级三个限额列，分别是「存了没人读」和「有列没入口」。

影响范围：所有配置了日/月 token 上限的 Agent 与租户。上限完全不产生约束力，超额消耗无上界，用量看板照常显示「已超上限」但请求继续放行。

排查过程中已排除的假设：统计修复后写入的表/字段与校验读取的数据源不一致（两侧同为 `Agent` 计数器 + `TenantTokenCounter`，一致）；时区 / UTC / 自然日月边界口径不一致（记账与判定共用 `periods.py`）；判定在 Run 内被陈旧的 Agent 实例误判（`model_step_service._load` 每个模型步都重新查库）；`caller.py::_get_agent_config` 里的旧限额逻辑造成干扰（`caller.py` 已是死代码，仅被测试引用，活路径是 durable runtime）。

## Bug Analysis

### Current Behavior (Defect)

当前用量达到上限后的实际表现。除标注「待验证」者外，以下各条均由代码阅读 + 实测确认。

1.1 WHEN Agent 当日用量已达到或超过 `Agent.max_tokens_per_day`、用户在直接对话中继续发送消息 THEN 系统仍然发起模型请求并正常返回结果，仅在后端日志留下一条 `[TokenBudget] ... blocked=False` 的 WARNING

1.2 WHEN Agent 当月用量已达到或超过 `Agent.max_tokens_per_month` THEN 系统同样继续发起模型请求，不拦截

1.3 WHEN 租户当日用量已达到或超过 `Tenant.max_tokens_per_day` THEN 系统同样继续发起模型请求，不拦截

1.4 WHEN Agent 已超限、由 cron / interval / webhook 等 Aware Engine 触发器自动唤醒 THEN Run 照常执行到底，额度持续被消耗

1.5 WHEN `system_settings` 中 `token_budget_enforcement_mode` 的值为迁移写入的默认值 `warn_only` THEN 命中限额的判定结果是「允许」，限额只产生日志，不产生任何用户可见的中断

1.6 WHEN `token_budget_enforcement_mode` 这一行缺失、值形状不符合约定、或读取过程本身抛异常 THEN 系统一律退回 `warn_only`，即「配置缺失 / 读取失败」被当作「不限制」

1.7 WHEN 管理员希望把限额从「只告警」切换为「真正拦截」 THEN 前端没有任何入口，后端也没有专用接口；`SystemSettingDAO` 只有 `get_by_key` / `get_value`，没有任何写方法，改值只能走通用的 `PUT /api/enterprise/system-settings/token_budget_enforcement_mode` 或直连数据库

1.8 WHEN 一次 Run 走到 Run 级上下文压缩（`run_compactor`） THEN 这次模型调用按 `agent_id` 记账、消耗 Agent 额度，但整条路径不经过任何限额判定

1.9 WHEN 一次 Run 触发群聊压缩、规划或连通性测试（`group_compact` / `planning` / `model_probe` 三条 system_scope 链路） THEN 这些调用会累加租户当日计数，但同样不经过任何限额判定

1.10 WHEN Agent 已超日/月限额、且在群聊中被 handoff 选中 THEN `group_handoff.py:435-444` 用一套自己手写的、无视执行模式的判断把它直接判为不可用；同一个 Agent 在直接对话里却完全放行，形成两套互相矛盾的口径

1.11 WHEN 通过后端 API 为某个模型写入「每日 token 上限」（`LLMModel.max_tokens_per_day`） THEN 该值只被写库与回显，没有任何执行路径读取它，配置完全不产生效果

> 1.11 表述更正（设计阶段代码勘察，2026-08-08）：初稿写作「管理员在『企业设置 → LLM』填写」，与代码不符。
> `frontend/src/pages/enterprise-settings/tabs/LlmTab.tsx` 只在 TS 接口里声明了这个字段（第 18 行），
> 模型新增 / 编辑表单里**没有任何输入框**。它的可写面只有后端 `LLMModelCreate` / `LLMModelUpdate` 两个 schema
> 与 `enterprise.py:441`、`enterprise.py:583-584` 两处赋值，加上 `LLMModelOut` 的回显。
> 所以这一条的准确形态是「一个只能由 API 直接写入、且永不生效的限额字段」，
> 而不是「界面上填了不生效」；2.11 的收口成本因此比初稿预估更低（见 design.md 变更 6）。

1.12 WHEN 管理员希望配置租户日上限或新建 Agent 的默认限额（`Tenant.max_tokens_per_day` / `default_agent_max_tokens_per_day` / `default_agent_max_tokens_per_month`） THEN 这三列只存在于数据库与 ORM 模型中，没有任何 API 或前端入口可以写入；其中前者是 1.3 判定所读的字段，因此该档限额实际上无法被配置

1.13 WHEN 用量已超上限、用户查看 Agent 概览 THEN 用量看板照常展示「已用 / 上限」并显示已超出，与「请求仍然成功」的实际行为互相矛盾，用户无法据此判断限额是否生效

1.14 （待验证）WHEN 在当前开发 / 生产库上查询 `system_settings` THEN `token_budget_enforcement_mode` 的实际值应为 `warn_only`；本地 PostgreSQL 未启动，该值、以及「用户配置的究竟是 Agent 级限额还是 1.11 的模型级限额」两点均未在真实数据上确认

上述行为可归结为同一个触发条件：

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type ModelCallAttempt
    // X.agent            当前 Agent（含 tokens_used_today/_month 与 max_tokens_per_day/_month）
    // X.tenant           当前租户（含 max_tokens_per_day）
    // X.tenant_counter   租户当日计数器
    // X.lane             调用链路：business_step | run_compact | group_compact | planning | model_probe
  OUTPUT: boolean

  // 任一档限额已被击穿
  breached ←
       (X.agent.max_tokens_per_day   ≠ NULL AND effective_used_day(X.agent)     ≥ X.agent.max_tokens_per_day)
    OR (X.agent.max_tokens_per_month ≠ NULL AND effective_used_month(X.agent)   ≥ X.agent.max_tokens_per_month)
    OR (X.tenant.max_tokens_per_day  ≠ NULL AND effective_used_day(X.tenant_counter) ≥ X.tenant.max_tokens_per_day)

  // 击穿后仍被放行的两种成因：
  //   a. 走的是有判定的链路，但执行模式是 warn_only（判定结果恒为「允许」）
  //   b. 走的是根本没有判定的链路
  RETURN breached AND (
       (X.lane = business_step AND current_enforcement_mode() = warn_only)
    OR (X.lane ≠ business_step)
  )
END FUNCTION
```

其中 `effective_used_*` 沿用 `budget._effective_used` 的语义：周期已按 Agent / 租户时区翻页时计数视为 0。

### Expected Behavior (Correct)

2.1 WHEN Agent 当日用量已达到或超过 `Agent.max_tokens_per_day`、用户在直接对话中继续发送消息 THEN 系统 SHALL 拒绝发起模型请求，并向用户返回一条说明「命中哪一档上限、已用 / 上限各是多少、额度何时释放」的可读错误

2.2 WHEN Agent 当月用量已达到或超过 `Agent.max_tokens_per_month` THEN 系统 SHALL 以同样方式拒绝发起模型请求

2.3 WHEN 租户当日用量已达到或超过 `Tenant.max_tokens_per_day` THEN 系统 SHALL 以同样方式拒绝发起模型请求

2.4 WHEN Agent 已超限、由 Aware Engine 触发器自动唤醒 THEN 系统 SHALL 以与直接对话相同的口径中断该 Run，不得因为链路不同而绕过限额

2.5 WHEN 平台未显式配置执行模式 THEN 系统 SHALL 默认真正拦截超限请求；「限额已配置」与「限额生效」之间不得再存在一个默认关闭、且不可见的开关

> 2.5 已定调（设计评审确认，2026-08-08）：采纳「默认改为 `enforce` **且** 补齐 2.7 的产品入口」，
> 并用一个有期限的 `grace_until` 观察窗口承担「避免升级即大面积误拦」这一实质职责
> （即前述方案 (a) + (b) + (c) 的合并形态）。原始顾虑的处理方式：
> `warn_only` 是上一次改动刻意选择的上线默认值，理由写在迁移注释里 ——「新口径把此前被丢弃的缓存与思考
> token 算进来，数字会变大。默认只告警不拦截，避免上线即大面积误拦；由管理员显式切到 enforce」。
> 本次不推翻这个灰度意图，而是把它从「一个默认关闭且不可见的开关」改成「一个有期限、可见、可提前结束的
> 观察窗口」：全新安装立即 `enforce`；存量安装由迁移改写为 `enforce` + 7 天 grace，
> 窗口内生效模式仍是 `warn_only`，并通过既有通知栏告知管理员按新口径复核上限值。
> 详见 design.md 变更 1 与变更 3。

2.6 WHEN `token_budget_enforcement_mode` 缺失、值为脏数据或读取失败 THEN 系统 SHALL 按显式定义的安全默认值行事，并把这一次降级按可检索的关键字记入日志；「读不到配置」不得被静默解释为「不限制」

2.7 WHEN 管理员需要查看或切换限额执行模式 THEN 系统 SHALL 提供一个明确的产品入口（前端设置项或专用接口），使该模式无需直连数据库即可读写，且当前生效值对管理员可见

2.8 WHEN 一次 Run 走到 Run 级上下文压缩 THEN 系统 SHALL 在发起该模型调用前执行限额判定；超限时不得继续消耗 Agent 额度

2.9 WHEN 一次 Run 触发群聊压缩、规划或连通性测试 THEN 系统 SHALL 在发起模型调用前对相应的限额档位（至少是租户日上限）执行判定；超限时不得继续消耗租户额度

2.10 WHEN 判断一个 Agent 是否超限 THEN 系统 SHALL 让 `group_handoff` 与直接对话链路复用同一套判定实现与同一份执行模式，不得各自手写一份口径

2.11 WHEN 模型级「每日 token 上限」字段（`LLMModel.max_tokens_per_day`）存在于 API 契约中 THEN 系统 SHALL 要么让它真正参与限额判定，要么把它从可写面移除；不得保留一个可写入但永不生效的限额字段

> 2.11 已定调（设计评审确认，2026-08-08）：采纳「从 API 写面移除、保留数据库列」。
> 理由是它没有可用的判定语义（三处计数器与 `daily_token_usage` 都不按 `llm_model_id` 分桶，
> 要让它生效必须新增「按模型 × 日」的计数维度，直接撞上 3.5 的记账口径红线），
> 且按 1.11 的更正它本来就没有前端入口、实际使用面接近零。保留列而不 DROP 是为了不丢历史数据。
> 详见 design.md 变更 6。

2.12 WHEN 管理员需要配置租户日上限或新建 Agent 的默认限额 THEN 系统 SHALL 提供可写入 `Tenant.max_tokens_per_day` / `default_agent_max_tokens_per_day` / `default_agent_max_tokens_per_month` 的接口与入口；作为 2.3 判定输入的字段不得处于无法配置的状态

2.13 WHEN Run 因超限被中断 THEN 系统 SHALL 以 `token_budget_exceeded` 作为终止原因（而非 `model_call_failed`），使排查者能从 Run 记录直接看出是限额而非模型故障

2.14 WHEN 用量看板显示某 Agent 已超上限 THEN 系统 SHALL 使该显示与实际拦截行为一致：显示已超上限即意味着后续请求会被拒绝

对上述期望行为的验证按 Fix Checking 组织：

```pascal
// Property: Fix Checking —— 命中限额的输入必须被拦截
FOR ALL X WHERE isBugCondition(X) DO
  result ← attemptModelCall'(X)
  ASSERT result.blocked = TRUE
  ASSERT result.provider_request_sent = FALSE
  ASSERT result.reason = "token_budget_exceeded"
  ASSERT result.message identifies (blocked_scope, used, limit, reset_at)
  ASSERT tokens_consumed_by(result) = 0
END FOR
```

其中 `attemptModelCall` 为修复前的行为、`attemptModelCall'` 为修复后的行为。

能力边界（沿用 `budget.py` 现有的设计前提，不在本次修复中改变）：预检基于估算，且 provider 的真实用量要等响应返回才知道，因此目标是「超限幅度有界（不超过一轮消耗量）」，不是「一个 token 都不超」。

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `max_tokens_per_day` / `max_tokens_per_month` 为 NULL THEN 系统 SHALL CONTINUE TO 视为无限制，不做任何拦截

3.2 WHEN 限额值被显式设为 0 THEN 系统 SHALL CONTINUE TO 按「禁止一切用量」处理，不与 NULL 合并为「无限制」

3.3 WHEN 用量未达上限 THEN 系统 SHALL CONTINUE TO 正常发起模型请求，不引入额外的拦截、额外的数据库往返或可感知的延迟

3.4 WHEN 上一次记账发生在按 Agent / 租户时区计算的上一个自然日或自然月 THEN 系统 SHALL CONTINUE TO 把陈旧计数视为 0 并放行，纯 cron 驱动的 Agent 不得被过期计数永久卡死

3.5 WHEN 记录一次 token 消耗 THEN 系统 SHALL CONTINUE TO 沿用现有记账口径：四种 provider 协议的归一化、cache_read / cache_creation / reasoning / estimated 的计入方式，以及在单事务内按固定顺序原子累加 `Agent` 计数器、`TenantTokenCounter` 与 `daily_token_usage`

3.6 WHEN 限额判定本身或其依赖加载（租户、租户计数器、执行模式）抛出异常 THEN 系统 SHALL CONTINUE TO fail-open 放行，并按异常类型分级记录日志（编程错误 ERROR、基础设施 / 瞬时故障 WARNING）；限额判定的故障不得级联成全平台模型调用失败

3.7 WHEN 多档限额同时被击穿 THEN 系统 SHALL CONTINUE TO 按 agent_day → agent_month → tenant_day 的顺序取第一个命中者写入判定结果，使错误信息指向最具体的那一档

3.8 WHEN 用量达到某档限额的 80% THEN 系统 SHALL CONTINUE TO 发出软告警，并沿用现有的「每周期每 scope 每主体只告警一次」去重

3.9 WHEN Agent 因超限而不参与群聊 handoff THEN 系统 SHALL CONTINUE TO 保持该 Agent 不被选中的结果（实现可以收敛为复用统一判定，但对外行为不变）

3.10 WHEN 记账时对应的 Agent 行已被删除 THEN 系统 SHALL CONTINUE TO 照常累加租户级计数、跳过明细行，并按 WARNING 记录该次删除竞态

3.11 WHEN 系统开销（群聊压缩 / 规划 / 连通性测试）落入 `daily_token_usage` THEN 系统 SHALL CONTINUE TO 按 `system_scope` 归属，并沿用现有的两个部分唯一索引

3.12 WHEN 用量看板与统计接口读取缓存命中率、估算占比、口径切换点 THEN 系统 SHALL CONTINUE TO 沿用现有算法，算不出来时返回 / 渲染「—」而不是伪造数字

对上述保留行为的验证按 Preservation Checking 组织：

```pascal
// Property: Preservation Checking —— 未命中限额的输入行为必须逐字节不变
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT attemptModelCall(X) = attemptModelCall'(X)
  ASSERT ledgerStateAfter(X) = ledgerStateAfter'(X)
END FOR
```
