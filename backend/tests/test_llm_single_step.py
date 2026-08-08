"""One-call LLM provider boundary tests for the durable Runtime."""

import uuid
from types import SimpleNamespace

import pytest

from app.services.llm import single_step
from app.services.llm.client import (
    AnthropicClient,
    GeminiClient,
    LLMMessage,
    LLMResponse,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
)

_TINY_PNG_DATA_URL = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2ZQAAAABJRU5ErkJggg=="
)


class _Client:
    def __init__(self, response: LLMResponse | Exception) -> None:
        self.response = response
        self.calls = []
        self.closed = False

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def close(self) -> None:
        self.closed = True


def _model():
    return SimpleNamespace(
        provider="openai",
        model="runtime-model",
        base_url="https://example.invalid",
        request_timeout=17,
        temperature=0.2,
        max_output_tokens=1024,
    )


def _patch_client(monkeypatch, client: _Client) -> None:
    monkeypatch.setattr(single_step, "create_llm_client", lambda **kwargs: client)
    monkeypatch.setattr(single_step, "get_model_api_key", lambda model: "secret")
    monkeypatch.setattr(single_step, "get_max_tokens", lambda *args: 1024)


def test_native_gemini_preserves_dynamic_system_context_once() -> None:
    client = GeminiClient(api_key="test", model="gemini-test")

    payload = client._build_payload(
        [
            LLMMessage(
                role="system",
                content="Static Base Prompt",
                dynamic_content="Dynamic Runtime Context",
            ),
            LLMMessage(role="user", content="Do the task"),
        ],
        tools=None,
        temperature=0.2,
        max_tokens=1024,
    )

    system_text = payload["systemInstruction"]["parts"][0]["text"]
    assert system_text.count("Static Base Prompt") == 1
    assert system_text.count("Dynamic Runtime Context") == 1
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "Do the task"}]}]


def test_provider_payloads_preserve_static_and_dynamic_system_context_once() -> None:
    messages = [
        LLMMessage(
            role="system",
            content="Static Base Prompt",
            dynamic_content="Dynamic Runtime Context",
        ),
        LLMMessage(role="user", content="Do the task"),
    ]
    openai_payload = OpenAICompatibleClient(
        api_key="test",
        model="openai-test",
    )._build_payload(messages, None, 0.2, 1024)
    responses_payload = OpenAIResponsesClient(
        api_key="test",
        model="responses-test",
    )._build_payload(messages, None, 0.2, 1024)
    anthropic_payload = AnthropicClient(
        api_key="test",
        model="anthropic-test",
    )._build_payload(messages, None, 0.2, 1024)
    gemini_payload = GeminiClient(
        api_key="test",
        model="gemini-test",
    )._build_payload(messages, None, 0.2, 1024)

    serialized_systems = (
        str(openai_payload["messages"][0]["content"]),
        str(responses_payload["input"][0]["content"]),
        "\n".join(block["text"] for block in anthropic_payload["system"]),
        gemini_payload["systemInstruction"]["parts"][0]["text"],
    )
    for system_content in serialized_systems:
        assert system_content.count("Static Base Prompt") == 1
        assert system_content.count("Dynamic Runtime Context") == 1


@pytest.mark.asyncio
async def test_complete_once_normalizes_tools_and_records_usage_without_executing_them(
    monkeypatch,
) -> None:
    client = _Client(
        LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": "notes.md"},
                    },
                }
            ],
            reasoning_content="inspect the file",
            usage={
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
            },
        )
    )
    _patch_client(monkeypatch, client)
    recorded = []

    async def record(agent_id, usage):
        recorded.append((agent_id, usage))

    monkeypatch.setattr(single_step, "record_token_usage", record)
    agent_id = uuid.uuid4()
    messages = [LLMMessage(role="user", content="Read notes")]
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    result = await single_step.complete_llm_once(
        _model(),
        messages,
        tools=tools,
        agent_id=agent_id,
    )

    assert result.content == ""
    assert result.reasoning_content == "inspect the file"
    assert result.retry_instruction is None
    assert result.tool_calls == (
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path": "notes.md"}',
            },
        },
    )
    assert result.usage.total_tokens == 25
    assert len(client.calls) == 1
    assert client.calls[0]["messages"] == messages
    assert client.calls[0]["tools"] == tools
    assert client.closed is True
    assert recorded[0][0] == agent_id
    assert recorded[0][1].total_tokens == 25


