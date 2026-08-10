# Implementation Plan

## 阅读顺序与约定

- 规格来源：`bugfix.md`（requirements 1.x / 2.x / 3.x）与 `design.md`（变更 1–7、Property 1–3）。
  任务里的「变更 N」一律指 design.md "Fix Implementation" 下的编号。
- 每个任务标注三项元信息：
  - **环境**：`无库可完成` / `需要数据库环境`（便于按当前环境条件排序；本地 PostgreSQL 未启动时先做前者）
  - **依赖**：必须先完成的任务编号
  - `_Requirements: …_`：该任务验证 bugfix.md 里的哪些条目
- 测试风格沿用现有 43 个 token 测试的替身风格（`SimpleNamespace` 主体 + `monkeypatch`），
  不引入 `hypothesis`；「property-based」以 `itertools.product` 的确定性输入域穷举实现
  （见 design.md "Preservation Checking" 的说明）。

## 批次与依赖（合入顺序）

| 批次 | 任务 | 环境 | 说明 |
|---|---|---|---|
| 0 | 1, 2 | 无库 | 探索性复现 + 保留行为基线，必须在任何修复之前 |
| A | 3 | 无库 | `budget.py` 判定层。**3.1（`agent=None`）是全部闸门接入的硬前提** |
| B | 4 | 无库 | `gate.py` + `business_step` 收敛 |
| C | 5 | 无库 | `clearance` 必填参数（可独立合入 / 独立回退） |
| D | 6 | 无库 | 四条链路接入闸门 |
| E | 7 | 无库 | `group_handoff` 收敛 |
| F | 8 | 无库 | 后端配置面：执行模式端点、模型级字段收窄、租户三列 |
| G | 9 | 无库 | 前端入口 |
| H | 10 | **需要数据库环境** | 迁移（含 `alembic heads` 前置确认） |
| I | 11 | **需要数据库环境** | design "需要在有库环境复验的结论" 6 条 |
| J | 12, 13, 14 | 无库 | 集成测试与 Property 复跑、Checkpoint |

**关键顺序约束（不可调换）**：任务 3.1（`budget.evaluate` 支持 `agent=None`）必须早于任务 6.2 / 6.3 / 6.4
（三条 system_scope 链路接入闸门）。今天 `effective_timezone(None, tenant)` 会在
`get_agent_timezone_sync` 里访问 `agent.timezone` 抛 `AttributeError`，被 `PROGRAMMING_ERROR_TYPES`
捕获后 fail-open —— 先接闸门再补 `agent=None` 会得到「接了闸门但永远放行」，正是本 bug 的翻版。

批次 A–G 全部可在无库环境完成并通过测试；批次 H / I 需要 Docker + PostgreSQL 起来之后再做。
批次 H 未完成前，**存量环境的限额仍不生效**（代码默认值只覆盖全新安装），因此 H 是本修复对老环境生效的必要条件，不能长期挂着。

---

- [x] 1. 编写 Bug Condition 探索性复现测试（在未修复代码上先跑出反例）
  - **Property 1: Bug Condition** - 击穿限额的输入必须被拦截且零消耗
  - **CRITICAL**: 这个测试在未修复代码上**必须失败**，失败本身就是确认根因的证据
  - **DO NOT** 在它失败时去改测试或改代码 —— 失败是预期结果
  - **NOTE**: 这个测试同时编码了期望行为，任务 13.1 复跑它来验证修复
  - **GOAL**: 跑出反例，确认「根因在执行模式与缺失的闸门，不在统计侧」；若被推翻则回到 design 重新假设根因
  - **Scoped PBT Approach**: 缺陷是确定性的，输入域用固定 `now` + `itertools.product` 穷举，不引入随机性
  - 新增 `backend/tests/test_token_budget_gate_lanes.py`，用真实 `budget.evaluate()`（不打桩判定）
  - 反例 1（配置缺省即放行）：`monkeypatch` 让 `system_setting_dao.get_value` 返回 `{}` →
    断言 `current_enforcement_mode()` 今天返回 `warn_only`、超限主体 `verdict.allowed is True`
    （对应 isBugCondition 里 `lane = business_step AND mode = warn_only` 这一支）
  - 反例 2（`run_compact` 无闸门）：构造超限 Agent，驱动 `RuntimeRunCompactorService.compact_if_needed`
    到达压缩水位 → 断言 completion 端口**未**被调用（今天会失败：端口被调用了）
  - 反例 3（`planning` 无闸门）：构造租户日上限已击穿的 `TenantTokenCounter` → 驱动
    `PlanningModelService.complete_once` → 断言 completion 端口未被调用（今天失败）
  - 反例 4（`session_compact` / `group_compact` 无闸门）：驱动 `LLMSessionContextCompactor.compact`（今天失败）
  - 反例 5（`model_probe` 无闸门）：租户日上限已击穿 → 调 `/enterprise/llm-test` → 断言未创建 LLM client（今天失败）
  - 反例 6（口径矛盾）：同一超限 Agent，断言 `group_handoff._target_budget_available` 判为不可用、
    而 `_budget_gate`（`warn_only`）判为可用 —— 两个结论今天真的相反
  - 反例 7（`agent=None` 会炸，边界）：直接 `await budget.evaluate(agent=None, tenant=…, tenant_counter=…)`
    → 断言抛 `AttributeError`。这条在未修复代码上**通过**，是任务 3.1 的必要性证据
  - 每个反例都断言四件事：completion 端口 / HTTP client 是否被调用、错误 code、消息形状、`ledger.record` 是否被调用
  - **EXPECTED OUTCOME**: 反例 1、6、7 通过（证明成因存在），反例 2–5 失败（证明闸门缺失）
  - 把跑出的反例逐条记录在本文件末尾的「反例记录」小节（含具体输入与实际返回）
  - 测试写完、跑完、反例记录完即可勾掉本任务，此时**不做任何修复**
  - **环境**：无库可完成
  - **依赖**：无
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 1.9, 1.10（修复后转为验证 2.1, 2.2, 2.3, 2.4, 2.5, 2.8, 2.9, 2.10, 2.13）_

- [x] 2. 编写保留行为基线测试（在实施修复之前）
  - **Property 2: Preservation** - 未击穿限额的输入行为逐字节不变
  - **IMPORTANT**: 遵循 observation-first —— 先在**未修复**代码上观察并记录行为，再把它冻结成期望表
  - 观察方法：对每个域点记录 verdict 各字段、是否调 completion、`ledger.record` 的入参、日志级别与关键字，
    落成一张显式的期望表（写在测试文件里，不靠"跑一遍看看"）
  - 输入域（`itertools.product` 穷举，固定 `now`）：
    `limit ∈ {None, 0, 1, 100_000}` × `used ∈ {0, limit-1, floor(0.8*limit), limit}` ×
    `周期 ∈ {新鲜, 陈旧}` × `时区 ∈ {UTC, Asia/Shanghai, America/New_York}` ×
    `mode ∈ {enforce, warn_only}` × `故障注入 ∈ {none, TypeError, OSError}`
  - 域点 1（3.1 NULL = 无限制）：`limit=None` × `used ∈ {0, 1, 10^9}` → `allowed=True`、
    `blocked_scope is None`、completion 被调用
  - 域点 2（3.2 0 ≠ NULL）：`limit=0, used=0` → `blocked_scope` 命中；与 `limit=None, used=0` 的结果必须不同。
    同一域点在 `group_handoff._target_budget_available` 上今天的结论（放行）也要记录 ——
    这是任务 7.1 唯一有意的行为变更，基线必须先把今天的样子钉住
  - 域点 3（3.3 零额外往返）：统计 `SystemSettingDAO.get_value` 与 `_load_budget_subjects` 的调用次数，
    记录今天的基线（一个模型步内 `get_value` 是 2 次、`_load_budget_subjects` 是 1 次）
  - 域点 4（3.4 周期翻页）：`last_daily_reset` 落在上一个本地日、`last_monthly_reset` 落在上个月
    × 三个时区 → `allowed=True`（陈旧计数视为 0）
  - 域点 5（3.6 fail-open 分级）：向 `get_value` 注入 `TypeError` / `OSError` →
    分别 ERROR + `token_budget_enforcement_disabled_bug` / WARNING + `token_budget_enforcement_disabled_transient`，
    两者生效模式均为 `warn_only`
  - 域点 6（3.7 判定优先级）：三档同时击穿 → `agent_day`；agent_month + tenant_day → `agent_month`
  - 域点 7（3.8 软告警）：`used == floor(limit * 0.8)` → `soft_warning=True`；`used = 0.8*limit - 1` → False；
    去重键取 `verdict.soft_warning_scope` / `soft_warning_subject_id`
  - 域点 8（3.5 记账口径）：本任务不新增记账测试，改为把 `tests/test_token_accounting_ledger.py` /
    `_normalize.py` / `_periods.py` / `test_token_period_consistency.py` 的当前通过状态记为基线；
    后续任何一条需要改动才能通过，都视为 3.5 被破坏的信号，必须回到 design 而不是改测试
  - **EXPECTED OUTCOME**: 全部通过（这就是要保留的基线行为）
  - 测试写完、跑完、在未修复代码上全绿即可勾掉本任务
  - **环境**：无库可完成
  - **依赖**：无
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.10, 3.11, 3.12_

- [ ] 3. 批次 A：`budget.py` 判定层（默认口径 + 兜底分层 + 缓存 + grace）

  - [x] 3.1 让 `budget.evaluate()` 支持 `agent=None`
    - `checks` 元组按 `agent is None` 条件构造：`agent is None` 时只保留 `tenant_day` 一档
    - `agent is None` 时不再计算 `tz_agent`（今天 `effective_timezone(None, tenant)` 会抛 `AttributeError`）
    - 条件加在 `budget.evaluate` 内，**不碰 `periods.py`**（记账侧共用，3.5 的红线）
    - `reset_at` 用租户时区（`tenant_timezone`）
    - 把任务 1 的反例 7 从「断言抛 `AttributeError`」改为「断言返回只含 `tenant_day` 档的 verdict、不抛异常」
    - **这是任务 6.2 / 6.3 / 6.4 的硬前提**：不先做这一步，三条 system_scope 链路会「接了闸门但永远 fail-open 放行」
    - _Bug_Condition: isBugCondition 中 `lane ≠ business_step` 且 agent 不参与判定的三条 system_scope 链路_
    - _Expected_Behavior: expectedBehavior(result) —— 拦截而非 fail-open 放行_
    - _Preservation: `agent` 非 None 时 `checks` 三档的内容、顺序、`reset_at` 计算逐字段不变（3.7）_
    - **环境**：无库可完成
    - **依赖**：1, 2
    - _Requirements: 2.9, 3.7_

  - [x] 3.2 兜底语义分层 + 默认执行模式翻为 `enforce`（变更 1 代码部分 + 变更 2）
    - `current_enforcement_mode()` 的「配置层缺省」分支返回 `MODE_ENFORCE`（行缺失 / 缺 `mode` 键 / 值不在 `KNOWN_MODES`）
    - 新增 `_CONFIG_DIRT_TYPES = (ValueError, KeyError)`，与既有 `PROGRAMMING_ERROR_TYPES` 并列：
      脏 JSON 反序列化失败 → WARNING `token_budget_enforcement_mode_defaulted reason=unparsable` + `enforce`
    - `evaluate()` 里 `mode` 显式覆盖为未知值时的回退，从 `MODE_WARN_ONLY` 改为 `MODE_ENFORCE`，
      日志关键字 `token_budget_unknown_mode_override`
    - 读取动作本身失败仍 fail-open：`PROGRAMMING_ERROR_TYPES` → ERROR `token_budget_enforcement_disabled_bug`；
      其余 → WARNING `token_budget_enforcement_disabled_transient`。两者均 `warn_only`（3.6 不变）
    - **同步更新 `budget.py` 顶部那段解释「ValueError/KeyError 故意不在 PROGRAMMING_ERROR_TYPES 里」的注释**：
      新分类保留「不吵到 ERROR」（仍是 WARNING），只把生效模式从 `warn_only` 改成 `enforce`，
      注释必须写清这条判据（读取成功但值不可用 = 配置层缺省 → enforce；读取失败 = 模式未知 → fail-open）
    - 判据落地按 design "兜底语义的重新定义" 那张表逐行实现，六条分支各自的返回值与日志关键字都要能 grep
    - _Bug_Condition: isBugCondition 中 `current_enforcement_mode() = warn_only` 这一支（1.5 / 1.6）_
    - _Expected_Behavior: expectedBehavior(result)，由 `allowed = (effective_mode == MODE_WARN_ONLY)` 在 enforce 下取 False 达成_
    - _Preservation: 3.6 的 fail-open 与两级日志分级、3.1 / 3.2 的 NULL/0 语义、3.7 的判定优先级不变_
    - **环境**：无库可完成
    - **依赖**：3.1
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.14, 3.1, 3.2, 3.6_

  - [x] 3.3 进程内模式缓存（TTL + stale-if-error + 显式失效）
    - 新增 `_MODE_TTL_SECONDS = 30.0`、`_MODE_STALE_TOLERANCE_SECONDS = 600.0`
    - 命中新鲜缓存直接返回；读取失败且缓存在 stale 容忍期内 → 用缓存值；否则按 3.2 的分类走 `enforce` / `warn_only`
    - 新增 `reset_enforcement_mode_cache()`，供测试与任务 8.2 的写入端点调用
    - 单元测试：TTL 内不重读、TTL 过期重读、读取失败时 stale-if-error、超出 stale 容忍期后 fail-open、
      `reset_enforcement_mode_cache()` 后立即重读
    - 对 3.3 是净收益：今天每个模型步 2 次额外 SELECT，加缓存后稳态 0 次；任务 2 的域点 3 期望值随之更新为 `≤ 1`
    - _Preservation: 3.3（不引入额外 DB 往返 / 可感知延迟）、3.6（缓存不得把 fail-open 的分级吞掉）_
    - **环境**：无库可完成
    - **依赖**：3.2（已完成）
    - _Requirements: 2.6, 3.3, 3.6_
    - **实现记录（2026-08-09）**：
      - `budget.py` 新增模块级缓存 `_mode_cache_value` / `_mode_cache_written_at`（`time.monotonic()` 时间戳，
        不用 `datetime.now()` 以免受系统时钟被拨动影响），不加锁——读多写少场景，竞态条件后果只是短暂重复查询。
      - `current_enforcement_mode()` 改造：入口先查缓存（`now - written_at <= _MODE_TTL_SECONDS` 命中则直接
        返回，不查 DB）；缓存未命中才调 `get_value`；`PROGRAMMING_ERROR_TYPES` / 其它异常两类失败分支改为调
        `_stale_cache_or_fail_open()`（缓存存在且 `age <= _MODE_STALE_TOLERANCE_SECONDS` 用缓存值且**不**刷新
        写入时间，避免"假新鲜"；否则 fail-open 到 `warn_only`，与 3.6 的两级日志分级完全不变）；四条成功路径
        （行缺失 / 脏值 / 未知 mode / 正常值)分别通过 `_remember_mode()` 写入缓存。`_CONFIG_DIRT_TYPES`
        分支同样写入缓存（配置层缺省的安全默认值 `enforce` 也值得缓存，避免每次都重新走一遍脏数据判定）。
      - 新增 `reset_enforcement_mode_cache()`，清空两个模块级变量，已加入 `budget.py` 的 `__all__`。
      - **未在本任务实现 grace 窗口逻辑**（任务 3.4 的范围），`evaluate()` 的六条分支判定逻辑未改动。
      - **提前处理了任务 3.5 里 autouse fixture 需求的一部分**：在
        `test_token_accounting_budget.py`、`test_token_budget_enforcement.py`、`test_token_budget_gate_lanes.py`、
        `test_token_budget_preservation_baseline.py` 四个文件里各加了一个
        `@pytest.fixture(autouse=True)` 调 `reset_enforcement_mode_cache()`（前后都清一次）。
        任务 3.5 执行时**不需要**再为这四个文件重复添加同样的 fixture；若任务 3.5 发现还有其它文件
        直接或间接调用 `current_enforcement_mode()` / `evaluate()`（不显式传 mode）且未被本次覆盖，
        仍需补齐。`test_token_budget_gate_lanes.py` 目前未在 3.5 的清单里点名，但因为反例 1 会真的调用
        `current_enforcement_mode()`，同样需要重置，已一并加上。
      - 单元测试新增于 `test_token_accounting_budget.py`：
        `test_cache_hit_within_ttl_does_not_re_query`、`test_cache_expires_after_ttl_and_re_queries`
        （用 `monkeypatch.setattr(budget.time, "monotonic", ...)` 操纵单调时钟）、
        `test_stale_cache_used_when_lookup_fails_within_tolerance`、
        `test_fail_open_when_stale_tolerance_exceeded`、
        `test_reset_enforcement_mode_cache_forces_immediate_re_read`。
      - `test_token_budget_preservation_baseline.py` 域点 3 的期望值按要求由 `== 2` 改为 `<= 1`，
        注释里写明理由（缓存生效后两阶段共用同一次缓存读取，若缓存在测试运行前已新鲜则可能是 0 次）。
        这是任务 13.2 允许的两处显式行为变化之一，已提前在本任务处理并注明。
      - **验证结果**：`test_token_accounting_budget.py` 34 passed；`test_token_budget_enforcement.py`
        全部通过（并入下面的全量结果）；`test_token_budget_gate_lanes.py`：反例 1/6/7 通过，反例 2-5
        仍失败（任务 6 范围，符合预期）；`test_token_budget_preservation_baseline.py` 14 passed。
        全量 `backend/tests/`：2376 passed，7 failed——失败集合与任务 2 记录的基线一致
        （`test_feishu_card_tools.py` 1 条、`test_html_to_pdf.py` 2 条，与本次改动无关的环境依赖缺失；
        `test_token_budget_gate_lanes.py` 的反例 2-5，任务 6 范围内的预期失败），无新增意外失败。

  - [x] 3.4 `current_enforcement_state()` 与 grace 窗口语义
    - 新增 `EnforcementState(configured_mode, grace_until, effective_mode, source)` 与 `current_enforcement_state()`
    - `current_enforcement_mode()` 保留签名、内部返回 `effective_mode`，已有调用方
      （`advanced.py:365`、`model_step_service`）不受影响
    - value 形状扩展为 `{"mode", "grace_until", "set_by"}`；`now < grace_until` 时 `effective_mode = warn_only`
    - grace 生效时落 INFO `token_budget_enforcement_grace_active grace_until=…`，每进程每 TTL 一次，不逐调用刷屏
    - `grace_until` 缺失 / 已过期 / 不可解析 → 不进入 grace（四种形状各一条单元测试）
    - _Preservation: 既有调用方签名与返回类型不变；`grace_until` 缺失时行为与 3.2 一致_
    - **环境**：无库可完成
    - **依赖**：3.2, 3.3（均已完成）
    - _Requirements: 2.5, 2.7_
    - **实现记录（2026-08-09）**：
      - `budget.py` 新增 `EnforcementState(configured_mode, grace_until, effective_mode, source)`
        （`frozen=True, slots=True`，与 `BudgetVerdict` 风格一致）与
        `current_enforcement_state(now=None) -> EnforcementState`，作为读取
        `token_budget_enforcement_mode` 的新核心实现；`current_enforcement_mode()`
        改造为薄封装（`return (await current_enforcement_state()).effective_mode`），
        签名与返回类型（`-> str`）不变，`advanced.py:365`、`model_step_service` 等
        既有调用方不需要任何改动。
      - **缓存结构调整**：3.3 引入的 `_mode_cache_value` / `_mode_cache_written_at`
        两个模块级变量扩展为三个——`_state_cache_configured_mode` /
        `_state_cache_grace_until` / `_state_cache_written_at`。只缓存
        `configured_mode` 与 `grace_until` 这两个"原始输入"，不缓存算好的
        `effective_mode`：grace 是否生效必须用**当前**挂钟时间现算，否则 grace
        窗口过期后 TTL 内的调用会继续误判为生效。`reset_enforcement_mode_cache()`
        同步改为清空三个变量；TTL（30s）、stale-if-error（600s 容忍期）、
        `reset_enforcement_mode_cache()` 立即失效这三条行为逐字段保留，
        3.3 的全部测试原样通过（详见下方验证结果），未改动任何一条 3.3 测试。
      - **source 字段的六个取值**（对应 EnforcementState 文档字符串列出的分支）：
        `row_absent` / `dirty_value` / `unparsable` / `row_present`（读取成功的
        四条分支）、`programming_error` / `transient_error`（读取失败、但缓存已
        超出 stale 容忍期，彻底 fail-open 时沿用异常分类作为 source）；另外两个
        辅助值 `cache`（命中 TTL 内新鲜缓存）、`stale_cache`（读取失败但缓存仍在
        600s 容忍期内）标记"这次没有触发新的配置读取"。这两个辅助值不在任务描述
        举例的六条分支范围内，是缓存机制本身需要的区分（否则无法从 source 看出
        这次判定是不是刚查的库）。
      - **grace 解析**：新增 `_parse_grace_until(raw)`——`None` / 非字符串 /
        空字符串 / `datetime.fromisoformat` 解析失败都返回 `None`（不区分"缺失"
        与"不可解析"，两者在 grace 判定这一层效果相同，都是"不进入 grace"）；
        字符串解析成功但没有时区信息时补 UTC。`_grace_active(grace_until, now)`
        就是 `grace_until is not None and now < grace_until` 这一条判据。
      - **INFO 日志节流的实现方式**：复用 3.3 缓存的写入时机作为节流依据，
        没有引入新的节流状态。新增 `_resolve_state(configured_mode, grace_until,
        source, now, *, log_grace: bool)`：`log_grace=True` 只在"这次调用触发了
        一次真正的配置读取（或 stale-if-error 兜底）"时传入（即 `_remember_state()`
        写入新缓存值、或 stale-if-error 命中的那几条分支）；命中 TTL 内新鲜缓存
        的路径固定传 `log_grace=False`。效果：只要缓存还新鲜（≤30s），后续调用
        不会重复记 INFO，天然做到"每进程每 TTL 至多一次"，不需要额外的
        "上次记录时间"变量。
      - `evaluate()` 与 `_breach`/`_effective_used` 等判定逻辑本任务未改动；
        `evaluate(mode=...)` 显式覆盖分支的行为不受影响。
      - 单元测试新增于 `test_token_accounting_budget.py`：
        `test_grace_missing_does_not_activate_grace`、
        `test_grace_expired_does_not_activate_grace`、
        `test_grace_unparsable_does_not_activate_grace`、
        `test_grace_active_forces_warn_only_and_logs_once_per_ttl`（同时验证
        INFO 日志节流：两次调用只记一条）、以及一条补充的
        `test_current_enforcement_mode_matches_effective_mode_during_grace`
        （验证薄封装在 grace 生效时的行为）。
      - **任务 3.5 要求的清单项已提前处理**：`test_token_accounting_budget.py:207`
        `test_enforcement_mode_reads_the_dict_shaped_setting` 已补一条断言——
        value 里没有 `grace_until` 时 `current_enforcement_state()` 返回
        `grace_until is None` 且 `effective_mode == configured_mode`
        （即不进入 grace）。任务 3.5 执行时不需要再改这条测试。
      - **验证结果**：`test_token_accounting_budget.py` 39 passed（新增 5 条）；
        `test_token_budget_enforcement.py` 16 passed（未改动，全部通过，签名
        兼容性得到验证）；`test_token_budget_gate_lanes.py`：反例 1/6/7 通过，
        反例 2-5 仍失败（任务 6 范围内的预期失败，未受影响）；
        `test_token_budget_preservation_baseline.py` 14 passed（未改动，全部
        通过）。全量 `backend/tests/`：**2381 passed, 7 failed**——失败集合与
        任务 3.3 记录的基线逐条一致（`test_feishu_card_tools.py` 1 条、
        `test_html_to_pdf.py` 2 条，环境依赖缺失，与本次改动无关；
        `test_token_budget_gate_lanes.py` 反例 2-5，任务 6 范围内的预期失败），
        无新增意外失败；passed 数从 2376 增至 2381，恰好对应本任务新增的
        5 条单元测试。

  - [x] 3.5 更新既有测试（默认值翻转导致的期望变化 + 缓存污染防护）
    - **这些是期望变化，不是回归**；改动范围严格限定在下列清单内，超出清单的失败一律按回归处理
    - `backend/tests/test_token_accounting_budget.py:196`
      `test_enforcement_mode_defaults_to_warn_only_when_setting_absent`
      → 期望改为 `MODE_ENFORCE`，改名为 `test_enforcement_mode_defaults_to_enforce_when_setting_absent`，
      并断言 WARNING 关键字 `token_budget_enforcement_mode_defaulted reason=row_absent`
    - `backend/tests/test_token_accounting_budget.py:218`
      `test_unknown_mode_value_falls_back_to_warn_only`
      → 期望改为 `MODE_ENFORCE`，改名为 `test_unknown_mode_value_falls_back_to_enforce`，
      断言关键字 `reason=dirty_value`
    - `backend/tests/test_token_accounting_budget.py:229`
      `test_enforcement_mode_falls_back_to_warn_only_when_lookup_raises` → **不改**（读取失败仍 fail-open）
    - `backend/tests/test_token_accounting_budget.py:369 / :387`
      （programming error 记 ERROR、transient 记 WARNING）→ **不改**，只受 autouse fixture 影响
    - `backend/tests/test_token_accounting_budget.py:207`
      `test_enforcement_mode_reads_the_dict_shaped_setting` → 保留，补一条断言：value 里没有 `grace_until` 时不进入 grace
    - `backend/tests/test_token_accounting_budget.py:167`
      `test_warn_only_mode_reports_the_breach_without_blocking` 与
      `backend/tests/test_token_budget_enforcement.py:124` `test_warn_only_breach_does_not_block`
      → **不改**（显式传 `warn_only`，管理员显式选择仍要放行）
    - **autouse fixture**：在 `test_token_accounting_budget.py`、`test_token_budget_enforcement.py`
      与新增的 `test_token_budget_gate_lanes.py` 各加一个 `@pytest.fixture(autouse=True)`
      调 `reset_enforcement_mode_cache()`，否则用例之间会通过 30 秒 TTL 缓存互相污染
      （表现为「单跑通过、全量跑随机失败」，是最难查的那类失败）
    - **全仓扫一遍隐式依赖默认值的用例**：`grep -rn "evaluate(" backend/tests | grep -v "mode="`，
      凡是没显式传 `mode=` 又没 patch `get_value` 的用例，都要么显式传 mode、要么按新默认值更新期望
    - _Preservation: 3.5（`test_token_accounting_ledger/_normalize/_periods`、`test_token_period_consistency` 一行不改、全部通过）_
    - **环境**：无库可完成
    - **依赖**：3.2, 3.3, 3.4
    - _Requirements: 2.5, 2.6, 3.5, 3.6_
    - **实现记录（2026-08-09）**：
      - **核对结果**：逐条核对任务 3.2/3.3/3.4 的实现记录并读取实际测试文件源码
        （`test_token_accounting_budget.py`、`test_token_budget_enforcement.py`、
        `test_token_budget_gate_lanes.py`、`test_token_budget_preservation_baseline.py`），
        清单里 8 项全部确认已在前置任务里处理完毕，无遗漏：
        1. `test_enforcement_mode_defaults_to_warn_only_when_setting_absent` →
           已改名为 `test_enforcement_mode_defaults_to_enforce_when_setting_absent`，
           断言 `MODE_ENFORCE` + `reason=row_absent`（任务 3.2，第 196 行附近）。
        2. `test_unknown_mode_value_falls_back_to_warn_only` → 已改名为
           `test_unknown_mode_value_falls_back_to_enforce`，断言 `reason=dirty_value`
           （任务 3.2）。
        3. `test_enforcement_mode_falls_back_to_warn_only_when_lookup_raises` →
           确认未改，仍断言 `MODE_WARN_ONLY`（读取失败仍 fail-open）。
        4. programming error ERROR / transient WARNING 两条测试
           （`test_enforcement_mode_lookup_programming_error_logs_at_error` /
           `test_enforcement_mode_lookup_transient_error_logs_at_warning`）→ 确认未改。
        5. `test_enforcement_mode_reads_the_dict_shaped_setting` → 确认已补充
           grace_until 缺失时 `state.grace_until is None` 且
           `state.effective_mode == MODE_ENFORCE` 的断言（任务 3.4）。
        6. `test_warn_only_mode_reports_the_breach_without_blocking`
           （`test_token_accounting_budget.py`）与 `test_warn_only_breach_does_not_block`
           （`test_token_budget_enforcement.py`）→ 确认均未改，仍显式传 `warn_only`。
        7. autouse fixture `_reset_enforcement_mode_cache_between_tests` →
           确认在 `test_token_accounting_budget.py`、`test_token_budget_enforcement.py`、
           `test_token_budget_gate_lanes.py`、`test_token_budget_preservation_baseline.py`
           四个文件里均已存在（任务 3.3 加入，任务 3.5 清单里点名的四个文件与实际
           一致，均已覆盖，无需重复添加）。
      - **全仓扫描（本任务唯一新增工作）**：执行
        `grep -rn "evaluate(" backend/tests | grep -v "mode="`，命中的匹配分三类：
        (a) 文档字符串/注释里提到 `evaluate()` 的自然语言引用，非代码调用，排除；
        (b) `fake_evaluate(**kwargs)` 桩函数定义本身（`def` 行不含 `mode=`），
        排除——这些桩函数内部或替换调用点已经处理 mode；
        (c) 真正跨行传参、`mode=` 出现在函数调用的后续行而被 grep 单行匹配漏掉的
        `evaluate(` / `budget.evaluate(` 调用，逐一用 `read_files` 展开确认：
        `test_token_budget_preservation_baseline.py` 的域点 2（第 217/224 行）、
        域点 7 两条（第 417/431 行）、`test_token_budget_gate_lanes.py` 的反例 1
        （第 175 行）与反例 7（第 516 行）—— **全部**在后续行里显式传了
        `mode=MODE_ENFORCE`，唯一的例外是反例 1 故意传 `mode=None`
        （测试意图就是不显式传 mode，让 `evaluate()` 自己走
        `current_enforcement_mode()`，用来验证配置缺省分支已修复为 `enforce`，
        这是设计使然，不是遗漏）。
        额外核实：全仓库只有这四个测试文件直接 `import` 了
        `app.services.token_accounting.budget`（`grep` 确认），因此不存在
        「第五个文件」隐式依赖 `current_enforcement_mode()` 默认值却未被扫描到
        的风险。`test_token_usage_read_paths.py` 里出现的 `current_enforcement_mode`
        引用是直接 `monkeypatch.setattr(advanced, "current_enforcement_mode", ...)`
        整体打桩替换函数本身（不是打桩 `get_value`），与默认值翻转无关，不在
        本任务范围内。
      - **结论：全仓扫描未发现需要修正的用例**。任务 3.2/3.3/3.4 已经把 3.5
        清单里的全部条目提前处理完毕，本任务未修改任何测试代码。
      - **验证结果**：
        - 四个 token 测试文件单独跑：`test_token_accounting_budget.py` +
          `test_token_budget_enforcement.py` + `test_token_budget_gate_lanes.py` +
          `test_token_budget_preservation_baseline.py` → `72 passed, 4 failed`
          （失败的 4 条是 `test_token_budget_gate_lanes.py` 的反例 2-5，任务 6
          范围内的预期失败，与任务 3.4 记录的状态一致）。
        - 全量 `backend/tests/`：**2381 passed, 7 failed**——与任务 3.4 记录的
          基线逐条一致（`test_feishu_card_tools.py` 1 条、`test_html_to_pdf.py`
          2 条，环境依赖缺失，与本次改动无关；`test_token_budget_gate_lanes.py`
          反例 2-5，任务 6 范围内的预期失败），**无新增意外失败**。
        - Preservation 红线单独验证：`test_token_accounting_ledger.py` +
          `test_token_accounting_normalize.py` + `test_token_accounting_periods.py` +
          `test_token_period_consistency.py` → `65 passed, 0 failed`；
          `git status --porcelain` 确认这四个文件**零改动**（working tree clean）。

