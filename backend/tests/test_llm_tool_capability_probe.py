"""Model test contract for native Agent tool-calling capability."""

import json
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import enterprise
from app.models.llm import LLMModel
from app.schemas.schemas import LLMModelUpdate
from app.services.llm.client import LLMResponse
from app.services.token_accounting.ledger import SYSTEM_SCOPE_MODEL_PROBE


class _Client:
    def __init__(self, *responses: LLMResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self) -> None:
        self.closed = True


def _target(*, model_id: uuid.UUID | None = None):
    return enterprise.LLMTestTarget(
        model_id=model_id,
        provider="ollama",
        model="qwen-local",
        api_key="ollama",
        base_url="http://localhost:11434/v1",
        stored_config_fingerprint="stored-fingerprint" if model_id else None,
    )


@pytest.mark.asyncio
async def test_unsaved_draft_test_separates_capabilities_but_does_not_record_them(
    monkeypatch,
) -> None:
    """``capability_recorded`` is about capability persistence, not the token ledger.

    An unsaved draft has no owning ``LLMModel`` row to persist
    ``supports_tool_calling`` against, but its token consumption is real and must
    still be attributed to the tenant even though neither provider response
    carries a ``usage`` field.
    """
    client = _Client(
        LLMResponse(content="ok"),
        LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "probe-finish",
                    "type": "function",
                    "function": {
                        "name": "finish",
                        "arguments": json.dumps({"content": "ok"}),
                    },
                }
            ],
        ),
    )
    monkeypatch.setattr(
        enterprise,
        "_resolve_llm_test_target",
        AsyncMock(return_value=_target()),
    )
    monkeypatch.setattr(enterprise, "create_llm_client", lambda **_kwargs: client)
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(enterprise, "record_token_usage_ledger", record)

    result = await enterprise.test_llm_model(
        enterprise.LLMTestRequest(
            provider="ollama",
            model="qwen-local",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
        ),
        current_user=SimpleNamespace(id=uuid.uuid4(), role="admin", tenant_id=uuid.uuid4()),
    )

    assert result["success"] is True
    assert result["connection_success"] is True
    assert result["tool_calling_supported"] is True
    assert result["capability_recorded"] is False
    assert len(client.calls) == 2
    assert client.calls[0]["tools"] is None
    assert [tool["function"]["name"] for tool in client.calls[1]["tools"]] == ["finish"]
    assert client.closed is True
    # Neither provider response carries a `usage` field at all — both calls
    # must still land in the ledger via the character-based estimate rather
    # than being silently dropped because there was nothing to normalize.
    assert record.await_count == 2
    for call in record.await_args_list:
        assert call.args[0].estimated_tokens > 0
        assert call.kwargs["system_scope"] == SYSTEM_SCOPE_MODEL_PROBE


@pytest.mark.asyncio
async def test_tool_probe_transport_failure_records_unknown_not_unsupported(
    monkeypatch,
) -> None:
    model_id = uuid.uuid4()
    target = _target(model_id=model_id)
    client = _Client(LLMResponse(content="ok"), TimeoutError("local model busy"))
    record = AsyncMock(return_value=True)
    ledger_record = AsyncMock(return_value=True)
    monkeypatch.setattr(
        enterprise,
        "_resolve_llm_test_target",
        AsyncMock(return_value=target),
    )
    monkeypatch.setattr(enterprise, "_record_llm_tool_capability", record)
    monkeypatch.setattr(enterprise, "create_llm_client", lambda **_kwargs: client)
    monkeypatch.setattr(enterprise, "record_token_usage_ledger", ledger_record)

    result = await enterprise.test_llm_model(
        enterprise.LLMTestRequest(
            provider="ollama",
            model="qwen-local",
            model_id=str(model_id),
        ),
        current_user=SimpleNamespace(id=uuid.uuid4(), role="admin", tenant_id=uuid.uuid4()),
    )

    assert result["success"] is False
    assert result["connection_success"] is True
    assert result["tool_calling_supported"] is None
    assert "TimeoutError" in result["tool_calling_error"]
    assert record.await_args.kwargs["supported"] is None
    assert client.closed is True
    # The connectivity call succeeded with no `usage` field and must still be
    # estimated and recorded; the tool-calling call never got a response at
    # all (it raised), so there is nothing to record for it.
    ledger_record.assert_awaited_once()
    assert ledger_record.await_args.args[0].estimated_tokens > 0
    assert ledger_record.await_args.kwargs["system_scope"] == SYSTEM_SCOPE_MODEL_PROBE


