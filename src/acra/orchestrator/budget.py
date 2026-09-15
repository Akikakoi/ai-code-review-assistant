"""预算守卫与熔断。

对应开发文档 §4.2：

| 条件                        | 动作                                                   |
| --------------------------- | ------------------------------------------------------ |
| 变更行数 > 3000             | 只审高风险文件，并在 summary 中说明降级原因              |
| 变更文件数 > 80             | 同上，并跳过 L3 上下文                                  |
| 预估 token 成本 > 单次预算  | 降级模型档位；仍超则只跑静态分析模式                     |
| 当日累计成本 > 日预算       | 暂停自动审查，仅响应手动触发，并向管理员告警             |

前两条由 `analysis/risk_rules.apply_budget_guard` 执行（那里能同时看到文件清单），
这里负责 token 与成本维度的判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from acra.models import DiffSet

#: 每变更行折算的输入 token 经验值（L1 diff + L2 方法源码 + 提示词固定开销）
TOKENS_PER_CHANGED_LINE = 30
#: 每次审查的固定开销（系统提示、Schema、summary）
FIXED_OVERHEAD_TOKENS = 6_000


@dataclass(slots=True)
class BudgetDecision:
    allowed: bool = True
    static_only: bool = False
    downgrade_model: bool = False
    skip_l3: bool = False
    notes: list[str] = field(default_factory=list)
    estimated_input_tokens: int = 0


def estimate_input_tokens(diff_set: DiffSet) -> int:
    return FIXED_OVERHEAD_TOKENS + diff_set.total_added_lines * TOKENS_PER_CHANGED_LINE


def evaluate(
    settings,
    *,
    diff_set: DiffSet,
    daily_cost_micros: int = 0,
    run_budget_micros: int | None = None,
    priced: bool = True,
) -> BudgetDecision:
    """给出本次审查的预算决策。"""
    decision = BudgetDecision()
    decision.estimated_input_tokens = estimate_input_tokens(diff_set)

    if diff_set.total_added_lines > settings.acra_max_changed_lines:
        decision.skip_l3 = True

    if decision.estimated_input_tokens > settings.acra_total_input_token_budget:
        decision.downgrade_model = True
        decision.skip_l3 = True
        decision.notes.append(
            f"预估输入 {decision.estimated_input_tokens} tokens 超过单次预算 "
            f"{settings.acra_total_input_token_budget}，已降级模型档位"
        )
        # 降档后仍显著超预算（>1.5 倍）才退到纯静态模式，否则降档就够了
        if decision.estimated_input_tokens > settings.acra_total_input_token_budget * 1.5:
            decision.static_only = True
            decision.notes.append("降档后仍超预算上限 1.5 倍，退化为纯静态分析模式")

    daily_budget = settings.acra_daily_budget_micros
    if priced and daily_budget > 0 and daily_cost_micros >= daily_budget:
        decision.allowed = False
        decision.notes.append(
            f"当日累计成本 ¥{daily_cost_micros / 1_000_000:.4f}已达日预算 "
            f"¥{daily_budget / 1_000_000:.4f}，暂停自动审查，仅响应手动触发"
        )
    if (
        run_budget_micros is not None
        and priced
        and run_budget_micros > 0
        and daily_cost_micros + run_budget_micros > daily_budget > 0
    ):
        decision.skip_l3 = True
        decision.notes.append("接近日预算上限，本次跳过 L3 上下文")

    return decision


def truncate_chunks_to_budget(chunks: list, budget_tokens: int) -> tuple[list, str | None]:
    """全局预算耗尽时终止剩余分块（文档 §7.3 末段）。

    保留顺序靠前的块 —— 它们在 diff 中的位置更靠前，且通常已通过高风险文件优先排序。
    """
    kept: list = []
    used = 0
    for chunk in chunks:
        if used + chunk.token_estimate > budget_tokens:
            break
        kept.append(chunk)
        used += chunk.token_estimate
    if len(kept) == len(chunks):
        return chunks, None
    return (
        kept,
        f"全局 token 预算 {budget_tokens} 已耗尽，本次仅分析前 {len(kept)}/{len(chunks)} 个代码块",
    )