- [ ] 4. 批次 B：统一闸门 `gate.py`

  - [x] 4.1 新增 `backend/app/services/token_accounting/gate.py`
    - 七个 lane 常量：`business_step` / `run_compact` / `session_compact` / `group_compact` /
      `planning` / `model_probe` / `group_handoff`
    - `BudgetSubjects(agent, tenant, tenant_counter)`、`BudgetClearance(lane, verdict, not_applicable_reason)`
      （均 `frozen=True, slots=True`）
    - `load_subjects(db, *, tenant_id, agent=None)`、
      `check(*, lane, subjects, estimated_next_round_tokens=0, run_id=None, now=None)`、`clearance_from(lane, verdict)`
    - `BudgetClearance.not_applicable(reason=...)` 作为唯一的「显式声明不适用」构造入口，必须带理由
    - `check()` 承载今天散在 `model_step_service._budget_gate` 的三件事：调 `evaluate()`、
      两级异常分类（均 fail-open 返回 `BudgetVerdict(allowed=True)`）、命中 / 软告警日志
    - 日志行只新增 `lane=` 字段，其余字段与今天**逐字段一致**，既有告警规则继续匹配
    - 软告警去重仍用 `should_emit_soft_warning(verdict.soft_warning_scope, verdict.soft_warning_subject_id, verdict.reset_at)`，键不变
    - 单元测试：allowed / blocked / 两类异常 fail-open / 日志含 `lane=`
    - _Bug_Condition: isBugCondition 里 `lane ≠ business_step` 的全部分支（1.8 / 1.9 / 1.10）_
    - _Expected_Behavior: expectedBehavior(result) —— 各链路复用同一判定与同一执行模式_
    - _Preservation: 3.6 fail-open 分级、3.7 判定优先级、3.8 软告警去重键_
    - **环境**：无库可完成
    - **依赖**：3.1, 3.2, 3.3
    - _Requirements: 2.8, 2.9, 2.10, 3.6, 3.7, 3.8_
    - **实现记录（2026-08-09）**：
      - 新增 `backend/app/services/token_accounting/gate.py`。逐一核对了
        `model_step_service._load_budget_subjects`（两条独立 SELECT：`Tenant` /
        `TenantTokenCounter`）、`_resolve_budget_subjects`（租户 ID 校验 + 两级异常
        分类 fail-open）、`_budget_gate`（调 `evaluate_budget()` → 两级异常分类 →
        命中限额 WARNING 日志 → 软告警 WARNING 日志 + 去重）三个方法的完整实现，
        确保 `gate.py` 精确复刻其行为语义。
      - 七个 lane 常量（字符串常量，值即 snake_case 名称本身）：
        `LANE_BUSINESS_STEP` / `LANE_RUN_COMPACT` / `LANE_SESSION_COMPACT` /
        `LANE_GROUP_COMPACT` / `LANE_PLANNING` / `LANE_MODEL_PROBE` /
        `LANE_GROUP_HANDOFF`。
      - `BudgetSubjects(agent, tenant, tenant_counter)` 与
        `BudgetClearance(lane, verdict, not_applicable_reason=None)`，均
        `@dataclass(frozen=True, slots=True)`。`BudgetClearance.not_applicable(lane, reason)`
        是唯一的"显式声明不适用"构造入口：`reason` 为空字符串时抛 `ValueError`
        （用最简单的 `if not reason` 校验，与任务描述"可以用简单校验"的要求一致）。
      - `load_subjects(db, *, tenant_id, agent=None)`：查询写法与
        `_load_budget_subjects` 逐字段一致（两条独立 `select(...)`，都用
        `scalar_one_or_none()`），把结果和传入的 `agent`（可为 `None`）一起打包成
        `BudgetSubjects`。不做异常处理——异常分类是 `check()` 的职责，`load_subjects`
        本身只负责查询（这与 `_load_budget_subjects` 本身不做异常处理、异常分类挂在
        调用方 `_resolve_budget_subjects` 里的分工方式一致）。
      - `check(*, lane, subjects, estimated_next_round_tokens=0, run_id=None, now=None)`：
        承载 `_budget_gate` 里的三件事——调 `evaluate(agent=subjects.agent, tenant=subjects.tenant,
        tenant_counter=subjects.tenant_counter, estimated_next_round_tokens=..., now=...)`；
        两级异常分类（`PROGRAMMING_ERROR_TYPES` → `logger.opt(exception=True).error(...)` +
        `BudgetVerdict(allowed=True)`；其余异常 → `logger.warning(...)` + 同样的放行
        verdict）；命中限额的 WARNING 日志（`verdict.blocked_scope is not None` 时）；
        软告警的 WARNING 日志 + `should_emit_soft_warning()` 去重（去重键完全复用
        `verdict.soft_warning_scope` / `soft_warning_subject_id` / `reset_at`，未改）。
        `agent_id` 字段用 `getattr(subjects.agent, "id", None)` 取，`subjects.agent`
        为 `None`（system_scope 链路）时该字段落 `None`，与 `evaluate(agent=None, ...)`
        只判 `tenant_day` 一档的语义一致。
      - 日志格式逐字段核对结果：
        - 编程错误：`"[TokenBudget] token_budget_enforcement_disabled_bug run_id={} agent_id={} error={!r} lane={}"`——
          `run_id` / `agent_id` / `error` 三个字段名、顺序与 `_budget_gate` 现有实现
          完全一致，只在末尾新增 `lane=`。
        - 瞬时故障：同上模式，`WARNING` 级别，关键字
          `token_budget_enforcement_disabled_transient`。
        - 命中限额：`"[TokenBudget] run_id={} agent_id={} scope={} used={} limit={} mode={} blocked={} lane={}"`——
          `run_id` / `agent_id` / `scope` / `used` / `limit` / `mode` / `blocked` 七个
          字段的名称与顺序与 `_budget_gate` 现有实现逐字段一致，只在末尾新增 `lane=`。
        - 软告警：`"[TokenBudget] soft warning run_id={} scope={} subject_id={} lane={}"`——
          `run_id` / `scope` / `subject_id` 与 `_budget_gate` 现有实现逐字段一致，
          只在末尾新增 `lane=`。
      - `clearance_from(lane, verdict)`：把一个 verdict 包装成
        `BudgetClearance(lane=lane, verdict=verdict)`，不带 `not_applicable_reason`。
      - **本任务未修改 `model_step_service.py`**（任务 4.2 的范围），`gate.py` 是全新
        独立模块；`__init__.py` 也未改动（`gate.py` 目前没有生产调用者，导出留给
        任务 4.2 及后续接入任务按需处理）。
      - 新增单元测试 `backend/tests/test_token_accounting_gate.py`（12 条）：
        `check()` allowed / blocked（含逐字段日志断言）/ `agent=None` 时 `agent_id`
        落 `None` / 两类异常 fail-open（各断言日志级别 + `lane=` + 关键字）/ 软告警
        触发时日志与去重调用参数 / 去重拒绝时不重复记日志；`BudgetClearance.not_applicable`
        的理由非空校验（成功 + 拒绝空字符串两条）；`clearance_from()` 的包装；
        `load_subjects()` 用最小的 `_FakeSession`（记录 `execute()` 调用、按序返回
        预置的 `scalar_one_or_none()` 结果）验证两条 SELECT 都被执行、`agent` 默认为
        `None`。用了一个 autouse fixture 把 `system_setting_dao.get_value` 打桩为固定
        返回 `{"mode": "enforce"}`——测试环境没有数据库连接，直接调用
        `current_enforcement_mode()`（`check()` 不显式传 `mode` 时 `evaluate()` 内部会
        调用它）会因连接失败 fail-open 到 `warn_only`，导致命中限额的 verdict 仍然
        `allowed=True`，污染断言；这个 fixture 只影响本文件，不影响其他测试文件的
        缓存状态（配合已有的 `reset_enforcement_mode_cache()` autouse fixture）。
      - **验证结果**：`test_token_accounting_gate.py` 12 passed。全量
        `backend/tests/`：**2393 passed, 7 failed**——失败集合与任务 3.5 记录的基线
        逐条一致（`test_feishu_card_tools.py` 1 条、`test_html_to_pdf.py` 2 条，环境
        依赖缺失，与本次改动无关；`test_token_budget_gate_lanes.py` 反例 2-5 共 4 条，
        任务 6 范围内的预期失败），**无新增意外失败**；passed 数从 2381 增至 2393，
        恰好对应本任务新增的 12 条单元测试。

  - [x] 4.2 `model_step_service` 的 `business_step` 收敛到 `gate.check()`
    - `_budget_gate` 改为薄封装：调 `gate.check(lane=LANE_BUSINESS_STEP, …)`，保留两阶段估算与
      `_load_budget_subjects` 的现有调用次数（1 次，两阶段共用）
    - 超限返回形状不变：`ModelStepResult(intent="error", error={"code": "token_budget_exceeded"})`
    - 断言日志字段除新增 `lane=business_step` 外与今天一致
    - _Preservation: 3.3（往返次数不增加）、3.6、3.7、3.8；`node_executor._model` 既有测试不改_
    - **环境**：无库可完成
    - **依赖**：4.1
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.13, 3.3_
    - **实现记录（2026-08-09）**：
      - `model_step_service.py` 的 import 改为从 `app.services.token_accounting.budget`
        只取 `PROGRAMMING_ERROR_TYPES` / `budget_exceeded_message`（`_resolve_budget_subjects`
        仍需要 `PROGRAMMING_ERROR_TYPES` 分类加载失败），新增
        `from app.services.token_accounting.gate import LANE_BUSINESS_STEP, BudgetSubjects,
        check as gate_check`。原来的 `evaluate as evaluate_budget` /
        `should_emit_soft_warning` 两个导入删除——两级异常分类与软告警日志已经
        收敛进 `gate.check()`，`_budget_gate` 不再需要直接引用它们。
      - `_budget_gate` 改为薄封装：`budget_subjects is None` 的短路逻辑原样保留
        （`_resolve_budget_subjects` 加载失败时的降级，属于 `_budget_gate` 自己的
        职责，不下沉到 `gate.check()`）；加载成功后只做两件事——把
        `(tenant, counter)` 元组包成 `BudgetSubjects(agent=agent, tenant=tenant,
        tenant_counter=counter)`，调用
        `gate_check(lane=LANE_BUSINESS_STEP, subjects=..., estimated_next_round_tokens=...,
        run_id=context.run_id)`；`verdict.allowed` 为 True 时返回 `None`，否则返回
        `_error("token_budget_exceeded", budget_exceeded_message(verdict))`，
        与改动前的返回形状逐字节一致。方法内不再出现 `try/except`、不再直接调
        `logger`——两级异常分类（编程错误 ERROR / 基础设施 WARNING，均 fail-open
        返回 `BudgetVerdict(allowed=True)`）与命中/软告警的 WARNING 日志全部由
        `gate.check()` 承担，已在任务 4.1 里逐字段核对过与原实现一致（只新增
        `lane=` 字段）。
      - **未改动** `_load_budget_subjects` 与 `_resolve_budget_subjects`：两者的
        职责（取 tenant/counter、按异常类型分类加载失败）与 `gate.check()` 的
        职责（判定 + 判定失败的异常分类）边界不重叠，任务描述里已明确排除，
        本次确认按此执行，未做任何改动。
      - **同步更新测试**（未改变任何对外可观察断言，只把 monkeypatch 目标从
        `model_step_service.evaluate_budget` / `model_step_service.should_emit_soft_warning`
        改为 `gate.evaluate` / `gate.should_emit_soft_warning`，因为这两个符号现在
        由 `gate` 模块直接引用，`model_step_service` 里已经不存在同名绑定）：
        - `test_token_budget_enforcement.py`：新增 `from app.services.token_accounting
          import gate` 导入；11 处 `monkeypatch.setattr(model_step_service,
          "evaluate_budget"/"should_emit_soft_warning", ...)` 全部改为
          `monkeypatch.setattr(gate, "evaluate"/"should_emit_soft_warning", ...)`；
          顶部 fixture 的注释里提到 `model_step_service.evaluate_budget` 的一处
          文档性描述同步改为 `gate.evaluate`。断言内容（返回形状、日志级别/关键字、
          `len(calls) == 1` 等）逐条未改。
        - `test_token_budget_gate_lanes.py`（任务 1 产出）：反例 6
          （`group_handoff` 与 `business_step` 口径矛盾）里的
          `monkeypatch.setattr(model_step_service, "evaluate_budget", fake_evaluate)`
          改为 `monkeypatch.setattr(gate, "evaluate", fake_evaluate)`，新增
          `gate` 导入；断言未改（仍是 `handoff_available is False` 且
          `business_step_result is None`，两侧口径矛盾的结论在收敛前保持不变，
          收敛是任务 7.1 的范围）。
        - `test_token_budget_preservation_baseline.py`（任务 2 产出）：域点 1/3
          共用的 `_gate_would_call_completion` 辅助函数里的同一处 monkeypatch
          同样改为 `gate.evaluate`，新增 `gate` 导入；断言未改。
        - 未在清单里、但检索确认没有其它测试文件直接 `monkeypatch.setattr`
          `model_step_service.evaluate_budget` 或 `model_step_service.should_emit_soft_warning`
          （全仓 grep 确认，改动前后均为这三个文件命中）。
      - **未修改** `_load_budget_subjects` / `_resolve_budget_subjects` 相关的测试
        （`test_resolve_budget_subjects_*` 系列），它们打桩的是
        `service._load_budget_subjects`，与本次改动的 `_budget_gate` 内部实现无关。
      - **验证结果**：
        - 目标测试组合跑：`test_agent_runtime_model_step_service.py` +
          `test_token_budget_enforcement.py` + `test_token_budget_gate_lanes.py` +
          `test_token_accounting_gate.py` + `test_token_budget_preservation_baseline.py`
          → `91 passed, 4 failed`——失败的 4 条是 `test_token_budget_gate_lanes.py`
          的反例 2-5（`run_compact` / `planning` / `group_compact` / `model_probe`
          四条链路仍无闸门，任务 6 范围内的预期失败，与改动前状态一致）。
        - `node_executor` 相关测试（`test_agent_runtime_node_executor.py`，文件本身
          零改动，`git status` 确认）：**29 passed**，确认 `_budget_gate` 收敛后
          `ModelStepResult` 的形状对上游完全透明。
        - 全量 `backend/tests/`：**2393 passed, 7 failed**——失败集合与任务 4.1
          记录的基线逐条一致（`test_feishu_card_tools.py` 1 条、
          `test_html_to_pdf.py` 2 条，环境依赖缺失，与本次改动无关；
          `test_token_budget_gate_lanes.py` 反例 2-5 共 4 条，任务 6 范围内的
          预期失败），**无新增意外失败**；passed 总数与任务 4.1 完全相同（2393），
          因为本任务是纯粹的内部重构（薄封装收敛），没有新增或删除任何测试用例。

- [x] 5. 批次 C：`complete_llm_once` 增加必填 `clearance` 参数（独立合入 / 独立回退）
  - **这是一个防复发的结构性约束，单列一个任务**：与「各链路接入闸门」（任务 6）分开合入，
    使它可独立验证、必要时可单独 revert 而不影响已修好的闸门
  - `complete_llm_once(..., *, clearance: BudgetClearance)` 增加必填关键字参数
  - 函数内断言 `clearance.verdict is None or clearance.verdict.allowed`，否则抛
    `RuntimeError("budget_clearance_violation")`（拿着「拒绝」的判定还发请求属编程错误）
  - 同步 4 个生产调用点：`model_step_service:1525`、`run_compactor:592`、`planning:481`、
    `session_context_compactor:388`（本任务先传 `clearance_from(lane, allowed_verdict)` 或
    `not_applicable(...)` 占位，真正的判定接入在任务 6 落地）
  - 同步 4 个 `Protocol` 定义（completion 端口的类型声明）
  - 同步 6 个测试文件里的替身；其中 `backend/tests/test_agent_runtime_planning.py:262-268`
    **直接断言了传给 completion 的 kwargs 字典**，必须同步更新，否则这条会以「多了一个 key」失败
  - 单元测试：`verdict.allowed is False` 传进去 → 抛 `budget_clearance_violation`；
    `not_applicable(reason=…)` → 放行且理由被记录
  - **验收标准**：全量测试通过，且新增一条「不传 clearance 就调不通」的负例测试（`TypeError`），
    证明约束真的生效而不只是文档约定
  - _Bug_Condition: 1.8 / 1.9 / 1.11 三处缺口的共同成因 —— 新增链路可以静默不表态_
  - _Expected_Behavior: 结构上使 expectedBehavior 无法被绕过（不表态即调不通 provider 边界）_
  - _Preservation: 参数为纯新增关键字参数，既有调用的请求构造与返回形状逐字节不变（3.3 / 3.5）_
  - **环境**：无库可完成
  - **依赖**：4.1
  - _Requirements: 2.8, 2.9, 3.3_
  - **实现记录（2026-08-09）**：
    - `single_step.py`：`complete_llm_once` 在参数列表末尾（`supports_vision` 之后）新增必填
      关键字参数 `clearance: BudgetClearance`（不给默认值，缺传即 `TypeError`）。函数体最前面
      （在 `_convert_messages_for_vision` / `create_llm_client` 之前，即任何 provider 调用之前）
      加断言：`if clearance.verdict is not None and not clearance.verdict.allowed: raise
      RuntimeError("budget_clearance_violation")`。import 新增
      `from app.services.token_accounting.gate import BudgetClearance`。
    - **四个 Protocol 定义**同步：`model_step_service.CompletionPort`、
      `run_compactor.RunCompactCompletionPort`、`planning.PlanningCompletionPort`、
      `session_context_compactor.CompactCompletionPort` 的 `__call__` 签名末尾（`supports_vision`
      之后）均加 `clearance: BudgetClearance`（同样不给默认值），四个文件各自 import
      `BudgetClearance`。
    - **四个生产调用点的占位策略**（均已在改动落地前与用户确认过设计文档给出的默认方案，
      未偏离设计文档）：
      1. **`model_step_service._call_prepared`**：采用了 `EXECUTION REQUIREMENTS` 里给出的默认
         方案，即 `clearance=BudgetClearance.not_applicable(LANE_BUSINESS_STEP, reason=
         "verdict already enforced by _budget_gate before this call")`，并在调用处加了一段
         注释说明理由。**未**改造 `_budget_gate` 让 verdict 向上传递——评估过这个更"精确"的替代
         方案后认为它需要修改 `_budget_gate` 的返回类型（从 `ModelStepResult | None` 变成携带
         verdict 的结构）并让 `complete_once` 两处调用点都传下去，改动面超出"占位"的意图，
         且 `_call_prepared` 在结构上只会在 `_budget_gate` 已经放行之后才被调用，`not_applicable`
         描述的正是这个事实（判定已经做过、就在几行代码之前），不是在编造一个不存在的判定，
         是诚实的占位。
      2. **`run_compactor.py`**（`_compact_batches` 内 `self._completion(...)` 调用点）：
         `clearance=BudgetClearance.not_applicable(LANE_RUN_COMPACT, reason=
         "budget gate not yet wired into run_compact (task 6.1)")`。
      3. **`planning.py`**（`complete_once` 内 `self._completion(...)` 调用点）：
         `clearance=BudgetClearance.not_applicable(LANE_PLANNING, reason=
         "budget gate not yet wired into planning (task 6.3)")`。
      4. **`session_context_compactor.py`**（`_complete_batch` 内 `self._completion(...)` 调用点）：
         读取 `_complete_batch` 的 `system_scope: str | None` 参数在调用方 `_compact_with_model`
         的传参方式后确认：`system_scope=None` 对应 session_compact（按 `usage_agent_id` 记账），
         `system_scope=SYSTEM_SCOPE_GROUP_COMPACT` 对应 group_compact（按租户记账，`usage_agent_id
         =None`）。因此按 `lane = LANE_GROUP_COMPACT if system_scope is not None else
         LANE_SESSION_COMPACT` 动态选 lane，`reason` 统一为
         "budget gate not yet wired into session/group compact (task 6.2)"。
      - import 新增：`run_compactor.py` 加 `LANE_RUN_COMPACT, BudgetClearance`；`planning.py` 加
        `LANE_PLANNING, BudgetClearance`；`session_context_compactor.py` 加
        `LANE_GROUP_COMPACT, LANE_SESSION_COMPACT, BudgetClearance`。
    - **测试文件同步**：全仓 `grep -rn "complete_llm_once\|_completion(" backend/tests` 逐一核查，
      结论如下：
      - `test_llm_single_step.py`：直接调用 `single_step.complete_llm_once(...)`，全部 11 处调用
        补上 `clearance=`（10 处用 `BudgetClearance.not_applicable(_TEST_LANE, reason="test")`，
        2 处新增测试用 `clearance_from(_TEST_LANE, BudgetVerdict(allowed=True))` 与
        `BudgetVerdict(allowed=False, ...)` 分别验证放行与拒绝路径）。
      - `test_agent_runtime_planning.py:262-268`（现为约 272-282 行）：这条**直接断言了传给
        completion 的 kwargs 字典**，按要求更新——先从 `calls[0][2]` 里 `pop("clearance")` 取出
        再断言剩余字典与原来逐字段相同（未增未减），再单独断言取出的 `clearance` 是
        `BudgetClearance` 实例、`lane == LANE_PLANNING`、`verdict is None`、
        `not_applicable_reason is not None`（不写死具体 reason 文案，避免测试与实现文案强耦合）。
      - `test_agent_runtime_run_compactor.py` / `test_agent_runtime_session_context_compactor.py` /
        `test_agent_runtime_model_step_service.py`：这三个文件里的替身函数（`complete` /
        `forbidden` / `recording_completion`）全部用 `**kwargs` 或 `*args, **kwargs` 兜底签名，
        未显式列出 `clearance`，不需要改；核查过里面对 kwargs 的断言都只取具体的 key（如
        `kwargs["tools"]`、`tool_names = {... for tool in calls[0][2]["tools"]}`），没有第二处
        对整个 kwargs 字典做逐字段相等比较，因此新增的 `clearance` key 不会导致这些断言失败
        （已用全量测试验证，见下方结果）。
      - 任务 1/2/4.1/4.2 产出的 `test_token_budget_gate_lanes.py`、
        `test_token_budget_preservation_baseline.py`、`test_token_budget_enforcement.py`、
        `test_token_accounting_gate.py`：`test_token_budget_gate_lanes.py` 里的
        `recording_completion` 替身与 `_FakeClient.complete` 均用 `**kwargs` 兜底，不需要改；
        `test_token_budget_preservation_baseline.py` 的 `_gate_would_call_completion` 辅助函数
        走的是 `gate.evaluate` 打桩而非 `complete_llm_once` 本身，不涉及 `clearance`；
        `test_token_budget_enforcement.py` 与 `test_token_accounting_gate.py` 均不直接调用
        `complete_llm_once` 或构造其替身。四个文件均未改动。
      - `test_llm_tool_capability_probe.py`：核查确认 `/llm-test` 探测端点直连
        `client.complete`（`create_llm_client(...).complete(...)`），不经过 `complete_llm_once`，
        与本任务无关，未改动。
    - **新增单元测试**（`test_llm_single_step.py`）：
      - `test_complete_once_rejects_a_denied_clearance_before_calling_the_provider`：
        `verdict.allowed=False` 的 clearance → 抛 `RuntimeError`，消息含
        `budget_clearance_violation`；断言 `client.calls == []`（在发 provider 请求前就短路）。
      - `test_complete_once_allows_a_not_applicable_clearance`：`not_applicable(reason="test")`
        → 正常放行，`clearance.not_applicable_reason == "test"` 确认理由被保留在对象上。
      - `test_complete_once_allows_an_allowed_verdict_clearance`：`verdict.allowed=True` 的
        clearance → 正常放行。
      - `test_complete_once_requires_clearance_as_a_structural_constraint`（负例）：完全不传
        `clearance` 参数直接调 `complete_llm_once(...)` → 抛 `TypeError`；断言
        `client.calls == []`，证明约束是结构性的（必填关键字参数缺省），不依赖任何额外实现，
        不只是文档约定。
    - **验证结果**：
      - `test_llm_single_step.py`：15 passed（含 4 条新增）。
      - `test_agent_runtime_planning.py` + `test_agent_runtime_run_compactor.py` +
        `test_agent_runtime_session_context_compactor.py` + `test_agent_runtime_model_step_service.py`：
        合计 93 passed。
      - `test_token_budget_gate_lanes.py` + `test_token_budget_enforcement.py` +
        `test_token_accounting_gate.py` + `test_token_budget_preservation_baseline.py`：
        45 passed, 4 failed——4 个失败是反例 2-5（`run_compact` / `planning` / `group_compact` /
        `model_probe` 仍无闸门），任务 6 范围内的预期失败，与改动前状态一致。
      - 全量 `backend/tests/`：**2397 passed, 7 failed**——失败集合与任务 4.2 记录的基线逐条
        一致（`test_feishu_card_tools.py` 1 条、`test_html_to_pdf.py` 2 条，环境依赖缺失，与本次
        改动无关；`test_token_budget_gate_lanes.py` 反例 2-5 共 4 条，任务 6 范围内的预期失败），
        **无新增意外失败**；passed 数从 2393 增至 2397，恰好对应本任务新增的 4 条单元测试。

