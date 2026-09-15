"""评分、分级、排序与截断。

开发文档 §9.2（评分函数）与 §9.3（排序与截断）。

说明：§16 把 ranker 放在阶段二，但 CLI 输出必须有确定性排序，否则同一份 diff 两次
运行的评论顺序都可能不同、且会刷屏。因此阶段一先把 §9.2 / §9.3 完整实现，阶段二只需
接评估数据回调权重。

排序键（降序）：`score` → `severity_rank` → 变更行号升序。
截断规则：
- 前 `max_comments` 条（默认 5）作为行级评论；
- 其余按 `score ≥ 0.5` 的写入 summary 的折叠区，`< 0.5` 只落库不上报；
- 单文件最多 2 条行级评论（防止一个文件刷屏）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from acra.models import SEVERITY_BOOST, Finding, ReviewContext, clamp

SUMMARY_THRESHOLD = 0.5
PER_FILE_MAX = 2

EVIDENCE_BONUS = 0.15
RISK_CATEGORY_BONUS = 0.10
HUMAN_JUDGMENT_PENALTY = 0.15
STYLE_PENALTY = 0.20
DUP_PENALTY_UNIT = 0.10


def score_finding(
    finding: Finding,
    *,
    risk_categories: set[str] | None = None,
    dup_count: int = 0,
) -> float:
    """文档 §9.2 的评分函数。"""
    risk_categories = risk_categories or set()
    s = finding.confidence
    s += SEVERITY_BOOST.get(finding.severity, 0.0)
    s += EVIDENCE_BONUS if finding.evidence_rule_ids else 0.0
    s += RISK_CATEGORY_BONUS if finding.category in risk_categories else 0.0
    s -= HUMAN_JUDGMENT_PENALTY if finding.needs_human_judgment else 0.0
    s -= STYLE_PENALTY if finding.category == "style" else 0.0
    s -= DUP_PENALTY_UNIT * max(0, dup_count)
    return clamp(s, 0.0, 1.0)


def assign_scores(findings: list[Finding], ctx: ReviewContext | None = None) -> list[Finding]:
    """就地写入 score 并返回同一列表（保持调用方顺序无关）。

    最后统一乘上 `score_penalty`：它表达"这条结论值不值得占用展示位"，
    与 confidence（真值估计）是两件事，因此作用在 score 上而不是 confidence 上。
    """
    risk_categories = set(ctx.risk_categories) if ctx else set()
    counters = Counter((f.path, f.category) for f in findings)
    for finding in findings:
        dup_count = max(0, counters[(finding.path, finding.category)] - 1)
        score = score_finding(finding, risk_categories=risk_categories, dup_count=dup_count)
        penalty = getattr(finding, "score_penalty", 1.0) or 1.0
        finding.score = round(max(0.0, min(1.0, score * penalty)), 4)
    return findings


@dataclass(slots=True)
class RankResult:
    """排序结果。"""

    line_comments: list[Finding] = field(default_factory=list)
    summary_only: list[Finding] = field(default_factory=list)
    hidden: list[Finding] = field(default_factory=list)

    @property
    def all_reported(self) -> list[Finding]:
        return [*self.line_comments, *self.summary_only]

    @property
    def all_ranked(self) -> list[Finding]:
        return [*self.line_comments, *self.summary_only, *self.hidden]

    def counts_by_severity(self) -> dict[str, int]:
        return dict(Counter(f.severity for f in self.all_reported))


def order_and_truncate(
    findings: list[Finding],
    *,
    max_comments: int = 5,
    per_file_max: int = PER_FILE_MAX,
    summary_threshold: float = SUMMARY_THRESHOLD,
    ctx: ReviewContext | None = None,
) -> RankResult:
    """排序、分级并截断。"""
    assign_scores(findings, ctx)
    ordered = sorted(findings, key=lambda f: f.sort_key())

    result = RankResult()
    per_file: Counter[str] = Counter()

    for finding in ordered:
        if len(result.line_comments) < max_comments and per_file[finding.path] < per_file_max:
            if finding.score < summary_threshold:
                result.hidden.append(finding)
                continue
            result.line_comments.append(finding)
            per_file[finding.path] += 1
        elif finding.score >= summary_threshold:
            result.summary_only.append(finding)
        else:
            result.hidden.append(finding)

    return result
