"""离线扫置信度阈值（`ACRA_CONFIDENCE_THRESHOLD` 的校准）。

## 为什么需要它

评估集第一轮跑分暴露出的最大漏报来源就是第 8 步门槛：**7 个漏报里绝大多数
是"模型报出来了、但置信度没过 0.65"**。要回答"阈值该设多少"，直觉做法是
改配置重跑评估 —— 但那是 N 次完整的模型调用，既慢又要花钱，而且跑批间的
模型随机性会让每次的差异根本不可比。

正确做法是**跑一次、离线重放**：一次真实运行把"过门槛之前的完整候选池"
（含每条候选的 confidence / severity / category / 行号）落进报告，
之后扫阈值只是在这个池子上重新过滤 + 排序 + 截断 + 匹配 ground truth，
零额外模型成本，且**同一份数据**下各阈值严格可比。

## 一处明确的近似

排序与截断用运行时算好的 score（`Finding.score`），而 score 里的
`dup_count`（同文件同类别的候选数）会随阈值变化有轻微二阶影响。
因此扫描结果在 `max_comments` 附近的边界上可能与真实重跑略有差异。
这不影响"阈值提高会掉哪些、降低会多出哪些"这个主要结论 ——
受影响的只是"多出来的候选挤掉谁"的边界情况。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from acra.eval.dataset import EvalCase
from acra.eval.metrics import (
    CaseOutcome,
    EvalMetrics,
    Reported,
    compute,
)
from acra.eval.runner import match_case

#: 默认扫描的阈值序列：覆盖"完全不过滤"到"明显偏严"
DEFAULT_THRESHOLDS: tuple[float, ...] = (0.0, 0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80)


@dataclass(slots=True)
class SweepRow:
    """单个阈值下的指标。"""

    threshold: float
    metrics: EvalMetrics

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "cases": self.metrics.cases,
            "precision": round(self.metrics.precision, 4),
            "recall": round(self.metrics.recall, 4),
            "false_positive_rate": round(self.metrics.false_positive_rate, 4),
            "true_positives": self.metrics.true_positives,
            "false_positives": self.metrics.false_positives,
            "false_negatives": self.metrics.false_negatives,
            "avg_comments": round(self.metrics.avg_comments, 3),
            "reported_total": self.metrics.reported_total,
        }


@dataclass(slots=True)
class SweepReport:
    rows: list[SweepRow] = field(default_factory=list)
    #: 所有用例的候选总数（反映这份数据的统计力度）
    pool_size: int = 0
    cases_with_pool: int = 0

    def best_by(self, metric: str, *, minimum_recall: float = 0.0) -> SweepRow | None:
        """在满足最低 Recall 的前提下挑最优阈值。

        **只挑最大 Precision 没有意义** —— 阈值拉到 1.0 时 Precision 必然最高
        （一条都不报），所以要带 Recall 下限约束。
        """
        candidates = [r for r in self.rows if r.metrics.recall >= minimum_recall]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda r: (getattr(r.metrics, metric), -r.threshold),
        )

    def to_dict(self) -> dict:
        return {
            "pool_size": self.pool_size,
            "cases_with_pool": self.cases_with_pool,
            "rows": [row.to_dict() for row in self.rows],
        }


def _select_pool(case: EvalCase, pool: list[dict], threshold: float) -> list[Reported]:
    """按阈值从候选池里取出会进入输出的候选（含排序截断）。"""
    kept = [c for c in pool if float(c.get("confidence") or 0.0) >= threshold]
    # 用运行时算好的 score 排序；没有 score 时退化为按置信度排
    kept.sort(
        key=lambda c: (
            -float(c.get("score") or c.get("confidence") or 0.0),
            {
                "blocker": 0,
                "high": 1,
                "medium": 2,
                "low": 3,
                "nit": 4,
            }.get(str(c.get("severity") or "medium"), 2),
            int(c.get("line") or 0),
        )
    )
    return [
        Reported(str(c.get("path") or ""), int(c.get("line") or 0), str(c.get("category") or ""))
        for c in kept
    ]


def sweep(
    cases: list[EvalCase],
    outcomes: list[CaseOutcome],
    *,
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
    tolerance: int = 3,
    max_comments: int | None = None,
) -> SweepReport:
    """在既有跑分结果上扫描阈值，返回每个阈值下的指标。

    `max_comments` 传 `None` 表示不模拟截断（评的是"过门槛后的全部候选"）；
    传整数则模拟 ranker 的 top-K 截断，更接近作者实际看到的东西。
    """
    by_id = {case.case_id: case for case in cases}
    report = SweepReport()
    report.pool_size = sum(len(o.candidate_pool) for o in outcomes)
    report.cases_with_pool = sum(1 for o in outcomes if o.candidate_pool)

    for threshold in thresholds:
        rebuilt: list[CaseOutcome] = []
        for original in outcomes:
            case = by_id.get(original.case_id)
            if case is None or original.error:
                # 执行失败的用例在重放里保持失败，不参与指标
                rebuilt.append(
                    CaseOutcome(
                        case_id=original.case_id,
                        expected=original.expected,
                        error=original.error or "缺少用例定义，无法重放",
                    )
                )
                continue

            selected = _select_pool(case, original.candidate_pool, threshold)
            if max_comments is not None:
                selected = selected[:max_comments]

            result = match_case(
                case,
                expectations=case.expectations,
                expected_lines=original.expected_lines,
                reported=[r.as_tuple() for r in selected],
                tolerance=tolerance,
            )
            rebuilt.append(
                CaseOutcome(
                    case_id=original.case_id,
                    expected=original.expected,
                    expected_lines=original.expected_lines,
                    matched=result.matched,
                    spurious=result.spurious,
                    missed=result.missed,
                    category_mismatches=result.category_mismatches,
                    raw_candidates=original.raw_candidates,
                    anchor_dropped=original.anchor_dropped,
                    verify_enabled=original.verify_enabled,
                    verify_rejection_rate=original.verify_rejection_rate,
                    duration_ms=original.duration_ms,
                    usage=original.usage,
                )
            )
        report.rows.append(SweepRow(threshold=threshold, metrics=compute(rebuilt)))
    return report


def render_sweep(report: SweepReport, *, current_threshold: float | None = None) -> str:
    lines = [
        f"候选池共 {report.pool_size} 条（来自 {report.cases_with_pool}/{report.rows[0].metrics.cases if report.rows else 0} 个用例）",
        "",
        "阈值   Precision  Recall   误报率   TP   FP   FN   平均评论数",
    ]
    for row in report.rows:
        marker = "  ← 当前" if current_threshold is not None and abs(row.threshold - current_threshold) < 1e-9 else ""
        lines.append(
            f"{row.threshold:.2f}   {row.metrics.precision:>9.3f}  {row.metrics.recall:>6.3f}"
            f"  {row.metrics.false_positive_rate:>6.3f}  {row.metrics.true_positives:>3}"
            f"  {row.metrics.false_positives:>3}  {row.metrics.false_negatives:>3}"
            f"  {row.metrics.avg_comments:>10.2f}{marker}"
        )
    lines.append("")
    lines.append(
        "读法：阈值调低会换来 Recall、付出 Precision；调高反之。"
        "挑选阈值必须带 Recall 下限，否则「全不报」会拿到最高 Precision。"
    )
    return "\n".join(lines)