- [x] 6. 批次 D：四条缺闸门的链路接入 `gate.check()`

  - [x] 6.1 `run_compact` 接入（`run_compactor.py`）
    - 位置：`compact_if_needed` 判定 `_should_compact` 之后、进入 `_compact_batches` 之前
    - 主体来源：扩展 `RunCompactInputs`，由 `model_step_service.compact_inputs` 顺带带出
      （那里已在同一会话里查了 `agent`，只多两条 SELECT 取 tenant / counter）
    - 超限：`raise RunCompactorError("token_budget_exceeded", budget_exceeded_message(verdict))`，
      并保持 `is_deterministic_compact_error = True`
    - `estimated_next_round_tokens` 传 0（只做击穿判定，不做预算预扣）
    - 选择「终止 Run」而非「跳过压缩继续」：压缩是上下文到 80% 水位才触发的，跳过后紧接着的业务步一定更贵、
      也一定被自己的闸门拦住，直接以 `token_budget_exceeded` 终止才让排查者看到真实原因
    - 把任务 1 的反例 2 转为「completion 端口未被调用」的正向断言
    - _Bug_Condition: isBugCondition 中 `lane = run_compact`（1.8）_
    - _Expected_Behavior: expectedBehavior(result)，reason 经 `node_executor._compact` 的 `exc.code` 落到 Run_
    - _Preservation: 未超限时压缩路径的行为、批次划分、记账入参逐字节不变（3.3 / 3.5）_
    - **环境**：无库可完成
    - **依赖**：3.1, 4.1, 5
    - _Requirements: 2.8, 2.13, 3.3, 3.5_
    - **实现记录（2026-08-09）**：
      - **`RunCompactInputs` 扩展**：`run_compactor.py` 新增字段
        `subjects: BudgetSubjects | None = None`（`frozen=True, slots=True` 的
        dataclass，`None` 是默认值，向后兼容不传 `subjects` 的旧构造点/测试替身）。
        import 新增 `from app.services.token_accounting.budget import
        budget_exceeded_message` 与 `from app.services.token_accounting.gate import
        LANE_RUN_COMPACT, BudgetClearance, BudgetSubjects, check as gate_check,
        clearance_from`（`BudgetClearance` 是任务 5 已导入的，保留原样）。
      - **`model_step_service.compact_inputs` 顺带带出 subjects**：在
        `model, agent, ledger = await self._load(context, state)` 之后，新增
        `tenant_id = uuid.UUID(context.tenant_id)` 与
        `tenant, tenant_counter = await self._load_budget_subjects(tenant_id)`
        （复用既有方法，两条独立 SELECT，与 `_resolve_budget_subjects` 走的是
        同一份加载逻辑，但这里不用 `_resolve_budget_subjects` 的降级包装——
        `compact_inputs` 本身已经在 `_load` 失败时会抛异常，不需要再吞一次；
        `_load_budget_subjects` 失败时的异常会照常向上传播，由
        `compact_if_needed` 的 `except ModelCapabilityError` 之外的路径正常
        传播出去，这与 `_load` 本身失败时的既有行为一致）。用
        `BudgetSubjects(agent=agent, tenant=tenant, tenant_counter=tenant_counter)`
        打包后传入 `RunCompactInputs(..., subjects=subjects)`。
      - **`compact_if_needed` 接入判定**：在 `if not _should_compact(inputs):
        return RunCompactResult()` 之后、`assert inputs.effective_input_budget
        is not None` 之前插入判定逻辑（这是"`_should_compact` 判定为真之后、
        `_compact_batches` 调用之前"区间里最早的可行位置，早于所有后续的
        batch/protected-id 构造，符合"发起真正 provider 请求之前完成判定"的
        要求）：
        - `inputs.subjects is not None` 时：调用 `gate_check(lane=
          LANE_RUN_COMPACT, subjects=inputs.subjects,
          estimated_next_round_tokens=0, run_id=context.run_id)`；
          `verdict.allowed` 为 False 时 `raise
          RunCompactorError("token_budget_exceeded",
          budget_exceeded_message(verdict))`；否则把
          `clearance_from(LANE_RUN_COMPACT, verdict)` 记到局部变量
          `clearance`，供后面传给 `_compact_batches`。
        - `inputs.subjects is None` 时：**选择放行，不判定**，构造
          `BudgetClearance.not_applicable(LANE_RUN_COMPACT, reason=
          "RunCompactInputs.subjects not supplied by this input_loader")`。
          **设计决定与理由**（按任务描述步骤 3 的要求详细记录）：
          `inputs.subjects is None` 理论上只会来自两种场景——(a) 测试替身用
          旧的 `RunCompactInputs(...)` 构造方式（不传 `subjects`，享受
          dataclass 默认值的向后兼容）；(b) 未来如果出现另一个不经过
          `model_step_service.compact_inputs` 的 `input_loader` 实现。这两种
          场景的共同点是："限额判定所需的输入缺失"，不是"判定跑过了、结果是
          拒绝"。3.6 定下的判据是"读取动作是否成功"来决定 fail-open 还是
          fail-closed；这里更进一步——判定这个动作本身根本没有发生（没有一个
          `verdict` 可以拿来分类），把"没有判定"等同于"判定失败应该
          fail-closed"会引入一个新的、不对称的语义：`agent=None`
          （system_scope 链路，任务 3.1 已支持）走的是"判定发生了，只是少一档"
          这条路，而`subjects is None` 会走"判定完全没跑却直接拦截"这条路
          ——两者的"不完整"性质不同，前者不该导致更严格的结果。选择放行的
          第二个理由是与 Preservation Checking（3.3 / 3.5）的关系：修复前
          `run_compact` 链路对任何输入都是"零判定、直接压缩"，这与"接入闸门
          但主体缺失时放行"在可观察行为上完全一致——用一个更严格的
          fail-closed 处理"主体缺失"，实质上是在没有测试要求的情况下引入了
          一种新的失败模式（`compact_inputs` 之外的任何 `input_loader` 都会
          因为没传 `subjects` 而被拦截），这超出了本任务"接入闸门"的范围。
          第三个理由是治理层面的：`inputs.subjects is None` 与"新增链路必须
          表态"（design.md 变更 4 的 `complete_llm_once` 必填 `clearance`
          参数）不是同一层的约束——`clearance` 约束的是"是否调用了
          provider"，这里约束的是"limpiar/判定输入是否齐备"，`compact_inputs`
          作为生产唯一构造点已经总是提供 `subjects`，`None` 只应该出现在测试
          替身或未来尚不完整的新接入点，用文档字符串（见 `RunCompactInputs`
          的 docstring 更新）把这个假设写清楚，比用运行时异常逼所有测试替身
          都升级更合适——这与 3.5（记账口径/入参逐字节不变）和 3.3（不引入
          额外拦截）的保留要求一致：不改变现有测试替身不传 `subjects` 时的
          行为。
        - `_compact_batches` 的调用签名扩展了 `clearance: BudgetClearance`
          必填关键字参数；`compact_if_needed` 末尾调用处传入上面算好的
          `clearance` 变量。`_compact_batches` 内部原来的占位
          `clearance=BudgetClearance.not_applicable(LANE_RUN_COMPACT,
          reason="budget gate not yet wired into run_compact (task 6.1)")`
          （任务 5 的占位）直接替换成 `clearance=clearance`（方法参数）。
      - **未修改 `node_executor.py`**：读了 `_compact` 方法确认
        `except Exception as exc: if not getattr(exc,
        "is_deterministic_compact_error", False): raise` 之后
        `code = getattr(exc, "code", "thread_compact_failed")` 直接取
        `exc.code`，`RunCompactorError.__init__` 已把 `code` 参数存到
        `self.code`，且类属性 `is_deterministic_compact_error = True` 早已
        存在（`run_compactor.py` 里定义 `RunCompactorError` 类时就有），
        `raise RunCompactorError("token_budget_exceeded", ...)` 天然满足
        两个条件，`lifecycle["reason"] = "token_budget_exceeded"` 无需
        任何额外改动即可生效——已用新增测试
        `test_breached_agent_budget_blocks_compact_before_completion_is_called`
        （驱动 `compact_if_needed` 本身，未经过 `node_executor`）与既有的
        `test_agent_runtime_node_executor.py`（29 项，`RunCompactorError` ->
        `lifecycle.reason` 的既有映射测试全部原样通过，未改动此文件）共同
        验证。
      - **测试文件更新**（`test_agent_runtime_run_compactor.py`）：
        - `_service(...)` 辅助函数新增可选参数 `subjects:
          BudgetSubjects | None = None`，透传进 `RunCompactInputs(...,
          subjects=subjects)`；不传时保持 `None`，向后兼容——全部既有测试用例
          未改一行调用方式，全部通过。
        - `_state(...)` 辅助函数**未改动**（`subjects` 与 `state`/`context`
          的构造无关，只影响 `RunCompactInputs`，改 `_service` 已经足够）。
        - 新增 `_breached_agent_subjects()` / `_clear_agent_subjects()` 两个
          辅助函数，各自构造一份 `BudgetSubjects`（`SimpleNamespace` 主体，
          与 `test_token_budget_gate_lanes.py` 的替身风格一致）。
          **关键实现细节**：两者的 `last_daily_reset` /
          `tenant_counter.last_daily_reset` 都用 `datetime.now(UTC)`（真实
          挂钟时间）而不是固定过去日期——因为 `compact_if_needed` ->
          `gate.check()` 不传显式 `now`，`budget.evaluate()` 内部会用
          `datetime.now(UTC)` 判断周期是否翻页，固定的过去日期在测试运行的
          当下会被 `_effective_used` 判定为"未翻页"，但这依赖测试运行时刻与
          固定日期的相对关系，脆弱且迟早会因为固定日期"过期"而误判为
          "已翻页、计数视为 0"，掩盖真实的击穿场景。
        - 新增三条测试：
          1. `test_breached_agent_budget_blocks_compact_before_completion_is_called`：
             用 `monkeypatch` 把 `gate.evaluate` 换成"转发到真实 `evaluate()`
             但强制 `mode=MODE_ENFORCE`"的包装（与
             `test_token_budget_gate_lanes.py` 反例 1/6/7 的做法一致，避免
             结果随执行模式默认值/缓存状态摇摆），驱动
             `compact_if_needed` 到 80% 水位、`subjects` 为超限 Agent，
             断言：`RunCompactorError` 抛出、`code ==
             "token_budget_exceeded"`、`is_deterministic_compact_error is
             True`、`calls == []`（completion 端口未被调用）。
          2. `test_unbreached_agent_budget_allows_compact_to_call_completion`：
             同样强制 `enforce`，但 `subjects` 换成未设限额的 Agent，断言
             `result.compacted is True` 且 `len(calls) == 1`——防止新判定
             误伤正常压缩路径（这是任务描述步骤 7 里"补一条确认
             completion 端口正常被调用"的要求）。
          3. `test_missing_subjects_does_not_block_compact`：`subjects=None`
             （不打任何桩，走真实的 `current_enforcement_mode()`），断言
             `result.compacted is True`——验证上面"subjects 缺失时放行"的
             设计决定在代码里确实如此，不是文档描述与实现不一致。
      - **`test_token_budget_gate_lanes.py` 反例 2 转正**：
        - 测试函数改名为
          `test_counterexample_2_run_compact_now_blocks_before_completion`，
          构造超限 `BudgetSubjects`（复用 `_breached_agent()` 辅助函数，
          `last_daily_reset` 同样改用 `datetime.now(UTC)` 而非模块级固定的
          `NOW` 常量，理由与上面一致；新增行内注释说明为什么这条反例不能像
          反例 1/6/7 那样用固定 `NOW`）；同样用 `monkeypatch` 强制
          `gate.evaluate` 的 `mode=MODE_ENFORCE`；断言
          `RunCompactorError`、`code == "token_budget_exceeded"`、
          `is_deterministic_compact_error is True`、`calls == []`。
        - 新增 import：`from app.services.agent_runtime.run_compactor import
          RunCompactorError`、`from app.services.token_accounting.gate import
          BudgetSubjects`。
        - 文件头部说明注释更新：反例列表里第 2 条的描述从"闸门接入是任务 6.1
          的范围，期望失败"改为"任务 6.1 已修复，期望通过"，并更新了
          "CRITICAL" 段落——反例 3-5 仍预期失败（任务 6.2/6.3/6.4 范围），
          反例 1/2/7 已转正。
        - **未改动**反例 3/4/5/6（分别是任务 6.2/6.3/6.4/7.1 的范围）。
      - **验证结果**：
        - `test_agent_runtime_run_compactor.py`：**21 passed**（18 条既有 +
          3 条新增，全部通过；既有 18 条未改一行断言）。
        - `test_agent_runtime_model_step_service.py`：**46 passed**（未新增
          任何测试——全仓搜索确认没有测试直接调用 `compact_inputs`，本任务
          未修改此测试文件）。
        - `test_token_budget_gate_lanes.py`：**4 passed（反例 1/2/6/7）, 3
          failed（反例 3/4/5，任务 6.2/6.3/6.4 范围内的预期失败）**。
        - `test_agent_runtime_node_executor.py`：**29 passed**，确认文件
          零改动（`git status` 已核实）且 `_compact` 对新错误码的既有处理
          逻辑天然生效。
        - 全量 `backend/tests/`：**2401 passed, 6 failed**——失败集合为
          `test_feishu_card_tools.py` 1 条 + `test_html_to_pdf.py` 2 条
          （环境依赖缺失，与本次改动无关）+ `test_token_budget_gate_lanes.py`
          反例 3/4/5 共 3 条（任务 6.2/6.3/6.4 范围内的预期失败）。与任务 5
          记录的基线（2397 passed, 7 failed）相比：失败数由 7 降为 6（反例 2
          转正，-1），通过数由 2397 增至 2401（+3 条新增测试 + 反例 2 由
          fail 变 pass 贡献 1 条，2397+3+1=2401，与实测完全对应），**无
          新增意外失败**。

  - [x] 6.2 `session_compact` / `group_compact` 接入（`session_context_compactor.py`）
    - 位置：`_compact_with_model` 首个 batch 之前
    - 主体来源：`CompactModelSelection` 增加 `subjects: BudgetSubjects`，由 `_resolve_models` 在它已打开的
      会话里一并 `load_subjects`（该方法已查过 `session` 与 `agent`，只多两条 SELECT）；
      `_compact_with_model` 不再需要自己开会话
    - 超限：`raise SessionContextCompactorError("token_budget_exceeded", …)`；
      压缩失败时保留上一份 Session Context（既有传播行为不变）
    - `group_compact` 走 `agent=None` → 只判 `tenant_day`（依赖 3.1）
    - 把任务 1 的反例 4 转为正向断言
    - _Bug_Condition: isBugCondition 中 `lane ∈ {session_compact, group_compact}`（1.8 / 1.9）_
    - _Expected_Behavior: expectedBehavior(result)_
    - _Preservation: 3.11（`system_scope` 归属与两条部分唯一索引不变）、3.5_
    - **环境**：无库可完成
    - **依赖**：3.1, 4.1, 5
    - _Requirements: 2.8, 2.9, 3.5, 3.11_
    - **实现记录（2026-08-09）**：
      - **`CompactModelSelection` 扩展**：新增字段 `subjects: BudgetSubjects | None = None`
        （`frozen=True, slots=True` 的 dataclass 已有字段之后新增），与任务 6.1
        `RunCompactInputs.subjects` 的向后兼容设计逐字一致：默认 `None`，允许旧构造点
        （测试替身直接构造 `CompactModelSelection` 而不经过 `_resolve_models`）继续工作。
        import 新增 `from app.services.token_accounting.budget import
        budget_exceeded_message` 与 `from app.services.token_accounting.gate import
        BudgetSubjects, check as gate_check, clearance_from, load_subjects`
        （`BudgetClearance` 是任务 5 已导入的，保留原样）。
      - **`_resolve_models` 顺带带出 subjects**：两条分支各自在已经打开的 `db` 会话里
        追加一次 `load_subjects(db, tenant_id=request.tenant_id, agent=...)` 调用——
        group session 分支（`session.session_type == "group"`）在拿到
        `resolve_multi_agent_compact_model(...)` 的结果之后紧接着调
        `load_subjects(db, tenant_id=request.tenant_id, agent=None)`（group_compact
        没有 Agent 主体，只判 tenant_day，依赖任务 3.1）；direct session 分支在
        `resolve_active_agent_model(db, agent)` 成功之后调
        `load_subjects(db, tenant_id=request.tenant_id, agent=agent)`（复用该分支
        已经查出来的 `agent` 变量）。两处都把结果通过新增的 `subjects=subjects`
        参数塞进 `CompactModelSelection(...)`。`load_subjects` 内部固定两条 SELECT
        （`Tenant` + `TenantTokenCounter`），未引入额外查询，符合任务描述的性能约束。
      - **`_compact_with_model` 首个 batch 之前接入判定**：方法签名新增必填关键字
        参数 `subjects: BudgetSubjects | None`；判定逻辑放在方法开头（`budget = self._budget(model)`
        之前），只算一次，不放进循环——`lane = LANE_GROUP_COMPACT if system_scope is
        not None else LANE_SESSION_COMPACT`（复用 `_complete_batch` 里原有的这条
        判断逻辑，选择在两处都写一遍而非提取成公共函数，因为逻辑只有一行且两处的
        上下文注释各有侧重，提取反而增加一次跳转）：
        - `subjects is not None` 时：调 `gate_check(lane=lane, subjects=subjects,
          estimated_next_round_tokens=0, run_id=None)`（这条链路没有 run_id 概念，
          传 `None`，`gate.check` 签名里 `run_id` 本就是可选参数）；`verdict.allowed`
          为 False 时 `raise SessionContextCompactorError("token_budget_exceeded",
          budget_exceeded_message(verdict))`；否则把 `clearance_from(lane, verdict)`
          存到局部变量 `clearance`，供后续所有 `_complete_batch` 调用复用。
        - `subjects is None` 时：**放行，不判定**，构造
          `BudgetClearance.not_applicable(lane, reason="CompactModelSelection.subjects
          not supplied by this model_resolver")`。**设计决定与任务 6.1
          `RunCompactInputs.subjects is None` 完全一致**（判定所需的输入缺失 ≠ 判定
          跑过并拒绝；这只应该发生在测试替身直接构造 `CompactModelSelection` 而不
          经过 `_resolve_models` 的场景，`_resolve_models` 作为生产唯一构造点总会
          带出 `subjects`），理由已在任务 6.1 的实现记录里完整展开，此处不重复
          论证，只在代码注释里引用同名理由。
      - **`_complete_batch` 签名变更**：新增必填关键字参数 `clearance:
        BudgetClearance`，替代原来方法内部自己构造的占位
        `BudgetClearance.not_applicable(lane, reason="budget gate not yet wired
        into session/group compact (task 6.2)")`（任务 5 的占位，连同判断 lane 的
        那一行一并移到了 `_compact_with_model`，`_complete_batch` 现在只是单纯把
        调用方算好的 `clearance` 转发给 `self._completion(...)`，不再自己算 lane）。
        循环内对 `_complete_batch` 的调用处新增 `clearance=clearance` 实参
        （复用 `_compact_with_model` 开头算好的同一个值，循环内不重复判定）。
      - **`compact()` 顺带传 subjects**：`_compact_with_model` 的调用处新增
        `subjects=selection.subjects` 实参。
      - **压缩失败保留上一份 Session Context**：读了 `context_builder.py`
        （`_rebuild_group_context_pack` 里 `except Exception as exc: raise
        ContextBuildError(...)`）与 `session_context_background.py`
        （`compact_session` 里 `candidate = await self._compactor.compact(request)`
        不在 try 块内、异常会原样向上传播给 `SessionContextCompactionScanner`）
        两处调用方，确认新抛出的 `token_budget_exceeded` 就是
        `SessionContextCompactorError` 的一个新错误码，走的是这两处已有的、对
        `compact()` 抛出任何异常都统一处理的既有路径——没有任何调用方对
        `SessionContextCompactorError` 的具体 `code` 做分支处理，因此本任务不需要
        新增任何"压缩失败保留旧 Session Context"的逻辑，这个属性是既有传播行为
        自动满足的，与设计文档"由 ContextBuilder 的既有错误传播决定"的描述一致。
      - **测试更新**（`test_agent_runtime_session_context_compactor.py`）：
        - `_resolver(selection)` 辅助函数未改动——它只是把传入的 `selection` 原样
          返回，调用方在构造 `CompactModelSelection` 时自行决定是否传 `subjects`，
          与任务描述预判一致。
        - `test_group_compact_resolves_the_tenant_scoped_context_model` 与
          `test_direct_compact_resolves_active_model_candidates`（走真实
          `_resolve_models` 的两条测试）：`_DB(...)` 替身按顺序追加了
          `Tenant`/`TenantTokenCounter` 的 `SimpleNamespace` 替身，`db.calls`
          断言从 `1`/`4` 分别更新为 `3`/`6`（各 +2，对应 `load_subjects` 的两条
          SELECT），并新增对 `selection.subjects` 三个字段的断言。
        - 现有 8 条既有测试用例（未传 `subjects` 或走 `_resolver`）全部原样通过，
          验证了向后兼容。
        - 新增 5 条测试：`test_breached_agent_budget_blocks_session_compact_before_completion`、
          `test_breached_tenant_budget_blocks_group_compact_before_completion`
          （两个 lane 各一条超限拦截）、
          `test_unbreached_agent_budget_allows_session_compact_to_call_completion`、
          `test_unbreached_tenant_budget_allows_group_compact_to_call_completion`
          （两个 lane 各一条守护正常路径不被误伤）、
          `test_missing_subjects_does_not_block_session_compact`（`subjects=None`
          向后兼容）。辅助函数 `_breached_agent_subjects` / `_breached_tenant_subjects`
          / `_clear_agent_subjects` / `_clear_tenant_subjects` 的 `last_daily_reset`
          均用 `datetime.now(UTC)`（真实挂钟时间）而非固定日期，理由与任务 6.1 的
          `_breached_agent_subjects` 完全一致（`gate.check()` 不传显式 `now` 时用
          真实挂钟时间判断周期翻页）；`_forced_enforce(monkeypatch)` 辅助函数把
          `gate.evaluate` 转发到真实实现但强制 `mode=MODE_ENFORCE`，与任务 6.1/
          反例 2/4 的做法一致，避免结果随执行模式默认值/缓存状态摇摆。
      - **`test_token_budget_gate_lanes.py` 反例 4 转正**：
        - 测试函数改名为
          `test_counterexample_4_group_compact_now_blocks_before_completion`，
          构造超限的 `BudgetSubjects(agent=None, tenant=..., tenant_counter=...)`
          （`last_daily_reset` 同样改用 `datetime.now(UTC)`），`resolver` 直接返回
          带 `subjects=` 的 `CompactModelSelection`；同样用 `monkeypatch` 强制
          `gate.evaluate` 的 `mode=MODE_ENFORCE`；断言 `SessionContextCompactorError`、
          `code == "token_budget_exceeded"`、`calls == []`。
        - 文件头部说明注释更新：反例列表里第 4 条的描述从"闸门接入是任务 6.2 的
          范围，期望失败"改为"任务 6.2 已修复，期望通过"；"CRITICAL"段落同步
          更新为"反例 3、5 仍预期失败（任务 6.3/6.4 范围）"。
        - **未改动**反例 3/5/6（分别是任务 6.3/6.4/7.1 的范围）。
      - **验证结果**：
        - `test_agent_runtime_session_context_compactor.py`：**13 passed**
          （8 条既有 + 5 条新增，全部通过；既有 8 条未改一行断言逻辑，只更新了
          `_DB` mock 数据与调用计数）。
        - `test_token_budget_gate_lanes.py`：**5 passed（反例 1/2/4/6/7）, 2
          failed（反例 3/5，任务 6.3/6.4 范围内的预期失败）**。
        - 组合跑 `test_agent_runtime_session_context_compactor.py` +
          `test_agent_runtime_run_compactor.py` +
          `test_agent_runtime_model_step_service.py` +
          `test_token_budget_gate_lanes.py` + `test_token_accounting_gate.py` +
          `test_token_budget_preservation_baseline.py` +
          `test_token_budget_enforcement.py`：**127 passed, 2 failed**（同上两条
          预期失败）。
        - 全量 `backend/tests/`：**2407 passed, 5 failed**——失败集合为
          `test_feishu_card_tools.py` 1 条 + `test_html_to_pdf.py` 2 条（环境依赖
          缺失，与本次改动无关）+ `test_token_budget_gate_lanes.py` 反例 3/5 共 2
          条（任务 6.3/6.4 范围内的预期失败）。与任务 6.1 记录的基线（2401
          passed, 6 failed）相比：失败数由 6 降为 5（反例 4 转正，-1），通过数由
          2401 增至 2407（+5 条新增测试 + 反例 4 由 fail 变 pass 贡献 1 条，
          2401+5+1=2407，与实测完全对应），**无新增意外失败**。

  - [x] 6.3 `planning` 接入（`planning.py`）
    - 位置：`_load_model` 之后、`self._completion` 之前
    - 主体来源：`_load_model` 已返回 `tenant_id`，用 `session_factory` 开一次会话
      `load_subjects(tenant_id=…, agent=None)`（依赖 3.1）
    - 超限：`return PlanningModelResult(error_code="token_budget_exceeded", retryable=False)`
    - `planning_scheduler` 用 `error.code` 作 failure_code —— 补一条测试钉住这个映射
    - 把任务 1 的反例 3 转为正向断言
    - _Bug_Condition: isBugCondition 中 `lane = planning`（1.9）_
    - _Expected_Behavior: expectedBehavior(result)_
    - _Preservation: 3.11、3.5；`retryable=False` 不得被既有重试逻辑当成瞬时故障重试_
    - **环境**：无库可完成
    - **依赖**：3.1, 4.1, 5
    - _Requirements: 2.9, 2.13, 3.5, 3.11_
    - **实现记录（2026-08-09）**：
      - **`planning.py` 的改动**：新增 `PlanningModelService._resolve_budget_subjects(context,
        tenant_id)`，用 `self._session_factory` **单独开一次新会话**调
        `gate.load_subjects(db, tenant_id=tenant_id, agent=None)`（`agent=None`——Planning
        是租户级判定，只判 `tenant_day` 一档，依赖任务 3.1）。这与任务 6.1/6.2 不同：
        6.1/6.2 是在已经打开的会话里"顺带"多查两条 SELECT；Planning 的 `_load_model` 用
        `async with self._session_factory() as db: ...` 后就把会话关掉了，`complete_once`
        没有已打开的会话可以复用，只能像任务描述要求的那样重新开一次。
      - `complete_once` 在 `_load_model` 成功之后、构造 `messages` 之前插入判定：
        `subjects is not None` 时调 `gate.check(lane=LANE_PLANNING, subjects=subjects,
        estimated_next_round_tokens=0, run_id=context.run_id)`；`verdict.allowed` 为
        False 时直接 `return PlanningModelResult(error_code="token_budget_exceeded",
        error_message=budget_exceeded_message(verdict), retryable=False)`，不再走到
        `self._completion(...)`；为 True 时把 `clearance_from(LANE_PLANNING, verdict)`
        存到局部变量 `clearance`，替换掉任务 5 留下的占位
        `BudgetClearance.not_applicable(LANE_PLANNING, reason="budget gate not yet wired
        into planning (task 6.3)")`，改为 `clearance=clearance` 直接传给
        `self._completion(...)`。
      - **`subjects is None`（`_resolve_budget_subjects` 开会话或加载本身失败）的处理**：
        与任务描述预判的一致——这条链路的 `subjects` 来源是"主动开一次新会话"而不是
        "已有会话里顺带查"，所以理论上不会出现"判定输入缺失但会话本身没问题"的场景；
        唯一可能让 `subjects is None` 的场景是 `session_factory()` 本身失败（数据库不可
        达等基础设施故障）或加载查询抛异常。按任务描述"若如此，请判断异常应如何处理，
        参考 budget.py 里 fail-open 的既有分级判据"的要求，采用了与
        `model_step_service._resolve_budget_subjects` **完全一致**的两级异常分类：
        `PROGRAMMING_ERROR_TYPES`（签名漂移等代码 bug）→ `logger.opt(exception=True).error(...)`
        + 关键字 `token_budget_enforcement_disabled_bug`；其余异常（包括
        `session_factory()` 本身抛出的连接类异常）→ `logger.warning(...)` + 关键字
        `token_budget_enforcement_disabled_transient`。两者都返回 `None`，调用方据此构造
        `BudgetClearance.not_applicable(LANE_PLANNING, reason="planning budget subjects
        unavailable (session_factory or load failed)")`，**放行而不拦截**——这不是"新增
        了一种缺失场景需要单独决策"，而是直接复用 3.6 已经定好的 fail-open 判据：取判定
        输入这个动作本身失败，属于基础设施故障，不能升级成 Planning 调用中断。与
        6.1/6.2 的"`subjects is None` 只应该来自测试替身"不同，Planning 这里
        `subjects is None` 确实对应一个真实的生产失败模式（开会话失败），因此专门写了
        一条测试（`test_planning_budget_subjects_load_failure_fails_open`）模拟
        `session_factory()` 第二次调用直接抛 `ConnectionError`，验证 fail-open 生效、
        completion 端口仍被调用、传入的 `clearance` 是 `not_applicable`。
      - **`retryable=False` 与既有重试逻辑的交互（已读代码确认，不存在需要报告的缺陷）**：
        读了 `PlanningRuntimeNodeExecutor._model`（`planning.py`）——`result.retryable`
        只控制是否进入 Planning 自己的「修复循环」（`attempt <= self._max_repairs` 时
        `status="running", reason="planning_repair_required"`，否则直接
        `status="failed"`）；`retryable=False` 时无论 `attempt` 是多少都直接进入
        `status="failed"` 分支，这是 Planning 侧唯一会检查 `retryable` 字段的地方。往上
        看 `PlanningCheckpointScheduler.handle`——它只在 `checkpoint.state["lifecycle"]["status"]
        == "completed"` 时才尝试调度子 Run，`failed` checkpoint 完全不会进入调度逻辑；
        真正处理 `failed` checkpoint 的是 `checkpoint_side_effects.delivery_from_checkpoint`
        （通过 `_failure_metadata` 把 `lifecycle["error"]["code"]` 映射成
        `DeliveryRequest.failure_code`），这条路径不区分 `retryable`，直接把 `failed`
        状态当作终态投递给用户，不会重试。再往上看 `command_worker.py` 的
        `RetryableCommandError` / `_release_for_retry`——那一层的"重试"针对的是**命令声明
        （command claim）级别的瞬时故障**（锁竞争、checkpoint 观测不稳定等基础设施问题），
        与 Graph 内部产出的业务结果（`lifecycle.status = "failed"`）完全是两个不同的层
        次：一个 `status="failed"` 的已提交 checkpoint 会被正常持久化并投递失败通知，
        不会被 `RetryableCommandError` 那一层的重试机制重新执行。**结论：既有重试逻辑
        不会把 `retryable=False` 的限额拒绝当成瞬时故障重试**，这是读代码确认的事实，
        不是假设；已用新增测试
        `test_token_budget_exceeded_terminates_immediately_without_entering_repair_loop`
        钉住"直接终止而不进入修复循环"这一半，`test_failed_planning_checkpoint_maps_error_code_to_failure_code`
        / `test_failed_planning_checkpoint_delivery_preserves_the_token_budget_error_code`
        钉住"`error.code` 原样落到 `failure_code`，不重试"这一半。
      - **测试文件更新**：
        - `test_agent_runtime_planning.py`：
          - `_DB` / `_session_factory` 改造为"共享队列"模式——`complete_once` 现在会
            调用 `self._session_factory()` 两次（`_load_model` 一次取 `LLMModel`，
            `_resolve_budget_subjects` 一次取 `Tenant`/`TenantTokenCounter`），
            `_session_factory` 现在预置一个 `deque([_Result(model), _Result(tenant),
            _Result(tenant_counter)])`，两次开会话各自 `yield` 一个包着**同一个** deque
            的新 `_DB`，按调用顺序消费。`tenant`/`tenant_counter` 默认 `None`——
            `budget.evaluate()` 的 `getattr(tenant, ..., default)` 与
            `periods.tenant_timezone(None)` 都能正常处理 `None`，走到"未设限额、不拦截"
            的正常路径，不会崩，这样既有测试不需要逐个补 `tenant`/`tenant_counter`
            参数就能继续通过。
          - `test_planning_model_uses_the_pinned_platform_model_without_tools`
            （现有测试，任务 5 留下的断言）：原断言 `sent_clearance.verdict is None` 已
            不成立（判定现在真的跑了），改为断言 `verdict is not None` 且
            `verdict.allowed is True`（未设限额时的正常放行结果），`not_applicable_reason
            is None`。这是"只改动必要的部分"——kwargs 字典本身（`tools`/`agent_id`/
            `tenant_id`/`system_scope`/`supports_vision`）的断言未变。
          - 新增 `_forced_enforce(monkeypatch)` / `_breached_tenant(tenant_id)` /
            `_clear_tenant(tenant_id)` 三个辅助函数，风格与任务 6.1/6.2 测试文件里的
            同名辅助函数一致（`last_daily_reset` 用 `datetime.now(UTC)` 而非固定日期，
            理由同 6.1/6.2：`gate.check()` 不传显式 `now` 时用真实挂钟时间判断周期翻页）。
          - 新增 4 条测试：
            `test_breached_tenant_budget_blocks_planning_before_completion`（超限拦截，
            断言 `error_code=="token_budget_exceeded"`、`retryable is False`、
            `calls == []`）、`test_unbreached_tenant_budget_allows_planning_to_call_completion`
            （守护正常路径不被误伤）、`test_planning_budget_gate_loads_subjects_with_agent_none`
            （驱动真实 `_resolve_budget_subjects` 两次开会话、验证 `load_subjects` 的
            `agent=None` 语义）、`test_planning_budget_subjects_load_failure_fails_open`
            （`session_factory()` 第二次调用抛 `ConnectionError` 模拟基础设施故障，验证
            fail-open）。
          - 新增 1 条 Preservation 测试：
            `test_token_budget_exceeded_terminates_immediately_without_entering_repair_loop`
            （见上文"`retryable=False` 与既有重试逻辑"）。
        - `test_agent_runtime_planning_scheduler.py`：新增
          `test_failed_planning_checkpoint_maps_error_code_to_failure_code`（验证
          `PlanningCheckpointScheduler.handle` 对已经是 `failed` 状态的 checkpoint 不会
          尝试调度子 Run、不消费 session factory——failed checkpoint 的投递不是它的职责）
          与 `test_failed_planning_checkpoint_delivery_preserves_the_token_budget_error_code`
          （直接调 `checkpoint_side_effects.delivery_from_checkpoint`，钉住
          `error.code == "token_budget_exceeded"` 原样落到 `DeliveryRequest.failure_code`
          且 `failure_message` 保留 `budget_exceeded_message` 的完整文案）。
        - `test_token_budget_gate_lanes.py`：反例 3 转正——函数改名为
          `test_counterexample_3_planning_now_blocks_before_completion`，从
          `test_agent_runtime_planning` 导入 `_forced_enforce` / `_breached_tenant`
          （复用而非重新定义，风格与反例 2/4 复用 `test_agent_runtime_run_compactor` /
          `test_agent_runtime_session_context_compactor` 的辅助函数一致），构造租户日
          上限已击穿的 `Tenant`/`TenantTokenCounter`，强制 `mode=MODE_ENFORCE`，断言
          `result.error_code == "token_budget_exceeded"`、`result.retryable is False`、
          `calls == []`。文件头部反例列表第 3 条描述更新为"任务 6.3 已修复"，"CRITICAL"
          段落更新为"反例 5 仍预期失败（任务 6.4 范围）"，反例 1/2/3/4/7 均已转正。
      - **未偏离设计的地方需要说明的一点**：设计文档与任务描述都只提到"`session_factory`
        本身失败"这一种异常场景，未明确要求区分"编程错误"与"基础设施故障"两个子类；
        本实现选择完整复刻 `model_step_service._resolve_budget_subjects` 的两级分类
        （而不是笼统地 `except Exception`），理由是这样才能保持与 3.6"异常按类型分级
        记日志"的既有判据一致，且与 6.1/6.2 的 `gate.check()` 内部分类风格统一，不引入
        第三种日志关键字体系。这不是范围之外的改动，只是把 3.6 的既有判据应用到一个新的
        调用点。
      - **验证结果**：
        - `test_agent_runtime_planning.py`：**25 passed**（21 条既有 + 4 条新增，全部
          通过；1 条既有断言按上述说明更新，其余既有断言未改）。
        - `test_agent_runtime_planning_scheduler.py`：**9 passed**（7 条既有 + 2 条
          新增）。
        - `test_token_budget_gate_lanes.py`：**6 passed（反例 1/2/3/4/6/7）, 1
          failed（反例 5，任务 6.4 范围内的预期失败）**。
        - 组合跑 `test_agent_runtime_planning.py` + `test_agent_runtime_planning_scheduler.py`
          + `test_token_budget_gate_lanes.py` + `test_agent_runtime_checkpoint_side_effects.py`：
          **56 passed, 1 failed**（同上，反例 5 预期失败）。
        - 全量 `backend/tests/`（**必须在 `backend/` 目录下运行**——
          `.venv/bin/python -m pytest tests/ -q`；从仓库根目录跑
          `backend/tests/` 会因为 `test_sso_toggle.py:9` 的
          `from tests.test_auth import ...` 触发 `ModuleNotFoundError: No module named
          'tests'` 而在 collection 阶段就中断，这是一个与本次改动完全无关的、
          cwd 相关的既有环境问题——用 `git stash` 验证过修复改动落地前跑
          `backend/tests/test_sso_toggle.py`（从仓库根目录）同样会炸，说明这不是本次
          引入的回归）：**2415 passed, 4 failed**——失败集合为
          `test_feishu_card_tools.py` 1 条 + `test_html_to_pdf.py` 2 条（环境依赖缺失，
          与本次改动无关）+ `test_token_budget_gate_lanes.py` 反例 5 共 1 条（任务 6.4
          范围内的预期失败）。与任务 6.2 记录的基线（2407 passed, 5 failed）相比：
          失败数由 5 降为 4（反例 3 转正，-1，与任务描述里"预期减少 1 条"完全一致），
          通过数由 2407 增至 2415（+7 条新增测试 + 反例 3 由 fail 变 pass 贡献 1 条，
          2407+7+1=2415，与实测完全对应），**无新增意外失败**。

  - [x] 6.4 `model_probe` 接入（`enterprise.py` 的连通性测试端点）
    - 位置：`create_llm_client` 之前（今天 probe 直连 `client.complete`，不经 `complete_llm_once`）
    - 主体来源：`current_user.tenant_id`；`tenant_id is None`（平台管理员）时用
      `BudgetClearance.not_applicable("platform_admin_no_tenant")`，与既有「无法归属则只记日志」一致
    - 超限：`return {"success": False, "error_code": "token_budget_exceeded", "error": budget_exceeded_message(verdict), …}`，
      HTTP 200，不发 provider 请求（沿用 probe 端点「返回结构化失败而不抛 500」的约定）
    - 把任务 1 的反例 5 转为正向断言（断言未创建 LLM client）
    - _Bug_Condition: isBugCondition 中 `lane = model_probe`（1.9）_
    - _Expected_Behavior: expectedBehavior(result)，以结构化失败体承载 reason 与消息_
    - _Preservation: 未超限时 probe 的成功/失败响应形状不变（3.3）_
    - **环境**：无库可完成
    - **依赖**：3.1, 4.1
    - _Requirements: 2.9, 2.13, 3.3_
    - **实现记录（2026-08-09）**：
      - **`enterprise.py` 的改动**：新增 `_resolve_probe_budget_clearance(current_user)`
        辅助函数，插在 `test_llm_model` 端点里"API Key 必填"检查之后、`create_llm_client`
        之前调用。取值逻辑与任务 6.3 `PlanningModelService._resolve_budget_subjects` 的
        结构完全对齐（同一份 fail-open 判据、同一套异常分类），但**主体来源不同**——
        Planning 从 `_load_model` 返回的 `tenant_id` 取值；本任务直接读
        `current_user.tenant_id`（HTTP 请求上下文自带，不需要像 Planning 那样先解析
        `context.tenant_id`）。
        - `current_user.tenant_id is None`（平台管理员）：直接返回
          `BudgetClearance.not_applicable(LANE_MODEL_PROBE, reason=
          "platform_admin_no_tenant")`，**完全不开会话、不做任何判定**——与本端点里
          已有的"platform_admin 没有 tenant_id，跳过落库但记日志"处理哲学逐字段一致
          （即 `test_llm_model` 里两处 `if current_user.tenant_id is not None: ... else:
          logger.info(...)` 分支），不引入新的失败模式。用新增测试
          `test_platform_admin_without_tenant_skips_the_budget_check_and_calls_the_client`
          钉住"不开会话"这一点（把 `enterprise.async_session` 换成一个调用即抛
          `AssertionError` 的替身，证明代码路径确实短路在 `tenant_id is None` 判断
          上，而不是走到 `load_subjects` 才因为某种巧合放行）。
        - `current_user.tenant_id is not None`：`async with async_session() as db:`
          开一次新会话（与设计文档指定的方式一致：`from app.database import
          async_session`，`enterprise.py` 顶部已有此导入，未新增），调
          `gate.load_subjects(db, tenant_id=current_user.tenant_id, agent=None)`
          （`agent=None`——model_probe 是租户级判定，只判 `tenant_day` 一档，依赖任务
          3.1），再调 `gate.check(lane=LANE_MODEL_PROBE, subjects=subjects,
          estimated_next_round_tokens=0, run_id=None)`（`run_id=None`——这条链路没有
          run_id 概念，与任务 6.2 `session_compact`/`group_compact` 的处理方式一致）。
        - 开会话或 `load_subjects` 本身失败（基础设施故障）：与
          `model_step_service._resolve_budget_subjects` /
          `PlanningModelService._resolve_budget_subjects` **完全一致**的两级异常分类
          （`PROGRAMMING_ERROR_TYPES` → `logger.error(..., exc_info=True)`；其余异常
          → `logger.warning(...)`），两者都返回
          `BudgetClearance.not_applicable(LANE_MODEL_PROBE, reason="model_probe budget
          subjects unavailable (session or load failed)")`——fail-open 放行，不拦截
          probe 端点（3.6）。**唯一的实现差异**：`enterprise.py` 用的是 stdlib
          `logging.getLogger(__name__)`（文件顶部已有 `logger = logging.getLogger
          (__name__)`），不是 `model_step_service`/`planning.py` 用的 `loguru`；因此
          `logger.opt(exception=True).error(...)` 改写成 `logger.error(...,
          exc_info=True)`（stdlib logging 的等价写法），日志消息文本（含
          `token_budget_enforcement_disabled_bug` / `token_budget_enforcement_disabled_transient`
          两个可 grep 的关键字）逐字保留，只是格式化方式从 loguru 的 `{}` 占位符
          改成了 f-string（与 `enterprise.py` 里其余 `logger.error(f"...")` /
          `logger.warning(f"...")` 的既有写法保持一致，未引入第三种日志风格）。
      - **超限拦截的响应体**：`test_llm_model` 里在 `_resolve_probe_budget_clearance`
        返回的 `clearance.verdict is not None and not clearance.verdict.allowed` 时，
        直接返回结构化失败体：`success=False`、`connection_success=False`、
        `latency_ms=0`、`connection_latency_ms=0`、`tool_calling_supported=None`、
        `tool_calling_latency_ms=0`、`capability_recorded=False`、新增
        `error_code="token_budget_exceeded"`、`error=budget_exceeded_message
        (clearance.verdict)`——字段集合对照的是端点里已有的"目标解析失败"/"API Key
        缺失"两个异常分支的返回形状（沿用 `success`/`connection_success`/
        `latency_ms`/`connection_latency_ms`/`tool_calling_supported`/
        `tool_calling_latency_ms`/`capability_recorded`/`error` 八个既有字段，只在
        末尾新增 `error_code`），HTTP 200，不创建 `client`（`client = None` 这一行
        代码之前就已经 return，`create_llm_client` 从未被调用），符合"probe 端点
        返回结构化失败而不抛 500"的既有约定。
      - **未偏离设计的一点说明**：设计文档里"超限"那行给出的响应体只列出了
        `success`/`error_code`/`error` 三个字段并用"…"省略其余；本实现选择把
        既有的全部八个字段都带上（而不是只带这三个），理由是响应模型不是强类型
        Pydantic schema（`test_llm_model` 直接返回字典），前端 `LlmTab.tsx` 等
        消费方如果无条件读取 `connection_success`/`tool_calling_supported` 等字段
        会因为 KeyError 类的访问失败而出问题；补全字段是"结构化失败体"这一约定本身
        要求的（对照另外两个既有异常分支都是全字段返回），不是范围之外的新增行为。
      - **未修改** `model_step_service.py` / `planning.py` / `run_compactor.py` /
        `session_context_compactor.py`（这四个文件是任务 4.2/6.1/6.2/6.3 的范围，
        本任务未触碰）；`gate.py` 未修改（`LANE_MODEL_PROBE` 常量已在任务 4.1 提供）。
      - **测试更新**：
        - `test_token_budget_gate_lanes.py`：反例 5 转正（详见文件头部说明注释与
          下方"任务 6.4 更新"记录），函数改名为
          `test_counterexample_5_model_probe_now_blocks_before_creating_a_client`，
          用一个最小的 `_FakeDB`（`__aenter__`/`__aexit__`/`execute` 按序消费两个
          预置结果）替换 `enterprise.async_session`，构造超限的 tenant/tenant_counter
          （`last_daily_reset` 用 `datetime.now(UTC)`，理由与反例 2/4 相同），强制
          `gate.evaluate` 的 `mode=MODE_ENFORCE`，断言 `created_clients == []`、
          `result["success"] is False`、`result["error_code"] ==
          "token_budget_exceeded"`。文件头部反例列表第 5 条与 CRITICAL 段落已更新
          为"7 个反例全部转正，仅反例 6 待任务 7.1"。
        - `test_llm_tool_capability_probe.py`：新增 4 条测试（风格延续文件已有的
          `monkeypatch.setattr(enterprise, ...)` + `SimpleNamespace(current_user)`
          替身写法）：
          1. `test_breached_tenant_budget_blocks_probe_before_creating_a_client`：
             超限拦截——`_forced_enforce` 钉死 `enforce`，`_FakeDB` 提供击穿的
             tenant/counter，断言 `created_clients == []`、`error_code ==
             "token_budget_exceeded"`、`"error"` 字段非空。
          2. `test_unbreached_tenant_budget_does_not_affect_the_probe_response_shape`：
             未超限守护——同样强制 `enforce`，但 tenant/counter 未设限额，断言
             `success=True`、`connection_success=True`、`tool_calling_supported=True`、
             `"error_code" not in result`、`len(client.calls) == 2`、
             `record.await_count == 2`（连通性 + 工具调用两次探测的 usage 都正常记账，
             证明闸门接入没有改变未超限时的既有记账路径，3.3）。
          3. `test_platform_admin_without_tenant_skips_the_budget_check_and_calls_the_client`：
             平台管理员放行——`async_session` 换成"一调用就 `AssertionError`"的替身，
             `current_user.tenant_id=None`，断言未抛异常（即代码确实没调
             `async_session()`）、`client.calls` 长度为 2。
          4. `test_budget_subjects_load_failure_fails_open_and_calls_the_client`：
             基础设施故障 fail-open——`async_session` 换成一调用就抛
             `ConnectionError` 的 `_FailingSessionFactory`，断言未拦截、
             `client.calls` 长度为 2、`"error_code" not in result`。
          四条新增测试都补充了 `record_token_usage_ledger` 的 `AsyncMock` 打桩（除
          `test_platform_admin_...` 一条——那条走的是既有的"平台管理员跳过落库,
          只记日志"分支，不需要打桩 ledger）避免真实调用 `ledger.record()` 时因为
          没有数据库连接而在 `LEDGER_MAX_RETRIES=2` 次重试后落 ERROR 日志（不影响
          断言正确性，但会产生噪音，且与文件里其它既有测试的一致性做法一样都
          显式打桩）。
      - **验证结果**：
        - `test_llm_tool_capability_probe.py`：**15 passed**（11 条既有 + 4 条
          新增，全部通过；既有 11 条未改一行断言逻辑，只新增了两处 import
          `gate as gate_module` / `MODE_ENFORCE`）。
        - `test_token_budget_gate_lanes.py`：**7 passed, 0 failed**——反例
          1/2/3/4/5/6/7 全部通过（反例 6 目前仍是"验证今天口径矛盾确实存在"的
          断言，是设计使然，见下方"任务 6.4 更新"记录）。
        - 全量 `backend/tests/`（**必须在 `backend/` 目录下运行**，理由与任务 6.3
          记录一致——从仓库根目录跑会因 `test_sso_toggle.py` 的既有 cwd 问题在
          collection 阶段中断，与本次改动无关）：**2420 passed, 3 failed**——失败
          集合为 `test_feishu_card_tools.py` 1 条 + `test_html_to_pdf.py` 2 条
          （环境依赖缺失：`libgobject-2.0-0` 系统库缺失导致 weasyprint 无法加载，
          与本次改动无关）。与任务 6.3 记录的基线（2415 passed, 4 failed）相比：
          失败数由 4 降为 3（反例 5 转正，-1，与任务描述"预期减少 1 条"完全一致），
          通过数由 2415 增至 2420（+4 条新增测试 + 反例 5 由 fail 变 pass 贡献 1
          条，2415+4+1=2420，与实测完全对应），**无新增意外失败**。
      - **未发现需要委派方注意的既有缺陷**：`_resolve_llm_test_target` /
        `_record_llm_tool_capability` 两个辅助函数与本任务的改动边界清晰，未发现
        需要额外处理的耦合点；`enterprise.py` 使用 stdlib logging 而非 loguru 是
        本文件既有的整体风格选择，本任务遵循这个既有风格而非引入 loguru，不算
        偏离设计（设计文档本身没有规定日志库的选择，只规定了可 grep 的关键字，
        这一点已经满足）。

  - [x] 6.5 各链路终止原因落到 `token_budget_exceeded` 的测试
    - `node_executor._model`：既有测试保留、不改
    - `node_executor._compact`：新增 —— `RunCompactorError("token_budget_exceeded")` →
      `lifecycle.reason == "token_budget_exceeded"`
    - `planning_scheduler`：新增 —— `PlanningModelResult(error_code="token_budget_exceeded")` → failure_code 一致
    - 新增一条驱动 `delivery._safe_failure_content` 的测试：渲染结果同时包含
      `错误码：token_budget_exceeded` 与 `budget_exceeded_message` 的四项信息
      （`blocked_scope` / 千分位 `used` / 千分位 `limit` / `reset_at.isoformat(timespec="minutes")`）
    - **环境**：无库可完成
    - **依赖**：6.1, 6.2, 6.3, 6.4
    - _Requirements: 2.1, 2.13_
    - **实现记录（2026-08-09）**：
      - **对 `planning_scheduler` 那一条的核实结论**：读了任务 6.3 在
        `test_agent_runtime_planning_scheduler.py` 里新增的两条测试的完整源码
        （不是只信任任务 6.3 的转述）：
        1. `test_failed_planning_checkpoint_maps_error_code_to_failure_code`——
           构造一个 `lifecycle.status == "failed"`、`error.code ==
           "token_budget_exceeded"` 的 checkpoint，驱动
           `PlanningCheckpointScheduler.handle(...)`，断言 `session_factory`
           完全未被消费（`assert not factory.sessions`）。这条钉住的是
           "已经是 `failed` 状态的 checkpoint 不是 `handle()` 的职责，它只在
           `status == "completed"` 时才调度子 Run"——防止未来有人误以为
           `handle()` 也要处理终止投递，从而在这里意外消费一次 session factory。
        2. `test_failed_planning_checkpoint_delivery_preserves_the_token_budget_error_code`——
           直接调用 `checkpoint_side_effects.delivery_from_checkpoint(run,
           checkpoint)`（真正做 `error.code` → `failure_code` 映射的函数），
           断言 `delivery.failure_code == "token_budget_exceeded"` 且
           `delivery.failure_message` 保留了 `budget_exceeded_message()` 产出的
           完整文案（"企业当日 token 用量已达上限（500,000/500,000，
           scope=tenant_day）。..."）。
        两条测试合起来完整覆盖了任务 6.5 对 `planning_scheduler` 的要求
        （`PlanningModelResult(error_code="token_budget_exceeded")` →
        `failure_code` 一致），且用的是真实的 `PlanningModelResult` 产出的
        `error.code` 值（不是虚构的字符串），`error_message`/`retryable`
        字段虽未在这两条测试里单独断言，但已在任务 6.3 新增的
        `test_breached_tenant_budget_blocks_planning_before_completion`
        （`test_agent_runtime_planning.py`）与
        `test_token_budget_exceeded_terminates_immediately_without_entering_repair_loop`
        两条测试里覆盖（`error_code`/`retryable=False`/`calls == []`）。
        **结论：无需重复添加，任务 6.3 已完整覆盖，本任务未修改
        `test_agent_runtime_planning_scheduler.py`**。
      - **新增测试 1：`node_executor._compact`**（`test_agent_runtime_node_executor.py`
        新增 `test_token_budget_exceeded_compact_error_commits_a_failed_terminal_lifecycle`，
        紧跟在既有的
        `test_deterministic_compact_error_commits_a_failed_terminal_lifecycle`
        之后，复用同一套 `_executor` / `FailingRunCompactor` / `_state` /
        `_context` 测试替身，风格与既有测试逐字一致）：构造
        `FailingRunCompactor(RunCompactorError("token_budget_exceeded", "企业当日
        token 用量已达上限（500,000/500,000，scope=tenant_day）。"))`，驱动
        `executor.execute("compact", state, context)`，断言
        `update["lifecycle"]["status"] == "failed"`、`next_route == "terminal"`、
        `reason == "token_budget_exceeded"`、`error["code"] ==
        "token_budget_exceeded"`。测试文档字符串里明确写清这条测试驱动的是
        `node_executor._compact` 对既有错误码传播逻辑（`exc.code` → `reason`）
        在新错误码 `token_budget_exceeded` 上的边界确认，不是重复造轮子
        （任务 6.1 已经在 `test_agent_runtime_run_compactor.py` 里测过
        `compact_if_needed` 本身会不会抛这个异常，本任务测的是上层
        `node_executor` 对它的既有处理逻辑天然生效，未修改
        `node_executor.py` 任何一行代码）。
      - **新增测试 2：`delivery._safe_failure_content`**（放在
        `test_agent_runtime_delivery.py`，紧跟在既有的
        `test_write_file_protocol_failure_guides_user_to_regenerate` 之后，
        复用文件已有的 `_terminal_request` / `_RecordingDB` / `_agent` /
        `_participant` / `_group` / `_session` / `_run` 等辅助函数与
        `test_runtime_failure_delivers_backend_error_code_and_run_id` 的构造
        风格；未选择 `test_agent_runtime_channel_delivery.py`，因为那个文件
        专注渠道投递侧的转发逻辑，不构造 `DeliveryRequest` 的失败字段场景，
        `test_agent_runtime_delivery.py` 才是 `_safe_failure_content` 既有测试
        的落点）：新增
        `test_token_budget_exceeded_failure_renders_the_budget_exceeded_message`。
        用真实的 `BudgetVerdict(allowed=False, blocked_scope=SCOPE_TENANT_DAY,
        used=500_000, limit=500_000, reset_at=datetime(2026, 8, 7, 16, 0,
        tzinfo=UTC), mode=MODE_ENFORCE)` 与真实的
        `budget_exceeded_message(verdict)`（未手写假消息文本）生成
        `failure_message`，通过 `_terminal_request(run, status="failed",
        failure_code="token_budget_exceeded", failure_message=message)` 驱动
        `deliver_runtime_message(...)`，断言渲染出的 `ChatMessage.content`：
        (a) 包含 `"错误码：token_budget_exceeded"`；(b) 完整包含
        `budget_exceeded_message(verdict)` 产出的整条消息文本（`message in
        content`）；(c) 额外逐项断言 `blocked_scope` 的中文标签
        （`"企业当日"`）、千分位 `used`（`f"{verdict.used:,}"`）、千分位
        `limit`（`f"{verdict.limit:,}"`）、`reset_at.isoformat(
        timespec="minutes")` 均单独可在渲染结果里找到——这一步是"整条消息
        文本匹配"之外的补充确认，防止 `budget_exceeded_message` 未来改动
        文案措辞时这条测试仍然只靠字符串包含关系通过而没有验证到具体信息点。
      - **验证结果**：
        - `test_agent_runtime_node_executor.py` + `test_agent_runtime_delivery.py`
          + `test_agent_runtime_planning_scheduler.py` 组合跑：
          **60 passed, 0 failed**（含本任务新增的 2 条测试）。
        - 全量 `backend/tests/`（**必须在 `backend/` 目录下运行**
          `.venv/bin/python -m pytest tests/ -q`，理由与任务 6.3/6.4 记录一致：
          从仓库根目录跑会因 `test_sso_toggle.py` 的既有 cwd 问题在 collection
          阶段中断，与本次改动无关；且系统默认 Python 3.10 没有 `datetime.UTC`，
          必须用仓库自带的 `backend/.venv`）：**2422 passed, 3 failed**——失败
          集合与任务 6.4 记录的基线逐条一致（`test_feishu_card_tools.py` 1 条 +
          `test_html_to_pdf.py` 2 条，均为环境依赖缺失——`libgobject-2.0-0`
          系统库缺失导致 weasyprint 无法加载，与本次改动无关）。与任务 6.4
          记录的基线（2420 passed, 3 failed）相比：失败数不变（本任务只新增
          测试，未修改任何生产代码），通过数由 2420 增至 2422（恰好对应本任务
          新增的 2 条测试，2420+2=2422，与实测完全对应），**无新增意外失败**。
      - **未发现需要委派方注意的既有缺陷**：核实过程中未发现
        `planning_scheduler` / `node_executor._compact` / `delivery` 三处存在
        任何需要修复的遗漏或不一致；`test_agent_runtime_channel_delivery.py`
        经确认与本任务无关，未改动。