@pytest.mark.asyncio
async def test_complete_once_with_tenant_and_system_scope_records_to_the_ledger_not_the_agent(
    monkeypatch,
) -> None:
    """群聊压缩/规划/连通性测试都只传 tenant_id+system_scope，不传 agent_id.

    这条路径此前完全不记账；现在必须走 ledger.record，而不是走
    legacy 的按 agent 解析租户的 record_token_usage。
    """
    client = _Client(
        LLMResponse(
            content="done",
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 4,
                "total_tokens": 15,
            },
        )
    )
    _patch_client(monkeypatch, client)
    ledger_calls = []
    legacy_calls = []

    async def record_ledger(usage, *, tenant_id, agent_id=None, system_scope=None):
        ledger_calls.append((usage, tenant_id, agent_id, system_scope))
        return True

    async def record_legacy(agent_id, usage):
        legacy_calls.append((agent_id, usage))

    monkeypatch.setattr(single_step, "record_token_usage_ledger", record_ledger)
    monkeypatch.setattr(single_step, "record_token_usage", record_legacy)
    tenant_id = uuid.uuid4()

    result = await single_step.complete_llm_once(
        _model(),
        [LLMMessage(role="user", content="Compact this")],
        tenant_id=tenant_id,
        system_scope="group_compact",
    )

    assert result.usage.total_tokens == 15
    assert len(ledger_calls) == 1
    recorded_usage, recorded_tenant, recorded_agent, recorded_scope = ledger_calls[0]
    assert recorded_usage.total_tokens == 15
    assert recorded_tenant == tenant_id
    assert recorded_agent is None
    assert recorded_scope == "group_compact"
    assert legacy_calls == []


@pytest.mark.asyncio
async def test_complete_once_still_records_the_direct_agent_path_when_no_tenant_is_given(
    monkeypatch,
) -> None:
    """确认新增的 tenant_id/system_scope 参数没有把按 Agent 记账的直聊路径静默改道。"""
    client = _Client(
        LLMResponse(
            content="done",
            usage={
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
            },
        )
    )
    _patch_client(monkeypatch, client)
    ledger_calls = []
    legacy_calls = []

    async def record_ledger(usage, *, tenant_id, agent_id=None, system_scope=None):
        ledger_calls.append((usage, tenant_id, agent_id, system_scope))
        return True

    async def record_legacy(agent_id, usage):
        legacy_calls.append((agent_id, usage))

    monkeypatch.setattr(single_step, "record_token_usage_ledger", record_ledger)
    monkeypatch.setattr(single_step, "record_token_usage", record_legacy)
    agent_id = uuid.uuid4()

    await single_step.complete_llm_once(
        _model(),
        [LLMMessage(role="user", content="Chat")],
        agent_id=agent_id,
    )

    assert legacy_calls == [(agent_id, legacy_calls[0][1])]
    assert legacy_calls[0][1].total_tokens == 10
    assert ledger_calls == []


@pytest.mark.asyncio
async def test_complete_once_records_provider_authoritative_usage_for_an_unregistered_provider(
    monkeypatch,
) -> None:
    """model.provider 不在 PROVIDER_REGISTRY 里时，create_llm_client 仍会退到 OpenAICompatibleClient。

    见 app/services/llm/client.py 的 "Default to OpenAI-compatible for
    unknown providers" 分支：它返回真实 usage。记账必须假定同一种协议形状；
    用空协议 `""` 兜底会让 normalize() 对未知协议返回 None，
    usage_from_response_or_estimate 转而退回字符估算——total_tokens 不再等于
    provider 权威的计数，estimated_tokens 也不再是 0。
    """
    client = _Client(
        LLMResponse(
            content="done",
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 4,
                "total_tokens": 15,
            },
        )
    )
    _patch_client(monkeypatch, client)
    model = SimpleNamespace(
        provider="my-unregistered-gateway",
        model="runtime-model",
        base_url="https://example.invalid",
        request_timeout=17,
        temperature=0.2,
        max_output_tokens=1024,
    )

    result = await single_step.complete_llm_once(
        model,
        [LLMMessage(role="user", content="Compact this")],
    )

    assert result.usage.total_tokens == 15
    assert result.usage.estimated_tokens == 0


