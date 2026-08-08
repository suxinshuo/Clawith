"""统一 token 计量口径：provider usage -> TokenUsage。纯函数，零 IO。

口径不变式：total_tokens == input_tokens + output_tokens，其中 input_tokens
含缓存，cache_read / cache_creation 是它的细分，reasoning_tokens 仅供展示。

分派键是协议而不是 provider 名：PROVIDER_REGISTRY 里有十几个 provider，但只有
四个协议，按 provider 名分派会让一大批 openai_compatible 的第三方 provider 落
进未知分支丢掉 usage。本模块刻意不在模块作用域 import app.services.llm 下任何
东西 —— 它是全代码库最重的包，import 其任意子模块都会触发
app/services/llm/__init__.py，进而拉进 .caller、.client 等一整条依赖链；而
client 又需要反过来调用本模块的 merge_streaming_usage，互引会形成循环导入。
本模块是零 IO、位于依赖图底部的模块，理应被别人依赖而不是反过来依赖 llm 包。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

PROTOCOL_OPENAI_COMPATIBLE = "openai_compatible"
PROTOCOL_OPENAI_RESPONSES = "openai_responses"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_GEMINI = "gemini"

KNOWN_PROTOCOLS = frozenset(
    {
        PROTOCOL_OPENAI_COMPATIBLE,
        PROTOCOL_OPENAI_RESPONSES,
        PROTOCOL_ANTHROPIC,
        PROTOCOL_GEMINI,
    }
)

CHARS_PER_TOKEN = 3


@dataclass
class TokenUsage:
    """归一化后的 token 计量。

    保持可变 dataclass 与 add()，使 token_tracker 转发层签名和 caller.py 的既有
    测试都不受影响。
    """

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_tokens: int = 0

    def add(self, other: TokenUsage) -> None:
        self.total_tokens += other.total_tokens
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.estimated_tokens += other.estimated_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(
            0,
            self.input_tokens - self.cache_read_tokens - self.cache_creation_tokens,
        )


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _detail(usage: dict, *keys: str) -> dict:
    for key in keys:
        candidate = usage.get(key)
        if isinstance(candidate, dict):
            return candidate
    return {}


def _pick(source: dict, *keys: str) -> int:
    """取第一个非零候选键 —— 各网关对同一语义用的键名不统一。"""
    for key in keys:
        value = _int(source.get(key))
        if value:
            return value
    return 0


def estimate_tokens_from_chars(total_chars: int) -> int:
    """provider 完全不回 usage 时的粗估。"""
    return max(total_chars // CHARS_PER_TOKEN, 1)


def estimate_token_usage_from_chars(total_chars: int) -> TokenUsage:
    tokens = estimate_tokens_from_chars(total_chars)
    return TokenUsage(
        total_tokens=tokens,
        output_tokens=tokens,
        estimated_tokens=tokens,
    )


def merge_streaming_usage(
    existing: dict | None,
    incoming: dict | None,
) -> dict | None:
    """逐字段取最大值合并流式 usage。

    Anthropic 的 message_start 携带 input 与两个缓存计数，message_delta 携带
    output，且部分网关在 delta 里只发 output_tokens。旧代码在那里直接赋值，于是
    输入与缓存计数在主聊天路径上被抹掉。message_delta 的 usage 按文档是累计值，
    所以取 max 在"delta 携带完整值"和"delta 只携带子集"两种情况下都正确。
    """
    if not incoming:
        return existing
    if not existing:
        return dict(incoming)

    merged = dict(existing)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = merge_streaming_usage(current, value)
        elif isinstance(value, bool) or isinstance(current, bool):
            merged[key] = value
        elif isinstance(value, (int, float)) and isinstance(current, (int, float)):
            merged[key] = max(current, value)
        else:
            merged[key] = value
    return merged


def _normalize_openai_shaped(usage: dict, *, responses_api: bool) -> TokenUsage:
    if responses_api:
        raw_input = _pick(usage, "input_tokens", "prompt_tokens")
        raw_output = _pick(usage, "output_tokens", "completion_tokens")
        input_details = _detail(usage, "input_tokens_details", "prompt_tokens_details")
        output_details = _detail(usage, "output_tokens_details", "completion_tokens_details")
    else:
        raw_input = _pick(usage, "prompt_tokens", "input_tokens")
        raw_output = _pick(usage, "completion_tokens", "output_tokens")
        input_details = _detail(usage, "prompt_tokens_details", "input_tokens_details")
        output_details = _detail(usage, "completion_tokens_details", "output_tokens_details")

    cache_read = _pick(input_details, "cached_tokens", "cache_read_tokens", "cache_read_input_tokens") or _pick(
        usage, "cached_tokens", "cache_read_tokens", "cache_read_input_tokens"
    )
    cache_creation = _pick(input_details, "cache_creation_tokens", "cache_creation_input_tokens") or _pick(
        usage, "cache_creation_tokens", "cache_creation_input_tokens"
    )
    reasoning = _pick(output_details, "reasoning_tokens") or _pick(usage, "reasoning_tokens")

    # 这些协议约定 input 已含缓存。但把 Anthropic 语义透传成 OpenAI 形状的网关会
    # 让 input 小于缓存量；取 max 是单调保守的修补 —— 本来就含缓存时不变，明显
    # 不含时至少不再少算。
    input_tokens = max(raw_input, cache_read + cache_creation)

    return TokenUsage(
        total_tokens=input_tokens + raw_output,
        input_tokens=input_tokens,
        output_tokens=raw_output,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        reasoning_tokens=reasoning,
    )


def _normalize_anthropic(usage: dict) -> TokenUsage:
    cache_read = _pick(usage, "cache_read_input_tokens", "cache_read_tokens")
    cache_creation = _pick(usage, "cache_creation_input_tokens", "cache_creation_tokens")
    # Anthropic 的 input_tokens 排除两个缓存计数，必须相加而不是取 max。
    input_tokens = _int(usage.get("input_tokens")) + cache_read + cache_creation
    output_tokens = _int(usage.get("output_tokens"))

    return TokenUsage(
        total_tokens=input_tokens + output_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
    )


def _normalize_gemini(usage: dict) -> TokenUsage:
    cache_read = _int(usage.get("cachedContentTokenCount"))
    input_tokens = max(_int(usage.get("promptTokenCount")), cache_read)
    thoughts = _int(usage.get("thoughtsTokenCount"))
    output_tokens = _int(usage.get("candidatesTokenCount")) + thoughts

    return TokenUsage(
        total_tokens=input_tokens + output_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        reasoning_tokens=thoughts,
    )


def _provider_reported_total(protocol: str, usage: dict) -> int | None:
    """provider 自报的总量，仅用于自校验。

    anthropic 协议刻意返回 None：网关在那里给的 total_tokens 用的是旧的排除缓存
    定义，拿它比对会每次调用都告警，纯噪音。
    """
    if protocol == PROTOCOL_ANTHROPIC:
        return None
    if protocol == PROTOCOL_GEMINI:
        reported = _int(usage.get("totalTokenCount"))
        return reported or None
    reported = _int(usage.get("total_tokens"))
    return reported or None


def normalize(protocol: str, usage: dict | None) -> TokenUsage | None:
    """按协议归一化 provider usage。未知协议或空 usage 返回 None。"""
    if not usage or not isinstance(usage, dict):
        return None

    if protocol == PROTOCOL_ANTHROPIC:
        result = _normalize_anthropic(usage)
    elif protocol == PROTOCOL_GEMINI:
        result = _normalize_gemini(usage)
    elif protocol == PROTOCOL_OPENAI_RESPONSES:
        result = _normalize_openai_shaped(usage, responses_api=True)
    elif protocol == PROTOCOL_OPENAI_COMPATIBLE:
        result = _normalize_openai_shaped(usage, responses_api=False)
    else:
        logger.warning(
            "token_unknown_protocol protocol={} usage_keys={}",
            protocol,
            sorted(usage.keys()),
        )
        return None

    if result.total_tokens <= 0:
        return None

    reported_total = _provider_reported_total(protocol, usage)
    if reported_total is not None and reported_total != result.total_tokens:
        # 只告警不纠正：当前这批 bug 正是因为错误的算术静默才潜伏这么久，接新网关
        # 或某家改语义时应该表现为告警。
        logger.warning(
            "token_calibration_mismatch protocol={} reported={} computed={} usage={}",
            protocol,
            reported_total,
            result.total_tokens,
            usage,
        )

    return result


def usage_from_response_or_estimate(
    protocol: str,
    usage: dict | None,
    messages: object,
    content: str | None,
) -> TokenUsage:
    """有真实 usage 就用，没有才退回字符估算，并把估算量单独记下来。"""
    normalized = normalize(protocol, usage)
    if normalized is not None:
        return normalized

    # 延迟到函数内 import：app.services.llm.__init__ 会经 .caller 拉入 .client，
    # client 又在模块作用域 import 本模块的 merge_streaming_usage，若把这个
    # import 提到模块顶层会重新形成 normalize -> llm/__init__ -> caller ->
    # client -> normalize 的循环导入。CLAUDE.md 允许函数内 import 的例外情形
    # 正是"打破循环导入"，此处即是。
    from app.services.llm.multimodal_content import estimate_multimodal_tokens

    input_tokens = estimate_multimodal_tokens(
        messages,
        chars_per_token=CHARS_PER_TOKEN,
    )
    output_tokens = estimate_tokens_from_chars(len(content or ""))
    total = input_tokens + output_tokens
    return TokenUsage(
        total_tokens=total,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_tokens=total,
    )


__all__ = [
    "CHARS_PER_TOKEN",
    "KNOWN_PROTOCOLS",
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_GEMINI",
    "PROTOCOL_OPENAI_COMPATIBLE",
    "PROTOCOL_OPENAI_RESPONSES",
    "TokenUsage",
    "estimate_token_usage_from_chars",
    "estimate_tokens_from_chars",
    "merge_streaming_usage",
    "normalize",
    "usage_from_response_or_estimate",
]