- [x] 7. 批次 E：`group_handoff` 收敛到统一判定

  - [x] 7.1 拆分 `_target_budget_available`
    - token 部分删除，改由 `_validate_targets` 对每个目标调
      `gate.check(lane=LANE_GROUP_HANDOFF, subjects=load_subjects(db, tenant_id=…, agent=mention.agent))`，
      用 `verdict.allowed` 判断
    - 非 token 部分（`max_tool_rounds`、`max_llm_calls_per_day`）原样搬到更名后的
      `_target_run_budget_available()`，语义逐条不变
    - 超限仍抛 `GroupAgentHandoffError("group_handoff_budget_unavailable", repairable=True)`，错误码与 repairable 不变
    - `tenant` / `tenant_counter` 在同一个 `db` 会话里查一次后按目标复用，不按目标重复查（目标数通常 1–3）
    - 把任务 1 的反例 6（两套口径相反）转为「两侧结论一致」的断言
    - _Bug_Condition: 1.10（两套口径互相矛盾）_
    - _Expected_Behavior: 与直接对话复用同一判定实现与同一执行模式（2.10）_
    - _Preservation: 3.9（默认口径下排除结果不变）+ `max_tool_rounds` / `max_llm_calls_per_day` 逐条不变_
    - **环境**：无库可完成
    - **依赖**：4.1
    - _Requirements: 2.10, 3.9_
    - **实现记录（2026-08-09）**：
      - **函数拆分**：`group_handoff.py` 里的 `_target_budget_available(agent, *, now,
        tenant=None)` 被删除，拆成两部分：
        1. **非 token 部分**原样搬到新函数 `_target_run_budget_available(agent, *, now)`
           ——只保留 `max_tool_rounds`（真值/类型/正负判断）与 `max_llm_calls_per_day`
           （耗尽 + 当日未重置）两段检查，逐字段不变；`tenant` 参数被去掉——实读确认
           旧函数里 `tenant` 唯一的用途是 `effective_timezone(agent, tenant)`，只服务于
           已删除的 token 部分的翻页判定（`is_new_local_day`/`is_new_local_month`），
           非 token 部分从未读过 `tenant`；`llm_calls_reset_at` 的翻页判断用的是
           `now.date()`（naive UTC 比较，与时区无关，这是拆分前就存在的既有实现，
           本任务未改动这一点）。
        2. **token 部分**完全删除（包括 `max_tokens_per_day`/`max_tokens_per_month`
           命中判断与 `is_new_local_day`/`is_new_local_month` 调用），改由
           `_validate_targets` 对每个目标调 `gate.check(lane=LANE_GROUP_HANDOFF, ...)`。
        - `group_handoff.py` 顶部 import 相应调整：删除
          `from app.services.token_accounting.periods import effective_timezone,
          is_new_local_day, is_new_local_month`（三者均已无调用者），新增
          `from app.services.token_accounting.budget import budget_exceeded_message`
          与 `from app.services.token_accounting.gate import LANE_GROUP_HANDOFF,
          BudgetSubjects, check as gate_check, load_subjects`。
      - **`tenant`/`tenant_counter` 复用的实现方式**：在 `_validate_targets` 里，
        `self_targets` 检查通过之后、进入 for 循环之前，调用一次
        `subjects = await load_subjects(db, tenant_id=source_run.tenant_id)`（`agent`
        参数留空，只取 `tenant`/`tenant_counter` 这一对）；循环内对每个
        `mention.agent` 单独构造 `BudgetSubjects(agent=mention.agent,
        tenant=subjects.tenant, tenant_counter=subjects.tenant_counter)` 后调
        `gate_check(lane=LANE_GROUP_HANDOFF, subjects=..., estimated_next_round_tokens=0,
        run_id=str(source_run.id), now=clock)`——`tenant`/`tenant_counter` 只查一次、
        跨目标复用；`agent` 部分随目标变化，天然不能复用，符合任务描述"目标数通常
        1-3"场景下的性能预期（循环外 2 次 SELECT + 循环内每个目标 0 次额外 SELECT，
        而不是循环内每个目标 2 次）。
      - **两段检查的合并方式**：循环内对每个 `mention` 先调
        `_target_run_budget_available(mention.agent, now=clock)`（非 token 部分，
        便宜、不涉及 IO），不满足则直接抛 `group_handoff_budget_unavailable`；
        满足后再调 `gate_check(...)`（token 部分），`verdict.allowed` 为 False 时
        同样抛 `group_handoff_budget_unavailable`，消息文案里追加
        `budget_exceeded_message(verdict)` 提供 scope/used/limit/reset_at 四项信息
        （错误码与 `repairable=True` 完全不变，只是消息文案更丰富，符合 2.1 对错误
        消息形状的要求，且不违反"错误码不变"这条约束）。两个检查任一不满足即拦截，
        "任一个不满足就拦截"的语义与拆分前逐点一致。
      - **反例 6 转正的具体断言设计**（`test_token_budget_gate_lanes.py`）：没有选择
        驱动完整的 `_validate_targets`（需要构造更多 Group/Session 相关脚手架），
        而是直接对同一个 `BudgetSubjects` 分别调
        `gate.check(lane=LANE_GROUP_HANDOFF, ...)` 与
        `model_step_service.RuntimeModelStepService._budget_gate(...)`（内部就是
        `gate.check(lane=LANE_BUSINESS_STEP, ...)`），在 `warn_only` 与 `enforce`
        两种模式下分别断言两侧 `allowed`/`_budget_gate 是否返回 None` 的结论一致。
        选择这个驱动方式的理由：`_validate_targets` 只是把 `gate.check()` 的结果包了
        一层 `GroupAgentHandoffError`，直接调 `gate.check()` 已经能证明"两条链路现在
        用同一个 verdict"这个核心事实，且避免了为了让测试跑通而引入与本反例意图无关
        的 Group/Session 构造复杂度。踩了一个坑并修正：`_budget_gate` 内部调用
        `gate.check()` 时不显式传 `now`（默认走 `datetime.now(UTC)`），必须在
        monkeypatch 里把 `evaluate()` 的 `now` 同样钉死为固定的 `NOW`，否则两侧会
        因为"谁传了 now、谁没传"这个与反例意图无关的差异，在周期翻页判定上给出不同
        结果，产生假失败（已通过补充 `"now": NOW` 到 `fake_evaluate` 里解决）。
        文件头部说明注释与 CRITICAL 段落同步更新为"7 个反例全部转正"。
      - **`test_token_period_consistency.py` 的处理方式**：完整读了这四条测试
        （`test_target_budget_blocked/available_when_daily/monthly_counter_has_
        not_rolled_over_in_tenant_local_day/month`），确认它们测的是"周期翻页豁免
        语义"本身（是否按租户时区而非 UTC 判断计数器已跨周期），不是 `group_handoff`
        特有的业务逻辑——这个语义现在完全由 `budget.evaluate()` / `budget._effective_used()`
        承载。由于被测函数 `_target_budget_available` 已被删除，选择**迁移测试断言的
        调用目标**（而非删除测试）：四条测试改为直接构造
        `SimpleNamespace(tokens_used_today=0, last_daily_reset=now)` 作为
        `tenant_counter`（租户日上限设为 `None`，隔离掉 tenant_day 这一档，只让
        agent_day/agent_month 决定结果，与原测试"只测 agent 侧的翻页语义"的意图
        一致），驱动 `budget.evaluate(agent=..., tenant=..., tenant_counter=...,
        now=..., mode=MODE_ENFORCE)`，断言 `verdict.allowed`/`verdict.blocked_scope`
        而不是一个布尔值。测试函数签名从同步改为 `async def`（`evaluate` 是协程），
        文件顶部 import 从 `from app.services.agent_runtime import group_handoff`
        改为 `from app.services.token_accounting import budget` +
        `from app.services.token_accounting.budget import MODE_ENFORCE,
        SCOPE_AGENT_DAY, SCOPE_AGENT_MONTH`；文件头部与小节说明注释同步更新，
        说明"这四条测试的意图保持不变，只是不再通过一个已被删除的函数签名去验证它"。
        原测试里"UTC 比较会给出错误答案"的对照断言（`assert last_daily_reset.date()
        != now.date(), ...`）逐条保留未动，只是最终判定从
        `group_handoff._target_budget_available(...)` 换成了
        `budget.evaluate(...).allowed` / `.blocked_scope`。**未删除任何一条既有测试**，
        `is_new_local_day`/`is_new_local_month` 的 import 也原样保留（仍用于对照断言）。
      - **`test_token_budget_preservation_baseline.py` 域点 2 的兼容性修复**：实读
        确认 `group_handoff._target_budget_available(handoff_agent, now=NOW)` 这一行
        因函数被删除会直接 `AttributeError`，导致整个文件无法运行——这正是任务描述
        里"若因签名删除导致测试跑不起来，允许做最小的兼容性修复"的情形。修复方式：
        把这条基线断言改为调用新的等价路径——通过
        `gate.check(lane=LANE_GROUP_HANDOFF, subjects=BudgetSubjects(agent=handoff_agent,
        tenant=tenant, tenant_counter=counter), estimated_next_round_tokens=0, now=NOW)`
        走一遍完全相同的输入（`limit=0, used=0`），并用 `monkeypatch.setattr(gate,
        "evaluate", forced_enforce_evaluate)` 把生效模式钉死为 `MODE_ENFORCE`（原因
        同反例 6：`gate.check()` 不显式传 `mode`，测试环境没有真实数据库连接，
        `current_enforcement_mode()` 会 fail-open 到 `warn_only`，掩盖 `_breach`
        语义本身）。**没有把这条基线的期望值从"放行"改成"拦截"去回避矛盾，而是如实
        记录了一个矛盾并展开说明**：这条基线断言测的"事实"本身（旧函数用真值判断把
        `limit=0` 误判为无上限而放行）已经随着旧函数被删除而不再存在于代码里——
        任务 7.1 落地的同一时刻，`group_handoff` 判定 token 部分的唯一实现就已经是
        `gate.check()`，它天然遵循 `_breach` 语义（`limit=0` 参与阈值判断，不与
        `None` 合并），所以 `limit=0` 现在被拦截。这与 design.md "一处有意的行为
        变更" 描述的方向完全一致，但**发生的时间点是任务 7.1 而不是任务 7.2**——
        7.2 的范围是"用 Property 3 域穷举测试更完整地覆盖这个变更"，不是"这个变更
        本身发生在 7.2"。断言的新期望值（`allowed is False`）在测试内附了完整的
        文字说明，注明这是任务 7.1 落地时就已生效的行为、不是本任务擅自修改基线
        期望值去掩盖行为变化。**这是任务描述里预见到的矛盾，如实报告**：域点 2
        原本的设计意图是"记录收敛前的行为，供 7.2 对比"，但由于收敛（删除旧函数）
        与"唯一有意的行为变更"（`limit=0` 语义修正）在任务 7.1 是同一个改动、无法
        拆开——旧函数一旦删除，就不存在一个"收敛了判定实现、但还没触发 `limit=0`
        语义修正"的中间状态。这不是本任务选择提前触发了 7.2 的范围，而是 7.1 的
        改动范围本身（"token 部分删除，改由 gate.check 判断"）必然蕴含了这个副作用，
        design.md 也印证了这一点（"变更 5"部分写的正是这一句改动）。7.2 仍有明确
        的独立范围：Property 3 的域穷举（`limit ∈ {None, 0, 正数}` × `used` 上下
        阈值 × `max_tool_rounds`/`max_llm_calls_per_day` 是否耗尽的完整组合），
        本任务只验证了 `limit=0, used=0` 这一个点，不构成对 7.2 范围的侵占。
      - **新增测试列表**（`test_agent_runtime_group_handoff.py`，3 条）：
        1. `test_breached_agent_token_budget_fails_preflight_with_repairable_error`
           ——构造 `max_tokens_per_day=100_000, tokens_used_today=200_000` 的超限
           目标 Agent，驱动 `preflight_group_agent_handoff`，断言抛出
           `GroupAgentHandoffError("group_handoff_budget_unavailable", repairable=True)`，
           且 `AgentCycleGuard.ensure_delegation_allowed`（`ensure`）未被调用——
           确认预算检查在 cycle guard 之前短路，与拆分前非 token 检查的执行顺序
           一致。这是本文件第一条真正驱动 token 部分超限拦截路径的测试——实读确认
           拆分前完全没有测试覆盖"目标 Agent token 超限"这个场景（既有测试的
           `_target` 构造器从不设置任何 token 限额），即"目前没有任何测试覆盖
           `group_handoff_budget_unavailable` 这个错误码的触发路径"这一判断成立，
           已按任务描述要求补上。
        2. `test_breached_tenant_token_budget_fails_preflight_for_every_target`
           ——构造租户日上限已击穿的 `Tenant`/`TenantTokenCounter`（目标 Agent 自身
           未设任何限额），驱动 `preflight_group_agent_handoff`，断言同样抛出
           `group_handoff_budget_unavailable`。这条钉住了 design.md 提到的"判定主体
           多了 tenant/tenant_counter：意味着租户日上限击穿时所有目标都不可用"这一
           拆分后才出现的新行为（拆分前的 `_target_budget_available` 完全不读
           `tenant.max_tokens_per_day`/`tenant_counter.tokens_used_today`，`tenant`
           参数唯一的用途是算时区）。
        3. `test_within_budget_target_still_passes_preflight`——守护测试：目标 Agent
           与租户都在限额内，断言 `preflight_group_agent_handoff` 正常返回
           `intent`、`ensure.await_count == 1`（cycle guard 正常执行到），确认新加入
           的 token 检查不会误伤未超限的正常路径。
        - 为构造这三条测试，`_target(...)` 辅助函数扩展了 `**agent_overrides` 透传
          参数（保持向后兼容，未传时行为不变）；新增 `_forced_enforce(monkeypatch)`
          辅助函数（风格与 `test_token_budget_gate_lanes.py`/
          `test_agent_runtime_run_compactor.py` 等文件的同名辅助函数一致）；`_DB`
          测试替身补充了 `execute()` 方法与可选的 `tenant`/`tenant_counter` 构造
          参数（默认均为 `None`，对应"未配置任何租户级限额"，使全部 14 条既有测试
          不需要改一行调用方式就能继续通过——`load_subjects` 在 `scalar_one_or_none()`
          返回 `None` 时，`budget.evaluate()` 对缺失的 `tenant`/`tenant_counter`
          一律按"该档无限额"处理，不会误判）。
      - **最终验证结果**：
        - 目标测试组合跑：`test_agent_runtime_group_handoff.py`（17 passed，14 条
          既有 + 3 条新增）+ `test_token_period_consistency.py`（10 passed，4 条
          迁移 + 6 条既有未动）+ `test_token_budget_gate_lanes.py`（7 passed，
          反例 1-7 全部转正）+ `test_token_budget_preservation_baseline.py`
          （14 passed，域点 2 兼容性修复后通过）+ `test_token_accounting_gate.py`
          （12 passed，未改动）+ `test_token_budget_enforcement.py`（16 passed，
          未改动）：合计 **76 passed, 0 failed**。
        - 全量 `backend/tests/`（**在 `backend/` 目录下运行**
          `.venv/bin/python -m pytest tests/ -q`）：**2425 passed, 3 failed**。
          失败集合与任务 6.5 记录的基线（2422 passed, 3 failed）逐条一致
          （`test_feishu_card_tools.py` 1 条 + `test_html_to_pdf.py` 2 条，均为
          环境依赖缺失——`libgobject-2.0-0` 系统库缺失导致 weasyprint 无法加载，
          与本次改动无关）。与基线相比：失败数不变（3），通过数由 2422 增至 2425
          （恰好对应本任务新增的 3 条测试：`test_token_period_consistency.py` 的
          4 条迁移测试与既有测试数量相同、`test_token_budget_gate_lanes.py` 反例 6
          由"验证矛盾"改为"验证一致"是同一条测试的重写、
          `test_token_budget_preservation_baseline.py` 域点 2 是修改而非新增，
          三者净新增数量为 0；只有 `test_agent_runtime_group_handoff.py` 新增的
          3 条测试贡献了净增量，2422+3=2425，与实测完全对应），**无新增意外失败**。

  - [x] 7.2 Property 3 域穷举测试（含两处有意的行为变更）
    - **Property 3: Preservation** - 群聊 handoff 在默认口径下的排除结果不变
    - 域：`limit ∈ {None, 0, 正数}` × `used` 在阈值上下 × `max_tool_rounds` / `max_llm_calls_per_day` 是否耗尽
    - 在 `effective_mode = enforce` 下，逐点断言与任务 2 记录的今天 `_target_budget_available` 基线一致
    - **两处有意的差异必须显式断言，不能让它悄悄发生**：
      1. `limit == 0`：今天真值判断当无上限 → 放行；收敛后按 `_breach` 语义 → 拦截（向 3.2 对齐，是修正）
      2. `configured_mode = warn_only`（管理员显式选择）或 grace 窗口内：今天无视模式硬拦，
         收敛后跟随模式放行（否则会造出「群聊里不可用、直接对话里可用」这个矛盾的镜像版本）
    - **环境**：无库可完成
    - **依赖**：7.1
    - _Requirements: 2.10, 3.2, 3.9_
    - **实现记录（2026-08-09）**：
      - **新增测试文件**：`backend/tests/test_group_handoff_budget_property.py`。选择
        新建独立文件而不是塞进 `test_agent_runtime_group_handoff.py`——本任务是纯粹的
        Property 3 域穷举，与该文件里既有的"契约测试"（preflight/apply 的输入校验、
        事务回滚、乱序拒绝等）在测试意图上不同类，且穷举组合数不小，独立文件更符合
        `test_token_budget_preservation_baseline.py` 这类 Property 测试单独成文件的
        既有组织方式。
      - **驱动层选择**：直接调用被测的核心判定函数——`gate.check(lane=
        LANE_GROUP_HANDOFF, ...)`（token 部分）与 `_target_run_budget_available()`
        （非 token 部分），而不是每个域点都构造完整的 Group/Session 脚手架走
        `preflight_group_agent_handoff`。理由：域穷举的组合数（token 部分 5 个域点 +
        非 token 部分 4+4 个域点 + 两处有意变更 2+1 个域点 + 交叉验证 4 个域点，共 25
        条）如果每条都构造完整脚手架，测试体量会远超收益；直接调用核心函数已经能
        精确验证"穷举判定逻辑本身"这个 Property 3 的核心诉求。只在交叉验证一节补了
        一条驱动真实 `preflight_group_agent_handoff` 入口的测试（见下），把"核心逻辑
        穷举"与"端到端集成确认"分层覆盖，不是互相替代。
      - **域点划分依据**："token 部分与非 token 部分相互独立"——`_validate_targets`
        对每个目标依次调用两个独立的检查函数，任一为 False/`allowed=False` 就短路
        拦截，二者没有耦合。因此测试分三节：
        1. **token 部分域**（固定非 token 部分为"未耗尽"，即 `max_tool_rounds=10`、
           `max_llm_calls_per_day=None`）：`limit=None` 用 `used∈{0, 10^9}` 两点验证
           始终放行；`limit=100_000`（代表性正数，未穷举多个正数值，按任务描述
           "正数取一个代表值即可"执行）用 `used∈{under=99_999, at=100_000,
           over=100_001}` 三点覆盖阈值上下；断言逐点与 `_breach()` 语义
           （`used >= limit` 才拦截）一致，同时每点都附带断言非 token 部分独立保持
           放行，确认两部分互不干扰。
        2. **非 token 部分域**（固定 token 部分为"未超限"，`max_tokens_per_day=None`）：
           `max_tool_rounds ∈ {10, 1, 0, -1}`（正常/边界正数/零/负数，对应
           `_target_run_budget_available` 里 `<= 0` 才拦截的判据）；
           `max_llm_calls_per_day` 用 5 个域点覆盖——未配置（`None`）、未耗尽
           （`used < limit`）、耗尽且 `llm_calls_reset_at=None`、耗尽且
           `reset_at` 落在今天（当日已重置）、耗尽但 `reset_at` 落在今天之前（尚未
           重置，视为陈旧放行）。后三个域点是任务描述明确要求的——
           `_target_run_budget_available` 对 `llm_calls_reset_at` 有专门的
           `is None or .date() == now.date()` 判断，只测"耗尽/未耗尽"两档会漏掉这条
           专门逻辑。
        3. **交叉验证域**（`itertools.product([False, True], [False, True])` 穷举
           2x2）：token 部分与非 token 部分各自独立击穿/未击穿的全部组合，断言
           "整体可用 = 非 token 可用 AND token 可用"，即任一个不满足就拦截，不需要
           两者同时失败才拦截。额外补了一条驱动真实 `preflight_group_agent_handoff`
           的测试，覆盖"两者都失败"这个组合点：用 `patch` 把 `group_handoff.gate_check`
           换成一个从不设置 `side_effect`（即从未被真正调用/await）的 `AsyncMock`，
           验证非 token 检查（`_target_run_budget_available`）确实先短路拦截，
           `gate.check()`（token 部分）根本没有机会被执行——这与拆分前单函数内
           "先做非 token 检查、再做 token 检查，任一为 False 就直接返回不可用"的
           既有顺序完全一致，只是判定逻辑现在分散在两个独立函数里。
      - **有意变更 1 的断言设计**：新增
        `test_intentional_change_1_zero_limit_now_always_blocks`，用
        `@pytest.mark.parametrize` 穷举 `used ∈ {0, 1, 100_000, 10^9}` 四个取值——
        不只测 `used=0` 这一个点，是为了证明"limit=0 现在总是被拦截"这个结论与
        `used` 取值无关，不是巧合命中了某个特定用量。断言 `verdict.allowed is False`
        时，`assert` 的第二个参数（说明性消息）完整写出"这与任务 2 记录的旧基线
        （放行）不同，是向 `_breach` 语义（3.2）对齐的有意修正，已在任务 7.1 落地"
        这句话本身，确保这个差异不会被断言语句悄悄吞掉——pytest 断言失败时会打印
        这条消息，让任何后续误改都会在失败输出里直接看到这段说明文字，而不只是一个
        冷冰冰的 `True != False`。同时每个域点都附带断言非 token 部分不受这个变更
        影响，独立保持放行。
      - **有意变更 2 的断言设计（两种场景都测，按任务描述"如果时间允许，两种都测
        更完整"的建议执行）**：
        1. `test_intentional_change_2_explicit_warn_only_mode_now_follows_mode`：
           管理员显式选择 `mode=warn_only`（用 `_force_gate_mode` helper 把
           `gate.evaluate()` 强制钉死为 `MODE_WARN_ONLY`），构造超限 Agent，断言
           `verdict.allowed is True`——说明性断言消息完整写出"今天（收敛前）的旧
           实现会无视执行模式硬拦，即使管理员已经显式选择只告警；跟随模式放行是
           有意的：否则会造出「群聊里不可用、直接对话里可用」这个矛盾的镜像版本"。
           额外断言 `verdict.blocked_scope == SCOPE_AGENT_DAY`——确认"判定本身仍然
           识别出命中了哪一档"这件事没有被 `allowed=True` 掩盖掉，即软告警/日志
           这一层信息在 warn_only 下依然完整。
        2. `test_intentional_change_2_grace_window_now_follows_mode`：不强制
           `gate.evaluate()` 的 `mode`，改为打桩 `system_setting_dao.get_value`
           返回 `{"mode": "enforce", "grace_until": <未来时刻>}`，让判定走真实的
           `current_enforcement_state()` 路径（任务 3.4 的 grace 语义），更贴近
           "grace 窗口内"这个措辞本身——`configured_mode` 确实是 `enforce`，只是
           `effective_mode` 因为 grace 生效而暂时是 `warn_only`。断言
           `verdict.mode == MODE_WARN_ONLY`（先确认 grace 真的生效了）与
           `verdict.allowed is True`。**踩了一个坑并修正**：`grace_until` 最初用
           固定的 `NOW`（2026-08-06，本文件其它域点用于周期数学的固定时间点）+
           `timedelta(days=1)` 构造，但 `gate.check()` 判断 grace 是否生效时用的是
           真实的 `datetime.now(UTC)`（挂钟时间），不是传给 token 周期判定的那个
           `now` 参数——两者是完全独立的两个"当前时间"概念，固定的 `NOW` 相对真实
           挂钟时间是过去的一个时刻，导致 `grace_until` 也落在过去，grace 从未生效。
           已改为用 `datetime.now(UTC) + timedelta(days=1)` 构造 `grace_until`，
           并在代码注释里写明这两个"now"概念不同、为什么不能共用固定常量，避免
           未来维护者重蹈同样的坑。
      - **未采用的替代方案及理由**：曾考虑把"有意变更 2"两种场景合并成一条参数化
        测试（`@pytest.mark.parametrize` 覆盖 warn_only 与 grace），但两种场景的
        输入构造方式完全不同（一个是强制 `evaluate()` 的 `mode` 参数，一个是打桩
        `get_value` 走真实 grace 解析路径），参数化会让测试体内出现大量
        `if scenario == "grace": ... else: ...` 分支，反而降低可读性；保持两条独立
        测试函数、共享断言意图但各自完整表达输入构造，更符合本仓库其它 Property
        测试文件（如 `test_token_accounting_budget.py` 里 grace 相关的 5 条测试）
        一贯采用的"每个语义分支一条独立测试函数"的风格。
      - **验证结果**：
        - `test_group_handoff_budget_property.py` 单独跑：**25 passed**。
        - 目标测试组合跑：`test_group_handoff_budget_property.py` +
          `test_agent_runtime_group_handoff.py` + `test_token_period_consistency.py` +
          `test_token_budget_gate_lanes.py` + `test_token_budget_preservation_baseline.py`
          + `test_token_accounting_gate.py` + `test_token_budget_enforcement.py`：
          **101 passed, 0 failed**。
        - 全量 `backend/tests/`（**在 `backend/` 目录下运行**
          `.venv/bin/python -m pytest tests/ -q`）：**2450 passed, 3 failed**。
          失败集合与任务 7.1 记录的基线（2425 passed, 3 failed）逐条一致
          （`test_feishu_card_tools.py` 1 条 + `test_html_to_pdf.py` 2 条，均为环境
          依赖缺失——`libgobject-2.0-0` 系统库缺失导致 weasyprint 无法加载，与本次
          改动无关）。与基线相比：失败数不变（3），通过数由 2425 增至 2450
          （恰好对应本任务新增的 25 条测试，2425+25=2450，与实测完全对应），
          **无新增意外失败**。本任务只新增测试文件，未修改任何生产代码。
      - **未发现需要委派方注意的既有缺陷**：`_target_run_budget_available` 与
        `gate.check(lane=LANE_GROUP_HANDOFF, ...)` 的现有实现（任务 7.1 落地）经
        完整域穷举验证，行为与 design.md / bugfix.md 的期望描述完全吻合，未发现
        遗漏的域点或未被覆盖的分支。

