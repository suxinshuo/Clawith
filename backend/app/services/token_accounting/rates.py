"""展示用比率。纯函数，零 IO。

收进一个共享函数而不是在各读取路径各写一遍除法 —— 五处各写一遍正是这次分母集体
算错的原因（`cache_read / total_tokens`，分母含 output）。
"""

from __future__ import annotations


def cache_hit_rate(cache_read: int | None, input_tokens: int | None) -> float | None:
    """缓存命中率 = 命中读取 / 输入总量（含缓存）。

    分母绝不能含 output —— output 按定义不可能被缓存读取。

    返回 None 表示"算不出来"，不是 0：新口径下 cache_read 是 input_tokens 的子集，
    所以 cache_read > input_tokens 只可能来自校准迁移之前的历史数据（迁移给
    agents.input_tokens_* 建列时不回填，而 cache_read_tokens_* 里累计着全部历史）。
    这种情况夹到 1.0 会显示成自信的"100% 命中"，退回 0.0 会显示成自信的"0% 命中"，
    两者都是编出来的数字。调用方应把 None 渲染成"—"，并配合 calibration_switched_at
    说明这段历史跨越了口径切换点。
    """
    read = int(cache_read or 0)
    denominator = int(input_tokens or 0)
    if read > denominator:
        return None
    if denominator <= 0:
        # 上面的 read > denominator 已经拦掉了 input=0 而 cache_read>0 的存量行，所以
        # 走到这里必然 read <= 0 且 denominator <= 0：没有任何输入、也没有任何命中读取。
        # 0/0 定义成 0% 是这一格唯一能显示的数（注意不能反推"这个 Agent 没用过 token"
        # ——存量行的 tokens_used_* 可能很大，只是 input_tokens_* 没回填而已）。
        return 0.0
    return round(read / denominator, 4)


def estimated_share(estimated: int | None, total: int | None) -> float:
    """该数字里有多少是字符估算而非 provider 上报。

    这里可以放心夹到 1.0、也不需要 None：estimated_tokens 与 total_tokens 由
    ledger.record 在同一个事务里用同一份 TokenUsage 写入，estimated 天然是 total 的
    子集，比值 > 1 只能是我们自己写入侧的 bug，而不是跨口径的历史数据。与
    cache_hit_rate 返回 None 的不对称是刻意的，不是漏写。
    """
    denominator = int(total or 0)
    if denominator <= 0:
        return 0.0
    return round(min(int(estimated or 0) / denominator, 1.0), 4)


__all__ = ["cache_hit_rate", "estimated_share"]
