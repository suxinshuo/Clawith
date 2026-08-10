"""One-call LLM provider boundary for checkpointed Runtime nodes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
import uuid

from app.services.token_accounting.gate import BudgetClearance
from app.services.token_accounting.ledger import record as record_token_usage_ledger
from app.services.token_accounting.normalize import (
    PROTOCOL_OPENAI_COMPATIBLE,
    TokenUsage,
    usage_from_response_or_estimate,
)
from app.services.token_tracker import record_token_usage

from .caller import (
    _convert_messages_for_vision,
    _get_model_timeout,
    _sanitize_tool_calls_for_context,
)
from .client import LLMMessage, get_provider_spec
from .utils import create_llm_client, get_max_tokens, get_model_api_key

if TYPE_CHECKING:
    from app.models.llm import LLMModel


@dataclass(frozen=True, slots=True)
class LLMCompletionStep:
    """One normalized provider response with no tool or lifecycle side effects."""

    content: str | None
    tool_calls: tuple[dict, ...]
    reasoning_content: str | None
    retry_instruction: str | None
    usage: TokenUsage
    retry_tool_name: str | None = None


async def complete_llm_once(
    model: LLMModel,
    messages: list[LLMMessage],
    *,
    tools: list[dict] | None = None,
    agent_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    system_scope: str | None = None,
    supports_vision: bool = False,
    clearance: BudgetClearance,
) -> LLMCompletionStep:
    """Call one pinned model exactly once and normalize its tool proposals.

    This function never executes tools, retries, appends repair prompts, or
    advances a lifecycle. Those decisions belong to the durable Graph.

    Usage attribution: pass ``agent_id`` alone for the per-agent chat path,
    which resolves its own tenant. Callers with no owning Agent (group
    compaction, planning, connectivity probes) must pass ``tenant_id`` and
    ``system_scope`` instead, or their usage is dropped rather than recorded.

    ``clearance`` is a required keyword argument by design (see design.md
    变更 4): every caller must state whether a budget verdict was checked
    before reaching this provider boundary, or explicitly declare that budget
    enforcement does not apply here (``BudgetClearance.not_applicable``).
    Passing a clearance whose verdict is a denial (``verdict.allowed is
    False``) and still calling this function is a programming error - the
    caller should have short-circuited before ever reaching this boundary.
    """
    if clearance.verdict is not None and not clearance.verdict.allowed:
        raise RuntimeError("budget_clearance_violation")
    if system_scope is not None and tenant_id is None:
        # system_scope only makes sense against the ledger's tenant-level
        # scope, and the legacy branch below has no tenant to check it
        # against — it would record against agent_id (if any) and silently
        # discard system_scope. Fail loudly instead, mirroring
        # ledger.record's own exactly-one-of guard.
        raise ValueError("complete_llm_once requires tenant_id when system_scope is given")
    api_messages = _convert_messages_for_vision(messages, supports_vision)
    client = create_llm_client(
        provider=model.provider,
        api_key=get_model_api_key(model),
        model=model.model,
        base_url=model.base_url,
        timeout=_get_model_timeout(model),
    )
    try:
        response = await client.complete(
            messages=api_messages,
            tools=tools or None,
            temperature=model.temperature,
            max_tokens=get_max_tokens(
                model.provider,
                model.model,
                getattr(model, "max_output_tokens", None),
            ),
        )
    finally:
        await client.close()

    spec = get_provider_spec(model.provider)
    # model.provider 不在 registry 里时，create_llm_client 仍会退到
    # OpenAICompatibleClient（见 app/services/llm/client.py 里 "Default to
    # OpenAI-compatible for unknown providers" 分支），记账必须假定同一种协议
    # 形状，否则 usage_from_response_or_estimate 会因未知协议拿不到归一化结果
    # 而退回字符估算，悄悄丢失 provider 权威的计数。
    protocol = spec.protocol if spec is not None else PROTOCOL_OPENAI_COMPATIBLE
    usage = usage_from_response_or_estimate(
        protocol,
        response.usage,
        [{"role": message.role, "content": message.content} for message in api_messages],
        response.content,
    )
    if usage.total_tokens > 0:
        if tenant_id is not None:
            # A caller supplied a tenant explicitly (with agent_id and/or
            # system_scope for attribution) — record straight through the
            # ledger; it enforces exactly-one-of agent_id/system_scope.
            await record_token_usage_ledger(
                usage,
                tenant_id=tenant_id,
                agent_id=agent_id,
                system_scope=system_scope,
            )
        elif agent_id is not None:
            # Legacy per-agent path: no tenant given, resolve it from the
            # Agent. Only the direct per-agent chat path still takes this
            # branch — group compaction, planning, and connectivity probes
            # now pass tenant_id and land in the branch above instead.
            await record_token_usage(agent_id, usage)

    sanitized_tool_calls: list[dict] | None = []
    retry_instruction = None
    retry_tool_name = None
    if response.tool_calls:
        sanitized_tool_calls, retry_instruction, retry_tool_name = (
            _sanitize_tool_calls_for_context(response.tool_calls)
        )
    return LLMCompletionStep(
        content=response.content,
        tool_calls=tuple(sanitized_tool_calls or ()),
        reasoning_content=response.reasoning_content,
        retry_instruction=retry_instruction,
        usage=usage,
        retry_tool_name=retry_tool_name,
    )


__all__ = ["LLMCompletionStep", "complete_llm_once"]