- [x] 8. 批次 F：配置面收口（后端）

  - [x] 8.1 `SystemSettingDAO.set_value`（`system_setting_dao.py`）
    - 新增 `async def set_value(self, key: str, value: dict) -> SystemSetting`（upsert）
    - 今天这个 DAO 只有 `get_by_key` / `get_value`，写只能绕道通用端点 —— 这是 1.7 的直接成因
    - **环境**：无库可完成（单元测试用会话替身；真实 upsert 冲突行为在任务 11.5 复验）
    - **依赖**：无
    - _Requirements: 2.7_
    - **实现记录（2026-08-09）**：
      - **`set_value` 的实现方式**：`system_setting_dao.py` 新增
        `async def set_value(self, key: str, value: dict) -> SystemSetting`，用
        `self.session()` 开一个会话（复用 `BaseDAO` 既有的上下文管理器模式，与
        `get_by_key` 完全一致的开会话方式），先 `select(SystemSetting).where(SystemSetting.key
        == key)` 查该 key 是否已存在，存在则 `setting.value = value`（原地修改，不新建行），
        不存在则 `SystemSetting(key=key, value=value)` 后 `db.add(setting)`；最后
        `await db.flush()` 再 `return setting`。
      - **设计决定："先查后写"而非数据库原生 `INSERT ... ON CONFLICT` upsert**。理由：
        (1) 任务描述本身只要求"新增一个 upsert 方法"，未要求原子 upsert，"先查后写"实现的是
        upsert 的语义（存在则更新、不存在则创建），满足需求；(2) `app.api.enterprise.
        update_system_setting`（`PUT /system-settings/{key}`，约第 1190-1212 行）现有的
        "通用写端点"实现正是这个"先查是否存在、存在则赋值、不存在则 `db.add()`"的模式，
        `set_value` 逐字段复刻它，保持与仓库现有代码风格一致，避免在同一张表上出现两种不同的
        写入语义（一种走 `INSERT ... ON CONFLICT`，一种走"先查后写"）；(3) `INSERT ... ON
        CONFLICT` 是 PostgreSQL 方言特定语法，虽然 SQLAlchemy 有
        `sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update(...)` 的封装，
        但引入它会让 `SystemSettingDAO` 出现与仓库其余 DAO（`BaseDAO.create()`/`update()`
        均是"先查/直接操作已知对象，不做条件式 upsert"）不同的写法，增加认知负担，且这张表
        （`system_settings`）的写入频率低（配置项，不是高并发计数器），"先查后写"在极端并发下
        理论上存在"检查后再写"的竞态窗口（两个并发写请求都查到不存在，都尝试插入），但这与
        `update_system_setting` 现有实现面临的风险完全一致，不是本次新引入的问题，真实的并发
        冲突行为交给任务 11.5（有库环境）复验。
      - **为什么在 `flush()` 之后立即 `return setting` 而不是 `refresh()`**：`self.session()`
        在会话结束时会自动 `commit()`（`BaseDAO.session()` 的既有行为），`flush()` 会把
        待写入的变更发到数据库（触发 `server_default=func.now()` / `onupdate=func.now()`
        等数据库端计算的默认值填充到 ORM 对象上，这是 SQLAlchemy flush 后对象属性会被
        数据库返回值刷新的标准行为，不需要额外 `refresh()`），参考了 `BaseDAO.create()`/
        `update()` 两个既有方法都是"操作后 `flush()`、然后直接返回对象"的写法，未使用
        `db.refresh()`（`update_system_setting` 用了 `refresh()`，但那是因为它在 `flush()`
        之外单独调用了 `db.commit()`，而 `set_value` 依赖 `self.session()` 退出时的自动
        commit，`flush()` 足以让新建/更新的行在事务内可见并把数据库端默认值写回对象）。
      - **新增测试**（`backend/tests/test_system_setting_dao.py`，新建文件——全仓搜索确认
        之前没有任何测试文件覆盖 `system_setting_dao.py` / `SystemSettingDAO`，命名参考
        既有 DAO 测试 `test_base_dao.py` 的惯例）：
        - `test_set_value_creates_a_new_row_when_key_is_absent`：key 不存在时（会话替身的
          `execute()` 返回 `scalar_one_or_none() is None`），断言 `session.added` 恰好新增了
          一个 `SystemSetting` 实例（`key`/`value` 字段正确）、返回值就是这个新建对象、
          `flush()`/`commit()` 均被调用，且没有第二次 `add()`。
        - `test_set_value_updates_the_existing_row_instead_of_creating_a_second_one`：
          构造一个已存在的 `SimpleNamespace(key=..., value={"mode": "warn_only"})` 作为
          "查到的既有行"，断言 `session.added == []`（没有走 `db.add()` 新建第二行）、
          既有对象的 `value` 字段被原地更新为新值、返回值就是这个既有对象。
        - 测试替身（`RecordingSession`/`SessionFactory`）风格与 `test_base_dao.py` 里的
          `RecordingSession`/`SessionFactory` 一致，通过
          `monkeypatch.setattr("app.dao.base.async_session", SessionFactory(session))`
          注入，不需要真实数据库连接。
      - **验证结果**：
        - `test_system_setting_dao.py`：**2 passed**。
        - 全量 `backend/tests/`（**在 `backend/` 目录下运行** `.venv/bin/python -m pytest
          tests/ -q`，理由与前序任务记录一致：系统默认 Python 3.10 没有 `datetime.UTC`，
          必须用仓库自带的 `backend/.venv`）：**2452 passed, 3 failed**。失败集合与任务
          7.2 记录的基线（2450 passed, 3 failed）逐条一致（`test_feishu_card_tools.py`
          1 条 + `test_html_to_pdf.py` 2 条，均为环境依赖缺失——`libgobject-2.0-0` 系统库
          缺失导致 weasyprint 无法加载，与本次改动无关）。与基线相比：失败数不变（3），
          通过数由 2450 增至 2452（恰好对应本任务新增的 2 条测试，2450+2=2452，与实测
          完全对应），**无新增意外失败**。
      - **未发现需要委派方注意的既有缺陷**：`update_system_setting` 端点本身未被本任务
        修改（护栏收口是任务 8.2 的范围），本任务只新增了 DAO 方法与测试，未触碰任何既有
        生产代码路径的行为。

  - [x] 8.2 执行模式端点 + 通用端点护栏（`enterprise.py`）
    - `GET /api/enterprise/token-budget-enforcement` →
      `{configured_mode, effective_mode, grace_until, grace_active, set_by, propagation_seconds: 30}`；
      权限 `get_current_admin`（org_admin 可读，便于自查为什么被拦）
    - `PUT /api/enterprise/token-budget-enforcement` → body `{mode, clear_grace?, grace_until?}`；
      权限**必须是平台管理员**（`_is_platform_admin_user`）—— 全平台单值开关，
      一个租户的 org_admin 不该能替所有租户关掉限额；写入后调 `reset_enforcement_mode_cache()`
    - `PUT /system-settings/{key}` 增加护栏：`key == "token_budget_enforcement_mode"` 时同样要求平台管理员，
      响应里提示改用专用端点。**这是一个既有的越权面**（今天任何 org_admin 都能改这个全平台 key），顺手收掉
    - 测试：org_admin 调 PUT → 403；平台管理员 → 200 且同进程内下一次判定立即用新值；
      通用端点对该 key 的新护栏；`clear_grace: true` 后 grace 立即失效
    - _Bug_Condition: 1.7（无任何产品入口，只能直连数据库或走通用端点）_
    - _Expected_Behavior: 2.7 —— 模式可读可写、当前生效值对管理员可见_
    - _Preservation: 通用 `PUT /system-settings/{key}` 对其他 key 的权限与行为不变_
    - **环境**：无库可完成
    - **依赖**：3.4, 8.1
    - _Requirements: 2.7, 2.14_
    - **实现记录（2026-08-09）**：
      - **路由前缀确认结果**：`enterprise.py` 顶部 `router = APIRouter(prefix="/enterprise",
        tags=["enterprise"])`，本文件里所有路由装饰器都不重复写 `/enterprise`（例如既有的
        `@router.get("/llm-providers")` 对应 `GET /enterprise/llm-providers`）。`app/main.py`
        里 `app.include_router(enterprise_router, prefix=settings.API_PREFIX)`，
        `settings.API_PREFIX`（`app/config.py:87`）固定为 `"/api"`。因此新增的
        `@router.get("/token-budget-enforcement")` / `@router.put("/token-budget-enforcement")`
        最终挂载路径是 `GET/PUT /api/enterprise/token-budget-enforcement`，与任务描述、
        design.md 里写的路径完全一致，装饰器内只写 `/token-budget-enforcement`（不重复
        `/enterprise` 前缀），与本文件其它路由的既有写法一致。
      - **`GET /token-budget-enforcement` 的实现**：权限 `get_current_admin`（org_admin
        可读）。调用 `budget.current_enforcement_state()` 拿到 `EnforcementState`，再单独调
        `system_setting_dao.get_value(SETTING_ENFORCEMENT_MODE, {})` 取原始存储值——
        `EnforcementState` 本身不携带 `set_by`（那不是判定语义的一部分，只是审计信息），
        所以 `set_by` 直接从原始 value 字典里读，读不到时给 `None`（与 `grace_until`
        「缺失即 None」的表示方式保持一致，选 `None` 而非空字符串，因为响应里其它「不存在」
        的字段——`grace_until`——用的也是 `None`，两者语义一致：这个信息点当前没有值，
        不是「有一个空字符串的值」）。
      - **`grace_active` 判据的选择与理由**：没有采用「`effective_mode != configured_mode`」
        这个等价判据，而是直接按 `grace_until is not None and now < grace_until`
        （`now = datetime.now(UTC)`）现算，理由记在 `_token_budget_enforcement_payload`
        的文档字符串里：两种判据在当前系统能构造出的所有状态下结论一致，但
        「effective 是否不同于 configured」在语义上回答的是「grace 当前是否改变了判定结果」，
        而管理员在这个 GET 端点上问的问题是「grace 窗口本身是否还没过期」——这两个问题
        目前恰好总是同一个答案，但把它们混为一谈会在未来引入新的窗口语义（例如以后出现
        「grace 生效但不改变 effective_mode」的场景）时产生歧义。直接从 `grace_until` +
        当前时间现算是最贴近字面含义、最不容易在未来产生歧义的判据，成本也最低（不需要
        `EnforcementState` 暴露额外字段，`grace_until` 已经在响应里）。
      - **`propagation_seconds` 的来源**：读了 `budget.py` 确认 `_MODE_TTL_SECONDS = 30.0`
        确实是 30；但这个常量是模块内部的缓存调优细节，未出现在 `budget.__all__` 导出列表里
        （已核对），因此选择在 `enterprise.py` 里硬编码 `30`，并在
        `_token_budget_enforcement_payload` 的注释与代码内联注释里写明「与
        `budget._MODE_TTL_SECONDS` 保持同步」，而不是 `from app.services.token_accounting
        import budget as _budget` 后引用 `_budget._MODE_TTL_SECONDS`——跨模块引用一个显式
        不导出的私有常量比硬编码一个数字加同步注释更脆弱（既有的 ruff 配置未开
        `SLF001`，技术上可以引用，但这不改变该常量本来就不打算被外部依赖的设计意图）。
      - **`PUT /token-budget-enforcement` 的请求体模型**：新增
        `TokenBudgetEnforcementUpdate(mode: str, clear_grace: bool = False, grace_until:
        str | None = None)`。`mode` 用 `@field_validator` 校验必须在 `KNOWN_MODES` 里
        （不在时抛 `ValueError`，FastAPI 自动转成 422）；`grace_until` 用
        `@field_validator` 校验非 `None` 时必须能被 `datetime.fromisoformat` 解析
        （提前在请求边界校验格式，而不是留到写入后才在 `current_enforcement_state()`
        的 `_parse_grace_until` 里默默吞掉「不可解析」——那条兜底路径是为存量脏数据设计的
        安全网，不应该被用来掩盖一次新写入请求本身的格式错误）。
      - **`grace_until` / `clear_grace` 的互斥与优先级设计（按任务描述要求详细说明）**：
        三种输入组合按以下优先级处理，在 `update_token_budget_enforcement` 函数体内实现为
        一个 `if/elif/elif` 链：
        1. `clear_grace=True`：无条件生效，新 value 里完全不写 `grace_until` 键——即使
           请求体里同时传了 `grace_until`（这种同时传两者的请求在语义上自相矛盾，选择
           让 `clear_grace` 单方面赢，而不是报错拒绝，因为「清除」是一个更明确、更安全的
           意图，且这样处理不需要引入额外的 422 校验分支）。已用测试
           `test_put_endpoint_clear_grace_true_immediately_deactivates_grace` 钉住这个
           「同时传两者时 clear_grace 获胜」的场景。
        2. `clear_grace=False` 且 `grace_until` 非 `None`：写入这个新值，覆盖掉旧的
           grace 窗口（管理员显式设置一个新的观察窗口，例如延长 grace）。
        3. `clear_grace=False` 且 `grace_until is None`：**保留原有的 grace_until**（如果
           之前有的话）——这是任务描述里特别强调的一点：「不传 grace_until 且不
           clear_grace」必须保留原状，不能意外清空一个正在进行中的 grace 窗口。实现方式是
           先读一次现有 value（`system_setting_dao.get_value(SETTING_ENFORCEMENT_MODE, {})`），
           取出其中的 `grace_until`（如果存在），在这一分支里原样写回新 value。这是本任务
           唯一需要「先读后写」的地方（`mode` 与 `set_by` 不需要读旧值，每次都是全新计算）。
      - **`set_by` 的记录方式**：`getattr(current_user, "email", None) or str(current_user.id)`
        ——优先记邮箱（人类可读，便于审计时直接看出是谁改的），没有邮箱（理论上不会发生，
        `User` 模型的 `email` 是 association proxy，但用 `getattr` 兜底更安全）时退回
        `str(current_user.id)`。这与仓库里其它记录「操作者」的既有代码（如
        `enterprise.py:616` 的 `AuditLog(user_id=current_user.id, ...)`）不完全一致——
        那里存的是 UUID 类型的 `user_id` 外键；这里 `set_by` 是 `system_settings.value`
        JSONB 里的一个自由格式字符串字段（不是外键，没有约束要求必须是 UUID），选择存邮箱
        是因为这个字段唯一的消费场景是 GET 端点直接把它显示给管理员看（「这是谁改的」），
        UUID 字符串对人类不友好，而这个字段不需要参与任何 JOIN 或索引查询。
      - **写入后调用 `reset_enforcement_mode_cache()`**：在 `system_setting_dao.set_value(...)`
        之后立即调用，确保同进程内下一次 `current_enforcement_mode()` /
        `current_enforcement_state()` 立即读到新值，不等 30 秒 TTL——这正是测试
        `test_put_endpoint_platform_admin_succeeds_and_next_read_in_process_sees_the_new_value`
        验证的核心行为（该测试特意先「预热」一次缓存到旧值，再验证写入后立即读到新值，
        不是像其它测试那样只验证 API 响应体本身，而是额外验证 `budget.
        current_enforcement_mode()` 这个独立调用路径也立即观察到新值——这才是真正证明
        缓存失效生效，而不是恰好两次都命中了同一个 mock）。
      - **`PUT /system-settings/{key}` 的护栏实现**：在既有的
        `if key == "platform" and not _is_platform_admin_user(current_user): ...` 之后，
        新增一段独立的 `if key == SETTING_ENFORCEMENT_MODE and not
        _is_platform_admin_user(current_user): raise HTTPException(403, detail=...)`——
        选择写成两个独立的 `if` 块而不是合并成一个「多 key 判断」的通用函数，理由是当前
        只有两个需要平台管理员权限的 key，合并成通用机制（例如一个
        `PLATFORM_ADMIN_ONLY_KEYS` 集合）在只有两个成员时收益不明显，反而让每个 key
        各自的错误提示文案（`platform` 的提示是「Only platform admin can modify platform
        settings」，`token_budget_enforcement_mode` 的提示额外附带「改用专用端点」的引导）
        不好独立维护；两个 `if` 块保持了与现有代码风格（本文件里权限检查大量是就地
        `if ... and not _is_platform_admin_user(...): raise HTTPException(...)` 的重复
        写法，例如 `identity-providers` 那几处）一致，不引入新的抽象。detail 文案写明
        「请改用专用端点 PUT /enterprise/token-budget-enforcement」（带上完整的
        `/enterprise` 前缀，与本任务确认的路由挂载方式一致——管理员看到这条提示应该能
        直接照着拼出正确的调用路径，不需要再去确认前缀）。
      - **3.9 Preservation 的落实**：护栏只新增了一个针对
        `key == SETTING_ENFORCEMENT_MODE` 的独立分支，未改动 `key == "platform"` 分支
        与「无匹配任何特殊 key」时的默认「先查后写」逻辑；已用测试
        `test_generic_endpoint_is_unaffected_for_other_keys`（用
        `invitation_code_enabled` 作代表）与
        `test_generic_endpoint_platform_only_guard_for_platform_key_is_unchanged`
        （确认 `platform` key 的既有护栏逐字节不变）钉住。
      - **新增测试文件**：`backend/tests/test_enterprise_token_budget_enforcement.py`（新建，
        13 条测试）。测试替身 `_FakeSettingStore` 是一个用 dict 实现的 `system_setting_dao`
        替代品（只实现 `get_value`/`set_value` 两个被生产代码实际调用的方法），通过辅助函数
        `_patch_store(monkeypatch, store)` **同时**打到 `enterprise.system_setting_dao` 与
        `budget.system_setting_dao`——`enterprise.py` 直接调用 DAO 单例做读写，而
        `current_enforcement_state()`（两个端点内部、以及验证「同进程立即生效」的测试）
        通过 `budget.py` 自己的模块级引用读取同一个单例；只打一侧会让另一侧仍然尝试连接
        真实数据库（测试环境不可达），fail-open 到 `warn_only`，掩盖被测行为——这个坑在
        实现测试的第一轮就踩到并在函数文档字符串里记录了原因，避免以后重蹈。
        - `GET` 端点：3 条（org_admin 可读、grace 生效时的 `grace_active`/`effective_mode`
          组合、`set_by` 缺失时的默认值）。
        - `PUT` 端点：`org_admin → 403`（且断言 store 未被写入，证明护栏在写入前生效）、
          `平台管理员 → 200` 且验证同进程下一次判定立即用新值（核心断言）、
          `grace_until`/`clear_grace` 三种优先级组合各一条（保留旧值 / 显式覆盖 /
          `clear_grace` 优先）、非法 `mode` 触发 Pydantic 校验失败。
        - 通用端点新护栏：`org_admin → 403`（且断言未写入）、`平台管理员 → 200`
          （确认新护栏没有连平台管理员一起拦住）、`其它 key`（`invitation_code_enabled`）
          不受影响、既有 `platform` key 护栏不受影响。
      - **验证结果**：`test_enterprise_token_budget_enforcement.py`：**13 passed**。全量
        `backend/tests/`（**在 `backend/` 目录下运行** `.venv/bin/python -m pytest tests/ -q`，
        理由与前序任务记录一致：系统默认 Python 3.10 没有 `datetime.UTC`，必须用仓库自带的
        `backend/.venv`）：**2465 passed, 3 failed**。失败集合与任务 8.1 记录的基线
        （2452 passed, 3 failed）逐条一致（`test_feishu_card_tools.py` 1 条 +
        `test_html_to_pdf.py` 2 条，均为环境依赖缺失——`libgobject-2.0-0` 系统库缺失导致
        weasyprint 无法加载，与本次改动无关）。与基线相比：失败数不变（3），通过数由 2452
        增至 2465（恰好对应本任务新增的 13 条测试，2452+13=2465，与实测完全对应），
        **无新增意外失败**，3.9 Preservation 得到验证。
      - **未发现需要委派方注意的其它既有缺陷**：`get_system_setting`（通用读端点，
        `GET /system-settings/{key}`）本身未被本任务修改——它对任何 key 都只要求
        `get_current_user`（无特殊权限），任务描述与 design.md 均未要求收紧这个读端点的
        权限（`token_budget_enforcement_mode` 的读权限收口走的是新增的专用
        `GET /enterprise/token-budget-enforcement` 端点，二者并存：通用读端点仍然对任何
        已登录用户可读，这与「读」本身风险较低、且专用端点已经提供了更完整的读取形状
        的设计意图一致，不在本任务范围内收紧）。

  - [x] 8.3 变更 6：模型级 `max_tokens_per_day` 从 API 写面移除（保留数据库列）
    - `LLMModelCreate`（`schemas.py:408`）/ `LLMModelUpdate`（`schemas.py:420`）/
      `LLMModelOut`（`schemas.py:433`）去掉该字段
    - `enterprise.py:441` 与 `enterprise.py:583-584` 两处赋值删除
    - `models/llm.py:59` 列上补注释：「未被任何执行路径读取，保留仅为兼容历史数据；是否 DROP 留给独立清理迁移」
    - **不写迁移、不 DROP COLUMN**（存量库可能有历史值，DROP 不可逆）
    - `LLMModelOut` 是响应模型，去字段会改变响应形状 —— 已确认前端除 `LlmTab.tsx:18` 那行类型声明外无消费点；
      若存在外部 API 消费者，需改为「先保留回显、下个版本再移除」的兼容路径
    - **前置检查项**：本任务的前提是任务 11.2（真实数据里用户配的究竟是哪一档）。
      若复验发现真实数据里只有模型级被填过，本任务的「移除」结论需要重新评估，届时先停下来问用户
    - _Bug_Condition: 1.11（可写入但永不生效的限额字段）_
    - _Expected_Behavior: 2.11_
    - _Preservation: 数据库列与历史值不动；其余 LLM 模型字段的读写行为不变_
    - **环境**：无库可完成（结论依赖 11.2 的复验）
    - **依赖**：无（但合入前需 11.2 的结论）
    - _Requirements: 2.11_
    - **实现记录（2026-08-09）**：
      - **前置检查项未闭环，显式记录风险**：本任务的前提是任务 11.2（复验真实数据里
        用户配置的上限究竟在哪一档），但 11.2 需要真实数据库环境，本地 PostgreSQL /
        Docker 未启动，**无法执行**。按委派方明确指示：requirements 阶段用户已经把
        「四个缺口：run_compact/session+group_compact/planning/model_probe 补判定、
        group_handoff 收敛、模型级字段移除、租户三列补齐」定调为已确认的决策方向
        （不是待确认项），因此不因为 11.2 无法执行而停下来问用户，直接按 design.md
        变更 6 给出的默认方案（「从 API 写面移除、保留数据库列」）执行本任务。
        **风险显式记录**：若后续 11.2 在有库环境复验后发现真实数据里只有模型级
        `LLMModel.max_tokens_per_day` 被填过（Agent 级与租户级均未配置），
        本任务这次移除的结论需要回头重新评估——这个前提未经真实数据验证，只是基于
        design.md 的取舍依据（该字段没有可用的判定语义、前端从未渲染输入框、
        可写面只有直连 API）与用户已确认的决策方向直接实施，不是已经被验证过的结论。
      - **`backend/app/schemas/schemas.py`**：删除三处字段声明——
        `LLMModelCreate`（原第 411 行）、`LLMModelUpdate`（原第 424 行）、
        `LLMModelOut`（原第 439 行）里各自的 `max_tokens_per_day: int | None = None`
        一行，三个模型的其余字段（`provider`/`model`/`api_key`/`base_url`/`label`/
        `temperature`/`enabled`/`supports_vision`/`max_output_tokens`/
        `request_timeout` 等）逐字段未动。核实过本文件里的 `BaseModel` 没有全局
        `model_config = {"extra": "forbid"}` 之类的设置（全仓搜索 `model_config`/
        `ConfigDict`/`class Config` 只找到若干个各响应模型自己声明的
        `model_config = {"from_attributes": True}`，没有一个作用于请求体模型、
        也没有影响 `LLMModelCreate`/`LLMModelUpdate` 的全局配置）——即 Pydantic 走
        默认行为：调用方如果传 `max_tokens_per_day=123` 构造 `LLMModelCreate`/
        `LLMModelUpdate`，这个未声明字段会被**静默忽略**而不是报错。按任务描述第 6
        条的说明，这是一种可以接受的既有行为，未额外补测试验证「字段已被移除」这件事
        本身。
      - **`backend/app/api/enterprise.py`**：删除两处赋值——
        `add_llm_model` 端点里构造 `LLMModel(...)` 时的
        `max_tokens_per_day=data.max_tokens_per_day,` 一行（原第 529 行附近）；
        `update_llm_model` 端点里的
        `if data.max_tokens_per_day is not None: model.max_tokens_per_day =
        data.max_tokens_per_day` 两行（原第 671-672 行附近），`if` 判断与赋值一并
        删除。两个端点其余字段的读写逐行未动（`provider`/`model`/`label`/`api_key`/
        `base_url`/`temperature`/`enabled`/`supports_vision`/`max_output_tokens`/
        `request_timeout` 等）。
      - **`backend/app/models/llm.py`**：在 `max_tokens_per_day: Mapped[int | None] =
        mapped_column(Integer)` 这一行上方补充英文行内注释（与本文件其它字段注释的
        英文风格一致），内容为：「Not read by any execution path (see bugfix
        `token-usage-limit-not-enforced`, change 6): removed from the API write
        surface (LLMModelCreate/Update/Out), retained only for backward compatibility
        with historical values. Whether to DROP this column is left to a separate
        cleanup migration.」列本身的类型（`Integer`，可空）未改动，**未写任何迁移、
        未 DROP COLUMN**。
      - **前端**：确认未改动 `LlmTab.tsx:18`（该行 TS 类型声明的删除是任务 9.3 的
        范围，本任务不涉及）。全仓搜索 `max_tokens_per_day` 后逐条核对：前端命中的
        其它文件（`AgentCreate.tsx`、`SettingsTab.tsx`、`AgentDetailPage.tsx`、
        `Dashboard.tsx`、`types/index.ts`、`apiError.ts`）读写的都是 `Agent.
        max_tokens_per_day`（同名但不同字段，属于 Agent 级限额，与本任务的
        `LLMModel.max_tokens_per_day` 无关），确认前端在企业 LLM 模型管理相关文件里
        除 `LlmTab.tsx:18` 的类型声明外没有其它消费点，与 design.md「已确认前端除那一行
        类型声明之外没有任何消费点」的结论一致。未发现外部 API 消费者的证据（本次审查
        范围内的前端代码库），因此未采用「先保留回显、下个版本再移除」的兼容路径，直接
        一次性移除。
      - **既有测试核查结论：无需修改或删除任何测试**。全仓搜索
        `grep -rn "max_tokens_per_day" backend/tests` 并逐条核对命中结果：
        - `test_token_period_consistency.py`、`test_agent_runtime_session_context_compactor.py`、
          `test_agent_runtime_run_compactor.py`、`test_token_accounting_budget.py`、
          `test_agent_runtime_group_handoff.py`、`test_llm_tool_capability_probe.py`、
          `test_group_handoff_budget_property.py` 等文件里出现的 `max_tokens_per_day`
          全部是 `Agent.max_tokens_per_day` 或 `Tenant.max_tokens_per_day`（同名不同
          字段），与 `LLMModelCreate`/`LLMModelUpdate`/`LLMModelOut`/`LLMModel` 无关，
          不受本次改动影响。
        - 专门搜索 `LLMModelCreate`/`LLMModelUpdate`/`LLMModelOut` 的构造/断言：
          全仓没有任何测试直接构造 `LLMModelCreate`；`LLMModelUpdate` 只在
          `test_llm_tool_capability_probe.py` 里以 `provider=`/`model=`/`base_url=`/
          `api_key=` 四个字段参数化构造（用于验证「修改模型身份字段会失效已有的工具
          调用能力探测结果」），从未传入或断言 `max_tokens_per_day`；`LLMModelOut`
          没有任何测试直接构造或断言其字段。
        - 全仓构造 `LLMModel(...)` ORM 实例的测试（`test_model_capabilities.py`、
          `test_runtime_model_settings_api.py`、`test_agent_runtime_*.py` 系列、
          `test_model_logical_delete.py`、`test_llm_tool_capability_probe.py` 等）
          逐一核查其 `values`/构造参数字典，均不包含 `max_tokens_per_day` 这个 key
          （这些测试关心的是 `provider`/`model`/`label`/`enabled`/`tenant_id`/
          `supports_tool_calling` 等其它字段），字段的默认 `None` 值对这些测试的
          断言逻辑无影响。
        - **结论：没有一条既有测试的核心意图是验证 `max_tokens_per_day` 这个字段的
          读写行为**（不存在「创建模型时 max_tokens_per_day 被正确写入」这类测试需要
          被删除），因此本任务未删除或修改任何测试文件，任务描述第 5 条要求的
          「测试更新/删除」在本次改动中没有对应的目标。
      - **未新增测试**：按任务描述第 6 条的判断依据（`schemas.py` 无全局
        `extra="forbid"` 配置，未声明字段会被静默忽略而非报错，属于可以接受的既有
        Pydantic 默认行为），未补充「确认字段已被移除会被拒绝」的测试。
      - **验证结果**：
        - 目标测试组合跑：`test_llm_tool_capability_probe.py` +
          `test_model_capabilities.py` + `test_runtime_model_settings_api.py` +
          `test_model_logical_delete.py` → **42 passed, 0 failed**。
        - 全量 `backend/tests/`（**在 `backend/` 目录下运行**
          `.venv/bin/python -m pytest tests/ -q`，理由与前序任务记录一致：系统默认
          Python 3.10 没有 `datetime.UTC`，必须用仓库自带的 `backend/.venv`）：
          **2465 passed, 3 failed**——失败集合与任务 8.2 记录的基线逐条一致
          （`test_feishu_card_tools.py` 1 条 + `test_html_to_pdf.py` 2 条，均为环境
          依赖缺失——`libgobject-2.0-0` 系统库缺失导致 weasyprint 无法加载，与本次
          改动无关）。与基线相比：失败数不变（3），通过数不变（2465，因为本任务未
          新增或删除任何测试，只是移除了三个 schema 字段与两处赋值），**无新增
          意外失败**。
      - **未发现需要委派方注意的其它既有缺陷**：`LLMModelOut` 响应形状变化（少了
        一个 `max_tokens_per_day` 字段）经核实不影响 `list_llm_models`/`add_llm_model`/
        `update_llm_model` 三个端点的其它既有行为；`_llm_config_fingerprint`
        （`update_llm_model` 里用于判断配置是否变化、进而失效工具调用探测结果的函数）
        经确认不读取 `max_tokens_per_day`，本次改动不影响它的判定结果。

  - [x] 8.4 变更 7：租户三列的读写接口（`enterprise.py`）
    - `TenantQuotaUpdate` 增加 `max_tokens_per_day` / `default_agent_max_tokens_per_day` /
      `default_agent_max_tokens_per_month`（均 `int | None`）
    - **「不变更」与「显式设为无限制」必须能区分**：现有 PATCH 一律 `if data.x is not None`，
      而这三列的 `None` 恰好是有效值（无限制）。改用 Pydantic v2 的 `data.model_fields_set`：
      key 出现在请求体 → 写入（含 `null` → NULL）；未出现 → 不动。**其余既有字段的处理方式不变**，避免连带改动
    - `GET /tenant-quotas` 返回这三列
    - 测试：三态语义（key 缺失 = 不变、`null` = 无限制、正整数 = 上限）各一条
    - `Tenant.max_tokens_per_day` 是 2.3 判定所读的字段，本任务是让那一档限额「可被配置」的前提
    - _Bug_Condition: 1.12（判定输入字段处于无法配置的状态）_
    - _Expected_Behavior: 2.12_
    - _Preservation: 既有 quota 字段的 PATCH 语义与响应形状不变_
    - **环境**：无库可完成
    - **依赖**：无
    - _Requirements: 2.3, 2.12_
    - **实现记录（2026-08-09）**：
      - **前置读取确认**：完整读了 `backend/app/models/tenant.py` 的 `Tenant` 类——三列已存在
        （`max_tokens_per_day`（约第 38 行，注释「租户日 token 天花板。NULL = 无限。含系统开销
        （群聊压缩 / 规划 / 连通性测试）」）、`default_agent_max_tokens_per_day` / `_month`
        （约第 40-41 行，注释「新建 Agent 时带入的默认 token 限额」）），均
        `Mapped[int | None] = mapped_column(Integer, nullable=True)`，本任务只需给它们加读写接口，
        未改模型、未加迁移。完整读了 `enterprise.py` 里 `TenantQuotaUpdate`（原 857-866 行）、
        `get_tenant_quotas`（原 869-892 行）、`update_tenant_quotas`（原 894-946 行）三处的完整实现，
        确认现有九个字段全部是 `if data.x is not None: tenant.x = data.x` 模式逐字段重复，逐一确认
        未遗漏任何字段。确认了 `pydantic>=2.0.0`（`backend/pyproject.toml:13`），`model_fields_set`
        是 Pydantic v2 API，本项目已有先例——`backend/app/api/groups.py:610-632` 的
        `patch_group`/`PatchGroupIn` 用 `"name" not in body.model_fields_set` /
        `"description" in body.model_fields_set` 判断请求体是否显式提供了某个字段，本任务的用法
        （`"max_tokens_per_day" in data.model_fields_set`）与该先例风格一致（同样是"判断 key 是否
        出现"，不是"判断值是否为 None"）。**这不是本项目第一次引入这个模式**，是复用已有先例。
        全仓搜索 `model_fields_set` 确认除 `groups.py` 外无其它命中（除本 spec 的 design.md/tasks.md
        文本描述外）。
      - **`TenantQuotaUpdate` 的改动**：在九个既有字段之后新增
        `max_tokens_per_day: int | None = None`、`default_agent_max_tokens_per_day: int | None = None`、
        `default_agent_max_tokens_per_month: int | None = None`，并在类内加了一段注释说明这三列的
        `None` 是"有效值（无限制）"而非"未提供"，与九个既有字段的语义不同，指向下面
        `update_tenant_quotas` 里 `model_fields_set` 的用法。**未改动任何既有字段的声明**。
      - **`update_tenant_quotas` 的改动**：在九个既有字段的 `if data.x is not None: ...` 代码块
        （逐行未改，包括 `min_heartbeat_interval_minutes` 那段带 `enforce_heartbeat_floor` 副作用
        的特殊处理）之后，新增三行独立的 `if "<field>" in data.model_fields_set: tenant.<field> =
        data.<field>` —— 每行都直接把 `data.<field>` 的值（可能是 `None`）赋给 `tenant.<field>`，
        `None` 时即写 NULL（显式设为无限制），未出现在请求体里（不在 `model_fields_set` 里）时
        完全不执行赋值语句，保留原值。三行放在 `await db.commit()` 之前，返回值
        `{"message": "Tenant quotas updated", "heartbeat_agents_adjusted": adjusted_count}`
        的形状未改动。
      - **`get_tenant_quotas` 的改动**：在既有九个键的返回字典末尾追加三个键值对
        `"max_tokens_per_day": tenant.max_tokens_per_day`、
        `"default_agent_max_tokens_per_day": tenant.default_agent_max_tokens_per_day`、
        `"default_agent_max_tokens_per_month": tenant.default_agent_max_tokens_per_month`，
        既有九个键逐字段未改。
      - **新增测试文件**：`backend/tests/test_enterprise_tenant_quotas.py`（新建，全仓确认此前
        没有任何测试覆盖 `tenant-quotas` 相关端点）。测试直接调用
        `enterprise.get_tenant_quotas` / `enterprise.update_tenant_quotas` 两个路由函数（绕过依赖
        注入），用一个最小的 `_DB`（`execute()` 固定返回预置的 `Tenant` 替身，`commit()` 记录是否
        被调用）与 `SimpleNamespace` 构造的 `Tenant`/`current_user` 替身，风格与
        `test_enterprise_token_budget_enforcement.py`（任务 8.2 产出）一致。8 条测试：
        1. `test_key_absent_leaves_the_column_unchanged`：只传 `default_message_limit`，断言
           `max_tokens_per_day` 保持调用前的原值（100_000）不变。
        2. `test_explicit_null_clears_an_existing_cap_to_unlimited`：显式传
           `max_tokens_per_day=None`，断言写入后变为 `None`（即使原值是具体数字）。
        3. `test_positive_integer_sets_a_new_cap`：传 `max_tokens_per_day=250_000`，断言写入后
           等于该值。
        4. `test_default_agent_max_tokens_per_day_three_state_semantics` /
           `test_default_agent_max_tokens_per_month_three_state_semantics`：对另外两个字段各自
           验证完整的三态语义（缺失/null/正整数各一次断言，合并进单条测试函数内的三个独立
           `_DB` 调用，而非拆成三条独立测试函数——因为三态本身是一个不可分割的语义整体，且这两个
           字段的验证逻辑与 `max_tokens_per_day` 完全对称，拆开只会增加样板代码）。
        5. `test_existing_field_patch_semantics_and_new_columns_are_unaffected`
           （Preservation）：只传 `default_message_limit=999`，断言该字段被正确写入
           （`tenant.default_message_limit == 999`），且三个新列全部保持调用前原值不变，并断言
           返回值形状（`{"message": ..., "heartbeat_agents_adjusted": 0}`）与改动前逐字段一致。
        6. `test_get_tenant_quotas_includes_the_three_new_columns`：构造设了这三列的 `Tenant`，
           断言响应体包含这三个键值对且值正确，同时断言既有键（`default_message_limit`、
           `max_webhook_rate_ceiling`）仍存在（响应形状不回归）。
        7. `test_get_tenant_quotas_reports_null_when_unset`：三列均为默认 `None` 时，断言响应体
           里三个键的值都是 `None`（而不是被意外转成 0 或缺失该键）。
      - **验证结果**：`test_enterprise_tenant_quotas.py`：**8 passed**。全量 `backend/tests/`
        （**在 `backend/` 目录下运行** `.venv/bin/python -m pytest tests/ -q`，理由与前序任务记录
        一致：系统默认 Python 3.10 没有 `datetime.UTC`，必须用仓库自带的 `backend/.venv`）：
        **2473 passed, 3 failed**——失败集合与任务 8.3 记录的基线（2465 passed, 3 failed）逐条
        一致（`test_feishu_card_tools.py` 1 条 + `test_html_to_pdf.py` 2 条，均为环境依赖缺失，
        与本次改动无关）。与基线相比：失败数不变（3），通过数由 2465 增至 2473（恰好对应本任务
        新增的 8 条测试，2465+8=2473，与实测完全对应），**无新增意外失败**。
      - **未发现需要委派方注意的其它既有缺陷**：`enforce_heartbeat_floor` 的副作用逻辑
        （`min_heartbeat_interval_minutes` 字段专属）未被本次改动触碰；本任务未修改前端
        （`EnterpriseSettings.tsx` 的三列输入框是任务 9.2 的范围，本次未涉及）。

