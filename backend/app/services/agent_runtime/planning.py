"""Planning v2 model contract and terminal checkpoint transition."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from typing import Protocol, cast
import uuid

from app.models.llm import LLMModel
from app.services.agent_runtime.command_worker import RuntimeSessionFactory
from app.services.agent_runtime.model_capabilities import (
    ModelCapabilityError,
    ModelCapabilityResolver,
)
from app.services.agent_runtime.node_executor import (
    RuntimeCancelSource,
    RuntimeInvocationCancelled,
)
from app.services.agent_runtime.state import (
    JsonObject,
    JsonValue,
    RuntimeContext,
    RuntimeGraphState,
    RuntimeLifecycle,
    RuntimeNodeExecutor,
    RuntimeNodeName,
    RuntimeStateUpdate,
)
from loguru import logger

from app.services.llm.client import LLMMessage
from app.services.llm.single_step import LLMCompletionStep, complete_llm_once
from app.services.llm.model_resolution import load_active_model
from app.services.llm.utils import get_max_tokens
from app.services.token_accounting.budget import (
    PROGRAMMING_ERROR_TYPES,
    budget_exceeded_message,
)
from app.services.token_accounting.gate import (
    LANE_PLANNING,
    BudgetClearance,
    BudgetSubjects,
    check as gate_check,
    clearance_from,
    load_subjects,
)
from app.services.token_accounting.ledger import SYSTEM_SCOPE_PLANNING


_PLANNING_ROLE = "group_planning"
_PLAN_VERSION = 2
_PLAN_MODES = frozenset({"advisory", "enforced"})
_MAX_ENTRY_STEPS = 50
_PLAN_FIELDS = frozenset({"version", "mode", "goal", "plan_prompt", "entry_steps"})
_ENTRY_FIELDS = frozenset({"agent_id", "instruction"})
_SIMPLE_CHECK_INS = frozenset(
    {
        "在吗",
        "在嘛",
        "在么",
        "在不在",
        "都在吗",
        "都在嘛",
        "你们在吗",
        "你们在嘛",
        "有人吗",
        "你好",
        "你好呀",
        "你们好",
        "大家好",
        "嗨",
        "哈喽",
        "哈啰",
        "hi",
        "hello",
        "hey",
    }
)
_CHECK_IN_PUNCTUATION = re.compile(r"[\s,，.!！?？。:：;；~～、]+")

_SYSTEM_PROMPT = """You are Clawith's internal multi-Agent planning component.
Return exactly one JSON object and no Markdown. Never call tools and never do the work yourself.
Use only candidate agent_id values supplied by the caller.
Return exactly this schema and no additional fields:
{
  "version": 2,
  "mode": "advisory | enforced",
  "goal": "collaboration goal",
  "plan_prompt": "complete plan, roles, transitions, branches, and completion rules",
  "entry_steps": [
    {
      "agent_id": "candidate UUID",
      "instruction": "this entry Agent's current responsibility"
    }
  ]
}
Set mode to enforced only when the human explicitly specified workflow constraints such as Agent assignments, order, rounds, dependencies, branches, or completion conditions. Otherwise use advisory.
Use the simplest plan that satisfies the human's actual request. Do not invent analysis, synthesis, status reporting, review, or collaboration merely because several Agents were mentioned.
Before arranging any work, silently rewrite user_goal into clear directives in the original mention order. For each directive, fix the exact Agent, action, input, expected public output, and any dependency or next Agent. Then build goal, plan_prompt, and entry_steps only from those normalized directives; do not return the rewrite as an extra field.
Bind an instruction after an @mentioned Agent to that Agent until the next @mention, unless the human explicitly says otherwise. Never swap or reassign that work based on candidate order, Agent name, role_description, or perceived capability. For example, "@A write a poem @B then translate it" means A writes the poem, publicly hands that poem to B, and B translates it; it never means B writes and A translates.
When wording is vague, make the smallest literal interpretation explicit in goal, plan_prompt, and entry instructions before scheduling. Preserve every unambiguous Agent-to-responsibility binding, and never resolve ambiguity by moving work to a different Agent.
When repairing an invalid previous output, repeat this normalization from the original user_goal and verify every Agent-to-responsibility binding before returning corrected JSON. Do not merely repair JSON syntax or preserve a semantically wrong assignment from previous_output.
For a greeting or check-in, start the addressed Agents in parallel and tell each one to reply briefly as itself. Do not ask one Agent to report another Agent's status, unify their greetings, or exchange public handoffs.
entry_steps starts only the first Agent or first parallel Agents. It may be a subset of candidates. Do not describe a DAG, step IDs, dependencies, progress, or later scheduling fields. Later collaboration proceeds through public Agent handoffs.
Create a public handoff only when a different Agent must provide a new reply for the task to proceed. Never create a handoff from an Agent to itself.
Each assigned Agent must author its own public group reply. Never route a planned group transition through private A2A, never ask an entry Agent to wait for a private result, and never ask one Agent to perform or claim another Agent's assigned work.
For every sequential transition, plan_prompt and the responsible entry instruction must say exactly which different Agent to wake publicly next, what concrete result to pass in the public group message, and what that Agent must reply with in the group.
plan_prompt must be complete enough for every later participating Agent to receive unchanged. Preserve the human's explicit constraints, but do not repeat platform rules or invent mandatory constraints."""