@pytest.mark.asyncio
async def test_plain_text_probe_is_not_reported_as_agent_compatible_and_is_recorded(
    monkeypatch,
) -> None:
    model_id = uuid.uuid4()
    target = _target(model_id=model_id)
    client = _Client(
        LLMResponse(content="ok"),
        LLMResponse(content="I am done", tool_calls=[]),
    )
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(
        enterprise,
        "_resolve_llm_test_target",
        AsyncMock(return_value=target),
    )
    monkeypatch.setattr(enterprise, "_record_llm_tool_capability", record)
    monkeypatch.setattr(enterprise, "create_llm_client", lambda **_kwargs: client)

    result = await enterprise.test_llm_model(
        enterprise.LLMTestRequest(
            provider="ollama",
            model="qwen-local",
            model_id=str(model_id),
        ),
        current_user=SimpleNamespace(id=uuid.uuid4(), role="admin", tenant_id=uuid.uuid4()),
    )

    assert result["success"] is False
    assert result["connection_success"] is True
    assert result["tool_calling_supported"] is False
    assert result["capability_recorded"] is True
    assert "plain text" in result["tool_calling_error"].lower()
    record.assert_awaited_once()
    assert record.await_args.args[0] is target
    assert record.await_args.kwargs["supported"] is False
    assert client.closed is True


@pytest.mark.asyncio
async def test_connectivity_and_tool_probe_usage_is_recorded_to_the_tenant_system_scope(
    monkeypatch,
) -> None:
    """/llm-test 此前直接调 client.complete 两次,完全绕过 complete_llm_once,零记录.

    现在两次探测消耗的 usage 都必须落到调用者租户的 model_probe 系统开销,
    且绝不能带 agent_id(没有归属 Agent)。
    """
    tenant_id = uuid.uuid4()
    client = _Client(
        LLMResponse(
            content="ok",
            usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        ),
        LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "probe-finish",
                    "type": "function",
                    "function": {
                        "name": "finish",
                        "arguments": json.dumps({"content": "ok"}),
                    },
                }
            ],
            usage={"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8},
        ),
    )
    monkeypatch.setattr(
        enterprise,
        "_resolve_llm_test_target",
        AsyncMock(return_value=_target()),
    )
    monkeypatch.setattr(enterprise, "create_llm_client", lambda **_kwargs: client)
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(enterprise, "record_token_usage_ledger", record)

    result = await enterprise.test_llm_model(
        enterprise.LLMTestRequest(
            provider="ollama",
            model="qwen-local",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
        ),
        current_user=SimpleNamespace(id=uuid.uuid4(), role="admin", tenant_id=tenant_id),
    )

    assert result["success"] is True
    assert record.await_count == 2
    for call in record.await_args_list:
        assert call.kwargs["tenant_id"] == tenant_id
        assert call.kwargs["system_scope"] == SYSTEM_SCOPE_MODEL_PROBE
        assert "agent_id" not in call.kwargs
    recorded_totals = {call.args[0].total_tokens for call in record.await_args_list}
    assert recorded_totals == {4, 8}


@pytest.mark.asyncio
async def test_connectivity_and_tool_probe_usage_is_estimated_when_provider_omits_usage_entirely(
    monkeypatch,
) -> None:
    """A provider (or gateway in front of it) can omit the `usage` field altogether.

    `normalize()` returns `None` for an absent/empty usage dict, which used to make
    both the ledger `record` call and the "unattributed consumption" log get
    skipped together — the tokens the call actually cost become invisible. The
    probe must fall back to `usage_from_response_or_estimate`, exactly like every
    other call site in this plan, so a missing `usage` still yields a recorded,
    non-zero estimate instead of silence.
    """
    tenant_id = uuid.uuid4()
    client = _Client(
        LLMResponse(content="ok"),
        LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "probe-finish",
                    "type": "function",
                    "function": {
                        "name": "finish",
                        "arguments": json.dumps({"content": "ok"}),
                    },
                }
            ],
        ),
    )
    monkeypatch.setattr(
        enterprise,
        "_resolve_llm_test_target",
        AsyncMock(return_value=_target()),
    )
    monkeypatch.setattr(enterprise, "create_llm_client", lambda **_kwargs: client)
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(enterprise, "record_token_usage_ledger", record)

    result = await enterprise.test_llm_model(
        enterprise.LLMTestRequest(
            provider="ollama",
            model="qwen-local",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
        ),
        current_user=SimpleNamespace(id=uuid.uuid4(), role="admin", tenant_id=tenant_id),
    )

    assert result["success"] is True
    assert record.await_count == 2
    for call in record.await_args_list:
        assert call.kwargs["tenant_id"] == tenant_id
        assert call.kwargs["system_scope"] == SYSTEM_SCOPE_MODEL_PROBE
        usage = call.args[0]
        assert usage.total_tokens > 0
        assert usage.estimated_tokens > 0
        assert usage.estimated_tokens == usage.total_tokens