- [x] 9. 批次 G：前端入口

  - [x] 9.1 「Token 限额执行」区块（`EnterpriseSettings.tsx` quotas tab）
    - 显示 `effective_mode`（拦截 / 仅告警）、grace 剩余时间、一个 mode 下拉、
      一个「立即启用拦截」按钮（等价于 `clear_grace: true`）
    - 非平台管理员：只读展示 + 说明文案
    - 文案必须写明「修改后最长 30 秒在全部 worker 生效」，数字与任务 11.6 的实测一致
    - 这是 2.14 的落点之一：让「看板显示已超上限」与「后续请求会被拒绝」在同一屏可互相印证
    - _Expected_Behavior: 2.7 / 2.14_
    - **环境**：无库可完成
    - **依赖**：8.2
    - **实现记录（2026-08-09）**：
      - **前置读取确认**：完整读了任务 8.2 落地的
        `GET/PUT /api/enterprise/token-budget-enforcement` 实现（`backend/app/api/enterprise.py`
        约 1276-1406 行），确认响应形状为
        `{configured_mode, effective_mode, grace_until, grace_active, set_by,
        propagation_seconds}`（`configured_mode`/`effective_mode` 是字符串
        `"enforce"|"warn_only"`，`grace_until` 是 ISO8601 字符串或 `null`，
        `grace_active` 是布尔值，`propagation_seconds` 固定 `30`）；`PUT` 权限要求
        `_is_platform_admin_user`，`GET` 权限只需 `get_current_admin`（org_admin 也能读）。
        同时读了 `LlmTab.tsx` 里 `useAuthStore`/`canManageRuntimeModels` 的写法（含
        org_admin）与 `Layout.tsx`/`AdminCompanies.tsx` 里 `canAccessPlatformSettings`
        的写法（`user?.role === 'platform_admin' || !!(user as any)?.is_platform_admin`，
        不含 org_admin），确认本任务的权限判断必须采用后者——这是一个全平台单值开关，
        与 `LlmTab` 的运行时模型选择（租户级）权限语义不同。
      - **新增 state 与数据流**：`EnterpriseSettings.tsx` 顶部新增
        `const currentUser = useAuthStore((s) => s.user)` 与
        `isPlatformAdmin = currentUser?.role === 'platform_admin' ||
        !!(currentUser as any)?.is_platform_admin`（未含 org_admin，代码内联注释写明
        原因，对照 `LlmTab.tsx` 的 `canManageRuntimeModels` 显式指出这里"更严格"）。
        新增四组独立 state（未混入既有的 `quotaForm`，因为响应形状完全不同，与任务描述的
        要求一致）：`tokenBudgetEnforcement`（`TokenBudgetEnforcementState | null`，初值
        `null`，用于区分"尚未加载完成"与"已加载"两个状态，加载完成前 UI 显示
        `common.loading` 占位文案）、`pendingMode`（mode 下拉的临时编辑态，初值
        `'enforce'`，`GET` 响应回来后立即同步为 `effective_mode`）、`tokenBudgetSaving`/
        `tokenBudgetSaved`（保存中/保存成功提示，风格与既有 `quotaSaving`/`quotaSaved`
        逐一对应）。`loadTokenBudgetEnforcement()` 函数封装了 `GET` 请求，在
        `activeTab === 'quotas'` 的 `useEffect` 里调用（与既有 `quotaForm` 的拉取
        `useEffect` 完全同构，但是独立的一个 `useEffect`，不合并进同一个回调——两者
        请求不同的端点，合并只会增加耦合而无收益）。`saveTokenBudgetEnforcement(overrides)`
        接受 `{mode, clear_grace?}`，调 `PUT` 后用响应体（与 GET 同形状）直接刷新
        `tokenBudgetEnforcement` 与 `pendingMode`——不需要在保存成功后再发一次 GET，
        因为 PUT 的响应体已经是刷新后的完整状态（已核对后端实现：PUT handler 最后调用
        `current_enforcement_state()` 重新算一遍再返回，不是回显请求体）。
      - **两个交互点的具体调用**："保存"按钮调
        `saveTokenBudgetEnforcement({ mode: pendingMode })`（不传 `clear_grace`，即不清除
        grace，与任务描述"不清除 grace"的要求一致）；"立即启用拦截"按钮调
        `saveTokenBudgetEnforcement({ mode: 'enforce', clear_grace: true })`（无论
        `pendingMode` 当前是什么，语义都是"立刻结束 grace 窗口并确保处于拦截模式"，
        与后端 `clear_grace=True` 优先级最高、直接忽略同时传入的其它字段的实现一致）。
      - **grace 剩余时间的计算**：新增 `formatGraceRemaining(graceUntil)` 纯函数——
        `grace_active` 为真时，用 `new Date(grace_until).getTime() - Date.now()` 算剩余
        毫秒数，`> 0` 才格式化（`<= 0` 或解析失败时返回 `null`，UI 层用
        `?? t('...graceEndingSoon', '即将结束')` 兜底，理由：`grace_active` 是后端在
        GET 响应那一刻算出的快照，前端渲染时可能已经过去几秒到几十秒（页面停留期间不做
        定时器刷新——这是最小实现，抓的是"管理员点进这个 tab 时看到的状态"，不是一个
        实时倒计时组件，任务描述未要求实时刷新）；天数 > 0 时显示"剩余 X 天 Y 小时"，
        否则显示"剩余 X 小时 Y 分钟"，与任务描述给出的示例格式一致。
      - **UI 结构与交互设计**：新增区块作为 quotas tab 里独立的
        `<div className="card">`，放在现有配额卡片**上方**（选择"独立卡片 + 置顶"而非
        "并入现有卡片的一个分组"：任务描述里两个选项都提到了，选独立卡片的理由是——
        这个区块的数据源、权限判断、保存动作都完全独立于 `quotaForm`，与现有卡片内部
        `conversationLimits`/`agentLimits`/`system`/`triggerLimits` 四个分组"共享同一份
        `quotaForm`+同一个保存按钮"的结构不同，硬塞进去会让"保存"按钮的语义变得模糊——
        点一次保存到底保存了哪些字段；独立卡片可以有自己独立的保存按钮，交互更清晰）。
        卡片内部结构：先用与既有分组标题一致的
        `<div style={{ fontSize: '12px', fontWeight: 600, ... }}>` 小标题
        「Token 限额执行」；未加载完成时显示 loading 占位；加载完成后先展示两个只读信息点
        （当前生效模式、grace 剩余时间，用 flex 布局并排展示，字段标签用
        `fontSize: '11px', color: 'var(--text-tertiary)'`、值用
        `fontSize: '14px', fontWeight: 600`，风格参考了本文件其它地方"标签 + 粗体值"
        的展示模式而非严格复用 `form-group`——因为这两项是只读展示不是输入框）；
        平台管理员再往下看到 mode 下拉（`<select className="form-input">`，选项
        `enforce`/`warn_only`，展示文案「拦截」/「仅告警」）+ 保存按钮 + 「立即启用拦截」
        按钮（`btn btn-secondary`，与主保存按钮的 `btn btn-primary` 区分视觉主次）+
        保存成功提示（复用 `IconCheck` + `var(--success)`，与既有 `quotaSaved` 提示逐字段
        一致的风格）；「修改后最长 N 秒生效」的文案固定跟在按钮行下方，
        `fontSize: '11px', color: 'var(--text-tertiary)'`，与既有输入框下方说明文案的
        字号/颜色一致。**非平台管理员**：按任务描述"倾向于不显示可交互元素，只展示当前
        状态的文本 + 一句说明"的建议，直接不渲染下拉框和两个按钮，只展示上面两个只读信息点
        + 一句「仅平台管理员可修改此设置。」（未采用"显示但 disabled"，理由：disabled
        的下拉框仍然会让非管理员看到一个不能操作的表单控件，容易让人误以为是加载失败或
        权限判断出了 bug，纯文本说明更明确地传达"这不是给你用的"）。
      - **i18n 文案的处理方式**：先搜索本文件 i18n 翻译文件的位置——
        `frontend/src/i18n/{zh,en}.json`（`i18n/index.ts` 里
        `resources: { zh: { translation: zh }, en: { translation: en } }`），确认存在
        专门的 locale 文件（不是"找不到就直接用 fallback"的情形），因此在
        `enterprise.quotas` 命名空间下新增了一个 `tokenBudgetEnforcement` 子对象，
        中英文各补了 14 个 key（`title`/`effectiveMode`/`modeEnforce`/`modeWarnOnly`/
        `graceWindow`/`graceInactive`/`graceEndingSoon`/`graceRemainingDays`/
        `graceRemainingHours`/`modeLabel`/`enforceNow`/`propagationHint`/
        `readOnlyHint`）——两个文件的 key 集合与嵌套结构逐一对应，插入位置紧跟在
        `"quotas": {` 开括号之后（即该命名空间的第一个子键），未打乱既有 key 的顺序。
        代码里全部调用都用 `t('enterprise.quotas.tokenBudgetEnforcement.xxx', '默认文案')`
        这种带 fallback 参数的写法，与本文件其它地方（如
        `t('enterprise.llm.deleteConfirm', 'Disable {{name}}? ...', { name: ... })`）
        的写法风格一致；`propagationHint`/`graceRemainingDays`/`graceRemainingHours`
        三个用了 `{{变量}}` 插值（`seconds`/`days`+`hours`/`hours`+`minutes`），与
        `LlmTab.tsx` 里 `deleteConfirm` 的 `{{name}}` 插值用法同构。「拦截」/「仅告警」/
        「修改后最长 30 秒在全部 worker 生效」等必须包含的文案均已落在
        `modeEnforce`/`modeWarnOnly`/`propagationHint` 三个 key 里，`propagationHint`
        的秒数来自 `tokenBudgetEnforcement.propagation_seconds ?? 30`（未加载完成或该
        字段异常缺失时的 fallback 默认值是 `30`，与任务描述"数字必须来自后端返回的
        `propagation_seconds` 字段，不要硬编码"的要求一致——只有在数据还没到达时才用
        `30` 兜底渲染，一旦数据到达就完全跟随后端返回值）。
      - **未改动**：`quotaForm`/`saveQuotas`/既有 quotas 卡片的任何字段或逻辑；
        `LlmTab.tsx`/`Layout.tsx` 等参考文件；后端任何代码（本任务是纯前端 UI 层）。
      - **未新增测试**：核实本仓库前端测试基础设施——`frontend/package.json` 的
        `"test"` 脚本是 `node --test tests/*.test.mjs`（Node 内置 test runner，
        `frontend/tests/` 目录下的 `.test.mjs` 文件），未发现任何 React 组件渲染测试
        框架（无 `@testing-library/react`、无 `vitest`、无 `jest` 依赖）。按任务描述
        "如果没有或很薄弱，不要为了这一个任务新增测试框架"的指引，以及委派方任务列表里
        9.2/9.3 同样未要求前端单元测试的先例，本任务未新增任何测试。
      - **验证结果**：
        - `npx tsc --noEmit`（`frontend/package.json` 的 `build` 脚本
          `tsc && vite build` 里编译检查的那一步，独立跑以避免完整 `vite build` 的
          额外耗时）：**无任何输出，退出码 0**，确认新增代码无类型错误。
        - `node -e "JSON.parse(...)"` 分别校验 `src/i18n/zh.json` 与 `src/i18n/en.json`：
          均解析成功，确认新增的 JSON 片段未破坏文件语法。
        - IDE 诊断工具对三个改动文件（`EnterpriseSettings.tsx`/`zh.json`/`en.json`）
          均报告"No diagnostics found"。
        - 未运行完整 `npm run build`（`vite build`）：`tsc --noEmit` 已覆盖类型检查，
          且 `vite build` 会产出构建artifact、耗时更长，按任务描述"类型检查通过即可，
          不强制要求完整构建"的说明，未额外执行。
      - **需要委派方注意的一点**：任务描述提到「grace 剩余时间...数字要与任务 11.6
        的实测一致」——本任务的 `propagationHint` 文案已经做到"数字跟随后端字段自动变化"，
        不依赖硬编码，所以即使任务 11.6（需要数据库/部署环境）后续实测出 30 秒这个 TTL
        在真实多 worker 部署下有偏差，也只需要调整后端 `propagation_seconds`
        返回值或 `budget._MODE_TTL_SECONDS`，前端不需要任何改动——这是本任务在设计
        `propagationHint` 时特意做的、比"硬编码文案"更省心的处理，供委派方在推进批次 I
        时知悉。
    - _Requirements: 2.7, 2.14_

  - [x] 9.2 租户三列输入框（`EnterpriseSettings.tsx`）
    - `quotaForm` 增加三个字段，初值 `null`；三个数字输入框，空值 → `null`（无限制），
      非正数一律转 null（与 Agent 设置页 `toPositiveIntOrNull` 一致）
    - 因此 `0 = 禁止一切用量`（3.2）仍只能通过 API 设置，前端不提供 ——
      与 Agent 级限额今天的口径完全一致，不引入新的不一致
    - `max_tokens_per_day` 标注「含系统开销（群聊压缩 / 规划 / 连通性测试）」，与 `models/tenant.py:38` 注释一致
    - **环境**：无库可完成
    - **依赖**：8.4
    - _Requirements: 2.12, 3.2_
    - **实现记录（2026-08-09）**：
      - **前置读取确认**：完整读了 `AgentDetailPage.tsx`（约 306-317 行）里
        `toPositiveIntOrNull` 及其上方的文档字符串，读了任务 8.4 落地的
        `GET/PATCH /enterprise/tenant-quotas`（`enterprise.py`，`TenantQuotaUpdate` 三态
        语义 + `model_fields_set`）实现，读了 `models/tenant.py:38` 三列的注释，读了
        `EnterpriseSettings.tsx` 现有 `quotaForm`/`saveQuotas`/Quotas tab 渲染区域以及
        任务 9.1 已落地的独立「Token 限额执行」卡片（确认两者数据源、保存方式完全不同，
        本任务不往那个卡片里塞任何内容）。
      - **`toPositiveIntOrNull` 的处理方式：本地复制，不导出复用**。理由：
        `AgentDetailPage.tsx` 是一个体量很大、带有大量顶层副作用式代码（路由数据加载、
        WebSocket 连接等）的页面组件文件，从中 `export` 一个函数并在
        `EnterpriseSettings.tsx` 里 `import` 会在两个本来互相独立的企业管理页面之间引入
        一条模块依赖边；而本次改动的定性是"纯 UI 层"的字段/输入框新增（design.md 变更 7
        的前端配套），复制一份同样逻辑的 4 行纯函数，成本远低于引入跨页面耦合的收益。
        在 `EnterpriseSettings.tsx` 顶部（`export default function EnterpriseSettings()`
        之前）新增了一份逐字复制的 `toPositiveIntOrNull`，并在函数上方写了一段说明，
        指出这是有意的本地复制而非导出复用，理由与本条记录一致。
      - **`quotaForm` 的类型标注**：现有 `quotaForm` 用 `useState({...})` 让 TS 从初始值
        推断类型，九个既有字段全是纯数字/字符串字面量。新增的三个字段初值为 `null`，如果
        不显式标注类型，TS 会把 `useState({..., max_tokens_per_day: null, ...})` 推断成
        `max_tokens_per_day: null`（字面量 `null` 类型，不接受赋值为 `number`），后续
        `setQuotaForm({ ...quotaForm, max_tokens_per_day: 250000 })` 会报类型错误。
        采用了显式泛型标注的写法（`useState<{...}>({...})`，把全部 12 个字段的类型都列出
        来），而不是 `AgentDetailPage.tsx` 里 `max_tokens_per_day: '' as string | number`
        这种单字段 `as` 断言的写法——理由：`quotaForm` 是一个 state 对象整体标注一次比
        给每个新增字段单独 `as` 断言更清晰，且 `useState<T>(...)` 是比 `as` 断言更常规的
        TS 模式（不依赖调用点断言，类型来源单一、后续任何地方读写这个 state 都能拿到正确的
        类型提示）；三个新字段类型为 `number | null`，其余九个字段类型从原有初始值推断
        （`number`/`string`）逐字段列出。已加内联注释说明这三个字段的 `null` 是"有效值
        （无限制）"而不是"未加载完成"，与该 state 里其余字段的语义不同。
      - **`useEffect` 拉取 `GET /enterprise/tenant-quotas` 后的合并逻辑：确认不需要改动**。
        `fetchJson<any>('/enterprise/tenant-quotas').then(d => { if (d && Object.keys(d).length)
        setQuotaForm(f => ({ ...f, ...d })); })` 是一个无差别地用响应体键覆盖现有 state 键
        的写法，不区分字段名单——只要响应体里带这个键，就会覆盖 `quotaForm` 里的同名字段。
        任务 8.4 的 `GET /tenant-quotas` 已经在返回字典末尾加入了这三个键
        （`max_tokens_per_day`/`default_agent_max_tokens_per_day`/
        `default_agent_max_tokens_per_month`，值为整数或 `null`），所以首次拉取后这三个
        字段会被后端返回的真实值（或 `null`）覆盖掉 state 初值 `null`——覆盖前后语义一致
        （都是"没有值就是 null"），不存在这段代码需要特殊处理三个新字段的情形。未修改这段
        代码。
      - **`saveQuotas` 确认不需要改动**：`await fetchJson('/enterprise/tenant-quotas',
        { method: 'PATCH', body: JSON.stringify(quotaForm) })` 把整个 `quotaForm`
        对象序列化发送，三个新字段加入 `quotaForm` 后自动随每次保存请求一起被发送
        （包括值为 `null` 的情况），这正好触发后端 `model_fields_set` 三态语义的第二档
        （"key 存在且为 null → 写 NULL"），与本任务"空值 → null（无限制）"的要求吻合，
        不需要额外的"决定是否发送这个 key"逻辑。未修改 `saveQuotas`。
      - **三个输入框的位置与实现**：新增在既有 Quotas 卡片内部（不是任务 9.1 那个独立的
        「Token 限额执行」卡片），紧跟在既有的"Trigger Limits"分组之后、保存按钮之前，
        新建了一个独立分组标题「Token 限额」（`enterprise.quotas.tokenLimits`），三栏
        `grid` 布局，风格与既有分组（`form-group` + `form-label` + `form-input
        type="number"` + 说明文案 `<div>`）逐字段一致。三个输入框：
        - `max_tokens_per_day`（企业每日 Token 上限）：`value={quotaForm.max_tokens_per_day
          ?? ''}`，`onChange` 调 `toPositiveIntOrNull(e.target.value)` 后写入
          `quotaForm`；说明文案「含系统开销（群聊压缩 / 规划 / 连通性测试）。留空表示
          无限制。」——与 `models/tenant.py:38` 的注释「租户日 token 天花板。NULL = 无限。
          含系统开销（群聊压缩 / 规划 / 连通性测试）。」核心信息点逐字对应。
        - `default_agent_max_tokens_per_day` / `default_agent_max_tokens_per_month`
          （数字员工默认每日/每月 Token 上限）：同样的 `toPositiveIntOrNull` 转换逻辑；
          说明文案「新建数字员工时带入的默认每日/每月 Token 限额。留空表示无限制。」——
          对应 `models/tenant.py` 里「新建 Agent 时带入的默认 token 限额」的描述（本仓库
          UI 文案统一把 Agent 称为"数字员工"，与既有 quotas 卡片里 `agentLimits`/
          `maxAgents` 等既有翻译用词一致）。
      - **`null` 值的显示处理**：三个输入框的 `value` 属性均写成
        `quotaForm.<field> ?? ''`（而不是直接 `quotaForm.<field>`），确保 `null` 时
        `<input>` 收到空字符串而不是字面上的 `null`，避免 React 把 `value={null}`
        当作未受控输入处理并报 warning。这与既有九个数字输入框的写法
        （`value={quotaForm.default_message_limit}`，那些字段永远是 `number`，不需要
        `?? ''`）不同，是本任务新增字段特有的处理。
      - **`toPositiveIntOrNull('')` 对空字符串的处理逐一验证**：`Number('')` 结果是 `0`，
        `Number.isFinite(0)` 为 `true`，但 `0 > 0` 为 `false`，所以三元表达式整体结果是
        `null`——已在浏览器 console 与 Node REPL 各自验证过这条链路（`Number('')` → `0`，
        `Number.isFinite(0)` → `true`，`0 > 0` → `false`），确认空字符串输入确实按预期
        收敛为 `null`，不是假设。非正数（`'0'`、`'-5'`）与非数字字符串（纯空白、
        非法字符）同样验证收敛为 `null`（与 `AgentDetailPage.tsx` 同名函数完全一致的行为，
        因为是逐字复制）。
      - **未新增测试**：与任务 9.1 一致的理由——本仓库前端无组件渲染测试框架
        （`frontend/package.json` 的 `"test"` 脚本是 Node 内置 test runner 跑
        `tests/*.test.mjs`，未发现 `@testing-library/react`/`vitest`/`jest` 等组件渲染
        测试依赖），未为这一个任务新增测试框架。
      - **验证结果**：
        - `npx tsc --noEmit`（`frontend` 目录下）：**无任何输出，退出码 0**，确认新增的
          `quotaForm` 类型标注、三个输入框、`toPositiveIntOrNull` 本地函数均无类型错误。
        - `node -e "JSON.parse(...)"` 分别校验 `src/i18n/zh.json` 与 `src/i18n/en.json`：
          均解析成功，确认新增的 10 个翻译 key（每个语言文件各 7 个新 key：
          `tokenLimits`/`tenantMaxTokensPerDay`/`tenantMaxTokensPerDayDesc`/
          `defaultAgentMaxTokensPerDay`/`defaultAgentMaxTokensPerDayDesc`/
          `defaultAgentMaxTokensPerMonth`/`defaultAgentMaxTokensPerMonthDesc`）未破坏
          文件语法。
        - IDE 诊断工具对三个改动文件（`EnterpriseSettings.tsx`/`zh.json`/`en.json`）
          均报告"No diagnostics found"。
        - 未运行完整 `npm run build`（`vite build`）：与任务 9.1 一致的判断依据——
          `tsc --noEmit` 已覆盖类型检查，`vite build` 会产出构建 artifact、耗时更长，
          类型检查通过即可，不强制要求完整构建。
      - **未偏离任务描述的地方**：`0 = 禁止一切用量`（3.2）仍只能通过 API 设置，前端
        三个输入框的 `toPositiveIntOrNull` 转换逐点验证过不会产出 `0`（非正数一律转
        `null`），与 Agent 级限额今天的口径完全一致，未引入新的不一致。
      - **未发现需要委派方注意的既有缺陷**：任务 9.1 的独立卡片与本任务的三个输入框
        经核对未产生任何数据/状态交叉（`tokenBudgetEnforcement`/`pendingMode` 与
        `quotaForm` 是两组完全独立的 state），也未发现 `saveQuotas`/`GET tenant-quotas`
        既有逻辑存在需要额外处理的边界情况。

  - [x] 9.3 删除 `LlmTab.tsx:18` 的 `max_tokens_per_day` TS 字段声明
    - 与 8.3 的响应形状收窄配套；确认 `grep -rn "max_tokens_per_day" frontend/src` 在企业 LLM 相关文件里无残留引用
    - **环境**：无库可完成
    - **依赖**：8.3
    - _Requirements: 2.11_
    - **实现记录（2026-08-09）**：
      - **实际删除内容**：完整读了 `LlmTab.tsx` 全文（636 行）后确认，`interface LLMModel`
        里 `max_tokens_per_day?: number;` 这一行当前位于第 18 行（未漂移，与 requirements
        1.11 更正说明、design.md 变更 6 里写的行号一致），紧邻在
        `api_key_masked?: string;` 之后、`enabled: boolean;` 之前。删除的只有这一行本身，
        `LLMModel` 接口其余全部字段（`id`/`provider`/`model`/`label`/`base_url`/
        `api_key_masked`/`enabled`/`supports_vision`/`supports_tool_calling`/
        `tool_calling_capability_source`/`tool_calling_checked_at`/`tool_calling_error`/
        `max_output_tokens`/`request_timeout`/`temperature`/`created_at`）逐行未动。
      - **额外读取点排查结果：未发现**。用 `grep -n "max_tokens"`（不加 `_per_day` 限定，
        避免漏掉变量名不完全匹配但语义相关的引用）对本文件全文搜索，命中的全部是与本任务
        无关的其它字段——`LLMProviderSpec.default_max_tokens`（供应商规格里的默认输出
        上限，用于新增模型时预填 `max_output_tokens` 表单字段）与
        `modelForm.max_output_tokens` 相关代码（新增/编辑模型表单里的"最大输出 Token"
        输入框及其说明文案）。这两者是完全独立的字段（模型的输出长度上限，不是"每日
        Token 用量上限"），命名相似但语义不同，未受本次删除影响。**本文件内没有任何代码
        读取 `model.max_tokens_per_day`**：`models.map((m) => ...)` 渲染模型列表的代码块
        （及编辑表单预填逻辑）里没有任何地方引用这个字段——这与 bugfix.md 1.11 更正说明
        「模型新增/编辑表单里没有对应输入框」的结论完全吻合。删除字段声明后运行的
        `tsc --noEmit`（见下方验证结果）确认了这一点：没有产生任何
        `Property 'max_tokens_per_day' does not exist on type 'LLMModel'` 类型的错误，
        证明确实不存在需要一并处理的读取点。
      - **全仓复核 `grep -rn "max_tokens_per_day" frontend/src` 结果**（改动后重新执行，
        逐条确认）：
        - `frontend/src/pages/enterprise-settings/tabs/LlmTab.tsx`：**零命中**（本任务的
          目标行已删除，且确认本文件内没有第二处引用）。
        - `frontend/src/types/index.ts:43`：`Agent` 接口里的字段声明（紧邻
          `input_tokens_total`/`max_tokens_per_month`/`heartbeat_enabled` 之间，已逐行
          核对该接口定义，确认属于 `Agent`，与 `LLMModel` 无关）。
        - `frontend/src/pages/agent-detail/tabs/SettingsTab.tsx`（2 处）、
          `frontend/src/pages/agent-detail/AgentDetailPage.tsx`（5 处）、
          `frontend/src/pages/Dashboard.tsx`（1 处）、
          `frontend/src/pages/AgentCreate.tsx`（7 处）：均是 `Agent.max_tokens_per_day`
          （Agent 级每日限额的表单/展示/校验逻辑），与本任务要删除的
          `LLMModel.max_tokens_per_day` 完全无关，**未触碰**。
        - `frontend/src/pages/EnterpriseSettings.tsx`（8 处）：均是任务 9.2 新增的
          `Tenant.max_tokens_per_day`（企业每日 Token 上限的表单字段/输入框），与本任务
          无关，**未触碰**。
        - `frontend/src/services/apiError.ts:32`：`max_tokens_per_day: '每日 Token 上限'`
          ——通用的"字段名 → 中文标签"映射表条目，服务于 `Agent`/`Tenant` 的同名字段在
          校验错误场景下的文案翻译（这两个字段仍然存在，映射条目仍然有效），不属于
          `LLMModel` 这个已删除的字段，**未删除**。
        - **结论：改动后的全仓复核结果与任务描述"已确认信息"逐条一致**，除
          `LlmTab.tsx` 本身归零外，其余命中全部确认是 `Agent`/`Tenant` 字段或通用映射表，
          均未被误删或误改。
      - **验证结果**：
        - `npx tsc --noEmit`（`frontend` 目录下）：**无任何输出，退出码 0**——确认删除
          这一行字段声明后没有任何代码因为读取已删除字段而报类型错误，印证了上面"额外
          读取点排查结果：未发现"的结论不是假设，而是经过编译器验证的事实。
        - `get_diagnostics` 对 `LlmTab.tsx`：报告 "No diagnostics found"。
      - **未新增测试**：与任务 9.1/9.2 一致的理由——本仓库前端无组件渲染测试框架
        （`frontend/package.json` 的 `"test"` 脚本是 Node 内置 test runner 跑
        `tests/*.test.mjs`，未发现 `@testing-library/react`/`vitest`/`jest` 等组件渲染
        测试依赖），未为这一个任务新增测试框架。
      - **未发现需要委派方注意的额外发现**：本任务范围内（`LlmTab.tsx` 单文件、单行
        删除）没有发现任何超出任务描述预期的情况——字段声明位置与预期行号一致、没有
        隐藏的读取点、全仓复核结果与已知信息完全吻合。

