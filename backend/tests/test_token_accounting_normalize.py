"""Provider usage 归一化：口径不变式与各协议语义。

线上观察到的两个缺陷决定了这里的断言：
1. Anthropic 的 input_tokens 排除两个缓存计数，旧代码 total = input + output
   因此丢掉全部缓存量，命中率越高丢得越多。
2. Anthropic 流式 message_delta 覆盖 message_start，只回 output_tokens 的网关
   会让输入与缓存计数归零。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from loguru import logger

from app.services.llm.client import PROVIDER_REGISTRY, get_provider_spec
from app.services.token_accounting.normalize import (
    KNOWN_PROTOCOLS,
    PROTOCOL_ANTHROPIC,
    PROTOCOL_GEMINI,
    PROTOCOL_OPENAI_COMPATIBLE,
    PROTOCOL_OPENAI_RESPONSES,
    TokenUsage,
    merge_streaming_usage,
    normalize,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def test_anthropic_total_includes_both_cache_counters() -> None:
    """Anthropic 的 input_tokens 排除缓存，所以 total 必须把两个缓存计数加回来。"""
    usage = normalize(
        PROTOCOL_ANTHROPIC,
        {
            "input_tokens": 1_000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 2_000,
            "cache_read_input_tokens": 90_000,
        },
    )

    assert usage is not None
    assert usage.input_tokens == 93_000
    assert usage.cache_read_tokens == 90_000
    assert usage.cache_creation_tokens == 2_000
    assert usage.output_tokens == 500
    assert usage.total_tokens == 93_500
    assert usage.uncached_input_tokens == 1_000


def test_openai_prompt_tokens_already_include_cached() -> None:
    """OpenAI 的 prompt_tokens 已含 cached_tokens，不得重复加。"""
    usage = normalize(
        PROTOCOL_OPENAI_COMPATIBLE,
        {
            "prompt_tokens": 93_000,
            "completion_tokens": 500,
            "total_tokens": 93_500,
            "prompt_tokens_details": {"cached_tokens": 90_000},
            "completion_tokens_details": {"reasoning_tokens": 300},
        },
    )

    assert usage is not None
    assert usage.input_tokens == 93_000
    assert usage.cache_read_tokens == 90_000
    assert usage.total_tokens == 93_500
    assert usage.uncached_input_tokens == 3_000
    assert usage.reasoning_tokens == 300


def test_reasoning_tokens_are_not_added_to_any_total() -> None:
    """reasoning 已含在 output 里，重复计入就是重新引入正在修的这类 bug。"""
    usage = normalize(
        PROTOCOL_OPENAI_RESPONSES,
        {
            "input_tokens": 100,
            "output_tokens": 400,
            "total_tokens": 500,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens_details": {"reasoning_tokens": 350},
        },
    )

    assert usage is not None
    assert usage.output_tokens == 400
    assert usage.total_tokens == 500


def test_gemini_output_includes_thinking_tokens() -> None:
    """thoughtsTokenCount 要计费，必须计入 output。"""
    usage = normalize(
        PROTOCOL_GEMINI,
        {
            "promptTokenCount": 1_200,
            "candidatesTokenCount": 300,
            "thoughtsTokenCount": 700,
            "cachedContentTokenCount": 900,
            "totalTokenCount": 2_200,
        },
    )

    assert usage is not None
    assert usage.input_tokens == 1_200
    assert usage.cache_read_tokens == 900
    assert usage.output_tokens == 1_000
    assert usage.total_tokens == 2_200
    assert usage.reasoning_tokens == 700


def test_anthropic_shaped_usage_with_total_tokens_is_not_read_as_openai() -> None:
    """很多 Anthropic 兼容网关也回 total_tokens，旧代码据此误判为 OpenAI。"""
    usage = normalize(
        PROTOCOL_ANTHROPIC,
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 5_000,
            "total_tokens": 30,
        },
    )

    assert usage is not None
    assert usage.cache_read_tokens == 5_000
    assert usage.total_tokens == 5_030


def test_openai_shaped_input_is_lifted_to_at_least_the_cache_sum() -> None:
    """网关把 Anthropic 语义透传成 OpenAI 形状时，prompt_tokens 会小于缓存量。

    取 max 是单调且保守的修补：本来就含缓存时不变，明显不含时至少不再少算。
    """
    usage = normalize(
        PROTOCOL_OPENAI_COMPATIBLE,
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cache_read_input_tokens": 8_000,
            "cache_creation_input_tokens": 1_000,
        },
    )

    assert usage is not None
    assert usage.input_tokens == 9_000
    assert usage.total_tokens == 9_050


def test_openai_compatible_without_any_cache_detail_degrades_cleanly() -> None:
    """绝大多数第三方 provider 只回 prompt/completion，此时缓存计数为 0 且总量准确。"""
    usage = normalize(
        PROTOCOL_OPENAI_COMPATIBLE,
        {"prompt_tokens": 800, "completion_tokens": 200, "total_tokens": 1_000},
    )

    assert usage is not None
    assert usage.cache_read_tokens == 0
    assert usage.cache_creation_tokens == 0
    assert usage.total_tokens == 1_000


def test_empty_usage_returns_none() -> None:
    assert normalize(PROTOCOL_ANTHROPIC, None) is None
    assert normalize(PROTOCOL_ANTHROPIC, {}) is None


def test_unknown_protocol_returns_none() -> None:
    assert normalize("not-a-protocol", {"prompt_tokens": 1, "completion_tokens": 1}) is None


def test_every_registered_provider_resolves_to_a_known_protocol() -> None:
    """新增 provider 时不能静默落进未知分支丢 usage。"""
    for provider in PROVIDER_REGISTRY:
        spec = get_provider_spec(provider)
        assert spec is not None, provider
        assert spec.protocol in KNOWN_PROTOCOLS, f"{provider} -> {spec.protocol}"


def test_streaming_merge_keeps_message_start_fields() -> None:
    """这就是当前 bug 的精确复现：delta 只回 output_tokens 时不得丢输入与缓存。"""
    message_start = {
        "input_tokens": 1_000,
        "output_tokens": 1,
        "cache_creation_input_tokens": 2_000,
        "cache_read_input_tokens": 90_000,
    }
    message_delta = {"output_tokens": 500}

    merged = merge_streaming_usage(message_start, message_delta)

    assert merged == {
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 2_000,
        "cache_read_input_tokens": 90_000,
    }


def test_streaming_merge_accepts_a_cumulative_full_delta() -> None:
    """Anthropic 文档说 message_delta 的 usage 是累计值，取 max 对两种网关都对。"""
    merged = merge_streaming_usage(
        {"input_tokens": 1_000, "output_tokens": 10, "cache_read_input_tokens": 90_000},
        {"input_tokens": 1_000, "output_tokens": 500, "cache_read_input_tokens": 90_000},
    )

    assert merged["output_tokens"] == 500
    assert merged["cache_read_input_tokens"] == 90_000


def test_streaming_merge_recurses_into_nested_detail_dicts() -> None:
    merged = merge_streaming_usage(
        {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 8}},
        {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}},
    )

    assert merged["prompt_tokens_details"]["cached_tokens"] == 8


def test_streaming_merge_handles_missing_sides() -> None:
    assert merge_streaming_usage(None, {"output_tokens": 5}) == {"output_tokens": 5}
    assert merge_streaming_usage({"output_tokens": 5}, None) == {"output_tokens": 5}
    assert merge_streaming_usage(None, None) is None


def test_calibration_mismatch_warns() -> None:
    """错误的算术必须报警而不是静默 —— 当前这批 bug 正是因为它静默才潜伏这么久。"""
    records: list[str] = []
    handler_id = logger.add(lambda message: records.append(message), level="WARNING")
    try:
        normalize(
            PROTOCOL_OPENAI_COMPATIBLE,
            {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 999},
        )
    finally:
        logger.remove(handler_id)

    assert any("token_calibration_mismatch" in record for record in records)


def test_anthropic_gateway_total_does_not_trigger_calibration_warning() -> None:
    """Anthropic 协议下网关给的 total_tokens 用的是旧的排除缓存定义，
    拿它比对会每次调用都告警，纯噪音。"""
    records: list[str] = []
    handler_id = logger.add(lambda message: records.append(message), level="WARNING")
    try:
        normalize(
            PROTOCOL_ANTHROPIC,
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 5_000,
                "total_tokens": 30,
            },
        )
    finally:
        logger.remove(handler_id)

    assert not any("token_calibration_mismatch" in record for record in records)


def test_anthropic_client_stream_merges_usage_across_events() -> None:
    """AnthropicClient.stream 必须用 merge_streaming_usage 而不是直接赋值。

    只回 output_tokens 的网关很常见；直接赋值会让 input 与两个缓存计数在主聊天
    路径上归零。这里用源码级断言守住这一点，因为构造一条真实的 SSE 流需要 HTTP
    fixture，而本仓库的测试不连外部依赖。
    """
    import inspect

    from app.services.llm import client as llm_client

    source = inspect.getsource(llm_client.AnthropicClient.stream)

    assert "merge_streaming_usage" in source
    assert 'final_usage = msg["usage"]' not in source
    assert 'final_usage = data["usage"]' not in source


def test_anthropic_stream_usage_merge_is_end_to_end_correct() -> None:
    """把两个事件的 usage 依次合并再归一化，等价于流式路径的实际算法。"""
    final_usage = merge_streaming_usage(
        None,
        {
            "input_tokens": 1_000,
            "output_tokens": 1,
            "cache_creation_input_tokens": 2_000,
            "cache_read_input_tokens": 90_000,
        },
    )
    final_usage = merge_streaming_usage(final_usage, {"output_tokens": 500})

    usage = normalize(PROTOCOL_ANTHROPIC, final_usage)

    assert usage is not None
    assert usage.total_tokens == 93_500
    assert usage.cache_read_tokens == 90_000


def test_token_usage_add_accumulates_every_field() -> None:
    total = TokenUsage()
    total.add(
        TokenUsage(
            total_tokens=10,
            input_tokens=6,
            output_tokens=4,
            cache_read_tokens=3,
            cache_creation_tokens=1,
            reasoning_tokens=2,
            estimated_tokens=10,
        )
    )
    total.add(TokenUsage(total_tokens=5, input_tokens=3, output_tokens=2))

    assert total.total_tokens == 15
    assert total.input_tokens == 9
    assert total.output_tokens == 6
    assert total.cache_read_tokens == 3
    assert total.cache_creation_tokens == 1
    assert total.reasoning_tokens == 2
    assert total.estimated_tokens == 10


def _assert_cold_import_succeeds(import_statement: str) -> None:
    """在全新解释器里跑一句 import，绕开 pytest 早就把 app.services.llm 导入
    过一遍从而"意外"把循环导入解开的问题。

    进程内 import（哪怕用 importlib.reload / 删 sys.modules 手动清缓存）都测不出
    这个 bug：本测试模块本身已经 `from app.services.llm.client import ...`，
    到运行到这里时 app.services.llm 早已在 sys.modules 里初始化完毕，circular
    import 只会在第一次、从空模块表开始加载时才会失败。必须开一个全新的子进程。
    """
    result = subprocess.run(
        [sys.executable, "-c", import_statement],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"cold import failed for `{import_statement}`\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_cold_import_of_token_accounting_package_does_not_cycle() -> None:
    """normalize.py 曾在模块作用域 import app.services.llm.multimodal_content，
    这会经 llm/__init__ -> caller -> client 拉回 normalize.merge_streaming_usage，
    形成循环导入。回归：包本身必须能在干净进程里独立 import。"""
    _assert_cold_import_succeeds("import app.services.token_accounting")


def test_cold_import_of_token_accounting_periods_does_not_cycle() -> None:
    """periods 通过包 __init__ 间接触发同一条循环导入路径，单独覆盖以防
    未来有人绕开包 __init__ 直接 `import app.services.token_accounting.periods`。"""
    _assert_cold_import_succeeds("import app.services.token_accounting.periods")