@pytest.mark.asyncio
async def test_complete_once_resolves_anthropic_usage_by_protocol_not_by_key_sniffing(
    monkeypatch,
) -> None:
    """Anthropic 的 `input_tokens` 排除两个缓存计数器，与 OpenAI 语义不同。

    按协议归一化才能算出正确的 input_tokens = input_tokens + 两个缓存计数
    器之和；如果按字段名"碰运气"识别协议，Anthropic 的 usage 会被当成
    OpenAI 语义误读。
    """
    client = _Client(
        LLMResponse(
            content="done",
            usage={
                "input_tokens": 10,
                "cache_read_input_tokens": 90,
                "output_tokens": 5,
            },
        )
    )
    _patch_client(monkeypatch, client)
    model = SimpleNamespace(
        provider="anthropic",
        model="claude-test",
        base_url="https://api.anthropic.com",
        request_timeout=17,
        temperature=0.2,
        max_output_tokens=1024,
    )

    result = await single_step.complete_llm_once(
        model,
        [LLMMessage(role="user", content="Hello")],
    )

    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 5
    assert result.usage.estimated_tokens == 0


@pytest.mark.asyncio
async def test_complete_once_rejects_system_scope_without_a_tenant() -> None:
    """无 tenant_id 时走 legacy 分支，会按 agent 记账并静默丢弃 system_scope。

    必须在这个组合出现时就报错，而不是让调用方以为 system_scope 生效了。
    """
    with pytest.raises(ValueError, match="tenant_id"):
        await single_step.complete_llm_once(
            _model(),
            [LLMMessage(role="user", content="Chat")],
            agent_id=uuid.uuid4(),
            system_scope="group_compact",
        )


@pytest.mark.asyncio
async def test_complete_once_returns_a_bounded_repair_instruction_for_invalid_arguments(
    monkeypatch,
) -> None:
    client = _Client(
        LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "call-bad",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":',
                    },
                }
            ],
        )
    )
    _patch_client(monkeypatch, client)
    result = await single_step.complete_llm_once(
        _model(),
        [LLMMessage(role="user", content="Write")],
    )

    assert result.tool_calls == ()
    assert result.retry_instruction is not None
    assert "valid JSON" in result.retry_instruction
    assert "not executed" in result.retry_instruction
    assert "same oversized whole-file content" in result.retry_instruction
    assert result.retry_tool_name == "write_file"
    assert client.closed is True


@pytest.mark.asyncio
async def test_complete_once_closes_the_provider_client_when_the_request_fails(
    monkeypatch,
) -> None:
    client = _Client(RuntimeError("provider unavailable"))
    _patch_client(monkeypatch, client)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await single_step.complete_llm_once(
            _model(),
            [LLMMessage(role="user", content="Hello")],
        )

    assert client.closed is True
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_complete_once_sends_standard_multimodal_content_to_vision_provider(
    monkeypatch,
) -> None:
    client = _Client(LLMResponse(content="described"))
    _patch_client(monkeypatch, client)
    original = LLMMessage(
        role="user",
        content=f"[image_data:{_TINY_PNG_DATA_URL}] Describe it",
    )

    result = await single_step.complete_llm_once(
        _model(),
        [original],
        supports_vision=True,
    )

    sent = client.calls[0]["messages"][0]
    assert sent.content == [
        {
            "type": "image_url",
            "image_url": {"url": _TINY_PNG_DATA_URL},
        },
        {"type": "text", "text": "Describe it"},
    ]
    assert isinstance(original.content, str)
    assert result.content == "described"