@pytest.mark.asyncio
async def test_connectivity_and_tool_probe_usage_is_recorded_for_an_unregistered_provider(
    monkeypatch,
) -> None:
    """provider 不在 PROVIDER_REGISTRY 里时，create_llm_client 仍会退到 OpenAICompatibleClient。

    见 app/services/llm/client.py 的 "Default to OpenAI-compatible for
    unknown providers" 分支：它返回真实 usage。如果记账这里用空协议 `""`
    兜底，normalize("", usage) 对未知协议直接返回 None，这两次探测消耗的
    usage 就会被悄悄丢弃——回到本任务要消灭的零记录状态。
    """
    tenant_id = uuid.uuid4()
    client = _Client(
        LLMResponse(
            content="ok",
            usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        ),
        LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "probe-finish",
                    "type": "function",
                    "function": {
                        "name": "finish",
                        "arguments": json.dumps({"content": "ok"}),
                    },
                }
            ],
            usage={"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8},
        ),
    )
    unregistered_target = enterprise.LLMTestTarget(
        model_id=None,
        provider="my-unregistered-gateway",
        model="qwen-local",
        api_key="ollama",
        base_url="http://localhost:11434/v1",
        stored_config_fingerprint=None,
    )
    monkeypatch.setattr(
        enterprise,
        "_resolve_llm_test_target",
        AsyncMock(return_value=unregistered_target),
    )
    monkeypatch.setattr(enterprise, "create_llm_client", lambda **_kwargs: client)
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(enterprise, "record_token_usage_ledger", record)

    result = await enterprise.test_llm_model(
        enterprise.LLMTestRequest(
            provider="my-unregistered-gateway",
            model="qwen-local",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
        ),
        current_user=SimpleNamespace(id=uuid.uuid4(), role="admin", tenant_id=tenant_id),
    )

    assert result["success"] is True
    assert record.await_count == 2
    for call in record.await_args_list:
        assert call.kwargs["tenant_id"] == tenant_id
        assert call.kwargs["system_scope"] == SYSTEM_SCOPE_MODEL_PROBE
    recorded_totals = {call.args[0].total_tokens for call in record.await_args_list}
    assert recorded_totals == {4, 8}


@pytest.mark.asyncio
async def test_platform_admin_probe_without_a_tenant_records_nothing(
    monkeypatch,
    caplog,
) -> None:
    """platform_admin 的 tenant_id 可能是 None；不能把 usage 记到一个不存在的租户上。

    跳过落库是对的,但这笔消耗不能因此变得不可见——必须留下一条可 grep 的日志。
    """
    client = _Client(
        LLMResponse(
            content="ok",
            usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        ),
        LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "probe-finish",
                    "type": "function",
                    "function": {
                        "name": "finish",
                        "arguments": json.dumps({"content": "ok"}),
                    },
                }
            ],
            usage={"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8},
        ),
    )
    monkeypatch.setattr(
        enterprise,
        "_resolve_llm_test_target",
        AsyncMock(return_value=_target()),
    )
    monkeypatch.setattr(enterprise, "create_llm_client", lambda **_kwargs: client)
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(enterprise, "record_token_usage_ledger", record)
    user_id = uuid.uuid4()

    with caplog.at_level(logging.INFO, logger="app.api.enterprise"):
        result = await enterprise.test_llm_model(
            enterprise.LLMTestRequest(
                provider="ollama",
                model="qwen-local",
                api_key="ollama",
                base_url="http://localhost:11434/v1",
            ),
            current_user=SimpleNamespace(id=user_id, role="platform_admin", tenant_id=None),
        )

    assert result["success"] is True
    record.assert_not_awaited()
    unattributed_logs = [r for r in caplog.records if str(user_id) in r.getMessage()]
    assert len(unattributed_logs) == 2
    for log_record in unattributed_logs:
        assert "total_tokens=" in log_record.getMessage()


class _Result:
    def __init__(self, model: LLMModel) -> None:
        self.model = model

    def scalar_one_or_none(self) -> LLMModel:
        return self.model


class _DB:
    def __init__(self, model: LLMModel) -> None:
        self.model = model
        self.committed = False
        self.refreshed = False

    async def execute(self, _statement):
        return _Result(self.model)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, model: LLMModel) -> None:
        assert model is self.model
        self.refreshed = True

    async def rollback(self) -> None:
        raise AssertionError("update should not roll back")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        LLMModelUpdate(provider="custom"),
        LLMModelUpdate(model="new-model"),
        LLMModelUpdate(base_url="http://localhost:8000/v1"),
        LLMModelUpdate(api_key="new-local-key"),
    ],
)
async def test_updating_model_identity_invalidates_prior_tool_probe(
    update: LLMModelUpdate,
) -> None:
    tenant_id = uuid.uuid4()
    checked_at = datetime.now(UTC)
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="ollama",
        model="old-model",
        api_key_encrypted="stored-key",
        label="Local",
        enabled=True,
        supports_vision=False,
        supports_tool_calling=True,
        tool_calling_capability_source="probe",
        tool_calling_checked_at=checked_at,
        tool_calling_error=None,
        created_at=checked_at,
    )
    db = _DB(model)

    updated = await enterprise.update_llm_model(
        model.id,
        update,
        current_user=SimpleNamespace(tenant_id=tenant_id, role="admin"),
        db=db,  # type: ignore[arg-type]
    )

    assert updated.supports_tool_calling is None
    assert updated.tool_calling_capability_source is None
    assert updated.tool_calling_checked_at is None
    assert "changed" in (updated.tool_calling_error or "").lower()
    assert db.committed is True
    assert db.refreshed is True
