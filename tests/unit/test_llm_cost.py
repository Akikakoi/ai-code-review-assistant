"""成本计算口径。

单独成文件的原因：成本是这个项目里**唯一会被拿来做决策、却完全没法肉眼校验**的数字 ——
summary、预算守卫、评估报告的成本维度都读它。算错不会报错，只会让所有结论失真。

重点覆盖缓存命中价：MiMo-V2.5-Pro 的命中价是 ¥0.025 / 1M，未命中是 ¥3.00 / 1M，
**相差 120 倍**。而"重复 push 场景成本下降 ≥60%"（文档 §16 阶段三验收）
这件事完全命中在这条路径上。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from acra.engine.llm_client import compute_cost

#: MiMo-V2.5-Pro 国内定价（微元 / 1M tokens）
MTOK = 1_000_000
IN_MICROS = 3_000_000
OUT_MICROS = 6_000_000
CACHED_MICROS = 25_000


def _settings(**kw) -> SimpleNamespace:
    base = {
        "llm_input_price_micros_per_mtok": IN_MICROS,
        "llm_output_price_micros_per_mtok": OUT_MICROS,
        "llm_cached_input_price_micros_per_mtok": CACHED_MICROS,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_unpriced_records_zero_instead_of_guessing() -> None:
    unpriced = _settings(
        llm_input_price_micros_per_mtok=0,
        llm_output_price_micros_per_mtok=0,
        llm_cached_input_price_micros_per_mtok=0,
    )
    assert compute_cost(unpriced, prompt_tokens=1000, completion_tokens=1000) == 0


def test_uncached_input_and_output_at_full_price() -> None:
    # 1M 输入（全未命中）+ 1M 输出 = ¥3 + ¥6 = ¥9 = 9_000_000 微元
    assert compute_cost(_settings(), prompt_tokens=MTOK, completion_tokens=MTOK) == 9_000_000


def test_fully_cached_input_is_120x_cheaper() -> None:
    """这条是成本口径的核心：全命中的输入单价必须走缓存价。"""
    cost = compute_cost(
        _settings(), prompt_tokens=MTOK, completion_tokens=0, cached_tokens=MTOK
    )
    assert cost == CACHED_MICROS

    full = compute_cost(_settings(), prompt_tokens=MTOK, completion_tokens=0)
    assert full == IN_MICROS
    assert full / cost == 120  # 与定价表的 3.00 / 0.025 一致


def test_partially_cached_input_splits_between_two_prices() -> None:
    # 0.5M 命中 + 0.5M 未命中 = 12500 + 1500000
    cost = compute_cost(
        _settings(), prompt_tokens=MTOK, completion_tokens=0, cached_tokens=MTOK // 2
    )
    assert cost == 1_500_000 + 12_500


def test_missing_cached_price_falls_back_to_full_price() -> None:
    """没配缓存价时必须**高估**而不是低估 —— 低估会让预算守卫失效。"""
    settings = _settings(llm_cached_input_price_micros_per_mtok=0)
    cost = compute_cost(
        settings, prompt_tokens=MTOK, completion_tokens=0, cached_tokens=MTOK
    )
    assert cost == IN_MICROS


@pytest.mark.parametrize(("prompt", "cached"), [(-5, -5), (100, 999), (0, 0)])
def test_absurd_cache_counts_never_produce_negative_cost(prompt: int, cached: int) -> None:
    """供应商回包异常（cached > prompt 或负数）不能让成本变成负数。"""
    cost = compute_cost(
        _settings(), prompt_tokens=prompt, completion_tokens=0, cached_tokens=cached
    )
    assert cost >= 0


def test_cost_scales_with_tokens_not_with_repo_size() -> None:
    """文档 §10.1：成本必须只与变更规模相关。这条用成本函数锁住这个性质。"""
    small = compute_cost(_settings(), prompt_tokens=10_000, completion_tokens=1_000)
    large = compute_cost(_settings(), prompt_tokens=20_000, completion_tokens=1_000)
    assert large > small
    # 输入翻倍，成本增量恰好等于一倍输入价（无隐藏的规模项）
    assert large - small == IN_MICROS * 10_000 // MTOK