class PlanningContractError(RuntimeError):
    """Planning data or transitions violate the checkpoint contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PlanningModelResult:
    """One side-effect-free planning call outcome."""

    plan: JsonObject | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw_output: str | None = None
    retryable: bool = False


class PlanningCompletionPort(Protocol):
    async def __call__(
        self,
        model: LLMModel,
        messages: list[LLMMessage],
        *,
        tools: list[dict] | None = None,
        agent_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
        system_scope: str | None = None,
        supports_vision: bool = False,
        clearance: BudgetClearance,
    ) -> LLMCompletionStep: ...


def _required_text(value: object, *, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningContractError("invalid_plan", f"{field} must not be blank")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise PlanningContractError(
            "invalid_plan",
            f"{field} exceeds {max_length} characters",
        )
    return normalized


def _require_exact_fields(
    value: Mapping[object, object],
    *,
    expected: frozenset[str],
    field: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unsupported = sorted(str(key) for key in actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unsupported:
            details.append("unsupported " + ", ".join(unsupported))
        raise PlanningContractError(
            "invalid_plan",
            f"{field} must use the exact Planning v2 fields ({'; '.join(details)})",
        )


def _uuid_text(value: object, *, field: str) -> uuid.UUID:
    if not isinstance(value, str):
        raise PlanningContractError("invalid_plan", f"{field} must be a UUID string")
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise PlanningContractError(
            "invalid_plan",
            f"{field} must be a UUID string",
        ) from exc


def _candidate_agent_ids(state: RuntimeGraphState) -> frozenset[uuid.UUID]:
    candidates = state["snapshots"].initial_input.get("candidate_agents")
    if not isinstance(candidates, Sequence) or isinstance(
        candidates,
        (str, bytes, bytearray),
    ):
        raise PlanningContractError(
            "invalid_planning_input",
            "candidate_agents must be an array",
        )
    resolved: list[uuid.UUID] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise PlanningContractError(
                "invalid_planning_input",
                "candidate_agents entries must be objects",
            )
        try:
            agent_id = uuid.UUID(str(candidate.get("agent_id")))
        except (TypeError, ValueError) as exc:
            raise PlanningContractError(
                "invalid_planning_input",
                "candidate agent_id must be a UUID",
            ) from exc
        resolved.append(agent_id)
    if len(resolved) < 2 or len(set(resolved)) != len(resolved):
        raise PlanningContractError(
            "invalid_planning_input",
            "Planning requires at least two distinct candidate Agents",
        )
    return frozenset(resolved)


def _simple_check_in_plan(
    state: RuntimeGraphState,
    *,
    goal: str,
    candidate_agent_ids: frozenset[uuid.UUID],
) -> JsonObject | None:
    """Return a deterministic one-reply-per-Agent plan for exact greetings."""
    raw_candidates = state["snapshots"].initial_input.get("candidate_agents")
    if not isinstance(raw_candidates, Sequence) or isinstance(
        raw_candidates,
        (str, bytes, bytearray),
    ):
        return None

    candidates: list[tuple[uuid.UUID, str]] = []
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, Mapping):
            return None
        try:
            agent_id = uuid.UUID(str(raw_candidate.get("agent_id")))
        except (TypeError, ValueError):
            return None
        raw_name = raw_candidate.get("name")
        if (
            agent_id not in candidate_agent_ids
            or not isinstance(raw_name, str)
            or not raw_name.strip()
        ):
            return None
        candidates.append((agent_id, raw_name.strip()))

    remaining = goal
    for _, name in sorted(candidates, key=lambda candidate: len(candidate[1]), reverse=True):
        remaining = remaining.replace(f"@{name}", " ")
    normalized = _CHECK_IN_PUNCTUATION.sub("", remaining).casefold()
    if normalized not in _SIMPLE_CHECK_INS:
        return None

    plan: JsonObject = {
        "version": _PLAN_VERSION,
        "mode": "advisory",
        "goal": "Each mentioned Agent replies briefly to the user's greeting or check-in as itself.",
        "plan_prompt": (
            "This is a simple greeting or check-in. Every entry Agent replies once, "
            "briefly, and only as itself. Do not report another Agent's status, do not "
            "ask another Agent to reply, and do not create a public handoff."
        ),
        "entry_steps": [
            {
                "agent_id": str(agent_id),
                "instruction": (
                    f"Reply briefly to the user's greeting or check-in as {name} only. "
                    "Do not report another Agent's status and do not mention or hand off "
                    "to another Agent."
                ),
            }
            for agent_id, name in candidates
        ],
    }
    return validate_planning_output(
        plan,
        candidate_agent_ids=candidate_agent_ids,
    )


def validate_planning_output(
    raw: object,
    *,
    candidate_agent_ids: frozenset[uuid.UUID],
) -> JsonObject:
    """Validate only Planning v2 structure and candidate scope."""
    if not isinstance(raw, Mapping):
        raise PlanningContractError("invalid_plan", "Planning output must be an object")
    _require_exact_fields(raw, expected=_PLAN_FIELDS, field="Planning output")
    if raw.get("version") != _PLAN_VERSION:
        raise PlanningContractError("invalid_plan", "Planning output version must be 2")
    mode = raw.get("mode")
    if mode not in _PLAN_MODES:
        raise PlanningContractError(
            "invalid_plan",
            "mode must be advisory or enforced",
        )
    goal = _required_text(raw.get("goal"), field="goal", max_length=10_000)
    plan_prompt = _required_text(
        raw.get("plan_prompt"),
        field="plan_prompt",
        max_length=40_000,
    )
    raw_entries = raw.get("entry_steps")
    if (
        not isinstance(raw_entries, Sequence)
        or isinstance(raw_entries, (str, bytes, bytearray))
        or not raw_entries
        or len(raw_entries) > _MAX_ENTRY_STEPS
    ):
        raise PlanningContractError(
            "invalid_plan",
            f"entry_steps must contain between 1 and {_MAX_ENTRY_STEPS} entries",
        )

    entries: list[JsonObject] = []
    seen_agents: set[uuid.UUID] = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping):
            raise PlanningContractError(
                "invalid_plan",
                "each entry_steps item must be an object",
            )
        _require_exact_fields(
            raw_entry,
            expected=_ENTRY_FIELDS,
            field=f"entry_steps[{index}]",
        )
        agent_id = _uuid_text(
            raw_entry.get("agent_id"),
            field=f"entry_steps[{index}].agent_id",
        )
        if agent_id not in candidate_agent_ids:
            raise PlanningContractError(
                "invalid_plan",
                "entry agent_id is not one of the mentioned candidate Agents",
            )
        if agent_id in seen_agents:
            raise PlanningContractError(
                "invalid_plan",
                "entry agent_id values must be unique",
            )
        seen_agents.add(agent_id)
        instruction = _required_text(
            raw_entry.get("instruction"),
            field=f"entry_steps[{index}].instruction",
            max_length=20_000,
        )
        entries.append(
            {
                "agent_id": str(agent_id),
                "instruction": instruction,
            }
        )

    return {
        "version": _PLAN_VERSION,
        "mode": cast(str, mode),
        "goal": goal,
        "plan_prompt": plan_prompt,
        "entry_steps": entries,
    }


def _parse_json_output(content: str | None) -> object:
    if content is None or not content.strip():
        raise PlanningContractError("invalid_plan", "Planning model returned no content")
    value = content.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1])
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise PlanningContractError(
            "invalid_plan",
            "Planning model output is not valid JSON",
        ) from exc


class PlanningModelService:
    """Call the pinned Group-tenant or platform model without fallback."""

    def __init__(
        self,
        *,
        session_factory: RuntimeSessionFactory,
        completion: PlanningCompletionPort = complete_llm_once,  # type: ignore[assignment]
    ) -> None:
        self._session_factory = session_factory
        self._completion = completion

    async def _load_model(self, context: RuntimeContext) -> tuple[LLMModel, uuid.UUID]:
        try:
            model_id = uuid.UUID(context.model_id)
            tenant_id = uuid.UUID(context.tenant_id)
        except ValueError as exc:
            raise PlanningContractError(
                "planning_model_unavailable",
                "Planning Run has an invalid pinned model",
            ) from exc
        async with self._session_factory() as db:
            model = await load_active_model(
                db,
                model_id=model_id,
                tenant_id=tenant_id,
            )
        if model is None:
            raise PlanningContractError(
                "planning_model_unavailable",
                "Pinned Planning model is not enabled for this Group tenant",
            )
        try:
            ModelCapabilityResolver.request_input_limit(
                model,
                requested_max_output_tokens=get_max_tokens(
                    model.provider,
                    model.model,
                    model.max_output_tokens,
                ),
            )
        except ModelCapabilityError as exc:
            raise PlanningContractError(
                "planning_model_capability_invalid",
                "Pinned Planning model has no safe input budget",
            ) from exc
        # Return the already-validated tenant_id so callers don't re-parse
        # context.tenant_id inside a try/except that would otherwise
        # misclassify a future parse failure as a retryable LLM call failure.
        return model, tenant_id

    async def _resolve_budget_subjects(
        self,
        context: RuntimeContext,
        tenant_id: uuid.UUID,
    ) -> BudgetSubjects | None:
        """一次性取好限额判定要用的主体（任务 6.3）。

        Planning 是租户级判定，只判 `tenant_day` 一档（`agent=None`，依赖任务 3.1），
        用 `self._session_factory` 单独开一次会话 —— 与任务 6.1 / 6.2 不同的是，
        `_load_model` 已经把它自己开的会话关掉了，这里不能顺带查，必须重新开一次。

        返回 `None` 表示开会话或加载本身失败（不是"判定输入缺失"，是"取判定输入这个
        动作本身失败"）——按异常类型分类记日志，判据与
        `model_step_service._resolve_budget_subjects` 完全一致：
        `PROGRAMMING_ERROR_TYPES`（签名漂移等代码 bug）吵到 ERROR，其余
        （基础设施/瞬时故障，包括 `session_factory()` 打开会话本身失败）落 WARNING，
        两者都 fail-open——放行而不阻塞 Planning（3.6）。调用方据此构造
        `BudgetClearance.not_applicable(...)`，不做拦截。
        """
        try:
            async with self._session_factory() as db:
                return await load_subjects(db, tenant_id=tenant_id, agent=None)
        except PROGRAMMING_ERROR_TYPES as exc:
            logger.opt(exception=True).error(
                "[TokenBudget] token_budget_enforcement_disabled_bug run_id={} lane={} error={!r}",
                context.run_id,
                LANE_PLANNING,
                exc,
            )
            return None
        except Exception as exc:  # noqa: BLE001 - 加载失败不能拖垮 Planning 调用（基础设施/瞬时故障）
            logger.warning(
                "[TokenBudget] token_budget_enforcement_disabled_transient run_id={} lane={} error={!r}",
                context.run_id,
                LANE_PLANNING,
                exc,
            )
            return None

    async def complete_once(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
    ) -> PlanningModelResult:
        try:
            candidates = _candidate_agent_ids(state)
        except PlanningContractError as exc:
            return PlanningModelResult(
                error_code=exc.code,
                error_message=str(exc),
                retryable=False,
            )
        simple_plan = _simple_check_in_plan(
            state,
            goal=context.goal,
            candidate_agent_ids=candidates,
        )
        if simple_plan is not None:
            return PlanningModelResult(plan=simple_plan)
        try:
            model, tenant_id = await self._load_model(context)
        except PlanningContractError as exc:
            return PlanningModelResult(
                error_code=exc.code,
                error_message=str(exc),
                retryable=False,
            )

        # 限额判定（任务 6.3）：Planning 调用会按租户当日计数记账
        # （`system_scope=SYSTEM_SCOPE_PLANNING`），必须在真正发起 provider 请求
        # （下面的 `self._completion(...)`）之前完成一次「击穿判定」
        # （`estimated_next_round_tokens=0`，只做判定不做预算预扣——超限幅度有界
        # 由 business_step 自己的两阶段估算保证）。`run_id` 传 `context.run_id`
        # （与 6.1 一致；这条链路确实有 run_id，不同于 6.2 的 session/group compact
        # 没有 run_id 概念的场景）。
        #
        # `subjects is None` 时不判定、直接放行：与任务 6.1 / 6.2 的设计决定一致——
        # 判定所需的输入缺失（这里特指 `_resolve_budget_subjects` 开会话或加载本身
        # 失败）不等于判定跑过并拒绝。理由已在 `run_compactor.compact_if_needed`
        # 里完整展开，此处不重复论证：让缺失主体 fail closed 会把一次基础设施故障
        # 伪装成一次限额拦截，制造更难查的假阳性；而放行则与 3.6 的 fail-open 判据
        # 完全一致（`_resolve_budget_subjects` 内部已经按异常类型分级记过日志）。
        subjects = await self._resolve_budget_subjects(context, tenant_id)
        if subjects is not None:
            verdict = await gate_check(
                lane=LANE_PLANNING,
                subjects=subjects,
                estimated_next_round_tokens=0,
                run_id=context.run_id,
            )
            if not verdict.allowed:
                # `retryable=False`：命中限额是一个确定性结果（判定输入没变，
                # 立刻重试只会拿到同样的拒绝），不属于 3.6 fail-open 覆盖的
                # "基础设施/瞬时故障"范畴，`_model` 节点据此直接终止 Planning
                # Run 而不进入修复循环（`attempt <= self._max_repairs`）；
                # `planning_scheduler` 消费 `error_code` 作为 `failure_code`
                # 时同样不区分 retryable，终止即最终结果——已用测试
                # （`test_agent_runtime_planning_scheduler.py`）钉住这个映射,
                # 并确认既有重试逻辑（Planning 修复循环）不会把它当瞬时故障重试。
                return PlanningModelResult(
                    error_code="token_budget_exceeded",
                    error_message=budget_exceeded_message(verdict),
                    retryable=False,
                )
            clearance = clearance_from(LANE_PLANNING, verdict)
        else:
            clearance = BudgetClearance.not_applicable(
                LANE_PLANNING,
                reason="planning budget subjects unavailable (session_factory or load failed)",
            )

        planning_state = state["lifecycle"].get("planning")
        repair_context = None
        if isinstance(planning_state, Mapping) and planning_state.get("last_error"):
            repair_context = {
                "previous_output": planning_state.get("last_raw_output"),
                "validation_error": planning_state.get("last_error"),
            }
        request = {
            "user_goal": context.goal,
            "candidate_agents": state["snapshots"].initial_input.get(
                "candidate_agents",
                [],
            ),
            "explicit_user_plan_has_priority": True,
            "repair": repair_context,
        }
        messages = [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=json.dumps(request, ensure_ascii=False, sort_keys=True),
            ),
        ]
        try:
            completion = await self._completion(
                model,
                messages,
                tools=None,
                agent_id=None,
                tenant_id=tenant_id,
                system_scope=SYSTEM_SCOPE_PLANNING,
                supports_vision=False,
                clearance=clearance,
            )
        except Exception:
            return PlanningModelResult(
                error_code="planning_model_call_failed",
                error_message="Planning model call failed",
                retryable=True,
            )
        if completion.tool_calls:
            return PlanningModelResult(
                error_code="invalid_plan",
                error_message="Planning model attempted to call a tool",
                raw_output=completion.content,
                retryable=True,
            )
        try:
            plan = validate_planning_output(
                _parse_json_output(completion.content),
                candidate_agent_ids=candidates,
            )
        except PlanningContractError as exc:
            return PlanningModelResult(
                error_code=exc.code,
                error_message=str(exc),
                raw_output=completion.content,
                retryable=True,
            )
        return PlanningModelResult(plan=plan, raw_output=completion.content)


def checkpoint_plan(state: RuntimeGraphState) -> JsonObject:
    """Revalidate the immutable v2 plan against its frozen candidate scope."""
    planning = state["lifecycle"].get("planning")
    if not isinstance(planning, Mapping):
        raise PlanningContractError(
            "invalid_planning_checkpoint",
            "Planning checkpoint has no v2 plan",
        )
    try:
        return validate_planning_output(
            planning,
            candidate_agent_ids=_candidate_agent_ids(state),
        )
    except PlanningContractError as exc:
        raise PlanningContractError(
            "invalid_planning_checkpoint",
            f"Planning checkpoint plan is invalid: {exc}",
        ) from exc


class PlanningRuntimeNodeExecutor:
    """Produce one immutable v2 plan and terminate the Planning Run."""

    def __init__(
        self,
        *,
        cancel_source: RuntimeCancelSource,
        model_service: PlanningModelService,
        max_repairs: int = 2,
    ) -> None:
        if max_repairs < 0:
            raise ValueError("max_repairs must not be negative")
        self._cancel_source = cancel_source
        self._model_service = model_service
        self._max_repairs = max_repairs

    @staticmethod
    def _require_planning_run(context: RuntimeContext) -> None:
        if context.system_role != _PLANNING_ROLE or context.agent_id is not None:
            raise PlanningContractError(
                "planning_identity_mismatch",
                "Planning executor requires the group_planning system Run",
            )

    async def _control(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
    ) -> RuntimeStateUpdate:
        lifecycle = dict(state["lifecycle"])
        if lifecycle["status"] in {"completed", "failed", "cancelled"}:
            lifecycle["next_route"] = "terminal"
            return {"lifecycle": cast(RuntimeLifecycle, lifecycle)}
        cancel = await self._cancel_source.get_cancel(state, context)
        if cancel is not None:
            if not cancel.command_id:
                raise PlanningContractError(
                    "invalid_cancel_command",
                    "cancel command ID must not be blank",
                )
            raise RuntimeInvocationCancelled(cancel)
        return {"lifecycle": cast(RuntimeLifecycle, lifecycle)}

    async def _model(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
    ) -> RuntimeStateUpdate:
        lifecycle = dict(state["lifecycle"])
        attempt = lifecycle.get("planning_attempt_count", 0)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raise PlanningContractError(
                "invalid_planning_checkpoint",
                "planning_attempt_count must be a non-negative integer",
            )
        attempt += 1
        result = await self._model_service.complete_once(state, context)
        lifecycle["planning_attempt_count"] = attempt
        if result.plan is not None:
            lifecycle.update(
                {
                    "status": "completed",
                    "next_route": "terminal",
                    "reason": "planning_v2_ready",
                    "planning": dict(result.plan),
                    "waiting_request": None,
                    "error": None,
                }
            )
            return {"lifecycle": cast(RuntimeLifecycle, lifecycle)}

        error_code = result.error_code or "planning_failed"
        error_message = result.error_message or "Planning did not produce a valid plan"
        lifecycle["planning"] = {
            "repair_count": attempt,
            "last_error": error_message,
            "last_raw_output": result.raw_output,
        }
        if result.retryable and attempt <= self._max_repairs:
            lifecycle.update(
                {
                    "status": "running",
                    "next_route": "model",
                    "reason": "planning_repair_required",
                }
            )
            return {"lifecycle": cast(RuntimeLifecycle, lifecycle)}
        lifecycle.update(
            {
                "status": "failed",
                "next_route": "terminal",
                "reason": error_code,
                "error": {"code": error_code, "message": error_message},
                "waiting_request": None,
            }
        )
        return {"lifecycle": cast(RuntimeLifecycle, lifecycle)}

    async def execute(
        self,
        node: RuntimeNodeName,
        state: RuntimeGraphState,
        context: RuntimeContext,
        *,
        resume_value: JsonValue | None = None,
    ) -> RuntimeStateUpdate:
        del resume_value
        self._require_planning_run(context)
        if node == "control_guard":
            return await self._control(state, context)
        if node == "model":
            return await self._model(state, context)
        if node == "terminal":
            return {"lifecycle": dict(state["lifecycle"])}
        raise PlanningContractError(
            "invalid_planning_route",
            f"Planning Graph cannot execute {node}",
        )


class RuntimeNodeExecutorRouter:
    """Select a node implementation from immutable checkpoint identity."""

    def __init__(
        self,
        *,
        agent_executor: RuntimeNodeExecutor,
        planning_executor: RuntimeNodeExecutor,
    ) -> None:
        self._agent_executor = agent_executor
        self._planning_executor = planning_executor

    async def execute(
        self,
        node: RuntimeNodeName,
        state: RuntimeGraphState,
        context: RuntimeContext,
        *,
        resume_value: JsonValue | None = None,
    ) -> RuntimeStateUpdate:
        executor = self._planning_executor if context.system_role == _PLANNING_ROLE else self._agent_executor
        return await executor.execute(
            node,
            state,
            context,
            resume_value=resume_value,
        )


__all__ = [
    "PlanningContractError",
    "PlanningModelResult",
    "PlanningModelService",
    "PlanningRuntimeNodeExecutor",
    "RuntimeNodeExecutorRouter",
    "checkpoint_plan",
    "validate_planning_output",
]