- [ ] 10. 批次 H：默认值迁移（**需要数据库环境**）

  - [~] 10.1 前置检查：用 `alembic heads` 确认 `down_revision`
    - **必须在真实库上执行 `alembic heads` 与 `alembic history`**，不能沿用静态扫描的结论
    - design 的静态扫描显示 `token_accounting_v2` 是一个 head，同时列出了另外几个未合并 head
      （`merge_v193_creds_focus`、`perf_indexes` 等），所以 design 里写的
      `down_revision = "token_accounting_v2"` 只是待确认的默认值，不能直接用
    - 若确认存在多 head，把新迁移改为 merge 迁移，或按仓库既有的 merge 惯例先合 head
    - 记录实测输出（heads 列表与最终选定的 `down_revision`）到本文件末尾的「复验记录」
    - **环境**：需要数据库环境
    - **依赖**：无（但阻塞 10.2）
    - _Requirements: 2.5_

  - [~] 10.2 编写迁移 `backend/alembic/versions/2026xxxxxxxx_token_budget_enforce_default.py`
    - upgrade：`UPDATE system_settings SET value = jsonb_build_object('mode','enforce','grace_until',(now() + interval '7 days')::text,'set_by','migration_token_budget_enforce_default') WHERE key='token_budget_enforcement_mode' AND value = '{"mode":"warn_only"}'::jsonb;`
    - 只改写**与旧迁移写入的形状逐字节相同**的行；被管理员改过的行保持不动
    - 已知缺口：管理员若恰好用 `{"mode": "warn_only"}` 这个精确形状显式设置过，会被误改写 ——
      grace 窗口 + 任务 8.2 的入口是这种情况的补救路径，部署说明里要写明
    - **不 `INSERT`**：全新安装由代码默认值（`enforce`、无 grace）覆盖，「未显式配置即拦截」在新环境立即成立，
      老环境有 7 天缓冲
    - downgrade：把 `set_by = 'migration_token_budget_enforce_default'` 的行改回 `{"mode": "warn_only"}`，不动其他行
    - _Bug_Condition: 1.5（存量行是 `warn_only`，仅改代码默认值对老环境无效 —— `ON CONFLICT DO NOTHING`）_
    - _Expected_Behavior: 2.5_
    - _Preservation: 不属于本次改写目标的 `system_settings` 行一行不动_
    - **环境**：需要数据库环境
    - **依赖**：10.1, 3.4
    - _Requirements: 2.5_

  - [~] 10.3 部署清单补一步灰度通知
    - 走既有的 `system_settings.notification_bar`（`GET /enterprise/system-settings/notification_bar/public`
      已存在且免鉴权、前端已消费），不新建通知基础设施
    - 内容：「token 限额将于 X 日起真正拦截，请按新口径（含缓存与思考 token）复核已有上限值」
    - grace 窗口承担「误拦保护」的实质职责，通知只承担「告知」
    - **环境**：需要数据库环境（部署环境）
    - **依赖**：10.2
    - _Requirements: 2.5_

- [ ] 11. 批次 I：design「需要在有库环境复验的结论」6 条（**需要数据库环境**）
  - 本地 PostgreSQL 未启动（Docker daemon 未运行）时这一整批都做不了；
    结论逐条回写到本文件末尾的「复验记录」，与 design.md 对应条目同步

  - [~] 11.1 复验 `system_settings.token_budget_enforcement_mode` 的实际值与形状
    - `SELECT key, value FROM system_settings WHERE key = 'token_budget_enforcement_mode';`
    - 任务 10.2 的 `WHERE value = '{"mode":"warn_only"}'::jsonb` 依赖它逐字节等于旧迁移写入的形状；
      若实际是别的形状（例如带额外键），存量行不会被改写，需要人工处理并在迁移里补一条针对该形状的分支
    - 这条同时结掉 requirements 1.14 的前半段「待验证」
    - **环境**：需要数据库环境
    - **依赖**：无
    - _Requirements: 1.5, 1.14, 2.5_

  - [~] 11.2 复验用户配置的上限究竟在哪一档
    - 查 `agents.max_tokens_per_day/_month`、`tenants.max_tokens_per_day`、`llm_models.max_tokens_per_day`
      三处的非空值分布
    - **这是任务 8.3 的前置检查项**：若真实数据里只有模型级被填过，变更 6 的「移除」结论需要重新评估，
      届时先停下来问用户，不要按当前结论直接改
    - 这条同时结掉 requirements 1.14 的后半段「待验证」
    - **环境**：需要数据库环境
    - **依赖**：无
    - _Requirements: 1.11, 1.14, 2.11_

  - [~] 11.3 复验迁移链 head（并入任务 10.1 执行，此处只登记结论）
    - `alembic heads` / `alembic history` 的实测输出与最终选定的 `down_revision`
    - **环境**：需要数据库环境
    - **依赖**：10.1
    - _Requirements: 2.5_

  - [~] 11.4 复验 `jsonb` 比较与 `jsonb_build_object` 行为 + upgrade/downgrade 往返
    - `value = '{"mode":"warn_only"}'::jsonb` 的键序无关性依赖 jsonb 语义（理论成立），仍需真实库跑一次
    - 往返实测：upgrade 后存量 `warn_only` 行被改写为 `enforce` + grace；
      downgrade 后仅 `set_by = 'migration_token_budget_enforce_default'` 的行回到 `{"mode": "warn_only"}`；
      被管理员改过形状的行两个方向都不动
    - **环境**：需要数据库环境
    - **依赖**：10.2
    - _Requirements: 2.5_

  - [~] 11.5 复验记账侧未受影响（3.5 / 3.11）
    - 本次不写 `daily_token_usage`，理论上两条部分唯一索引无影响
    - 在有库环境跑一遍依赖真实 DB 的记账测试（含 `test_token_accounting_schema.py` 这类需要库的用例），确认全绿
    - 顺带确认任务 8.1 的 upsert 在真实库上的冲突行为符合预期
    - **环境**：需要数据库环境
    - **依赖**：3–8 全部完成
    - _Requirements: 3.5, 3.11_

  - [~] 11.6 实测多 worker 下模式改动的生效时延
    - 在部署形态（gunicorn / uvicorn 多进程）下实测 30 秒 TTL 的实际表现
    - 实测值必须与任务 9.1 的 UI 文案数字一致；若实测显著偏离 30 秒，改文案或改 TTL，二者取其一
    - **环境**：需要数据库环境（部署环境）
    - **依赖**：3.3, 8.2, 9.1
    - _Requirements: 2.7, 3.3_

- [x] 12. 全链路集成测试（无库部分）
  - **直接对话全链路**：超限 Agent → `RuntimeModelStepService.complete_once` → `node_executor._model`
    → lifecycle `failed/token_budget_exceeded` → `delivery_from_checkpoint` → `_safe_failure_content`
    渲染出的用户可见文本包含 blocked_scope / used / limit / reset_at 四项
  - **触发器链路等价性**：同一 Agent 由 `source_type = trigger` 的 Run 驱动，断言 lifecycle 的 reason
    与直接对话完全一致；测试要把「二者共用同一个 `RuntimeModelStepService` 实例」这个关系钉住，防止未来分叉
  - **压缩 → 业务步的顺序**：Agent 未超限但压缩会把它推过上限时，断言压缩节点先被拦截，
    而不是压缩成功后业务步才失败
  - **群聊 handoff**：超限目标 → `preflight_group_agent_handoff` 抛
    `group_handoff_budget_unavailable(repairable=True)`；模型收到 repair 指令而不是 Run 失败
  - **执行模式切换生效**：`PUT /token-budget-enforcement` 从 `warn_only` 切到 `enforce` 后，
    同进程内下一次判定立即用新值（缓存被显式失效）
  - **grace 窗口**：`configured_mode=enforce` + `grace_until` 在未来 → 超限请求放行且落
    `token_budget_enforcement_grace_active` 日志；`clear_grace: true` 之后同一请求被拦截
  - **环境**：无库可完成
  - **依赖**：3–9 全部完成
  - _Requirements: 2.1, 2.4, 2.7, 2.8, 2.13, 3.9_
  - **实现记录（2026-08-09）**：
    - **既有覆盖核对结果**：在写任何新测试之前，先逐条核对了本任务描述的六个场景
      是否已被任务 3-9 交付的测试间接覆盖。结论：`gate.check()` 本身（allowed/
      blocked/两级异常/软告警去重）已被 `test_token_accounting_gate.py` 完整覆盖；
      五条链路（`run_compact`/`session_compact`/`group_compact`/`planning`/
      `model_probe`/`group_handoff`）各自接入闸门后的单点行为已分别被
      `test_agent_runtime_run_compactor.py`、`test_agent_runtime_session_context_compactor.py`、
      `test_agent_runtime_planning.py`、`test_llm_tool_capability_probe.py`、
      `test_agent_runtime_group_handoff.py` / `test_group_handoff_budget_property.py`
      覆盖；`node_executor._compact`/`node_executor._model` 对 `token_budget_exceeded`
      的既有错误码传播逻辑已被任务 6.5 新增的两条测试覆盖；`_safe_failure_content`
      渲染 `budget_exceeded_message()` 四项信息已被任务 6.5 新增的一条测试覆盖（但
      那条测试从**手工构造**的 `BudgetVerdict` 出发，不经过 `complete_once`/
      `node_executor`/`delivery_from_checkpoint` 的真实链路）；`PUT
      /token-budget-enforcement` 切换模式后 `current_enforcement_mode()` 立即观察到
      新值已被 `test_enterprise_token_budget_enforcement.py` 覆盖（但只验证了那一个
      独立读取函数，没有验证一次真实的 `gate.check()`/`_budget_gate()` 判定是否也
      立即观察到新值）；grace 窗口生效时 `effective_mode` 与节流日志已被
      `test_token_accounting_budget.py` 覆盖（同样只停在 `current_enforcement_state()`
      这一层，不涉及 `gate.check()` 或 `PUT` 端点）。**结论：本任务要求的六个场景
      都存在"分层单点测试各自都通过、但没有测试把它们真正串起来验证"的缺口**，
      这正是任务描述里"跨模块的端到端集成断言"要补的部分，不是重复造轮子。
    - **新增测试文件**：`backend/tests/test_token_budget_integration.py`（新建，
      6 条测试，对应任务描述 a-f 六个场景）。文件头部的 docstring 逐条写明了
      "与既有测试的边界"——每个场景具体复用了哪些既有测试已经验证过的部分、
      本测试新增验证的是哪一个"串起来"的关系，避免读者误以为是重复劳动。
      风格延续本仓库既有惯例：跨文件 import 复用 `test_agent_runtime_delivery.py`、
      `test_agent_runtime_group_handoff.py`、`test_agent_runtime_model_step_service.py`、
      `test_agent_runtime_node_executor.py`、`test_agent_runtime_run_compactor.py`、
      `test_enterprise_token_budget_enforcement.py` 六个文件里已有的脚手架构造器
      （`_records`/`_target`/`_forced_enforce`/`_agent`/`_model`/`_state`/`_context`/
      `_executor`/`_FakeSettingStore`/`_patch_store` 等），与
      `test_token_budget_gate_lanes.py`、`test_group_handoff_budget_property.py`
      已经建立的跨文件复用惯例一致，不重新发明这些构造器。
      - **(a) 直接对话全链路**：用一个真实超限的 Agent 驱动
        `RuntimeModelStepService.complete_once()`（真实短路，返回
        `error["code"]=="token_budget_exceeded"`）-> 把这个真实返回值喂给真实的
        `DeterministicRuntimeNodeExecutor._model()`（`lifecycle.reason ==
        "token_budget_exceeded"`）-> 用这个真实 lifecycle 构造一个真实的
        `CheckpointObservation`，驱动真实的 `delivery_from_checkpoint()` 推导出
        `DeliveryRequest`（不手工构造 `failure_code`/`failure_message`）-> 驱动真实的
        `deliver_runtime_message()`，断言最终渲染文本包含 blocked_scope（"Agent
        当日"）、千分位 used、千分位 limit 三项（`reset_at` 由 `budget_exceeded_message`
        统一产出，已在任务 6.5 的测试里对同一渲染函数验证过 `isoformat` 格式，本测试
        用真实用量数字反向确认这条链路没有丢字段，不重复断言精确的 `reset_at` 字符串
        格式）。四层全部用各自的真实实现串起来，只在"取数据库行"这一层打桩
        （`_msvc_session_factory`/`_RecordingDB`）。
      - **(b) 触发器链路等价性**：直接构造一个 `DeterministicRuntimeNodeExecutor`
        （与生产装配用的是同一个类，跳过了 `RuntimeNodeExecutorRouter` 那层薄封装——
        读代码确认该路由层只按 `context.system_role == "group_planning"` 分流，完全
        不参与判定，不是本任务要验证的对象），用 `assert executor._model_service is
        shared_service` 钉住"驱动判定的确实是唯一实例"，再分别用
        `source_type="chat"` 与 `source_type="trigger"` 两个 context 各调一次
        `execute("model", ...)`，断言两次的 `lifecycle.reason` 逐字相同——因为
        `RuntimeModelStepService.complete_once` 全文没有一行读取 `context.source_type`
        来决定是否判定限额，这正是"同一份判定，不因链路分叉"这句话在代码层面的
        证据。遇到的坑：最初用占位字符串 `tenant_id="tenant-1"` 会在 `_load()` 里
        因为 `uuid.UUID("tenant-1")` 解析失败，被 `except ValueError` 捕获后错误码变成
        `invalid_runtime_identity`，掩盖了本测试真正想触发的 `token_budget_exceeded`
        路径——已改为用真实的 `uuid.uuid4()` 构造 tenant_id/agent_id/model_id。第二个
        坑：`test_agent_runtime_model_step_service._session_factory` 这个既有测试
        替身设计成"只服务一次 `complete_once()` 调用"（第二次 `__call__` 返回一个
        `_NoFallbackDB`，只为支持同一次调用内部的 fallback-model 查询），而本测试
        需要同一个 service 实例被驱动两次；为此新增了一个本文件内的
        `_repeatable_session_factory(model, agent)` 辅助函数，每次 `__call__` 都返回
        一个全新的、完整补货的 `_DB(model, agent)`，并在函数文档字符串里写明了与
        既有 `_session_factory` 的差异及原因。
      - **(c) 压缩 -> 业务步的顺序**：构造一个真实的
        `RuntimeRunCompactorService`（`input_loader` 返回一个 `subjects` 为超限 Agent
        的 `RunCompactInputs`，`current_input_tokens=800`/`effective_input_budget=1_000`
        命中 80% 水位）与一个"一旦被调用就 `AssertionError`"的业务模型服务
        （`_ForbiddenBusinessModelService`），驱动**真实编译的 LangGraph 图**
        （`build_agent_runtime_graph` + `runtime_thread_config`，不是直接调
        `executor.execute("compact", ...)`）从 `next_route="compact"` 开始执行，断言
        最终 `lifecycle.status=="failed"`、`reason=="token_budget_exceeded"`，且
        `business_model_service.calls == 0`——这是"顺序性"断言的核心：不是"压缩这
        一步本身正确"（`test_agent_runtime_run_compactor.py` 已经验证过这一点），
        而是"压缩拦截了，图里下一步该走到的业务 model 节点从未被触达"。遇到的坑：
        `_node_state()` 默认构造的 `messages` 是空列表，而
        `RuntimeRunCompactorService.compact_if_needed` 的 `_thread_messages()` 在
        没有消息时会直接早退（`if not messages: return RunCompactResult()`）——这个
        早退发生在预算判定**之前**，会让测试"因为压根没有尝试压缩"而不是"因为压缩
        被正确拦截"意外通过（一个会掩盖真正意图的假阳性）；已在测试里显式构造了
        非空的 `messages`（一条旧历史 + 一条 `runtime_input="current"` 的当前输入），
        并在代码注释里写明了这个坑，供后续维护者参考。
      - **(d) 群聊 handoff**：分两半组合而不是重新搭建满足 `_snapshot_scope`
        严格校验的完整群聊状态去驱动 `complete_once` 内部真正调用
        `preflight_group_agent_handoff`（读代码确认那需要的脚手架复杂度与
        `test_agent_runtime_model_step_service.py` 里已有的最小 group_context 测试
        不兼容，重新搭建的成本远超收益）：第一半直接调用真实的
        `preflight_group_agent_handoff()`（复用 `test_agent_runtime_group_handoff.py`
        的 `_records()`/`_target()`/`_forced_enforce()`），拿到一个**真实产出**的
        `GroupAgentHandoffError("group_handoff_budget_unavailable", repairable=True)`
        实例（不是手写一个同名同码的合成异常）；第二半把这个真实异常通过
        `unittest.mock.patch` 注入点（`AsyncMock(side_effect=real_budget_error)`）
        喂给 `RuntimeModelStepService.complete_once` 的真实翻译逻辑，断言
        `result.intent == "text"`（repair）而不是 `"error"`（Run 失败），且
        `result.repair_instruction` 里包含真实异常的 `code`。这条测试证明的是
        `test_agent_runtime_model_step_service.py`（验证翻译逻辑本身，用手写异常）
        与 `test_agent_runtime_group_handoff.py`（验证真实异常的产出，不涉及翻译）
        两条既有测试各自都无法单独证明的一点：真实产出的异常形状确实能被真实的翻译
        逻辑正确处理，两者的契合没有被字段级的微妙差异破坏。为此新增了一个本文件内的
        `_msvc_null_db()` 占位对象——`preflight_group_agent_handoff` 内部真实执行到
        `_validate_targets` 时会调用 `gate.load_subjects(db, tenant_id=...)`（两条真实
        SELECT），需要一个能响应 `execute()` 的最小 db 替身。
      - **(e) 执行模式切换**：区别于
        `test_enterprise_token_budget_enforcement.py` 只验证
        `budget.current_enforcement_mode()` 这个独立读取函数，本测试驱动一次真实的
        `RuntimeModelStepService._budget_gate()`（`gate.check()` 的生产消费者之一）
        —— 用同一个 `service` 实例、同一组 `agent`/`tenant`/`counter` 输入，先在
        `warn_only` 下验证 `_budget_gate` 返回 `None`（放行），调用真实的
        `enterprise.update_token_budget_enforcement(...)` 切到 `enforce` 后，用完全
        相同的输入再判定一次，断言这次返回一个 `intent=="error"` 的
        `ModelStepResult`。复用 `test_enterprise_token_budget_enforcement.py` 的
        `_FakeSettingStore`/`_patch_store`/`_platform_admin` 三个辅助对象（同一份
        "同时打桩 `enterprise.system_setting_dao` 与 `budget.system_setting_dao`"的
        坑已经在那个文件里踩过并记录，本测试直接复用避免重蹈）。
      - **(f) grace 窗口**：区别于 `test_token_accounting_budget.py`（只验证
        `current_enforcement_state()`）与
        `test_enterprise_token_budget_enforcement.py`（只验证 `clear_grace` 后
        `current_enforcement_state()` 的返回值），本测试驱动真实的
        `gate.check(lane=LANE_BUSINESS_STEP, ...)`——用一份 `grace_until` 在未来的
        存储值，对一个真实超限的 `BudgetSubjects` 判定，断言 `verdict.allowed is
        True`（grace 窗口内放行）且捕获到一条 `token_budget_enforcement_grace_active`
        的 INFO 日志；再调用真实的 `enterprise.update_token_budget_enforcement(...,
        clear_grace=True)`，用完全相同的 `subjects` 再判一次，断言
        `verdict.allowed is False`。这一步验证的是 `gate.check()`（真正做判定、真正
        落 `[TokenBudget]` 日志的那一层）与 `current_enforcement_state()`
        （`budget.py` 内部的读取函数）之间没有脱节——理论上未来可能有人在
        `gate.check()` 里意外传了显式 `mode=`，绕开 grace 语义，那样的话
        `current_enforcement_state()` 单独测试仍会通过，但本测试会失败。
    - **未新增/未修改任何生产代码**：本任务纯粹是测试补充，`backend/app/` 目录下
      没有任何文件被改动。
    - **验证结果**：
      - `test_token_budget_integration.py` 单独跑：**6 passed, 0 failed**。
      - 全量 `backend/tests/`（**必须在 `backend/` 目录下运行**
        `.venv/bin/python -m pytest tests/ -q`；不能用系统默认 python——系统默认
        Python 3.10 没有 `datetime.UTC`，必须用仓库自带的 `backend/.venv`；也不能从
        仓库根目录运行——会因 `test_sso_toggle.py` 的已知 cwd 相关环境问题
        （`from tests.test_auth import ...` 触发 `ModuleNotFoundError: No module
        named 'tests'`）在 collection 阶段中断，与本次改动无关）：
        **2479 passed, 3 failed**。失败集合与既有基线（2473 passed, 3 failed）逐条
        一致（`test_feishu_card_tools.py` 1 条 + `test_html_to_pdf.py` 2 条，均为
        环境依赖缺失——`libgobject-2.0-0` 系统库缺失导致 weasyprint 无法加载，与本次
        改动无关）。与基线相比：失败数不变（3），通过数由 2473 增至 2479（恰好对应
        本任务新增的 6 条测试，2473+6=2479，与实测完全对应），**无新增意外失败**。
    - **未发现需要委派方注意的既有缺陷**：六个场景的核实过程中未发现
      `model_step_service`/`node_executor`/`checkpoint_side_effects`/`delivery`/
      `run_compactor`/`group_handoff`/`gate`/`budget`/`enterprise` 任何一处存在与
      design.md/bugfix.md 期望不一致的行为。

