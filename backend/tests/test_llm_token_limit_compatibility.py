"""Consumer-visible request contract for model-specific token limit names."""

import json

import httpx
import pytest

from app.services.llm.client import LLMMessage, create_llm_client


MODELS = ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("provider", ("openai", "azure", "custom"))
@pytest.mark.parametrize("stream", (False, True))
async def test_gpt6_requests_use_completion_token_limit(model, provider, stream):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if stream:
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = create_llm_client(provider, "test-key", model, "https://test.invalid/v1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        method = client.stream if stream else client.complete
        response = await method(messages=[LLMMessage(role="user", content="ok")], max_tokens=256)
        assert response.content == "ok"
        assert len(requests) == 1
        assert requests[0]["max_completion_tokens"] == 256
        assert "max_tokens" not in requests[0]
        assert requests[0]["model"] == model
    finally:
        await client.close()


@pytest.mark.parametrize(
    "model,strategy,expected",
    [
        ("gpt-4o", "auto", "max_tokens"),
        ("private-alias", "auto", "max_tokens"),
        ("gpt-6-unknown", "auto", "max_tokens"),
        ("gpt-6-sol", "max_tokens", "max_tokens"),
        ("private-alias", "max_completion_tokens", "max_completion_tokens"),
        (" GPT-6-LUNA ", "auto", "max_completion_tokens"),
    ],
)
def test_token_strategy_preserves_unknown_models_and_allows_override(model, strategy, expected):
    client = create_llm_client("custom", "key", model, token_limit_param=strategy)
    payload = client._build_payload([LLMMessage(role="user", content="ok")], None, None, 123)
    assert payload[expected] == 123
    assert len({"max_tokens", "max_completion_tokens"}.intersection(payload)) == 1


@pytest.mark.parametrize("strategy", ("invalid", "", None))
def test_invalid_strategy_is_rejected(strategy):
    with pytest.raises(ValueError, match="token_limit_param"):
        create_llm_client("openai", "key", "gpt-6-sol", token_limit_param=strategy)


@pytest.mark.parametrize("model", ("gpt-6-sol", "private-alias"))
def test_duplicate_token_limit_kwargs_are_rejected(model):
    client = create_llm_client("openai", "key", model)
    with pytest.raises(ValueError, match="token"):
        client._build_payload(
            [LLMMessage(role="user", content="ok")], None, None, 256,
            max_completion_tokens=128,
        )


@pytest.mark.parametrize("provider", ("openai-response", "anthropic", "gemini"))
@pytest.mark.parametrize("strategy", ("auto", "max_completion_tokens", "max_tokens"))
def test_other_protocols_keep_native_token_field(provider, strategy):
    client = create_llm_client(provider, "key", "gpt-6-sol", token_limit_param=strategy)
    payload = client._build_payload([LLMMessage(role="user", content="ok")], None, None, 256)
    if provider == "openai-response":
        assert payload["max_output_tokens"] == 256
    elif provider == "anthropic":
        assert payload["max_tokens"] == 256
    else:
        assert payload["generationConfig"]["maxOutputTokens"] == 256
    assert "max_completion_tokens" not in payload
