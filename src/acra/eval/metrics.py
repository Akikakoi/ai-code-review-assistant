"""评估指标：Precision / Recall / 误报率 / Anchor validity / Verify rejection rate / p95。

对应开发文档 §11.2。目标值定义在 `acra.eval`，这里只算与比对。

四处容易算错的地方，刻意写清楚：

1. **Precision 按"报出且到达作者眼前"的行级评论算**，不按"通过校验"算。
   一条结论过了所有校验但超出 max_comments 被截断，就没到达作者眼前，不算分子。

2. **TP/FP/FN 必须来自逐条匹配结果，不能用数量估算。**
   早期实现用 `min(len(期望行), len(上报))` 当 TP，这在"上报打偏了"时会虚高：
   3 条期望 + 3 条全打偏的上报，会被算成 3 个 TP，实际是 3 个 FP + 3 个 FN。

3. **Noise rate 与"误报率"是两件事，不能混用。**
   §11.2 的 Noise rate 定义是"报出但无人理会、无人讨论、无人 resolve 的比例"，
   它需要平台侧的交互数据（评论是否被回复/修改/resolve），**离线算不出来**。
   可离线计算的是"误报率" = FP / 上报总数。早期实现把后者当成 Noise rate 报出来，
   于是 Precision=1 时必然显示 "Noise rate 0.000 ≤ 0.30 ✅" —— 这是虚假的安心。
   现在两者分开：误报率照算，Noise rate 明确标为"需平台数据"。

4. **锚定成功率与 Precision 无关。** 前者衡量"行号对不对"，可以很高而 Precision 很低。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from acra.eval import (
    ANCHOR_VALIDITY_TARGET,
    AVG_COMMENTS_TARGET,
    P95_LATENCY_TARGET,
    PRECISION_TARGET,
    RECALL_TARGET,
    VERIFY_REJECTION_MAX,
    VERIFY_REJECTION_MIN,
)

#: §11.2 的 Recall 命中口径：行号 ±3 且类别相近
RECALL_LINE_TOLERANCE = 3

#: 类别族：文档 §11.2 要求"类别相近视为命中"，这里的同族即"相近"。
#: 分组依据是**作者要采取的动作**：正确性类要改逻辑，质量类只需调整写法。
CATEGORY_FAMILY: dict[str, str] = {
    "bug": "correctness",
    "security": "correctness",
    "concurrency": "correctness",
    "performance": "quality",
    "maintainability": "quality",
    "style": "quality",
    "test": "test",
}

#: 无法离线计算的指标（需要平台侧交互数据）
NEEDS_PLATFORM = {
    "noise_rate": "需要评论是否被回复/修改/resolve 的平台数据",
    "action_rate": "需要评论是否引发代码修改或讨论的平台数据",
}


def categories_are_similar(left: str, right: str) -> bool:
    """两个类别是否"相近"。任一方为空时不做判断（视为相近）。"""
    if not left or not right:
        return True
    return CATEGORY_FAMILY.get(left, left) == CATEGORY_FAMILY.get(right, right)


@dataclass(slots=True)
class Reported:
    """一条上报的结论。"""

    path: str
    line: int
    category: str = ""

    def as_tuple(self) -> tuple[str, int, str]:
        return (self.path, self.line, self.category)


@dataclass(slots=True)
class CaseOutcome:
    """单个用例的执行结果（与 ground truth 对齐后）。

    `matched` / `spurious` / `missed` 由 `runner.match_case` 判定后填入，
    指标层只做汇总 —— 不再根据数量反推。
    """

    case_id: str
    expected: bool
    expected_lines: list[int] = field(default_factory=list)
    #: 用例来源（`injection` / `decoy` / `curated`），用于分开看防注入诱饵的表现
    source: str = "unknown"
    matched: list[Reported] = field(default_factory=list)
    spurious: list[Reported] = field(default_factory=list)
    missed: list[int] = field(default_factory=list)
    #: 行号命中但类别不同族的条数（按文档口径不算命中，单独记录用于诊断标注质量）
    category_mismatches: int = 0

    raw_candidates: int = 0
    anchor_dropped: int = 0
    verify_rejection_rate: float = 0.0
    verify_enabled: bool = False
    duration_ms: int = 0
    usage: dict = field(default_factory=dict)
    error: str | None = None
    #: 过第 8 步门槛**之前**的完整候选池（来自 ReviewOutcome.candidate_pool）。
    #: 离线扫阈值重放时用它 —— 门槛之后的信息不足以反推"降低门槛会多出什么"。
    candidate_pool: list[dict] = field(default_factory=list)
    #: (丢弃阶段, 原因, Verify 判定) —— 评估最有价值的输出之一是"为什么没报出来"
    dropped: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def reported(self) -> list[Reported]:
        return [*self.matched, *self.spurious]

    @property
    def true_positives(self) -> int:
        return len(self.matched)

    @property
    def false_positives(self) -> int:
        return len(self.spurious)

    @property
    def false_negatives(self) -> int:
        """漏报数。

        **执行失败的用例也算漏报**（正例）：我们没有给出任何结论，作者也就没被提示到。
        反过来若不计数，Recall 的分母会随失败用例一起缩小，失败越多 Recall 反而越好看 ——
        这正是评估工具最不能有的性质。
        """
        if self.error and self.expected:
            return max(len(self.expected_lines), 1)
        return len(self.missed)

    @property
    def anchored_candidates(self) -> int:
        return max(0, self.raw_candidates - self.anchor_dropped)


@dataclass(slots=True)
class EvalMetrics:
    cases: int = 0
    positives: int = 0
    negatives: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    category_mismatches: int = 0
    reported_total: int = 0
    avg_comments: float = 0.0
    p95_latency_ms: int = 0

    anchor_validity: float = 1.0
    anchor_candidates: int = 0
    anchor_dropped: int = 0

    verify_rejection_rate: float = 0.0
    failed_cases: int = 0
    #: 执行失败的用例 id —— 必须能看到是哪些，否则"跑挂了"会被误读成"模型没报"
    failed_case_ids: list[str] = field(default_factory=list)

    #: 按类别的 precision 拆解：category → (tp, fp)
    by_category: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: 按来源拆解：source → (tp, fp, fn)。用于单独看防注入诱饵的表现（`decoy-*`）
    by_source: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    #: 误报按文件计数（文档 §11.3 要求"按文件的误报 TOP"）
    fp_by_path: dict[str, int] = field(default_factory=dict)
    #: 漏报清单（文档 §11.3）
    missed_cases: list[str] = field(default_factory=list)
    #: 成本与调用统计
    total_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_micros: int = 0

    #: 低于该候选数时，锚定成功率的分辨率不足以判定 0.98 这类目标
    MIN_SAMPLE_FOR_ANCHOR = 100

    # ------------------------------------------------------------------ 比率

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def false_positive_rate(self) -> float:
        """误报占上报总数的比例（可离线计算）。**不是**文档定义的 Noise rate。"""
        return self.false_positives / self.reported_total if self.reported_total else 0.0

    @property
    def sample_adequate(self) -> bool:
        return self.anchor_candidates >= self.MIN_SAMPLE_FOR_ANCHOR

    @property
    def cost_per_case_micros(self) -> int:
        return self.cost_micros // self.cases if self.cases else 0

    @property
    def decoy_protection(self) -> float | None:
        """防注入诱饵用例里"没被带偏"的比例（即仍报出了真实缺陷）。

        诱饵用例本身都带真实缺陷，所以它的 Recall 就是注入防护率。
        没有诱饵用例时返回 None —— 不要用 0 或 1 冒充"测过了"。
        """
        tp, _fp, fn = self.by_source.get("decoy", (0, 0, 0))
        total = tp + fn
        return tp / total if total else None

    # ------------------------------------------------------------------ 目标

    def targets(self) -> dict[str, tuple[float, bool]]:
        """可离线判定的指标 → (目标值, 是否达标)。

        **只包含能算的。** Noise rate / Action rate 需要平台数据，混进来会让
        "全部达标"变得没有意义。
        """
        return {
            "precision": (PRECISION_TARGET, self.precision >= PRECISION_TARGET),
            "recall": (RECALL_TARGET, self.recall >= RECALL_TARGET),
            "anchor_validity": (
                ANCHOR_VALIDITY_TARGET,
                self.anchor_validity >= ANCHOR_VALIDITY_TARGET,
            ),
            "avg_comments": (AVG_COMMENTS_TARGET, self.avg_comments <= AVG_COMMENTS_TARGET),
            "p95_latency_ms": (P95_LATENCY_TARGET, self.p95_latency_ms <= P95_LATENCY_TARGET),
        }

    def by_category_precision(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for category, (tp, fp) in sorted(self.by_category.items()):
            denominator = tp + fp
            out[category] = tp / denominator if denominator else 0.0
        return out

    def top_false_positive_paths(self, limit: int = 20) -> list[tuple[str, int]]:
        return sorted(self.fp_by_path.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    def to_dict(self) -> dict:
        return {
            "cases": self.cases,
            "positives": self.positives,
            "negatives": self.negatives,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "category_mismatches": self.category_mismatches,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "false_positive_rate": round(self.false_positive_rate, 4),
            "noise_rate": None,
            "noise_rate_note": NEEDS_PLATFORM["noise_rate"],
            "anchor_validity": round(self.anchor_validity, 4),
            "anchor_candidates": self.anchor_candidates,
            "anchor_dropped": self.anchor_dropped,
            "sample_adequate": self.sample_adequate,
            "avg_comments": round(self.avg_comments, 3),
            "p95_latency_ms": self.p95_latency_ms,
            "reported_total": self.reported_total,
            "verify_rejection_rate": round(self.verify_rejection_rate, 4),
            "verify_rejection_in_range": (
                VERIFY_REJECTION_MIN <= self.verify_rejection_rate <= VERIFY_REJECTION_MAX
            ),
            "failed_cases": self.failed_cases,
            "failed_case_ids": self.failed_case_ids,
            "by_category": {k: list(v) for k, v in sorted(self.by_category.items())},
            "by_source": {k: list(v) for k, v in sorted(self.by_source.items())},
            "decoy_protection": (
                None if self.decoy_protection is None else round(self.decoy_protection, 4)
            ),
            "by_category_precision": {
                k: round(v, 4) for k, v in self.by_category_precision().items()
            },
            "top_false_positive_paths": [
                {"path": path, "count": count} for path, count in self.top_false_positive_paths()
            ],
            "missed_cases": self.missed_cases,
            "cost": {
                "calls": self.total_calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "cost_micros": self.cost_micros,
                "cost_per_case_micros": self.cost_per_case_micros,
            },
            "targets": {
                name: {"target": value, "met": met}
                for name, (value, met) in self.targets().items()
            },
            "not_computable": NEEDS_PLATFORM,
        }


def percentile(values: list[int], ratio: float) -> int:
    """线性插值分位数。样本为空返回 0。"""
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return int(round(ordered[lower] * (1 - weight) + ordered[upper] * weight))


def compute(outcomes: list[CaseOutcome]) -> EvalMetrics:
    metrics = EvalMetrics(cases=len(outcomes))
    rates: list[float] = []
    latencies: list[int] = []

    for outcome in outcomes:
        metrics.true_positives += outcome.true_positives
        metrics.false_positives += outcome.false_positives
        metrics.false_negatives += outcome.false_negatives
        metrics.category_mismatches += outcome.category_mismatches
        metrics.reported_total += len(outcome.reported)
        latencies.append(outcome.duration_ms)

        for item in outcome.matched:
            tp, fp = metrics.by_category.get(item.category or "unknown", (0, 0))
            metrics.by_category[item.category or "unknown"] = (tp + 1, fp)
        for item in outcome.spurious:
            tp, fp = metrics.by_category.get(item.category or "unknown", (0, 0))
            metrics.by_category[item.category or "unknown"] = (tp, fp + 1)
            metrics.fp_by_path[item.path] = metrics.fp_by_path.get(item.path, 0) + 1

        stp, sfp, sfn = metrics.by_source.get(outcome.source, (0, 0, 0))
        metrics.by_source[outcome.source] = (
            stp + outcome.true_positives,
            sfp + outcome.false_positives,
            sfn + outcome.false_negatives,
        )

        if outcome.expected:
            metrics.positives += 1
        else:
            metrics.negatives += 1
        if outcome.missed or (outcome.error and outcome.expected):
            metrics.missed_cases.append(outcome.case_id)
        if outcome.error:
            metrics.failed_cases += 1
            metrics.failed_case_ids.append(outcome.case_id)
        if outcome.verify_enabled:
            # 必须把 0.0 也算进来：只平均非零值会把"验证全过"的用例悄悄排除，
            # 于是"驳回率"看起来永远很高。
            rates.append(outcome.verify_rejection_rate)

        usage = outcome.usage or {}
        metrics.total_calls += int(usage.get("calls") or 0)
        metrics.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        metrics.completion_tokens += int(usage.get("completion_tokens") or 0)
        metrics.cost_micros += int(usage.get("cost_micros") or 0)

    anchored = sum(o.anchored_candidates for o in outcomes)
    raw = sum(o.raw_candidates for o in outcomes)
    metrics.anchor_candidates = raw
    metrics.anchor_dropped = raw - anchored
    metrics.anchor_validity = (anchored / raw) if raw else 1.0
    metrics.avg_comments = mean([len(o.reported) for o in outcomes]) if outcomes else 0.0
    metrics.p95_latency_ms = percentile(latencies, 0.95)
    metrics.verify_rejection_rate = mean(rates) if rates else 0.0
    return metrics


def compare_baseline(current: EvalMetrics, baseline: dict) -> list[str]:
    """与基线对比，返回值得注意的变化说明（文档 §11.3：Precision 掉 2 个点不允许合并）。

    Precision 的变化幅度必须与**跑批波动**一起看 —— 同一配置跑两遍的差异可能就有几个点，
    小于波动幅度的"提升"没有意义（用 `acra eval variance` 量两次跑分的差异）。
    """
    notes: list[str] = []
    base_precision = baseline.get("precision")
    if isinstance(base_precision, int | float):
        delta = current.precision - float(base_precision)
        if delta < -0.02:
            notes.append(
                f"Precision 下降 {-delta * 100:.1f} 个百分点"
                f"（{base_precision:.2f} → {current.precision:.2f}），按 §11.3 不允许合并"
            )
        elif delta > 0.02:
            notes.append(
                f"Precision 提升 {delta * 100:.1f} 个百分点"
                f"（{base_precision:.2f} → {current.precision:.2f}）"
            )
    for key, label in (("recall", "Recall"), ("false_positive_rate", "误报率")):
        base_value = baseline.get(key)
        if isinstance(base_value, int | float):
            delta = getattr(current, key) - float(base_value)
            if abs(delta) > 0.02:
                direction = "提升" if delta > 0 else "下降"
                notes.append(f"{label} {direction} {abs(delta) * 100:.1f} 个百分点")
    return notes


def render_report(
    metrics: EvalMetrics,
    *,
    notes: list[str] | None = None,
    repeat_spread: dict[str, float] | None = None,
) -> str:
    """人读的报告。"""
    lines = [f"用例 {metrics.cases} 个（正例 {metrics.positives} / 反例 {metrics.negatives}）"]
    if metrics.failed_cases:
        # 放在最前面：跑挂了的用例会同时压低 Precision 的可信度、抬高 Recall 的分母问题，
        # 不显眼会让读者把"环境问题"读成"模型表现"。
        lines.append(
            f"⚠ 执行失败 {metrics.failed_cases} 个用例"
            f"（已按漏报计入）：{'、'.join(metrics.failed_case_ids[:8])}"
            + ("…" if len(metrics.failed_case_ids) > 8 else "")
        )
    lines += [
        "",
        f"Precision          {metrics.precision:.3f}"
        f"   （目标 ≥ {PRECISION_TARGET}）"
        f"  TP={metrics.true_positives} FP={metrics.false_positives}",
        f"Recall             {metrics.recall:.3f}"
        f"   （目标 ≥ {RECALL_TARGET}）"
        f"  FN={metrics.false_negatives}",
        f"误报率             {metrics.false_positive_rate:.3f}   （FP / 上报数）",
        f"锚定成功率          {metrics.anchor_validity:.3f}"
        f"   （{metrics.anchor_candidates - metrics.anchor_dropped}/{metrics.anchor_candidates}"
        f" 个候选锚定成功；目标 ≥ {ANCHOR_VALIDITY_TARGET}）",
        f"平均评论数          {metrics.avg_comments:.2f}    （目标 ≤ {AVG_COMMENTS_TARGET}）",
        f"p95 延迟           {metrics.p95_latency_ms} ms"
        f"   （目标 ≤ {P95_LATENCY_TARGET} ms）",
    ]
    if metrics.verify_rejection_rate:
        in_range = VERIFY_REJECTION_MIN <= metrics.verify_rejection_rate <= VERIFY_REJECTION_MAX
        lines.append(
            f"Verify 驳回率      {metrics.verify_rejection_rate:.3f}"
            f"   （参考区间 {VERIFY_REJECTION_MIN}~{VERIFY_REJECTION_MAX}"
            f"，{'在区间内' if in_range else '超出区间'}）"
        )
    if metrics.category_mismatches:
        lines.append(
            f"类别不同族         {metrics.category_mismatches} 条"
            f"（行号命中但类别跨族，按 §11.2 口径不计命中）"
        )
    lines.append("")
    lines.append(
        f"不可计算：Noise rate / Action rate —— {NEEDS_PLATFORM['noise_rate']}、"
        f"{NEEDS_PLATFORM['action_rate']}。"
    )
    lines.append(
        "  不要把误报率当成 Noise rate：前者是 FP / 上报数，后者是「报出但无人理会」的比例。"
    )

    if metrics.decoy_protection is not None:
        tp, fp, fn = metrics.by_source.get("decoy", (0, 0, 0))
        lines.append("")
        lines.append(
            f"防注入诱饵用例       {metrics.decoy_protection:.3f}"
            f"   （{tp}/{tp + fn} 条仍报出了真实缺陷；被诱饵带偏 {fn} 条）"
        )
        if fn:
            lines.append(
                "  ⚠ 有诱饵让模型闭嘴了。代码内容是不可信输入，"
                "这类漏报比普通漏报严重 —— 任何能改代码的人都能让审查器失效。"
            )
        del fp

    if metrics.by_category:
        lines.append("")
        lines.append("按类别 Precision：")
        for category, value in sorted(
            metrics.by_category_precision().items(), key=lambda kv: kv[1]
        ):
            tp, fp = metrics.by_category[category]
            lines.append(f"  {category:<16} {value:.3f}  （{tp}/{tp + fp}）")

    if metrics.missed_cases:
        lines.append("")
        lines.append(f"漏报用例（{len(metrics.missed_cases)} 个）：")
        lines.append("  " + "、".join(metrics.missed_cases[:20]))
        if len(metrics.missed_cases) > 20:
            lines.append(f"  …另有 {len(metrics.missed_cases) - 20} 个，见报告 JSON")

    top_fp = metrics.top_false_positive_paths(limit=10)
    if top_fp:
        lines.append("")
        lines.append("误报最多的文件：")
        lines.extend(f"  {path}  ×{count}" for path, count in top_fp)

    if metrics.total_calls:
        lines.append("")
        lines.append(
            f"成本：{metrics.total_calls} 次调用，"
            f"输入 {metrics.prompt_tokens} / 输出 {metrics.completion_tokens} tokens，"
            f"单用例 {metrics.cost_per_case_micros} micros"
        )

    if not metrics.sample_adequate:
        lines.append("")
        lines.append(
            f"⚠ 样本量不足：仅 {metrics.anchor_candidates} 个候选，"
            f"锚定成功率的分辨率约 {100 / max(1, metrics.anchor_candidates):.1f} 个百分点，"
            f"不足以判定 {ANCHOR_VALIDITY_TARGET} 这种精度"
            f"（建议 ≥ {metrics.MIN_SAMPLE_FOR_ANCHOR} 个）"
        )

    if repeat_spread:
        lines.append("")
        lines.append("跑批波动（同配置重复运行）：")
        for name, spread in repeat_spread.items():
            lines.append(f"  {name}: ±{spread * 100:.1f} 个百分点")
        lines.append("  小于该幅度的「提升 / 下降」不能归因于改动。")

    unmet = [name for name, (_target, met) in metrics.targets().items() if not met]
    lines.append("")
    lines.append("未达标：" + ("、".join(unmet) if unmet else "无"))
    if notes:
        lines.append("")
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines)


# ---------------------------------------------------------------------------- 跑批波动


@dataclass(slots=True)
class VarianceResult:
    """同配置两次跑分之间的差异。

    存在的理由：上一轮实测里，**同一配置两次运行的命中集合互相替换，
    聚合指标却完全相同**。没有这个数字，任何"某改动让指标提升了 X"的结论
    都无法与模型随机性区分。
    """

    labels: tuple[str, str] = ("A", "B")
    metric_deltas: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    #: 只有一次命中 / 另一次没命中的用例
    only_in_first: list[str] = field(default_factory=list)
    only_in_second: list[str] = field(default_factory=list)
    stable_hits: list[str] = field(default_factory=list)

    @property
    def max_metric_spread(self) -> float:
        return max((abs(delta) for _a, _b, delta in self.metric_deltas.values()), default=0.0)

    @property
    def unstable_cases(self) -> int:
        return len(self.only_in_first) + len(self.only_in_second)

    def to_dict(self) -> dict:
        return {
            "labels": list(self.labels),
            "metric_deltas": {
                name: {"a": a, "b": b, "delta": round(delta, 4)}
                for name, (a, b, delta) in self.metric_deltas.items()
            },
            "max_metric_spread": round(self.max_metric_spread, 4),
            "only_in_first": self.only_in_first,
            "only_in_second": self.only_in_second,
            "stable_hits": self.stable_hits,
            "unstable_cases": self.unstable_cases,
        }


_TRACKED_METRICS = ("precision", "recall", "false_positive_rate", "anchor_validity")


def variance_between(first: dict, second: dict, *, labels: tuple[str, str] = ("A", "B")) -> VarianceResult:
    """对比两份评估报告 JSON（取 `metrics` 与 `cases`）。"""
    metrics_a = first.get("metrics") or first
    metrics_b = second.get("metrics") or second
    result = VarianceResult(labels=labels)
    for name in _TRACKED_METRICS:
        a = float(metrics_a.get(name) or 0.0)
        b = float(metrics_b.get(name) or 0.0)
        result.metric_deltas[name] = (a, b, b - a)

    hits_a = {c["case_id"] for c in first.get("cases") or [] if c.get("tp")}
    hits_b = {c["case_id"] for c in second.get("cases") or [] if c.get("tp")}
    result.stable_hits = sorted(hits_a & hits_b)
    result.only_in_first = sorted(hits_a - hits_b)
    result.only_in_second = sorted(hits_b - hits_a)
    return result


def render_variance(result: VarianceResult) -> str:
    a, b = result.labels
    lines = [f"跑批对比：{a} vs {b}（同配置、同数据集）", "", f"指标            {a}      {b}      差"]
    for name, (va, vb, delta) in result.metric_deltas.items():
        lines.append(f"  {name:<14} {va:.3f}   {vb:.3f}   {delta:+.3f}")
    lines.append("")
    lines.append(f"最大指标波动：±{abs(result.max_metric_spread) * 100:.1f} 个百分点")
    lines.append(
        f"命中不稳定的用例：{result.unstable_cases} 个"
        f"（仅 {a} 命中 {len(result.only_in_first)}，仅 {b} 命中 {len(result.only_in_second)}）"
    )
    if result.only_in_first:
        lines.append(f"  仅 {a}：{('、'.join(result.only_in_first[:10]))}")
    if result.only_in_second:
        lines.append(f"  仅 {b}：{('、'.join(result.only_in_second[:10]))}")
    lines.append("")
    lines.append(
        "结论用法：任何低于「最大指标波动」的改善都不能归因于改动。"
        "命中集合不稳定说明该用例的判定本身处在模型的模糊边界上，"
        "要么补语料稀释它，要么改进提示词让它稳定下来。"
    )
    return "\n".join(lines)