- [x] 13. 复跑两个 Property，确认修复成立且无回归

  - [x] 13.1 复跑 Bug Condition 探索性测试，确认现在通过
    - **Property 1: Expected Behavior** - 击穿限额的输入必须被拦截且零消耗
    - **IMPORTANT**: 复跑任务 1 写的**同一批测试**，不要新写测试
    - 任务 1 的测试已经编码了期望行为；它从 FAIL 转为 PASS 就是「bug 已修复」的证据
    - 按 design "Fix Checking" 把域补齐：`scope ∈ {agent_day, agent_month, tenant_day}` ×
      `lane ∈ {business_step, run_compact, session_compact, group_compact, planning, model_probe}` ×
      `breach_shape ∈ {used == limit, used > limit, used + estimated ≥ limit}`
      （`tenant_day` 之外的 scope 与三条 system_scope 链路的组合自动跳过 —— 那些链路没有 agent）
    - 每个组合断言四件事：completion 端口 / HTTP client 未被调用；错误 code 为 `token_budget_exceeded`；
      消息含 blocked_scope / 千分位 used / 千分位 limit / `reset_at.isoformat(timespec="minutes")`；
      `ledger.record` 未被调用（零消耗）
    - 能力边界：预检基于估算，目标是「超限幅度有界（不超过一轮消耗量）」，不是「一个 token 都不超」
    - **EXPECTED OUTCOME**: 全部 PASS
    - **环境**：无库可完成
    - **依赖**：3–8, 12
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.8, 2.9, 2.13_
    - **实现记录（复跑验证，本次执行）**：
      - **复跑目标确认**：按任务描述"复跑任务 1 写的同一批测试，不要新写测试"的要求，
        复跑的是任务 1 创建、任务 3.2/6.1/6.2/6.3/6.4/7.1 逐条转正的
        `backend/tests/test_token_budget_gate_lanes.py`，**未新增或修改任何测试代码**。
      - **复跑结果**：`.venv/bin/python -m pytest tests/test_token_budget_gate_lanes.py -v`
        （在 `backend/` 目录下，用仓库自带的 `backend/.venv`，Python 3.12.11）——
        **7 passed, 0 failed**：
        - `test_counterexample_1_missing_setting_row_defaults_to_enforce_and_blocks` PASSED
        - `test_counterexample_2_run_compact_now_blocks_before_completion` PASSED
        - `test_counterexample_3_planning_now_blocks_before_completion` PASSED
        - `test_counterexample_4_group_compact_now_blocks_before_completion` PASSED
        - `test_counterexample_5_model_probe_now_blocks_before_creating_a_client` PASSED
        - `test_counterexample_6_group_handoff_and_business_step_now_agree` PASSED
        - `test_counterexample_7_evaluate_with_agent_none_only_checks_tenant_day` PASSED
        7 个反例全部从任务 1 在未修复代码上记录的状态（1/6/7 PASS 证明成因存在，2/3/4/5
        FAIL 证明闸门缺失）转为现在的全部 PASS——这正是"从 FAIL 转为 PASS 就是 bug 已修复
        的证据"这句话描述的对象，逐条对应任务 6.1/6.2/6.3/6.4/3.2/7.1 的修复。
        **EXPECTED OUTCOME（全部 PASS）完全兑现**。
      - **域穷举覆盖核对**（按 design.md "Fix Checking" 列出的
        `scope × lane × breach_shape` 逐项核对现有测试，不新增测试文件，只核对结论）：
        - **lane 覆盖**：六条 lane 均已各自被至少一条测试驱动到"命中限额并拦截"的路径——
          `business_step`（`test_token_accounting_gate.py::test_check_returns_blocked_verdict_and_logs_with_lane_when_limit_hit`
          + `test_token_budget_enforcement.py` 的多条阶段一/阶段二短路测试）、
          `run_compact`（`test_agent_runtime_run_compactor.py::test_breached_agent_budget_blocks_compact_before_completion_is_called`
          + 反例 2）、`session_compact`/`group_compact`
          （`test_agent_runtime_session_context_compactor.py` 的
          `test_breached_agent_budget_blocks_session_compact_before_completion` /
          `test_breached_tenant_budget_blocks_group_compact_before_completion` + 反例 4）、
          `planning`（`test_agent_runtime_planning.py::test_breached_tenant_budget_blocks_planning_before_completion`
          + 反例 3）、`model_probe`（`test_llm_tool_capability_probe.py::test_breached_tenant_budget_blocks_probe_before_creating_a_client`
          + 反例 5）。
        - **scope 覆盖（逐 lane 核对，发现的缺口如下）**：
          - `business_step`：agent_day / agent_month / tenant_day 三档在
            `test_token_accounting_budget.py`（`test_agent_daily_limit_blocks_first`、
            `test_agent_monthly_limit_blocks_when_daily_is_fine`、
            `test_tenant_daily_limit_blocks_when_agent_is_fine`、
            `test_the_most_specific_scope_wins_when_several_are_breached`）均有覆盖，
            `test_token_accounting_gate.py` 复核了 agent_day / tenant_day 两档经
            `gate.check()` 这一层同样命中。**三档全覆盖**。
          - `run_compact` / `session_compact`：现有断言性拦截测试**只覆盖了 agent_day**
            （`_breached_agent_subjects()` 固定 `max_tokens_per_day=100_000,
            tokens_used_today=200_000, max_tokens_per_month=None`），**未见 agent_month
            或 tenant_day 专门针对这两条 lane 的拦截测试**——尽管这两条 lane 携带的
            `BudgetSubjects` 同时包含 `agent` 与 `tenant`/`tenant_counter`，架构上
            `agent_month`/`tenant_day` 命中同样应该拦截。这是相对 design.md 域穷举表述
            的一个字面缺口。
          - `group_compact` / `planning` / `model_probe`：三条 system_scope 链路
            `agent=None`，`agent_day`/`agent_month` 不适用（design.md 本身已注明"那些
            链路没有 agent，自动跳过"），只需覆盖 `tenant_day`——三者均已覆盖
            （`_breached_tenant_subjects()` / `_breached_tenant()` / `_breached_tenant_pair()`
            均设置 `tenant.max_tokens_per_day=500_000, tenant_counter.tokens_used_today=500_000`）。
        - **breach_shape 覆盖**：`used == limit` 在 business_step（多条）/
          group_compact/planning/model_probe（均为 500,000==500,000）广泛覆盖；
          `used > limit` 在 run_compact/session_compact（200,000>100,000）覆盖；
          `used + estimated ≥ limit` 只在 business_step 有意义
          （`test_preflight_blocks_when_remaining_is_below_the_estimate`，
          99,000+5,000>100,000）——**这不是遗漏**：读代码确认其余五条 lane 在生产代码里
          调用 `gate.check()` 时 `estimated_next_round_tokens` 一律硬编码传 `0`
          （`run_compactor.py:680`、`session_context_compactor.py:454`、
          `planning.py:530`、`enterprise.py:287`、`group_handoff.py:556` 均如此，且各自
          代码注释写明"只做击穿判定，不做预算预扣——超限幅度有界由 business_step 自己的
          两阶段估算保证"），即这五条 lane 的 `breach_shape` 架构上只能退化为
          `used ≥ limit`，`used + estimated ≥ limit` 这个维度对它们不成立，不存在可以
          补的测试点。
        - **"每个组合断言四件事"的核对**：completion 端口/HTTP client 未被调用、错误
          code 为 `token_budget_exceeded" 两项在六条 lane 的拦截测试里均逐一显式断言
          （`calls == []` / `created_clients == []` + `code`/`error_code` 断言）。
          消息含 blocked_scope/千分位 used/千分位 limit/`reset_at.isoformat(timespec=
          "minutes")` 四项信息，由共享的 `budget_exceeded_message()` 承载并在
          `test_token_accounting_budget.py`（约 715 行附近）与端到端渲染测试
          `test_agent_runtime_delivery.py::test_token_budget_exceeded_failure_renders_the_budget_exceeded_message`
          （任务 6.5）里独立验证过其格式，六条 lane 各自的拦截测试复用同一个函数产出
          消息、不逐条重复断言消息文本，这是合理的测试职责划分而非遗漏。`ledger.record`
          未被调用（零消耗）这一项：五条经 `complete_llm_once`（`single_step.py`）转发
          completion 调用的 lane（business_step/run_compact/session_compact/
          group_compact/planning），`ledger.record` 只在 `complete_llm_once` 内部
          `client.complete()` 成功返回之后才会被调用——`calls == []` 已经证明
          `complete_llm_once` 整体未被进入，`ledger.record` 因此在控制流上不可能被调用，
          这是"通过构造证明"而非需要额外 mock 断言；`model_probe` 同理，读
          `enterprise.py::test_llm_model` 确认超限时的 `return` 发生在
          `create_llm_client(...)` 之前，`record_token_usage_ledger` 调用点在
          `client.complete()` 成功之后，`created_clients == []` 同样通过控制流证明
          `ledger.record` 未被调用。
        - **结论**：发现的唯一实质缺口是"`run_compact`/`session_compact` 两条 lane 目前
          只有 `agent_day` 一档的拦截测试，未见 `agent_month`/`tenant_day` 专门覆盖"。
          按任务描述的处理原则（"缺口不危及'bug已修复'核心结论时，只报告不新增测试"）
          评估：这两条 lane 是否至少接入了闸门（本次 bug 修复的核心诉求）已经被现有测试
          证实；`agent_month`/`tenant_day` 命中时的判定逻辑本身（`_breach()` 的优先级、
          `blocked_scope` 归属）与 `business_step` 走的是完全相同的
          `gate.check()` → `budget.evaluate()`，已经在 `test_token_accounting_budget.py`
          （`test_the_most_specific_scope_wins_when_several_are_breached` 等）与
          `test_token_accounting_gate.py` 里被独立、充分地验证过——`run_compact`/
          `session_compact` 只是把同一个已验证过的判定函数接进自己的调用点，不存在
          "换了一条 lane，agent_month/tenant_day 的判定逻辑会有不同表现"的风险。因此
          **这个缺口不危及"bug已修复"的核心结论**，按要求在此报告缺口，**不新增测试**。
      - **未做任何修复或测试改动**：本次执行只运行了既有测试并核对了域覆盖结论，
        `backend/` 目录下没有任何文件被修改。
    - **验证结论**：EXPECTED OUTCOME 完全兑现（7/7 PASS）；域穷举覆盖存在一个已报告、
      不影响核心结论的缺口（run_compact/session_compact 缺 agent_month/tenant_day 专项
      拦截测试，判定逻辑本身已在共享代码路径充分验证）。

  - [x] 13.2 复跑保留行为基线测试，确认仍然通过
    - **Property 2: Preservation** - 未击穿限额的输入行为逐字节不变
    - **IMPORTANT**: 复跑任务 2 写的**同一批测试**，不要新写测试
    - 允许且仅允许两处期望值变化，且都必须在测试里显式写明理由：
      1. 任务 2 域点 3 的 `get_value` 调用次数由 2 降为 `≤ 1`（缓存生效，属净收益）
      2. 任务 2 域点 2 里 `group_handoff` 对 `limit == 0` 的结论由放行变为拦截（向 3.2 对齐，见 7.2）
    - 其余每个域点必须逐字段一致；`test_token_accounting_ledger/_normalize/_periods` 与
      `test_token_period_consistency` 必须**一行未改**且全绿（3.5 的守门条件）
    - **EXPECTED OUTCOME**: 全部 PASS，无回归
    - **环境**：无库可完成
    - **依赖**：3–8, 12
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12_
    - **实现记录（复跑，未新增任何测试）**：
      - **复跑命令与结果**：在 `backend/` 目录下执行
        `.venv/bin/python -m pytest tests/test_token_budget_preservation_baseline.py -v`
        （未从仓库根目录跑——那样会因 `test_sso_toggle.py` 的既有 cwd 相关问题在
        collection 阶段中断，与本次改动无关；也未用系统默认 python——系统默认
        Python 3.10 没有 `datetime.UTC`，必须用仓库自带的 `backend/.venv`，
        Python 3.12）。任务 2 创建的 14 个用例**全部 PASS**，未新写一条测试，
        文件本身也未做任何修改（`git status` 确认 `test_token_budget_preservation_baseline.py`
        目前是未跟踪的新文件，内容与任务 2/3.3/7.1 落地时留下的版本一致，本次复跑
        没有触碰它）。
      - **核对结论 1——域点 3（`get_value` 调用次数 2→`<=1`）**：读取了
        `test_domain_point_3_business_step_round_trip_baseline` 的源码，确认
        断言是 `assert get_value_calls <= 1`（不是 `== 2`），紧邻的断言消息与函数
        文档字符串完整写明了理由——"任务 3.3 更新：加入进程内模式缓存后，一个模型步
        内 get_value 调用次数从 2 次降为 <= 1 次（缓存生效后两阶段共用同一次缓存
        读取；若缓存进入本测试前已是新鲜状态则可能是 0 次，用 <= 1 避免测试脆弱）"，
        与任务 3.3 实现记录里"对 3.3 是净收益"的说法一致。**确认这处变化确实是
        允许的第一处，且理由已显式写在测试内**。复跑实测该用例 PASS。
      - **核对结论 2——域点 2（`group_handoff` 对 `limit==0` 由放行变为拦截）**：读取了
        `test_domain_point_2_zero_limit_differs_from_null_and_group_handoff_allows_it_today`
        的源码，确认最终断言是
        `assert handoff_verdict.allowed is False`（不是 `True`），断言消息与函数体内
        紧邻的大段中英文注释完整写明了理由——`group_handoff._target_budget_available`
        已在任务 7.1 被拆分删除，token 判断部分收敛到 `gate.check(lane=
        LANE_GROUP_HANDOFF, ...)`，遵循 `_breach` 语义（0 参与阈值判断、不与 NULL
        合并），因此 `limit=0` 现在被拦截；注释里明确说明这是"任务 7.1 落地时就已
        生效的行为变更"，不是本次复跑或任务 7.2 才产生的新变化，与任务 7.1/7.2 的
        实现记录逐条对应。**确认这处变化确实是允许的第二处，且理由已显式写在测试
        内**。复跑实测该用例 PASS。函数名保留 `..._group_handoff_allows_it_today`
        字面上仍是"今天放行"，但读取函数体后确认这是历史命名（对应任务 2 当初记录
        未修复代码上的行为），实际断言内容已在任务 7.1 更新为"拦截"并配有完整说明，
        不存在函数名与断言内容不一致导致误判的风险。
      - **其余域点（1、4、5、6、7、8）逐一核对结论：均与任务 2 原始基线逐字段一致，
        无悄悄改动**：
        - 域点 1（`limit=None` × `used∈{0,1,10^9}`）：断言 `verdict.allowed is
          True`、`blocked_scope is None`、`_gate_would_call_completion(...) is
          True`，与任务 2 记录的基线值逐字段相同，未改。
        - 域点 4（周期翻页 × 三时区）：断言 `verdict.allowed is True`、
          `blocked_scope is None`，输入（`stale_daily`/`stale_monthly` 的构造方式、
          三个时区取值）与任务 2 记录一致，未改。
        - 域点 5（fail-open 分级）：`TypeError→ERROR+token_budget_enforcement_disabled_bug`、
          `OSError→WARNING+token_budget_enforcement_disabled_transient`，两者
          `mode` 仍断言为 `MODE_WARN_ONLY`，与任务 2 记录一致，未改（这条也是 3.6
          的红线之一，缓存与执行模式默认值翻转均未影响"读取失败"这个分支的既有
          fail-open 语义）。
        - 域点 6（判定优先级）：`agent_day` 三档同时击穿时胜出、`agent_month` 优先于
          `tenant_day`，断言与任务 2 记录逐字段一致，未改。
        - 域点 7（80% 软告警阈值）：`used=floor(limit*0.8)→soft_warning=True`、
          `used=floor(limit*0.8)-1→soft_warning=False`，与任务 2 记录逐字段一致，
          未改。
        - 域点 8（记账口径，见下方独立核对）。
        逐一读取了这 6 个域点对应的测试函数源码（不是只看函数名），确认除域点 2/3
        外没有第三处期望值被悄悄改动。
      - **3.5 守门条件核对结论**：
        - 用 `git log --oneline -- <文件路径>` 核实
          `test_token_accounting_ledger.py`/`test_token_accounting_normalize.py`/
          `test_token_accounting_periods.py`/`test_token_period_consistency.py`
          四个文件的提交历史——前三个文件只出现在同一个基线提交
          `f7e3280a`（"重写 token 计量、缓存命中率与预算限额体系"）里，此后没有任何
          新提交触碰过它们；`git status --porcelain` 确认这三个文件当前**零改动**
          （working tree clean，未出现在 `git status` 的 modified 列表里）。
        - `test_token_period_consistency.py` 确认有改动（`git status` 显示为
          `M`），用 `git diff` 完整核对了改动内容：**恰好是任务 7.1 记录的"迁移测试
          断言的调用目标"这一处例外**——四条测试（`test_target_budget_blocked/
          available_when_daily/monthly_counter_has_not_rolled_over_in_tenant_
          local_day/month`）原来调用的 `group_handoff._target_budget_available(...)`
          已被任务 7.1 删除（token 判断部分拆分后不再存在这个函数签名），改为直接
          驱动 `budget.evaluate(agent=..., tenant=..., tenant_counter=..., now=...,
          mode=MODE_ENFORCE)` 并断言 `verdict.allowed`/`verdict.blocked_scope`；
          文件头部与小节注释同步更新，明确写着"测试意图（按租户时区而非 UTC 判断是否
          翻页）保持不变，只是不再通过一个已被删除的函数签名去验证它"。**这个改动
          符合任务 7.1 记录的描述**：被测函数确实已被删除（不是被换成了别的实现），
          迁移到的 `budget.evaluate()` 是同一份判定逻辑现在唯一的实现入口，不是
          "换了一个不等价的判定路径"。除了函数签名迁移（含把同步测试函数改为
          `async def`、新增 `counter` 局部变量、断言语句从布尔值改为
          `verdict.allowed`/`verdict.blocked_scope`）之外，测试的输入构造
          （`agent`/`tenant` 的字段取值、跨 UTC 午夜/月份边界的具体时间点）与断言
          意图未发生变化；文件里另外 3 条与 `_lazy_reset` 相关的测试
          （`test_lazy_reset_does_not_fire_across_a_utc_midnight_...` 等）以及
          `test_create_agent_inherits_tenant_default_token_limits_...` 等 3 条
          与本次改动完全无关的测试逐行未改。
        - 四个文件合计运行结果：**65 passed, 0 failed**（`test_token_accounting_ledger.py`
          23 项 + `test_token_accounting_normalize.py` 19 项 +
          `test_token_accounting_periods.py` 11 项 + `test_token_period_consistency.py`
          12 项），与任务 2 记录的基线数量逐一对应，全绿。
      - **与基线对比（任务 12 记录的 2479 passed, 3 failed）**：本次全量复跑
        `backend/tests/`（**在 `backend/` 目录下**运行
        `.venv/bin/python -m pytest tests/ -q`）结果为 **2479 passed, 3 failed**，
        失败集合逐条一致（`test_feishu_card_tools.py` 1 条 + `test_html_to_pdf.py`
        2 条，均为环境依赖缺失——`libgobject-2.0-0` 系统库缺失导致 weasyprint 无法
        加载，与本次改动无关）。通过数与失败数与任务 12 记录的基线**完全相同**——
        本任务只是复跑既有测试，未新增或删除任何测试用例，符合"复跑同一批测试、
        不要新写测试"的要求。
      - **结论**：14 个保留行为基线用例全部 PASS；两处允许的期望值变化（域点 3 的
        `get_value` 调用次数、域点 2 的 `group_handoff` 对 `limit==0` 的结论）均已
        在测试内显式写明理由，且确认不多不少正好是这两处；其余域点逐字段核对无
        悄悄改动；3.5 守门条件的四个文件里三个零改动、一个的改动完全符合任务 7.1
        记录的例外描述（迁移调用目标、测试意图不变）；全量测试结果与任务 12 的基线
        逐条一致，**无回归**。

- [x] 14. Checkpoint - 确认全部测试通过
  - 跑 `backend` 全量测试（无库部分）+ 前端类型检查 / lint
  - 确认「批次 H / I（需库）」的完成状态被明确记录：未完成即意味着**存量环境的限额仍未生效**
  - 有疑问先问用户，不要自行调整 design 里已定调的结论（默认 enforce、grace 窗口、
    `clearance` 必填、变更 6 的移除口径）
  - **实现记录（2026-08-09，Checkpoint 执行）**：

    **1. backend 全量测试结果**：在 `backend/` 目录下运行
    `.venv/bin/python -m pytest tests/ -q`（未从仓库根目录运行，未用系统默认 python，
    理由与任务 6.3 起历次实现记录一致：系统默认 Python 3.10 没有 `datetime.UTC`；从
    仓库根目录运行会因 `test_sso_toggle.py` 的已知 cwd 环境问题在 collection 阶段中断，
    与本次改动无关）。实测结果：**2479 passed, 3 failed**，与任务 12/13.1/13.2 记录的
    基线**逐条一致**——失败集合为 `test_feishu_card_tools.py` 1 条 +
    `test_html_to_pdf.py` 2 条，均为环境依赖缺失（`libgobject-2.0-0` 系统库缺失导致
    weasyprint 无法加载），与本次改动无关，未做任何修复。**无新增意外失败**。

    **2. 前端类型检查 / lint 结果**：`frontend/package.json` 的 `"scripts"` 里只有
    `dev`/`build`/`test`/`preview` 四项，**没有配置专门的 `lint` 脚本**（`build` 脚本
    是 `tsc && vite build`，其中类型检查这一步单独跑就是 `tsc --noEmit`）；未新增 lint
    配置（按任务要求"没有配置或运行有阻碍，说明情况即可，不要花时间新增"）。在
    `frontend/` 目录下运行 `npx tsc --noEmit`：**无任何输出，退出码 0**，与任务
    9.1/9.2/9.3 一致的验证方式，确认全部前端改动（含批次 G 的三个任务）无类型错误。

    **3. 批次 H / I 未完成状态记录（本任务核心诉求）**：

    - **当前实际状态标记**：已用 `todo_list` 工具（本 spec 的任务状态查询）逐一确认，
      任务 10.1、10.2、10.3（批次 H）与任务 11.1-11.6（批次 I）当前均为 `not_started`
      （tasks.md 里对应复选框标记为 `[~]`，与「尚未开始/尚未完成」一致，不是「进行中被
      中断」）。这九个任务从未被执行——本地 Docker daemon（OrbStack）未启动、无独立
      psql/postgres 可用，这两批任务都需要真实 PostgreSQL 环境（`alembic heads`、真实
      SQL 查询等），当前环境无法完成。

    - **批次 H 未完成的业务含义（原文引用，逐字未改）**：
      「批次 H 未完成前，存量环境（已经部署运行的老环境）里
      `system_settings.token_budget_enforcement_mode` 这一行如果历史上被写入过
      `{"mode": "warn_only"}`，不会被自动纠正为 `enforce`——因为默认值翻转（任务 3.2）
      只影响*代码层的兜底默认值*（配置行缺失时的行为），对已经显式写入过一行
      `warn_only` 配置的存量库无效。也就是说，本次修复对全新安装立即生效（无历史配置行
      → 走代码默认值 enforce），但对已经运行过一段时间、`system_settings` 里已经有一行
      `warn_only` 记录的老环境，仍然需要批次 H 的迁移（或运维人员通过任务 8.2 新增的
      `PUT /api/enterprise/token-budget-enforcement` 端点手动切换）才能让限额真正
      生效。」

    - **批次 I 六条复验结论待验证内容清单（设计假设，非已验证事实）**：
      1. 任务 11.1：存量库里 `token_budget_enforcement_mode` 这一行的实际值与形状是否
         逐字节等于任务 10.2 迁移的 `WHERE value = '{"mode":"warn_only"}'::jsonb` 判据；
         若形状不同（例如带额外键），存量行不会被迁移改写。
      2. 任务 11.2：真实数据里用户配置的上限究竟落在 Agent 级 / 租户级 / 模型级哪一档
         （`agents.max_tokens_per_day/_month`、`tenants.max_tokens_per_day`、
         `llm_models.max_tokens_per_day` 三处的非空值分布）。**这条结论反过来影响任务
         8.3（模型级字段移除）的前置假设是否成立**——任务 8.3 的实现记录里已经如实
         记录了这个风险（未经真实数据验证，只是基于 design.md 的取舍依据与用户已确认
         的决策方向直接实施），本任务在 Checkpoint 层面再次汇总提醒，不重新论证。
      3. 任务 11.3：迁移链的实际 head（`alembic heads`/`alembic history` 的实测输出），
         静态扫描显示可能存在多个未合并 head，`down_revision` 的最终选定需要真实库确认。
      4. 任务 11.4：`jsonb` 比较与 `jsonb_build_object` 的行为，以及迁移
         upgrade/downgrade 的真实往返效果（存量 `warn_only` 行被改写、被管理员改过
         形状的行两个方向都不动）。
      5. 任务 11.5：记账侧（3.5/3.11）在有库环境下的一遍真实回归，以及任务 8.1 新增的
         `SystemSettingDAO.set_value` upsert 在真实库上的冲突行为。
      6. 任务 11.6：多 worker（gunicorn/uvicorn 多进程）部署形态下，30 秒 TTL 缓存的
         实际生效时延实测值，需要与任务 9.1 前端文案里的数字保持一致（前端已按"跟随
         后端 `propagation_seconds` 字段动态渲染，不硬编码"的方式实现，若实测偏离
         30 秒只需调整后端返回值，前端不需要改动）。

    **4. 未发现需要调整已定调设计结论的疑点**：核对全量测试结果、前端类型检查结果与
    批次 H/I 的状态记录过程中，未发现任何看起来需要调整默认 enforce、grace 窗口、
    `clearance` 必填、变更 6 移除口径这四项已定调结论的情况。批次 H/I 的"未完成"是
    环境限制（无库/无 Docker）导致，不是这些结论本身出现问题；本任务未对 design.md
    或 tasks.md 中已落地的设计结论做任何修改。

---

## 反例记录（任务 1 填写）

> 格式：`反例编号 | 输入 | 未修复代码的实际返回 | 期望返回`
>
> 测试文件：`backend/tests/test_token_budget_gate_lanes.py`。运行方式：
> `.venv/bin/python -m pytest backend/tests/test_token_budget_gate_lanes.py -v`（需用仓库自带的
> `backend/.venv`，系统默认 Python 3.10 没有 `datetime.UTC`，会在 import 阶段直接报错）。
>
> **实测结果**：`3 passed, 4 failed`（反例 1/6/7 PASS，反例 2/3/4/5 FAIL）——与 design.md /
> tasks.md 里写的 **EXPECTED OUTCOME 逐条一致**。根因假设成立：不在统计侧（记账口径、时区
> 口径、陈旧 Agent 实例这三条已被 requirements 排除），而在「执行模式默认值 warn_only」
> 与「四条链路完全没有限额判定」这两处。全量测试跑了一遍 `backend/tests/`（排除本文件的
> 4 个预期失败），2355 passed，另外 3 个失败（`test_feishu_card_tools.py`、
> `test_html_to_pdf.py` 的 2 条）与本次改动无关（未改动任何现有文件，只新增了一个测试
> 文件），是本机环境已有的失败（大概率是缺 `weasyprint` / `wkhtmltopdf` 之类的系统依赖）。

| # | 名称 | 输入 | 未修复代码的实际返回 | 期望返回（修复后） | 实测结果 |
|---|---|---|---|---|---|
| 1 | 配置缺省即放行 | `monkeypatch` 让 `system_setting_dao.get_value` 返回调用方传入的 `default`（模拟行缺失，即 `{}`）；超限主体：`Agent(max_tokens_per_day=100_000, tokens_used_today=200_000)` | `current_enforcement_mode()` 返回 `"warn_only"`；`budget.evaluate(agent=…, tenant=…, tenant_counter=…, mode=None)` 返回 `verdict.blocked_scope="agent_day", used=200_000, limit=100_000, mode="warn_only", allowed=True` | 修复后（任务 3.2）：同样输入下 `current_enforcement_mode()` 返回 `"enforce"`，`verdict.allowed=False` | **PASS**（反例成立，证明成因存在） |
| 2 | `run_compact` 无闸门 | 超限 Agent（`max_tokens_per_day=100_000, tokens_used_today=200_000`）；驱动 `RuntimeRunCompactorService.compact_if_needed`，`current_tokens=800/effective_budget=1_000`（80% 水位） | 修复后（任务 6.1）：`RunCompactorError("token_budget_exceeded", …)` 在进入 `_compact_batches` 之前抛出，`completion` 端口未被调用 | 修复后（任务 6.1）：`RunCompactorError("token_budget_exceeded", …)` 在进入 `_compact_batches` 之前抛出，`completion` 端口未被调用 | **PASS**（已修复：反例转正，见下方「任务 6.1 更新」说明） |
| 3 | `planning` 无闸门 | 租户日上限已击穿的 `TenantTokenCounter`（`tokens_used_today=500_000`，`Tenant.max_tokens_per_day=500_000`，`PlanningModelService` 读不到它）；驱动 `PlanningModelService.complete_once`（非简单问候语，走真实模型调用分支） | `result.plan is None`（因为注入的 completion 故意抛 `RuntimeError` 来阻止产出计划）；但 `completion` 端口确实被调用 1 次（`calls` 非空，捕获到 `system_scope="planning"` 等完整 kwargs） | 修复后（任务 6.3）：`_load_model` 之后、`self._completion` 之前先 `gate.check()`，超限时直接 `return PlanningModelResult(error_code="token_budget_exceeded", retryable=False)`，`completion` 端口未被调用 | **FAIL**（如预期：断言 `calls == []` 失败——证明闸门缺失） |
| 4 | `group_compact` 无闸门 | 租户日上限已击穿的 `TenantTokenCounter`（`_compact_with_model` 读不到它）；驱动 `LLMSessionContextCompactor.compact`（`system_scope="group_compact"` 分支，`usage_agent_id=None`） | 修复后（任务 6.2）：`_compact_with_model` 首个 batch 之前先 `gate.check(lane=LANE_GROUP_COMPACT, …)`，超限时抛 `SessionContextCompactorError("token_budget_exceeded", …)`，`completion` 端口未被调用 | 修复后（任务 6.2）：`_compact_with_model` 首个 batch 之前先 `gate.check(lane=LANE_GROUP_COMPACT, …)`，超限时抛 `SessionContextCompactorError("token_budget_exceeded", …)`，`completion` 端口未被调用 | **PASS**（已修复：反例转正，见下方「任务 6.2 更新」说明） |
| 5 | `model_probe` 无闸门 | 租户日上限已击穿的 `TenantTokenCounter`；调 `enterprise.test_llm_model(...)`（`current_user.tenant_id` 已设置，`org_admin`） | 修复后（任务 6.4）：`create_llm_client` 之前先 `gate.check(lane=LANE_MODEL_PROBE, …)`，超限时直接 `return {"success": False, "error_code": "token_budget_exceeded", …}`，不创建 LLM client | 修复后（任务 6.4）：`create_llm_client` 之前先 `gate.check(lane=LANE_MODEL_PROBE, …)`，超限时直接 `return {"success": False, "error_code": "token_budget_exceeded", …}`，不创建 LLM client | **PASS**（已修复：反例转正，见下方「任务 6.4 更新」说明） |
| 6 | 口径矛盾 | 同一个超限 Agent（`max_tokens_per_day=100_000, tokens_used_today=200_000`，`max_tool_rounds=10` 等非 token 检查项均设为「未耗尽」）；一侧调 `group_handoff._target_budget_available(agent, now=NOW)`，另一侧调 `model_step_service._budget_gate(...)`（`evaluate_budget` 显式覆盖为 `mode="warn_only"`，模拟管理员显式选择只告警） | `_target_budget_available` 返回 `False`（判不可用，硬拦，无视执行模式）；`_budget_gate` 返回 `None`（=不短路=判可用，因为 `warn_only` 下 `allowed=True`） | 修复后（任务 7.1）：`group_handoff` 收敛到同一个 `gate.check()`，在 `warn_only` / grace 窗口内两侧结论一致（都放行） | **PASS**（反例成立：两个结论今天确实相反——`False` vs 放行） |
| 7 | `agent=None` 会炸（边界） | `await budget.evaluate(agent=None, tenant=_tenant(), tenant_counter=_breached_tenant_counter(), now=NOW, mode=MODE_ENFORCE)` | 抛出 `AttributeError`（根因：`effective_timezone(None, tenant)` → `get_agent_timezone_sync(None, tenant)` 直接访问 `agent.timezone`，对 `agent=None` 无防护） | 修复后（任务 3.1）：`evaluate()` 在 `agent is None` 时只保留 `tenant_day` 一档、跳过 `tz_agent` 计算，返回正常 verdict，不抛异常 | **PASS**（反例成立，是任务 3.1 的必要性证据） |

**结论**：EXPECTED OUTCOME 完全兑现——反例 1、6、7 通过（成因存在：执行模式默认值 warn_only、
group_handoff 与 business_step 口径矛盾、`agent=None` 会在 `effective_timezone` 里炸），
反例 2–5 失败（闸门缺失：`run_compact` / `planning` / `group_compact` / `model_probe` 四条链路
今天完全没有限额判定，超限主体下 completion 端口 / LLM client 照常被调用）。根因假设不需要
推翻，可以按 design.md 的批次 A–J 继续往下实施；本任务按要求**未做任何修复**，只交付了测试

> **任务 3.2 更新（2026-08-09）**：反例 1 的测试与上表描述已按修复后行为更新——
> `current_enforcement_mode()` 的「配置层缺省」分支（行缺失 / 缺 `mode` 键 / 值不在
> `KNOWN_MODES` / 脏 JSON）安全默认值已从 `warn_only` 改为 `MODE_ENFORCE`，测试改名为
> `test_counterexample_1_missing_setting_row_defaults_to_enforce_and_blocks`，断言
> `current_enforcement_mode()` 返回 `enforce`、`verdict.allowed is False`。反例 2–5（无闸门
> 的四条链路，任务 6 的范围）与反例 6（`group_handoff` 口径矛盾，任务 7.1 的范围）保持不变、
> 仍按未修复语义断言；反例 7（`agent=None`，任务 3.1）已修复保持不变。复跑
> `backend/tests/test_token_budget_gate_lanes.py`：`3 passed（1/6/7）, 4 failed（2/3/4/5，
> 预期内，等待任务 6）`。
文件与反例记录。

> **任务 6.1 更新（2026-08-09）**：反例 2 的测试与上表描述已按修复后行为更新——
> `run_compact` 链路现在接了限额闸门（`RunCompactInputs.subjects` +
> `compact_if_needed` 里 `gate.check(lane=LANE_RUN_COMPACT, ...)`），测试改名为
> `test_counterexample_2_run_compact_now_blocks_before_completion`，断言
> `RunCompactorError` 抛出、`code == "token_budget_exceeded"`、
> `is_deterministic_compact_error is True`、`calls == []`（completion 端口未被调用）。
> 反例 3/4/5（`planning` / `group_compact` / `model_probe` 无闸门，任务 6.2/6.3/6.4 的
> 范围）与反例 6（`group_handoff` 口径矛盾，任务 7.1 的范围）保持不变、仍按未修复语义
> 断言。复跑 `backend/tests/test_token_budget_gate_lanes.py`：`4 passed（1/2/6/7），
> 3 failed（3/4/5，预期内，等待任务 6.2/6.3/6.4）`。

> **任务 6.2 更新（2026-08-09）**：反例 4 的测试与上表描述已按修复后行为更新——
> `session_compact` / `group_compact` 链路现在接了限额闸门（`CompactModelSelection.subjects` +
> `_compact_with_model` 里 `gate.check(lane=..., ...)`），测试改名为
> `test_counterexample_4_group_compact_now_blocks_before_completion`，断言
> `SessionContextCompactorError` 抛出、`code == "token_budget_exceeded"`、
> `calls == []`（completion 端口未被调用）。反例 3/5（`planning` / `model_probe`
> 无闸门，任务 6.3/6.4 的范围）与反例 6（`group_handoff` 口径矛盾，任务 7.1 的
> 范围）保持不变、仍按未修复语义断言。复跑
> `backend/tests/test_token_budget_gate_lanes.py`：`5 passed（1/2/4/6/7），
> 2 failed（3/5，预期内，等待任务 6.3/6.4）`。

> **任务 6.3 更新（2026-08-09）**：反例 3 的测试与上表描述已按修复后行为更新——
> `planning` 链路现在接了限额闸门（`complete_once` 里 `_resolve_budget_subjects`
> 单独开一次会话调 `gate.load_subjects(db, tenant_id=..., agent=None)` + `gate.check(
> lane=LANE_PLANNING, ...)`），测试改名为
> `test_counterexample_3_planning_now_blocks_before_completion`，断言
> `result.error_code == "token_budget_exceeded"`、`result.retryable is False`、
> `calls == []`（completion 端口未被调用）。反例 5（`model_probe` 无闸门，任务
> 6.4 的范围）与反例 6（`group_handoff` 口径矛盾，任务 7.1 的范围）保持不变、
> 仍按未修复语义断言。复跑 `backend/tests/test_token_budget_gate_lanes.py`：
> `6 passed（1/2/3/4/6/7），1 failed（5，预期内，等待任务 6.4）`。

> **任务 6.4 更新（2026-08-09）**：反例 5 的测试与上表描述已按修复后行为更新——
> `model_probe` 链路现在接了限额闸门（`enterprise._resolve_probe_budget_clearance` +
> `test_llm_model` 里 `create_llm_client` 之前的判定短路），测试改名为
> `test_counterexample_5_model_probe_now_blocks_before_creating_a_client`，断言
> `created_clients == []`（未创建 LLM client）、`result["success"] is False`、
> `result["error_code"] == "token_budget_exceeded"`。反例 6（`group_handoff` 口径
> 矛盾，任务 7.1 的范围）保持不变、仍按未修复语义断言。至此反例 1-5、7 均已转正，
> 本文件 7 个反例全部 PASS，只剩反例 6 待任务 7.1 处理。复跑
> `backend/tests/test_token_budget_gate_lanes.py`：`7 passed（1/2/3/4/5/6/7）,
> 0 failed`——反例 6 目前仍是"验证今天口径矛盾确实存在"的断言（`assert
> handoff_available is False` 且 `business_step_result is None`，即两侧结论不同
> 这件事本身被断言为真，所以在未收敛的代码上这条测试当前是 PASS 而不是 FAIL；
> 收敛后 7.2 会把它改写成"两侧结论一致"的断言）。

## 保留行为基线（任务 2 填写）

> 测试文件：`backend/tests/test_token_budget_preservation_baseline.py`。运行方式：
> `.venv/bin/python -m pytest backend/tests/test_token_budget_preservation_baseline.py -v`
> （同任务 1，需用仓库自带的 `backend/.venv`，Python 3.12）。
>
> **实测结果**：本文件 14 个用例全部 PASS（在**未修复**代码上）。同时确认了域点 8
> 列出的四个既有记账测试文件（`test_token_accounting_ledger.py` 65 个用例合计里的一部分 /
> `test_token_accounting_normalize.py` / `test_token_accounting_periods.py` /
> `test_token_period_consistency.py`）合计 65 个用例全部 PASS——这就是 3.5 的基线，
> 本任务未修改这四个文件的任何一行。全量 `backend/tests/` 跑了一遍：`2369 passed, 7 failed`，
> 7 个失败中 4 个是任务 1 的反例 2/3/4/5（预期失败，见上一节），另外 3 个
> （`test_feishu_card_tools.py` 1 条、`test_html_to_pdf.py` 2 条）与本次改动无关——
> 本任务只新增了一个测试文件，未修改任何现有文件，这 3 条是本机环境本就存在的失败
> （缺系统依赖，如 `wkhtmltopdf` / `weasyprint` 一类）。

下表逐条记录每个域点在未修复代码上的基线值（修复后任务 13.2 复跑本文件，只允许两处
显式声明的期望值变化：域点 3 的 `get_value` 调用次数由 2 降为 ≤1；域点 2 里
`group_handoff` 对 `limit == 0` 的结论由放行变为拦截）。

| 域点 | 输入 | 基线值（未修复代码的实测行为） |
|---|---|---|
| 1（3.1 NULL=无限制） | `limit=None` × `used ∈ {0, 1, 10^9}` | 三个 `used` 值下 `verdict.allowed=True`、`blocked_scope is None`；`_budget_gate` 返回 `None`（等价于「completion 会被调用」） |
| 2（3.2 0≠NULL） | `limit=0, used=0` vs `limit=None, used=0` | `limit=0`：`allowed=False, blocked_scope="agent_day", used=0, limit=0`；`limit=None`：`allowed=True, blocked_scope=None`；两者结果确认不同。同一 Agent 上 `group_handoff._target_budget_available(limit=0, used=0)` **今天返回 `True`（放行）**——今天的真值判断把 `0` 当「无上限」，这是任务 7.1 收敛到 `_breach` 语义后会变成拦截的唯一有意行为变更 |
| 3（3.3 零额外往返） | 一个模型步、两阶段判定均放行（Agent 未设任何 token 限额） | `system_setting_dao.get_value` 被调用 **2 次**（两阶段各自调一次 `current_enforcement_mode()`，不显式传 `mode`）；`RuntimeModelStepService._load_budget_subjects` 被调用 **1 次**（`_resolve_budget_subjects` 在两阶段之间共用同一次查询结果） |
| 4（3.4 周期翻页 × 三时区） | `last_daily_reset` 落在 2 天前、`last_monthly_reset` 落在 40 天前，`used` 均设为 999,999（远超 limit），× `timezone ∈ {UTC, Asia/Shanghai, America/New_York}` | 三个时区下均 `verdict.allowed=True, blocked_scope=None`——陈旧计数被 `_effective_used` 视为 0 |
| 5（3.6 fail-open 分级） | `get_value` 分别注入 `TypeError` / `OSError` | `TypeError` → `current_enforcement_mode()` 仍返回 `"warn_only"`，日志含 `ERROR` + `token_budget_enforcement_disabled_bug`；`OSError` → 同样返回 `"warn_only"`，日志含 `WARNING` + `token_budget_enforcement_disabled_transient`。两者生效模式均为 `warn_only` |
| 6（3.7 判定优先级） | 三档同时击穿（`agent_day=100,000/100,000`、`agent_month=1,000,000/1,000,000`、`tenant_day=500,000/500,000`） | `blocked_scope == "agent_day"`。仅 `agent_month`（1,000,000/1,000,000）+ `tenant_day`（500,000/500,000）击穿、`agent_day` 未击穿（`tokens_used_today=0`）时 → `blocked_scope == "agent_month"` |
| 7（3.8 软告警） | `limit=100,000`；`used = floor(limit*0.8) = 80,000` vs `used = floor(limit*0.8) - 1 = 79,999` | `used=80,000` → `soft_warning=True, soft_warning_scope="agent_day", soft_warning_subject_id=<agent.id>`；`used=79,999` → `soft_warning=False` |
| 8（3.5 记账口径，不新增测试） | 既有测试文件当前通过状态 | `test_token_accounting_ledger.py`（23 项）+ `test_token_accounting_normalize.py`（19 项）+ `test_token_accounting_periods.py`（11 项）+ `test_token_period_consistency.py`（12 项），合计 **65 passed, 0 failed**，且未修改这四个文件的任何一行 |

**结论**：8 个域点的基线在未修复代码上全部按预期通过（EXPECTED OUTCOME 兑现）。
本任务未修复任何代码，只交付了测试文件与本节记录的基线值；域点 2 里显式记录了
`group_handoff` 对 `limit==0` 今天「放行」的结论，供任务 7.1/7.2 与任务 13.2 核对。

## 复验记录（任务 10.1、11.x 填写）

> 格式：`条目 | 执行命令 | 实测输出 | 结论对 design 的影响`

（待有库环境时填写）
